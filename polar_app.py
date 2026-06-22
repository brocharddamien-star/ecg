#!/usr/bin/env python3
"""Polar H10 — Application graphique d'enregistrement ECG + rapport PDF automatique."""

import sys
import os
import logging
from datetime import datetime

try:
    from PyQt5.QtWidgets import (
        QApplication, QMainWindow, QWidget,
        QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFrame,
        QDoubleSpinBox, QCheckBox, QFormLayout, QGroupBox,
        QDialog, QProgressBar,
    )
    from PyQt5.QtCore import Qt, QTimer
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
    from bleak import BleakScanner, BleakClient  # noqa: F401 (vérifie la présence)
except ImportError:
    print("bleak requis : pip install bleak")
    sys.exit(1)

from theme import (
    C_PINK, C_NAVY, C_WHITE, C_GRAY, C_LGRAY,
    C_PURPLE, C_LPINK, C_GREEN, C_RED, C_ORANGE,
)
from ecg_signal import ECG_SAMPLES
from ble_worker import BLEWorker

# Imports PDF (optionnels)
try:
    import numpy as np          # noqa: F401
    import pandas as pd         # noqa: F401
    import matplotlib           # noqa: F401
    import scipy                # noqa: F401
    import reportlab            # noqa: F401
    from ecg_dialogs import PdfScopeDialog, EcgSelectorDialog, PdfWorker
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════════════════════
# INTERFACE GRAPHIQUE PRINCIPALE
# ═══════════════════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker      = BLEWorker()
        self._pdf_worker = None
        self._last_csv   = None
        self._connect_signals()
        self._build_ui()
        self.setWindowTitle("Polar H10 — Enregistreur ECG")
        self.setFixedSize(520, 780)
        self._timer = QTimer()
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._refresh_ecg)
        self._timer.start()

    # ── Connexion des signaux BLE ──────────────────────────────────────────────

    def _connect_signals(self):
        w = self.worker
        w.status_msg.connect(self._set_status)
        w.device_found.connect(
            lambda n: self._set_status(f"Capteur trouvé : {n}", C_LGRAY))
        w.connected_sig.connect(self._on_connected)
        w.disconnected_sig.connect(self._on_disconnected)
        w.hr_sig.connect(self._on_hr)
        w.ecg_count_sig.connect(self._on_ecg_count)
        w.ecg_ok_sig.connect(self._on_ecg_ok)
        w.file_sig.connect(self._on_file)
        w.recording_done.connect(self._on_recording_done)

    # ── Construction de l'interface ────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        central.setStyleSheet(f"background-color: {C_NAVY};")
        root = QVBoxLayout(central)
        root.setContentsMargins(24, 16, 24, 16)
        root.setSpacing(10)

        for text, style in [
            ("Polar H10", f"color:{C_WHITE}; font-size:22px; font-weight:700;"),
            ("Enregistreur ECG", f"color:{C_LPINK}; font-size:12px;"),
        ]:
            lbl = QLabel(text)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet(style)
            root.addWidget(lbl)

        root.addWidget(self._build_hr_card())
        root.addWidget(self._build_ecg_frame())

        if PDF_AVAILABLE:
            root.addWidget(self._build_pdf_box())
        else:
            self.chk_pdf = self.spin_window = self.chk_seuil = self.spin_seuil = None
            self.lbl_pdf = QLabel("PDF désactivé (numpy/pandas/reportlab manquants)")
            self.lbl_pdf.setStyleSheet(f"color:{C_LGRAY}; font-size:10px;")
            root.addWidget(self.lbl_pdf)

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

    def _build_hr_card(self):
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background-color: {C_PURPLE}; border-radius: 12px; }}")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(18, 12, 18, 12)
        cv.setSpacing(2)

        self.lbl_status = QLabel("Non connecté")
        self.lbl_status.setAlignment(Qt.AlignCenter)
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet(f"color:{C_LGRAY}; font-size:11px;")
        cv.addWidget(self.lbl_status)

        hr_row = QHBoxLayout()
        hr_row.setAlignment(Qt.AlignCenter)
        self.lbl_hr = QLabel("––")
        self.lbl_hr.setStyleSheet(f"color:{C_PINK}; font-size:56px; font-weight:700;")
        hr_row.addWidget(self.lbl_hr)
        lbl_bpm = QLabel("bpm")
        lbl_bpm.setStyleSheet(f"color:{C_LGRAY}; font-size:16px; padding-top:26px;")
        hr_row.addWidget(lbl_bpm)
        cv.addLayout(hr_row)

        self.lbl_rr = QLabel("")
        self.lbl_rr.setAlignment(Qt.AlignCenter)
        self.lbl_rr.setStyleSheet(f"color:{C_LGRAY}; font-size:11px;")
        cv.addWidget(self.lbl_rr)
        return card

    def _build_ecg_frame(self):
        ecg_frame = QFrame()
        ecg_frame.setStyleSheet(
            f"QFrame {{ background-color: {C_PURPLE}; border-radius: 12px; }}")
        ecg_fl = QVBoxLayout(ecg_frame)
        ecg_fl.setContentsMargins(0, 0, 0, 0)
        ecg_fl.setSpacing(0)

        self.ecg_plot = pg.PlotWidget()
        self.ecg_plot.setBackground(C_PURPLE)
        self.ecg_plot.setMinimumHeight(180)
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
        self.lbl_file.setStyleSheet(f"color:{C_LGRAY}; font-size:10px; font-style:italic;")
        info_row.addWidget(self.lbl_file)
        ecg_fl.addLayout(info_row)
        return ecg_frame

    def _build_pdf_box(self):
        pdf_box = QGroupBox("Rapport PDF")
        pdf_box.setStyleSheet(f"""
            QGroupBox {{
                color: {C_LPINK}; font-size: 11px; font-weight: 700;
                border: 1px solid {C_GRAY}; border-radius: 8px;
                margin-top: 6px; padding: 6px;
            }}
            QGroupBox::title {{ subcontrol-origin: margin; left: 10px; }}
        """)
        pdf_form = QFormLayout(pdf_box)
        pdf_form.setContentsMargins(8, 4, 8, 4)
        pdf_form.setSpacing(6)

        self.chk_pdf = QCheckBox("Générer automatiquement à la fin")
        self.chk_pdf.setChecked(True)
        self.chk_pdf.setStyleSheet(f"color:{C_WHITE}; font-size:11px;")
        pdf_form.addRow(self.chk_pdf)

        self.spin_window = QDoubleSpinBox()
        self.spin_window.setRange(5.0, 120.0)
        self.spin_window.setValue(15.0)
        self.spin_window.setSuffix(" s")
        self.spin_window.setStyleSheet(
            f"background:{C_PURPLE}; color:{C_WHITE}; border-radius:4px; padding:2px;")
        pdf_form.addRow(
            QLabel("Durée par tranche :", styleSheet=f"color:{C_LGRAY};font-size:10px;"),
            self.spin_window)

        seuil_row = QHBoxLayout()
        self.chk_seuil = QCheckBox("Seuil FC :")
        self.chk_seuil.setStyleSheet(f"color:{C_WHITE}; font-size:11px;")
        self.spin_seuil = QDoubleSpinBox()
        self.spin_seuil.setRange(40.0, 220.0)
        self.spin_seuil.setValue(100.0)
        self.spin_seuil.setSuffix(" bpm")
        self.spin_seuil.setEnabled(False)
        self.spin_seuil.setStyleSheet(
            f"background:{C_PURPLE}; color:{C_WHITE}; border-radius:4px; padding:2px;")
        self.chk_seuil.toggled.connect(self.spin_seuil.setEnabled)
        seuil_row.addWidget(self.chk_seuil)
        seuil_row.addWidget(self.spin_seuil)
        seuil_row.addStretch()
        pdf_form.addRow(seuil_row)

        self.lbl_pdf = QLabel("")
        self.lbl_pdf.setWordWrap(True)
        self.lbl_pdf.setStyleSheet(f"color:{C_LGRAY}; font-size:10px;")
        pdf_form.addRow(self.lbl_pdf)

        self.pdf_progress = QProgressBar()
        self.pdf_progress.setRange(0, 100)
        self.pdf_progress.setValue(0)
        self.pdf_progress.setTextVisible(True)
        self.pdf_progress.setFixedHeight(14)
        self.pdf_progress.setStyleSheet(
            f"QProgressBar {{ border:1px solid {C_GRAY}; border-radius:6px; "
            f"background:{C_LGRAY}; text-align:center; font-size:9px; color:{C_WHITE}; }}"
            f"QProgressBar::chunk {{ background:{C_PINK}; border-radius:5px; }}")
        self.pdf_progress.hide()
        pdf_form.addRow(self.pdf_progress)

        btn_open_csv = QPushButton("📂  Ouvrir un CSV existant → PDF")
        btn_open_csv.setMinimumHeight(34)
        btn_open_csv.setStyleSheet(
            f"QPushButton {{ background:{C_PURPLE}; color:{C_WHITE}; "
            f"border:1px solid {C_GRAY}; border-radius:7px; "
            f"font-size:11px; font-weight:600; padding:4px 12px; }}"
            f"QPushButton:hover {{ background:#5d2570; }}")
        btn_open_csv.clicked.connect(self._on_open_csv)
        pdf_form.addRow(btn_open_csv)
        return pdf_box

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

    # ── Rafraîchissement ECG ───────────────────────────────────────────────────

    def _refresh_ecg(self):
        buf = list(self.worker.ecg_buffer)
        if len(buf) < 2:
            return
        if not self._ecg_has_data:
            self._ecg_has_data = True
            self.ecg_plot.removeItem(self._ecg_wait)
        self.ecg_curve.setData(buf)
        self.ecg_plot.setXRange(0, ECG_SAMPLES, padding=0)

    # ── Gestionnaires de boutons ───────────────────────────────────────────────

    def _on_connect(self):
        self.btn_connect.setEnabled(False)
        self.btn_connect.setText("Recherche…")
        self.worker.scan_and_connect()

    def _on_start(self):
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.lbl_ecg.setText("ECG : enregistrement…")
        self.lbl_file.setText("")
        if self.lbl_pdf:
            self.lbl_pdf.setText("")
        self.worker.start_recording()

    def _on_stop(self):
        self.btn_stop.setEnabled(False)
        self.btn_start.setEnabled(True)
        self.lbl_ecg.setText("ECG : arrêté")
        self.worker.stop_recording()

    # ── Gestionnaires de signaux BLE ───────────────────────────────────────────

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
            self.lbl_ecg.setText("ECG non disponible")

    def _on_file(self, path):
        self._last_csv = path
        self.lbl_file.setText(os.path.basename(path))

    # ── Génération PDF ─────────────────────────────────────────────────────────

    def _on_recording_done(self, csv_path):
        if not PDF_AVAILABLE:
            return
        if self.chk_pdf and not self.chk_pdf.isChecked():
            return

        scope_dlg = PdfScopeDialog(self)
        if scope_dlg.exec_() != QDialog.Accepted:
            self.lbl_pdf.setText("Export PDF annulé")
            self.lbl_pdf.setStyleSheet(f"color:{C_LGRAY}; font-size:10px;")
            return

        time_ranges = None
        if scope_dlg.choice == "select":
            sel_dlg = EcgSelectorDialog(csv_path, self)
            if sel_dlg.exec_() != QDialog.Accepted:
                self.lbl_pdf.setText("Export PDF annulé")
                self.lbl_pdf.setStyleSheet(f"color:{C_LGRAY}; font-size:10px;")
                return
            time_ranges = sel_dlg.get_selected_ranges()
            if not time_ranges:
                return

        window_s = self.spin_window.value() if self.spin_window else 15.0
        bpm_thr  = (self.spin_seuil.value()
                    if self.chk_seuil and self.chk_seuil.isChecked()
                    else None)

        self.lbl_pdf.setText("⏳ Génération du PDF en cours…")
        self.lbl_pdf.setStyleSheet(f"color:{C_ORANGE}; font-size:10px;")
        self.pdf_progress.setValue(0)
        self.pdf_progress.show()

        self._pdf_worker = PdfWorker(csv_path, bpm_thr, window_s, time_ranges)
        self._pdf_worker.progress.connect(lambda m: self.lbl_pdf.setText(f"⏳ {m}"))
        self._pdf_worker.progress_pct.connect(self.pdf_progress.setValue)
        self._pdf_worker.finished.connect(self._on_pdf_done)
        self._pdf_worker.error.connect(self._on_pdf_error)
        self._pdf_worker.start()

    def _on_open_csv(self):
        from PyQt5.QtWidgets import QFileDialog
        data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        csv_path, _ = QFileDialog.getOpenFileName(
            self, "Choisir un fichier CSV", data_dir,
            "Fichiers CSV (*.csv);;Tous les fichiers (*)")
        if csv_path:
            self._on_recording_done(csv_path)

    def _on_pdf_done(self, pdf_path):
        self.lbl_pdf.setText(f"✓ PDF : {os.path.basename(pdf_path)}")
        self.lbl_pdf.setStyleSheet(f"color:{C_GREEN}; font-size:10px;")
        self.pdf_progress.hide()
        logging.info(f"[pdf] Rapport généré : {pdf_path}")

    def _on_pdf_error(self, msg):
        self.lbl_pdf.setText(f"✗ Erreur PDF : {msg}")
        self.lbl_pdf.setStyleSheet(f"color:{C_RED}; font-size:10px;")
        self.pdf_progress.hide()
        logging.error(f"[pdf] Erreur : {msg}")

    def closeEvent(self, event):
        self._timer.stop()
        self.worker.request_disconnect()
        event.accept()


# ═══════════════════════════════════════════════════════════════════════════════
# POINT D'ENTRÉE
# ═══════════════════════════════════════════════════════════════════════════════

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

    try:
        import dbus  # noqa: F401
    except ImportError:
        logging.warning("dbus-python non installé : sudo apt install python3-dbus")

    if not PDF_AVAILABLE:
        logging.warning(
            "Librairies PDF manquantes — installez avec :\n"
            "pip install numpy pandas matplotlib scipy reportlab")

    app = QApplication(sys.argv)
    app.setFont(QFont("Montserrat", 10))
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())
