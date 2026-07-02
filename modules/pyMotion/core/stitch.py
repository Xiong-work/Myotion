"""Merge a kinematics/force-plate .c3d with a separately-recorded EMG source
(.c3d or .mat) into a single new .c3d, for hardware-synced trials that were
saved to two files instead of one.

The kinematics file is always the trial's time reference (frame 0 = t=0).
`offset_s` is the kinematics-clock timestamp at which the EMG source's own
sample index 0 was recorded:
  - two c3d files sharing a hardware start/stop trigger -> 0.0 (default)
  - a Noraxon .mat export -> its own `begin_time` field, which is already
    expressed relative to the same trigger

Original files are never modified; a new file is written alongside the
kinematics file.
"""
import os

import c3d
import numpy as np

from .mat import matFile
from .logger import *


class StitchError(Exception):
    """Raised when two trials cannot be merged (incompatible rates, missing
    channels, no overlap, etc). Never raised for things a resample can fix."""
    pass


class EmgSource:
    __slots__ = ("labels", "rate", "arrays", "default_offset_s", "source_path")

    def __init__(self, labels, rate, arrays, default_offset_s, source_path):
        self.labels = labels
        self.rate = rate
        self.arrays = arrays  # label -> 1D float32 ndarray, native sample rate
        self.default_offset_s = default_offset_s
        self.source_path = source_path


def _load_emg_from_c3d(path):
    with open(path, "rb") as fh:
        reader = c3d.Reader(fh)
        labels = [l.strip() for l in reader.analog_labels]
        rate = float(reader.analog_rate)
        if not labels or rate <= 0:
            raise StitchError("'{}' has no analog channels to use as an EMG source.".format(path))

        chunks = {l: [] for l in labels}
        for _frame_no, _points, analog in reader.read_frames(copy=False):
            for i, label in enumerate(labels):
                chunks[label].append(np.asarray(analog[i], dtype=np.float32).copy())
        arrays = {l: np.concatenate(v) for l, v in chunks.items()}

    return EmgSource(labels=labels, rate=rate, arrays=arrays, default_offset_s=0.0, source_path=path)


def _load_emg_from_mat(path):
    mat = matFile(path)
    channels = mat.movements["channels"]
    if not channels:
        raise StitchError("'{}' has no channels to use as an EMG source.".format(path))

    labels = [ch.name for ch in channels]
    rate = float(channels[0].frequency)
    arrays = {ch.name: np.asarray(ch.data, dtype=np.float32) for ch in channels}

    begin_times = [float(ch.begin_time) for ch in channels if ch.begin_time is not None]
    default_offset_s = begin_times[0] if begin_times else 0.0

    return EmgSource(labels=labels, rate=rate, arrays=arrays,
                      default_offset_s=default_offset_s, source_path=path)


def load_emg_source(emg_path):
    ext = os.path.splitext(emg_path)[1].lower()
    if ext == ".c3d":
        return _load_emg_from_c3d(emg_path)
    if ext == ".mat":
        return _load_emg_from_mat(emg_path)
    raise StitchError("Unsupported EMG source format: '{}'. Expected .c3d or .mat.".format(ext))


def _pack_labels_utf8(labels):
    """Byte-accurate label packing for a C3D string-array parameter (see the
    long comment where this is used in stitch_c3d for why this exists instead
    of c3d.Writer.pack_labels()). Returns (bytes_blob, max_label_bytes) with
    len(bytes_blob) == max_label_bytes * len(labels) exactly."""
    encoded = [l.encode("utf-8") for l in labels]
    max_len = max((len(e) for e in encoded), default=0)
    padded = b"".join(e.ljust(max_len, b" ") for e in encoded)
    return padded, max_len


def _pick_target_rate(point_rate, kin_analog_rate, emg_rate, kin_path, emg_path):
    """Pick the merged file's single analog rate: prefer the higher of the two
    native rates (EMG fidelity matters more than force-plate fidelity), as
    long as it is an integer multiple of the point rate. Falls back to the
    lower rate if that's the only one that divides evenly."""
    candidates = [r for r in (max(kin_analog_rate, emg_rate), min(kin_analog_rate, emg_rate)) if r > 0]
    for rate in candidates:
        if abs(rate % point_rate) < 1e-6:
            return rate
    raise StitchError(
        "Cannot merge '{}' (point rate {} Hz, force-plate analog {} Hz) with '{}' (EMG rate {} Hz): "
        "no common analog sample rate is an integer multiple of the point rate.".format(
            os.path.basename(kin_path), point_rate, kin_analog_rate,
            os.path.basename(emg_path), emg_rate,
        )
    )


