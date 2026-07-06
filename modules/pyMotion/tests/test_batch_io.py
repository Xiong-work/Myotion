"""
Headless regression check for batch_io.py's Cycle* event parsing and its
use as the default cycle source in from_workspace().

Uses synthetic data only (no sample files) so it can run standalone:
    cd modules/pyMotion/tests && python test_batch_io.py
"""
import sys
sys.path.insert(0, '../')

import os
import tempfile
import shutil

from core.trial import TrialEvent
from core.cycle_detection import _cycles_from_events
from core.batch_io import (
    from_workspace, write_external_folder, load_external_folder, prep_data_adv_from_workspace,
)
from core.timeSeriesTable import timeSeriesTable


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    assert cond, label


# ---- _cycles_from_events: single task, multiple reps -------------------------
events = [
    TrialEvent(0.5, "CycleStart_Gait #1", "Cycle"),
    TrialEvent(1.5, "CycleEnd_Gait #1", "Cycle"),
    TrialEvent(1.5, "CycleStart_Gait #2", "Cycle"),
    TrialEvent(2.5, "CycleEnd_Gait #2", "Cycle"),
    TrialEvent(0.1, "Some Other Event", "General"),  # ignored: wrong context
]
cycles = _cycles_from_events(events)
check("parses 2 gait cycles from unnumbered context", cycles == [(0.5, 1.5), (1.5, 2.5)])

# ---- single-rep case: no " #n" suffix -----------------------------------
single = [
    TrialEvent(1.0, "CycleStart_Squat", "Cycle"),
    TrialEvent(2.0, "CycleEnd_Squat", "Cycle"),
]
check("parses a single unsuffixed rep", _cycles_from_events(single) == [(1.0, 2.0)])

# ---- no Cycle events at all -> empty list ---------------------------------
check("no Cycle* events returns []", _cycles_from_events([TrialEvent(0.0, "X", "General")]) == [])

# ---- multiple task types -> disambiguation required -----------------------
mixed = events + [
    TrialEvent(3.0, "CycleStart_Squat #1", "Cycle"),
    TrialEvent(3.5, "CycleEnd_Squat #1", "Cycle"),
]
try:
    _cycles_from_events(mixed)
    check("raises ValueError on ambiguous task_type", False)
except ValueError:
    check("raises ValueError on ambiguous task_type", True)
check("task_type= disambiguates to Gait", _cycles_from_events(mixed, task_type="Gait") == [(0.5, 1.5), (1.5, 2.5)])
check("task_type= disambiguates to Squat", _cycles_from_events(mixed, task_type="Squat") == [(3.0, 3.5)])


# ---- from_workspace(): Cycle* events take priority over crop_interval -----
class FakeEMG:
    def __init__(self, fs, labels, data):
        self.emgTST = timeSeriesTable(fs, labels, data)
        self.Channels = labels
        self.enabledChannels = set(labels)


class FakeProfile:
    def __init__(self, emg, extra_events, crop_interval=None):
        self.emg = emg
        self.extra_events = extra_events
        self.crop_interval = crop_interval


class FakePerson:
    def __init__(self, name):
        self.name = name


class FakeWorkspace:
    def __init__(self, profiles):
        self._profiles = profiles  # {name: FakeProfile}

    def __getitem__(self, person):
        return self._profiles[person.name]


import numpy as np
fs = 100.0
emg = FakeEMG(fs, ["EMG1"], {"EMG1": np.zeros(500)})

# Participant with detected gait cycles AND a crop_interval -- cycles should win.
p_with_cycles = FakeProfile(
    emg,
    [
        TrialEvent(0.5, "CycleStart_Gait #1", "Cycle"),
        TrialEvent(1.5, "CycleEnd_Gait #1", "Cycle"),
    ],
    crop_interval=(0.0, 5.0),
)
# Participant with only a crop_interval -- falls back as before.
p_crop_only = FakeProfile(emg, [], crop_interval=(0.0, 5.0))
# Participant with neither -- never cropped or cycle-detected in kinematics
# (e.g. only batch-processed so far) -- falls back to the whole recording.
p_bare = FakeProfile(emg, [])

