"""
Gait event detection for the Gait Analysis module.

Returns per-foot heel-strike (HS) / toe-off (TO) times plus basic
spatiotemporal parameters (step/stride length, time, cadence, velocity).
Kept separate from cycle_detection.py -- that module returns generic
(t_start, t_end) cycle pairs for the Kinematics Inspection "task type"
picker; this one returns a per-foot HS/TO + spatiotemporal shape that only
the Gait Analysis module needs.

Two detection sources, chosen automatically:
  - force plates (preferred, when present): per-plate vertical force (Fz)
    threshold crossing for HS/TO, center-of-pressure-to-heel-marker
    proximity to assign each contact to a foot.
  - markers only (fallback): a toe marker's height above the floor,
    thresholded -- simpler and more robust than an AP-projection (Zeni)
    method since it needs no walking-direction detection, at the cost of
    assuming a level, un-tilted capture volume (true for a typical gait lab).

Scope note: this is the "basic spatiotemporals" tier -- no stance/swing
phase percentages, no back-and-forth multi-pass segmentation (a trial with
direction reversals should be trimmed to one pass first). A forward-walking
axis is inferred per trial from whichever horizontal axis a heel marker
covers more range on; this only holds for a single straight pass.
"""

import numpy as np
import scipy.signal as _sig


# ── Marker helpers ───────────────────────────────────────────────────────────

def marker_xyz_array(kin, label):
    """(n, 3) ndarray of a marker's raw C3D-frame xyz (mm), NaN wherever the
    point's residual is negative (the C3D convention for an occluded/gap
    sample). Returns None if *label* isn't a loaded marker."""
    if label not in getattr(kin, "reallabels", []):
        return None
    pts = kin.data[label]
    arr = np.full((len(pts), 3), np.nan)
    for i, p in enumerate(pts):
        if p.error is not None and p.error < 0:
            continue
        arr[i] = p.xyz
    return arr


def angle_xyz_array(kin, label):
    """Same shape/gap-handling as marker_xyz_array(), for a Model Output
    (Angle) instead of a marker -- kin.data[label] uses the same point
    container for both, only the membership check differs. Returns None if
    *label* isn't a loaded angle."""
    if label not in getattr(kin, "anglelabels", []):
        return None
    pts = kin.data[label]
    arr = np.full((len(pts), 3), np.nan)
    for i, p in enumerate(pts):
        if p.error is not None and p.error < 0:
            continue
        arr[i] = p.xyz
    return arr


_JOINT_CANDIDATES = {
    ("Hip", "Right"): ["rhipangles", "r hip", "right hip"],
    ("Hip", "Left"): ["lhipangles", "l hip", "left hip"],
    ("Knee", "Right"): ["rkneeangles", "r knee", "right knee"],
    ("Knee", "Left"): ["lkneeangles", "l knee", "left knee"],
    ("Ankle", "Right"): ["rankleangles", "r ankle", "right ankle"],
    ("Ankle", "Left"): ["lankleangles", "l ankle", "left ankle"],
}


def guess_joint_angle_label(anglelabels, joint, side):
    """Best-effort angle label for (joint, side) in {"Hip","Knee","Ankle"} x
    {"Right","Left"} from common Plug-in-Gait-style naming. Returns None if
    no loaded angle looks like a match -- caller should fall back to
    simulated/example data rather than guessing further."""
    candidates = _JOINT_CANDIDATES.get((joint, side), [])
    lower_map = {l.lower(): l for l in anglelabels}
    for cand in candidates:
        if cand in lower_map:
            return lower_map[cand]
    joint_l = joint.lower()
    side_prefix = "r" if side == "Right" else "l"
    for label in anglelabels:
        low = label.lower()
        if joint_l in low and (low.startswith(side_prefix) or side.lower() in low):
            return label
    return None


def cycles_from_hs(hs_times):
    """[(t0, t1), ...] for consecutive same-side heel-strike times, each pair
    being one full gait cycle (HS to next same-side HS) for that side."""
    hs = sorted(hs_times)
    return [(hs[i], hs[i + 1]) for i in range(len(hs) - 1)]


def cycles_from_hs_or_fallback(hs_times_side, to_times_other_side):
    """Real same-foot HS-to-next-HS cycles (see cycles_from_hs) when there
    are 2+; otherwise, when there's exactly one HS on this side and at
    least one TO on the OTHER side after it, a single approximate window
    [HS(this side), nearest following TO(other side)]. This comes up under
    "Force-plate-verified cycles only" (gait_analysis_dialog.py) when a
    1-2-plate lab only verifies one footfall per foot -- too few for a real
    cycle, but still enough for a real (if approximate) window to measure
    EMG/CCI from instead of nothing.

    Returns (cycles, is_fallback) -- is_fallback True means the caller
    should label the result as an approximate window, not a verified full
    gait cycle (e.g. in a report caption or CSV column)."""
    real = cycles_from_hs(hs_times_side)
    if real:
        return real, False
    hs = sorted(hs_times_side)
    if len(hs) == 1 and to_times_other_side:
        later = sorted(t for t in to_times_other_side if t > hs[0])
        if later:
            return [(hs[0], later[0])], True
    return [], False