def _resample_channels(data, src_rate, dst_n_samples, dst_rate, src_time0_s):
    """Resample each channel in `data` (dict label -> 1D array, native src_rate,
    where index 0 was recorded at kinematics-clock time `src_time0_s`) onto a
    uniform grid of `dst_n_samples` samples at `dst_rate`, starting at
    kinematics t=0. Queries outside the source's recorded span are clamped to
    the nearest edge sample (np.interp default)."""
    dst_times = np.arange(dst_n_samples, dtype=np.float64) / dst_rate
    out = {}
    for label, arr in data.items():
        arr = np.asarray(arr, dtype=np.float64)
        src_times_local = np.arange(len(arr), dtype=np.float64) / src_rate
        query_local_times = dst_times - src_time0_s
        out[label] = np.interp(query_local_times, src_times_local, arr).astype(np.float32)
    return out


def check_alignment(kin_path, emg_path, tolerance_s=None):
    """Return (offset_s, is_trusted, message) without writing anything.

    is_trusted=True means the offset was derived from a reliable anchor
    (matching recording durations for two c3d files sharing a hardware
    trigger, or a MAT begin_time whose window fully covers the kinematics
    trial) and stitch_c3d() can be called with it directly. is_trusted=False
    means it's only a starting guess for a manual offset viewer.
    """
    with open(kin_path, "rb") as fh:
        kin_reader = c3d.Reader(fh)
        point_rate = float(kin_reader.point_rate)
        kin_duration = kin_reader.frame_count / point_rate if point_rate > 0 else 0.0

    frame_period = 1.0 / point_rate if point_rate > 0 else 0.0
    tol = tolerance_s if tolerance_s is not None else frame_period

    emg_src = load_emg_source(emg_path)
    emg_duration = len(next(iter(emg_src.arrays.values()))) / emg_src.rate if emg_src.arrays else 0.0

    if os.path.splitext(emg_path)[1].lower() == ".mat":
        offset_s = emg_src.default_offset_s
        window_start = 0.0 - offset_s
        window_end = kin_duration - offset_s
        fits = window_start >= -1e-6 and window_end <= emg_duration + 1e-6
        if fits:
            return offset_s, True, "Using recorded begin_time offset from the MAT file ({:.4f}s).".format(offset_s)
        return offset_s, False, (
            "MAT begin_time ({:.4f}s) does not fully cover the kinematics trial ({:.3f}s); "
            "confirm alignment manually.".format(offset_s, kin_duration)
        )

    # Two c3d files: assume a shared hardware trigger (offset 0) if durations match closely.
    if abs(kin_duration - emg_duration) <= tol:
        return 0.0, True, "Durations match ({:.3f}s vs {:.3f}s); assuming a shared start trigger.".format(
            kin_duration, emg_duration)
    return 0.0, False, (
        "Durations differ ({:.3f}s vs {:.3f}s) by more than one point-frame period; "
        "confirm alignment manually.".format(kin_duration, emg_duration)
    )


def _default_out_path(kin_path):
    base = os.path.splitext(kin_path)[0]
    candidate = base + "_stitched.c3d"
    i = 1
    while os.path.exists(candidate):
        candidate = "{}_stitched_{}.c3d".format(base, i)
        i += 1
    return candidate