ws = FakeWorkspace({"withCycles": p_with_cycles, "cropOnly": p_crop_only, "bare": p_bare})
participants = [FakePerson("withCycles"), FakePerson("cropOnly"), FakePerson("bare")]
ds = from_workspace(ws, participants)

check("participant with Cycle* events uses those, not crop_interval",
      ds["withCycles"].cycles == [(0.5, 1.5)])
check("participant without Cycle* events falls back to crop_interval",
      ds["cropOnly"].cycles == [(0.0, 5.0)])
check("participant with neither falls back to the whole recording",
      ds["bare"].cycles == [(0.0, 5.0)])  # 500 samples @ 100 Hz = 5.0 s

# ---- write_external_folder(): from_workspace() dataset round-trips through
# ---- write_external_folder() -> load_external_folder() -------------------
tmp_dir = tempfile.mkdtemp()
try:
    dest = write_external_folder(ds, os.path.join(tmp_dir, "Data_Adv"))
    check("write_external_folder writes flat emg/+cycles/ (single implicit group)",
          os.path.isdir(os.path.join(dest, "emg")) and os.path.isdir(os.path.join(dest, "cycles")))

    reloaded = load_external_folder(
        os.path.join(dest, "cycles"), os.path.join(dest, "emg"),
        header_cycles=False, header_emg=True,
    )
    check("round-trip preserves participant names", sorted(reloaded.names) == ["bare", "cropOnly", "withCycles"])
    check("round-trip preserves cycles", reloaded["withCycles"].cycles == [(0.5, 1.5)])
    check("round-trip preserves channel labels", reloaded["withCycles"].emg.labels == ["EMG1"])
finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)

# ---- prep_data_adv_from_workspace(): reads each participant's own already-
# ---- exported files on disk (not the live workspace object) --------------
class DiskFakePerson:
    def __init__(self, name):
        self.name = name


class DiskFakeWorkspace:
    def __init__(self, participants):
        self.participants = participants