def resample_cycle(t, y, t0, t1, n_points=101):
    """Resample y (sampled at times t) to n_points evenly spaced fractions of
    [t0, t1] via linear interpolation -- the standard 0-100% gait-cycle
    normalization. Returns an (n_points,) ndarray; NaN throughout if the
    window is degenerate or has fewer than 2 valid (non-NaN) samples."""
    t = np.asarray(t, dtype=float)
    y = np.asarray(y, dtype=float)
    if t1 <= t0 or len(t) == 0:
        return np.full(n_points, np.nan)
    valid = ~np.isnan(y)
    if valid.sum() < 2:
        return np.full(n_points, np.nan)
    query_t = np.linspace(t0, t1, n_points)
    return np.interp(query_t, t[valid], y[valid], left=np.nan, right=np.nan)


_HEEL_CANDIDATES = {
    "Right": ["RHEE", "RHeel", "R_Heel", "RCAL", "RightHeel", "R.Heel"],
    "Left":  ["LHEE", "LHeel", "L_Heel", "LCAL", "LeftHeel", "L.Heel"],
}
_TOE_CANDIDATES = {
    "Right": ["RTOE", "RToe", "R_Toe", "RBigToe", "RightToe", "R.Toe"],
    "Left":  ["LTOE", "LToe", "L_Toe", "LBigToe", "LeftToe", "L.Toe"],
}


def _best_match(labels, candidates):
    lower_map = {l.lower(): l for l in labels}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None


def guess_gait_markers(labels):
    """Best-effort {role: label} guess for RightHeel/LeftHeel/RightToe/
    LeftToe from common naming conventions (Plug-in-Gait and a few
    variants). A role missing from the result couldn't be matched --
    callers should treat that source as unavailable rather than guessing
    further."""
    guess = {}
    for side, cands in _HEEL_CANDIDATES.items():
        m = _best_match(labels, cands)
        if m:
            guess[side + "Heel"] = m
    for side, cands in _TOE_CANDIDATES.items():
        m = _best_match(labels, cands)
        if m:
            guess[side + "Toe"] = m
    return guess


def _infer_forward_axis(positions_xyz):
    """0 (C3D X) or 1 (C3D Y) -- whichever horizontal axis has the larger
    range in *positions_xyz*, used as the walking direction. Cheap
    single-pass heuristic; see module docstring for its limits."""
    valid = positions_xyz[~np.any(np.isnan(positions_xyz), axis=1)]
    if len(valid) < 2:
        return 0
    range_x = valid[:, 0].max() - valid[:, 0].min()
    range_y = valid[:, 1].max() - valid[:, 1].min()
    return 0 if range_x >= range_y else 1


# ── Center of pressure ───────────────────────────────────────────────────────

def plate_cop(fp):
    """Center of pressure (lab-frame X/Y/Z, mm) for every sample of a
    ForcePlateGroup.

    Prefers the plate's own precomputed COP channels (fp.Cx/fp.Cy -- e.g.
    "COP1X"/"COP1Y" in the source C3D) when present: these are assumed to
    already be in lab-frame mm (the common convention, matching markers),
    and are more accurate than the derived version since they account for
    the plate's true origin offset. Falls back to the standard planar
    approximation COPx=-My/Fz, COPy=Mx/Fz about the plate's local
    center/axes (from `corners`) when only raw Fz/Mx/My are available --
    that approximation ignores the small correction for the force sensor's
    vertical offset from the plate surface (not available from
    FORCE_PLATFORM:CORNERS alone). Either way this is accurate enough to
    assign a contact to the nearest foot; it is not intended for
    moment/force-plate mechanics analysis.

    Returns an (n, 3) ndarray, or None if neither COP channels nor
    corners+moments are available. NaN rows mark samples with |Fz| below a
    stability floor (COP is undefined with no load).
    """
    fz = np.asarray(fp.Fz, dtype=float)
    n = len(fz)
    stable = np.abs(fz) >= 5.0  # newtons -- below this, COP is numerically meaningless

    if fp.Cx is not None and fp.Cy is not None:
        cx = np.asarray(fp.Cx, dtype=float)
        cy = np.asarray(fp.Cy, dtype=float)
        if len(cx) != n or len(cy) != n:
            return None
        z = float(np.asarray(fp.corners, dtype=float).mean(axis=0)[2]) if fp.corners is not None else 0.0
        cop = np.full((n, 3), np.nan)
        cop[stable, 0] = cx[stable]
        cop[stable, 1] = cy[stable]
        cop[stable, 2] = z
        return cop

    if fp.corners is None:
        return None
    mx = np.asarray(fp.Mx, dtype=float)
    my = np.asarray(fp.My, dtype=float)
    if len(mx) != n or len(my) != n:
        return None

    corners = np.asarray(fp.corners, dtype=float)
    origin = corners[0]
    ex = corners[1] - origin
    ey = corners[3] - origin
    ex_norm = np.linalg.norm(ex)
    ey_norm = np.linalg.norm(ey)
    if ex_norm == 0 or ey_norm == 0:
        return None
    ex_hat = ex / ex_norm
    ey_hat = ey / ey_norm
    center = corners.mean(axis=0)

    x_local = np.full(n, np.nan)
    y_local = np.full(n, np.nan)
    x_local[stable] = -my[stable] / fz[stable]
    y_local[stable] = mx[stable] / fz[stable]

    cop = np.full((n, 3), np.nan)
    cop[stable] = center + np.outer(x_local[stable], ex_hat) + np.outer(y_local[stable], ey_hat)
    return cop


