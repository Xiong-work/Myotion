"""
Task-type repetition/cycle detection for the kinematics workflow.

Mirrors onset_detection.py's shape: pure functions over a marker (and
optionally a force-plate) vertical trajectory, returning a list of
(t_start_s, t_end_s) cycle/repetition boundaries. No Qt, no workspace
state -- callers (Controller) turn the returned pairs into TrialEvents.

Three families, covering all 7 task-type dropdown entries (Phase B1 + B2):
  - detect_gait_cycles()          -- gait / running: foot-strike based,
                                      one cycle == consecutive foot contacts.
  - detect_sit_stand_cycles()     -- sit-to-stand / squat / trunk flexion:
                                      vertical-velocity burst detection,
                                      one cycle == one rep.
  - detect_reach_cycles()         -- lifting / pointing: resultant-3D-speed
                                      burst detection, one cycle == one rep.
detect_squat_cycles, detect_trunk_flexion_cycles, detect_lifting_cycles,
and detect_pointing_cycles are thin wrappers (some with retuned defaults)
over the two shared burst detectors, so the task-type dropdown can offer a
distinct entry per task while sharing implementation where the underlying
signal shape is the same. See detect_trunk_flexion_cycles' docstring for a
noted simplification (single-marker vertical burst, not a true 2-marker
trunk angle).
"""

import numpy as np
import scipy.signal as _sig


