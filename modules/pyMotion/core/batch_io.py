"""
modules/pyMotion/core/batch_io.py — adapters that build a BatchDataset.

Entry points feed the same BatchDataset:
  - load_external_folder(): an emg/+cycles/ folder pair prepared outside
    Myotion. Folder/file convention matches musclesynergies_py's
    read_data() (matching base filenames, cycles file has no header,
    emg file has a header row with Time as the first column).
  - load_external_groups(): a parent folder containing one emg/+cycles/
    folder pair per comparison group (e.g. Adv_Analyses/Control/{emg,cycles},
    Adv_Analyses/LBP/{emg,cycles}) -- one call loads every group it finds.
  - from_workspace(): participants already loaded/processed in a Myotion
    workspace, using each participant's crop_interval as the single cycle
    unless an explicit per-participant cycle list is supplied.
"""

from pathlib import Path

import numpy as np
import pandas as pd

from .batch_dataset import BatchTrial, BatchDataset, DEFAULT_GROUP
from .timeSeriesTable import timeSeriesTable
from .cycle_detection import _cycles_from_events


def load_external_folder(
    path_cycles,
    path_emg,
    cycle_mode="discrete",
    header_cycles=False,
    header_emg=True,
    group=None,
    dataset=None,
):
    """Load one emg/+cycles/ folder pair (one comparison group's worth of
    participants) into a BatchDataset.

    group: label to file these trials under (see BatchDataset.groups). Trial
        names are prefixed "group/participant" when a group is given, so
        participant IDs that repeat across groups (e.g. both folders having
        a "01") don't collide once merged into one dataset; left unprefixed
        (and filed under DEFAULT_GROUP) when group is None, matching this
        function's original single-group behavior.
    dataset: an existing BatchDataset to append into (for loading several
        groups' folder pairs into one dataset one call at a time), or None
        to create a fresh one.
    """
    if cycle_mode == "continuous":
        raise NotImplementedError(
            "continuous (multi-phase, gap-free gait cycle) folders are not "
            "supported yet; only discrete-repetition (start, end) cycle "
            "files are implemented"
        )

    path_cycles = Path(path_cycles)
    path_emg = Path(path_emg)

    cycle_files = sorted(f for f in path_cycles.iterdir() if f.is_file())
    emg_files = sorted(f for f in path_emg.iterdir() if f.is_file())

    all_exts = {f.suffix.lower() for f in cycle_files} | {f.suffix.lower() for f in emg_files}
    if len(all_exts) != 1:
        raise ValueError(f"all files must share the same extension, found: {all_exts}")
    ext = next(iter(all_exts))
    if ext not in {".txt", ".csv"}:
        raise ValueError(f"unsupported file type '{ext}', use .txt or .csv")
    sep = "\t" if ext == ".txt" else ","

    cycle_map = {f.stem: f for f in cycle_files}
    emg_map = {f.stem: f for f in emg_files}

    missing_in_emg = set(cycle_map) - set(emg_map)
    missing_in_cycles = set(emg_map) - set(cycle_map)
    if missing_in_emg or missing_in_cycles:
        raise ValueError(
            "file name mismatch between folders.\n"
            f"  in cycles but not emg: {missing_in_emg}\n"
            f"  in emg but not cycles: {missing_in_cycles}"
        )

    if dataset is None:
        dataset = BatchDataset(cycle_mode=cycle_mode)
    elif dataset.cycle_mode != cycle_mode:
        raise ValueError(
            f"dataset is '{dataset.cycle_mode}' but this folder pair is '{cycle_mode}'"
        )
    group_name = group or DEFAULT_GROUP

    for participant in sorted(cycle_map):
        cycles_df = pd.read_csv(cycle_map[participant], sep=sep, header=0 if header_cycles else None)
        if cycles_df.shape[1] != 2:
            raise ValueError(
                f"'{participant}': discrete cycle_mode expects exactly 2 columns "
                f"(start, end), got {cycles_df.shape[1]}"
            )
        cycles = list(
            zip(cycles_df.iloc[:, 0].astype(float), cycles_df.iloc[:, 1].astype(float))
        )

        emg_df = pd.read_csv(emg_map[participant], sep=sep, header=0 if header_emg else None)
        time = emg_df.iloc[:, 0].to_numpy(dtype=float)
        fs = round(1.0 / float(np.mean(np.diff(time))))
        labels = [str(c) for c in emg_df.columns[1:]]
        data = {lbl: emg_df[col].to_numpy(dtype=float) for lbl, col in zip(labels, emg_df.columns[1:])}
        tst = timeSeriesTable(fs, labels, data)

        name = f"{group_name}/{participant}" if group else participant
        dataset.add(BatchTrial(name, tst, cycles, group=group_name))

    return dataset