# ── Force-plate HS/TO ─────────────────────────────────────────────────────────

def _sanitize_kernel(k):
    k = max(int(k), 3)
    return k if k % 2 == 1 else k + 1


def detect_plate_contacts(fz, fs, threshold_n=20.0, min_interval_s=0.5, median_kernel=11):
    """HS/TO sample indices from one plate's vertical force via median
    filter + threshold. Returns (hs_indices, to_indices) int ndarrays, same
    length -- each HS paired with the next TO after it; a trailing HS still
    in contact at the end of the trial is dropped (no matching TO)."""
    fz = np.asarray(fz, dtype=float)
    if len(fz) < 3:
        return np.array([], dtype=int), np.array([], dtype=int)

    fz_f = _sig.medfilt(np.abs(fz), kernel_size=_sanitize_kernel(median_kernel))
    contact = fz_f > threshold_n
    hs_all = np.where((~contact[:-1]) & contact[1:])[0] + 1
    to_all = sorted(int(t) for t in (np.where(contact[:-1] & (~contact[1:]))[0] + 1))

    min_gap = max(1, int(fs * min_interval_s))
    hs = []
    for idx in hs_all:
        if not hs or idx - hs[-1] >= min_gap:
            hs.append(int(idx))

    pairs = []
    for h in hs:
        later = [t for t in to_all if t > h]
        if later:
            pairs.append((h, later[0]))

    if not pairs:
        return np.array([], dtype=int), np.array([], dtype=int)
    hs_arr = np.array([p[0] for p in pairs], dtype=int)
    to_arr = np.array([p[1] for p in pairs], dtype=int)
    return hs_arr, to_arr


def assign_plate_contacts_to_feet(hs_idx, to_idx, cop, plate_fs, marker_xyz_by_label, marker_fs,
                                   right_heel_label, left_heel_label):
    """Assign each (hs, to) contact on one plate to 'Right'/'Left' by
    comparing the plate's COP (at the contact's midpoint) to the two heel
    markers' positions at the same instant. Returns a list[str or None]
    (same length as hs_idx) -- None where heel markers are missing/gapped,
    signalling the caller to fall back to alternation."""
    right = marker_xyz_by_label.get(right_heel_label)
    left = marker_xyz_by_label.get(left_heel_label)
    sides = []
    for h, t in zip(hs_idx, to_idx):
        if cop is None or right is None or left is None:
            sides.append(None)
            continue
        mid = h + (t - h) // 2
        if mid >= len(cop) or np.any(np.isnan(cop[mid])):
            sides.append(None)
            continue
        marker_i = int(round(mid * (marker_fs / plate_fs)))
        if marker_i >= len(right) or marker_i >= len(left):
            sides.append(None)
            continue
        r_pos, l_pos = right[marker_i], left[marker_i]
        if np.any(np.isnan(r_pos)) or np.any(np.isnan(l_pos)):
            sides.append(None)
            continue
        r_dist = np.linalg.norm(r_pos - cop[mid])
        l_dist = np.linalg.norm(l_pos - cop[mid])
        sides.append("Right" if r_dist <= l_dist else "Left")
    return sides


def detect_gait_events_force_plate(force_plates, marker_xyz_by_label, marker_fs,
                                    right_heel_label="RHEE", left_heel_label="LHEE",
                                    threshold_n=20.0, min_interval_s=0.5):
    """Returns {"Right": [(hs_t, to_t), ...], "Left": [...]}, times in
    seconds, sorted by hs_t. Contacts whose foot can't be determined by COP
    proximity (missing heel markers, gapped frame) fall back to alternating
    sides in chronological order."""
    events = []
    for fp in force_plates:
        hs_idx, to_idx = detect_plate_contacts(fp.Fz, fp.fs, threshold_n, min_interval_s)
        if len(hs_idx) == 0:
            continue
        cop = plate_cop(fp)
        sides = assign_plate_contacts_to_feet(
            hs_idx, to_idx, cop, fp.fs, marker_xyz_by_label, marker_fs,
            right_heel_label, left_heel_label,
        )
        for h, t, side in zip(hs_idx, to_idx, sides):
            events.append({"hs_t": h / fp.fs, "to_t": t / fp.fs, "side": side})

    events.sort(key=lambda e: e["hs_t"])

    next_guess = "Right"
    for e in events:
        if e["side"] is None:
            e["side"] = next_guess
        next_guess = "Left" if e["side"] == "Right" else "Right"

    result = {"Right": [], "Left": []}
    for e in events:
        result[e["side"]].append((e["hs_t"], e["to_t"]))
    return result


