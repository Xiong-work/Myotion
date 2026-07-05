"""Gap-fill C3D marker trajectories, matching the methods Vicon Nexus's own
gap-fill tools offer, and export the result as a new .c3d file (the
original file is never modified):

- spline_fill      -- Spline Fill (Woltring-style quintic interpolation)
- pattern_fill     -- Pattern Fill (copy a donor marker's movement pattern)
- rigid_body_fill  -- Rigid Body Fill (Kabsch rotation solve from a cluster)
- cyclic_fill      -- Cyclic Fill (detrend + autocorrelation period match)
- linear_fill      -- Linear Fill (straight line across the gap)
- rename_marker    -- rename a marker (keeps c3d_obj's own labels in sync)

The rigid-body math (Kabsch rotation solve) is reused as-is from
c3d_convert.py, where it was ported from the GRN_gait project's tested
conversion scripts for a fixed pelvis cluster. Here the same math is applied
to a caller-chosen target marker + reference-marker set (picked explicitly
per gap in the UI) instead of a fixed default cluster.
"""

import numpy as np
import ezc3d
from scipy.interpolate import make_interp_spline

from modules.playground.c3d_convert import _average_rigid_template, _solve_rigid_and_reconstruct

DEFAULT_SPLINE_MAX_GAP = 15  # frames -- matches c3d_convert.py's "brief occlusion blip" default


class GapFillError(Exception):
    pass


class C3dMarkerData:
    def __init__(self, c3d_obj, labels, marker_indices, marker_points, frame_rate, first_frame):
        self.c3d_obj = c3d_obj                  # raw ezc3d object, kept so export can preserve everything else
        self.labels = labels                    # real marker names ("*NN" placeholder channels excluded)
        self.marker_indices = marker_indices     # index into c3d_obj's POINT rows, parallel to labels
        self.marker_points = marker_points       # (3, n_markers, n_frames) ndarray, NaN = missing, mm
        self.frame_rate = frame_rate
        self.first_frame = first_frame


class Gap:
    def __init__(self, marker, marker_idx, start, end):
        self.marker = marker
        self.marker_idx = marker_idx
        self.start = start  # inclusive frame index (0-based, within marker_points)
        self.end = end      # inclusive

    @property
    def length(self):
        return self.end - self.start + 1

    def __repr__(self):
        return f"Gap({self.marker}, {self.start}-{self.end}, {self.length} frames)"


def load_c3d_markers(path):
    """Load a C3D file's marker trajectories for gap inspection/filling.
    Untracked placeholder channels (labels starting with "*", the Vicon
    convention for reserved-but-unused marker slots) are excluded, matching
    c3d_convert.py's own marker filtering."""
    try:
        c3d_obj = ezc3d.c3d(path)
    except Exception as e:
        raise GapFillError(f"Could not read C3D file: {path}. {e}") from e

    labels_all = c3d_obj["parameters"]["POINT"]["LABELS"]["value"]
    if not labels_all:
        raise GapFillError(f"C3D file has no point trajectories: {path}")

    marker_indices = [
        i for i, s in enumerate(labels_all)
        if str(s).strip() != "" and not str(s).strip().startswith("*")
    ]
    labels = [labels_all[i].strip() for i in marker_indices]
    if not labels:
        raise GapFillError(f"C3D file has no named marker trajectories: {path}")

    points = c3d_obj["data"]["points"]
    marker_points = points[:3, marker_indices, :].copy()  # working copy -- original c3d_obj is untouched until export

    frame_rate = float(c3d_obj["header"]["points"]["frame_rate"])
    first_frame = int(c3d_obj["header"]["points"]["first_frame"])

    return C3dMarkerData(c3d_obj, labels, marker_indices, marker_points, frame_rate, first_frame)


def detect_gaps(marker_points, labels):
    """Return {marker_name: [Gap, ...]} -- every missing-data run per marker,
    in frame order. Markers with no gaps map to an empty list."""
    n_markers = marker_points.shape[1]
    nan_mask = np.isnan(marker_points).any(axis=0)  # (n_markers, n_frames)

    gaps = {}
    for m in range(n_markers):
        runs = []
        idx = np.where(nan_mask[m])[0]
        if idx.size:
            start = prev = idx[0]
            for x in idx[1:]:
                if x != prev + 1:
                    runs.append(Gap(labels[m], m, start, prev))
                    start = x
                prev = x
            runs.append(Gap(labels[m], m, start, prev))
        gaps[labels[m]] = runs
    return gaps


