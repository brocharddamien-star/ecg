#!/usr/bin/env python3
"""
ECG Launcher — interface graphique pour ecg_to_pdf.py
Dépendance : pip install PyQt5
Lancer     : python ecg_launcher.py
"""

import sys
import os
import subprocess
import platform
import threading

try:
    from PyQt5.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QLineEdit, QPushButton, QSlider, QSpinBox, QCheckBox,
        QFileDialog, QTextEdit, QFrame, QSizePolicy, QMessageBox,
        QScrollArea, QGroupBox
    )
    from PyQt5.QtCore import Qt, QThread, pyqtSignal, QObject
    from PyQt5.QtGui import QFont, QColor, QPalette, QTextCursor
except ImportError:
    print("PyQt5 non trouvé. Installez-le avec :\n  pip install PyQt5")
    sys.exit(1)

# ── Palette brand ─────────────────────────────────────────────────────────────
PINK   = "#c6168d"
NAVY   = "#162142"
PURPLE = "#4c1e58"
LIGHT  = "#ebf0f7"
PINK_L = "#f7cae2"
GRAY   = "#585a5e"
GRAY_L = "#a8acb2"
WHITE  = "#ffffff"

STYLE = f"""
QWidget {{
    font-family: 'Montserrat', 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
    font-size: 13px;
    color: {GRAY};
    background: {LIGHT};
}}
QMainWindow, QScrollArea, QWidget#root {{
    background: {LIGHT};
}}

/* ── Cards ── */
QGroupBox {{
    background: {WHITE};
    border: 1px solid {GRAY_L};
    border-radius: 8px;
    margin-top: 18px;
    padding: 12px 14px 14px 14px;
    font-size: 11px;
    font-weight: bold;
    color: {GRAY_L};
    letter-spacing: 0.06em;
    text-transform: uppercase;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 6px;
    left: 10px;
    color: {GRAY_L};
}}

/* ── Inputs ── */
QLineEdit, QSpinBox {{
    background: {LIGHT};
    border: 1px solid {GRAY_L};
    border-radius: 5px;
    padding: 6px 10px;
    color: {NAVY};
    selection-background-color: {PINK};
}}
QLineEdit:focus, QSpinBox:focus {{
    border-color: {PINK};
}}

/* ── Sliders ── */
QSlider::groove:horizontal {{
    height: 4px;
    background: {PINK_L};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {PINK};
    border: none;
    width: 16px;
    height: 16px;
    margin: -6px 0;
    border-radius: 8px;
}}
QSlider::sub-page:horizontal {{
    background: {PINK};
    border-radius: 2px;
}}

/* ── Buttons ── */
QPushButton {{
    background: {LIGHT};
    border: 1px solid {GRAY_L};
    border-radius: 5px;
    padding: 7px 16px;
    color: {NAVY};
    font-weight: 500;
}}
QPushButton:hover  {{ background: {PINK_L}; border-color: {PINK}; }}
QPushButton:pressed {{ background: {PINK}; color: {WHITE}; }}
QPushButton:disabled {{ background: {LIGHT}; color: {GRAY_L}; }}

QPushButton#primary {{
    background: {PINK};
    color: {WHITE};
    border: none;
    font-weight: bold;
    padding: 9px 22px;
}}
QPushButton#primary:hover   {{ background: {PURPLE}; }}
QPushButton#primary:pressed {{ background: {NAVY};   }}
QPushButton#primary:disabled {{ background: {GRAY_L}; color: {WHITE}; }}

/* ── Checkbox ── */
QCheckBox {{
    spacing: 8px;
    color: {GRAY};
}}
QCheckBox::indicator {{
    width: 18px; height: 18px;
    border: 1px solid {GRAY_L};
    border-radius: 4px;
    background: {WHITE};
}}
QCheckBox::indicator:checked {{
    background: {PINK};
    border-color: {PINK};
    image: none;
}}

/* ── Log ── */
QTextEdit#log {{
    background: {NAVY};
    color: #c8ffd4;
    font-family: 'Courier New', monospace;
    font-size: 12px;
    border: none;
    border-radius: 5px;
    padding: 8px;
}}

/* ── Badge label ── */
QLabel#badge {{
    background: {PINK_L};
    color: {PINK};
    border-radius: 4px;
    padding: 3px 10px;
    font-weight: bold;
    min-width: 52px;
}}
QLabel#title {{
    font-size: 20px;
    font-weight: bold;
    color: {WHITE};
}}
QLabel#subtitle {{
    font-size: 12px;
    color: {GRAY_L};
}}
"""


# ── Worker thread ─────────────────────────────────────────────────────────────

