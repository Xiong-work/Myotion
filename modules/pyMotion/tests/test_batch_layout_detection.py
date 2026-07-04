"""
Headless regression check for batch_scan.detect_layout() -- proposing a
BatchLayout by inspecting a real folder tree instead of requiring a
hand-written glob. Pure filesystem inspection, no c3d/mat parsing involved,
so no monkeypatching is needed. Runs standalone:
    cd modules/pyMotion/tests && python test_batch_layout_detection.py
"""
import sys
sys.path.insert(0, '../')

import os
import tempfile

from core.batch_scan import detect_layout


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    assert cond, label


def touch(*parts):
    path = os.path.join(*parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()


# ---- Case 1: flat, one file per participant, no MVC bucket -----------------
with tempfile.TemporaryDirectory() as root:
    touch(root, "P01", "P01_trial.c3d")
    touch(root, "P02", "P02_trial.c3d")
    touch(root, "P03", "P03_trial.c3d")

    s = detect_layout(root)
    check("flat: participant_glob is one level ('*')", s.participant_glob == "*")
    check("flat: no mvc bucket detected", s.mvc_glob == "")
    check("flat: exactly one task candidate proposed", len(s.task_candidates) == 1)
    check("flat: task candidate is a wildcard covering all 3 participants",
          s.task_candidates[0].glob == "*.c3d" and s.task_candidates[0].coverage == 3)


# ---- Case 2: nested Group/Participant/{Tasks,MVCs}/*.mat, mirrors the real
# lifting-and-bending sample dataset -----------------------------------------
with tempfile.TemporaryDirectory() as root:
    for group, ids in (("Affected_Group", ["A01", "A02", "A03"]),
                        ("Control_Group", ["C01", "C02", "C03"])):
        for pid in ids:
            touch(root, group, pid, "Tasks", "task_lift.mat")
            touch(root, group, pid, "Tasks", "task_bending.mat")
            for mvc in ("beiji", "fcj", "guer"):
                touch(root, group, pid, "MVCs", mvc + ".mat")

    s = detect_layout(root)
    check("nested: participant_glob is two levels ('*/*')", s.participant_glob == "*/*")
    check("nested: mvc bucket detected as 'MVCs/*.mat'", s.mvc_glob == "MVCs/*.mat")
    check("nested: 6 participants detected", s.participant_count == 6)
    task_globs = {c.glob for c in s.task_candidates}
    check("nested: both task files proposed as full-coverage candidates",
          task_globs == {"Tasks/task_lift.mat", "Tasks/task_bending.mat"})
    check("nested: both candidates cover all 6 participants",
          all(c.coverage == 6 for c in s.task_candidates))
    check("nested: no warnings on a fully consistent tree", s.warnings == [])


# ---- Case 3: an inconsistent participant (missing MVC bucket) still yields
# a usable suggestion, with a warning rather than a crash -------------------
with tempfile.TemporaryDirectory() as root:
    for pid in ("P01", "P02", "P03"):
        touch(root, pid, "Tasks", "task1.mat")
    for pid in ("P01", "P02"):  # P03 has no MVCs subfolder
        touch(root, pid, "MVCs", "mvc1.mat")

    s = detect_layout(root)
    check("inconsistent: still detects participant_glob '*'", s.participant_glob == "*")
    check("inconsistent: still finds the majority MVC bucket", s.mvc_glob == "MVCs/*.mat")
    check("inconsistent: full-coverage task candidate still found",
          len(s.task_candidates) == 1 and s.task_candidates[0].coverage == 3)


# ---- Case 4: empty folder -> no crash, clear warning -----------------------
with tempfile.TemporaryDirectory() as root:
    os.makedirs(os.path.join(root, "empty_subdir"))
    s = detect_layout(root)
    check("empty folder: no participant_glob guess beyond the default",
          s.participant_glob == "*")
    check("empty folder: warns instead of silently returning nothing useful",
          len(s.warnings) == 1 and "No" in s.warnings[0])
    check("empty folder: no task candidates", s.task_candidates == [])


print("\nAll detect_layout checks passed.")
