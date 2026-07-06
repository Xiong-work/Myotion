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

write_external_folder() is the inverse of load_external_folder()/
load_external_groups(): persists a BatchDataset (typically one just built by
from_workspace()) to disk in the same emg/+cycles/ convention.

prep_data_adv_from_workspace() is what Advanced EMG's "Prep Data" button
actually uses: it reads each participant's own already-exported files on
disk (<name>_emg_processed.csv, <name>_Events.csv) instead of the live
in-memory workspace (extra_events in a live session can be lost well before
Prep Data runs -- toggling Detect Cycles off clears that task's events,
reloading the workspace discards unsaved ones, etc. -- so the on-disk
exports are the durable source of truth, not from_workspace()'s live read).
"""

import csv
import io
import re
from pathlib import Path

import numpy as np
import pandas as pd

from .batch_dataset import BatchTrial, BatchDataset, DEFAULT_GROUP
from .timeSeriesTable import timeSeriesTable
from .cycle_detection import resolve_participant_cycles


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
        Detect Cycles button); passed through to resolve_participant_cycles().

    Per participant, in order: cycles_by_name override, then CycleStart_/
    CycleEnd_ events written by the kinematics workflow's task-type cycle
    detector (modules/kinematics/controller.py), then crop_interval as a
    single whole-trial cycle, then the full recording as a last-resort
    single cycle if no crop/cycles were ever set -- see
    resolve_participant_cycles().
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

        override = cycles_by_name.get(person.name) if cycles_by_name is not None else None
        cycles = resolve_participant_cycles(
            profile, task_type=task_type, override=override, participant_name=person.name,
            trial_duration=sub_tst.time,
        )

        dataset.add(BatchTrial(person.name, sub_tst, cycles))

    return dataset


def write_external_folder(dataset, dest_dir):
    """Write a BatchDataset out as an emg/+cycles/ folder pair, the on-disk
    mirror of load_external_folder() -- or one such pair per group, under
    dest_dir/<group>/{emg,cycles}, matching load_external_groups(), when the
    dataset holds more than one real comparison group.

    A dataset with exactly one group and that group being DEFAULT_GROUP
    (e.g. straight from from_workspace(), which doesn't assign groups) is
    written flat: dest_dir/emg, dest_dir/cycles -- no group subfolder.

    dest_dir is created if missing. Existing files with the same name are
    overwritten. Returns dest_dir (as a Path).
    """
    dest_dir = Path(dest_dir)
    group_names = dataset.group_names
    flat = len(group_names) == 1 and group_names[0] == DEFAULT_GROUP

    for group_name in group_names:
        group_dir = dest_dir if flat else dest_dir / group_name
        emg_dir = group_dir / "emg"
        cycles_dir = group_dir / "cycles"
        emg_dir.mkdir(parents=True, exist_ok=True)
        cycles_dir.mkdir(parents=True, exist_ok=True)

        for trial in dataset.trials_in(group_name):
            # Trial names are "group/participant" when the trial was loaded
            # under an explicit group (see load_external_folder's docstring);
            # the on-disk file itself is always just the participant's name.
            participant = trial.name.rsplit("/", 1)[-1]

            emg_df = trial.emg.toPandasFrame()
            emg_df.insert(0, "Time", trial.emg.getLinspace())
            emg_df.to_csv(emg_dir / f"{participant}.csv", index=False)

            with open(cycles_dir / f"{participant}.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                for t_start, t_end in trial.cycles:
                    w.writerow([t_start, t_end])

    return dest_dir


def _read_cycles_section(events_csv_path):
    """Parse the "# Cycles" section of a <name>_Events.csv file written by
    main.py's per-participant event export (Task, Cycle, Start (s), End (s)
    rows, grouped by task -- see MainWindow._exportParticipantEventsCore).

    Returns {task: [(start, end), ...]} (cycles sorted by cycle number), or
    {} if the file doesn't exist or has no "# Cycles" section.
    """
    path = Path(events_csv_path)
    if not path.is_file():
        return {}

    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "# Cycles")
    except StopIteration:
        return {}

    section = []
    for ln in lines[start + 1:]:
        if not ln.strip() or ln.lstrip().startswith("#"):
            break
        section.append(ln)
    if len(section) < 2:  # header + at least one data row
        return {}

    df = pd.read_csv(io.StringIO("\n".join(section)))
    by_task = {}
    for _, row in df.iterrows():
        by_task.setdefault(str(row["Task"]), {})[int(row["Cycle"])] = (
            float(row["Start (s)"]), float(row["End (s)"])
        )
    return {task: [pairs[n] for n in sorted(pairs)] for task, pairs in by_task.items()}


# Matches CycleStart_<task>[ #n] / CycleEnd_<task>[ #n] labels -- same
# convention as core.cycle_detection's _CYCLE_EVENT_RE, duplicated here since
# this parses CSV rows, not TrialEvent objects.
_CYCLE_LABEL_RE = re.compile(r'^Cycle(Start|End)_(.+?)(?:\s+#(\d+))?$')


def _parse_events_section_cycles(events_csv_path):
    """Fallback for _read_cycles_section(): recover cycle boundaries from the
    flat "# Events" section (Time (s), Label) instead of a "# Cycles"
    section, for <name>_Events.csv files exported before the Cycles section
    existed -- CycleStart_/CycleEnd_ rows are already in there, just not
    pre-paired, so there's no need to redo cycle detection/re-export just to
    pick this up.

    Returns {task: [(start, end), ...]}, same shape as _read_cycles_section.
    """
    path = Path(events_csv_path)
    if not path.is_file():
        return {}

    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "# Events")
    except StopIteration:
        return {}

    section = []
    for ln in lines[start + 1:]:
        if not ln.strip() or ln.lstrip().startswith("#"):
            break
        section.append(ln)
    if len(section) < 2:
        return {}

    df = pd.read_csv(io.StringIO("\n".join(section)))
    by_task = {}
    for _, row in df.iterrows():
        m = _CYCLE_LABEL_RE.match(str(row["Label"]))
        if not m:
            continue
        kind, task, num_txt = m.group(1).lower(), m.group(2), m.group(3)
        num = int(num_txt) if num_txt else 1
        by_task.setdefault(task, {}).setdefault(num, {})[kind] = round(float(row["Time (s)"]), 4)

    result = {}
    for task, reps in by_task.items():
        pairs = [
            (reps[n]["start"], reps[n]["end"])
            for n in sorted(reps)
            if "start" in reps[n] and "end" in reps[n]
        ]
        if pairs:
            result[task] = pairs
    return result


def prep_data_adv_from_workspace(ws, workspace_dir, dest_dirname="Data_Adv", groups_by_name=None):
    """Build <workspace_dir>/<dest_dirname>/{emg,cycles} for Advanced EMG's
    "Prep Data" workflow, reading each participant's own already-exported
    files on disk instead of the live workspace object:

      - <name>_emg_processed.csv (workspace.saveReport()) -> copied through
        (its "# ..." header comment lines stripped) as the emg/ file.
      - <name>_Events.csv's "# Cycles" section (main.py's Export Events /
        Export All Events), falling back to parsing CycleStart_/CycleEnd_
        rows directly out of the flat "# Events" section for exports
        written before the Cycles section existed (see
        _parse_events_section_cycles) -> the cycles/ file, one (start, end)
        row per cycle. Falls back further to the whole processed segment as
        a single cycle if no cycle events are found at all (mirrors
        workspace.saveReport()'s own "no crop set = whole trial is the
        analysis segment" convention).

    groups_by_name: optional {participant_name: group_label} for splitting
        participants into comparison groups (e.g. Control/LBP), as assigned
        via the Advanced EMG "Current Workspace" tab's per-participant group
        chips. Participants absent from this mapping (or the mapping itself
        being None/empty) are treated as ungrouped. If every participant
        ends up ungrouped, the output is flat: dest_dir/emg, dest_dir/cycles
        (matching load_external_folder's convention). Otherwise each group
        gets its own dest_dir/<group>/{emg,cycles} (matching
        load_external_groups' convention) -- ungrouped participants are
        filed under BatchDataset.DEFAULT_GROUP ("Ungrouped").

    Participants missing _emg_processed.csv (not yet batch-processed/saved)
    are skipped. Ambiguous Events.csv (cycles under more than one task) also
    falls back to the whole segment, since there's no task_type to
    disambiguate by here.

    Returns (dest_dir, warnings, grouped) -- warnings is a list of human-
    readable per-participant notes (skipped, or fell back to a whole-trial
    cycle) for the caller to report instead of silently hiding them; grouped
    is True if the output used per-group subfolders (so the caller knows to
    reload via load_external_groups() instead of load_external_folder()).
    """
    workspace_dir = Path(workspace_dir)
    dest_dir = workspace_dir / dest_dirname

    def group_for(name):
        return (groups_by_name or {}).get(name) or DEFAULT_GROUP

    distinct_groups = {group_for(p.name) for p in ws.participants}
    grouped = not (len(distinct_groups) == 1 and DEFAULT_GROUP in distinct_groups)

    warnings = []
    for person in ws.participants:
        group_dir = dest_dir if not grouped else dest_dir / group_for(person.name)
        emg_dir = group_dir / "emg"
        cycles_dir = group_dir / "cycles"
        emg_dir.mkdir(parents=True, exist_ok=True)
        cycles_dir.mkdir(parents=True, exist_ok=True)

        participant_dir = workspace_dir / person.name
        emg_csv = participant_dir / f"{person.name}_emg_processed.csv"
        if not emg_csv.is_file():
            warnings.append(
                f"'{person.name}': no {emg_csv.name} found -- "
                "batch-process/save a report for this participant first, skipped"
            )
            continue

        data_lines = [
            ln for ln in emg_csv.read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("#")
        ]
        if len(data_lines) < 3:  # header + at least 2 samples (for a time step)
            warnings.append(f"'{person.name}': {emg_csv.name} has too few data rows, skipped")
            continue
        (emg_dir / f"{person.name}.csv").write_text("\n".join(data_lines) + "\n", encoding="utf-8")

        events_csv = participant_dir / f"{person.name}_Events.csv"
        by_task = _read_cycles_section(events_csv) or _parse_events_section_cycles(events_csv)
        cycles = None
        if len(by_task) == 1:
            cycles = next(iter(by_task.values()))
        elif len(by_task) > 1:
            warnings.append(
                f"'{person.name}': cycles found under more than one task in its "
                f"Events export ({', '.join(sorted(by_task))}) -- can't disambiguate, "
                "used the whole processed segment as one cycle instead"
            )

        if cycles is None:
            first_time = float(data_lines[1].split(",")[0])
            last_time = float(data_lines[-1].split(",")[0])
            cycles = [(first_time, last_time)]
            if not by_task:
                warnings.append(
                    f"'{person.name}': no cycles found in its Events export -- "
                    "used the whole processed segment as one cycle"
                )

        with open(cycles_dir / f"{person.name}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            for t_start, t_end in cycles:
                w.writerow([t_start, t_end])

    return dest_dir, warnings, grouped
