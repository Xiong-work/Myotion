"""
modules/pyMotion/core/batch_scan.py — resolve a batch-import folder into
per-participant file candidates, using a BatchConfig's [batch] layout.

One batch == one task (see batch_config.py): each participant subfolder is
expected to hold exactly one task EMG/kinematics file plus zero or more
optional MVC files, per BatchLayout's glob patterns. Fully unattended --
ambiguous matches (0 or >1 task files) are reported as an error for that
participant rather than prompting interactively, since batch import must
not require a per-participant click the way the single-add wizard's
"Scan Folder" does.
"""

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .c3d import c3dFile, c3d_probe
from .mat import matFile
from .emg import emg, _is_non_emg_channel
from .kinematic import kinematic
from .person import person
from .muscle_guess import _is_sync_channel
from .batch_stitch import _pairing_key


@dataclass
class ParticipantCandidate:
    name: str                                      # participant name, taken from the folder name
    folder: str                                    # absolute path to the participant folder
    emg_file: str = None                           # absolute path, or None if unresolved
    mvc_files: list = field(default_factory=list)  # absolute paths
    channels: list = field(default_factory=list)   # EMG channel labels, if resolved
    status: str = "invalid"                        # "ready" | "missing_emg" | "ambiguous_emg" | "invalid"
    message: str = ""


def _validate_emg_data_file(file_path):
    """Return (ok, message, channels).

    Standalone port of EMGAddWindow.validateEMGDataFile (main.py) for
    unattended batch use. Deliberately duplicated rather than shared: that
    method's messages go through self.tr() for the single-add wizard's
    translation infrastructure, and this scan runs headless/unattended with
    its own plain-English status reporting, so refactoring them into one
    function would mean routing translated UI text through a non-QObject
    context for no real benefit -- not worth the risk to the working wizard.

    C3D labels come from c3d_probe() (header-only, no per-frame data) --
    validation only needs the channel list, and scanning a whole batch root
    calls this once per candidate, so paying for a full frame-by-frame parse
    here would make "detect folder" / "scan" noticeably slow for real
    multi-MB motion-capture files.
    """
    path = Path(file_path)
    if not path.is_file():
        return False, "File does not exist: {}".format(file_path), []

    ext = path.suffix.lower()
    if ext not in (".c3d", ".mat"):
        return False, "Unsupported file format: {}".format(ext), []

    try:
        if ext == ".c3d":
            _, labels = c3d_probe(str(path))
        else:
            parsed = matFile(str(path))
            labels = parsed.labels
    except Exception as e:
        return False, "Failed to read {}: {}".format(path.name, str(e)), []

    if not labels:
        return False, "No channels found in file: {}".format(path.name), []

    if ext == ".c3d":
        emg_labels = [l for l in labels if not _is_non_emg_channel(l) and not _is_sync_channel(l)]
        if not emg_labels:
            return False, (
                "No EMG channels found in '{}'. All {} analog channel(s) "
                "appear to be force plate or sensor data.".format(path.name, len(labels))
            ), []
        return True, "", emg_labels

    # .mat exports can carry a sync/trigger channel alongside real EMG too
    # (e.g. Noraxon's "...同步" line) -- exclude it here the same way the
    # single-add wizard does right after loading (main.py's
    # importEMGBtnClicked), so it never becomes part of the channel-mapping
    # table or the cohort channel-signature comparison.
    mat_labels = [l for l in labels if not _is_sync_channel(l)]
    if not mat_labels:
        return False, "No EMG channels found in '{}' (only sync/trigger channel(s)).".format(
            path.name
        ), []
    return True, "", mat_labels


def scan_batch_folder(root, layout):
    """Resolve every participant_glob subfolder of *root* into a ParticipantCandidate.

    layout: a BatchLayout (see batch_config.py).
    Returns list[ParticipantCandidate], sorted by participant (folder) name.
    """
    root_path = Path(root)
    candidates = []

    for folder in sorted(p for p in root_path.glob(layout.participant_glob) if p.is_dir()):
        name = folder.name
        emg_matches = sorted(folder.glob(layout.emg_file))
        mvc_matches = sorted(folder.glob(layout.mvc_glob)) if layout.mvc_glob else []

        if len(emg_matches) == 0:
            candidates.append(ParticipantCandidate(
                name=name, folder=str(folder), status="missing_emg",
                message="No file matches '{}' in this folder.".format(layout.emg_file),
            ))
            continue
        if len(emg_matches) > 1:
            candidates.append(ParticipantCandidate(
                name=name, folder=str(folder), status="ambiguous_emg",
                message="{} files match '{}' -- batch import needs exactly one "
                        "(rename files or narrow emg_file in the config).".format(
                            len(emg_matches), layout.emg_file),
            ))
            continue

        emg_file = emg_matches[0]
        ok, msg, channels = _validate_emg_data_file(emg_file)
        if not ok:
            candidates.append(ParticipantCandidate(
                name=name, folder=str(folder), emg_file=str(emg_file),
                status="invalid", message=msg,
            ))
            continue

        candidates.append(ParticipantCandidate(
            name=name, folder=str(folder), emg_file=str(emg_file),
            mvc_files=[str(f) for f in mvc_matches],
            channels=channels, status="ready", message="",
        ))

    return candidates


