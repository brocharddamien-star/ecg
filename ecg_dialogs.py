"""Dialogues PDF et worker de génération de rapport."""

import logging
import os
from io import BytesIO

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import pyqtgraph as pg

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QFrame, QScrollArea, QWidget, QProgressBar,
)

from theme import (
    C_PINK, C_NAVY, C_WHITE, C_GRAY, C_LGRAY, C_PURPLE, C_LPINK,
    C_GREEN, C_RED, C_ORANGE,
    BLUE_HDR, TEAL_ACC,
    RED_ECG, GRID_MAJ, GRID_MIN,
)
from ecg_signal import bandpass_filter, detect_r_peaks, adaptive_x_spacing, fmt_mmss


# ═══════════════════════════════════════════════════════════════════════════════
# FONCTIONS UTILITAIRES PDF (internes à ce module)
# ═══════════════════════════════════════════════════════════════════════════════

def _mmss_formatter_func(x, _=None):
    return fmt_mmss(x)

_mmss_formatter = plt.FuncFormatter(_mmss_formatter_func)


def _compute_stats(df, peaks, fs):
    duration = float(df["t_s"].iloc[-1] - df["t_s"].iloc[0])
    n_beats  = len(peaks)
    mean_hr  = (n_beats / duration) * 60 if duration > 0 else 0
    rr_ms    = np.diff(peaks) / fs * 1000 if len(peaks) > 1 else np.array([])
    hr_csv   = df["hr"].dropna()
    if len(hr_csv) >= 3:
        mean_hr = float(hr_csv.mean())
    sdnn  = float(np.std(rr_ms))  if len(rr_ms) > 1 else None
    rmssd = float(np.sqrt(np.mean(np.diff(rr_ms) ** 2))) if len(rr_ms) > 1 else None
    return {
        "duration_s":  round(duration, 1),
        "fs_hz":       round(fs, 1),
        "n_beats":     n_beats,
        "mean_hr":     round(mean_hr, 1),
        "min_hr":      round(float(hr_csv.min()), 1) if len(hr_csv) else None,
        "max_hr":      round(float(hr_csv.max()), 1) if len(hr_csv) else None,
        "ecg_min_mv":  round(float(df["ecg"].min()), 3),
        "ecg_max_mv":  round(float(df["ecg"].max()), 3),
        "rr_mean_ms":  round(float(np.mean(rr_ms)), 1) if len(rr_ms) > 0 else None,
        "rr_std_ms":   round(sdnn, 1)  if sdnn  else None,
        "rmssd_ms":    round(rmssd, 1) if rmssd else None,
    }


def _ecg_grid(ax, t, signal, peaks=None, title=""):
    ax.set_facecolor("#FFFAFA")
    duration  = float(t[-1] - t[0]) if len(t) > 1 else 1.0
    xm, xM    = adaptive_x_spacing(duration)
    ax.xaxis.set_minor_locator(plt.MultipleLocator(xm))
    ax.xaxis.set_major_locator(plt.MultipleLocator(xM))
    y_span = float(np.ptp(signal)) if len(signal) > 1 else 1.0
    ym = 0.1 if y_span / 0.1 <= 500 else round(y_span / 50, 2)
    yM = 0.5 if y_span / 0.5 <= 200 else round(y_span / 20, 2)
    ax.yaxis.set_minor_locator(plt.MultipleLocator(ym))
    ax.yaxis.set_major_locator(plt.MultipleLocator(yM))
    ax.grid(which="minor", color=GRID_MIN, linewidth=0.4)
    ax.grid(which="major", color=GRID_MAJ, linewidth=0.8)
    ax.plot(t, signal, color=RED_ECG, linewidth=0.8, antialiased=True)
    if peaks is not None and len(peaks) > 0:
        ax.scatter(t[peaks], signal[peaks], color="#E74C3C", s=18, zorder=5)
    ax.set_title(title, fontsize=8, color=BLUE_HDR, fontweight="bold", pad=3)
    ax.set_ylabel("mV", fontsize=7, color="#555")
    ax.set_xlabel("Temps (mm:ss)", fontsize=7, color="#555")
    ax.tick_params(labelsize=6)
    ax.xaxis.set_major_formatter(_mmss_formatter)
    for sp in ax.spines.values():
        sp.set_edgecolor("#CCC")