# ── Marker-only HS/TO fallback ────────────────────────────────────────────────

def detect_gait_events_marker_only(marker_xyz_by_label, marker_fs,
                                    right_toe_label="RTOE", left_toe_label="LTOE",
                                    height_threshold_mm=30.0, min_interval_s=0.5,
                                    median_kernel=5):
    """Height-threshold HS/TO: a foot is 'down' while its toe marker's
    vertical (C3D Z) position stays within height_threshold_mm of that
    marker's own trial floor estimate (a robust low percentile, not the
    bare minimum, so a single noisy low outlier doesn't shift it). The
    height signal is median-filtered and HS onsets are debounced by
    min_interval_s first -- same treatment detect_plate_contacts gives
    force data -- since without it, marker jitter right at the threshold
    produces several spurious HS/TO pairs a fraction of a second apart.

    Returns ({"Right": [(hs_t, to_t), ...], "Left": [...]}, warnings).
    """
    events = {"Right": [], "Left": []}
    warnings = []
    min_gap = max(1, int(marker_fs * min_interval_s))
    for side, label in (("Right", right_toe_label), ("Left", left_toe_label)):
        arr = marker_xyz_by_label.get(label)
        if arr is None:
            warnings.append("Marker '{}' not found -- no {} events.".format(label, side))
            continue
        z = arr[:, 2]
        valid = ~np.isnan(z)
        if valid.sum() < 3:
            warnings.append("Marker '{}' has too few valid samples.".format(label))
            continue
        z_filled = np.where(valid, z, np.nanmedian(z[valid]))
        z_f = _sig.medfilt(z_filled, kernel_size=_sanitize_kernel(median_kernel))
        floor_z = np.nanpercentile(z, 2)
        contact = (z_f - floor_z) < height_threshold_mm
        contact[~valid] = False

        diff = np.diff(contact.astype(int))
        hs_all = list(np.where(diff == 1)[0] + 1)
        to_all = sorted(int(t) for t in (np.where(diff == -1)[0] + 1))
        if contact[0]:
            hs_all = [0] + hs_all

        hs_idx = []
        for idx in sorted(int(t) for t in hs_all):
            if not hs_idx or idx - hs_idx[-1] >= min_gap:
                hs_idx.append(idx)

        pairs = []
        for h in hs_idx:
            later = [t for t in to_all if t > h]
            if later:
                pairs.append((h / marker_fs, later[0] / marker_fs))
        events[side] = pairs
    return events, warnings


# ── Spatiotemporal parameters ─────────────────────────────────────────────────

def _heel_xyz_mm(label, marker_xyz_by_label, marker_fs, t):
    arr = marker_xyz_by_label.get(label)
    if arr is None:
        return None
    idx = int(round(t * marker_fs))
    if idx < 0 or idx >= len(arr):
        return None
    pos = arr[idx]
    return None if np.any(np.isnan(pos)) else pos


def compute_spatiotemporals(events_by_side, marker_xyz_by_label, marker_fs,
                             right_heel_label="RHEE", left_heel_label="LHEE"):
    """events_by_side: {"Right": [(hs_t, to_t), ...], "Left": [...]}.

    Returns {"steps": [...], "stride": {"Right": {...}, "Left": {...}},
    "forward_axis": "X"|"Y", "warnings": [...]}. NaN marks a metric that
    couldn't be computed (usually a heel-marker gap at that instant).
    """
    warnings = []
    heel_label = {"Right": right_heel_label, "Left": left_heel_label}

    ref = marker_xyz_by_label.get(right_heel_label)
    if ref is None:
        ref = marker_xyz_by_label.get(left_heel_label)
    forward_axis = _infer_forward_axis(ref) if ref is not None else 0

    all_hs = sorted(
        [("Right", hs, to) for hs, to in events_by_side.get("Right", [])] +
        [("Left", hs, to) for hs, to in events_by_side.get("Left", [])],
        key=lambda x: x[1],
    )

    steps = []
    for i in range(len(all_hs) - 1):
        side, hs_t, _ = all_hs[i]
        next_side, next_hs_t, _ = all_hs[i + 1]
        if next_side == side:
            continue  # same-foot consecutive HS is a stride, not a step
        p0 = _heel_xyz_mm(heel_label[side], marker_xyz_by_label, marker_fs, hs_t)
        p1 = _heel_xyz_mm(heel_label[next_side], marker_xyz_by_label, marker_fs, next_hs_t)
        step_time = next_hs_t - hs_t
        width_axis = 1 - forward_axis
        if p0 is None or p1 is None:
            length_m = float("nan")
            width_m = float("nan")
            warnings.append("Step at {:.3f}s: heel marker gap, length not computed.".format(next_hs_t))
        else:
            length_m = abs(p1[forward_axis] - p0[forward_axis]) / 1000.0
            width_m = abs(p1[width_axis] - p0[width_axis]) / 1000.0
        steps.append({
            "side": next_side,  # the step lands on this foot
            "hs_t": next_hs_t,
            "step_length_m": length_m,
            "step_width_m": width_m,
            "step_time_s": step_time,
            "cadence_spm": (60.0 / step_time) if step_time > 0 else float("nan"),
        })

    stride = {}
    for side in ("Right", "Left"):
        hs_list = sorted(hs for hs, _to in events_by_side.get(side, []))
        lengths, times, cadences, velocities = [], [], [], []
        for i in range(len(hs_list) - 1):
            t0, t1 = hs_list[i], hs_list[i + 1]
            p0 = _heel_xyz_mm(heel_label[side], marker_xyz_by_label, marker_fs, t0)
            p1 = _heel_xyz_mm(heel_label[side], marker_xyz_by_label, marker_fs, t1)
            stride_time = t1 - t0
            if stride_time <= 0:
                continue
            if p0 is None or p1 is None:
                length = float("nan")
            else:
                length = abs(p1[forward_axis] - p0[forward_axis]) / 1000.0
            lengths.append(length)
            times.append(stride_time)
            cadences.append(120.0 / stride_time)
            velocities.append(length / stride_time if not np.isnan(length) else float("nan"))
        # Every metric here is a (mean, sd) tuple averaged across all strides
        # detected for this side, not just the first one -- an earlier
        # version of this function only used the first stride pair, which
        # silently discarded the rest of a multi-cycle trial.
        stride[side] = {
            "stride_length_m": _mean_std(lengths),
            "stride_time_s": _mean_std(times),
            "cadence_spm": _mean_std(cadences),
            "velocity_m_s": _mean_std(velocities),
        }

    return {"steps": steps, "stride": stride, "warnings": warnings,
            "forward_axis": "XY"[forward_axis]}


