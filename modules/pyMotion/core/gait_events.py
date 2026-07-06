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
                                    height_threshold_mm=30.0):
    """Height-threshold HS/TO: a foot is 'down' while its toe marker's
    vertical (C3D Z) position stays within height_threshold_mm of that
    marker's own trial floor estimate (a robust low percentile, not the
    bare minimum, so a single noisy low outlier doesn't shift it).

    Returns ({"Right": [(hs_t, to_t), ...], "Left": [...]}, warnings).
    """
    events = {"Right": [], "Left": []}
    warnings = []
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
        floor_z = np.nanpercentile(z, 2)
        contact = (z - floor_z) < height_threshold_mm
        contact[~valid] = False

        diff = np.diff(contact.astype(int))
        hs_idx = list(np.where(diff == 1)[0] + 1)
        to_idx = sorted(int(t) for t in (np.where(diff == -1)[0] + 1))
        if contact[0]:
            hs_idx = [0] + hs_idx

        pairs = []
        for h in sorted(int(t) for t in hs_idx):
            later = [t for t in to_idx if t > h]
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
        if p0 is None or p1 is None:
            length_m = float("nan")
            warnings.append("Step at {:.3f}s: heel marker gap, length not computed.".format(next_hs_t))
        else:
            length_m = abs(p1[forward_axis] - p0[forward_axis]) / 1000.0
        steps.append({
            "side": next_side,  # the step lands on this foot
            "hs_t": next_hs_t,
            "step_length_m": length_m,
            "step_time_s": step_time,
            "cadence_spm": (60.0 / step_time) if step_time > 0 else float("nan"),
        })

    stride = {}
    for side in ("Right", "Left"):
        hs_list = sorted(hs for hs, _to in events_by_side.get(side, []))
        if len(hs_list) >= 2:
            t0, t1 = hs_list[0], hs_list[1]
            p0 = _heel_xyz_mm(heel_label[side], marker_xyz_by_label, marker_fs, t0)
            p1 = _heel_xyz_mm(heel_label[side], marker_xyz_by_label, marker_fs, t1)
            stride_time = t1 - t0
            if p0 is None or p1 is None or stride_time <= 0:
                stride_length = float("nan")
            else:
                stride_length = abs(p1[forward_axis] - p0[forward_axis]) / 1000.0
            stride[side] = {
                "stride_length_m": stride_length,
                "stride_time_s": stride_time,
                "cadence_spm": (120.0 / stride_time) if stride_time > 0 else float("nan"),
                "velocity_m_s": (stride_length / stride_time)
                                if (stride_time > 0 and not np.isnan(stride_length)) else float("nan"),
            }
        else:
            stride[side] = {"stride_length_m": float("nan"), "stride_time_s": float("nan"),
                             "cadence_spm": float("nan"), "velocity_m_s": float("nan")}

    return {"steps": steps, "stride": stride, "warnings": warnings,
            "forward_axis": "XY"[forward_axis]}


# ── Top-level entry point ─────────────────────────────────────────────────────

def detect_gait_events(force_plates, marker_xyz_by_label, marker_fs,
                        right_heel_label="RHEE", left_heel_label="LHEE",
                        right_toe_label="RTOE", left_toe_label="LTOE",
                        threshold_n=20.0, min_interval_s=0.5, height_threshold_mm=30.0):
    """Detect gait HS/TO events and basic spatiotemporals for one trial.

    Prefers force plates when present; falls back to marker-only detection
    when there are none, or when the force-plate pass finds nothing (e.g.
    the person never contacted a plate).

    Returns {"source": "force_plate"|"markers", "HS": {...}, "TO": {...},
    "steps": [...], "stride": {...}, "forward_axis": "X"|"Y",
    "warnings": [...]}.
    """
    warnings = []
    events_by_side = {"Right": [], "Left": []}
    source = None

    if force_plates:
        events_by_side = detect_gait_events_force_plate(
            force_plates, marker_xyz_by_label, marker_fs,
            right_heel_label, left_heel_label, threshold_n, min_interval_s,
        )
        source = "force_plate"
        if not events_by_side["Right"] and not events_by_side["Left"]:
            warnings.append("No force-plate contacts detected; falling back to marker-based detection.")
            source = None

    if source is None:
        events_by_side, mk_warnings = detect_gait_events_marker_only(
            marker_xyz_by_label, marker_fs, right_toe_label, left_toe_label, height_threshold_mm,
        )
        warnings.extend(mk_warnings)
        source = "markers"

    spatio = compute_spatiotemporals(
        events_by_side, marker_xyz_by_label, marker_fs, right_heel_label, left_heel_label,
    )
    warnings.extend(spatio["warnings"])

    return {
        "source": source,
        "HS": {side: [hs for hs, _to in pairs] for side, pairs in events_by_side.items()},
        "TO": {side: [to for _hs, to in pairs] for side, pairs in events_by_side.items()},
        "steps": spatio["steps"],
        "stride": spatio["stride"],
        "forward_axis": spatio["forward_axis"],
        "warnings": warnings,
    }
