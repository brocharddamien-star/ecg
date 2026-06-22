"""Worker BLE Polar H10 et écriture CSV."""

import asyncio
import csv
import logging
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from io import BytesIO
import os

from PyQt5.QtCore import QObject, pyqtSignal

from bleak import BleakScanner, BleakClient
from bleak.exc import BleakError

from theme import C_RED, C_LGRAY, C_GREEN
from ecg_signal import ECG_SAMPLES

# ── UUIDs et commandes BLE ─────────────────────────────────────────────────────
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
PMD_CONTROL    = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA       = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"
ECG_START_CMD  = bytearray([0x02, 0x00, 0x00, 0x01, 0x82, 0x00, 0x01, 0x01, 0x0E, 0x00])
ECG_START_133  = bytearray([0x02, 0x00, 0x00, 0x01, 0x85, 0x00, 0x01, 0x01, 0x0E, 0x00])
ECG_STOP_CMD   = bytearray([0x03, 0x00])
POLAR_KEYWORDS = ("polar", "h10")


def parse_hr(data: bytearray) -> tuple:
    """Décode un paquet BLE Heart Rate Measurement."""
    flags = data[0]
    hr = int.from_bytes(data[1:3], "little") if (flags & 0x01) else data[1]
    offset = 3 if (flags & 0x01) else 2
    if flags & 0x08:
        offset += 2
    rr_ms = 0
    if flags & 0x10 and len(data) >= offset + 2:
        rr_ms = round(int.from_bytes(data[offset:offset + 2], "little") * 1000 / 1024)
    return hr, rr_ms


def dbus_pair_and_trust(address: str, log) -> bool:
    """Appaire et marque un appareil Bluetooth comme de confiance via D-Bus."""
    try:
        import dbus
        bus      = dbus.SystemBus()
        dev_path = "/org/bluez/hci0/dev_" + address.replace(":", "_")
        dev_obj  = bus.get_object("org.bluez", dev_path)
        props    = dbus.Interface(dev_obj, "org.freedesktop.DBus.Properties")
        if bool(props.Get("org.bluez.Device1", "Paired")):
            log("[pair] Déjà appairé")
            try:
                props.Set("org.bluez.Device1", "Trusted", dbus.Boolean(True))
            except Exception:
                pass
            return True
        log("[pair] Lancement appairage D-Bus…")
        dbus.Interface(dev_obj, "org.bluez.Device1").Pair()
        log("[pair] Appairage OK")
        props.Set("org.bluez.Device1", "Trusted", dbus.Boolean(True))
        log("[pair] Trusted = True")
        return True
    except Exception as e:
        log(f"[pair] Erreur : {e}")
        return False


class CSVWriter:
    """Écrit les données ECG, HR et RR dans un fichier CSV."""

    def __init__(self, path: str):
        self.path        = path
        self.ecg_mode    = False
        self._f          = open(path, "w", newline="", encoding="utf-8")
        self._w          = csv.writer(self._f)
        self._pending_hr = None
        self._pending_rr = None
        self._w.writerow(["time", "ecg", "hr", "rr", "marker"])

    def write_ecg(self, samples: list, ts_ns: int, fs: int = 130):
        dt_ns = 1_000_000_000 // fs
        for i, s in enumerate(samples):
            self._w.writerow([
                ts_ns + i * dt_ns,
                round(s / 1000, 6),
                self._pending_hr if i == 0 else "",
                self._pending_rr if i == 0 else "",
                "",
            ])
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


