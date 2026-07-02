"""
TKE-based EMG onset/offset detection.

Python port of Xiong Zhao's MATLAB emg_onset_detection (2020-08-24 / 2020-09-16).
Algorithm: Teager-Kaiser Energy operator + amplitude-threshold + run-length filter.

Baseline selection cascade (replaces manual ginput):
  1. Pre-crop period  — if crop_start_s > 0.3 s, use tke[0 : crop_start]
  2. Quietest window  — sliding 300 ms window, pick minimum-RMS segment
  3. Fallback         — first baseline_duration_s seconds
"""

import numpy as np


def detect_emg_onsets(
    signal,
    fs,
    threshold_std=3.0,
    window_above_s=0.050,
    window_below_s=0.100,
    baseline_duration_s=0.5,
    crop_start_s=None,
):
    """Detect EMG onset/offset pairs from a linear-envelope signal.

    Parameters
    ----------
    signal : array-like
        Processed EMG — must be the linear envelope (rectified + LP filtered).
    fs : float
        Sampling frequency in Hz.
    threshold_std : float
        Threshold as a multiple of baseline TKE standard deviation (default 3.0).
    window_above_s : float
        Minimum duration above threshold to confirm an onset (default 50 ms).
    window_below_s : float
        Minimum gap below threshold needed to end an active segment (default
        100 ms). Below-threshold dips shorter than this are bridged rather
        than ending the segment — real envelopes ripple during a sustained
        contraction, and 10-50 ms was too tight to survive that (measured on
        a real jump trial: a single ~2s leg-muscle burst fragmented into 22
        onset/offset pairs at 10 ms, still 22 at 50 ms — every real gap was
        55 ms or longer — and collapsed to the expected handful at 100 ms).
    baseline_duration_s : float
        Duration used for the fallback baseline window (default 0.5 s).
    crop_start_s : float or None
        If provided and > 0.3 s, the pre-crop period is used as baseline first.

    Returns
    -------
    list of (float, float)
        (onset_time_s, offset_time_s) pairs, sorted by onset time.
        Empty list if no activations are detected.
    """
    arr = np.asarray(signal, dtype=float)
    n = len(arr)
    if n < 3:
        return []

    # --- Teager-Kaiser Energy Operator ---
    # tke[i] = x[i]^2 - x[i-1]*x[i+1]  (Teager, 1990)
    tke = arr[1:-1] ** 2 - arr[:-2] * arr[2:]
    tke = np.concatenate([[tke[0]], tke, [tke[-1]]])  # pad to original length

    window_above = max(1, int(window_above_s * fs))
    window_below = max(1, int(window_below_s * fs))
    _MIN_BASELINE = max(10, int(0.1 * fs))

    # --- Baseline selection ---
    baseline_tke = _select_baseline(tke, fs, crop_start_s, baseline_duration_s, _MIN_BASELINE)
    if baseline_tke is None or len(baseline_tke) < _MIN_BASELINE:
        return []

    baseline_std = float(np.std(baseline_tke))
    if baseline_std <= 0:
        return []

    threshold = threshold_std * baseline_std

    # --- Samples above threshold ---
    above = np.where(tke >= threshold)[0]
    if len(above) == 0:
        return []

    # --- Group into segments, merging gaps < window_below ---
    # Direct port of MATLAB logic:
    #   index_ninf = [-inf; indices_1]
    #   index_inf  = [indices_1; inf]
    #   starts = indices_1(diff(index_ninf) > window_below+1)
    #   ends   = indices_1(diff(index_inf)  > window_below+1)
    diff_ninf = np.diff(np.concatenate([[-np.inf], above.astype(float)]))
    diff_inf  = np.diff(np.concatenate([above.astype(float), [np.inf]]))

    starts = above[diff_ninf > window_below + 1]
    ends   = above[diff_inf  > window_below + 1]

    if len(starts) == 0 or len(ends) == 0 or len(starts) != len(ends):
        return []

    # --- Filter by minimum active duration ---
    pairs = np.column_stack([starts, ends])
    pairs = pairs[(pairs[:, 1] - pairs[:, 0]) >= window_above - 1]

    if len(pairs) == 0:
        return []

    return [(int(row[0]) / fs, int(row[1]) / fs) for row in pairs]


def _select_baseline(tke, fs, crop_start_s, baseline_duration_s, min_samples):
    """Return a baseline TKE array using the three-level cascade."""
    n = len(tke)

    # 1. Pre-crop period: user already defined the active segment via crop,
    #    so everything before crop_start is the resting/quiet phase.
    if crop_start_s is not None and crop_start_s > 0.3:
        end_idx = min(n, int(crop_start_s * fs))
        if end_idx >= min_samples:
            return tke[:end_idx]

    # 2. Quietest 300 ms sliding window across the entire signal.
    win = max(min_samples, int(0.3 * fs))
    if n >= win * 2:
        best_start, best_std = 0, np.inf
        stride = max(1, win // 2)          # half-window stride for speed
        for i in range(0, n - win + 1, stride):
            s = float(np.std(tke[i:i + win]))
            if s < best_std:
                best_std = s
                best_start = i
        return tke[best_start:best_start + win]

    # 3. Fallback: first baseline_duration_s seconds.
    end_idx = min(n, max(min_samples, int(baseline_duration_s * fs)))
    return tke[:end_idx]