def build_participant(candidate, mapping):
    """Build (person, emg, kinematic) for one ready ParticipantCandidate.

    mapping: (enabled: set[str], muscle: dict[str,str], mvc_file: dict[str,str])
        -- same shape ChannelMappingPanel.get_mapping() returns (minus errors).
        mvc_file values are basenames, resolved here against
        candidate.mvc_files (absolute paths).

    Mirrors EMGAddWindow.confirmBtnClicked's exact operation order (main.py):
    MVC files must be assigned via setMVCFile() using each channel's RAW name
    *before* renameChannel() runs -- renameChannel() propagates the relabel
    into emgMVCTST too, so doing it in the other order would assign MVC data
    to a channel name that doesn't exist yet.

    Person demographics default to "N/A" (not collected during batch import,
    same as the single-add wizard's own default when left blank).
    """
    enabled, muscle, mvc_file = mapping

    # A combined C3D is parsed once here and shared between emg()/kinematic()
    # -- each would otherwise independently re-parse the identical file from
    # disk (a full frame-by-frame read that can take several seconds), doubling
    # the cost of loading every participant for no benefit.
    preparsed = c3dFile(candidate.emg_file) if candidate.emg_file.lower().endswith(".c3d") else None

    e = emg(candidate.emg_file, preparsed_c3d=preparsed)

    mvc_by_basename = {os.path.basename(f): f for f in candidate.mvc_files}
    for chan, mvc_basename in mvc_file.items():
        if chan in e.Channels and mvc_basename in mvc_by_basename:
            e.setMVCFile(chan, mvc_by_basename[mvc_basename])

    for c in enabled:
        if c in e.Channels:
            e.enableChannel(c)

    for chan, new_name in muscle.items():
        if chan in e.Channels:
            e.renameChannel(chan, new_name)

    p = person(candidate.name, "N/A", "N/A")
    kin = kinematic(candidate.emg_file, preparsed_c3d=preparsed)
    return p, e, kin


def reassign_mvc_file(e, current_channel, raw_channel, mvc_path):
    """(Re)assign MVC data to an already-renamed channel -- for editing an
    already-loaded participant's mapping (main.py's Edit Mapping action),
    where emg.setMVCFile() can no longer be used directly.

    setMVCFile() requires its `channel` argument to simultaneously (a) be a
    current emg.Channels entry and (b) be a label found in the MVC file's
    own raw analog channels -- true only before a channel has been renamed.
    Once renamed (the normal state for anything loaded through Batch
    Import), no single string satisfies both, so this replicates
    setMVCFile()'s core logic directly against emgMVCTST/mvcFilesMap, keyed
    by the channel's CURRENT name. raw_channel is the original device
    channel name the MVC file's own labels are expected to use -- reusing
    the same convention build_participant() relies on (a cohort shares one
    recording setup, so MVC files use the same raw channel names as the
    task file did before it was renamed).

    Raises ValueError on any mismatch (channel/file missing, raw label not
    present in this particular MVC file) -- callers should catch this and
    skip/report per participant rather than let one bad pairing abort a
    whole batch edit.
    """
    if current_channel not in e.Channels:
        raise ValueError("Channel {} does not exist".format(current_channel))
    if mvc_path is None or not os.path.isfile(mvc_path):
        raise ValueError("MVC file not found: {}".format(mvc_path))

    if e.isC3D(mvc_path):
        mvc_obj = c3dFile(mvc_path)
        mvc_labels = mvc_obj.analog.labels
        mvc_tst = mvc_obj.analog.convertToTST()
    elif e.isMAT(mvc_path):
        mvc_obj = matFile(mvc_path)
        mvc_labels = mvc_obj.labels
        mvc_tst = mvc_obj.convertToTST()
    else:
        raise ValueError("Unsupported MVC file format: {}".format(mvc_path))

    if raw_channel not in mvc_labels:
        raise ValueError("Channel {} not found in MVC file {}".format(raw_channel, mvc_path))

    e.emgMVCTST[current_channel] = mvc_tst[raw_channel]
    e.mvcFilesMap[current_channel] = mvc_path


