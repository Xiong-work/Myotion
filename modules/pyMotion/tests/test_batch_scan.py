"""
Headless regression check for batch_scan.py (batch-import folder resolution).

Builds a synthetic folder tree in a tempdir and monkeypatches c3dFile/matFile
so no real C3D/MAT fixture files are needed -- runs standalone:
    cd modules/pyMotion/tests && python test_batch_scan.py
"""
import sys
sys.path.insert(0, '../')

import os
import tempfile

import core.batch_scan as batch_scan
from core.batch_config import BatchLayout
from core.batch_scan import scan_batch_folder, channel_signature_groups


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    assert cond, label


# ---- fake c3dFile/matFile so parsing doesn't need real binary fixtures -----
class _FakeAnalog:
    def __init__(self, labels):
        self.labels = labels


class FakeC3D:
    # Per-filename label sets, keyed by basename -- lets the same fake class
    # return different channel sets for different files in one test run.
    _by_name = {
        "task1.c3d": ["EMG1", "EMG2", "Fx1", "Fy1", "Fz1"],   # normal cohort member
        "P02_task1.c3d": ["EMG1", "EMG2", "Fx1", "Fy1", "Fz1"],
        "P03_task1.c3d": ["EMG1", "EMG3"],                     # different channel set
        "task1_forceplate_only.c3d": ["Fx1", "Fy1", "Fz1"],    # no EMG channels
        "task1_unparseable.c3d": None,                          # raises on "open"
    }

    def __init__(self, path):
        name = os.path.basename(path)
        labels = self._by_name.get(name, ["EMG1", "EMG2"])
        if labels is None:
            raise ValueError("corrupt file")
        self.analog = _FakeAnalog(labels)


class FakeMat:
    def __init__(self, path):
        self.labels = ["EMG1", "EMG2"]


# Keep references to the real implementations -- restored later for the
# build_participant() section, which needs a real parsed fixture.
_real_c3dFile = batch_scan.c3dFile
_real_matFile = batch_scan.matFile
_real_is_non_emg_channel = batch_scan._is_non_emg_channel

batch_scan.c3dFile = FakeC3D
batch_scan.matFile = FakeMat

# _is_non_emg_channel is imported by name into batch_scan's namespace;
# patch it there too so Fx1/Fy1/Fz1 are correctly treated as non-EMG.
import re as _re
def _fake_is_non_emg(label):
    return bool(_re.match(r'^(Fx|Fy|Fz|Mx|My|Mz)\d*$', label))
batch_scan._is_non_emg_channel = _fake_is_non_emg


# ---- build a synthetic batch folder -----------------------------------------
with tempfile.TemporaryDirectory() as root:
    # P01: normal participant, task file + one mvc file
    os.makedirs(os.path.join(root, "P01", "unused_subdir"))
    open(os.path.join(root, "P01", "task1.c3d"), "w").close()
    open(os.path.join(root, "P01", "mvc_ta.c3d"), "w").close()

    # P02: same channel set as P01, no MVC file (optional)
    os.makedirs(os.path.join(root, "P02"))
    open(os.path.join(root, "P02", "P02_task1.c3d"), "w").close()

    # P03: valid file but a different channel set than P01/P02
    os.makedirs(os.path.join(root, "P03"))
    open(os.path.join(root, "P03", "P03_task1.c3d"), "w").close()

    # P04: no file matching the emg_file glob at all
    os.makedirs(os.path.join(root, "P04"))
    open(os.path.join(root, "P04", "notes.txt"), "w").close()

    # P05: two files both match the glob -- ambiguous
    os.makedirs(os.path.join(root, "P05"))
    open(os.path.join(root, "P05", "task1_a.c3d"), "w").close()
    open(os.path.join(root, "P05", "task1_b.c3d"), "w").close()

    # P06: matching file exists but has no EMG channels (force-plate only)
    os.makedirs(os.path.join(root, "P06"))
    open(os.path.join(root, "P06", "task1_forceplate_only.c3d"), "w").close()

    # P07: matching file exists but "fails to parse"
    os.makedirs(os.path.join(root, "P07"))
    open(os.path.join(root, "P07", "task1_unparseable.c3d"), "w").close()

    # A stray file directly under root, not inside a participant folder --
    # participant_glob="*" should only match directories.
    open(os.path.join(root, "root_level_stray.c3d"), "w").close()

    layout = BatchLayout(participant_glob="*", emg_file="*task1*.c3d", mvc_glob="mvc*.c3d")
    candidates = scan_batch_folder(root, layout)

    by_name = {c.name: c for c in candidates}
    check("scans exactly the 7 participant folders (not the stray root file)",
          set(by_name) == {"P01", "P02", "P03", "P04", "P05", "P06", "P07"})

    check("P01 resolved as ready", by_name["P01"].status == "ready")
    check("P01 emg_file resolved correctly",
          os.path.basename(by_name["P01"].emg_file) == "task1.c3d")
    check("P01 mvc file resolved", [os.path.basename(f) for f in by_name["P01"].mvc_files] == ["mvc_ta.c3d"])
    check("P01 channels exclude force-plate columns",
          by_name["P01"].channels == ["EMG1", "EMG2"])

    check("P02 resolved as ready with no MVC (optional)", by_name["P02"].status == "ready")
    check("P02 has no mvc files", by_name["P02"].mvc_files == [])

    check("P03 resolved as ready but with a different channel set",
          by_name["P03"].status == "ready" and by_name["P03"].channels == ["EMG1", "EMG3"])

    check("P04 (no matching file) reported as missing_emg", by_name["P04"].status == "missing_emg")
    check("P05 (two matching files) reported as ambiguous_emg", by_name["P05"].status == "ambiguous_emg")
    check("P06 (force-plate only, no EMG) reported as invalid", by_name["P06"].status == "invalid")
    check("P07 (unparseable file) reported as invalid", by_name["P07"].status == "invalid")
    check("P07 message surfaces the parse error", "corrupt file" in by_name["P07"].message)

    # ---- channel_signature_groups: cohort consistency check ----------------
    groups = channel_signature_groups(candidates)
    check("two channel-signature groups among the 3 ready participants",
          len(groups) == 2)
    p01_p02_sig = frozenset(["EMG1", "EMG2"])
    check("P01 and P02 grouped together (same channel set)",
          sorted(groups[p01_p02_sig]) == ["P01", "P02"])
    p03_sig = frozenset(["EMG1", "EMG3"])
    check("P03 in its own group (different channel set)",
          groups[p03_sig] == ["P03"])
    check("non-ready candidates (P04-P07) excluded from grouping",
          sum(len(v) for v in groups.values()) == 3)


