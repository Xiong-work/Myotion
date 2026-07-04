"""
Headless regression check for task-type cycle detection (cycle_detection.py).

Uses synthetic data only (no sample files) so it can run standalone:
    cd modules/pyMotion/tests && python test_cycle_detection.py
"""
import sys
sys.path.insert(0, '../')

import numpy as np
from core.cycle_detection import (
    detect_gait_cycles, detect_sit_stand_cycles, detect_squat_cycles,
    detect_trunk_flexion_cycles, detect_reach_cycles,
    detect_lifting_cycles, detect_pointing_cycles,
)


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    assert cond, label


# ---- detect_gait_cycles: marker-only fallback --------------------------------
# 5 strides at 1.0 Hz cadence, 5 s @ 100 Hz -- foot marker bobs with each step,
# touching a low point (foot-strike) once per stride. |sin(pi*t)| peaks (== gait_z
# minima, i.e. foot-strikes) at t=0.5,1.5,2.5,3.5,4.5 -- 5 strikes, 4 cycles.
fs_mkr = 100.0
t = np.arange(0, 5.0, 1.0 / fs_mkr)
gait_z = 10.0 - 8.0 * np.abs(np.sin(np.pi * 1.0 * t))
cycles = detect_gait_cycles(gait_z, fs_mkr, mode="walk")
check("gait: detects 4 cycles from 5 strikes", len(cycles) == 4)
if cycles:
    check("gait: first cycle starts near t=0.5", abs(cycles[0][0] - 0.5) < 0.1)
    check("gait: cycle duration ~= stride period (1.0s)",
          all(abs((e - s) - 1.0) < 0.15 for s, e in cycles))

# ---- detect_gait_cycles: force-plate GRF path --------------------------------
fs_fp = 1000.0
t_fp = np.arange(0, 5.0, 1.0 / fs_fp)
# Stance phase (Fz > 0) for 0.6s of each 1.0s cycle, swing (Fz = 0) for 0.4s.
fz = np.where((t_fp % 1.0) < 0.6, 500.0 * np.sin(np.pi * (t_fp % 1.0) / 0.6), 0.0)
cycles_fp = detect_gait_cycles(np.zeros_like(t), fs_mkr, fp_vertical=fz, fp_fs=fs_fp, mode="walk")
check("gait (force plate): detects 4 cycles from 5 stance phases", len(cycles_fp) == 4)

# ---- detect_sit_stand_cycles --------------------------------------------------
# 3 reps: quiet (seated) -> rise+fall (stand up, sit down) -> quiet, repeated.
fs_sts = 100.0
seg_quiet = np.zeros(int(0.8 * fs_sts))
seg_rep = 0.3 * np.sin(np.linspace(0, np.pi, int(1.2 * fs_sts)))  # one smooth rise+fall
sts_z = np.concatenate([seg_quiet, seg_rep] * 3 + [seg_quiet])
sts_cycles = detect_sit_stand_cycles(sts_z, fs_sts)
check("sit-to-stand: detects 3 reps", len(sts_cycles) == 3)
if sts_cycles:
    check("sit-to-stand: each rep is roughly the expected duration",
          all(0.8 < (e - s) < 1.6 for s, e in sts_cycles))

# ---- detect_squat_cycles is an alias --------------------------------------
check("squat: same result as sit-to-stand detector on the same signal",
      detect_squat_cycles(sts_z, fs_sts) == sts_cycles)

# ---- detect_trunk_flexion_cycles: same signal shape, slower defaults --------
# Reuse the sit-to-stand signal but with a longer quiet phase between reps,
# since trunk flexion's defaults expect a slower, more pausing motion.
seg_quiet_slow = np.zeros(int(1.0 * fs_sts))
trunk_z = np.concatenate([seg_quiet_slow, seg_rep] * 3 + [seg_quiet_slow])
trunk_cycles = detect_trunk_flexion_cycles(trunk_z, fs_sts)
check("trunk flexion: detects 3 reps", len(trunk_cycles) == 3)

# ---- detect_reach_cycles / detect_lifting_cycles / detect_pointing_cycles --
# 3 reaches: hand at rest -> moves out (x,y,z all change) -> returns -> rest.
fs_reach = 100.0
rest = np.tile([0.0, 0.0, 0.0], (int(0.5 * fs_reach), 1))
reach = np.stack([
    0.2 * np.sin(np.linspace(0, np.pi, int(0.4 * fs_reach))),
    0.15 * np.sin(np.linspace(0, np.pi, int(0.4 * fs_reach))),
    0.1 * np.sin(np.linspace(0, np.pi, int(0.4 * fs_reach))),
], axis=1)
reach_xyz = np.concatenate([rest, reach] * 3 + [rest], axis=0)

reach_cycles = detect_reach_cycles(reach_xyz, fs_reach)
check("reach: detects 3 reps from 3D resultant speed", len(reach_cycles) == 3)

pointing_cycles = detect_pointing_cycles(reach_xyz, fs_reach)
check("pointing: detects reps (reach detector with retuned defaults)", len(pointing_cycles) >= 1)

# Lifting's min_cycle_s default (0.6s) is longer than the quick 0.4s reach
# used above, so give it a slower, longer movement of its own.
reach_slow = np.stack([
    0.2 * np.sin(np.linspace(0, np.pi, int(0.7 * fs_reach))),
    0.15 * np.sin(np.linspace(0, np.pi, int(0.7 * fs_reach))),
    0.1 * np.sin(np.linspace(0, np.pi, int(0.7 * fs_reach))),
], axis=1)
reach_xyz_slow = np.concatenate([rest, reach_slow] * 3 + [rest], axis=0)
lifting_cycles = detect_lifting_cycles(reach_xyz_slow, fs_reach)
check("lifting: detects reps (reach detector with retuned defaults)", len(lifting_cycles) >= 1)

# ---- edge cases -----------------------------------------------------------
check("gait: empty signal returns no cycles", detect_gait_cycles([], fs_mkr) == [])
check("sit-to-stand: flat signal returns no cycles",
      detect_sit_stand_cycles(np.zeros(500), fs_sts) == [])
check("trunk flexion: flat signal returns no cycles",
      detect_trunk_flexion_cycles(np.zeros(500), fs_sts) == [])
check("reach: flat 3D signal returns no cycles",
      detect_reach_cycles(np.zeros((500, 3)), fs_reach) == [])
check("reach: malformed (1D) input returns no cycles",
      detect_reach_cycles(np.zeros(500), fs_reach) == [])

print("\nAll cycle_detection checks passed.")