def frame_presence_count(marker_points):
    """Per-frame count of markers with valid (non-NaN) data. Frames where
    this is 0 have no marker data at all in the whole system (e.g. the
    subject hadn't yet entered the capture volume) and cannot be filled by
    any method here -- both fill functions below will raise GapFillError for
    a gap that falls entirely in such a region, rather than fabricate data."""
    return (~np.isnan(marker_points).any(axis=0)).sum(axis=0)


def spline_fill(marker_points, marker_idx, start, end, context_frames=10, degree=5):
    """Return a new marker_points with marker_idx's [start, end] gap filled
    by spline interpolation, using that marker's own valid samples
    immediately surrounding the gap (independent per X/Y/Z axis). Defaults
    to a quintic (degree=5) spline, matching Woltring's GCVSPL convention --
    this is a plain interpolating spline through the surrounding data
    (not Woltring's full generalized-cross-validation *smoothing* spline,
    which also denoises the valid data itself -- a separate concern from
    bridging a gap), degrading to a lower order automatically if too few
    context frames are available to fit a quintic. Requires valid data on
    both sides of the gap -- raises GapFillError if the gap touches the
    start or end of the trial, or if there's no surrounding data at all."""
    series = marker_points[:, marker_idx, :]
    valid = ~np.isnan(series).any(axis=0)

    before = np.where(valid[:start])[0]
    after = np.where(valid[end + 1:])[0] + (end + 1)
    if before.size < 2 or after.size < 2:
        raise GapFillError(
            "Spline fill needs at least 2 valid frames on both sides of the gap; "
            "this gap is missing that context (likely touches the start/end of the trial)."
        )
    before = before[-context_frames:]
    after = after[:context_frames]
    sample_frames = np.concatenate([before, after])
    k = min(degree, len(sample_frames) - 1)

    filled = marker_points.copy()
    gap_frames = np.arange(start, end + 1)
    for axis in range(3):
        spline = make_interp_spline(sample_frames, series[axis, sample_frames], k=k)
        filled[axis, marker_idx, start:end + 1] = spline(gap_frames)
    return filled


def pattern_fill(marker_points, labels, target_label, donor_label, start, end, context_frames=10):
    """Return a new marker_points with target_label's [start, end] gap
    filled by copying donor_label's own movement pattern: a constant offset
    (target - donor, averaged over valid frames immediately surrounding the
    gap) is added to the donor's trajectory across the gap. Most effective
    when donor and target move together (e.g. two markers on the same rigid
    segment) -- Vicon's "Pattern Fill". Raises GapFillError if the donor
    isn't fully visible across the gap, or there's no nearby frame where
    both target and donor are valid to solve the offset from."""
    name_to_idx = {name: i for i, name in enumerate(labels)}
    target_idx = name_to_idx.get(target_label)
    donor_idx = name_to_idx.get(donor_label)
    if target_idx is None or donor_idx is None:
        raise GapFillError("Unknown target/donor marker.")
    if donor_idx == target_idx:
        raise GapFillError("Donor marker must be different from the target marker.")

    target_series = marker_points[:, target_idx, :]
    donor_series = marker_points[:, donor_idx, :]
    donor_valid = ~np.isnan(donor_series).any(axis=0)
    if not donor_valid[start:end + 1].all():
        raise GapFillError(f"Donor marker '{donor_label}' is not fully visible across the whole gap.")

    target_valid = ~np.isnan(target_series).any(axis=0)
    both_valid = target_valid & donor_valid
    before = np.where(both_valid[:start])[0]
    after = np.where(both_valid[end + 1:])[0] + (end + 1)
    context = np.concatenate([before[-context_frames:], after[:context_frames]])
    if context.size == 0:
        raise GapFillError(
            "Pattern fill needs at least one frame where both target and donor are visible, "
            "near the gap, to compute the offset between them."
        )
    offset = (target_series[:, context] - donor_series[:, context]).mean(axis=1)

    filled = marker_points.copy()
    gap_frames = np.arange(start, end + 1)
    filled[:, target_idx, gap_frames] = donor_series[:, gap_frames] + offset[:, None]
    return filled