class BLEWorker(QObject):
    """Gère la connexion BLE au Polar H10 dans un thread asyncio dédié."""

    status_msg       = pyqtSignal(str, str)
    device_found     = pyqtSignal(str)
    connected_sig    = pyqtSignal(str)
    disconnected_sig = pyqtSignal()
    hr_sig           = pyqtSignal(int, int)
    ecg_count_sig    = pyqtSignal(int)
    ecg_ok_sig       = pyqtSignal(bool)
    file_sig         = pyqtSignal(str)
    recording_done   = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._loop = self._client = self._writer = None
        self._recording = self._connected = self._running = False
        self._sample_count = 0
        self._stop_evt = self._ctrl_evt = None
        self._ctrl_resp = b""
        self._time_offset = None
        self.ecg_buffer = deque(maxlen=ECG_SAMPLES)

    # ── API publique ───────────────────────────────────────────────────────────

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

    # ── Thread asyncio ─────────────────────────────────────────────────────────

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
        p = lambda m: (print(m, flush=True), logging.info(m))

        self.status_msg.emit("Recherche du Polar H10…", C_LGRAY)
        devices = await BleakScanner.discover(timeout=6.0)
        polar = next(
            (d for d in devices
             if d.name and any(k in d.name.lower() for k in POLAR_KEYWORDS)),
            None)
        if not polar:
            self.status_msg.emit(
                "Polar H10 non trouvé — vérifiez que le capteur est allumé.", C_RED)
            self._running = False
            return

        address = polar.address
        self.device_found.emit(polar.name or address)
        p(f"[ble] Trouvé : {polar.name}  {address}")

        subprocess.run(["bluetoothctl", "disconnect", address],
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
                    raw  = await client.read_gatt_char(
                        "00002a00-0000-1000-8000-00805f9b34fb")
                    name = raw.decode("utf-8", errors="ignore")
                except Exception:
                    pass
                p(f"[ble] Connecté : {name}  {address}")

                self.status_msg.emit("Appairage BLE…", C_LGRAY)
                paired = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: dbus_pair_and_trust(address, p))
                if paired:
                    self.status_msg.emit(f"✓  Connecté — {name}", C_GREEN)
                    await asyncio.sleep(0.5)
                else:
                    self.status_msg.emit("Non appairé — PMD_DATA peut échouer", C_LGRAY)
                self.connected_sig.emit(name)

                async def _hr_cb(_, data: bytearray):
                    hr, rr = parse_hr(data)
                    p(f"[hr] {hr} bpm  RR={rr} ms")
                    self.hr_sig.emit(hr, rr)
                    if not self._recording or not self._writer:
                        return
                    if self._writer.ecg_mode:
                        self._writer.set_pending_hr(hr, rr)
                    else:
                        self._writer.write_hr_only(hr, rr)

                await client.start_notify(HR_MEASUREMENT, _hr_cb,
                                          bluez={"use_start_notify": True})
                p("[ble] HR notify OK")

                def _ctrl_cb(_, data: bytearray):
                    p(f"[pmd ctrl←] {data.hex()}")
                    self._ctrl_resp = bytes(data)
                    self._ctrl_evt.set()

                ctrl_ok = False
                for attempt in range(1, 4):
                    try:
                        await asyncio.wait_for(
                            client.start_notify(PMD_CONTROL, _ctrl_cb,
                                                bluez={"use_start_notify": True}),
                            timeout=8.0)
                        p(f"[ble] PMD_CTRL OK (tentative {attempt})")
                        ctrl_ok = True
                        break
                    except asyncio.TimeoutError:
                        p(f"[ble] PMD_CTRL timeout {attempt}/3")
                        await asyncio.sleep(1.5)
                    except Exception as e:
                        p(f"[ble] PMD_CTRL erreur {attempt}/3 : {e}")
                        try:
                            await client.stop_notify(PMD_CONTROL)
                        except Exception:
                            pass
                        await asyncio.sleep(1.5)

                if not ctrl_ok:
                    self.status_msg.emit("PMD_CTRL inaccessible", C_RED)
                    self.ecg_ok_sig.emit(False)
                    await self._stop_evt.wait()
                    return

                _n_data = [0]

                def _data_cb(_, data: bytearray):
                    _n_data[0] += 1
                    n = _n_data[0]
                    if n <= 10 or n % 130 == 0:
                        b9 = f"{data[9]:#04x}" if len(data) > 9 else "?"
                        p(f"[pmd data #{n}] len={len(data)} "
                          f"b0={data[0]:#04x} b9={b9} hex={data[:16].hex()}")
                    if len(data) < 10 or data[0] != 0x00:
                        return
                    if data[9] not in (0x00, 0x01):
                        return
                    ts_raw = int.from_bytes(data[1:9], "little")
                    if self._time_offset is None:
                        self._time_offset = time.time_ns() - ts_raw
                    ts = ts_raw + self._time_offset
                    samples = [
                        int.from_bytes(data[o:o+3], "little", signed=True)
                        for o in range(10, len(data), 3)]
                    if not samples:
                        return
                    if len(self.ecg_buffer) == 0:
                        p(f"[ecg] *** 1er paquet *** {len(samples)} éch.")
                    for s in samples:
                        self.ecg_buffer.append(s / 1000.0)
                    if self._recording and self._writer:
                        self._sample_count += len(samples)
                        self._writer.ecg_mode = True
                        self._writer.write_ecg(samples, ts)
                        self.ecg_count_sig.emit(self._sample_count)

                data_ok = False
                for attempt, usn in enumerate([False, True], start=1):
                    label  = "use_start_notify" if usn else "AcquireNotify"
                    kwargs = ({"bluez": {"use_start_notify": True}} if usn else {})
                    p(f"[ble] PMD_DATA start_notify ({label})…")
                    try:
                        await asyncio.wait_for(
                            client.start_notify(PMD_DATA, _data_cb, **kwargs),
                            timeout=10.0)
                        p(f"[ble] PMD_DATA OK ({label})")
                        data_ok = True
                        break
                    except asyncio.TimeoutError:
                        p(f"[ble] PMD_DATA timeout ({label})")
                    except Exception as e:
                        p(f"[ble] PMD_DATA erreur ({label}) : {e}")
                        try:
                            await client.stop_notify(PMD_DATA)
                        except Exception:
                            pass
                    await asyncio.sleep(1.0)

                if not data_ok:
                    self.status_msg.emit(
                        "PMD_DATA bloqué — appairez le H10 (bouton 7 s)", C_RED)
                    self.ecg_ok_sig.emit(False)
                    await self._stop_evt.wait()
                    return

                await asyncio.sleep(0.8)

                p("[pmd] >>> GET_SETTINGS")
                await self._pmd_send(client, bytearray([0x01, 0x00]), "GET_SETTINGS")
                await asyncio.sleep(0.5)

                cmds = [ECG_START_CMD, ECG_START_133, ECG_START_CMD]
                ecg_started = False
                for attempt in range(1, 4):
                    p(f"[pmd] >>> STOP ({attempt}/3)")
                    err_stop, _ = await self._pmd_send(
                        client, ECG_STOP_CMD, f"STOP({attempt})")
                    await asyncio.sleep(2.0 + (attempt - 1) * 1.5)
                    cmd = cmds[attempt - 1]
                    hz  = 133 if cmd is ECG_START_133 else 130
                    p(f"[pmd] >>> START ECG {hz}Hz ({attempt}/3)")
                    err_start, resp = await self._pmd_send(client, cmd, f"START({attempt})")
                    p(f"[pmd] START réponse : {resp.hex() if resp else 'vide'}")
                    if err_start == 0:
                        p(f"[ecg] *** START OK {hz}Hz ***")
                        self.ecg_ok_sig.emit(True)
                        ecg_started = True
                        break
                    if err_stop == 6 and err_start == 6:
                        self.status_msg.emit("H10 bloqué — bouton 7 s", C_RED)
                        self.ecg_ok_sig.emit(False)
                        break
                    p(f"[pmd] err={err_start} — retry")

                if not ecg_started:
                    p("[ecg] ÉCHEC démarrage ECG")

                async def _watchdog():
                    for _ in range(24):
                        await asyncio.sleep(10)
                        p(f"[watchdog] buf={len(self.ecg_buffer)} pkts={_n_data[0]}")
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

    # ── Envoi PMD ─────────────────────────────────────────────────────────────

    async def _pmd_send(self, client, cmd, label, timeout=8.0):
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

    # ── Gestion de l'enregistrement ───────────────────────────────────────────

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
            path = self._writer.path
            self._writer.close()
            self._writer = None
            self.recording_done.emit(path)
