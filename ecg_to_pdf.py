#!/usr/bin/env python3
"""
ECG PDF Report Generator
Reads a CSV ECG file and produces a clean medical-style PDF report.

Usage: python ecg_to_pdf.py <input.csv> [output.pdf]
"""

import sys
import os
import argparse
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from datetime import datetime
from io import BytesIO
from scipy.signal import find_peaks

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm, cm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Image as RLImage, HRFlowable, PageBreak
)

from theme import RED_ECG, GRID_MAJ, GRID_MIN, BLUE_HDR, TEAL_ACC
from ecg_signal import bandpass_filter, adaptive_x_spacing


# ─── Signal processing ───────────────────────────────────────────────────────

def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {"time", "ecg"}
    if not required.issubset(df.columns):
        raise ValueError(f"CSV must contain columns: {required}. Found: {list(df.columns)}")
    t0 = df["time"].iloc[0]
    df["t_s"] = (df["time"] - t0) / 1e9
    df["ecg"] = pd.to_numeric(df["ecg"], errors="coerce")

    # ── Timestamp de début de l'enregistrement (colonne time = nanosecondes Unix)
    # On le stocke pour afficher l'heure absolue de la FC max
    df["time_abs"] = pd.to_datetime(df["time"], unit="ns", utc=True).dt.tz_convert("Europe/Paris")

    df_ecg = df.dropna(subset=["ecg"]).reset_index(drop=True)
    if df_ecg.empty:
        raise ValueError(
            "Aucune donnée ECG dans ce fichier (colonne 'ecg' vide). "
            "Vérifiez que l'enregistrement ECG était actif."
        )
    return df_ecg


def detect_r_peaks(ecg_filtered: np.ndarray, fs: float):
    """Détecteur de pics R médical.
    Définition : un pic R est tout maximum local du signal filtré
    dont l'amplitude absolue dépasse 0.5 mV (standard papier ECG).
    - height    : 0.5 mV fixe (pas relatif au max — évite de rater les pics faibles)
    - distance  : 300 ms mini entre 2 pics → supporte jusqu'à 200 bpm
    - prominence: 0.05 mV → ignore les micro-ondulations
    """
    AMPL_THR = 0.5          # mV — standard ECG : pic R > 0.5 mV
    distance = int(0.30 * fs)  # 300 ms mini

    peaks, props = find_peaks(
        ecg_filtered,
        distance=distance,
        height=AMPL_THR,       # seuil absolu 0.5 mV
        prominence=0.05,
    )

    import logging
    logging.info(
        f"[peaks] fs={fs:.1f}Hz  max={np.max(ecg_filtered):.3f}mV  "
        f"seuil_absolu={AMPL_THR}mV  distance={distance}samples  "
        f"trouvés={len(peaks)}"
    )
    return peaks, props


def compute_stats(df: pd.DataFrame, peaks: np.ndarray, fs: float) -> dict:
    duration = df["t_s"].iloc[-1] - df["t_s"].iloc[0]
    n_beats  = len(peaks)
    mean_hr  = (n_beats / duration) * 60 if duration > 0 else 0

    rr_intervals_ms = []
    if len(peaks) > 1:
        rr_intervals_ms = np.diff(peaks) / fs * 1000

    hr_from_csv = df["hr"].dropna()

    if len(hr_from_csv) >= 3:
        mean_hr = float(hr_from_csv.mean())

    sdnn  = float(np.std(rr_intervals_ms))  if len(rr_intervals_ms) > 1 else None
    rmssd = float(np.sqrt(np.mean(np.diff(rr_intervals_ms) ** 2))) \
        if len(rr_intervals_ms) > 1 else None

    # ── FC max : valeur + instant relatif (t_s) + heure absolue ──────────────
    hr_max_val      = None
    hr_max_t_s      = None   # secondes depuis le début
    hr_max_time_abs = None   # heure réelle HH:MM:SS

    if len(hr_from_csv) > 0:
        hr_col = df[df["hr"].notna()][["t_s", "hr", "time_abs"]]
        idx_max = hr_col["hr"].idxmax()
        hr_max_val      = round(float(hr_col.loc[idx_max, "hr"]), 1)
        hr_max_t_s      = round(float(hr_col.loc[idx_max, "t_s"]), 1)
        try:
            hr_max_time_abs = hr_col.loc[idx_max, "time_abs"].strftime("%H:%M:%S")
        except Exception:
            hr_max_time_abs = None

    return {
        "duration_s":        round(duration, 1),
        "fs_hz":             round(fs, 1),
        "n_beats":           n_beats,
        "mean_hr":           round(mean_hr, 1),
        "min_hr":            round(float(hr_from_csv.min()), 1) if len(hr_from_csv) else None,
        "max_hr":            hr_max_val,
        "max_hr_t_s":        hr_max_t_s,        # ← nouveau
        "max_hr_time_abs":   hr_max_time_abs,   # ← nouveau
        "ecg_min_mv":        round(float(df["ecg"].min()), 3),
        "ecg_max_mv":        round(float(df["ecg"].max()), 3),
        "ecg_mean_mv":       round(float(df["ecg"].mean()), 3),
        "rr_mean_ms":        round(float(np.mean(rr_intervals_ms)), 1) if len(rr_intervals_ms) > 0 else None,
        "rr_std_ms":         round(sdnn, 1)  if sdnn  else None,
        "rmssd_ms":          round(rmssd, 1) if rmssd else None,
    }


# ─── Matplotlib figures ───────────────────────────────────────────────────────

