"""Load EMG data (from a C3D, MAT, or CSV file) and check per-channel signal
quality via a simple RMS-ratio SNR estimate.

This is a standalone diagnostic: it loads whatever file the user picks into a
plain timeSeriesTable and reports a number, independent of the main app's EMG
processing pipeline (crop/normalization/frequency-analysis signal paths are
untouched by this).
"""

import os

import numpy as np
import pandas as pd

from modules.pyMotion.core.c3d import c3dFile
from modules.pyMotion.core.mat import matFile
from modules.pyMotion.core.timeSeriesTable import timeSeriesTable

DEFAULT_WINDOW_S = 0.5


class EmgSnrError(Exception):
    pass


def load_signal(path, fs=None):
    """Load path (.c3d / .mat / .csv) into a timeSeriesTable of raw channels.

    fs is only used for .csv files that have no inferable time column.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".c3d":
        return _load_c3d(path)
    elif ext == ".mat":
        return _load_mat(path)
    elif ext == ".csv":
        return _load_csv(path, fs=fs)
    else:
        raise EmgSnrError(f"Unsupported file type '{ext}'. Expected .c3d, .mat, or .csv.")


def _load_c3d(path):
    try:
        c3d_obj = c3dFile(path)
    except Exception as e:
        # c3dFile assumes at least one ANALOG channel exists; a marker-only
        # C3D (no analog data at all) fails inside its reader rather than
        # degrading gracefully -- turn that into a clear message here instead
        # of leaking the underlying AttributeError.
        raise EmgSnrError(f"C3D file has no analog (EMG) channels: {path}") from e
    ad = c3d_obj.analogdata
    if ad.channels() == 0:
        raise EmgSnrError(f"C3D file has no analog (EMG) channels: {path}")
    data = [np.asarray(ad[label], dtype=float) for label in ad.labels]
    return timeSeriesTable(ad.fs, list(ad.labels), data)


def _load_mat(path):
    mat_obj = matFile(path)
    tst = mat_obj.convertToTST()
    if tst is None:
        raise EmgSnrError(f"MAT file has no channels: {path}")
    return tst


def _load_csv(path, fs=None):
    df = pd.read_csv(path)
    if df.shape[1] < 2:
        raise EmgSnrError("CSV must have a time column (or known sample rate) plus at least one signal column.")

    time_col = next((c for c in df.columns if str(c).strip().lower() in ("time", "time (s)", "t")), None)
    if time_col is not None:
        t = df[time_col].to_numpy(dtype=float)
        if len(t) < 2:
            raise EmgSnrError("CSV time column has too few samples to infer a sample rate.")
        fs = 1.0 / np.median(np.diff(t))
        labels = [c for c in df.columns if c != time_col]
    elif fs is not None and fs > 0:
        labels = list(df.columns)
    else:
        raise EmgSnrError(
            "CSV has no recognizable time column ('time', 't') -- provide the sample rate explicitly."
        )

    data = [df[label].to_numpy(dtype=float) for label in labels]
    return timeSeriesTable(fs, labels, data)


def find_quietest_window(data, fs, window_s=DEFAULT_WINDOW_S):
    """Slide a window_s-second window across data (1D array) and return the
    (start_s, end_s) of the window with the lowest RMS -- a simple, explicit
    stand-in for a manually marked quiet/baseline segment."""
    n = len(data)
    win = max(1, int(round(window_s * fs)))
    if win >= n:
        return 0.0, n / fs

    step = max(1, win // 4)
    best_start, best_rms = 0, np.inf
    for start in range(0, n - win + 1, step):
        seg = data[start:start + win]
        rms = np.sqrt(np.mean(seg ** 2))
        if rms < best_rms:
            best_rms = rms
            best_start = start
    return best_start / fs, (best_start + win) / fs


def find_loudest_window(data, fs, window_s=DEFAULT_WINDOW_S):
    """Same sliding-window search as find_quietest_window, but for the
    highest-RMS window -- used as the default 'active burst' segment."""
    n = len(data)
    win = max(1, int(round(window_s * fs)))
    if win >= n:
        return 0.0, n / fs

    step = max(1, win // 4)
    best_start, best_rms = 0, -np.inf
    for start in range(0, n - win + 1, step):
        seg = data[start:start + win]
        rms = np.sqrt(np.mean(seg ** 2))
        if rms > best_rms:
            best_rms = rms
            best_start = start
    return best_start / fs, (best_start + win) / fs


def compute_snr(tst, channel, baseline_window=None, active_window=None, window_s=DEFAULT_WINDOW_S):
    """Compute a simple amplitude-ratio SNR (dB) for one channel:

        SNR_dB = 20 * log10(RMS_active / RMS_baseline)

    baseline_window / active_window are (start_s, end_s) tuples; if omitted,
    the quietest / loudest window_s-second window in the trial is used.
    Returns a dict with the SNR plus the windows actually used (so the UI can
    show/adjust them).
    """
    data = np.asarray(tst[channel], dtype=float)
    fs = tst.fs

    if baseline_window is None:
        baseline_window = find_quietest_window(data, fs, window_s)
    if active_window is None:
        active_window = find_loudest_window(data, fs, window_s)

    def _rms_of(window):
        start_i = max(0, int(round(window[0] * fs)))
        end_i = min(len(data), int(round(window[1] * fs)))
        if end_i <= start_i:
            raise EmgSnrError(f"Empty window {window} for channel '{channel}'.")
        seg = data[start_i:end_i]
        return float(np.sqrt(np.mean(seg ** 2)))

    rms_baseline = _rms_of(baseline_window)
    rms_active = _rms_of(active_window)

    if rms_baseline <= 0:
        snr_db = float("inf") if rms_active > 0 else 0.0
    else:
        snr_db = 20.0 * np.log10(rms_active / rms_baseline)

    return {
        "channel": channel,
        "snr_db": snr_db,
        "rms_active": rms_active,
        "rms_baseline": rms_baseline,
        "baseline_window": baseline_window,
        "active_window": active_window,
    }


def compute_snr_all_channels(tst, window_s=DEFAULT_WINDOW_S):
    return [compute_snr(tst, ch, window_s=window_s) for ch in tst.labels]