def aggregate_steps(steps):
    """Aggregate compute_spatiotemporals()'s flat per-step list into mean+SD
    per side for length/time/cadence, plus one combined mean+SD across both
    sides for width -- step width describes the lateral spacing between two
    opposite feet, not a single foot's own property, so it isn't split by
    side (matches the reference clinical report's convention).

    Returns {"Right": {"step_length_m": (mean,sd), "step_time_s": (mean,sd),
    "cadence_spm": (mean,sd)}, "Left": {...}, "step_width_m": (mean,sd)}.
    """
    result = {"Right": {}, "Left": {}}
    for side in ("Right", "Left"):
        side_steps = [s for s in steps if s["side"] == side]
        for key in ("step_length_m", "step_time_s", "cadence_spm"):
            result[side][key] = _mean_std([s[key] for s in side_steps])
    result["step_width_m"] = _mean_std([s["step_width_m"] for s in steps])
    return result


def _merge_side_events(marker_pairs, plate_pairs, tolerance_s=0.25):
    """Combine one side's marker-based (hs, to) pairs with its force-plate
    pairs: markers cover every footfall across the whole trial (a walkway is
    usually much longer than the 1-2 plates on it), while a force plate only
    sees the few footfalls that happen to land on it -- but times those more
    accurately. For each plate contact, replace the nearest marker contact
    within *tolerance_s* (same footfall, more accurate timing); if none is
    close enough, add it as an extra footfall the markers alone missed
    (e.g. a plate at the very start/end of the marker-based detection
    window). Returns [(hs, to, source), ...] sorted by hs, source being
    "plate" or "marker" -- the source tag a plate-verified cycle carries
    through to its TrialEvent (see gait_analysis_dialog._apply_gait_events)
    and the Manual Cycles editor's row coloring."""
    merged = [(hs, to, "marker") for hs, to in marker_pairs]
    for p_hs, p_to in plate_pairs:
        best_i, best_d = None, tolerance_s
        for i, (m_hs, _m_to, _src) in enumerate(merged):
            d = abs(m_hs - p_hs)
            if d < best_d:
                best_i, best_d = i, d
        if best_i is not None:
            merged[best_i] = (p_hs, p_to, "plate")
        else:
            merged.append((p_hs, p_to, "plate"))
    return sorted(merged, key=lambda triple: triple[0])


def pair_hs_to(hs_times, to_times):
    """Pair each HS with the nearest following TO (same foot's own toe-off
    ending that stance instance) -- [(hs, to), ...] sorted by hs, NaN for the
    "to" of a trailing HS with no later TO (still in contact at trial end, or
    a manually-added HS with no matching TO). Used to recover a real
    HS/TO pairing for phase-percentage and toe-out-angle calculations after
    events have gone through the flat, side-only shape the Gait Analysis
    dialog's event list stores them in (see _current_hs_to_by_side)."""
    hs = sorted(hs_times)
    to_sorted = sorted(to_times)
    pairs = []
    for h in hs:
        later = [t for t in to_sorted if t > h]
        pairs.append((h, later[0] if later else float("nan")))
    return pairs


def _mean_std(values):
    values = [v for v in values if not np.isnan(v)]
    if not values:
        return float("nan"), float("nan")
    return float(np.mean(values)), float(np.std(values))


