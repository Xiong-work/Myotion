"""
modules/pyMotion/core/batch_dataset.py — shared batch-analysis data model.

BatchDataset is the common structure that Advanced EMG Analysis (time
normalization, co-contraction, wavelet, muscle synergy, SPM) operates on,
regardless of whether the data came from a Myotion workspace or from an
externally-prepared emg/+cycles/ folder pair (see batch_io.py).
"""

from typing import Optional

from .timeSeriesTable import timeSeriesTable


class BatchTrial:
    """One trial's EMG plus the cycle/rep boundaries used for time-normalization."""

    def __init__(self, name, emg: timeSeriesTable, cycles):
        self.name = name
        self.emg = emg
        self.cycles = list(cycles)  # list of (t_start_s, t_end_s) tuples


def subset_cycles(trial: BatchTrial, cy_start: int = 1, cy_max: Optional[int] = None) -> BatchTrial:
    """Restrict a trial to a range of its cycles/reps (R/subsetEMG.R's role).

    Ported against this codebase's representation rather than R's raw-EMG-
    row-index lookup: since BatchTrial already stores cycle boundaries
    separately from the full EMG array, "subsetting" is just slicing the
    cycles list -- the underlying EMG signal is shared, not copied.

    cy_start: 1-based index of the first cycle to include.
    cy_max: number of cycles to include from cy_start onward. None or <= 0
        means "all remaining cycles". Clipped to the trial's actual cycle
        count rather than raising, so one cy_start/cy_max pair can be
        applied uniformly across trials with different rep counts.
    """
    if cy_start < 1:
        raise ValueError("cy_start is 1-based and must be >= 1")
    start_idx = cy_start - 1
    if cy_max is None or cy_max <= 0:
        subset = trial.cycles[start_idx:]
    else:
        subset = trial.cycles[start_idx:start_idx + cy_max]
    return BatchTrial(trial.name, trial.emg, subset)


class BatchDataset:
    """A named collection of BatchTrial, sharing one cycle-normalization convention.

    cycle_mode:
        "discrete"   -> each cycle is an explicit (t_start, t_end) pair with
                        possible rest gaps between cycles (e.g. Sit2Stand reps).
                        Uses timeSeriesTable.timeNormalizeCycle(s).
        "continuous" -> not yet implemented. Reserved for gap-free, chained
                        gait-style cycles (heel-strike -> toe-off -> next
                        heel-strike) with no rest between them.
    """

    def __init__(self, cycle_mode="discrete"):
        if cycle_mode not in ("discrete", "continuous"):
            raise ValueError("cycle_mode must be 'discrete' or 'continuous'")
        self.cycle_mode = cycle_mode
        self.trials = {}

    def add(self, trial: BatchTrial):
        self.trials[trial.name] = trial

    def __getitem__(self, name):
        return self.trials[name]

    def __iter__(self):
        return iter(self.trials.values())

    def __len__(self):
        return len(self.trials)

    @property
    def names(self):
        return list(self.trials.keys())