tmp_dir = tempfile.mkdtemp()
try:
    # N01: has a processed-EMG export AND an Events.csv with one task's cycles.
    n01_dir = os.path.join(tmp_dir, "N01")
    os.makedirs(n01_dir)
    with open(os.path.join(n01_dir, "N01_emg_processed.csv"), "w", encoding="utf-8") as f:
        f.write("# Sample frequency: 10.0 Hz\n# Analysis segment: 0.000 s - 1.000 s\n")
        f.write("Time (s),Ch1\n")
        for i in range(11):
            f.write(f"{i / 10.0},{i}\n")
    with open(os.path.join(n01_dir, "N01_Events.csv"), "w", encoding="utf-8") as f:
        f.write("# Participant: N01\n#\n# Cycles\n")
        f.write("Task,Cycle,Start (s),End (s)\n")
        f.write("Sit-to-Stand,1,0.1,0.4\n")
        f.write("Sit-to-Stand,2,0.5,0.9\n")

    # N02: processed EMG but no Events.csv -- falls back to whole segment.
    n02_dir = os.path.join(tmp_dir, "N02")
    os.makedirs(n02_dir)
    with open(os.path.join(n02_dir, "N02_emg_processed.csv"), "w", encoding="utf-8") as f:
        f.write("# Sample frequency: 10.0 Hz\n")
        f.write("Time (s),Ch1\n")
        for i in range(6):
            f.write(f"{i / 10.0},{i}\n")

    # N03: no exported files at all yet -- skipped.
    os.makedirs(os.path.join(tmp_dir, "N03"))

    # N04: pre-fix Events.csv -- CycleStart_/CycleEnd_ rows only in the flat
    # "# Events" section, no "# Cycles" section -- must still parse correctly
    # via _parse_events_section_cycles's fallback, no re-export required.
    n04_dir = os.path.join(tmp_dir, "N04")
    os.makedirs(n04_dir)
    with open(os.path.join(n04_dir, "N04_emg_processed.csv"), "w", encoding="utf-8") as f:
        f.write("# Sample frequency: 10.0 Hz\n")
        f.write("Time (s),Ch1\n")
        for i in range(11):
            f.write(f"{i / 10.0},{i}\n")
    with open(os.path.join(n04_dir, "N04_Events.csv"), "w", encoding="utf-8") as f:
        f.write("# Participant: N04\n#\n# Events\n")
        f.write("Time (s),Label\n")
        f.write("0.1,CycleStart_Sit-to-Stand #1\n")
        f.write("0.4,CycleEnd_Sit-to-Stand #1\n")
        f.write("0.5,CycleStart_Sit-to-Stand #2\n")
        f.write("0.9,CycleEnd_Sit-to-Stand #2\n")

    ws2 = DiskFakeWorkspace([
        DiskFakePerson("N01"), DiskFakePerson("N02"), DiskFakePerson("N03"), DiskFakePerson("N04"),
    ])
    dest_dir, prep_warnings, grouped = prep_data_adv_from_workspace(ws2, tmp_dir)
    check("no groups assigned -> flat output (not grouped)", grouped is False)

    check("N01, N02, N04 emg files written, N03 skipped",
          sorted(os.listdir(os.path.join(dest_dir, "emg"))) == ["N01.csv", "N02.csv", "N04.csv"])
    check("N03 flagged as skipped in warnings",
          any("N03" in w and "skipped" in w for w in prep_warnings))
    check("N02 flagged as whole-segment fallback in warnings",
          any("N02" in w and "no cycles found" in w for w in prep_warnings))
    check("N04 (old-format Events.csv) got no fallback warning -- cycles were found",
          not any("N04" in w for w in prep_warnings))

    prepped = load_external_folder(
        os.path.join(dest_dir, "cycles"), os.path.join(dest_dir, "emg"),
        header_cycles=False, header_emg=True,
    )
    check("N01's Events.csv Cycles section parsed correctly",
          prepped["N01"].cycles == [(0.1, 0.4), (0.5, 0.9)])
    check("N02 fell back to the whole processed segment as one cycle",
          prepped["N02"].cycles == [(0.0, 0.5)])
    check("N04's old-format flat Events section still parsed into paired cycles",
          prepped["N04"].cycles == [(0.1, 0.4), (0.5, 0.9)])
    check("N01 emg file's leading '#' comment lines were stripped",
          not open(os.path.join(dest_dir, "emg", "N01.csv"), encoding="utf-8").readline().startswith("#"))
finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)

# ---- prep_data_adv_from_workspace(): groups_by_name splits participants
# ---- into per-group subfolders, ungrouped ones fall under DEFAULT_GROUP --
from core.batch_dataset import DEFAULT_GROUP
from core.batch_io import load_external_groups

tmp_dir = tempfile.mkdtemp()
try:
    for name in ("N01", "N02", "P01"):
        d = os.path.join(tmp_dir, name)
        os.makedirs(d)
        with open(os.path.join(d, f"{name}_emg_processed.csv"), "w", encoding="utf-8") as f:
            f.write("Time (s),Ch1\n")
            for i in range(6):
                f.write(f"{i / 10.0},{i}\n")

    ws3 = DiskFakeWorkspace([DiskFakePerson("N01"), DiskFakePerson("N02"), DiskFakePerson("P01")])
    dest_dir, prep_warnings, grouped = prep_data_adv_from_workspace(
        ws3, tmp_dir, groups_by_name={"N01": "Control", "N02": "Control"},  # P01 left unassigned
    )
    check("assigning at least one real group -> grouped output", grouped is True)
    check("Control/Ungrouped subfolders both created",
          sorted(os.listdir(dest_dir)) == ["Control", DEFAULT_GROUP])

    loaded = load_external_groups(dest_dir, cycle_mode="discrete")
    check("Control group has N01+N02", sorted(t.name.split("/")[-1] for t in loaded.trials_in("Control")) == ["N01", "N02"])
    check("unassigned P01 filed under DEFAULT_GROUP",
          [t.name.split("/")[-1] for t in loaded.trials_in(DEFAULT_GROUP)] == ["P01"])
finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)

print("\nAll batch_io cycle-parsing checks passed.")
