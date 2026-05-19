#!/usr/bin/env python3
"""
Polar Pair GUI — scan Bluetooth + sélection MAC + lancement de l'appairage.

Stocke la MAC choisie dans ~/.config/polar_ecg/config.json pour les
prochains lancements.

Dépendances : PyQt5 (pip install PyQt5)
Externes    : bluetoothctl, bash, pair_polar_h10.sh dans le même dossier.

Lancer : python3 polar_pair_gui.py
"""

import os
import re
import sys
import json
import shutil
import subprocess
from pathlib import Path

try:
    from PyQt5.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QListWidget, QListWidgetItem, QGroupBox,
        QTextEdit, QFrame, QMessageBox, QLineEdit, QScrollArea,
    )
    from PyQt5.QtCore import Qt, QThread, pyqtSignal, QObject
    from PyQt5.QtGui import QTextCursor, QFont
except ImportError:
    print("PyQt5 non trouvé. Installez-le avec :\n  pip install PyQt5")
    sys.exit(1)

# ── Palette (cohérente avec ecg_launcher.py / polar_app.py) ──────────────────
PINK    = "#c6168d"
NAVY    = "#162142"
PURPLE  = "#4c1e58"
LIGHT   = "#ebf0f7"
PINK_L  = "#f7cae2"
GRAY    = "#585a5e"
GRAY_L  = "#a8acb2"
WHITE   = "#ffffff"
GREEN   = "#4caf50"
RED     = "#f44336"

STYLE = f"""
QWidget {{
    font-family: 'Montserrat', 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
    font-size: 13px;
    color: {GRAY};
    background: {LIGHT};
}}
QMainWindow, QScrollArea, QWidget#root {{ background: {LIGHT}; }}

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

QLineEdit {{
    background: {LIGHT};
    border: 1px solid {GRAY_L};
    border-radius: 5px;
    padding: 6px 10px;
    color: {NAVY};
    font-family: 'Courier New', monospace;
}}
QLineEdit:focus {{ border-color: {PINK}; }}

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

QListWidget {{
    background: {WHITE};
    border: 1px solid {GRAY_L};
    border-radius: 5px;
    padding: 4px;
    color: {NAVY};
}}
QListWidget::item {{
    padding: 8px 10px;
    border-radius: 4px;
    margin: 2px;
}}
QListWidget::item:selected {{
    background: {PINK_L};
    color: {PINK};
    border: 1px solid {PINK};
}}
QListWidget::item:hover:!selected {{
    background: {LIGHT};
}}

QTextEdit#log {{
    background: {NAVY};
    color: #c8ffd4;
    font-family: 'Courier New', monospace;
    font-size: 12px;
    border: none;
    border-radius: 5px;
    padding: 8px;
}}

QLabel#title    {{ font-size: 20px; font-weight: bold; color: {WHITE}; background: transparent; }}
QLabel#subtitle {{ font-size: 12px; color: {GRAY_L}; background: transparent; }}
QLabel#savedMac {{
    background: {PINK_L};
    color: {PINK};
    border-radius: 4px;
    padding: 4px 10px;
    font-family: 'Courier New', monospace;
    font-weight: bold;
}}
"""

# ── Config persistante ───────────────────────────────────────────────────────
CONFIG_DIR  = Path(os.path.expanduser("~/.config/polar_ecg"))
CONFIG_FILE = CONFIG_DIR / "config.json"

MAC_RE = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")
POLAR_LINE_RE = re.compile(
    r"Device\s+([0-9A-Fa-f:]{17})\s+(.+)", re.IGNORECASE)


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ── Worker : scan Bluetooth ──────────────────────────────────────────────────

