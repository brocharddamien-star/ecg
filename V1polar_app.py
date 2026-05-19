#!/usr/bin/env python3
"""Polar H10 — Application graphique d'enregistrement ECG."""

import sys
import os
import csv
import time
import asyncio
import logging
import threading
import subprocess
from collections import deque
from datetime import datetime

try:
    from PyQt5.QtWidgets import (
        QApplication, QMainWindow, QWidget,
        QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFrame,
    )
    from PyQt5.QtCore import Qt, QObject, QTimer, pyqtSignal
    from PyQt5.QtGui import QFont
except ImportError:
    print("PyQt5 requis : pip install PyQt5")
    sys.exit(1)

try:
    import pyqtgraph as pg
except ImportError:
    print("pyqtgraph requis : pip install pyqtgraph")
    sys.exit(1)

try:
    from bleak import BleakScanner, BleakClient
    from bleak.exc import BleakError
except ImportError:
    print("bleak requis : pip install bleak")
    sys.exit(1)

# ── Palette ───────────────────────────────────────────────────────────────────
C_PINK   = "#c6168d"
C_NAVY   = "#162142"
C_WHITE  = "#ffffff"
C_GRAY   = "#585a5e"
C_LGRAY  = "#a8acb2"
C_PURPLE = "#4c1e58"
C_LPINK  = "#f7cae2"
C_GREEN  = "#4caf50"
C_RED    = "#f44336"

ECG_WINDOW_S = 5
ECG_FS       = 130
ECG_SAMPLES  = ECG_WINDOW_S * ECG_FS   # 650

# ── BLE / Polar ───────────────────────────────────────────────────────────────
HR_MEASUREMENT  = "00002a37-0000-1000-8000-00805f9b34fb"
PMD_CONTROL     = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA        = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"
ECG_START_CMD   = bytearray([0x02, 0x00, 0x00, 0x01, 0x82, 0x00, 0x01, 0x01, 0x0E, 0x00])
ECG_STOP_CMD    = bytearray([0x03, 0x00])
POLAR_KEYWORDS  = ("polar", "h10")


def _parse_hr(data: bytearray) -> tuple[int, int]:
    flags = data[0]
    hr = int.from_bytes(data[1:3], "little") if (flags & 0x01) else data[1]
    offset = 3 if (flags & 0x01) else 2
    if flags & 0x08:
        offset += 2
    rr_ms = 0
    if flags & 0x10 and len(data) >= offset + 2:
        rr_raw = int.from_bytes(data[offset:offset + 2], "little")
        rr_ms = round(rr_raw * 1000 / 1024)
    return hr, rr_ms


# ── CSV Writer ────────────────────────────────────────────────────────────────
class CSVWriter:
    def __init__(self, path: str):
        self.path      = path
        self.ecg_mode  = False
        self._f        = open(path, "w", newline="", encoding="utf-8")
        self._w        = csv.writer(self._f)
        self._pending_hr = None
        self._pending_rr = None
        self._w.writerow(["time", "ecg", "hr", "rr", "marker"])

    def write_ecg(self, samples: list, ts_ns: int, fs: int = 130):
        dt_ns = 1_000_000_000 // fs
        for i, s in enumerate(samples):
            hr = self._pending_hr if i == 0 else ""
            rr = self._pending_rr if i == 0 else ""
            self._w.writerow([ts_ns + i * dt_ns, round(s / 1000, 6), hr, rr, ""])
        self._pending_hr = None
        self._pending_rr = None
        self._f.flush()

    def write_hr_only(self, hr: int, rr: int):
        self._w.writerow([time.time_ns(), "", hr, rr if rr else "", ""])
        self._f.flush()

    def set_pending_hr(self, hr: int, rr: int):
        self._pending_hr = hr
        self._pending_rr = rr if rr else ""

    def close(self):
        self._f.close()