def rigid_body_fill(marker_points, labels, target_label, reference_labels, start, end, min_ref_frames=5):
    """Return a new marker_points with target_label's [start, end] gap filled
    by rigid-body reconstruction from reference_labels: a rotation+
    translation is solved per gap-frame (Kabsch, from whichever reference
    markers are actually present that frame) that maps a template built from
    frames where every one of target+references is simultaneously visible.
    Raises GapFillError if there are fewer than 2 usable reference markers,
    or fewer than min_ref_frames frames to build the template from."""
    name_to_idx = {name: i for i, name in enumerate(labels)}
    target_idx = name_to_idx.get(target_label)
    ref_idxs = [name_to_idx[r] for r in reference_labels if r in name_to_idx and r != target_label]
    if target_idx is None:
        raise GapFillError(f"Unknown marker: {target_label}")
    if len(ref_idxs) < 2:
        raise GapFillError("Rigid body fill needs at least 2 valid reference markers.")

    all_idxs = [target_idx] + ref_idxs
    cluster = marker_points[:, all_idxs, :]
    present = ~np.isnan(cluster).any(axis=0)  # (1 + n_refs, n_frames)
    ref_mask = present.all(axis=0)
    if ref_mask.sum() < min_ref_frames:
        raise GapFillError(
            f"Only {int(ref_mask.sum())} frame(s) have the target marker and every reference marker "
            f"simultaneously visible -- need at least {min_ref_frames} to build a reliable rigid template. "
            "Try different/more reference markers."
        )

    template = _average_rigid_template(cluster[:, :, ref_mask])
    filled = marker_points.copy()
    for f in range(start, end + 1):
        present_f = present[:, f]
        if present_f[0]:
            continue  # target already present this frame -- nothing to fill
        if present_f[1:].sum() < 2:
            continue  # not enough references visible this exact frame -- leave as gap
        reconstructed = _solve_rigid_and_reconstruct(template, cluster[:, :, f], present_f)
        if reconstructed is not None:
            filled[:, target_idx, f] = reconstructed[:, 0]
    return filled


def _estimate_period(residual, valid_mask, min_period, max_period):
    """Autocorrelation-based period estimate (in frames) from the longest
    contiguous valid run in residual. Returns None if no reliable peak is
    found (too little data, or the signal isn't clearly periodic)."""
    idx = np.where(valid_mask)[0]
    if idx.size == 0:
        return None
    runs = []
    run_start = prev = idx[0]
    for x in idx[1:]:
        if x != prev + 1:
            runs.append((run_start, prev))
            run_start = x
        prev = x
    runs.append((run_start, prev))
    run_start, run_end = max(runs, key=lambda r: r[1] - r[0])

    seg = residual[run_start:run_end + 1]
    if seg.size < min_period * 2:
        return None
    seg = seg - seg.mean()
    autocorr = np.correlate(seg, seg, mode="full")[seg.size - 1:]
    if autocorr[0] == 0:
        return None
    autocorr = autocorr / autocorr[0]

    hi = min(max_period, len(autocorr) - 1)
    if hi <= min_period:
        return None
    search = autocorr[min_period:hi]
    peak_offset = int(np.argmax(search))
    peak = peak_offset + min_period
    if autocorr[peak] < 0.3:  # too weak a periodic signal to trust
        return None
    return peak


