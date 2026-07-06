"""
modules/pyMotion/core/batch_dataset.py — shared batch-analysis data model.

BatchDataset is the common structure that Advanced EMG Analysis (time
normalization, co-contraction, wavelet, muscle synergy, SPM) operates on,
regardless of whether the data came from a Myotion workspace or from an
externally-prepared emg/+cycles/ folder pair (see batch_io.py).
"""

from typing import Optional

from .timeSeriesTable import timeSeriesTable

# Group every trial is filed under unless a loader explicitly assigns one
# (see batch_io.load_external_folder's `group` argument). Keeps single-group
# callers (from_workspace, a lone Load Folder Pair) working exactly as before
# -- they just get one implicit group -- while multi-group loaders can tag
# trials for group-aware analyses (within/cross-group cosine similarity, SPM).
DEFAULT_GROUP = "Ungrouped"


class BatchTrial:
    """One trial's EMG plus the cycle/rep boundaries used for time-normalization."""

    def __init__(self, name, emg: timeSeriesTable, cycles, group: str = DEFAULT_GROUP):
        self.name = name
        self.emg = emg
        self.cycles = list(cycles)  # list of (t_start_s, t_end_s) tuples
        self.group = group


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
    return BatchTrial(trial.name, trial.emg, subset, group=trial.group)


class BatchDataset:
    """A named collection of BatchTrial, sharing one cycle-normalization convention.

    cycle_mode:
        "discrete"   -> each cycle is an explicit (t_start, t_end) pair with
                        possible rest gaps between cycles (e.g. Sit2Stand reps).
                        Uses timeSeriesTable.timeNormalizeCycle(s).
        "continuous" -> not yet implemented. Reserved for gap-free, chained
                        gait-style cycles (heel-strike -> toe-off -> next
                        heel-strike) with no rest between them.

    Trials are also indexed by `.group` (see BatchTrial), so a dataset can
    hold an arbitrary number of comparison groups (e.g. Control vs. LBP) --
    there is no cap on group count, same as there's no cap on trial count.
    `self.groups` preserves insertion order, so group pickers in the UI list
    groups in the order they were loaded.
    """

    def __init__(self, cycle_mode="discrete"):
        if cycle_mode not in ("discrete", "continuous"):
            raise ValueError("cycle_mode must be 'discrete' or 'continuous'")
        self.cycle_mode = cycle_mode
        self.trials = {}
        self.groups: dict[str, list[str]] = {}  # group_name -> [trial_name, ...]

    def add(self, trial: BatchTrial):
        old = self.trials.get(trial.name)
        if old is not None and old.group != trial.group:
            self._remove_from_group(old.group, trial.name)
        self.trials[trial.name] = trial
        bucket = self.groups.setdefault(trial.group, [])
        if trial.name not in bucket:
            bucket.append(trial.name)

    def _remove_from_group(self, group_name, trial_name):
        bucket = self.groups.get(group_name)
        if not bucket:
            return
        if trial_name in bucket:
            bucket.remove(trial_name)
        if not bucket:
            del self.groups[group_name]

    def trials_in(self, group_name):
        """BatchTrial list for one group, in load order."""
        return [self.trials[n] for n in self.groups.get(group_name, [])]

    def __getitem__(self, name):
        return self.trials[name]

    def __iter__(self):
        return iter(self.trials.values())

    def __len__(self):
        return len(self.trials)

    @property
    def names(self):
        return list(self.trials.keys())

    @property
    def group_names(self):
        return list(self.groups.keys())