class ScanWorker(QObject):
    line     = pyqtSignal(str, str)        # (text, tag)
    device   = pyqtSignal(str, str)        # (mac, name)
    finished = pyqtSignal()

    def __init__(self, duration=15):
        super().__init__()
        self.duration = duration
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        if not shutil.which("bluetoothctl"):
            self.line.emit("bluetoothctl introuvable.", "err")
            self.finished.emit()
            return

        # 1) Récupérer les devices déjà connus
        try:
            known = subprocess.run(
                ["bluetoothctl", "devices"],
                capture_output=True, text=True, timeout=5)
            for ln in known.stdout.splitlines():
                m = POLAR_LINE_RE.search(ln)
                if m and "polar" in m.group(2).lower():
                    self.line.emit(f"Déjà connu : {ln.strip()}", "info")
                    self.device.emit(m.group(1), m.group(2).strip())
        except Exception as ex:
            self.line.emit(f"(devices) {ex}", "err")

        # 2) Lancer un scan timeout
        self.line.emit(f"Scan en cours ({self.duration} s)…", "info")
        try:
            proc = subprocess.Popen(
                ["bluetoothctl", "--timeout", str(self.duration), "scan", "on"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)

            for raw in proc.stdout:
                if self._stop:
                    proc.terminate()
                    break
                txt = raw.rstrip()
                if "Device" in txt and ("NEW" in txt or "CHG" in txt):
                    m = POLAR_LINE_RE.search(txt)
                    if m:
                        mac, name = m.group(1), m.group(2).strip()
                        # On retient tout, mais on signale les Polar
                        if "polar" in name.lower():
                            self.line.emit(f"  ✓ {mac}  {name}", "ok")
                            self.device.emit(mac, name)
                        else:
                            self.line.emit(f"    {mac}  {name}", "")
            proc.wait()
        except Exception as ex:
            self.line.emit(f"Erreur scan : {ex}", "err")

        self.line.emit("Scan terminé.", "info")
        self.finished.emit()


# ── Worker : exécution pair_polar_h10.sh ─────────────────────────────────────

class PairWorker(QObject):
    line     = pyqtSignal(str, str)
    finished = pyqtSignal(int)

    def __init__(self, script, mac):
        super().__init__()
        self.script = script
        self.mac    = mac

    def run(self):
        try:
            proc = subprocess.Popen(
                ["bash", self.script, self.mac],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
            for raw in proc.stdout:
                txt = raw.rstrip()
                low = txt.lower()
                tag = ("err" if any(w in low for w in
                                    ("✗", "error", "erreur", "fail"))
                       else "ok" if "[✓]" in txt or "✓" in txt
                       else "info" if "──" in txt
                       else "")
                self.line.emit(txt, tag)
            proc.wait()
            self.finished.emit(proc.returncode)
        except Exception as ex:
            self.line.emit(f"Erreur : {ex}", "err")
            self.finished.emit(1)


# ── Fenêtre principale ───────────────────────────────────────────────────────

class PolarPairWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Polar H10 — Appairage")
        self.setMinimumSize(640, 720)
        self.setStyleSheet(STYLE)

        self.config = load_config()
        self.devices = {}   # mac -> name
        self._scan_thread = None
        self._scan_worker = None
        self._pair_thread = None
        self._pair_worker = None

        self._build_ui()
        self._refresh_saved_mac()

        # Si MAC sauvée, on la pré-ajoute à la liste
        saved = self.config.get("polar_mac")
        if saved:
            self._add_device(saved, self.config.get("polar_name", "Polar (mémorisé)"))

    # ── UI ────────────────────────────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        central.setObjectName("root")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        header = QWidget()
        header.setStyleSheet(f"background:{NAVY};")
        hl = QVBoxLayout(header)
        hl.setContentsMargins(24, 18, 24, 18)
        hl.setSpacing(4)
        t = QLabel("Polar H10")
        t.setObjectName("title")
        hl.addWidget(t)
        s = QLabel("Scan des capteurs Bluetooth et lancement de l'appairage")
        s.setObjectName("subtitle")
        hl.addWidget(s)
        root.addWidget(header)

        # Corps scrollable
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        body.setObjectName("root")
        bl = QVBoxLayout(body)
        bl.setContentsMargins(20, 16, 20, 20)
        bl.setSpacing(0)
        scroll.setWidget(body)
        root.addWidget(scroll)

        # Section : MAC mémorisée
        self._section_saved(bl)
        # Section : scan
        self._section_scan(bl)
        # Section : action
        self._section_action(bl)
        # Section : log
        self._section_log(bl)

    def _section_saved(self, parent):
        box = QGroupBox("MAC mémorisée")
        lay = QHBoxLayout(box)
        lay.setSpacing(10)

        self.lbl_saved = QLabel("Aucune")
        self.lbl_saved.setObjectName("savedMac")
        lay.addWidget(self.lbl_saved)
        lay.addStretch()

        self.btn_use_saved = QPushButton("Utiliser cette MAC")
        self.btn_use_saved.clicked.connect(self._use_saved)
        lay.addWidget(self.btn_use_saved)

        self.btn_clear_saved = QPushButton("Oublier")
        self.btn_clear_saved.clicked.connect(self._clear_saved)
        lay.addWidget(self.btn_clear_saved)

        parent.addWidget(box)

    def _section_scan(self, parent):
        box = QGroupBox("Scan Bluetooth")
        lay = QVBoxLayout(box)
        lay.setSpacing(10)

        info = QLabel(
            "1) Mettez le H10 en mode appairage (bouton ~7 s, LED clignote vite).  "
            "2) Cliquez sur Scanner. Les capteurs Polar détectés apparaîtront en rose.")
        info.setWordWrap(True)
        info.setStyleSheet(
            f"background:{PINK_L};color:{PURPLE};border-radius:5px;"
            f"padding:7px 10px;font-size:12px;")
        lay.addWidget(info)

        self.list_devices = QListWidget()
        self.list_devices.setMinimumHeight(160)
        self.list_devices.itemSelectionChanged.connect(self._on_select)
        lay.addWidget(self.list_devices)

        row = QHBoxLayout()
        self.btn_scan = QPushButton("🔍  Scanner (15 s)")
        self.btn_scan.clicked.connect(self._toggle_scan)
        row.addWidget(self.btn_scan)

        self.btn_clear_list = QPushButton("Vider la liste")
        self.btn_clear_list.clicked.connect(self._clear_list)
        row.addWidget(self.btn_clear_list)

        row.addStretch()

        self.btn_save = QPushButton("💾  Mémoriser la sélection")
        self.btn_save.clicked.connect(self._save_selected)
        self.btn_save.setEnabled(False)
        row.addWidget(self.btn_save)

        lay.addLayout(row)
        parent.addWidget(box)

    def _section_action(self, parent):
        box = QGroupBox("Appairage")
        lay = QVBoxLayout(box)
        lay.setSpacing(10)

        row = QHBoxLayout()
        row.addWidget(QLabel("MAC à appairer :"))
        self.mac_edit = QLineEdit()
        self.mac_edit.setPlaceholderText("XX:XX:XX:XX:XX:XX")
        self.mac_edit.textChanged.connect(self._validate_mac)
        row.addWidget(self.mac_edit, 1)
        lay.addLayout(row)

        self.btn_pair = QPushButton("▶  Lancer l'appairage")
        self.btn_pair.setObjectName("primary")
        self.btn_pair.clicked.connect(self._run_pair)
        self.btn_pair.setEnabled(False)
        lay.addWidget(self.btn_pair)

        parent.addWidget(box)

    def _section_log(self, parent):
        box = QGroupBox("Journal")
        lay = QVBoxLayout(box)
        self.log = QTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(180)
        lay.addWidget(self.log)
        parent.addWidget(box)

    # ── Helpers ───────────────────────────────────────────────────────────
    def _log(self, msg, tag=""):
        colors = {
            "err":  "#ff8080",
            "ok":   "#7affc4",
            "info": PINK_L,
        }
        color = colors.get(tag, "#c8ffd4")
        safe = msg.replace("<", "&lt;").replace(">", "&gt;")
        self.log.moveCursor(QTextCursor.End)
        self.log.insertHtml(
            f'<span style="color:{color};font-family:Courier New,monospace;'
            f'font-size:12px;">{safe}</span><br>')
        self.log.moveCursor(QTextCursor.End)

    def _refresh_saved_mac(self):
        mac = self.config.get("polar_mac")
        name = self.config.get("polar_name", "")
        if mac:
            self.lbl_saved.setText(f"{mac}   {name}".strip())
            self.btn_use_saved.setEnabled(True)
            self.btn_clear_saved.setEnabled(True)
        else:
            self.lbl_saved.setText("Aucune")
            self.btn_use_saved.setEnabled(False)
            self.btn_clear_saved.setEnabled(False)

    def _add_device(self, mac, name):
        mac = mac.upper()
        if mac in self.devices:
            # Mise à jour du nom si plus précis
            if name and name != "Polar (mémorisé)":
                self.devices[mac] = name
                for i in range(self.list_devices.count()):
                    it = self.list_devices.item(i)
                    if it.data(Qt.UserRole) == mac:
                        it.setText(self._fmt_item(mac, name))
            return
        self.devices[mac] = name
        it = QListWidgetItem(self._fmt_item(mac, name))
        it.setData(Qt.UserRole, mac)
        it.setData(Qt.UserRole + 1, name)
        self.list_devices.addItem(it)

    @staticmethod
    def _fmt_item(mac, name):
        return f"{mac}    {name}"

    def _validate_mac(self, text):
        ok = bool(MAC_RE.fullmatch(text.strip()))
        self.btn_pair.setEnabled(ok and not self._pair_running())
        return ok

    def _pair_running(self):
        return self._pair_thread is not None and self._pair_thread.isRunning()

    def _scan_running(self):
        return self._scan_thread is not None and self._scan_thread.isRunning()

    # ── Callbacks ─────────────────────────────────────────────────────────
    def _on_select(self):
        items = self.list_devices.selectedItems()
        if not items:
            self.btn_save.setEnabled(False)
            return
        mac = items[0].data(Qt.UserRole)
        self.mac_edit.setText(mac)
        self.btn_save.setEnabled(True)

    def _toggle_scan(self):
        if self._scan_running():
            self._scan_worker.stop()
            self.btn_scan.setText("Arrêt en cours…")
            self.btn_scan.setEnabled(False)
            return

        self.btn_scan.setText("⏹  Arrêter")
        self._log("─" * 50, "info")
        self._log("Démarrage du scan…", "info")

        self._scan_thread = QThread()
        self._scan_worker = ScanWorker(duration=15)
        self._scan_worker.moveToThread(self._scan_thread)
        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.line.connect(self._log)
        self._scan_worker.device.connect(self._add_device)
        self._scan_worker.finished.connect(self._on_scan_done)
        self._scan_worker.finished.connect(self._scan_thread.quit)
        self._scan_thread.start()

    def _on_scan_done(self):
        self.btn_scan.setText("🔍  Scanner (15 s)")
        self.btn_scan.setEnabled(True)

    def _clear_list(self):
        self.list_devices.clear()
        self.devices.clear()
        self.btn_save.setEnabled(False)
        # On remet la MAC sauvée si elle existe
        saved = self.config.get("polar_mac")
        if saved:
            self._add_device(saved, self.config.get("polar_name", "Polar (mémorisé)"))

    def _save_selected(self):
        items = self.list_devices.selectedItems()
        if not items:
            return
        mac  = items[0].data(Qt.UserRole)
        name = items[0].data(Qt.UserRole + 1) or ""
        self.config["polar_mac"]  = mac
        self.config["polar_name"] = name
        save_config(self.config)
        self._refresh_saved_mac()
        self._log(f"✓ MAC mémorisée : {mac}  ({name})", "ok")

    def _use_saved(self):
        mac = self.config.get("polar_mac")
        if mac:
            self.mac_edit.setText(mac)

    def _clear_saved(self):
        if QMessageBox.question(
                self, "Oublier la MAC",
                "Supprimer la MAC mémorisée ?") != QMessageBox.Yes:
            return
        self.config.pop("polar_mac", None)
        self.config.pop("polar_name", None)
        save_config(self.config)
        self._refresh_saved_mac()
        self._log("MAC oubliée.", "info")

    def _run_pair(self):
        if self._pair_running():
            return
        mac = self.mac_edit.text().strip().upper()
        if not MAC_RE.fullmatch(mac):
            QMessageBox.critical(self, "MAC invalide",
                                 "Format attendu : XX:XX:XX:XX:XX:XX")
            return

        script_dir = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(script_dir, "pair_polar_h10.sh")
        if not os.path.isfile(script):
            QMessageBox.critical(
                self, "Script introuvable",
                f"pair_polar_h10.sh introuvable dans :\n{script_dir}")
            return

        # Auto-mémorisation si pas encore enregistrée
        if self.config.get("polar_mac") != mac:
            self.config["polar_mac"]  = mac
            self.config["polar_name"] = self.devices.get(mac, "")
            save_config(self.config)
            self._refresh_saved_mac()
            self._log(f"✓ MAC mémorisée automatiquement : {mac}", "ok")

        self._log("─" * 50, "info")
        self._log(f"Lancement : bash {script} {mac}", "info")
        self._log("⚠  Le script peut demander le mot de passe sudo "
                  "dans le terminal d'origine.", "info")

        self.btn_pair.setEnabled(False)
        self.btn_pair.setText("⏳  Appairage en cours…")

        self._pair_thread = QThread()
        self._pair_worker = PairWorker(script, mac)
        self._pair_worker.moveToThread(self._pair_thread)
        self._pair_thread.started.connect(self._pair_worker.run)
        self._pair_worker.line.connect(self._log)
        self._pair_worker.finished.connect(self._on_pair_done)
        self._pair_worker.finished.connect(self._pair_thread.quit)
        self._pair_thread.start()

    def _on_pair_done(self, rc):
        self.btn_pair.setText("▶  Lancer l'appairage")
        self._validate_mac(self.mac_edit.text())
        if rc == 0:
            self._log("✅ Appairage terminé avec succès.", "ok")
        else:
            self._log(f"✖ Échec (code {rc})", "err")

    def closeEvent(self, ev):
        # Arrêt propre des threads
        if self._scan_running():
            self._scan_worker.stop()
            self._scan_thread.quit()
            self._scan_thread.wait(2000)
        if self._pair_running():
            self._pair_thread.quit()
            self._pair_thread.wait(2000)
        super().closeEvent(ev)


# ── Entrée ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    win = PolarPairWindow()
    win.show()
    sys.exit(app.exec_())