def _ecg_grid(ax, t, signal, peaks=None, title="ECG Signal",
              hr_max_t_s=None, hr_max_val=None):
    """Tracé ECG avec grille médicale standard.
    Axe X : petite case = 0.04 s (1 mm à 25 mm/s), grande case = 0.20 s (5 mm).
    Axe Y : petite case = 0.1 mV (1 mm), grande case = 0.5 mV (5 mm).
    """
    ax.set_facecolor("#FFFAFA")

    duration = float(t[-1] - t[0]) if len(t) > 1 else 1.0
    x_minor, x_major = adaptive_x_spacing(duration)

    # ── Grille X (temps) ─────────────────────────────────────────────────
    ax.xaxis.set_minor_locator(plt.MultipleLocator(x_minor))
    ax.xaxis.set_major_locator(plt.MultipleLocator(x_major))

    # ── Grille Y (amplitude) — standard médical : 0.1 mV / 0.5 mV ──────
    # Petite case = 0.1 mV, grande case = 0.5 mV (identique papier ECG)
    ax.yaxis.set_minor_locator(plt.MultipleLocator(0.1))
    ax.yaxis.set_major_locator(plt.MultipleLocator(0.5))

    # Grille mineure (petites cases) : rose très clair
    ax.grid(which="minor", color=GRID_MIN, linewidth=0.3, zorder=0)
    # Grille majeure (grandes cases) : rose moyen, plus visible
    ax.grid(which="major", color=GRID_MAJ, linewidth=0.8, zorder=0)

    # Signal ECG
    ax.plot(t, signal, color=RED_ECG, linewidth=0.8, antialiased=True, zorder=2)

    if peaks is not None and len(peaks) > 0:
        ax.scatter(t[peaks], signal[peaks], color="#E74C3C",
                   s=18, zorder=5, label="R-peak")

    # ── Marqueur FC max (ligne verticale orange) ──────────────────────────
    if hr_max_t_s is not None and float(t[0]) <= hr_max_t_s <= float(t[-1]):
        ax.axvline(hr_max_t_s, color="#E67E22", linewidth=1.2,
                   linestyle="--", zorder=6,
                   label=f"FC max ({hr_max_val:.0f} bpm)" if hr_max_val else "FC max")

    ax.set_title(title, fontsize=8, color=BLUE_HDR, fontweight="bold", pad=3)
    ax.set_ylabel("Amplitude (mV)", fontsize=7, color="#555")
    ax.set_xlabel("Temps (s)", fontsize=7, color="#555")
    ax.tick_params(axis="y", labelsize=6)
    ax.tick_params(axis="x", labelsize=6, rotation=45)
    ax.xaxis.set_major_formatter(plt.FormatStrFormatter("%.2f"))

    # Étiquettes X seulement sur les grandes cases
    ax.xaxis.set_tick_params(which="minor", labelbottom=False)

    for spine in ax.spines.values():
        spine.set_edgecolor("#CCC")