def _make_slice_fig(df, ecg_f, peaks, stats, t0, t1, idx, bpm_thr):
    t   = df["t_s"].values
    raw = df["ecg"].values
    mask    = (t >= t0) & (t < t1)
    t_win   = t[mask]; raw_win = raw[mask]

    fig, axes = plt.subplots(2, 1, figsize=(16, 5),
                              gridspec_kw={"hspace": 0.60})
    fig.patch.set_facecolor("white")

    alert = ""
    if bpm_thr is not None:
        hr_w = df[df["hr"].notna()]
        hr_w = hr_w[(hr_w["t_s"] >= t0) & (hr_w["t_s"] < t1)]
        if len(hr_w) and (hr_w["hr"] > bpm_thr).any():
            alert = f"  ⚠ FC > {bpm_thr:.0f} bpm"
    tc = "#C0392B" if alert else BLUE_HDR
    fig.suptitle(
        f"Tranche {idx}  —  "
        f"{int(t0//60):02d}:{int(t0%60):02d}"
        f" → {int(t1//60):02d}:{int(t1%60):02d}{alert}",
        fontsize=10, color=tc, fontweight="bold", y=0.98)

    _ecg_grid(axes[0], t_win, raw_win, title="Signal brut")
    axes[0].set_xlim(t0, t1)

    hr_df = df[df["hr"].notna()][["t_s", "hr"]]
    hr_w  = hr_df[(hr_df["t_s"] >= t0) & (hr_df["t_s"] < t1)]
    ax3   = axes[1]
    if len(hr_w) > 1:
        hv = hr_w["hr"].values; ht = hr_w["t_s"].values
        ax3.set_facecolor("#FFF5F5" if alert else "#F5FAFF")
        ax3.grid(True, color="#F5CCCC" if alert else "#D0E8F8", linewidth=0.6)
        if bpm_thr is not None:
            for i in range(len(ht)-1):
                c = "#C0392B" if max(hv[i:i+2]) > bpm_thr else "#2980B9"
                ax3.plot(ht[i:i+2], hv[i:i+2], color=c, linewidth=1.8)
            above = hv > bpm_thr
            ax3.scatter(ht[above],  hv[above],  color="#C0392B", s=22, zorder=5)
            ax3.scatter(ht[~above], hv[~above], color=TEAL_ACC, s=14, zorder=5)
            ax3.axhline(bpm_thr, color="#C0392B", linewidth=1.2, linestyle="--",
                        label=f"Seuil {bpm_thr:.0f} bpm")
            ax3.axhspan(bpm_thr, max(hv.max()+5, bpm_thr+10),
                        color="#C0392B", alpha=0.08)
            ax3.legend(fontsize=7, loc="upper right")
        else:
            ax3.plot(ht, hv, color="#2980B9", linewidth=1.5,
                     marker="o", markersize=4, markerfacecolor=TEAL_ACC)
        ax3.set_title("Fréquence cardiaque", fontsize=8, color=tc,
                      fontweight="bold", pad=3)
        ax3.set_ylabel("bpm", fontsize=7, color="#555")
        ax3.set_xlabel("Temps (mm:ss)", fontsize=7, color="#555")
        ax3.set_xlim(t0, t1)
        ax3.set_ylim(max(0, hv.min()-5), hv.max()+10)
        ax3.tick_params(labelsize=6)
        ax3.xaxis.set_major_formatter(_mmss_formatter)
    else:
        ax3.text(0.5, 0.5, "Données HR insuffisantes",
                 ha="center", va="center", transform=ax3.transAxes,
                 fontsize=10, color="#999")
        ax3.set_axis_off()

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


