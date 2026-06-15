#!/usr/bin/env python3
"""Polar H10 — Application graphique d'enregistrement ECG + rapport PDF automatique."""

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
from io import BytesIO

try:
    from PyQt5.QtWidgets import (
        QApplication, QMainWindow, QWidget,
        QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFrame,
        QDoubleSpinBox, QCheckBox, QFormLayout, QGroupBox,
        QDialog, QScrollArea, QProgressBar,
    )
    from PyQt5.QtCore import Qt, QObject, QTimer, pyqtSignal, QThread
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

# ── Imports PDF (optionnels — l'app fonctionne sans) ─────────────────────────
try:
    import numpy as np
    import pandas as pd
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from scipy.signal import butter, filtfilt, find_peaks
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        Image as RLImage, HRFlowable,
    )
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

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
C_ORANGE = "#ff9800"

# PDF palette
RED_ECG  = "#C0392B"
GRID_MAJ = "#F5CCCC"
GRID_MIN = "#FAE8E8"
BLUE_HDR = "#1A3A5C"
TEAL_ACC = "#1ABC9C"

ECG_WINDOW_S = 5
ECG_FS       = 130
ECG_SAMPLES  = ECG_WINDOW_S * ECG_FS

HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
PMD_CONTROL    = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA       = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"
ECG_START_CMD  = bytearray([0x02, 0x00, 0x00, 0x01, 0x82, 0x00, 0x01, 0x01, 0x0E, 0x00])
ECG_START_133  = bytearray([0x02, 0x00, 0x00, 0x01, 0x85, 0x00, 0x01, 0x01, 0x0E, 0x00])
ECG_STOP_CMD   = bytearray([0x03, 0x00])
POLAR_KEYWORDS = ("polar", "h10")


def _parse_hr(data: bytearray) -> tuple:
    flags = data[0]
    hr = int.from_bytes(data[1:3], "little") if (flags & 0x01) else data[1]
    offset = 3 if (flags & 0x01) else 2
    if flags & 0x08:
        offset += 2
    rr_ms = 0
    if flags & 0x10 and len(data) >= offset + 2:
        rr_ms = round(int.from_bytes(data[offset:offset + 2], "little") * 1000 / 1024)
    return hr, rr_ms


