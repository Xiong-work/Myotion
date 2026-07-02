"""
modules/pyMotion/core/batch_io.py — adapters that build a BatchDataset.

Two entry points feed the same BatchDataset:
  - load_external_folder(): an emg/+cycles/ folder pair prepared outside
    Myotion. Folder/file convention matches musclesynergies_py's
    read_data() (matching base filenames, cycles file has no header,
    emg file has a header row with Time as the first column).
  - from_workspace(): participants already loaded/processed in a Myotion
    workspace, using each participant's crop_interval as the single cycle
    unless an explicit per-participant cycle list is supplied.
"""

from pathlib import Path

import numpy as np
import pandas as pd

from .batch_dataset import BatchTrial, BatchDataset
from .timeSeriesTable import timeSeriesTable


def load_external_folder(
    path_cycles,
    path_emg,
    cycle_mode="discrete",
    header_cycles=False,
    header_emg=True,
):
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

    dataset = BatchDataset(cycle_mode=cycle_mode)
    for name in sorted(cycle_map):
        cycles_df = pd.read_csv(cycle_map[name], sep=sep, header=0 if header_cycles else None)
        if cycles_df.shape[1] != 2:
            raise ValueError(
                f"'{name}': discrete cycle_mode expects exactly 2 columns "
                f"(start, end), got {cycles_df.shape[1]}"
            )
        cycles = list(
            zip(cycles_df.iloc[:, 0].astype(float), cycles_df.iloc[:, 1].astype(float))
        )

        emg_df = pd.read_csv(emg_map[name], sep=sep, header=0 if header_emg else None)
        time = emg_df.iloc[:, 0].to_numpy(dtype=float)
        fs = round(1.0 / float(np.mean(np.diff(time))))
        labels = [str(c) for c in emg_df.columns[1:]]
        data = {lbl: emg_df[col].to_numpy(dtype=float) for lbl, col in zip(labels, emg_df.columns[1:])}
        tst = timeSeriesTable(fs, labels, data)

        dataset.add(BatchTrial(name, tst, cycles))

    return dataset


def from_workspace(ws, participants, cycles_by_name=None):
    """Build a BatchDataset from participants already loaded in a Myotion workspace.

    cycles_by_name: optional {participant_name: [(t0, t1), ...]} override.
    When omitted, each participant's own crop_interval is used as a single
    whole-trial cycle. Deriving multiple discrete-rep cycles automatically
    from kinematics/user events is not implemented yet -- pass
    cycles_by_name explicitly for that case.
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
        elif profile.crop_interval is not None:
            cycles = [tuple(profile.crop_interval)]
        else:
            raise ValueError(
                f"participant '{person.name}' has no crop_interval and no "
                "explicit cycles provided -- cannot define a cycle boundary"
            )

        dataset.add(BatchTrial(person.name, sub_tst, cycles))

    return dataset