def load_external_groups(
    parent_folder,
    cycle_mode="discrete",
    header_cycles=False,
    header_emg=True,
):
    """Load every group found directly under `parent_folder` into one
    BatchDataset -- one group per immediate subfolder that itself contains
    an emg/ and a cycles/ subfolder (matches this project's sample layout,
    e.g. Adv_Analyses/Control/{emg,cycles}, Adv_Analyses/LBP/{emg,cycles}).

    No cap on how many group subfolders are found -- each becomes one more
    entry in the returned dataset's `.groups`.
    """
    parent_folder = Path(parent_folder)
    group_dirs = sorted(
        d for d in parent_folder.iterdir()
        if d.is_dir() and (d / "emg").is_dir() and (d / "cycles").is_dir()
    )
    if not group_dirs:
        raise ValueError(
            f"no group subfolders with both emg/ and cycles/ found under '{parent_folder}'"
        )

    dataset = BatchDataset(cycle_mode=cycle_mode)
    for group_dir in group_dirs:
        load_external_folder(
            group_dir / "cycles", group_dir / "emg",
            cycle_mode=cycle_mode, header_cycles=header_cycles, header_emg=header_emg,
            group=group_dir.name, dataset=dataset,
        )
    return dataset


def from_workspace(ws, participants, cycles_by_name=None, task_type=None):
    """Build a BatchDataset from participants already loaded in a Myotion workspace.

    cycles_by_name: optional {participant_name: [(t0, t1), ...]} override,
        checked first for each participant.
    task_type: which task's detected cycles to use when a participant has
        Cycle* events for more than one task (see kinematics workflow's
        Detect Cycles button); passed through to _cycles_from_events().

    Per participant, in order: cycles_by_name override, then CycleStart_/
    CycleEnd_ events written by the kinematics workflow's task-type cycle
    detector (modules/kinematics/controller.py), then crop_interval as a
    single whole-trial cycle.
    """
    dataset = BatchDataset(cycle_mode="discrete")
    for person in participants:
        profile = ws[person]
        emg_obj = profile.emg
        tst = emg_obj.emgTST

        enabled = [c for c in emg_obj.Channels if c in emg_obj.enabledChannels and tst.hasChannel(c)]
        if not enabled:
            raise ValueError(f"participant '{person.name}' has no enabled channels")

        sub_tst = timeSeriesTable(tst.fs, enabled, {c: tst[c] for c in enabled})

        if cycles_by_name is not None and person.name in cycles_by_name:
            cycles = list(cycles_by_name[person.name])
        else:
            cycles = _cycles_from_events(getattr(profile, "extra_events", []), task_type)
            if not cycles and profile.crop_interval is not None:
                cycles = [tuple(profile.crop_interval)]
            if not cycles:
                raise ValueError(
                    f"participant '{person.name}' has no crop_interval, no "
                    "detected cycles, and no explicit cycles provided -- "
                    "cannot define a cycle boundary"
                )

        dataset.add(BatchTrial(person.name, sub_tst, cycles))

    return dataset