def channel_signature_groups(candidates):
    """Group "ready" candidates by their channel set, for the same cohort-
    consistency warning EMGBatchProcessButtonClicked already applies to
    already-loaded participants (see main.py).

    Returns {frozenset(channels): [participant_name, ...]}.
    """
    groups = {}
    for c in candidates:
        if c.status != "ready":
            continue
        sig = frozenset(c.channels)
        groups.setdefault(sig, []).append(c.name)
    return groups


# ---------------------------------------------------------------------------
# Layout detection -- propose a BatchLayout by inspecting a real folder tree,
# instead of asking the user to hand-write glob patterns from scratch.
# ---------------------------------------------------------------------------

_DATA_EXTS = (".c3d", ".mat")
_MVC_BUCKET_RE = re.compile(r'mvc|maximum.?voluntary', re.IGNORECASE)


@dataclass
class TaskCandidate:
    glob: str    # relative glob for BatchLayout.emg_file, e.g. "Tasks/task_lift.mat" or "*.c3d"
    coverage: int  # number of detected participant folders this pattern matches a file in
    example: str   # one matched relative path, for display


@dataclass
class LayoutSuggestion:
    participant_glob: str = "*"
    mvc_glob: str = ""
    task_candidates: list = field(default_factory=list)  # list[TaskCandidate], best first
    warnings: list = field(default_factory=list)
    participant_count: int = 0


def _superseded_by_stitch(f: Path, root_path: Path) -> bool:
    """True if *f* is a raw (pre-stitch) source file that already has a
    "<stem>_stitched.c3d" sibling next to it -- i.e. Batch Stitch already
    merged it, and it's no longer the file a task-file glob should point at.

    Uses the same pairing key find_stitch_pairs() groups by (stem, with a
    trailing "_EMG"/"-EMG" marker stripped) -- otherwise the EMG side of a
    two-c3d pair (e.g. "task_EMG.c3d", stitched output named after the
    kinematics file's own stem "task_stitched.c3d") would never match its
    own stem and would keep getting proposed as a stale separate candidate.
    """
    if f.stem.lower().endswith("_stitched"):
        return False
    return (root_path / f.parent / (_pairing_key(f.stem) + "_stitched.c3d")).is_file()