# ── BLE Worker ────────────────────────────────────────────────────────────────
class BLEWorker(QObject):
    status_msg       = pyqtSignal(str, str)
    device_found     = pyqtSignal(str)
    connected_sig    = pyqtSignal(str)
    disconnected_sig = pyqtSignal()
    hr_sig           = pyqtSignal(int, int)
    ecg_count_sig    = pyqtSignal(int)
    ecg_ok_sig       = pyqtSignal(bool)
    file_sig         = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: BleakClient | None = None
        self._writer: CSVWriter | None = None
        self._recording     = False
        self._sample_count  = 0
        self._connected     = False
        self._running       = False
        self._stop_evt: asyncio.Event | None = None
        self._ctrl_evt: asyncio.Event | None = None
        self._ctrl_resp: bytes = b""
        self._time_offset: int | None = None
        self.ecg_buffer: deque[float] = deque(maxlen=ECG_SAMPLES)

    def scan_and_connect(self):
        if self._running:
            return
        self._running = True
        threading.Thread(target=self._thread_main, daemon=True).start()

    def start_recording(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._begin_recording)

    def stop_recording(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._end_recording)

    def request_disconnect(self):
        if self._loop and self._stop_evt:
            self._loop.call_soon_threadsafe(self._stop_evt.set)

    def _thread_main(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        except Exception as e:
            self.status_msg.emit(f"Erreur : {e}", C_RED)
        finally:
            self._loop.close()
            self._loop    = None
            self._running = False

    async def _run(self):
        self.status_msg.emit("Recherche du Polar H10…", C_LGRAY)
        devices = await BleakScanner.discover(timeout=6.0)
        polar = next(
            (d for d in devices
             if d.name and any(k in d.name.lower() for k in POLAR_KEYWORDS)),
            None,
        )
        if not polar:
            self.status_msg.emit(
                "Polar H10 non trouvé — vérifiez que le capteur est allumé.", C_RED)
            self._running = False
            return

        address = polar.address
        self.device_found.emit(polar.name or address)
        self.status_msg.emit("Nettoyage bond BLE…", C_LGRAY)
        # Supprimer AVANT de connecter (pas après le scan) pour éviter
        # que BlueZ garde un état GATT cache qui bloque StartNotify sur PMD_CONTROL
        subprocess.run(["bluetoothctl", "disconnect", address],
                       capture_output=True, timeout=5)
        await asyncio.sleep(0.5)
        subprocess.run(["bluetoothctl", "remove", address],
                       capture_output=True, timeout=5)
        await asyncio.sleep(1.5)

        self._stop_evt    = asyncio.Event()
        self._ctrl_evt    = asyncio.Event()
        self._time_offset = None

        def on_disconnect(_):
            if not self._connected:
                return
            self._connected = False
            self.disconnected_sig.emit()
            self._stop_evt.set()

        self.status_msg.emit(f"Connexion à {polar.name}…", C_LGRAY)
        try:
            async with BleakClient(address,
                                   disconnected_callback=on_disconnect,
                                   timeout=30.0) as client:
                self._client    = client
                self._connected = True

                name = polar.name or address
                try:
                    raw = await client.read_gatt_char(
                        "00002a00-0000-1000-8000-00805f9b34fb")
                    name = raw.decode("utf-8", errors="ignore")
                except Exception:
                    pass
                self.connected_sig.emit(name)

                def _p(msg):
                    """Print + log partout."""
                    print(msg, flush=True)
                    logging.info(msg)

                _p(f"[ble] Connecté : {name}  {address}")

                async def _hr_cb(_, data: bytearray):
                    hr, rr = _parse_hr(data)
                    _p(f"[hr] {hr} bpm  RR={rr} ms")
                    self.hr_sig.emit(hr, rr)
                    if not self._recording or not self._writer:
                        return
                    if self._writer.ecg_mode:
                        self._writer.set_pending_hr(hr, rr)
                    else:
                        self._writer.write_hr_only(hr, rr)

                await client.start_notify(
                    HR_MEASUREMENT, _hr_cb,
                    bluez={"use_start_notify": True})
                _p("[ble] HR notify OK")

                def _ctrl_cb(_, data: bytearray):
                    _p(f"[pmd ctrl←] {data.hex()}")
                    self._ctrl_resp = bytes(data)
                    self._ctrl_evt.set()

                _n_data = [0]  # compteur paquets PMD_DATA

                def _data_cb(_, data: bytearray):
                    _n_data[0] += 1
                    n = _n_data[0]
                    # Log les 10 premiers + 1 sur 130 ensuite
                    if n <= 10 or n % 130 == 0:
                        _p(f"[pmd data #{n}] len={len(data)} "
                           f"b0={data[0]:#04x} b9={data[9]:#04x if len(data)>9 else '?'} "
                           f"hex={data[:16].hex()}")
                    if len(data) < 10:
                        _p(f"[ecg] REJETÉ #{n} trop court len={len(data)}")
                        return
                    if data[0] != 0x00:
                        _p(f"[ecg] REJETÉ #{n} type={data[0]:#04x} attendu=0x00")
                        return
                    if data[9] != 0x00:
                        _p(f"[ecg] REJETÉ #{n} frame_type={data[9]:#04x} attendu=0x00")
                        return
                    ts_raw = int.from_bytes(data[1:9], "little")
                    if self._time_offset is None:
                        self._time_offset = time.time_ns() - ts_raw
                    ts = ts_raw + self._time_offset
                    samples = [
                        int.from_bytes(data[o:o + 3], "little", signed=True)
                        for o in range(10, len(data), 3)
                    ]
                    if not samples:
                        _p(f"[ecg] REJETÉ #{n} aucun sample extrait")
                        return
                    first = len(self.ecg_buffer) == 0
                    for s in samples:
                        self.ecg_buffer.append(s / 1000.0)
                    if first:
                        _p(f"[ecg] *** 1er paquet valide *** "
                           f"{len(samples)} éch. val[0]={samples[0]} µV")
                    if n <= 10 or n % 130 == 0:
                        _p(f"[ecg] buf={len(self.ecg_buffer)} total_pkts={n}")
                    if self._recording and self._writer:
                        self._sample_count += len(samples)
                        self._writer.ecg_mode = True
                        self._writer.write_ecg(samples, ts)
                        self.ecg_count_sig.emit(self._sample_count)

                # PMD_CONTROL = INDICATE → pas de use_start_notify (cause timeout BlueZ)
                _p("[ble] start_notify PMD_CTRL (indicate)…")
                try:
                    await asyncio.wait_for(
                        client.start_notify(PMD_CONTROL, _ctrl_cb),
                        timeout=10.0)
                    _p("[ble] PMD_CTRL notify OK")
                except asyncio.TimeoutError:
                    _p("[ble] *** PMD_CTRL TIMEOUT — essai avec use_start_notify ***")
                    try:
                        await asyncio.wait_for(
                            client.start_notify(PMD_CONTROL, _ctrl_cb,
                                                bluez={"use_start_notify": True}),
                            timeout=10.0)
                        _p("[ble] PMD_CTRL notify OK (fallback use_start_notify)")
                    except Exception as exc2:
                        _p(f"[ble] *** PMD_CTRL ÉCHEC définitif : {exc2} ***")
                except Exception as exc:
                    _p(f"[ble] *** PMD_CTRL ERREUR : {type(exc).__name__}: {exc} ***")

                # PMD_DATA = NOTIFY → use_start_notify pour éviter AcquireNotify
                _p("[ble] start_notify PMD_DATA (notify)…")
                try:
                    await asyncio.wait_for(
                        client.start_notify(PMD_DATA, _data_cb,
                                            bluez={"use_start_notify": True}),
                        timeout=10.0)
                    _p("[ble] PMD_DATA notify OK")
                except asyncio.TimeoutError:
                    _p("[ble] *** PMD_DATA TIMEOUT ***")
                except Exception as exc:
                    _p(f"[ble] *** PMD_DATA ERREUR : {type(exc).__name__}: {exc} ***")

                await asyncio.sleep(0.4)

                # Démarrage ECG — 3 tentatives
                _p("[pmd] >>> GET_SETTINGS")
                await self._pmd_send(client, bytearray([0x01, 0x00]), "GET_SETTINGS")
                await asyncio.sleep(0.3)

                ecg_started = False
                for attempt in range(1, 4):
                    _p(f"[pmd] >>> STOP (tentative {attempt}/3)")
                    err_stop, _ = await self._pmd_send(
                        client, ECG_STOP_CMD, f"STOP({attempt})")
                    wait_s = 2.0 + (attempt - 1) * 2
                    _p(f"[pmd] attente {wait_s:.0f}s…")
                    await asyncio.sleep(wait_s)
                    _p(f"[pmd] >>> START ECG (tentative {attempt}/3)")
                    err_start, _ = await self._pmd_send(
                        client, ECG_START_CMD, f"START({attempt})")
                    if err_start == 0:
                        _p(f"[ecg] *** START OK tentative {attempt} — attente données ***")
                        self.ecg_ok_sig.emit(True)
                        ecg_started = True
                        break
                    if err_stop == 6 and err_start == 6:
                        _p("[ecg] *** H10 ZOMBIE STOP+START err=6 — reset bouton 7s ***")
                        self.status_msg.emit(
                            "H10 bloqué — réinitialisez le capteur (bouton 7 s)",
                            C_RED)
                        self.ecg_ok_sig.emit(False)
                        break
                    _p(f"[pmd] START({attempt}) err={err_start} — retry")

                if not ecg_started:
                    _p("[ecg] *** ÉCHEC démarrage ECG après 3 tentatives ***")

                # Watchdog : log toutes les 10s si toujours pas de données
                async def _watchdog():
                    for _ in range(12):  # 2 minutes max
                        await asyncio.sleep(10)
                        _p(f"[watchdog] buf={len(self.ecg_buffer)} "
                           f"pkts={_n_data[0]} recording={self._recording}")
                        if self._stop_evt.is_set():
                            break

                asyncio.ensure_future(_watchdog())

                await self._stop_evt.wait()

                if self._recording:
                    self._end_recording()
                try:
                    await self._pmd_send(client, ECG_STOP_CMD, "STOP(final)")
                except Exception:
                    pass

        except (BleakError, OSError) as e:
            self.status_msg.emit(f"Erreur BLE : {e}", C_RED)
        finally:
            self._connected = False
            if self._writer:
                self._writer.close()
                self._writer = None
            self._client = None

    async def _pmd_send(self, client, cmd: bytearray,
                        label: str, timeout: float = 8.0) -> tuple[int, bytes]:
        self._ctrl_evt.clear()
        await client.write_gatt_char(PMD_CONTROL, cmd, response=True)
        try:
            await asyncio.wait_for(self._ctrl_evt.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logging.warning(f"[pmd] {label} → TIMEOUT")
            return -1, b""
        r   = self._ctrl_resp
        err = r[3] if len(r) >= 4 else -2
        logging.info(f"[pmd] {label} → {r.hex()}  err={err}")
        return err, r

    def _begin_recording(self):
        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(data_dir, exist_ok=True)
        path = os.path.join(data_dir, f"polar_h10_{ts}.csv")
        self._writer       = CSVWriter(path)
        self._sample_count = 0
        self._recording    = True
        self.file_sig.emit(path)

    def _end_recording(self):
        self._recording = False
        if self._writer:
            self._writer.close()
            self._writer = None


# ── Interface graphique ───────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker = BLEWorker()
        self._connect_signals()
        self._build_ui()
        self.setWindowTitle("Polar H10 — Enregistreur ECG")
        self.setFixedSize(520, 680)

        self._timer = QTimer()
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._refresh_ecg)
        self._timer.start()

    def _connect_signals(self):
        w = self.worker
        w.status_msg.connect(self._set_status)
        w.device_found.connect(lambda n: self._set_status(f"Capteur trouvé : {n}", C_LGRAY))
        w.connected_sig.connect(self._on_connected)
        w.disconnected_sig.connect(self._on_disconnected)
        w.hr_sig.connect(self._on_hr)
        w.ecg_count_sig.connect(self._on_ecg_count)
        w.ecg_ok_sig.connect(self._on_ecg_ok)
        w.file_sig.connect(self._on_file)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        central.setStyleSheet(f"background-color: {C_NAVY};")

        root = QVBoxLayout(central)
        root.setContentsMargins(24, 16, 24, 16)
        root.setSpacing(10)

        # Titre
        lbl_title = QLabel("Polar H10")
        lbl_title.setAlignment(Qt.AlignCenter)
        lbl_title.setStyleSheet(
            f"color:{C_WHITE}; font-size:22px; font-weight:700;")
        root.addWidget(lbl_title)

        lbl_sub = QLabel("Enregistreur ECG")
        lbl_sub.setAlignment(Qt.AlignCenter)
        lbl_sub.setStyleSheet(f"color:{C_LPINK}; font-size:12px;")
        root.addWidget(lbl_sub)

        # ── Carte BPM ────────────────────────────────────────────────────────
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background-color: {C_PURPLE}; border-radius: 12px; }}")
        card_v = QVBoxLayout(card)
        card_v.setContentsMargins(18, 12, 18, 12)
        card_v.setSpacing(2)

        self.lbl_status = QLabel("Non connecté")
        self.lbl_status.setAlignment(Qt.AlignCenter)
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet(f"color:{C_LGRAY}; font-size:11px;")
        card_v.addWidget(self.lbl_status)

        hr_row = QHBoxLayout()
        hr_row.setAlignment(Qt.AlignCenter)
        self.lbl_hr = QLabel("––")
        self.lbl_hr.setStyleSheet(
            f"color:{C_PINK}; font-size:56px; font-weight:700;")
        hr_row.addWidget(self.lbl_hr)
        lbl_bpm = QLabel("bpm")
        lbl_bpm.setStyleSheet(
            f"color:{C_LGRAY}; font-size:16px; padding-top:26px;")
        hr_row.addWidget(lbl_bpm)
        card_v.addLayout(hr_row)

        self.lbl_rr = QLabel("")
        self.lbl_rr.setAlignment(Qt.AlignCenter)
        self.lbl_rr.setStyleSheet(f"color:{C_LGRAY}; font-size:11px;")
        card_v.addWidget(self.lbl_rr)

        root.addWidget(card)

        # ── Graphe ECG ───────────────────────────────────────────────────────
        ecg_frame = QFrame()
        ecg_frame.setStyleSheet(
            f"QFrame {{ background-color: {C_PURPLE}; border-radius: 12px; }}")
        ecg_fl = QVBoxLayout(ecg_frame)
        ecg_fl.setContentsMargins(0, 0, 0, 0)
        ecg_fl.setSpacing(0)

        self.ecg_plot = pg.PlotWidget()
        self.ecg_plot.setBackground(C_PURPLE)
        self.ecg_plot.setMinimumHeight(200)
        self.ecg_plot.hideAxis("left")
        self.ecg_plot.hideAxis("bottom")
        self.ecg_plot.setMouseEnabled(x=False, y=False)
        self.ecg_plot.setMenuEnabled(False)
        self.ecg_plot.setXRange(0, ECG_SAMPLES, padding=0)
        self.ecg_plot.enableAutoRange(axis='y')
        self.ecg_plot.addLine(y=0, pen=pg.mkPen(C_GRAY, width=1, style=Qt.DotLine))
        self.ecg_curve = self.ecg_plot.plot(pen=pg.mkPen(C_PINK, width=1.5))

        self._ecg_wait = pg.TextItem(
            "En attente du signal ECG…", color=C_LGRAY, anchor=(0.5, 0.5))
        self.ecg_plot.addItem(self._ecg_wait)
        self._ecg_wait.setPos(ECG_SAMPLES / 2, 0)
        self._ecg_has_data = False

        ecg_fl.addWidget(self.ecg_plot)

        info_row = QHBoxLayout()
        info_row.setContentsMargins(12, 4, 12, 8)
        self.lbl_ecg = QLabel("ECG : –")
        self.lbl_ecg.setStyleSheet(f"color:{C_LGRAY}; font-size:10px;")
        info_row.addWidget(self.lbl_ecg)
        info_row.addStretch()
        self.lbl_file = QLabel("")
        self.lbl_file.setStyleSheet(
            f"color:{C_LGRAY}; font-size:10px; font-style:italic;")
        info_row.addWidget(self.lbl_file)
        ecg_fl.addLayout(info_row)

        root.addWidget(ecg_frame)

        # ── Boutons ──────────────────────────────────────────────────────────
        self.btn_connect = self._btn("Connexion", C_PINK, C_WHITE)
        self.btn_connect.clicked.connect(self._on_connect)
        root.addWidget(self.btn_connect)

        row = QHBoxLayout()
        row.setSpacing(12)
        self.btn_start = self._btn("▶  Début", C_WHITE, C_NAVY)
        self.btn_start.setEnabled(False)
        self.btn_start.clicked.connect(self._on_start)
        row.addWidget(self.btn_start)

        self.btn_stop = self._btn("■  Fin", C_GRAY, C_WHITE)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop)
        row.addWidget(self.btn_stop)
        root.addLayout(row)

    def _btn(self, text, bg, fg):
        b = QPushButton(text)
        b.setMinimumHeight(44)
        b.setStyleSheet(f"""
            QPushButton {{
                background-color: {bg}; color: {fg};
                border: none; border-radius: 10px;
                font-size: 14px; font-weight: 700; padding: 6px 16px;
            }}
            QPushButton:disabled {{ background-color: #2a3550; color: {C_GRAY}; }}
        """)
        return b

    def _refresh_ecg(self):
        buf = list(self.worker.ecg_buffer)
        n = len(buf)
        if n < 2:
            return
        if not self._ecg_has_data:
            self._ecg_has_data = True
            self.ecg_plot.removeItem(self._ecg_wait)
        self.ecg_curve.setData(buf)
        self.ecg_plot.setXRange(0, ECG_SAMPLES, padding=0)

    def _on_connect(self):
        self.btn_connect.setEnabled(False)
        self.btn_connect.setText("Recherche…")
        self.worker.scan_and_connect()

    def _on_start(self):
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.lbl_ecg.setText("ECG : enregistrement…")
        self.lbl_file.setText("")
        self.worker.start_recording()

    def _on_stop(self):
        self.btn_stop.setEnabled(False)
        self.btn_start.setEnabled(True)
        self.lbl_ecg.setText("ECG : arrêté")
        self.worker.stop_recording()

    def _set_status(self, msg, color=C_LGRAY):
        self.lbl_status.setText(msg)
        self.lbl_status.setStyleSheet(f"color:{color}; font-size:11px;")

    def _on_connected(self, name):
        self._set_status(f"✓  Connecté — {name}", C_GREEN)
        self.btn_connect.setText("Connecté")
        self.btn_start.setEnabled(True)

    def _on_disconnected(self):
        self._set_status("Déconnecté", C_LGRAY)
        self.lbl_hr.setText("––")
        self.lbl_rr.setText("")
        self.btn_connect.setEnabled(True)
        self.btn_connect.setText("Reconnexion")
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(False)
        if self._ecg_has_data:
            self._ecg_has_data = False
            self.ecg_curve.setData([])
            self.ecg_plot.addItem(self._ecg_wait)

    def _on_hr(self, hr, rr):
        self.lbl_hr.setText(str(hr))
        self.lbl_rr.setText(f"RR : {rr} ms" if rr else "")

    def _on_ecg_count(self, n):
        self.lbl_ecg.setText(f"ECG : {n:,} échantillons")

    def _on_ecg_ok(self, ok):
        if not ok:
            self.lbl_ecg.setText("ECG non disponible (reset H10 requis)")

    def _on_file(self, path):
        self.lbl_file.setText(os.path.basename(path))

    def closeEvent(self, event):
        self._timer.stop()
        self.worker.request_disconnect()
        event.accept()


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _app_dir  = os.path.dirname(os.path.abspath(__file__))
    _logs_dir = os.path.join(_app_dir, "logs")
    os.makedirs(_logs_dir, exist_ok=True)
    _log_file = os.path.join(
        _logs_dir, datetime.now().strftime("polar_%Y%m%d_%H%M%S.log"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(_log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ])
    logging.info(f"Log : {_log_file}")

    app = QApplication(sys.argv)
    app.setFont(QFont("Montserrat", 10))
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())
