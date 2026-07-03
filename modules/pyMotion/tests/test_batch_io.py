"""
Headless regression check for batch_io.py's Cycle* event parsing and its
use as the default cycle source in from_workspace().

Uses synthetic data only (no sample files) so it can run standalone:
    cd modules/pyMotion/tests && python test_batch_io.py
"""
import sys
sys.path.insert(0, '../')

from core.trial import TrialEvent
from core.batch_io import _cycles_from_events, from_workspace
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

ws = FakeWorkspace({"withCycles": p_with_cycles, "cropOnly": p_crop_only})
participants = [FakePerson("withCycles"), FakePerson("cropOnly")]
ds = from_workspace(ws, participants)

check("participant with Cycle* events uses those, not crop_interval",
      ds["withCycles"].cycles == [(0.5, 1.5)])
check("participant without Cycle* events falls back to crop_interval",
      ds["cropOnly"].cycles == [(0.0, 5.0)])

print("\nAll batch_io cycle-parsing checks passed.")
