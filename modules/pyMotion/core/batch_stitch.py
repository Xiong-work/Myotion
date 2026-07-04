"""
modules/pyMotion/core/batch_stitch.py — detect and stitch separately-recorded
kinematics/EMG file pairs across a whole batch folder, ahead of Batch Import.

Some hardware setups save kinematics (+ force plates) and EMG to two
separate files per trial instead of one combined C3D -- either two different
extensions side by side (e.g. lift.c3d + lift.mat), or two files of the SAME
extension distinguished only by a naming convention (e.g. CAI06_CMJ_PRE01.c3d
+ CAI06_CMJ_PRE01_EMG.c3d). Batch Import expects one combined file per
participant per task, so this module bridges the gap:

  1. find_stitch_pairs()  -- walk participant folders, group files by
     pairing key (stem, with a trailing "_EMG"/"-EMG" marker stripped --
     see _pairing_key()), and classify each file's ROLE by actually parsing
     it (has real marker data? has real EMG channels?) -- never trust the
     extension or the "_EMG" name alone, since a .c3d can be EMG-only and a
     kinematics file can carry force-plate analog data that isn't EMG.
  2. stitch_all()         -- for each resolved kinematics+EMG pair, reuse
     the existing single-pair primitives (stitch.py's check_alignment /
     stitch_c3d) to write a new <stem>_stitched.c3d next to the kinematics
     file. Non-destructive: original files are never modified. Continues
     past individual failures rather than aborting the whole batch.

Once stitched, the resulting files are ordinary combined C3Ds -- Batch
Import's scan_batch_folder/build_participant/detect_layout need no changes
to consume them (e.g. emg_file="lift_stitched.c3d").
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .c3d import c3d_probe
from .mat import matFile
from .emg import _is_non_emg_channel
from .stitch import check_alignment, stitch_c3d, StitchError

_DATA_EXTS = (".c3d", ".mat")

# Some hardware setups record kinematics and EMG as two separate files of the
# SAME extension (e.g. "CAI06_CMJ_PRE01.c3d" + "CAI06_CMJ_PRE01_EMG.c3d")
# instead of same-stem-different-extension -- a plain stem can't group those,
# so pairing strips a trailing "_EMG"/"-EMG" marker (case-insensitive) first.
_EMG_STEM_SUFFIX_RE = re.compile(r'^(.+?)[_\-]emg$', re.IGNORECASE)


def _pairing_key(stem):
    """Group "<base>.ext" with "<base>_EMG.ext" under one key; falls back to
    the stem itself when no such suffix is present."""
    m = _EMG_STEM_SUFFIX_RE.match(stem)
    return m.group(1) if m else stem


@dataclass
class StitchPair:
    participant: str
    stem: str                    # shared filename (no extension), e.g. "lift"
    kin_path: str = ""
    emg_path: str = ""
    out_path: str = None         # set once stitched
    status: str = "pending"      # "pending" | "stitched" | "untrusted" | "ambiguous" | "error"
    message: str = ""


def _classify_file_role(path):
    """Return 'kinematics' | 'emg' | 'both' | 'neither' by actually parsing
    the file -- a .c3d can be EMG-only, and a kinematics .c3d can carry
    force-plate analog data that isn't EMG, so the extension alone can't
    tell you which role a file plays.

    Uses c3d_probe() (header-only, no per-frame data) rather than the full
    c3dFile parse -- classification only needs point/analog labels, and a
    full parse of a multi-MB motion-capture file can take several seconds
    each, which adds up fast when scanning a whole batch folder.
    """
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".c3d":
            has_points, analog_labels = c3d_probe(path)
            emg_labels = [l for l in analog_labels if not _is_non_emg_channel(l)]
            has_emg = bool(emg_labels)
        elif ext == ".mat":
            parsed = matFile(path)
            emg_labels = [l for l in parsed.labels if not _is_non_emg_channel(l)]
            has_emg = bool(emg_labels)
            has_points = False
        else:
            return "neither"
    except Exception:
        return "neither"

    if has_points and has_emg:
        return "both"
    if has_points:
        return "kinematics"
    if has_emg:
        return "emg"
    return "neither"


def find_stitch_pairs(root, participant_glob="*"):
    """Walk participant_glob subfolders of root -- and everything nested
    beneath each one, at any depth (flat Participant/, or nested
    Group/Participant/Task/, etc.) -- and resolve each directory's own files
    into StitchPairs, grouped by pairing key (stem, with a trailing
    "_EMG"/"-EMG" marker stripped -- see _pairing_key()).

    Stems with only one file are left alone (nothing to stitch -- could
    already be combined, or unrelated). Stems that already have a
    "<stem>_stitched.c3d" sibling, or where one of the files already
    classifies as 'both' (already combined), are skipped entirely. Stems
    with 2+ files that don't resolve to exactly one kinematics + one EMG
    source come back with status="ambiguous" for the caller to report.

    `participant` on the returned pairs is the directory's path relative to
    root (e.g. "P01" for a flat layout, "Normal/P01/CMJ" for a nested one) --
    just a display label, not used for grouping.

    Returns list[StitchPair], sorted by participant then stem.
    """
    root_path = Path(root)
    pairs = []

    for top in sorted(p for p in root_path.glob(participant_glob) if p.is_dir()):
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames.sort()
            folder = Path(dirpath)
            label = str(folder.relative_to(root_path))

            by_stem = {}
            for fn in sorted(filenames):
                f = folder / fn
                if f.suffix.lower() not in _DATA_EXTS:
                    continue
                if f.stem.endswith("_stitched"):
                    continue
                by_stem.setdefault(_pairing_key(f.stem), []).append(f)

            for stem, files in sorted(by_stem.items()):
                if len(files) < 2:
                    continue
                if (folder / (stem + "_stitched.c3d")).exists():
                    continue

                roles = {str(f): _classify_file_role(str(f)) for f in files}
                if any(r == "both" for r in roles.values()):
                    continue

                kin_candidates = [f for f in files if roles[str(f)] == "kinematics"]
                emg_candidates = [f for f in files if roles[str(f)] == "emg"]

                if len(kin_candidates) == 1 and len(emg_candidates) == 1:
                    pairs.append(StitchPair(
                        participant=label, stem=stem,
                        kin_path=str(kin_candidates[0]), emg_path=str(emg_candidates[0]),
                    ))
                else:
                    pairs.append(StitchPair(
                        participant=label, stem=stem,
                        status="ambiguous",
                        message="{} file(s) for '{}' don't resolve to exactly one kinematics "
                                "+ one EMG source (roles: {})".format(
                            len(files), stem,
                            {f.name: roles[str(f)] for f in files}
                        ),
                    ))

    return pairs


def stitch_all(pairs):
    """Run check_alignment + stitch_c3d for every "pending" pair, in place.

    Mutates each pair's status/message/out_path. Never touches kin_path or
    emg_path. Continues past individual failures -- one bad pairing doesn't
    block the rest of the batch.
    """
    for p in pairs:
        if p.status != "pending":
            continue
        try:
            offset_s, trusted, msg = check_alignment(p.kin_path, p.emg_path)
        except StitchError as e:
            p.status = "error"
            p.message = str(e)
            continue

        if not trusted:
            p.status = "untrusted"
            p.message = msg
            continue

        try:
            p.out_path = stitch_c3d(p.kin_path, p.emg_path, offset_s=offset_s)
            p.status = "stitched"
            p.message = msg
        except StitchError as e:
            p.status = "error"
            p.message = str(e)

    return pairs
