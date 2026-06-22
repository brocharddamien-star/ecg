"""Traitement du signal ECG — fonctions partagées."""

import numpy as np
from scipy.signal import butter, filtfilt, find_peaks

# Paramètres d'acquisition ECG
ECG_WINDOW_S = 5
ECG_FS       = 130
ECG_SAMPLES  = ECG_WINDOW_S * ECG_FS


def bandpass_filter(signal: np.ndarray, fs: float,
                    low: float = 0.5, high: float = 40.0) -> np.ndarray:
    """Filtre passe-bande Butterworth d'ordre 4 (standard clinique 0.5–40 Hz)."""
    nyq = fs / 2.0
    b, a = butter(4, [low / nyq, high / nyq], btype="band")
    return filtfilt(b, a, signal)


def detect_r_peaks(ecg_filtered: np.ndarray, fs: float,
                   height_thr: float = 0.5,
                   min_distance_s: float = 0.30,
                   prominence: float = 0.05):
    """Détecte les pics R.

    height_thr      : seuil absolu en mV (défaut 0.5 mV — standard ECG papier)
    min_distance_s  : distance minimale entre deux pics en secondes (défaut 300 ms → 200 bpm max)
    prominence      : proéminence minimale en mV
    """
    distance = int(min_distance_s * fs)
    peaks, props = find_peaks(
        ecg_filtered,
        distance=distance,
        height=height_thr,
        prominence=prominence,
    )
    return peaks, props


def adaptive_x_spacing(duration_s: float):
    """Retourne (minor_s, major_s) pour la grille temporelle ECG.

    Respecte le standard médical (petite case = 0.04 s, grande case = 0.20 s)
    et bascule sur des intervalles plus larges pour les longues durées.
    """
    if duration_s / 0.04 <= 500:
        return 0.04, 0.20
    candidates = [
        (0.20, 1.00), (0.40, 2.00), (1.00, 5.00),
        (2.00, 10.0), (5.00, 25.0), (10.0, 50.0),
    ]
    for minor, major in candidates:
        if duration_s / minor <= 500:
            return minor, major
    return 10.0, 50.0


def fmt_mmss(seconds: float) -> str:
    """Convertit des secondes en chaîne 'MM:SS'."""
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"