class Worker(QObject):
    line    = pyqtSignal(str, str)   # (text, tag)
    done    = pyqtSignal(int, str)   # (returncode, pdf_path)

    def __init__(self, cmd, pdf_path):
        super().__init__()
        self.cmd      = cmd
        self.pdf_path = pdf_path

    def run(self):
        try:
            proc = subprocess.Popen(
                self.cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, bufsize=1
            )
            for raw in proc.stdout:
                txt = raw.rstrip()
                tag = ("err"  if any(w in txt.lower()
                                     for w in ("error","erreur","traceback","exception"))
                       else "ok" if "✅" in txt
                       else "")
                self.line.emit(txt, tag)
            proc.wait()
            self.done.emit(proc.returncode, self.pdf_path)
        except Exception as ex:
            self.line.emit(f"Erreur : {ex}", "err")
            self.done.emit(1, "")


# ── Main window ───────────────────────────────────────────────────────────────

class ECGLauncher(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Générateur de rapport ECG")
        self.setMinimumSize(660, 760)
        self.setStyleSheet(STYLE)
        self._thread = None
        self._worker = None
        self._build_ui()

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("root")
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ── Header ──────────────────────────────────────────────────────────
        header = QWidget()
        header.setStyleSheet(f"background:{NAVY}; padding: 18px 24px;")
        hl = QVBoxLayout(header)
        hl.setSpacing(4)
        t = QLabel("Rapport ECG")
        t.setObjectName("title")
        t.setStyleSheet(f"font-size:22px;font-weight:bold;color:{WHITE};background:transparent;")
        s = QLabel("Interface de génération de rapport médical PDF")
        s.setObjectName("subtitle")
        s.setStyleSheet(f"font-size:12px;color:{GRAY_L};background:transparent;")
        hl.addWidget(t)
        hl.addWidget(s)
        root_layout.addWidget(header)

        # ── Scroll area ──────────────────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        body = QWidget()
        body.setObjectName("root")
        bl = QVBoxLayout(body)
        bl.setContentsMargins(20, 16, 20, 20)
        bl.setSpacing(0)
        scroll.setWidget(body)
        root_layout.addWidget(scroll)

        self._section_fichiers(bl)
        self._section_params(bl)
        self._section_seuil(bl)
        self._section_cmd(bl)
        self._section_log(bl)
        bl.addStretch()

    # ── Sections ──────────────────────────────────────────────────────────────

    def _section_fichiers(self, parent):
        box = QGroupBox("Fichier source")
        lay = QVBoxLayout(box)
        lay.setSpacing(8)

        lay.addWidget(QLabel("Fichier CSV ECG"))
        row1 = QHBoxLayout()
        self.csv_edit = QLineEdit()
        self.csv_edit.setPlaceholderText("/chemin/vers/mon_ecg.csv")
        self.csv_edit.textChanged.connect(self._update_cmd)
        row1.addWidget(self.csv_edit)
        btn_csv = QPushButton("Parcourir…")
        btn_csv.setFixedWidth(110)
        btn_csv.clicked.connect(self._browse_csv)
        row1.addWidget(btn_csv)
        lay.addLayout(row1)

        lay.addWidget(QLabel("Fichier de sortie PDF  (optionnel — vide = auto)"))
        row2 = QHBoxLayout()
        self.out_edit = QLineEdit()
        self.out_edit.setPlaceholderText("Laissez vide → même nom que le CSV + _rapport.pdf")
        self.out_edit.textChanged.connect(self._update_cmd)
        row2.addWidget(self.out_edit)
        btn_out = QPushButton("Enregistrer sous…")
        btn_out.setFixedWidth(140)
        btn_out.clicked.connect(self._browse_out)
        row2.addWidget(btn_out)
        lay.addLayout(row2)

        parent.addWidget(box)

    def _section_params(self, parent):
        box = QGroupBox("Paramètres d'affichage")
        lay = QVBoxLayout(box)
        lay.setSpacing(8)

        lay.addWidget(QLabel("Durée de chaque tranche (secondes)"))
        row = QHBoxLayout()
        self.fen_slider = QSlider(Qt.Horizontal)
        self.fen_slider.setRange(5, 120)
        self.fen_slider.setSingleStep(5)
        self.fen_slider.setPageStep(5)
        self.fen_slider.setValue(15)
        self.fen_slider.valueChanged.connect(self._on_fen)
        row.addWidget(self.fen_slider)
        self.fen_spin = QSpinBox()
        self.fen_spin.setRange(5, 120)
        self.fen_spin.setSingleStep(5)
        self.fen_spin.setValue(15)
        self.fen_spin.setFixedWidth(70)
        self.fen_spin.valueChanged.connect(self._on_fen_spin)
        row.addWidget(self.fen_spin)
        self.fen_badge = QLabel("15 s")
        self.fen_badge.setObjectName("badge")
        self.fen_badge.setFixedWidth(56)
        self.fen_badge.setAlignment(Qt.AlignCenter)
        row.addWidget(self.fen_badge)
        lay.addLayout(row)

        parent.addWidget(box)

    def _section_seuil(self, parent):
        box = QGroupBox("Seuil de fréquence cardiaque")
        lay = QVBoxLayout(box)
        lay.setSpacing(8)

        self.seuil_chk = QCheckBox(
            "Signaler en rouge les zones au-delà du seuil")
        self.seuil_chk.stateChanged.connect(self._toggle_seuil)
        lay.addWidget(self.seuil_chk)

        self.seuil_widget = QWidget()
        self.seuil_widget.setStyleSheet(f"background:{WHITE};")
        sw = QVBoxLayout(self.seuil_widget)
        sw.setContentsMargins(0, 4, 0, 0)
        sw.setSpacing(6)
        sw.addWidget(QLabel("Seuil FC (bpm)"))

        row = QHBoxLayout()
        self.seuil_slider = QSlider(Qt.Horizontal)
        self.seuil_slider.setRange(40, 220)
        self.seuil_slider.setValue(100)
        self.seuil_slider.valueChanged.connect(self._on_seuil)
        row.addWidget(self.seuil_slider)
        self.seuil_spin = QSpinBox()
        self.seuil_spin.setRange(40, 220)
        self.seuil_spin.setValue(100)
        self.seuil_spin.setFixedWidth(70)
        self.seuil_spin.valueChanged.connect(self._on_seuil_spin)
        row.addWidget(self.seuil_spin)
        self.seuil_badge = QLabel("100 bpm")
        self.seuil_badge.setObjectName("badge")
        self.seuil_badge.setFixedWidth(72)
        self.seuil_badge.setAlignment(Qt.AlignCenter)
        row.addWidget(self.seuil_badge)
        sw.addLayout(row)

        info = QLabel(
            "Ligne rouge pointillée + fond rose + titre ⚠ "
            "sur chaque tranche dépassant le seuil")
        info.setWordWrap(True)
        info.setStyleSheet(
            f"background:{PINK_L};color:{PURPLE};border-radius:5px;"
            f"padding:7px 10px;font-size:12px;")
        sw.addWidget(info)

        self.seuil_widget.setVisible(False)
        lay.addWidget(self.seuil_widget)
        parent.addWidget(box)

    def _section_cmd(self, parent):
        box = QGroupBox("Commande")
        lay = QVBoxLayout(box)
        lay.setSpacing(10)

        self.cmd_edit = QLineEdit()
        self.cmd_edit.setReadOnly(True)
        self.cmd_edit.setStyleSheet(
            f"background:{NAVY};color:{PINK_L};font-family:'Courier New',monospace;"
            f"font-size:12px;border-radius:5px;padding:8px;border:none;")
        lay.addWidget(self.cmd_edit)

        row = QHBoxLayout()
        self.run_btn = QPushButton("▶  Générer le PDF")
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self._run)
        row.addWidget(self.run_btn)

        btn_copy = QPushButton("Copier la commande")
        btn_copy.clicked.connect(self._copy_cmd)
        row.addWidget(btn_copy)

        row.addStretch()

        btn_reset = QPushButton("Réinitialiser")
        btn_reset.clicked.connect(self._reset)
        row.addWidget(btn_reset)

        lay.addLayout(row)
        parent.addWidget(box)
        self._update_cmd()

    def _section_log(self, parent):
        box = QGroupBox("Journal d'exécution")
        lay = QVBoxLayout(box)
        self.log = QTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(160)
        lay.addWidget(self.log)
        parent.addWidget(box)

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def _browse_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choisir le fichier CSV ECG", "",
            "CSV files (*.csv);;All files (*.*)")
        if path:
            self.csv_edit.setText(path)
            if not self.out_edit.text():
                base = os.path.splitext(path)[0]
                self.out_edit.setText(base + "_rapport.pdf")

    def _browse_out(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Enregistrer le PDF sous…", "",
            "PDF files (*.pdf)")
        if path:
            self.out_edit.setText(path)

    def _on_fen(self, val):
        self.fen_spin.blockSignals(True)
        self.fen_spin.setValue(val)
        self.fen_spin.blockSignals(False)
        self.fen_badge.setText(f"{val} s")
        self._update_cmd()

    def _on_fen_spin(self, val):
        self.fen_slider.blockSignals(True)
        self.fen_slider.setValue(val)
        self.fen_slider.blockSignals(False)
        self.fen_badge.setText(f"{val} s")
        self._update_cmd()

    def _on_seuil(self, val):
        self.seuil_spin.blockSignals(True)
        self.seuil_spin.setValue(val)
        self.seuil_spin.blockSignals(False)
        self.seuil_badge.setText(f"{val} bpm")
        self._update_cmd()

    def _on_seuil_spin(self, val):
        self.seuil_slider.blockSignals(True)
        self.seuil_slider.setValue(val)
        self.seuil_slider.blockSignals(False)
        self.seuil_badge.setText(f"{val} bpm")
        self._update_cmd()

    def _toggle_seuil(self, state):
        self.seuil_widget.setVisible(state == Qt.Checked)
        self._update_cmd()

    def _build_cmd(self):
        script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "ecg_to_pdf.py")
        csv = self.csv_edit.text().strip() or "<fichier.csv>"
        parts = [sys.executable, script, csv]
        out = self.out_edit.text().strip()
        if out:
            parts.append(out)
        fen = self.fen_slider.value()
        if fen != 15:
            parts += ["--fenetre", str(fen)]
        if self.seuil_chk.isChecked():
            parts += ["--seuil-bpm", str(self.seuil_slider.value())]
        return parts

    def _update_cmd(self):
        self.cmd_edit.setText(" ".join(self._build_cmd()))

    def _copy_cmd(self):
        QApplication.clipboard().setText(self.cmd_edit.text())
        self._log("Commande copiée dans le presse-papier.", "info")

    def _reset(self):
        self.csv_edit.clear()
        self.out_edit.clear()
        self.fen_slider.setValue(15)
        self.fen_spin.setValue(15)
        self.seuil_chk.setChecked(False)
        self.seuil_slider.setValue(100)
        self.seuil_spin.setValue(100)
        self._log("Paramètres réinitialisés.", "info")

    # ── Run ───────────────────────────────────────────────────────────────────

    def _run(self):
        if self._thread and self._thread.isRunning():
            return
        csv = self.csv_edit.text().strip()
        if not csv or not os.path.isfile(csv):
            QMessageBox.critical(self, "Fichier manquant",
                                 "Veuillez sélectionner un fichier CSV ECG valide.")
            return
        script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "ecg_to_pdf.py")
        if not os.path.isfile(script):
            QMessageBox.critical(self, "Script introuvable",
                                 f"ecg_to_pdf.py introuvable dans :\n"
                                 f"{os.path.dirname(script)}")
            return

        out = self.out_edit.text().strip()
        if not out:
            out = os.path.splitext(csv)[0] + "_rapport.pdf"

        cmd = self._build_cmd()
        self._log("─" * 52, "info")
        self._log("Lancement : " + " ".join(cmd), "info")

        self.run_btn.setEnabled(False)
        self.run_btn.setText("⏳  En cours…")

        self._thread = QThread()
        self._worker = Worker(cmd, out)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.line.connect(self._log)
        self._worker.done.connect(self._on_done)
        self._worker.done.connect(self._thread.quit)
        self._thread.start()

    def _on_done(self, returncode, pdf_path):
        self.run_btn.setEnabled(True)
        self.run_btn.setText("▶  Générer le PDF")
        if returncode == 0:
            self._log("✅ PDF généré avec succès !", "ok")
            if pdf_path and os.path.isfile(pdf_path):
                reply = QMessageBox.question(
                    self, "PDF prêt",
                    f"Rapport généré :\n{pdf_path}\n\nOuvrir le fichier ?",
                    QMessageBox.Yes | QMessageBox.No)
                if reply == QMessageBox.Yes:
                    self._open_file(pdf_path)
        else:
            self._log(f"✖ Erreur (code {returncode})", "err")

    def _open_file(self, path):
        system = platform.system()
        if system == "Windows":
            os.startfile(path)
        elif system == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def _log(self, msg, tag=""):
        colors = {"err": "#ff8080", "ok": "#7affc4", "info": PINK_L}
        color  = colors.get(tag, "#c8ffd4")
        self.log.moveCursor(QTextCursor.End)
        self.log.insertHtml(
            f'<span style="color:{color};font-family:Courier New,monospace;'
            f'font-size:12px;">{msg.replace("<","&lt;").replace(">","&gt;")}</span><br>')
        self.log.moveCursor(QTextCursor.End)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    win = ECGLauncher()
    win.show()
    sys.exit(app.exec_())
