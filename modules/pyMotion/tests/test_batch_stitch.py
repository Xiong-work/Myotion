"""
Headless regression check for batch_stitch.py (find_stitch_pairs / stitch_all).

Synthetic-data section monkeypatches c3d_probe/matFile/_is_non_emg_channel so
no real binary fixtures are needed; a second section (skipped if the path
isn't present) validates the whole pipeline against a real "separately
recorded" dataset. Runs standalone:
    cd modules/pyMotion/tests && python test_batch_stitch.py
"""
import sys
sys.path.insert(0, '../')

import os
import tempfile

import core.batch_stitch as batch_stitch
from core.batch_stitch import find_stitch_pairs, stitch_all, StitchPair
from core.stitch import StitchError


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    assert cond, label


def touch(*parts):
    path = os.path.join(*parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()


# ---- fake c3d_probe/matFile so role classification doesn't need real files -
# _classify_file_role() reads C3D role info via c3d_probe() (header-only, no
# per-frame data) rather than the full c3dFile parse -- fake that directly.
_C3D_BY_NAME = {
    # keyed by basename -- (has_points, analog_labels)
    "lift.c3d":            (True,  ["Fx1", "Fy1", "Fz1"]),          # kinematics-only (force plate, not EMG)
    "lift_combined.c3d":   (True,  ["EMG1", "EMG2"]),                 # already combined (points + real EMG)
    "wanyao.c3d":           (True,  []),                              # kinematics-only, no analog at all
    "zuoli.c3d":            (False, ["EMG1"]),                        # EMG-only c3d (no points!)
}


def _fake_c3d_probe(path):
    return _C3D_BY_NAME.get(os.path.basename(path), (False, []))


class FakeMat:
    _by_name = {
        "lift.mat": ["EMG1", "EMG2"],
        "mvc_only.mat": ["EMG1"],
    }

    def __init__(self, path):
        self.labels = self._by_name.get(os.path.basename(path), ["EMG1"])


_real_c3d_probe = batch_stitch.c3d_probe
_real_matFile = batch_stitch.matFile
_real_is_non_emg = batch_stitch._is_non_emg_channel

batch_stitch.c3d_probe = _fake_c3d_probe
batch_stitch.matFile = FakeMat

import re as _re
def _fake_is_non_emg(label):
    return bool(_re.match(r'^(Fx|Fy|Fz|Mx|My|Mz)\d*$', label))
batch_stitch._is_non_emg_channel = _fake_is_non_emg


# ---- _classify_file_role -----------------------------------------------
check("kinematics-only c3d (force-plate analog, no real EMG) classified as 'kinematics'",
      batch_stitch._classify_file_role("lift.c3d") == "kinematics")
check("EMG-only c3d (no points) classified as 'emg'",
      batch_stitch._classify_file_role("zuoli.c3d") == "emg")
check("already-combined c3d (points + real EMG) classified as 'both'",
      batch_stitch._classify_file_role("lift_combined.c3d") == "both")
check("mat file classified as 'emg'",
      batch_stitch._classify_file_role("lift.mat") == "emg")
check("kinematics c3d with no analog at all classified as 'kinematics'",
      batch_stitch._classify_file_role("wanyao.c3d") == "kinematics")


# ---- find_stitch_pairs: synthetic folder tree --------------------------
with tempfile.TemporaryDirectory() as root:
    # P01: clean pair -- lift.c3d (kin) + lift.mat (emg)
    touch(root, "P01", "lift.c3d")
    touch(root, "P01", "lift.mat")

    # P02: same clean pair, PLUS an already-stitched output -- should be skipped
    touch(root, "P02", "lift.c3d")
    touch(root, "P02", "lift.mat")
    touch(root, "P02", "lift_stitched.c3d")

    # P03: ambiguous -- zuoli.c3d is EMG-only (no points) here, same as
    # zuoli.mat, so this stem resolves to two EMG sources and zero
    # kinematics files instead of a clean 1-and-1 pair.
    touch(root, "P03", "zuoli.c3d")
    touch(root, "P03", "zuoli.mat")

    # P04: single lone file (no pair) -- should be silently ignored
    touch(root, "P04", "mvc_only.mat")

    # P05: already combined -- one file for the stem classifies as 'both',
    # alongside a stray duplicate; nothing to stitch
    touch(root, "P05", "lift_combined.c3d")
    touch(root, "P05", "lift_combined_2.c3d")  # different stem, irrelevant on its own (single file)

    pairs = find_stitch_pairs(root)
    by_key = {(p.participant, p.stem): p for p in pairs}

    check("exactly 2 pairs/issues found (P01 clean, P03 ambiguous)", len(pairs) == 2)
    check("P01/lift resolved as a clean pending pair",
          ("P01", "lift") in by_key and by_key[("P01", "lift")].status == "pending"
          and by_key[("P01", "lift")].kin_path.endswith("lift.c3d")
          and by_key[("P01", "lift")].emg_path.endswith("lift.mat"))
    check("P02/lift skipped (already has a _stitched sibling)", ("P02", "lift") not in by_key)
    check("P03/zuoli flagged ambiguous (two EMG sources, no kinematics file)",
          ("P03", "zuoli") in by_key and by_key[("P03", "zuoli")].status == "ambiguous")
    check("P04's lone file produced no entry at all", ("P04", "mvc_only") not in by_key)
    check("P05's already-combined stem produced no entry", ("P05", "lift_combined") not in by_key)


# ---- stitch_all: status-transition logic (mocked check_alignment/stitch_c3d)
def _mock_check_alignment(kin, emg):
    if "untrusted" in kin:
        return 0.0, False, "durations differ"
    if "broken" in kin:
        raise StitchError("simulated failure")
    return -1.0, True, "trusted"


def _mock_stitch_c3d(kin, emg, out_path=None, offset_s=None):
    if "writefail" in kin:
        raise StitchError("simulated write failure")
    return kin.replace(".c3d", "_stitched.c3d")


_real_check_alignment = batch_stitch.check_alignment
_real_stitch_c3d = batch_stitch.stitch_c3d
batch_stitch.check_alignment = _mock_check_alignment
batch_stitch.stitch_c3d = _mock_stitch_c3d

pairs = [
    StitchPair(participant="A", stem="ok", kin_path="ok.c3d", emg_path="ok.mat"),
    StitchPair(participant="B", stem="untrusted", kin_path="untrusted.c3d", emg_path="untrusted.mat"),
    StitchPair(participant="C", stem="broken", kin_path="broken.c3d", emg_path="broken.mat"),
    StitchPair(participant="D", stem="writefail", kin_path="writefail.c3d", emg_path="writefail.mat"),
    StitchPair(participant="E", stem="skip", status="ambiguous", message="pre-flagged"),
]
stitch_all(pairs)
by_stem = {p.stem: p for p in pairs}

check("trusted pair stitched successfully", by_stem["ok"].status == "stitched"
      and by_stem["ok"].out_path == "ok_stitched.c3d")
check("untrusted alignment reported, not stitched", by_stem["untrusted"].status == "untrusted")
check("check_alignment error reported as 'error'", by_stem["broken"].status == "error")
check("stitch_c3d write failure reported as 'error'", by_stem["writefail"].status == "error")
check("pre-flagged ambiguous pair left untouched", by_stem["skip"].status == "ambiguous"
      and by_stem["skip"].message == "pre-flagged")

batch_stitch.check_alignment = _real_check_alignment
batch_stitch.stitch_c3d = _real_stitch_c3d
batch_stitch.c3d_probe = _real_c3d_probe
batch_stitch.matFile = _real_matFile
batch_stitch._is_non_emg_channel = _real_is_non_emg


# ---- real dataset (skipped if not present on this machine) -------------
REAL_ROOT = r"F:\AccMov_dev\Myotion_dev\Test_data_myotion\EMG_Marker_sep\Mat&c3d"
if not os.path.isdir(REAL_ROOT):
    print("[SKIP] real EMG_Marker_sep checks -- folder not found: {}".format(REAL_ROOT))
else:
    real_pairs = find_stitch_pairs(REAL_ROOT)
    check("real dataset: 24 clean pairs found (8 participants x 3 tasks)",
          len(real_pairs) == 24 and all(p.status == "pending" for p in real_pairs))

    # Stitch just one pair for real, verify the output, then clean it up --
    # don't leave a stray artifact in the user's test-data folder.
    sample = real_pairs[0]
    stitch_all([sample])
    check("real dataset: sample pair stitched successfully", sample.status == "stitched")
    check("real dataset: stitched output file actually exists on disk",
          sample.out_path and os.path.isfile(sample.out_path))
    if sample.out_path and os.path.isfile(sample.out_path):
        os.remove(sample.out_path)

print("\nAll batch_stitch checks passed.")
