"""modules/pyMotion/core/td_metrics.py -- shared time-domain summary metrics.

Single source of truth for the 13 TD metrics used by both
workspace.saveReport()'s whole-trial <name>_summary.csv and main.py's
per-cycle <name>_summary_cycles.csv (see applyCyclesTimeDomainSummary) --
both need identical formulas so a cycle-based and whole-trial summary of
the same channel are directly comparable.
"""

import numpy as np
import scipy.stats as _ss

TD_METRIC_KEYS = [
    "min", "max", "mean", "median", "std", "var",
    "ptp", "zc", "auc", "rms", "mav", "skewness", "kurtosis",
]


def compute_td_metrics(arr, fs):
    """13 time-domain summary metrics for one channel's signal segment.

    arr: 1D array-like of samples.
    fs: sample frequency in Hz (used by auc's dx spacing).

    Returns a dict with keys TD_METRIC_KEYS. All-zero/degenerate for an
    empty arr rather than raising.
    """
    arr = np.asarray(arr, dtype=float)
    n = len(arr)
    if n == 0:
        return {k: (0 if k == "zc" else 0.0) for k in TD_METRIC_KEYS}

    zc = int(np.sum(np.diff(np.sign(arr)) != 0))
    return {
        "min":      round(float(arr.min()), 6),
        "max":      round(float(arr.max()), 6),
        "mean":     round(float(arr.mean()), 6),
        "median":   round(float(np.median(arr)), 6),
        "std":      round(float(arr.std(ddof=1)) if n > 1 else 0.0, 6),
        "var":      round(float(arr.var(ddof=1)) if n > 1 else 0.0, 6),
        "ptp":      round(float(np.ptp(arr)), 6),
        "zc":       zc,
        "auc":      round(float(np.trapz(np.abs(arr), dx=1.0 / fs)), 6),
        "rms":      round(float(np.sqrt(np.mean(arr ** 2))), 6),
        "mav":      round(float(np.mean(np.abs(arr))), 6),
        "skewness": round(float(_ss.skew(arr)), 6),
        "kurtosis": round(float(_ss.kurtosis(arr)), 6),
    }