def compute_phase_percentages(events_by_side, to_by_side=None):
    """Stance/swing/loading-response/pre-swing/single-support % (per side,
    mean+SD across cycles) and one combined double-support % (both feet's
    double-support windows describe the same shared events, so it isn't
    split by side -- matches the convention in the reference clinical
    report template).

    events_by_side: {"Right": [(hs_t, to_t), ...], "Left": [...]} -- real
    same-foot HS/TO pairs (from detect_gait_events_force_plate/marker_only,
    or pair_hs_to() when recovering pairs from a flat event list).

    to_by_side: optional {"Right": [t, ...], "Left": [...]} raw toe-off
    times (not paired to any particular HS). When a side has fewer than two
    real same-foot HS/TO pairs (so no full HS-to-next-HS cycle -- see
    cycles_from_hs), falls back to the same approximate HS(this side)-to-
    TO(opposite side) window already used for EMG/CCI (see
    cycles_from_hs_or_fallback) as a stand-in "cycle end" -- otherwise a
    1-2-plate lab that only verifies one footfall per foot ("Force-plate-
    verified cycles only" in gait_analysis_dialog.py) gets an all-NaN phase
    breakdown and, downstream, no gait-cycle illustration at all. Flagged
    per side via the returned "is_fallback" dict rather than blended in
    unlabeled.

    Definitions (see Perry-style gait phase glossary):
      stance   = HS[i] to TO[i] (own foot), as %% of HS[i] to HS[i+1]
      loading response = HS[i] to the opposite foot's TO occurring in
                 (HS[i], TO[i]) -- the initial double-support window
      pre-swing = the opposite foot's HS occurring in (HS[i], TO[i]) to
                 TO[i] -- the terminal double-support window
      single support = stance - loading response - pre-swing
      double support (combined) = loading response + pre-swing, pooled
                 across both feet's cycles

    Returns {"Right": {...}, "Left": {...}, "double_support_pct": (mean, sd),
    "is_fallback": {"Right": bool, "Left": bool}}. Each per-side value is a
    (mean, sd) tuple; NaN where too few complete cycles exist to compute it.
    """
    result = {}
    is_fallback = {"Right": False, "Left": False}
    ds_values = []
    for side, opp in (("Right", "Left"), ("Left", "Right")):
        pairs = sorted(events_by_side.get(side, []), key=lambda p: p[0])
        opp_pairs = sorted(events_by_side.get(opp, []), key=lambda p: p[0])
        stance_pcts, swing_pcts, lr_pcts, ps_pcts, ss_pcts = [], [], [], [], []

        if len(pairs) >= 2:
            windows = [(pairs[i][0], pairs[i][1], pairs[i + 1][0]) for i in range(len(pairs) - 1)]
        elif len(pairs) == 1 and to_by_side is not None and not np.isnan(pairs[0][1]):
            hs, to = pairs[0]
            later_opp_to = sorted(t for t in to_by_side.get(opp, []) if t > hs)
            windows = [(hs, to, later_opp_to[0])] if later_opp_to else []
            if windows:
                is_fallback[side] = True
        else:
            windows = []

        for hs, to, next_hs in windows:
            if np.isnan(to) or next_hs <= hs or not (hs < to < next_hs):
                continue
            cycle_dur = next_hs - hs
            stance_dur = to - hs
            stance_pct = stance_dur / cycle_dur * 100.0
            swing_pct = 100.0 - stance_pct
            stance_pcts.append(stance_pct)
            swing_pcts.append(swing_pct)

            # Loading response / pre-swing / single support need the
            # opposite foot's own events to know where double support
            # starts/ends -- if that side has no data at all (e.g. it never
            # contacted a force plate and markers weren't available), these
            # are unknown, not zero. Leaving them out of the mean here (NaN)
            # avoids reporting a fabricated "0% double support" for a side
            # we simply have no opposite-foot data for.
            if not opp_pairs:
                continue
            opp_to_in_window = [t for _h, t in opp_pairs if hs < t < to]
            lr_dur = (min(opp_to_in_window) - hs) if opp_to_in_window else 0.0
            opp_hs_in_window = [h for h, _t in opp_pairs if hs < h < to]
            ps_dur = (to - max(opp_hs_in_window)) if opp_hs_in_window else 0.0
            lr_pct = lr_dur / cycle_dur * 100.0
            ps_pct = ps_dur / cycle_dur * 100.0
            ss_pct = max(0.0, stance_pct - lr_pct - ps_pct)

            lr_pcts.append(lr_pct)
            ps_pcts.append(ps_pct)
            ss_pcts.append(ss_pct)
            ds_values.append(lr_pct + ps_pct)

        result[side] = {
            "stance_pct": _mean_std(stance_pcts),
            "swing_pct": _mean_std(swing_pcts),
            "loading_response_pct": _mean_std(lr_pcts),
            "pre_swing_pct": _mean_std(ps_pcts),
            "single_support_pct": _mean_std(ss_pcts),
        }
    result["double_support_pct"] = _mean_std(ds_values)
    result["is_fallback"] = is_fallback
    return result