def cyclic_fill(marker_points, marker_idx, start, end, min_period=5, max_period=200):
    """Return a new marker_points with marker_idx's [start, end] gap filled
    using the marker's own dominant motion cycle: fits a linear trend per
    axis (captures steady translation, e.g. walking forward), estimates the
    cycle period from the detrended residual via autocorrelation, then
    reconstructs each missing frame from the nearest valid frame(s) exactly
    one period away. Best for highly periodic movement (gait, cycling) --
    raises GapFillError if no reliable period can be estimated, or no frame
    one period away from a given gap frame is ever valid."""
    series = marker_points[:, marker_idx, :]
    valid = ~np.isnan(series).any(axis=0)
    n_frames = series.shape[1]
    gap_frames = np.arange(start, end + 1)
    valid_idx = np.where(valid)[0]
    if valid_idx.size < min_period * 2:
        raise GapFillError("Not enough valid data to estimate a motion cycle for cyclic fill.")

    filled = marker_points.copy()
    for axis in range(3):
        y = series[axis, :]
        trend_coeffs = np.polyfit(valid_idx, y[valid_idx], 1)
        trend = np.polyval(trend_coeffs, np.arange(n_frames))
        residual = y - trend

        period = _estimate_period(residual, valid, min_period, max_period)
        if period is None:
            raise GapFillError(
                "Could not find a reliable periodic pattern in this marker's trajectory -- "
                "cyclic fill needs clearly repetitive motion (e.g. gait)."
            )

        for f in gap_frames:
            candidates = []
            for k in range(1, n_frames // period + 1):
                for cand in (f - k * period, f + k * period):
                    if 0 <= cand < n_frames and valid[cand]:
                        candidates.append(residual[cand])
                if candidates:
                    break
            if not candidates:
                raise GapFillError(f"No valid frame found one cycle away from frame {f} for cyclic fill.")
            filled[axis, marker_idx, f] = trend[f] + np.mean(candidates)

    return filled


def linear_fill(marker_points, marker_idx, start, end):
    """Return a new marker_points with marker_idx's [start, end] gap filled
    by a straight line between the frame just before and just after the
    gap. Simplest and fastest method, but introduces a sharp direction
    change at the gap edges for anything but near-linear motion -- best
    reserved for very short gaps. Raises GapFillError if the gap touches the
    start/end of the trial or the immediately-adjacent frames aren't valid."""
    series = marker_points[:, marker_idx, :]
    n_frames = series.shape[1]
    if start == 0 or end >= n_frames - 1:
        raise GapFillError(
            "Linear fill needs one valid frame on both sides of the gap; "
            "this gap touches the start/end of the trial."
        )
    before, after = series[:, start - 1], series[:, end + 1]
    if np.isnan(before).any() or np.isnan(after).any():
        raise GapFillError("Linear fill needs the immediately-adjacent frames to be valid.")

    filled = marker_points.copy()
    gap_len = end - start + 1
    for axis in range(3):
        filled[axis, marker_idx, start:end + 1] = np.linspace(before[axis], after[axis], gap_len + 2)[1:-1]
    return filled


def rename_marker(marker_data, old_name, new_name):
    """Rename a marker in-place on marker_data -- updates both
    marker_data.labels (used throughout this module/UI) and the underlying
    c3d_obj's POINT:LABELS parameter, so the new name is what actually gets
    written by a later export_c3d() rather than being a session-only
    relabel that's silently lost on export. Raises GapFillError if old_name
    doesn't exist, new_name is empty, or new_name collides with a different
    existing marker."""
    new_name = new_name.strip()
    if not new_name:
        raise GapFillError("Marker name cannot be empty.")
    if old_name not in marker_data.labels:
        raise GapFillError(f"Unknown marker: {old_name}")
    if new_name != old_name and new_name in marker_data.labels:
        raise GapFillError(f"A marker named '{new_name}' already exists.")

    idx = marker_data.labels.index(old_name)
    marker_data.labels[idx] = new_name
    global_idx = marker_data.marker_indices[idx]
    marker_data.c3d_obj["parameters"]["POINT"]["LABELS"]["value"][global_idx] = new_name


def export_c3d(marker_data, marker_points, output_path):
    """Write marker_points back into marker_data's original ezc3d object
    (preserving analog data, events, and every other parameter untouched)
    and save as a new file at output_path. The original file on disk is
    never written to.

    A C3D point's validity is controlled by data.meta_points.residuals
    (negative = invalid/gap), a separate array from data.points itself --
    confirmed against a real file where the points array's own 4th
    ("residual") row is a static placeholder (always 1.0) and does NOT
    reflect gaps. Writing new coordinates alone is not enough: frames that
    were gap-filled here must also have their residual flipped to a
    non-negative value (0.0 -- "valid, no distortion info", the usual value
    for computed/interpolated points) or ezc3d's writer re-serializes them
    as invalid regardless of the coordinate values just written. Frames that
    are still unfilled (still NaN) keep their original negative residual.
    """
    points = marker_data.c3d_obj["data"]["points"]
    residuals = marker_data.c3d_obj["data"]["meta_points"]["residuals"]
    for local_i, global_i in enumerate(marker_data.marker_indices):
        points[:3, global_i, :] = marker_points[:, local_i, :]
        now_valid = ~np.isnan(marker_points[:, local_i, :]).any(axis=0)
        was_invalid = residuals[0, global_i, :] < 0
        residuals[0, global_i, now_valid & was_invalid] = 0.0
    try:
        marker_data.c3d_obj.write(output_path)
    except Exception as e:
        raise GapFillError(f"Failed to write C3D file: {output_path}. {e}") from e
    return output_path