# ---- build_participant(): real fixture, real emg/kinematic/person classes --
# Undo the c3dFile/matFile monkeypatch for this section -- we want the real
# parser against a real sample file here, not the fakes above.
import core.batch_scan as _bs
from core.batch_scan import ParticipantCandidate, build_participant, reassign_mvc_file
_bs.c3dFile = _real_c3dFile
_bs.matFile = _real_matFile
_bs._is_non_emg_channel = _real_is_non_emg_channel

fixture = os.path.abspath("Sample_data/Sample_data/c3d_emg/EMG_only.c3d")
if not os.path.isfile(fixture):
    print("[SKIP] build_participant checks -- fixture not found: {}".format(fixture))
else:
    cand = ParticipantCandidate(
        name="P01", folder=os.path.dirname(fixture), emg_file=fixture,
        mvc_files=[fixture],  # reuse the same file as its own MVC source -- just
                              # exercising the code path, not a real MVC trial
        status="ready",
    )
    raw_tib_r = "Noraxon Ultium-RT TIB.ANT."
    raw_per_r = "Noraxon Ultium-RT PERONEUS"
    mapping = (
        {raw_tib_r, raw_per_r},                       # enabled -- only 2 of 9 channels
        {raw_tib_r: "TA-R", raw_per_r: "PER-R"},       # muscle rename
        {raw_tib_r: "EMG_only.c3d"},                   # mvc_file (basename)
    )
    p, e, kin = build_participant(cand, mapping)

    check("build_participant returns a person with the candidate's name", p.name == "P01")
    check("enabled+renamed channels appear in getChannels()",
          sorted(e.getChannels()) == sorted(["TA-R", "PER-R"]))
    check("unmapped channels stay present but not enabled (e.g. the sync channel)",
          "Noraxon Ultium-Noraxon Ultium.Sync" in e.Channels
          and "Noraxon Ultium-Noraxon Ultium.Sync" not in e.getChannels())
    check("unmapped channels keep their raw (un-renamed) name",
          raw_per_r not in e.Channels and "PER-R" in e.Channels  # PERONEUS was renamed
          and "Noraxon Ultium-LT TIB.ANT." in e.Channels)         # this one was left alone
    check("MVC data assigned under the RENAMED channel name (setMVCFile-before-rename ordering)",
          e.emgMVCTST.hasChannel("TA-R") and len(e.emgMVCTST["TA-R"]) > 0)
    check("rawTST preserves the full original signal, unaffected by enable/disable",
          e.rawTST is not None and e.rawTST.hasChannel("TA-R"))

    # ---- reassign_mvc_file(): post-rename MVC (re)assignment for Edit Mapping
    check("PER-R has no MVC data yet (never assigned in the original mapping)",
          not e.emgMVCTST.hasChannel("PER-R") or len(e.emgMVCTST["PER-R"]) == 0)
    reassign_mvc_file(e, "PER-R", raw_per_r, fixture)
    check("reassign_mvc_file assigns MVC data under the current (renamed) name",
          e.emgMVCTST.hasChannel("PER-R") and len(e.emgMVCTST["PER-R"]) > 0)
    check("reassign_mvc_file records the file under the current name in mvcFilesMap",
          e.mvcFilesMap.get("PER-R") == fixture)

    try:
        reassign_mvc_file(e, "TA-R", "no such raw label", fixture)
        check("reassign_mvc_file raises on a raw label absent from the MVC file", False)
    except ValueError:
        check("reassign_mvc_file raises on a raw label absent from the MVC file", True)

    try:
        reassign_mvc_file(e, "NOT-A-CHANNEL", raw_per_r, fixture)
        check("reassign_mvc_file raises on an unknown current channel", False)
    except ValueError:
        check("reassign_mvc_file raises on an unknown current channel", True)

print("\nAll batch_scan checks passed.")