def stitch_c3d(kin_path, emg_path, out_path=None, offset_s=None):
    """Merge kin_path (markers + force plates) with emg_path (.c3d or .mat)
    into a new .c3d written to out_path (default: next to kin_path).

    offset_s: kinematics-clock timestamp of the EMG source's sample 0.
    Defaults to the source's own trusted anchor (0.0 for a c3d EMG file,
    `begin_time` for a mat EMG file) when not given explicitly.

    Returns the path written. Raises StitchError on incompatible inputs.
    Never modifies kin_path or emg_path.
    """
    if not os.path.isfile(kin_path):
        raise StitchError("Kinematics file not found: {}".format(kin_path))
    if not os.path.isfile(emg_path):
        raise StitchError("EMG file not found: {}".format(emg_path))

    emg_src = load_emg_source(emg_path)
    if offset_s is None:
        offset_s = emg_src.default_offset_s

    kin_fh = open(kin_path, "rb")
    try:
        kin_reader = c3d.Reader(kin_fh)
        point_rate = float(kin_reader.point_rate)
        if point_rate <= 0:
            raise StitchError("'{}' has no valid point rate; it cannot anchor a stitched trial.".format(kin_path))

        kin_analog_labels = [l.strip() for l in kin_reader.analog_labels]
        kin_analog_rate = float(kin_reader.analog_rate) if kin_analog_labels else 0.0

        overlap = set(kin_analog_labels) & set(emg_src.labels)
        if overlap:
            raise StitchError(
                "Channel name collision between kinematics and EMG sources: {}. "
                "Rename channels before stitching.".format(sorted(overlap))
            )

        target_rate = _pick_target_rate(point_rate, kin_analog_rate, emg_src.rate, kin_path, emg_path)

        # 'copy_metadata' deep-copies the header + ALL parameter groups (FORCE_PLATFORM,
        # EVENT, MANUFACTURER, TRIAL, POINT descriptions, ...) without frames, so nothing
        # besides the analog block needs to be rebuilt by hand below. Frames are read and
        # assigned to writer._frames directly (see below) rather than via Writer.add_frames()/
        # Writer.from_reader(..., 'copy'), which hit a numpy ragged-array error on this
        # numpy/python-c3d combination when point and analog arrays differ in row count.
        writer = c3d.Writer.from_reader(kin_reader, conversion="copy_metadata")
        kin_frames = list(kin_reader.read_frames(copy=True))  # (frame_no, points, analog)
    finally:
        kin_fh.close()

    kin_frame_count = len(kin_frames)
    if kin_frame_count == 0:
        raise StitchError("'{}' has no frames to stitch.".format(kin_path))

    analog_per_frame_target = int(round(target_rate / point_rate))
    n_target_samples = kin_frame_count * analog_per_frame_target

    kin_analog_dict = {}
    if kin_analog_labels:
        kin_analog_stack = np.concatenate([analog for (_fn, _pts, analog) in kin_frames], axis=1)
        kin_analog_dict = {label: kin_analog_stack[i] for i, label in enumerate(kin_analog_labels)}
    kin_resampled = _resample_channels(kin_analog_dict, kin_analog_rate, n_target_samples, target_rate, 0.0)
    emg_resampled = _resample_channels(emg_src.arrays, emg_src.rate, n_target_samples, target_rate, offset_s)

    merged_labels = kin_analog_labels + emg_src.labels
    merged_stack = np.stack(
        [kin_resampled[l] for l in kin_analog_labels] + [emg_resampled[l] for l in emg_src.labels],
        axis=0,
    ).astype(np.float32)

    new_frames = []
    for i, (_fn, points, _old_analog) in enumerate(kin_frames):
        start = i * analog_per_frame_target
        end = start + analog_per_frame_target
        new_frames.append((points, merged_stack[:, start:end]))
    writer._frames = new_frames

    n_total_channels = len(merged_labels)
    # Writer.set_analog_labels()/pack_labels() pad by character count, then
    # Param.binary_size() (which determines the on-disk "next parameter"
    # jump offset) sizes the block from that character count too. Any label
    # with multi-byte UTF-8 characters (e.g. Chinese channel names from a
    # bilingual EMG export) then has more encoded bytes than the declared
    # block size, so the offset undershoots and every parameter written
    # after LABELS becomes unreadable. Pack by encoded byte length instead
    # so the declared size always matches len(bytes) exactly. Also use
    # set() (overwrite), not set_analog_labels()'s add_str() (throws if
    # ANALOG:LABELS already exists, which it does — copy_metadata copied it
    # from the kinematics file).
    label_bytes, label_max_bytes = _pack_labels_utf8(merged_labels)
    writer.analog_group.set("LABELS", "Analog labels.", -1, None, label_bytes,
                             label_max_bytes, len(merged_labels))
    writer.set_analog_general_scale(1.0)
    writer.set_analog_scales([1.0] * n_total_channels)
    writer.set_analog_offsets([0] * n_total_channels)
    writer.analog_rate = target_rate  # validates divisibility, updates header.analog_per_frame
    writer.analog_group.set("RATE", "Analog sample rate", 4, "<f", float(target_rate))
    writer.analog_group.set("USED", "Number of analog channels", 2, "<H", n_total_channels)

    out_path = out_path or _default_out_path(kin_path)
    with open(out_path, "wb") as out_fh:
        writer.write(out_fh)

    logger.info(
        "stitch_c3d: wrote {} ({} analog channels: {} kinematics + {} EMG, {} Hz, offset {:.4f}s)".format(
            out_path, n_total_channels, len(kin_analog_labels), len(emg_src.labels), target_rate, offset_s
        )
    )
    return out_path