def make_ecg_figure_slice(df: pd.DataFrame, ecg_filtered: np.ndarray,
                          peaks: np.ndarray, stats: dict,
                          t_start: float, t_end: float,
                          minute_idx: int,
                          bpm_threshold: float | None = None) -> BytesIO:
    t   = df["t_s"].values
    raw = df["ecg"].values

    mask      = (t >= t_start) & (t < t_end)
    t_win     = t[mask]
    raw_win   = raw[mask]

    # 4 graphes : brut / BPM-RR calculé / BPM brut CSV / RR→BPM brut CSV
    fig, axes = plt.subplots(4, 1, figsize=(16, 11),
                              gridspec_kw={"hspace": 0.70,
                                           "height_ratios": [2, 1.2, 1.0, 1.0]})
    fig.patch.set_facecolor("white")

    # ── Alertes ───────────────────────────────────────────────────────────
    alert = ""
    if bpm_threshold is not None:
        hr_df_all  = df[df["hr"].notna()][["t_s", "hr"]]
        hr_win_all = hr_df_all[(hr_df_all["t_s"] >= t_start) & (hr_df_all["t_s"] < t_end)]
        if len(hr_win_all) > 0 and (hr_win_all["hr"] > bpm_threshold).any():
            alert = f"  ⚠ FC > {bpm_threshold:.0f} bpm"

    hr_max_t_s = stats.get("max_hr_t_s")
    hr_max_val = stats.get("max_hr")
    in_window  = (hr_max_t_s is not None and t_start <= hr_max_t_s < t_end)
    if in_window and not alert:
        alert = f"  ★ FC max : {hr_max_val:.0f} bpm"

    title_color = "#C0392B" if alert else BLUE_HDR
    fig.suptitle(
        f"Tranche {minute_idx}  —  "
        f"{int(t_start//60):02d}:{int(t_start%60):02d}"
        f" → {int(t_end//60):02d}:{int(t_end%60):02d}  (mm:ss){alert}",
        fontsize=10, color=title_color, fontweight="bold", y=0.99
    )

    mx_t = hr_max_t_s if in_window else None
    mx_v = hr_max_val if in_window else None

    # ── axes[0] : Signal brut ─────────────────────────────────────────────
    _ecg_grid(axes[0], t_win, raw_win, title="Signal ECG brut",
              hr_max_t_s=mx_t, hr_max_val=mx_v)
    axes[0].set_xlim(t_start, t_end)
    if mx_t is not None:
        axes[0].legend(fontsize=7, loc="upper right")

    # ── axes[1] : BPM instantané depuis intervalles RR (pics > 0.5 mV) ───
    # Calcul : pour chaque pic R valide (amplitude > 0.5 mV),
    # BPM = 60 / RR_s avec le pic valide précédent.
    # Même axe X que les graphes ECG (t_start → t_end).
    AMPL_THR = 0.5  # mV

    # Pics valides sur l'ensemble du signal (pour avoir le pic précédent
    # même s'il est avant t_start)
    pk_valid_all = np.array([
        p for p in peaks
        if 0 <= p < len(ecg_filtered) and ecg_filtered[p] > AMPL_THR
    ])

    rr_t_pts, rr_bpm_pts = [], []
    for i, p in enumerate(pk_valid_all):
        if i == 0:
            continue
        rr_s = float(t[p] - t[pk_valid_all[i - 1]])
        if 0.25 < rr_s < 2.5:   # garde-fou 24–240 bpm
            rr_t_pts.append(float(t[p]))
            rr_bpm_pts.append(60.0 / rr_s)

    rr_t_pts   = np.array(rr_t_pts)
    rr_bpm_pts = np.array(rr_bpm_pts)

    # Filtrer sur la fenêtre courante
    win_mask   = (rr_t_pts >= t_start) & (rr_t_pts <= t_end)
    rr_t_win   = rr_t_pts[win_mask]
    rr_bpm_win = rr_bpm_pts[win_mask]

    ax_rr = axes[1]
    ax_rr.set_facecolor("#F5F8FF")
    ax_rr.grid(axis="y", color="#D0DCF0", linewidth=0.5, zorder=0)
    ax_rr.grid(axis="x", color="#E0E8F5", linewidth=0.3, zorder=0)

    if len(rr_t_win) >= 2:
        # Courbe
        line_c = ["#C0392B" if (bpm_threshold and v > bpm_threshold)
                  else "#2980B9" for v in rr_bpm_win]
        for i in range(len(rr_t_win) - 1):
            ax_rr.plot(rr_t_win[i:i+2], rr_bpm_win[i:i+2],
                       color=line_c[i], linewidth=1.8, zorder=3)

        # Points
        ax_rr.scatter(rr_t_win, rr_bpm_win, s=30, zorder=4,
                      c=["#C0392B" if (bpm_threshold and v > bpm_threshold)
                         else TEAL_ACC for v in rr_bpm_win],
                      edgecolors="white", linewidths=0.5)

        # Zone remplie
        ax_rr.fill_between(rr_t_win, rr_bpm_win,
                           alpha=0.10, color="#2980B9", zorder=1)

        # Valeur annotée au-dessus de chaque point
        for xt, xv in zip(rr_t_win, rr_bpm_win):
            ax_rr.annotate(f"{xv:.0f}",
                           xy=(xt, xv),
                           xytext=(0, 5), textcoords="offset points",
                           ha="center", fontsize=6,
                           color="#C0392B" if (bpm_threshold and xv > bpm_threshold)
                                 else "#2C3E50",
                           fontweight="bold")

        # Ligne seuil
        if bpm_threshold is not None:
            ax_rr.axhline(bpm_threshold, color="#C0392B",
                          linewidth=0.9, linestyle="--", zorder=4)

        # Ligne FC max orange
        if mx_t is not None:
            ax_rr.axvline(mx_t, color="#E67E22", linewidth=1.0,
                          linestyle="--", zorder=4)

        mean_bpm = float(np.mean(rr_bpm_win))
        mean_rr  = float(np.mean(60.0 / rr_bpm_win * 1000))  # ms

        ax_rr.axhline(mean_bpm, color="#27AE60", linewidth=1.0,
                      linestyle=":", zorder=4)

        y_min = max(20, rr_bpm_win.min() - 10)
        y_max = rr_bpm_win.max() + 20
        ax_rr.set_ylim(y_min, y_max)
        ax_rr.yaxis.set_major_locator(plt.MultipleLocator(10))

        # ── Texte résumé sous le graphe ───────────────────────────────────
        summary = (f"Moy BPM : {mean_bpm:.1f}   |   "
                   f"Moy RR : {mean_rr:.0f} ms   |   "
                   f"Min : {rr_bpm_win.min():.0f} bpm   |   "
                   f"Max : {rr_bpm_win.max():.0f} bpm   |   "
                   f"N pics valides : {len(rr_t_win)}")
        ax_rr.text(0.5, -0.32, summary,
                   transform=ax_rr.transAxes,
                   ha="center", va="top", fontsize=7,
                   color="#444",
                   bbox=dict(boxstyle="round,pad=0.3",
                             facecolor="#EEF3FB",
                             edgecolor="#BBCFE8",
                             linewidth=0.8))
    else:
        ax_rr.text(0.5, 0.5, "Pas assez de pics R > 0.5 mV",
                   ha="center", va="center",
                   transform=ax_rr.transAxes,
                   fontsize=9, color="#999")

    ax_rr.set_title(f"BPM instantané (RR) — pics R > {AMPL_THR} mV",
                    fontsize=8, color=BLUE_HDR, fontweight="bold", pad=3)
    ax_rr.set_ylabel("BPM", fontsize=7, color="#555")
    ax_rr.set_xlabel("Temps (s)", fontsize=7, color="#555")

    # Même axe X que les graphes ECG
    ax_rr.set_xlim(t_start, t_end)
    ax_rr.tick_params(axis="y", labelsize=6)
    ax_rr.tick_params(axis="x", labelsize=6, rotation=45)
    ax_rr.xaxis.set_major_formatter(plt.FormatStrFormatter("%.2f"))
    for spine in ax_rr.spines.values():
        spine.set_edgecolor("#CCC")

    # ── axes[2] : BPM brut depuis la colonne HR du CSV ──────────────────
    ax_csv_hr = axes[2]
    ax_csv_hr.set_facecolor("#FFFDF0")
    ax_csv_hr.grid(axis="y", color="#EDE8C0", linewidth=0.5, zorder=0)
    ax_csv_hr.grid(axis="x", color="#EDE8C0", linewidth=0.3, zorder=0)

    hr_df  = df[df["hr"].notna()][["t_s", "hr"]]
    hr_win = hr_df[(hr_df["t_s"] >= t_start) & (hr_df["t_s"] <= t_end)]

    if len(hr_win) >= 2:
        ht = hr_win["t_s"].values
        hv = hr_win["hr"].values
        # Segments colorés rouge/bleu selon seuil
        for i in range(len(ht) - 1):
            c = "#C0392B" if (bpm_threshold and max(hv[i], hv[i+1]) > bpm_threshold)                 else "#E67E22"
            ax_csv_hr.plot(ht[i:i+2], hv[i:i+2], color=c, linewidth=1.6, zorder=3)
        ax_csv_hr.scatter(ht, hv, s=22, zorder=4,
                          c=["#C0392B" if (bpm_threshold and v > bpm_threshold)
                             else "#E67E22" for v in hv],
                          edgecolors="white", linewidths=0.4)
        ax_csv_hr.fill_between(ht, hv, alpha=0.10, color="#E67E22", zorder=1)
        # Annotations valeurs
        for xt, xv in zip(ht, hv):
            ax_csv_hr.annotate(f"{xv:.0f}", xy=(xt, xv),
                               xytext=(0, 5), textcoords="offset points",
                               ha="center", fontsize=5.5, fontweight="bold",
                               color="#C0392B" if (bpm_threshold and xv > bpm_threshold)
                                     else "#7D5A00")
        if bpm_threshold is not None:
            ax_csv_hr.axhline(bpm_threshold, color="#C0392B",
                              linewidth=0.9, linestyle="--", zorder=4)
        mean_csv = float(np.mean(hv))
        ax_csv_hr.axhline(mean_csv, color="#27AE60", linewidth=0.9,
                          linestyle=":", zorder=4)
        if mx_t is not None:
            ax_csv_hr.axvline(mx_t, color="#E67E22", linewidth=1.0,
                              linestyle="--", zorder=4)
        ax_csv_hr.set_ylim(max(20, hv.min() - 10), hv.max() + 20)
        ax_csv_hr.yaxis.set_major_locator(plt.MultipleLocator(10))
        # Résumé
        ax_csv_hr.text(0.5, -0.38,
                       f"Moy : {mean_csv:.1f} bpm  |  Min : {hv.min():.0f}  |  "
                       f"Max : {hv.max():.0f}  |  N mesures : {len(hv)}",
                       transform=ax_csv_hr.transAxes,
                       ha="center", va="top", fontsize=7, color="#444",
                       bbox=dict(boxstyle="round,pad=0.3", facecolor="#FEFAE8",
                                 edgecolor="#D4C060", linewidth=0.8))
    else:
        ax_csv_hr.text(0.5, 0.5, "Pas de données HR dans cette tranche",
                       ha="center", va="center",
                       transform=ax_csv_hr.transAxes, fontsize=9, color="#999")

    ax_csv_hr.set_title("BPM brut CSV (colonne hr)",
                        fontsize=8, color=BLUE_HDR, fontweight="bold", pad=3)
    ax_csv_hr.set_ylabel("BPM", fontsize=7, color="#555")
    ax_csv_hr.set_xlabel("Temps (s)", fontsize=7, color="#555")
    ax_csv_hr.set_xlim(t_start, t_end)
    ax_csv_hr.tick_params(axis="y", labelsize=6)
    ax_csv_hr.tick_params(axis="x", labelsize=6, rotation=45)
    ax_csv_hr.xaxis.set_major_formatter(plt.FormatStrFormatter("%.2f"))
    for spine in ax_csv_hr.spines.values():
        spine.set_edgecolor("#CCC")

    # ── axes[3] : RR brut CSV converti en BPM  (colonne rr en ms) ────────
    ax_csv_rr = axes[3]
    ax_csv_rr.set_facecolor("#F0FFF5")
    ax_csv_rr.grid(axis="y", color="#C0E8CC", linewidth=0.5, zorder=0)
    ax_csv_rr.grid(axis="x", color="#D5EFD8", linewidth=0.3, zorder=0)

    rr_df  = df[df["rr"].notna()][["t_s", "rr"]]
    rr_win = rr_df[(rr_df["t_s"] >= t_start) & (rr_df["t_s"] <= t_end)].copy()

    if len(rr_win) >= 2:
        rt = rr_win["t_s"].values
        # Conversion RR (ms) → BPM
        rr_ms_vals = rr_win["rr"].values.astype(float)
        rv = np.where(rr_ms_vals > 0, 60000.0 / rr_ms_vals, np.nan)
        # Garder seulement les valeurs réalistes
        valid = ~np.isnan(rv) & (rv > 20) & (rv < 300)
        rt = rt[valid]; rv = rv[valid]

        if len(rt) >= 2:
            for i in range(len(rt) - 1):
                c = "#C0392B" if (bpm_threshold and max(rv[i], rv[i+1]) > bpm_threshold)                     else "#27AE60"
                ax_csv_rr.plot(rt[i:i+2], rv[i:i+2], color=c, linewidth=1.6, zorder=3)
            ax_csv_rr.scatter(rt, rv, s=22, zorder=4,
                              c=["#C0392B" if (bpm_threshold and v > bpm_threshold)
                                 else TEAL_ACC for v in rv],
                              edgecolors="white", linewidths=0.4)
            ax_csv_rr.fill_between(rt, rv, alpha=0.10, color="#27AE60", zorder=1)
            for xt, xv in zip(rt, rv):
                ax_csv_rr.annotate(f"{xv:.0f}", xy=(xt, xv),
                                   xytext=(0, 5), textcoords="offset points",
                                   ha="center", fontsize=5.5, fontweight="bold",
                                   color="#C0392B" if (bpm_threshold and xv > bpm_threshold)
                                         else "#145A32")
            if bpm_threshold is not None:
                ax_csv_rr.axhline(bpm_threshold, color="#C0392B",
                                  linewidth=0.9, linestyle="--", zorder=4)
            mean_rr_bpm = float(np.mean(rv))
            mean_rr_ms  = float(np.mean(60000.0 / rv))
            ax_csv_rr.axhline(mean_rr_bpm, color="#27AE60", linewidth=0.9,
                              linestyle=":", zorder=4)
            if mx_t is not None:
                ax_csv_rr.axvline(mx_t, color="#E67E22", linewidth=1.0,
                                  linestyle="--", zorder=4)
            ax_csv_rr.set_ylim(max(20, rv.min() - 10), rv.max() + 20)
            ax_csv_rr.yaxis.set_major_locator(plt.MultipleLocator(10))
            ax_csv_rr.text(0.5, -0.38,
                           f"Moy : {mean_rr_bpm:.1f} bpm  |  "
                           f"Moy RR : {mean_rr_ms:.0f} ms  |  "
                           f"Min : {rv.min():.0f} bpm  |  "
                           f"Max : {rv.max():.0f} bpm  |  N : {len(rv)}",
                           transform=ax_csv_rr.transAxes,
                           ha="center", va="top", fontsize=7, color="#444",
                           bbox=dict(boxstyle="round,pad=0.3", facecolor="#E8F8EC",
                                     edgecolor="#80C890", linewidth=0.8))
        else:
            ax_csv_rr.text(0.5, 0.5, "RR invalides dans cette tranche",
                           ha="center", va="center",
                           transform=ax_csv_rr.transAxes, fontsize=9, color="#999")
    else:
        ax_csv_rr.text(0.5, 0.5, "Pas de données RR dans cette tranche",
                       ha="center", va="center",
                       transform=ax_csv_rr.transAxes, fontsize=9, color="#999")

    ax_csv_rr.set_title("BPM depuis RR brut CSV (colonne rr, converti : 60000 / rr_ms)",
                        fontsize=8, color=BLUE_HDR, fontweight="bold", pad=3)
    ax_csv_rr.set_ylabel("BPM", fontsize=7, color="#555")
    ax_csv_rr.set_xlabel("Temps (s)", fontsize=7, color="#555")
    ax_csv_rr.set_xlim(t_start, t_end)
    ax_csv_rr.tick_params(axis="y", labelsize=6)
    ax_csv_rr.tick_params(axis="x", labelsize=6, rotation=45)
    ax_csv_rr.xaxis.set_major_formatter(plt.FormatStrFormatter("%.2f"))
    for spine in ax_csv_rr.spines.values():
        spine.set_edgecolor("#CCC")

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