def _quietest_window_std(arr, win):
    """Std-dev of the flattest `win`-sample window in arr (baseline estimate)."""
    n = len(arr)
    if n < win:
        return float(np.std(arr)) if n > 0 else 0.0
    best_std = np.inf
    stride = max(1, win // 2)
    for i in range(0, n - win + 1, stride):
        s = float(np.std(arr[i:i + win]))
        if s < best_std:
            best_std = s
    return best_std


def _activity_segments(magnitude, fs, threshold, window_above_s, window_below_s):
    """Group samples above `threshold` into segments, bridging short dips.

    Same grouping algorithm as onset_detection.detect_emg_onsets, applied to
    a generic magnitude signal instead of TKE energy.

    Returns list of (start_idx, end_idx) sample-index pairs.
    """
    window_above = max(1, int(window_above_s * fs))
    window_below = max(1, int(window_below_s * fs))

    above = np.where(magnitude >= threshold)[0]
    if len(above) == 0:
        return []

    diff_ninf = np.diff(np.concatenate([[-np.inf], above.astype(float)]))
    diff_inf = np.diff(np.concatenate([above.astype(float), [np.inf]]))

    starts = above[diff_ninf > window_below + 1]
    ends = above[diff_inf > window_below + 1]

    if len(starts) == 0 or len(ends) == 0 or len(starts) != len(ends):
        return []

    pairs = np.column_stack([starts, ends])
    pairs = pairs[(pairs[:, 1] - pairs[:, 0]) >= window_above - 1]
    return [(int(row[0]), int(row[1])) for row in pairs]


def _burst_cycles(magnitude, fs, threshold_std, window_above_s, window_below_s, min_cycle_s):
    """Shared "quiet -> burst -> quiet" repetition detector.

    Used by every detector below that treats one repetition as a single
    continuous burst of some velocity-derived magnitude signal (vertical
    velocity for sit-to-stand/squat/trunk flexion, resultant 3D speed for
    reach-style tasks). Threshold is set relative to the quietest window's
    baseline std, with a small-fraction-of-range fallback when that baseline
    is degenerate (e.g. a perfectly flat quiet phase in heavily-filtered
    data), so a noise-free synthetic/clean trial still yields detections.

    Returns list of (t_start_s, t_end_s), sorted by start time.
    """
    if len(magnitude) < 3:
        return []

    win = max(10, int(0.3 * fs))
    baseline_std = _quietest_window_std(magnitude, win)
    if baseline_std <= 0:
        rng = float(np.max(magnitude) - np.min(magnitude))
        if rng <= 0:
            return []
        baseline_std = 0.01 * rng

    threshold = threshold_std * baseline_std
    segments = _activity_segments(magnitude, fs, threshold, window_above_s, window_below_s)
    if not segments:
        return []

    min_samples = min_cycle_s * fs
    return [
        (start / fs, end / fs)
        for start, end in segments
        if (end - start) >= min_samples
    ]


def detect_gait_cycles(marker_z, fs, fp_vertical=None, fp_fs=None, mode="walk"):
    """Detect gait/running cycles from a foot marker's vertical trajectory.

    One cycle spans consecutive foot-strike events (a full stride).

    Parameters
    ----------
    marker_z : array-like
        Vertical (C3D Z-axis) trajectory of a foot/heel marker.
    fs : float
        Marker sampling frequency in Hz.
    fp_vertical : array-like or None
        Vertical GRF (Fz) from a force plate under the same trial, when
        available -- foot-strike detection from GRF is far more robust than
        from marker position alone. None falls back to marker-only detection.
    fp_fs : float or None
        Force-plate sampling frequency in Hz. Required when fp_vertical is
        given (may differ from the marker fs).
    mode : "walk" or "run"
        Sets the minimum inter-strike interval used to reject spurious
        detections (running cadence is faster than walking).

    Returns
    -------
    list of (float, float)
        (t_start_s, t_end_s) cycle boundaries, sorted by start time.
        Empty list if fewer than 2 foot-strikes are found.
    """
    min_cycle_s = 0.4 if mode == "run" else 0.8

    if fp_vertical is not None and fp_fs:
        arr = np.asarray(fp_vertical, dtype=float)
        if len(arr) < 3:
            return []
        threshold = 0.05 * float(np.max(arr))
        if threshold <= 0:
            return []
        distance = max(1, int(min_cycle_s * fp_fs))
        above = arr >= threshold
        # Rising edges: sample i where below -> at/above threshold.
        rising = np.where(above[1:] & ~above[:-1])[0] + 1
        if len(rising) == 0:
            return []
        strikes = [rising[0]]
        for idx in rising[1:]:
            if idx - strikes[-1] >= distance:
                strikes.append(idx)
        strike_times = [i / fp_fs for i in strikes]
    else:
        arr = np.asarray(marker_z, dtype=float)
        if len(arr) < 3:
            return []
        distance = max(1, int(min_cycle_s * fs))
        # Foot-strike ~ local minimum of vertical marker position (foot at
        # its lowest, near ground contact).
        peaks, _ = _sig.find_peaks(-arr, distance=distance)
        if len(peaks) == 0:
            return []
        strike_times = [i / fs for i in peaks]

    if len(strike_times) < 2:
        return []
    return [(strike_times[i], strike_times[i + 1]) for i in range(len(strike_times) - 1)]


def detect_sit_stand_cycles(
    marker_z,
    fs,
    threshold_std=2.5,
    window_above_s=0.15,
    window_below_s=0.3,
    min_cycle_s=0.5,
):
    """Detect sit-to-stand / squat repetitions from a pelvis/hip marker.

    Each repetition is treated as one continuous vertical-velocity burst
    (rise + fall), bounded by quiescent periods -- so this covers both a
    sit-to-stand rep (stand up, briefly pause, sit down) and a squat rep
    (down-up with no pause) with the same detector.

    Parameters
    ----------
    marker_z : array-like
        Vertical (C3D Z-axis) trajectory of a pelvis/hip marker.
    fs : float
        Marker sampling frequency in Hz.
    threshold_std : float
        Threshold as a multiple of baseline vertical-velocity std (default 2.5).
    window_above_s : float
        Minimum duration above threshold to confirm a rep is starting
        (default 150 ms).
    window_below_s : float
        Minimum gap below threshold needed to end a rep; shorter dips are
        bridged (default 300 ms) -- tolerates the brief pause at the top/
        bottom of a rep without splitting it into two.
    min_cycle_s : float
        Reps shorter than this are discarded as noise (default 0.5 s).

    Returns
    -------
    list of (float, float)
        (t_start_s, t_end_s) rep boundaries, sorted by start time.
        Empty list if no reps are detected.
    """
    arr = np.asarray(marker_z, dtype=float)
    if len(arr) < 3:
        return []

    velocity = np.gradient(arr) * fs
    magnitude = np.abs(velocity)
    return _burst_cycles(magnitude, fs, threshold_std, window_above_s, window_below_s, min_cycle_s)


def detect_squat_cycles(marker_z, fs, **kwargs):
    """Detect squat repetitions. Alias of detect_sit_stand_cycles (same
    vertical-velocity-burst signal shape); kept as a separate name so the
    task-type dropdown can offer a distinct "Squat" entry."""
    return detect_sit_stand_cycles(marker_z, fs, **kwargs)


def detect_trunk_flexion_cycles(
    marker_z,
    fs,
    threshold_std=2.5,
    window_above_s=0.15,
    window_below_s=0.4,
    min_cycle_s=0.6,
):
    """Detect trunk flexion/extension repetitions from a single trunk marker.

    Simplification note: a true trunk angle needs two markers (e.g. a torso
    marker relative to the pelvis); the kinematics workflow's "Kinematics
    source" picker currently selects one marker at a time. This detector
    instead treats trunk flexion the same way as sit-to-stand/squat -- a
    single trunk marker (e.g. C7 or sternum) moves down and back up as the
    trunk bends over and returns to upright, which is the same vertical-
    velocity-burst signal shape. Defaults use a longer window_below_s/
    min_cycle_s than sit-to-stand since trunk flexion is typically slower
    and often pauses briefly at end-range.

    Parameters mirror detect_sit_stand_cycles; see there for details.

    Returns
    -------
    list of (float, float)
        (t_start_s, t_end_s) rep boundaries, sorted by start time.
    """
    return detect_sit_stand_cycles(
        marker_z, fs,
        threshold_std=threshold_std,
        window_above_s=window_above_s,
        window_below_s=window_below_s,
        min_cycle_s=min_cycle_s,
    )


def detect_reach_cycles(
    marker_xyz,
    fs,
    threshold_std=2.5,
    window_above_s=0.1,
    window_below_s=0.3,
    min_cycle_s=0.3,
):
    """Detect reach-style repetitions (lifting, pointing) from a hand/wrist
    marker's full 3D trajectory.

    Reach motions are not purely vertical, so this uses resultant 3D speed
    (magnitude of the 3-axis velocity vector) rather than a single-axis
    component -- otherwise identical burst-detection approach to
    detect_sit_stand_cycles: one repetition is one continuous speed burst
    (reach out, briefly pause/interact, return), bounded by quiescent
    (hand-at-rest) periods.

    Parameters
    ----------
    marker_xyz : array-like, shape (n, 3)
        Per-frame (x, y, z) position of a hand/wrist marker.
    fs : float
        Marker sampling frequency in Hz.
    threshold_std, window_above_s, window_below_s, min_cycle_s :
        Same meaning as detect_sit_stand_cycles.

    Returns
    -------
    list of (float, float)
        (t_start_s, t_end_s) rep boundaries, sorted by start time.
    """
    arr = np.asarray(marker_xyz, dtype=float)
    if arr.ndim != 2 or arr.shape[0] < 3 or arr.shape[1] < 3:
        return []

    velocity = np.gradient(arr, axis=0) * fs
    magnitude = np.linalg.norm(velocity, axis=1)
    return _burst_cycles(magnitude, fs, threshold_std, window_above_s, window_below_s, min_cycle_s)


def detect_lifting_cycles(marker_xyz, fs, **kwargs):
    """Detect lifting repetitions. Uses detect_reach_cycles with defaults
    tuned for a slower motion that often pauses briefly at the top."""
    kwargs.setdefault("min_cycle_s", 0.6)
    kwargs.setdefault("window_below_s", 0.4)
    return detect_reach_cycles(marker_xyz, fs, **kwargs)


def detect_pointing_cycles(marker_xyz, fs, **kwargs):
    """Detect pointing repetitions. Uses detect_reach_cycles with defaults
    tuned for a faster, more ballistic motion than lifting."""
    kwargs.setdefault("min_cycle_s", 0.25)
    kwargs.setdefault("window_below_s", 0.2)
    return detect_reach_cycles(marker_xyz, fs, **kwargs)