def _make_full_bpm_fig(df, bpm_thr=None):
    hr_df = df[df["hr"].notna()][["t_s", "hr"]]
    if len(hr_df) < 2:
        return None
    hv = hr_df["hr"].values
    ht = hr_df["t_s"].values
    fig, ax = plt.subplots(figsize=(16, 3))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("#F5FAFF")
    ax.grid(True, color="#D0E8F8", linewidth=0.6)
    if bpm_thr is not None:
        for i in range(len(ht) - 1):
            c = "#C0392B" if max(hv[i:i+2]) > bpm_thr else "#2980B9"
            ax.plot(ht[i:i+2], hv[i:i+2], color=c, linewidth=1.4)
        above = hv > bpm_thr
        ax.scatter(ht[above],  hv[above],  color="#C0392B", s=18, zorder=5)
        ax.scatter(ht[~above], hv[~above], color=TEAL_ACC,  s=10, zorder=5)
        ax.axhline(bpm_thr, color="#C0392B", linewidth=1.2, linestyle="--",
                   label=f"Seuil {bpm_thr:.0f} bpm")
        ax.axhspan(bpm_thr, max(hv.max()+5, bpm_thr+10), color="#C0392B", alpha=0.08)
        ax.legend(fontsize=7, loc="upper right")
    else:
        ax.plot(ht, hv, color="#2980B9", linewidth=1.4,
                marker="o", markersize=3, markerfacecolor=TEAL_ACC)
    ax.set_title("Fréquence cardiaque — enregistrement complet",
                 fontsize=10, color=BLUE_HDR, fontweight="bold")
    ax.set_ylabel("bpm", fontsize=8, color="#555")
    ax.set_xlabel("Temps (mm:ss)", fontsize=8, color="#555")
    ax.set_xlim(ht[0], ht[-1])
    ax.xaxis.set_major_formatter(_mmss_formatter)
    ax.set_ylim(max(0, hv.min()-5), hv.max()+10)
    ax.tick_params(labelsize=7)
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


def _make_rr_histogram(rr_ms):
    if len(rr_ms) < 3:
        return None
    fig, ax = plt.subplots(figsize=(5.5, 2.8))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("#FAFAFA")
    ax.hist(rr_ms, bins=min(15, len(rr_ms)),
            color=TEAL_ACC, edgecolor="white", linewidth=0.5, alpha=0.85)
    ax.axvline(np.mean(rr_ms), color=RED_ECG, linewidth=1.2, linestyle="--",
               label=f"Moyenne: {np.mean(rr_ms):.0f} ms")
    ax.legend(fontsize=7)
    ax.set_xlabel("Intervalle RR (ms)", fontsize=8)
    ax.set_ylabel("Fréquence", fontsize=8)
    ax.set_title("Distribution des intervalles RR", fontsize=9,
                 color=BLUE_HDR, fontweight="bold")
    ax.tick_params(labelsize=7)
    ax.grid(axis="y", color="#DDD", linewidth=0.5)
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


# ═══════════════════════════════════════════════════════════════════════════════
# GÉNÉRATION PDF
# ═══════════════════════════════════════════════════════════════════════════════