def make_ecg_figures_by_minute(df, ecg_filtered, peaks, stats,
                               window_s=10.0, bpm_threshold=190.0):
    t_min = float(df["t_s"].iloc[0])
    t_max = float(df["t_s"].iloc[-1])
    bufs, idx, t0 = [], 1, t_min
    while t0 < t_max:
        t1  = min(t0 + window_s, t_max)
        buf = make_ecg_figure_slice(df, ecg_filtered, peaks, stats,
                                    t_start=t0, t_end=t1, minute_idx=idx,
                                    bpm_threshold=bpm_threshold)
        bufs.append((buf, idx))
        t0 += window_s; idx += 1
    return bufs


def make_rr_histogram(rr_ms: np.ndarray) -> BytesIO | None:
    if len(rr_ms) < 3:
        return None
    fig, ax = plt.subplots(figsize=(5.5, 2.8))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("#FAFAFA")
    ax.hist(rr_ms, bins=min(15, len(rr_ms)),
            color=TEAL_ACC, edgecolor="white", linewidth=0.5, alpha=0.85)
    ax.axvline(np.mean(rr_ms), color=RED_ECG, linewidth=1.2,
               linestyle="--", label=f"Moyenne: {np.mean(rr_ms):.0f} ms")
    ax.legend(fontsize=7)
    ax.set_xlabel("Intervalle RR (ms)", fontsize=8)
    ax.set_ylabel("Fréquence", fontsize=8)
    ax.set_title("Distribution des intervalles RR", fontsize=9,
                 color=BLUE_HDR, fontweight="bold")
    ax.tick_params(labelsize=7)
    ax.grid(axis="y", color="#DDD", linewidth=0.5)
    for spine in ax.spines.values():
        spine.set_edgecolor("#CCC")
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


