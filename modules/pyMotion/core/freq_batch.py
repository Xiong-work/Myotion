"""modules/pyMotion/core/freq_batch.py -- pure helpers for turning a
trial's TrialEvents into a sequence of frequency-analysis segments, used by
the Frequency Analysis page's "Apply Events" batch workflow (see main.py).
No Qt/UI/EMG-specific code here, so this stays independently testable.
"""


def event_segments(events, crop_interval=None):
    """Consecutive-event segments: sorts *events* by time_s and pairs each
    one with the next (events[i] -> events[i+1]), one segment per pair.
    Leading time before the first event and trailing time after the last
    are not included -- an event marks a real, user-placed boundary, so
    only the space between two of them is treated as a defined segment.

    crop_interval, if given as (t0, t1), clips every segment to that
    window; a segment that ends up empty or reversed after clipping is
    dropped rather than producing a degenerate/negative-duration row.

    Returns [{"t0": float, "t1": float, "label": str}, ...] sorted by t0.
    "label" combines both events' own labels for traceability back to the
    source, e.g. "HeelStrike#1 -> ToeOff#1".
    """
    ordered = sorted(events, key=lambda ev: ev.time_s)
    segments = []
    for a, b in zip(ordered, ordered[1:]):
        t0, t1 = a.time_s, b.time_s
        if crop_interval is not None:
            c0, c1 = crop_interval
            t0, t1 = max(t0, c0), min(t1, c1)
        if t1 <= t0:
            continue
        segments.append({"t0": t0, "t1": t1, "label": "{} -> {}".format(a.label, b.label)})
    return segments