def generate_pdf(csv_path: str, pdf_path: str,
                 bpm_threshold: float = None,
                 window_s: float = 15.0,
                 time_ranges=None,
                 progress_cb=None,
                 progress_pct_cb=None) -> str:
    """Génère un rapport PDF depuis un CSV ECG. Retourne le chemin du PDF."""
    import math
    from datetime import datetime as _dt
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        Image as RLImage, HRFlowable,
    )

    def prog(msg, pct=None):
        logging.info(f"[pdf] {msg}")
        if progress_cb:
            progress_cb(msg)
        if pct is not None and progress_pct_cb:
            progress_pct_cb(pct)

    prog(f"Chargement : {os.path.basename(csv_path)}", pct=2)
    df = pd.read_csv(csv_path)
    t0 = df["time"].iloc[0]
    df["t_s"] = (df["time"] - t0) / 1e9
    df["ecg"]  = pd.to_numeric(df["ecg"],  errors="coerce")
    df["hr"]   = pd.to_numeric(df["hr"],   errors="coerce")
    df["rr"]   = pd.to_numeric(df["rr"],   errors="coerce")
    df_ecg = df.dropna(subset=["ecg"]).reset_index(drop=True)

    if df_ecg.empty:
        raise ValueError("Aucune donnée ECG dans ce fichier.")

    t   = df_ecg["t_s"].values
    raw = df_ecg["ecg"].values
    fs  = 1.0 / np.median(np.diff(t))

    prog(f"{len(df_ecg)} échantillons, fs≈{fs:.1f} Hz, durée={t[-1]:.1f} s", pct=8)
    prog("Filtrage et détection des pics R…", pct=10)

    ecg_f = bandpass_filter(raw, fs)
    # Seuil relatif adapté au contexte in-app (signal variable)
    height_thr = 0.3 * np.max(ecg_f)
    peaks, _ = detect_r_peaks(ecg_f, fs,
                               height_thr=height_thr,
                               min_distance_s=0.40,
                               prominence=0.1)
    stats = _compute_stats(df_ecg, peaks, fs)
    prog(f"{len(peaks)} pics R, FC moy={stats['mean_hr']} bpm", pct=18)

    t_data_min = float(df_ecg["t_s"].iloc[0])
    t_data_max = float(df_ecg["t_s"].iloc[-1])
    ranges = time_ranges if time_ranges else [(t_data_min, t_data_max)]

    total_slices = sum(
        max(1, math.ceil((min(float(r_end), t_data_max) - max(float(r_start), t_data_min)) / window_s))
        for r_start, r_end in ranges
    )

    prog(f"Génération des figures ({window_s:.0f} s / tranche)…", pct=20)
    figs, idx, done = [], 1, 0
    for r_start, r_end in ranges:
        r_start = max(float(r_start), t_data_min)
        r_end   = min(float(r_end),   t_data_max)
        ts = r_start
        while ts < r_end:
            te  = min(ts + window_s, r_end)
            buf = _make_slice_fig(df_ecg, ecg_f, peaks, stats,
                                  ts, te, idx, bpm_threshold)
            figs.append((buf, idx))
            done += 1
            prog(f"Tranche {done}/{total_slices}…",
                 pct=20 + int(done / max(total_slices, 1) * 70))
            ts += window_s; idx += 1

    n_parts = len(ranges)
    prog(f"{len(figs)} tranche(s) générée(s)"
         + (f" sur {n_parts} partie(s)" if time_ranges else ""), pct=90)

    rr_ms   = np.diff(peaks) / fs * 1000 if len(peaks) > 1 else np.array([])
    rr_buf  = _make_rr_histogram(rr_ms)
    bpm_buf = _make_full_bpm_fig(df_ecg, bpm_threshold)

    prog("Construction du PDF…")
    doc = SimpleDocTemplate(
        pdf_path, pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=18*mm,  bottomMargin=18*mm,
    )
    styles = getSampleStyleSheet()
    W = A4[0] - 30*mm

    title_sty = ParagraphStyle(
        "T", parent=styles["Title"],
        fontSize=20, textColor=colors.HexColor(BLUE_HDR),
        spaceAfter=4, alignment=TA_CENTER)
    sub_sty = ParagraphStyle(
        "S", parent=styles["Normal"],
        fontSize=9, textColor=colors.HexColor("#555555"),
        alignment=TA_CENTER, spaceAfter=2)
    sec_sty = ParagraphStyle(
        "H", parent=styles["Heading2"],
        fontSize=11, textColor=colors.HexColor(BLUE_HDR),
        spaceBefore=10, spaceAfter=4)
    cap_sty = ParagraphStyle(
        "C", parent=styles["Normal"],
        fontSize=7.5, textColor=colors.HexColor("#555555"),
        alignment=TA_CENTER, spaceAfter=4)
    note_sty = ParagraphStyle(
        "N", parent=styles["Normal"],
        fontSize=7.5, textColor=colors.HexColor("#777777"),
        leftIndent=5, spaceAfter=4)

    story = []
    now = _dt.now().strftime("%d/%m/%Y %H:%M")
    story.append(Paragraph("Rapport ECG", title_sty))
    story.append(Paragraph(
        f"Généré le {now} &nbsp;|&nbsp; "
        f"Fichier : <b>{os.path.basename(csv_path)}</b>",
        sub_sty))
    story.append(HRFlowable(width="100%", thickness=2,
                             color=colors.HexColor(BLUE_HDR), spaceAfter=8))

    story.append(Paragraph("Résumé de l'enregistrement", sec_sty))
    v = lambda x, u="": f"{x} {u}".strip() if x is not None else "N/A"
    tbl_data = [
        ["Paramètre", "Valeur", "Paramètre", "Valeur"],
        ["Durée", v(stats["duration_s"],"s"),
         "Fréquence d'échantillonnage", v(stats["fs_hz"],"Hz")],
        ["Battements (R)", v(stats["n_beats"]),
         "FC moyenne", v(stats["mean_hr"],"bpm")],
        ["FC min", v(stats["min_hr"],"bpm"),
         "FC max", v(stats["max_hr"],"bpm")],
        ["ECG min", v(stats["ecg_min_mv"],"mV"),
         "ECG max", v(stats["ecg_max_mv"],"mV")],
        ["Intervalle RR moyen", v(stats["rr_mean_ms"],"ms"),
         "SDNN", v(stats["rr_std_ms"],"ms")],
        ["RMSSD", v(stats["rmssd_ms"],"ms"),
         "Mesures HR (CSV)", str(df_ecg["hr"].notna().sum())],
    ]
    cw = [W*0.30, W*0.20, W*0.30, W*0.20]
    tbl = Table(tbl_data, colWidths=cw, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),(-1,0), colors.HexColor(BLUE_HDR)),
        ("TEXTCOLOR",     (0,0),(-1,0), colors.white),
        ("FONTNAME",      (0,0),(-1,0), "Helvetica-Bold"),
        ("FONTSIZE",      (0,0),(-1,0), 8.5),
        ("ALIGN",         (0,0),(-1,0), "CENTER"),
        ("BACKGROUND",    (0,1),(0,-1), colors.HexColor("#EBF2FA")),
        ("BACKGROUND",    (2,1),(2,-1), colors.HexColor("#EBF2FA")),
        ("FONTNAME",      (0,1),(0,-1), "Helvetica-Bold"),
        ("FONTNAME",      (2,1),(2,-1), "Helvetica-Bold"),
        ("FONTSIZE",      (0,1),(-1,-1), 8),
        ("GRID",          (0,0),(-1,-1), 0.4, colors.HexColor("#BBCFE8")),
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0),(-1,-1), 5),
        ("BOTTOMPADDING", (0,0),(-1,-1), 5),
        ("LEFTPADDING",   (0,0),(-1,-1), 7),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 8))

    if bpm_buf:
        story.append(Paragraph("Fréquence cardiaque — enregistrement complet", sec_sty))
        bpm_cap = "Courbe FC sur la totalité de l'enregistrement"
        if bpm_threshold:
            bpm_cap += f"  |  <font color='#C0392B'><b>Seuil : {bpm_threshold:.0f} bpm</b></font>"
        story.append(Paragraph(bpm_cap, cap_sty))
        story.append(RLImage(bpm_buf, width=W, height=W * 3/16))
        story.append(Spacer(1, 8))

    if time_ranges:
        parts_desc = "  ·  ".join(
            f"{int(s//60):02d}:{int(s%60):02d}→{int(e//60):02d}:{int(e%60):02d}"
            for s, e in time_ranges)
        story.append(Paragraph(
            f"Tracé ECG — {len(time_ranges)} partie(s) sélectionnée(s)"
            f" — tranches de {window_s:.0f} s", sec_sty))
        story.append(Paragraph(parts_desc, cap_sty))
    else:
        story.append(Paragraph(f"Tracé ECG — tranches de {window_s:.0f} secondes", sec_sty))
    cap = "De haut en bas : signal brut · fréquence cardiaque"
    if bpm_threshold:
        cap += f"  |  <font color='#C0392B'><b>Seuil FC : {bpm_threshold:.0f} bpm</b></font>"
    story.append(Paragraph(cap, cap_sty))
    for buf, _ in figs:
        story.append(RLImage(buf, width=W, height=W * 5/16))
        story.append(Spacer(1, 6))

    if rr_buf:
        story.append(Paragraph("Variabilité de la fréquence cardiaque (VFC)", sec_sty))
        rr_img = RLImage(rr_buf, width=W*0.6, height=W*0.6*2.8/5.5)
        t2 = Table([[rr_img]], colWidths=[W])
        t2.setStyle(TableStyle([("ALIGN",(0,0),(-1,-1),"CENTER")]))
        story.append(t2)
        if stats["rr_std_ms"]:
            story.append(Paragraph(
                f"SDNN = {stats['rr_std_ms']} ms  |  RMSSD = {v(stats['rmssd_ms'],'ms')}",
                cap_sty))

    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=0.8,
                             color=colors.HexColor("#BBBBBB"), spaceAfter=6))
    story.append(Paragraph(
        "<b>Avertissement :</b> Ce rapport est généré automatiquement à des fins "
        "d'analyse technique. Il ne constitue pas un diagnostic médical. "
        "Toute interprétation clinique doit être effectuée par un professionnel de santé qualifié.",
        note_sty))

    prog("Assemblage du PDF…", pct=92)
    doc.build(story)
    prog(f"PDF écrit : {os.path.basename(pdf_path)}", pct=100)
    return pdf_path