def detect_layout(root, exts=_DATA_EXTS):
    """Inspect a real batch-root folder tree and propose a BatchLayout.

    Heuristic, not authoritative -- meant to pre-fill BatchConfigDialog for
    the user to review/edit, never applied unconfirmed. Purely structural
    (not name-based), scanning shallow-to-deep and stopping at the first
    depth whose child folder names form a small, reused vocabulary (e.g.
    every participant sharing the same {"Tasks", "MVCs"} pair, or no further
    nesting at all). A depth is rejected -- and a deeper one tried -- when
    its "child names" look more like unique per-participant IDs than a
    small set of reused category folders (e.g. depth 1 of
    Group/Participant/Tasks trees, where each group's children are a
    different participant-ID vocabulary). A bucket name matching
    _MVC_BUCKET_RE becomes mvc_glob; every other bucket's files become
    task_candidates for the user to pick exactly one from (batches are
    single-task by convention -- see module docstring).
    """
    root_path = Path(root)
    files = []
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in exts:
                files.append(Path(dirpath, fn).relative_to(root_path))

    # Drop raw pre-stitch kinematics/EMG source files that have already been
    # merged into a "<stem>_stitched.c3d" sibling (same skip rule
    # find_stitch_pairs() applies) -- otherwise e.g. "lift.c3d", "lift.mat"
    # and "lift_stitched.c3d" all get proposed as three separate "task file"
    # candidates for what is really just one task.
    files = [f for f in files if not _superseded_by_stitch(f, root_path)]

    if not files:
        return LayoutSuggestion(warnings=[
            "No {} files found under this folder.".format(" / ".join(exts))
        ])

    max_depth = max(len(f.parts) - 1 for f in files)
    best = None  # (vocab_ratio, -depth, depth, groups) -- lowest ratio wins
    chosen = None
    for d in range(1, max_depth + 1):
        nested = [f for f in files if len(f.parts) > d]
        groups = {}
        for f in nested:
            groups.setdefault(f.parts[:d], []).append(f)
        if len(groups) < 2:
            continue

        n_groups = len(groups)
        bucket_names = {
            f.parts[d] for group_files in groups.values() for f in group_files
            if len(f.parts) > d + 1
        }
        vocab_ratio = len(bucket_names) / n_groups

        candidate = (vocab_ratio, -d, d, groups)
        if best is None or candidate < best:
            best = candidate
        if len(bucket_names) <= max(3, n_groups / 2):
            # A small, reused set of child folder names (or none at all) --
            # this looks like the real participant level; stop here rather
            # than descending into what would just be per-participant
            # bucket subfolders.
            chosen = (d, groups)
            break

    if chosen is not None:
        d, groups = chosen
    elif best is not None:
        _, _, d, groups = best
    else:
        # Every depth had fewer than 2 groups (e.g. a single participant) --
        # fall back to "each file's own parent folder is the participant".
        d = 1
        groups = {}
        for f in files:
            groups.setdefault(f.parts[:1], []).append(f)

    participant_glob = "/".join(["*"] * d)
    n = len(groups)

    mvc_files, task_files = [], []
    for group_files in groups.values():
        for f in group_files:
            bucket = f.parts[d] if len(f.parts) > d + 1 else None
            if bucket and _MVC_BUCKET_RE.search(bucket):
                mvc_files.append((bucket, f))
            else:
                task_files.append((bucket, f))

    mvc_glob = ""
    if mvc_files:
        bucket = Counter(b for b, _ in mvc_files).most_common(1)[0][0]
        ext_set = {f.suffix.lower() for b, f in mvc_files if b == bucket}
        ext = next(iter(ext_set)) if len(ext_set) == 1 else ""
        mvc_glob = "{}/*{}".format(bucket, ext)

    task_candidates = _propose_task_patterns(task_files, n)

    warnings = []
    covered = sum(len(v) for v in groups.values())
    if covered < len(files):
        warnings.append(
            "{} file(s) outside the detected participant structure were ignored "
            "(e.g. loose files at the batch root).".format(len(files) - covered)
        )
    if not task_candidates:
        warnings.append("Could not confidently guess a task file pattern -- set it manually.")
    elif task_candidates[0].coverage < n:
        warnings.append(
            "Best task-file guess only matches {}/{} participant folders -- "
            "review before use.".format(task_candidates[0].coverage, n)
        )

    return LayoutSuggestion(
        participant_glob=participant_glob,
        mvc_glob=mvc_glob,
        task_candidates=task_candidates,
        warnings=warnings,
        participant_count=n,
    )


def _propose_task_patterns(task_files, n):
    """task_files: list[(bucket_name_or_None, Path)]. Returns TaskCandidate
    list, best (highest-coverage) first."""
    if not task_files:
        return []

    exact_counts = Counter((bucket, f.name) for bucket, f in task_files)
    exact_examples = {}
    for bucket, f in task_files:
        exact_examples.setdefault((bucket, f.name), str(f))

    candidates = [
        TaskCandidate(
            glob="{}/{}".format(bucket, name) if bucket else name,
            coverage=count,
            example=exact_examples[(bucket, name)],
        )
        for (bucket, name), count in exact_counts.most_common()
    ]

    full_coverage = [c for c in candidates if c.coverage == n]
    if full_coverage:
        return full_coverage

    # Basenames vary per participant (e.g. embed the participant ID). If
    # every participant folder has exactly one task-bucket file, a wildcard
    # is the safe generalization. The participant dir is the bucket folder's
    # parent (or the file's own parent, if there's no bucket subfolder).
    per_participant = {}
    for bucket, f in task_files:
        participant_dir = f.parent.parent if bucket else f.parent
        per_participant.setdefault(participant_dir, []).append((bucket, f))
    if len(per_participant) == n and all(len(v) == 1 for v in per_participant.values()):
        entries = [v[0] for v in per_participant.values()]
        buckets = {b for b, _ in entries if b}
        ext_set = {f.suffix.lower() for _, f in entries}
        bucket = next(iter(buckets)) if len(buckets) == 1 else None
        ext = next(iter(ext_set)) if len(ext_set) == 1 else ""
        glob = "{}/*{}".format(bucket, ext) if bucket else "*{}".format(ext)
        example = str(entries[0][1])
        return [TaskCandidate(glob=glob, coverage=n, example=example)]

    # Multiple, differently-named files per participant with no shared exact
    # name -- return the partial-coverage exact matches for manual review.
    return sorted(candidates, key=lambda c: -c.coverage)
