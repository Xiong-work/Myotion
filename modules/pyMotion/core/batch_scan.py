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
from dataclasses import dataclass, field
from pathlib import Path

from .c3d import c3dFile
from .mat import matFile
from .emg import emg, _is_non_emg_channel
from .kinematic import kinematic
from .person import person


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
    """
    path = Path(file_path)
    if not path.is_file():
        return False, "File does not exist: {}".format(file_path), []

    ext = path.suffix.lower()
    if ext not in (".c3d", ".mat"):
        return False, "Unsupported file format: {}".format(ext), []

    try:
        if ext == ".c3d":
            parsed = c3dFile(str(path))
            labels = parsed.analog.labels
        else:
            parsed = matFile(str(path))
            labels = parsed.labels
    except Exception as e:
        return False, "Failed to read {}: {}".format(path.name, str(e)), []

    if not labels:
        return False, "No channels found in file: {}".format(path.name), []

    if ext == ".c3d":
        emg_labels = [l for l in labels if not _is_non_emg_channel(l)]
        if not emg_labels:
            return False, (
                "No EMG channels found in '{}'. All {} analog channel(s) "
                "appear to be force plate or sensor data.".format(path.name, len(labels))
            ), []
        return True, "", emg_labels

    return True, "", list(labels)


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

    e = emg(candidate.emg_file)

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
    kin = kinematic(candidate.emg_file)
    return p, e, kin


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