# ═══════════════════════════════════════════════════════════════════════════════
# WORKER PDF (thread séparé pour ne pas bloquer l'UI)
# ═══════════════════════════════════════════════════════════════════════════════

class PdfWorker(QThread):
    progress     = pyqtSignal(str)
    progress_pct = pyqtSignal(int)
    finished     = pyqtSignal(str)
    error        = pyqtSignal(str)

    def __init__(self, csv_path, bpm_threshold=None, window_s=15.0, time_ranges=None):
        super().__init__()
        self.csv_path      = csv_path
        self.bpm_threshold = bpm_threshold
        self.window_s      = window_s
        self.time_ranges   = time_ranges

    def run(self):
        try:
            pdf_path = os.path.splitext(self.csv_path)[0] + "_rapport.pdf"
            generate_pdf(
                self.csv_path, pdf_path,
                bpm_threshold=self.bpm_threshold,
                window_s=self.window_s,
                time_ranges=self.time_ranges,
                progress_cb=lambda m: self.progress.emit(m),
                progress_pct_cb=lambda p: self.progress_pct.emit(p),
            )
            self.finished.emit(pdf_path)
        except Exception as e:
            self.error.emit(str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# DIALOGUE : CHOIX DU PÉRIMÈTRE PDF
# ═══════════════════════════════════════════════════════════════════════════════

class PdfScopeDialog(QDialog):
    """Demande si le PDF doit couvrir tout l'enregistrement ou des parties."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.choice = None
        self.setWindowTitle("Export PDF")
        self.setModal(True)
        self.setFixedSize(400, 210)
        self.setStyleSheet(f"background:{C_NAVY};")
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(14)
        lay.setContentsMargins(28, 22, 28, 22)

        lbl = QLabel("Que souhaitez-vous inclure dans le PDF ?")
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color:{C_WHITE}; font-size:13px; font-weight:700;")
        lay.addWidget(lbl)

        btn_full = QPushButton("Tout l'enregistrement")
        btn_full.setMinimumHeight(44)
        btn_full.setStyleSheet(
            f"QPushButton {{ background:{C_PINK}; color:{C_WHITE}; "
            f"border-radius:10px; font-size:13px; font-weight:700; }}"
            f"QPushButton:hover {{ background:#d9269f; }}")
        btn_full.clicked.connect(lambda: self._pick("full"))
        lay.addWidget(btn_full)

        btn_sel = QPushButton("Sélectionner une ou plusieurs parties")
        btn_sel.setMinimumHeight(44)
        btn_sel.setStyleSheet(
            f"QPushButton {{ background:{C_PURPLE}; color:{C_WHITE}; "
            f"border-radius:10px; font-size:12px; font-weight:600; "
            f"border:2px solid {C_PINK}; }}"
            f"QPushButton:hover {{ background:#5d2570; }}")
        btn_sel.clicked.connect(lambda: self._pick("select"))
        lay.addWidget(btn_sel)

    def _pick(self, choice):
        self.choice = choice
        self.accept()


# ═══════════════════════════════════════════════════════════════════════════════
# DIALOGUE : SÉLECTEUR INTERACTIF DE PARTIES ECG
# ═══════════════════════════════════════════════════════════════════════════════

class EcgSelectorDialog(QDialog):
    """Graphique ECG complet avec tracé par clic-glisser pour choisir les parties."""

    def __init__(self, csv_path: str, parent=None):
        super().__init__(parent)
        self.csv_path    = csv_path
        self._selected   = []
        self._added_vis  = []
        self._total_s    = 0.0
        self._drag_start = None
        self._drag_item  = None
        self._setup_ui()
        self._load_and_plot()

    def _setup_ui(self):
        self.setWindowTitle("Sélection ECG — parties à inclure dans le PDF")
        self.setModal(True)
        self.setMinimumSize(900, 600)
        self.setStyleSheet(f"background:{C_NAVY};")

        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(16, 12, 16, 12)

        lbl_title = QLabel("Sélectionnez les parties à inclure dans le PDF")
        lbl_title.setAlignment(Qt.AlignCenter)
        lbl_title.setStyleSheet(f"color:{C_WHITE}; font-size:14px; font-weight:700;")
        root.addWidget(lbl_title)

        lbl_hint = QLabel("Cliquez et glissez sur le graphe pour sélectionner une partie")
        lbl_hint.setAlignment(Qt.AlignCenter)
        lbl_hint.setStyleSheet(f"color:{C_LGRAY}; font-size:10px;")
        root.addWidget(lbl_hint)

        self.plot = pg.PlotWidget(background=C_PURPLE)
        self.plot.setMinimumHeight(240)
        self.plot.setLabel("bottom", "Temps (mm:ss)")
        self.plot.setLabel("left", "Amplitude (mV)")
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.setMenuEnabled(False)
        vb = self.plot.getViewBox()
        vb.mousePressEvent   = self._vb_press
        vb.mouseMoveEvent    = self._vb_move
        vb.mouseReleaseEvent = self._vb_release
        root.addWidget(self.plot)

        lbl_bpm = QLabel("Fréquence cardiaque (enregistrement complet)")
        lbl_bpm.setStyleSheet(f"color:{C_LPINK}; font-size:10px; font-weight:600;")
        root.addWidget(lbl_bpm)

        self.bpm_plot = pg.PlotWidget(background=C_NAVY)
        self.bpm_plot.setMinimumHeight(110)
        self.bpm_plot.setMaximumHeight(140)
        self.bpm_plot.setLabel("bottom", "Temps (mm:ss)")
        self.bpm_plot.setLabel("left", "bpm")
        self.bpm_plot.showGrid(x=True, y=True, alpha=0.25)
        self.bpm_plot.setMouseEnabled(x=False, y=False)
        self.bpm_plot.setMenuEnabled(False)
        root.addWidget(self.bpm_plot)

        sel_hdr = QLabel("Parties sélectionnées :")
        sel_hdr.setStyleSheet(f"color:{C_LPINK}; font-size:11px; font-weight:600;")
        root.addWidget(sel_hdr)

        self._sel_container = QWidget()
        self._sel_container.setStyleSheet(f"background:{C_PURPLE};")
        self._sel_lay = QVBoxLayout(self._sel_container)
        self._sel_lay.setContentsMargins(8, 6, 8, 6)
        self._sel_lay.setSpacing(4)

        self._lbl_empty = QLabel(
            "Aucune sélection — tracez au moins une partie sur le graphe")
        self._lbl_empty.setAlignment(Qt.AlignCenter)
        self._lbl_empty.setStyleSheet(
            f"color:{C_GRAY}; font-size:10px; font-style:italic;")
        self._sel_lay.addWidget(self._lbl_empty)
        self._sel_lay.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(self._sel_container)
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(110)
        scroll.setStyleSheet(
            f"QScrollArea {{ background:{C_PURPLE}; "
            f"border:1px solid {C_GRAY}; border-radius:6px; }}")
        root.addWidget(scroll)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("Annuler")
        btn_cancel.setMinimumHeight(40)
        btn_cancel.setStyleSheet(
            f"background:{C_GRAY}; color:{C_WHITE}; border-radius:8px; "
            f"padding:6px 20px; font-weight:700;")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)

        self.btn_generate = QPushButton("Générer le PDF")
        self.btn_generate.setMinimumHeight(40)
        self.btn_generate.setEnabled(False)
        self.btn_generate.setStyleSheet(
            f"QPushButton {{ background:{C_PINK}; color:{C_WHITE}; "
            f"border-radius:8px; padding:6px 24px; font-weight:700; }}"
            f"QPushButton:disabled {{ background:#2a3550; color:{C_GRAY}; }}")
        self.btn_generate.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_generate)
        root.addLayout(btn_row)

    def _load_and_plot(self):
        try:
            df = pd.read_csv(self.csv_path)
            t0 = df["time"].iloc[0]
            df["t_s"] = (df["time"] - t0) / 1e9
            df_ecg = df.dropna(subset=["ecg"]).reset_index(drop=True)
            if df_ecg.empty:
                return

            t   = df_ecg["t_s"].values
            raw = df_ecg["ecg"].values.astype(float)
            self._total_s = float(t[-1])

            step = max(1, len(t) // 8000)
            self.plot.plot(t[::step], raw[::step], pen=pg.mkPen(C_PINK, width=1))
            self.plot.setXRange(0, self._total_s, padding=0.02)

            def _mmss_tick(values, scale, spacing):
                return [fmt_mmss(v) for v in values]

            self.plot.getAxis("bottom").tickStrings = _mmss_tick
            self.bpm_plot.getAxis("bottom").tickStrings = _mmss_tick

            hr_df = df[df["hr"].notna()].copy()
            if len(hr_df) > 1:
                ht = hr_df["t_s"].values
                hv = hr_df["hr"].values
                self.bpm_plot.plot(ht, hv,
                                   pen=pg.mkPen("#2980B9", width=1.5),
                                   symbol="o", symbolSize=4,
                                   symbolBrush=pg.mkBrush(TEAL_ACC))
                self.bpm_plot.setXRange(0, self._total_s, padding=0.02)

        except Exception as e:
            logging.error(f"[selector] {e}")

    # ── Sélection par clic-glisser ─────────────────────────────────────────────

    def _vb_x(self, event):
        vb = self.plot.getViewBox()
        pos = vb.mapSceneToView(event.scenePos())
        return max(0.0, min(float(pos.x()), self._total_s))

    def _vb_press(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_start = self._vb_x(event)
            self._drag_item = pg.LinearRegionItem(
                values=[self._drag_start, self._drag_start],
                movable=False,
                brush=pg.mkBrush(198, 22, 141, 60),
                pen=pg.mkPen(C_PINK, width=2),
            )
            self._drag_item.setZValue(50)
            self.plot.addItem(self._drag_item)
            event.accept()
        else:
            pg.ViewBox.mousePressEvent(self.plot.getViewBox(), event)

    def _vb_move(self, event):
        if self._drag_start is not None and self._drag_item is not None:
            x = self._vb_x(event)
            self._drag_item.setRegion([min(self._drag_start, x),
                                       max(self._drag_start, x)])
            event.accept()

    def _vb_release(self, event):
        if event.button() == Qt.LeftButton and self._drag_start is not None:
            x = self._vb_x(event)
            s = round(min(self._drag_start, x), 1)
            e = round(max(self._drag_start, x), 1)
            self.plot.removeItem(self._drag_item)
            self._drag_item  = None
            self._drag_start = None
            if e - s >= 0.5:
                self._add_selection(s, e)
            event.accept()
        else:
            pg.ViewBox.mouseReleaseEvent(self.plot.getViewBox(), event)

    def _add_selection(self, s, e):
        rng = (s, e)
        self._selected.append(rng)

        vis = pg.LinearRegionItem(
            values=[s, e], movable=False,
            brush=pg.mkBrush(76, 175, 80, 45),
            pen=pg.mkPen(C_GREEN, width=1.5),
        )
        vis.setZValue(1)
        self.plot.addItem(vis)
        self._added_vis.append(vis)

        if self._lbl_empty.isVisible():
            self._lbl_empty.hide()

        row = QFrame()
        row.setStyleSheet(f"background:{C_NAVY}; border-radius:4px;")
        rl = QHBoxLayout(row)
        rl.setContentsMargins(8, 2, 8, 2)
        lbl = QLabel(
            f"Partie {len(self._selected)} :  "
            f"{fmt_mmss(s)} → {fmt_mmss(e)}  ({e - s:.0f} s)")
        lbl.setStyleSheet(f"color:{C_WHITE}; font-size:10px;")
        rl.addWidget(lbl)
        rl.addStretch()
        btn_del = QPushButton("✕")
        btn_del.setFixedSize(22, 22)
        btn_del.setStyleSheet(
            f"background:{C_RED}; color:{C_WHITE}; border-radius:4px; "
            f"font-size:9px; font-weight:700;")
        btn_del.clicked.connect(
            lambda _, v=vis, w=row, r=rng: self._remove_selection(v, w, r))
        rl.addWidget(btn_del)
        self._sel_lay.insertWidget(self._sel_lay.count() - 1, row)
        self.btn_generate.setEnabled(True)

    def _remove_selection(self, vis_item, row_widget, rng):
        if rng in self._selected:
            self._selected.remove(rng)
        self.plot.removeItem(vis_item)
        if vis_item in self._added_vis:
            self._added_vis.remove(vis_item)
        row_widget.deleteLater()
        if not self._selected:
            self._lbl_empty.show()
            self.btn_generate.setEnabled(False)

    def get_selected_ranges(self):
        return list(self._selected)