# ─── Chart BPM global (pleine largeur, toute la durée) ───────────────────────

def make_hr_overview_chart(df: pd.DataFrame, stats: dict,
                           bpm_threshold: float | None = None) -> BytesIO | None:
    """Graphique BPM sur l'ensemble de l'enregistrement — pleine largeur."""
    hr_df = df[df["hr"].notna()][["t_s", "hr"]].copy()
    if len(hr_df) < 2:
        return None

    t_vals  = hr_df["t_s"].values
    hr_vals = hr_df["hr"].values

    fig, ax = plt.subplots(figsize=(14, 3.5))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("#F5FAFF")

    # ── Fond coloré au-dessus du seuil ───────────────────────────────────
    if bpm_threshold is not None:
        y_top = max(hr_vals.max() + 8, bpm_threshold + 15)
        ax.axhspan(bpm_threshold, y_top, color="#C0392B", alpha=0.07, zorder=1)
        ax.axhline(bpm_threshold, color="#C0392B", linewidth=1.0,
                   linestyle="--", zorder=3,
                   label=f"Seuil : {bpm_threshold:.0f} bpm")

    # ── Courbe BPM segmentée (rouge au-dessus du seuil, bleu sinon) ──────
    if bpm_threshold is not None:
        for i in range(len(t_vals) - 1):
            c = "#C0392B" if max(hr_vals[i], hr_vals[i+1]) > bpm_threshold \
                else "#2980B9"
            ax.plot(t_vals[i:i+2], hr_vals[i:i+2], color=c, linewidth=1.4)
        above  = hr_vals > bpm_threshold
        ax.scatter(t_vals[above],  hr_vals[above],
                   color="#C0392B", s=12, zorder=4)
        ax.scatter(t_vals[~above], hr_vals[~above],
                   color=TEAL_ACC, s=8, zorder=4)
    else:
        ax.plot(t_vals, hr_vals, color="#2980B9", linewidth=1.4, zorder=2)
        ax.scatter(t_vals, hr_vals, color=TEAL_ACC, s=8, zorder=3)

    # ── Marqueur FC max ───────────────────────────────────────────────────
    hr_max_t = stats.get("max_hr_t_s")
    hr_max_v = stats.get("max_hr")
    if hr_max_t is not None and hr_max_v is not None:
        ax.axvline(hr_max_t, color="#E67E22", linewidth=1.2,
                   linestyle="--", zorder=5,
                   label=f"FC max : {hr_max_v:.0f} bpm")
        ax.annotate(
            f"★ {hr_max_v:.0f} bpm",
            xy=(hr_max_t, hr_max_v),
            xytext=(hr_max_t + (t_vals[-1] - t_vals[0]) * 0.01,
                    hr_max_v + 2),
            fontsize=8, color="#E67E22", fontweight="bold",
            arrowprops=dict(arrowstyle="->", color="#E67E22", lw=0.8),
        )

    # ── Zones colorées de fond par tranche de 60 s ───────────────────────
    total = t_vals[-1]
    for i in range(int(total // 60) + 1):
        if i % 2 == 0:
            ax.axvspan(i * 60, min((i + 1) * 60, total),
                       color="#E8F4FD", alpha=0.35, zorder=0)

    # ── Axe X : étiquettes en mm:ss ──────────────────────────────────────
    max_ticks = 20
    step = max(1, int(total // max_ticks))
    tick_positions = np.arange(0, total + step, step * 60
                                if total > 120 else step)
    # Simplifier : une étiquette toutes les ~30 s ou 60 s selon la durée
    if total <= 120:
        tick_step = 15
    elif total <= 300:
        tick_step = 30
    elif total <= 600:
        tick_step = 60
    else:
        tick_step = 120
    ticks = np.arange(0, total + tick_step, tick_step)
    ax.set_xticks(ticks)
    ax.set_xticklabels(
        [f"{int(v//60):02d}:{int(v%60):02d}" for v in ticks],
        fontsize=7, rotation=45)

    ax.set_xlim(0, total)
    ax.set_ylim(max(0, hr_vals.min() - 8), hr_vals.max() + 12)
    ax.set_xlabel("Temps (mm:ss)", fontsize=8, color="#555")
    ax.set_ylabel("FC (bpm)", fontsize=8, color="#555")
    ax.set_title("Vue d'ensemble — Fréquence cardiaque sur l'enregistrement complet",
                 fontsize=10, color=BLUE_HDR, fontweight="bold", pad=5)
    ax.grid(axis="y", color="#D0E8F8", linewidth=0.5, zorder=0)
    ax.tick_params(axis="y", labelsize=7)
    ax.legend(fontsize=8, loc="upper right")
    for spine in ax.spines.values():
        spine.set_edgecolor("#CCC")

    # ── Statistiques annotées à droite ───────────────────────────────────
    mean_v = stats.get("mean_hr")
    min_v  = stats.get("min_hr")
    if mean_v:
        ax.axhline(mean_v, color="#27AE60", linewidth=0.8,
                   linestyle=":", zorder=3,
                   label=f"FC moy : {mean_v:.0f} bpm")
        ax.legend(fontsize=8, loc="upper right")

    fig.tight_layout()
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=160, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


# ─── PDF assembly ─────────────────────────────────────────────────────────────

def build_pdf(csv_path: str, output_path: str,
              bpm_threshold: float | None = 190.0,
              window_s: float = 10.0):
    print(f"[1/5] Chargement du fichier : {csv_path}")
    df  = load_csv(csv_path)
    t   = df["t_s"].values
    raw = df["ecg"].values

    dt = np.diff(t)
    fs = 1.0 / np.median(dt)
    print(f"      → {len(df)} échantillons, fs ≈ {fs:.1f} Hz, durée = {t[-1]:.1f} s")

    print("[2/5] Filtrage et détection des pics R …")
    ecg_f    = bandpass_filter(raw, fs)
    peaks, _ = detect_r_peaks(ecg_f, fs)
    stats    = compute_stats(df, peaks, fs)
    print(f"      → {len(peaks)} pics R détectés, FC moy = {stats['mean_hr']} bpm")
    if stats["max_hr"] is not None:
        t_rel = stats["max_hr_t_s"]
        t_abs = stats["max_hr_time_abs"] or "N/A"
        print(f"      → FC max = {stats['max_hr']} bpm "
              f"à t={t_rel:.1f} s ({t_abs})")

    print(f"[3/5] Génération des figures (1 toutes les {window_s:.0f} s) …")
    minute_figs = make_ecg_figures_by_minute(df, ecg_f, peaks, stats,
                                             window_s=window_s,
                                             bpm_threshold=bpm_threshold)
    print(f"      → {len(minute_figs)} tranche(s) générée(s)")

    rr_ms  = np.diff(peaks) / fs * 1000 if len(peaks) > 1 else np.array([])
    rr_buf = make_rr_histogram(rr_ms)
    hr_overview_buf = make_hr_overview_chart(df, stats, bpm_threshold)

    print("[4/5] Construction du PDF …")
    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=18*mm,  bottomMargin=18*mm,
    )
    styles = getSampleStyleSheet()
    W = A4[0] - 30*mm

    title_style = ParagraphStyle(
        "ECGTitle", parent=styles["Title"],
        fontSize=20, textColor=colors.HexColor(BLUE_HDR),
        spaceAfter=4, alignment=TA_CENTER)
    subtitle_style = ParagraphStyle(
        "ECGSubtitle", parent=styles["Normal"],
        fontSize=9, textColor=colors.HexColor("#555555"),
        alignment=TA_CENTER, spaceAfter=2)
    section_style = ParagraphStyle(
        "Section", parent=styles["Heading2"],
        fontSize=11, textColor=colors.HexColor(BLUE_HDR),
        spaceBefore=10, spaceAfter=4, borderPad=3)
    note_style = ParagraphStyle(
        "Note", parent=styles["Normal"],
        fontSize=7.5, textColor=colors.HexColor("#777777"),
        leftIndent=5, spaceAfter=4)
    caption_style = ParagraphStyle(
        "Caption", parent=styles["Normal"],
        fontSize=7.5, textColor=colors.HexColor("#555555"),
        alignment=TA_CENTER, spaceAfter=4)
    alert_style = ParagraphStyle(
        "Alert", parent=styles["Normal"],
        fontSize=9, textColor=colors.HexColor("#E67E22"),
        leftIndent=5, spaceAfter=4, fontName="Helvetica-Bold")

    story = []

    # ── En-tête ──────────────────────────────────────────────────────────
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    story.append(Paragraph("Rapport ECG", title_style))
    story.append(Paragraph(
        f"Généré le {now} &nbsp;|&nbsp; "
        f"Fichier : <b>{os.path.basename(csv_path)}</b>",
        subtitle_style))
    story.append(HRFlowable(width="100%", thickness=2,
                             color=colors.HexColor(BLUE_HDR), spaceAfter=8))

    # ── Tableau de résumé ─────────────────────────────────────────────────
    story.append(Paragraph("Résumé de l'enregistrement", section_style))

    def val(v, unit="", na="N/A"):
        return f"{v} {unit}".strip() if v is not None else na

    # Construire la cellule FC max avec l'heure
    hr_max_cell = val(stats["max_hr"], "bpm")
    if stats["max_hr_t_s"] is not None:
        mm_ss = f"{int(stats['max_hr_t_s']//60):02d}:{int(stats['max_hr_t_s']%60):02d}"
        hr_max_cell += f"\nà {mm_ss} (mm:ss)"
        if stats["max_hr_time_abs"]:
            hr_max_cell += f" — {stats['max_hr_time_abs']}"

    tbl_data = [
        ["Paramètre", "Valeur", "Paramètre", "Valeur"],
        ["Durée", val(stats["duration_s"], "s"),
         "Fréquence d'échantillonnage", val(stats["fs_hz"], "Hz")],
        ["Battements détectés (R)", val(stats["n_beats"]),
         "FC moyenne", val(stats["mean_hr"], "bpm")],
        ["FC min", val(stats["min_hr"], "bpm"),
         "FC max ★", hr_max_cell],           # ← FC max avec heure
        ["Amplitude min ECG", val(stats["ecg_min_mv"], "mV"),
         "Amplitude max ECG", val(stats["ecg_max_mv"], "mV")],
        ["Intervalle RR moyen", val(stats["rr_mean_ms"], "ms"),
         "SDNN", val(stats["rr_std_ms"], "ms")],
        ["RMSSD", val(stats["rmssd_ms"], "ms"),
         "Nb mesures HR (CSV)", str(df["hr"].notna().sum())],
    ]

    col_w = [W * 0.30, W * 0.20, W * 0.30, W * 0.20]
    tbl = Table(tbl_data, colWidths=col_w, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), colors.HexColor(BLUE_HDR)),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0), 8.5),
        ("ALIGN",         (0, 0), (-1, 0), "CENTER"),
        ("BACKGROUND",    (0, 1), (0, -1), colors.HexColor("#EBF2FA")),
        ("BACKGROUND",    (2, 1), (2, -1), colors.HexColor("#EBF2FA")),
        ("FONTNAME",      (0, 1), (0, -1), "Helvetica-Bold"),
        ("FONTNAME",      (2, 1), (2, -1), "Helvetica-Bold"),
        # Surligner la ligne FC max en orange clair
        ("BACKGROUND",    (2, 3), (3, 3), colors.HexColor("#FEF3E2")),
        ("TEXTCOLOR",     (3, 3), (3, 3), colors.HexColor("#E67E22")),
        ("FONTNAME",      (3, 3), (3, 3), "Helvetica-Bold"),
        *[("BACKGROUND", (1, i), (1, i), colors.HexColor("#F7FBFF"))
          for i in range(2, len(tbl_data), 2)],
        *[("BACKGROUND", (3, i), (3, i), colors.HexColor("#F7FBFF"))
          for i in range(2, len(tbl_data), 2)],
        ("FONTSIZE",      (0, 1), (-1, -1), 8),
        ("GRID",          (0, 0), (-1, -1), 0.4, colors.HexColor("#BBCFE8")),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 7),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 4))

    # ── Encadré FC max (résumé visuel) ────────────────────────────────────
    if stats["max_hr"] is not None:
        mm_ss = (f"{int(stats['max_hr_t_s']//60):02d}:"
                 f"{int(stats['max_hr_t_s']%60):02d}")
        abs_str = (f" ({stats['max_hr_time_abs']})"
                   if stats["max_hr_time_abs"] else "")
        story.append(Paragraph(
            f"★ FC maximale atteinte : <b>{stats['max_hr']:.0f} bpm</b> "
            f"à <b>{mm_ss} min:ss</b>{abs_str} depuis le début de l'enregistrement "
            f"— voir la tranche marquée ↓",
            alert_style))

    # ── Chart BPM global — juste après le tableau résumé ───────────────
    if hr_overview_buf:
        story.append(Paragraph(
            "Vue d'ensemble de la fréquence cardiaque", section_style))
        story.append(Paragraph(
            "Évolution du BPM sur la totalité de l'enregistrement. "
            "<font color='#E67E22'><b>Orange = FC max</b></font>  |  "
            "<font color='#27AE60'><b>Vert pointillé = FC moyenne</b></font>"
            + (f"  |  <font color='#C0392B'><b>Rouge = seuil {bpm_threshold:.0f} bpm</b></font>"
               if bpm_threshold else ""),
            caption_style))
        hr_img = RLImage(hr_overview_buf, width=W, height=W * 3.5 / 14)
        story.append(hr_img)
        story.append(Spacer(1, 6))

    story.append(Spacer(1, 8))

    # ── Tracés ECG par tranche ────────────────────────────────────────────
    story.append(Paragraph(
        f"Tracé ECG — tranches de {window_s:.0f} secondes", section_style))
    caption_txt = ("De haut en bas : signal brut · fréquence cardiaque  |  "
                   "<font color='#E67E22'><b>ligne orange = FC max</b></font>")
    if bpm_threshold is not None:
        caption_txt += (f"  |  <font color='#C0392B'><b>Seuil FC : "
                        f"{bpm_threshold:.0f} bpm</b></font> (ligne rouge pointillée)")
    story.append(Paragraph(caption_txt, caption_style))

    # 2 tranches par page : chaque paire est mise dans un tableau
    # côte à côte non (trop étroit) mais empilées, séparées par PageBreak
    # après chaque groupe de 2.
    H_slice = W * 13 / 16   # hauteur d'une tranche (5 graphes)
    for i, (ecg_buf, min_idx) in enumerate(minute_figs):
        ecg_img = RLImage(ecg_buf, width=W, height=H_slice)
        story.append(ecg_img)
        # Séparateur entre les 2 tranches d'une même page
        if i % 2 == 0 and i + 1 < len(minute_figs):
            story.append(HRFlowable(width="100%", thickness=0.5,
                                    color=colors.HexColor("#BBCFE8"),
                                    spaceAfter=4))
        # Saut de page après chaque groupe de 2 (sauf la dernière)
        elif i % 2 == 1 and i + 1 < len(minute_figs):
            story.append(PageBreak())

    # ── Histogramme RR ────────────────────────────────────────────────────
    if rr_buf:
        story.append(Paragraph(
            "Variabilité de la fréquence cardiaque (VFC)", section_style))
        rr_img = RLImage(rr_buf, width=W * 0.6, height=W * 0.6 * 2.8 / 5.5)
        tbl2 = Table([[rr_img]], colWidths=[W])
        tbl2.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER")]))
        story.append(tbl2)
        if stats["rr_std_ms"]:
            story.append(Paragraph(
                f"SDNN = {stats['rr_std_ms']} ms  |  "
                f"RMSSD = {val(stats['rmssd_ms'], 'ms')}",
                caption_style))

    # ── Disclaimer ───────────────────────────────────────────────────────
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=0.8,
                             color=colors.HexColor("#BBBBBB"), spaceAfter=6))
    story.append(Paragraph(
        "<b>Avertissement :</b> Ce rapport est généré automatiquement à des fins "
        "d'analyse technique. Il ne constitue pas un diagnostic médical. "
        "Toute interprétation clinique doit être effectuée par un professionnel de santé qualifié.",
        note_style))

    print("[5/5] Écriture du PDF …")
    doc.build(story)
    print(f"\n✅ PDF généré avec succès : {output_path}")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Génère un rapport PDF médical à partir d'un fichier CSV ECG."
    )
    parser.add_argument("csv", help="Chemin vers le fichier CSV ECG")
    parser.add_argument("output", nargs="?", default=None,
                        help="Chemin de sortie PDF")
    parser.add_argument("--fenetre", type=float, default=10.0, metavar="SEC",
                        help="Durée de chaque tranche en secondes (défaut : 10)")
    parser.add_argument("--seuil-bpm", type=float, default=190.0, metavar="BPM",
                        help="Seuil FC (ex: --seuil-bpm 100)")
    args = parser.parse_args()

    if not os.path.isfile(args.csv):
        print(f"Erreur : fichier introuvable : {args.csv}", file=sys.stderr)
        sys.exit(1)

    if args.seuil_bpm is not None:
        print(f"      → Seuil FC activé : {args.seuil_bpm} bpm")
    print(f"      → Fenêtre : {args.fenetre} s par tranche")

    out = args.output or os.path.splitext(args.csv)[0] + "_rapport.pdf"
    build_pdf(args.csv, out, bpm_threshold=args.seuil_bpm, window_s=args.fenetre)


if __name__ == "__main__":
    main()