def compute_toe_out_angles(events_by_side, marker_xyz_by_label, marker_fs,
                            heel_label, toe_label, forward_axis, mirror=False):
    """Toe-out angle (deg, mean+SD across cycles) for one side: the angle
    between the foot's long axis (heel->toe, at each HS) and the walking
    (forward_axis) direction, in the horizontal plane. Returns (mean, sd);
    NaN if the heel/toe marker is missing or gapped at every HS.

    mirror: negate the lateral (width-axis) component before taking the
    angle. The width axis has one fixed sign for the whole trial, but
    "outward" is the opposite lateral direction for the left vs. right
    foot -- without mirroring one side, a symmetric toe-out gait reads as
    +N deg on one foot and -N deg on the other for the *same* physical
    posture. Pass mirror=True for the left foot so positive consistently
    means toed-out and negative toed-in on both feet."""
    width_axis = 1 - forward_axis
    sign = -1.0 if mirror else 1.0
    angles = []
    for hs, _to in events_by_side:
        heel = _heel_xyz_mm(heel_label, marker_xyz_by_label, marker_fs, hs)
        toe = _heel_xyz_mm(toe_label, marker_xyz_by_label, marker_fs, hs)
        if heel is None or toe is None:
            continue
        vec = toe - heel
        angle = np.degrees(np.arctan2(sign * vec[width_axis], vec[forward_axis]))
        angles.append(angle)
    return _mean_std(angles)


# ── Muscle co-contraction index ──────────────────────────────────────────────

def _normalize_envelope(env, normalize):
    """normalize: "none" (unchanged) or "trial_max" (divide by the channel's
    own peak envelope value over the whole trial -- the app's existing
    "trial max" normalization option). Rudolph's CCI formula scales with the
    raw signals' absolute amplitude (unlike Falconer & Winter's, which is a
    pure ratio), so on unnormalized EMG -- typically ~1e-5 V for a raw
    envelope -- it reads as a real but visually-indistinguishable-from-zero
    number. Normalizing first makes it a meaningful, comparable index."""
    if normalize == "trial_max":
        m = float(np.nanmax(env)) if len(env) else 0.0
        return env / m if m > 0 else env
    return env


def compute_cci_for_side(a_env, a_fs, b_env, b_fs, cycles, a_name, b_name, method_key, normalize="none"):
    """CCI for one muscle pair over a list of (t0, t1) gait cycles: each
    cycle of both enveloped signals is resampled to 101 pts and concatenated
    before computing CCI, so the index reflects only the gait-cycle-relative
    activity pattern, never the whole-trial signal. Returns NaN if no cycle
    has valid (non-gapped) data on both channels. See _normalize_envelope
    for the normalize parameter."""
    a_env = _normalize_envelope(a_env, normalize)
    b_env = _normalize_envelope(b_env, normalize)
    ta = np.arange(len(a_env)) / a_fs
    tb = np.arange(len(b_env)) / b_fs
    a_curves = [resample_cycle(ta, a_env, t0, t1, 101) for t0, t1 in cycles]
    b_curves = [resample_cycle(tb, b_env, t0, t1, 101) for t0, t1 in cycles]
    valid = [
        (ac, bc) for ac, bc in zip(a_curves, b_curves)
        if not np.any(np.isnan(ac)) and not np.any(np.isnan(bc))
    ]
    if not valid:
        return float("nan")
    a_concat = np.concatenate([v[0] for v in valid])
    b_concat = np.concatenate([v[1] for v in valid])
    try:
        from modules.pyMotion.core.timeSeriesTable import timeSeriesTable
        tmp = timeSeriesTable(1.0, [a_name, b_name], {a_name: a_concat, b_name: b_concat})
        return tmp.cocontraction(a_name, b_name, method_key)
    except Exception:
        return float("nan")


def compute_cci_pair(a_name, a_env, a_fs, b_name, b_env, b_fs, hs_by_side, to_by_side,
                      side, method_key, normalize="none"):
    """(cci_or_nan, is_fallback) for one muscle pair on ONE side -- CCI only
    makes sense between two channels recorded on the same leg (see
    GaitAnalysisDialog's Co-contraction pair picker, which has its own Side
    column), so this computes exactly the side the pair was configured for,
    not both. cycles come from cycles_from_hs_or_fallback: real same-foot
    HS-to-HS cycles when there are 2+, otherwise a single approximate
    HS(this side)-to-TO(other side) window when that's all verified data
    allows -- is_fallback flags which one was used so the caller can label
    an approximate result instead of presenting it as a verified cycle."""
    opposite = "Left" if side == "Right" else "Right"
    cycles, is_fallback = cycles_from_hs_or_fallback(
        hs_by_side.get(side, []), to_by_side.get(opposite, []),
    )
    value = compute_cci_for_side(
        a_env, a_fs, b_env, b_fs, cycles, a_name, b_name, method_key, normalize,
    )
    return value, is_fallback


# ── Top-level entry point ─────────────────────────────────────────────────────

