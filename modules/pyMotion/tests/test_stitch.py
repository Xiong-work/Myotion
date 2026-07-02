"""Regression check for core.stitch against real hardware-synced test trials
(EMG and kinematics/force-plate data recorded to separate files).

Run directly: python test_stitch.py
Requires the Myotion_dev\\Test_data_myotion sample data checked out alongside
this repo; skips (prints a notice) if the fixtures aren't present rather than
failing, since that data isn't part of the repo itself.
"""
import sys
sys.path.insert(0, '../')

import os
import shutil
import tempfile

from core.stitch import stitch_c3d, check_alignment, StitchError, load_emg_source
from core.c3d import c3dFile
from core.kinematic import kinematic

BOTH_C3D_KIN = r"F:\AccMov_dev\Myotion_dev\Test_data_myotion\EMG_Marker_sep\Both_c3d\CAI06_CMJ_PRE01.c3d"
BOTH_C3D_EMG = r"F:\AccMov_dev\Myotion_dev\Test_data_myotion\EMG_Marker_sep\Both_c3d\CAI06_CMJ_PRE01_EMG.c3d"
MAT_KIN = r"F:\AccMov_dev\Myotion_dev\Test_data_myotion\EMG_Marker_sep\Mat&c3d\zuoli.c3d"
MAT_EMG = r"F:\AccMov_dev\Myotion_dev\Test_data_myotion\EMG_Marker_sep\Mat&c3d\zuoli.mat"


def _run_case(tmpdir, name, kin_src, emg_src):
    print("====", name, "====")
    kin = os.path.join(tmpdir, os.path.basename(kin_src))
    emg = os.path.join(tmpdir, os.path.basename(emg_src))
    shutil.copy2(kin_src, kin)
    shutil.copy2(emg_src, emg)
    kin_mtime, emg_mtime = os.path.getmtime(kin_src), os.path.getmtime(emg_src)

    offset_s, trusted, msg = check_alignment(kin, emg)
    print("  check_alignment:", trusted, "-", msg)
    assert trusted, "expected a trusted offset for this fixture pair"

    out = stitch_c3d(kin, emg, offset_s=offset_s)
    assert os.path.isfile(out)

    # originals must be untouched
    assert os.path.getmtime(kin_src) == kin_mtime
    assert os.path.getmtime(emg_src) == emg_mtime

    orig_kin = c3dFile(kin)
    stitched = c3dFile(out)

    # point/marker data is passed through unchanged
    assert stitched.point_labels == orig_kin.point_labels
    assert stitched.point_fs == orig_kin.point_fs
    assert stitched.frame_number == orig_kin.frame_number

    # analog channel set = kinematics file's own channels (force plates) + EMG source's
    emg_source = load_emg_source(emg)
    expected = set(l.strip() for l in orig_kin.channel_labels) | set(emg_source.labels)
    got = set(l.strip() for l in stitched.channel_labels)
    assert got == expected, "channel set mismatch: missing {}, extra {}".format(
        expected - got, got - expected)

    # the merged file still loads through the app's normal kinematics path,
    # including force-plate grouping (this is what the Kinematics Inspection
    # tab and workspace.profile.kinematic actually consume)
    kin_obj = kinematic(out)
    assert kin_obj.isValid()
    print("  points:", len(stitched.point_labels), "labels x", stitched.frame_number, "frames")
    print("  analog:", len(stitched.channel_labels), "channels @", stitched.analog_fs, "Hz")
    print("  force plates:", len(kin_obj.force_plates))
    print(name, "PASSED\n")


def main():
    if not (os.path.isfile(BOTH_C3D_KIN) and os.path.isfile(MAT_KIN)):
        print("Test fixtures not found under Test_data_myotion — skipping.")
        return

    tmpdir = tempfile.mkdtemp(prefix="test_stitch_")
    try:
        _run_case(tmpdir, "two c3d files (shared hardware trigger)", BOTH_C3D_KIN, BOTH_C3D_EMG)
        _run_case(tmpdir, "c3d + mat (begin_time offset)", MAT_KIN, MAT_EMG)

        # stitch_c3d() never writes to the input paths themselves, regardless
        # of which folder they live in — always operate on tmpdir copies.
        cross_kin = os.path.join(tmpdir, "cross_" + os.path.basename(BOTH_C3D_KIN))
        cross_emg = os.path.join(tmpdir, "cross_" + os.path.basename(MAT_EMG))
        shutil.copy2(BOTH_C3D_KIN, cross_kin)
        shutil.copy2(MAT_EMG, cross_emg)
        try:
            stitch_c3d(cross_kin, cross_emg, offset_s=0.0)
        except StitchError:
            pass  # channel-name collision or rate mismatch expected; either is a valid guard
        else:
            print("NOTE: mismatched kin/emg pair stitched without error — that's expected, "
                  "stitch_c3d() doesn't gate on trust; check_alignment() is what UI callers "
                  "should consult before picking an offset")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("ALL STITCH TESTS PASSED")


if __name__ == "__main__":
    main()