def _dbus_pair_and_trust(address: str, p) -> bool:
    try:
        import dbus
        bus      = dbus.SystemBus()
        dev_path = "/org/bluez/hci0/dev_" + address.replace(":", "_")
        dev_obj  = bus.get_object("org.bluez", dev_path)
        props    = dbus.Interface(dev_obj, "org.freedesktop.DBus.Properties")
        if bool(props.Get("org.bluez.Device1", "Paired")):
            p("[pair] Déjà appairé")
            try:
                props.Set("org.bluez.Device1", "Trusted", dbus.Boolean(True))
            except Exception:
                pass
            return True
        p("[pair] Lancement appairage D-Bus…")
        dbus.Interface(dev_obj, "org.bluez.Device1").Pair()
        p("[pair] Appairage OK")
        props.Set("org.bluez.Device1", "Trusted", dbus.Boolean(True))
        p("[pair] Trusted = True")
        return True
    except Exception as e:
        p(f"[pair] Erreur : {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# PDF GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

def _bandpass_filter(signal, fs, low=0.5, high=40.0):
    nyq = fs / 2.0
    b, a = butter(4, [low / nyq, high / nyq], btype="band")
    return filtfilt(b, a, signal)


def _detect_r_peaks(ecg_filtered, fs):
    distance  = int(0.4 * fs)
    height_thr = 0.3 * np.max(ecg_filtered)
    peaks, props = find_peaks(ecg_filtered, distance=distance,
                              height=height_thr, prominence=0.1)
    return peaks, props


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


def _adaptive_x_spacing(duration_s):
    candidates = [(0.04,0.20),(0.20,1.00),(0.50,2.50),(1.00,5.00),
                  (2.00,10.0),(5.00,25.0),(10.0,50.0)]
    for minor, major in candidates:
        if duration_s / minor <= 500:
            return minor, major
    return 10.0, 50.0


def _fmt_mmss(x, _=None):
    s = int(x)
    return f"{s//60:02d}:{s%60:02d}"

_mmss_formatter = plt.FuncFormatter(_fmt_mmss)


def _ecg_grid(ax, t, signal, peaks=None, title=""):
    ax.set_facecolor("#FFFAFA")
    duration  = float(t[-1] - t[0]) if len(t) > 1 else 1.0
    xm, xM    = _adaptive_x_spacing(duration)
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

    hr_df = df[df["hr"].notna()][["t_s","hr"]]
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
    """Graphe BPM sur la totalité de l'enregistrement."""
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


def generate_pdf(csv_path: str, pdf_path: str,
                 bpm_threshold: float = None,
                 window_s: float = 15.0,
                 time_ranges=None,
                 progress_cb=None,
                 progress_pct_cb=None) -> str:
    """Génère le PDF à partir du CSV. Retourne le chemin du PDF ou lève une exception."""

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
    dt  = np.diff(t)
    fs  = 1.0 / np.median(dt)

    prog(f"{len(df_ecg)} échantillons, fs≈{fs:.1f} Hz, durée={t[-1]:.1f} s", pct=8)

    prog("Filtrage et détection des pics R…", pct=10)
    ecg_f = _bandpass_filter(raw, fs)
    peaks, _ = _detect_r_peaks(ecg_f, fs)
    stats = _compute_stats(df_ecg, peaks, fs)
    prog(f"{len(peaks)} pics R, FC moy={stats['mean_hr']} bpm", pct=18)

    t_data_min = float(df_ecg["t_s"].iloc[0])
    t_data_max = float(df_ecg["t_s"].iloc[-1])
    ranges = time_ranges if time_ranges else [(t_data_min, t_data_max)]

    # Calcul du nombre total de tranches pour la progression
    import math as _math
    total_slices = sum(
        max(1, _math.ceil((min(float(r_end), t_data_max) - max(float(r_start), t_data_min)) / window_s))
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
            pct_fig = 20 + int(done / max(total_slices, 1) * 70)
            prog(f"Tranche {done}/{total_slices}…", pct=pct_fig)
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
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    story.append(Paragraph("Rapport ECG", title_sty))
    story.append(Paragraph(
        f"Généré le {now} &nbsp;|&nbsp; "
        f"Fichier : <b>{os.path.basename(csv_path)}</b>",
        sub_sty))
    story.append(HRFlowable(width="100%", thickness=2,
                             color=colors.HexColor(BLUE_HDR), spaceAfter=8))

    # Tableau de stats
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

    # Graphe BPM complet
    if bpm_buf:
        story.append(Paragraph("Fréquence cardiaque — enregistrement complet", sec_sty))
        bpm_cap = "Courbe FC sur la totalité de l'enregistrement"
        if bpm_threshold:
            bpm_cap += f"  |  <font color='#C0392B'><b>Seuil : {bpm_threshold:.0f} bpm</b></font>"
        story.append(Paragraph(bpm_cap, cap_sty))
        story.append(RLImage(bpm_buf, width=W, height=W * 3/16))
        story.append(Spacer(1, 8))

    # Tracés ECG
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

    # Histogramme RR
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

    # Disclaimer
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
# DIALOGUE : CHOIX PÉRIMÈTRE PDF
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
        lbl.setStyleSheet(
            f"color:{C_WHITE}; font-size:13px; font-weight:700;")
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
    """Graphique ECG complet avec région déplaçable pour choisir les parties."""

    def __init__(self, csv_path: str, parent=None):
        super().__init__(parent)
        self.csv_path   = csv_path
        self._selected  = []   # list of (t_start, t_end)
        self._added_vis = []   # LinearRegionItems figées (vert)
        self._total_s   = 0.0
        self._drag_start = None   # x position où le clic a débuté
        self._drag_item  = None   # LinearRegionItem temporaire (en cours de tracé)
        self._setup_ui()
        self._load_and_plot()

    # ── UI ────────────────────────────────────────────────────────────────────

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
        lbl_title.setStyleSheet(
            f"color:{C_WHITE}; font-size:14px; font-weight:700;")
        root.addWidget(lbl_title)

        lbl_hint = QLabel("Cliquez et glissez sur le graphe pour sélectionner une partie")
        lbl_hint.setAlignment(Qt.AlignCenter)
        lbl_hint.setStyleSheet(f"color:{C_LGRAY}; font-size:10px;")
        root.addWidget(lbl_hint)

        # Graphe ECG
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

        # Graphe BPM
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

        # Liste des sélections
        sel_hdr = QLabel("Parties sélectionnées :")
        sel_hdr.setStyleSheet(
            f"color:{C_LPINK}; font-size:11px; font-weight:600;")
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

        # Boutons bas
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

    # ── Chargement et tracé ───────────────────────────────────────────────────

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
            self.plot.plot(t[::step], raw[::step],
                           pen=pg.mkPen(C_PINK, width=1))
            self.plot.setXRange(0, self._total_s, padding=0.02)

            def _mmss_tick(values, scale, spacing):
                return [f"{int(v)//60:02d}:{int(v)%60:02d}" for v in values]

            self.plot.getAxis("bottom").tickStrings = _mmss_tick
            self.bpm_plot.getAxis("bottom").tickStrings = _mmss_tick

            # Tracé BPM
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

    # ── Sélection par clic-glisser ────────────────────────────────────────────

    def _vb_x(self, event):
        """Convertit la position souris en secondes dans le ViewBox."""
        vb = self.plot.getViewBox()
        pos = vb.mapSceneToView(event.scenePos())
        return max(0.0, min(float(pos.x()), self._total_s))

    def _vb_press(self, event):
        from PyQt5.QtCore import Qt as _Qt
        if event.button() == _Qt.LeftButton:
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
            s = min(self._drag_start, x)
            e = max(self._drag_start, x)
            self._drag_item.setRegion([s, e])
            event.accept()

    def _vb_release(self, event):
        from PyQt5.QtCore import Qt as _Qt
        if event.button() == _Qt.LeftButton and self._drag_start is not None:
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

    # ── Ajout / suppression de sélections ────────────────────────────────────

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

        def fmt(v):
            return f"{int(v // 60):02d}:{int(v % 60):02d}"

        row = QFrame()
        row.setStyleSheet(f"background:{C_NAVY}; border-radius:4px;")
        rl = QHBoxLayout(row)
        rl.setContentsMargins(8, 2, 8, 2)
        lbl = QLabel(
            f"Partie {len(self._selected)} :  "
            f"{fmt(s)} → {fmt(e)}  ({e - s:.0f} s)")
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


# ═══════════════════════════════════════════════════════════════════════════════
# THREAD DE GÉNÉRATION PDF (pour ne pas bloquer l'UI)
# ═══════════════════════════════════════════════════════════════════════════════

class PdfWorker(QThread):
    progress     = pyqtSignal(str)
    progress_pct = pyqtSignal(int)
    finished     = pyqtSignal(str)   # chemin PDF
    error        = pyqtSignal(str)

    def __init__(self, csv_path, bpm_threshold=None, window_s=15.0,
                 time_ranges=None):
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
# CSV WRITER
# ═══════════════════════════════════════════════════════════════════════════════

class CSVWriter:
    def __init__(self, path: str):
        self.path     = path
        self.ecg_mode = False
        self._f       = open(path, "w", newline="", encoding="utf-8")
        self._w       = csv.writer(self._f)
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


# ═══════════════════════════════════════════════════════════════════════════════
# BLE WORKER
# ═══════════════════════════════════════════════════════════════════════════════

class BLEWorker(QObject):
    status_msg       = pyqtSignal(str, str)
    device_found     = pyqtSignal(str)
    connected_sig    = pyqtSignal(str)
    disconnected_sig = pyqtSignal()
    hr_sig           = pyqtSignal(int, int)
    ecg_count_sig    = pyqtSignal(int)
    ecg_ok_sig       = pyqtSignal(bool)
    file_sig         = pyqtSignal(str)          # CSV path quand ouvert
    recording_done   = pyqtSignal(str)          # CSV path quand fermé

    def __init__(self):
        super().__init__()
        self._loop = self._client = self._writer = None
        self._recording = self._connected = self._running = False
        self._sample_count = 0
        self._stop_evt = self._ctrl_evt = None
        self._ctrl_resp = b""
        self._time_offset = None
        self.ecg_buffer = deque(maxlen=ECG_SAMPLES)

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
                    None, lambda: _dbus_pair_and_trust(address, p))
                if paired:
                    self.status_msg.emit(f"✓  Connecté — {name}", C_GREEN)
                    await asyncio.sleep(0.5)
                else:
                    self.status_msg.emit("Non appairé — PMD_DATA peut échouer", C_LGRAY)
                self.connected_sig.emit(name)

                async def _hr_cb(_, data: bytearray):
                    hr, rr = _parse_hr(data)
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
            self.recording_done.emit(path)   # ← déclenche la génération PDF


# ═══════════════════════════════════════════════════════════════════════════════
# INTERFACE GRAPHIQUE
# ═══════════════════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker     = BLEWorker()
        self._pdf_worker = None
        self._last_csv  = None
        self._connect_signals()
        self._build_ui()
        self.setWindowTitle("Polar H10 — Enregistreur ECG")
        self.setFixedSize(520, 780)
        self._timer = QTimer()
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._refresh_ecg)
        self._timer.start()

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
        w.recording_done.connect(self._on_recording_done)  # ← nouveau

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

        # Carte HR
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
        self.lbl_hr.setStyleSheet(
            f"color:{C_PINK}; font-size:56px; font-weight:700;")
        hr_row.addWidget(self.lbl_hr)
        lbl_bpm = QLabel("bpm")
        lbl_bpm.setStyleSheet(
            f"color:{C_LGRAY}; font-size:16px; padding-top:26px;")
        hr_row.addWidget(lbl_bpm)
        cv.addLayout(hr_row)

        self.lbl_rr = QLabel("")
        self.lbl_rr.setAlignment(Qt.AlignCenter)
        self.lbl_rr.setStyleSheet(f"color:{C_LGRAY}; font-size:11px;")
        cv.addWidget(self.lbl_rr)
        root.addWidget(card)

        # Graphe ECG
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
        self.ecg_plot.addLine(
            y=0, pen=pg.mkPen(C_GRAY, width=1, style=Qt.DotLine))
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

        # ── Options PDF ───────────────────────────────────────────────────────
        if PDF_AVAILABLE:
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

            root.addWidget(pdf_box)
        else:
            self.chk_pdf    = None
            self.spin_window = None
            self.chk_seuil  = None
            self.spin_seuil = None
            self.lbl_pdf = QLabel("PDF désactivé (numpy/pandas/reportlab manquants)")
            self.lbl_pdf.setStyleSheet(f"color:{C_LGRAY}; font-size:10px;")
            root.addWidget(self.lbl_pdf)

        # Boutons
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
        if len(buf) < 2:
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
        if self.lbl_pdf:
            self.lbl_pdf.setText("")
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
            self.lbl_ecg.setText("ECG non disponible")

    def _on_file(self, path):
        self._last_csv = path
        self.lbl_file.setText(os.path.basename(path))

    def _on_recording_done(self, csv_path):
        """Appelé dès que le fichier CSV est fermé — lance la génération PDF."""
        if not PDF_AVAILABLE:
            return
        if self.chk_pdf and not self.chk_pdf.isChecked():
            return

        # ── Demander : tout l'enregistrement ou parties sélectionnées ? ──────
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
                self.lbl_pdf.setStyleSheet(
                    f"color:{C_LGRAY}; font-size:10px;")
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
        self._pdf_worker.progress.connect(
            lambda m: self.lbl_pdf.setText(f"⏳ {m}"))
        self._pdf_worker.progress_pct.connect(self.pdf_progress.setValue)
        self._pdf_worker.finished.connect(self._on_pdf_done)
        self._pdf_worker.error.connect(self._on_pdf_error)
        self._pdf_worker.start()

    def _on_open_csv(self):
        """Ouvre un fichier CSV existant et lance l'export PDF."""
        from PyQt5.QtWidgets import QFileDialog
        data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        csv_path, _ = QFileDialog.getOpenFileName(
            self, "Choisir un fichier CSV", data_dir,
            "Fichiers CSV (*.csv);;Tous les fichiers (*)")
        if not csv_path:
            return
        self._on_recording_done(csv_path)

    def _on_pdf_done(self, pdf_path):
        name = os.path.basename(pdf_path)
        self.lbl_pdf.setText(f"✓ PDF : {name}")
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
# ENTRY POINT
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
        import dbus
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