def detect_gait_events(force_plates, marker_xyz_by_label, marker_fs,
                        right_heel_label="RHEE", left_heel_label="LHEE",
                        right_toe_label="RTOE", left_toe_label="LTOE",
                        threshold_n=20.0, min_interval_s=0.5, height_threshold_mm=30.0):
    """Detect gait HS/TO events and basic spatiotemporals for one trial.

    Runs both detectors when both inputs are available and merges them per
    side (see _merge_side_events): force plates only see the handful of
    footfalls that land on a plate, while toe-marker height detection covers
    every footfall across the whole walkway, so a typical 1-2-plate lab
    setup still gets a real trial-wide event stream instead of being capped
    at 1 stride per side. Falls back to whichever single source is actually
    available (unchanged behavior when only one is).

    Returns {"source": "force_plate"|"markers"|"force_plate+markers"|"none",
    "HS": {...}, "TO": {...}, "event_sources": {side: {"HS": {t: src}, "TO":
    {t: src}}}, "steps": [...], "stride": {...}, "forward_axis": "X"|"Y",
    "warnings": [...]}.
    """
    warnings = []
    plate_events = {"Right": [], "Left": []}
    marker_events = {"Right": [], "Left": []}
    have_plate = False
    have_markers = False

    if force_plates:
        plate_events = detect_gait_events_force_plate(
            force_plates, marker_xyz_by_label, marker_fs,
            right_heel_label, left_heel_label, threshold_n, min_interval_s,
        )
        have_plate = bool(plate_events["Right"] or plate_events["Left"])
        if not have_plate:
            warnings.append("No force-plate contacts detected.")

    has_toe_marker = right_toe_label in marker_xyz_by_label or left_toe_label in marker_xyz_by_label
    if has_toe_marker:
        marker_events, mk_warnings = detect_gait_events_marker_only(
            marker_xyz_by_label, marker_fs, right_toe_label, left_toe_label,
            height_threshold_mm, min_interval_s,
        )
        have_markers = bool(marker_events["Right"] or marker_events["Left"])
        warnings.extend(mk_warnings)

    # event_sources[side]["HS"/"TO"][t] = "plate"/"marker" -- which detector
    # actually produced that instant, for the Manual Cycles editor's row
    # coloring and the report/CSV's "verified cycles only" toggle (see
    # gait_analysis_dialog._apply_gait_events / _current_hs_to_by_side).
    event_sources = {"Right": {"HS": {}, "TO": {}}, "Left": {"HS": {}, "TO": {}}}

    if have_plate and have_markers:
        events_by_side = {}
        for side in ("Right", "Left"):
            triples = _merge_side_events(marker_events[side], plate_events[side])
            events_by_side[side] = [(hs, to) for hs, to, _src in triples]
            event_sources[side]["HS"] = {hs: src for hs, _to, src in triples}
            event_sources[side]["TO"] = {to: src for _hs, to, src in triples}
        source = "force_plate+markers"
    elif have_plate:
        events_by_side = plate_events
        for side in ("Right", "Left"):
            event_sources[side]["HS"] = {hs: "plate" for hs, _to in plate_events[side]}
            event_sources[side]["TO"] = {to: "plate" for _hs, to in plate_events[side]}
        source = "force_plate"
    elif have_markers:
        events_by_side = marker_events
        for side in ("Right", "Left"):
            event_sources[side]["HS"] = {hs: "marker" for hs, _to in marker_events[side]}
            event_sources[side]["TO"] = {to: "marker" for _hs, to in marker_events[side]}
        source = "markers"
    else:
        events_by_side = {"Right": [], "Left": []}
        source = "none"
        warnings.append("No usable force-plate or marker data for gait event detection.")

    spatio = compute_spatiotemporals(
        events_by_side, marker_xyz_by_label, marker_fs, right_heel_label, left_heel_label,
    )
    warnings.extend(spatio["warnings"])

    phases = compute_phase_percentages(events_by_side)

    forward_axis_idx = 0 if spatio["forward_axis"] == "X" else 1
    toe_label = {"Right": right_toe_label, "Left": left_toe_label}
    heel_label = {"Right": right_heel_label, "Left": left_heel_label}
    for side in ("Right", "Left"):
        if heel_label[side] not in marker_xyz_by_label or toe_label[side] not in marker_xyz_by_label:
            warnings.append(
                "Toe-out angle for {} needs both a heel and a toe marker mapped "
                "(one or both are missing) -- left as N/A.".format(side)
            )
    toe_out = {
        side: compute_toe_out_angles(
            events_by_side.get(side, []), marker_xyz_by_label, marker_fs,
            heel_label[side], toe_label[side], forward_axis_idx, mirror=(side == "Left"),
        )
        for side in ("Right", "Left")
    }

    return {
        "source": source,
        "HS": {side: [hs for hs, _to in pairs] for side, pairs in events_by_side.items()},
        "TO": {side: [to for _hs, to in pairs] for side, pairs in events_by_side.items()},
        "event_sources": event_sources,
        "steps": spatio["steps"],
        "stride": spatio["stride"],
        "phases": phases,
        "toe_out_deg": toe_out,
        "forward_axis": spatio["forward_axis"],
        "warnings": warnings,
    }
