"""
modules/pyMotion/core/batch_config.py — TOML batch-import configuration.

One batch config == one task, one shared EMG channel cohort (project
convention: a cohort shares channels; a batch is single-task, e.g. each
participant folder holds one optional MVC file plus one task file). Sections:

  [batch]            folder layout convention for scanning a batch root
  [channel_mapping]   channel enable/rename/MVC-file assignment, shared
                       across every participant in the batch -- empty until
                       mapped once via the UI, then reusable/editable
  [emg_processing]    the 5 configurable classical_steps fields, round-
                       tripped to/from the existing emgConfigure object so
                       BatchEMGWorker/processWithConfigure need no changes

Read via tomllib (builtin, Python 3.11+; no new dependency). Written with a
small hand-rolled serializer -- the schema is flat and known, so no
TOML-writer dependency is needed either.
"""

import tomllib
from dataclasses import dataclass, field

from .emg import emgConfigure, emgFilterEnum, emgNormTypeEnum


@dataclass
class BatchLayout:
    task_type: str = ""            # free text; matches the kinematics Task type dropdown when relevant
    participant_glob: str = "*"    # subfolders of the batch root, one per participant
    emg_file: str = "*.c3d"        # glob (within each participant folder) for the single task file
    mvc_glob: str = ""             # glob for MVC file(s); "" means no MVC expected


@dataclass
class ChannelMapping:
    enabled: list = field(default_factory=list)   # list[str] of raw channel names to keep
    muscle: dict = field(default_factory=dict)    # {raw_chan: muscle_short_name}
    mvc_file: dict = field(default_factory=dict)  # {raw_chan: mvc filename}

    def is_empty(self):
        return not self.enabled and not self.muscle


@dataclass
class EMGProcessingParams:
    """Mirrors emgConfigure's fixed 6-step classical pipeline (steps 0-5);
    step 2 (rectify) and step 5 (summary) have no tunable fields."""
    dc_offset_enable: bool = True
    bandpass_enable: bool = True
    bandpass_cutoff_l: float = 50
    bandpass_cutoff_h: float = 450
    bandpass_order: int = 2
    rectify_enable: bool = True
    envelope_enable: bool = True
    envelope_cutoff: float = 6
    envelope_order: int = 2
    normalization_enable: bool = False
    normalization_type: str = "mvc"  # "mvc" | "trial_max"


@dataclass
class BatchConfig:
    layout: BatchLayout = field(default_factory=BatchLayout)
    channel_mapping: ChannelMapping = field(default_factory=ChannelMapping)
    processing: EMGProcessingParams = field(default_factory=EMGProcessingParams)


def load_batch_config(path) -> BatchConfig:
    with open(path, "rb") as f:
        data = tomllib.load(f)

    b = data.get("batch", {})
    layout = BatchLayout(
        task_type=b.get("task_type", ""),
        participant_glob=b.get("participant_glob", "*"),
        emg_file=b.get("emg_file", "*.c3d"),
        mvc_glob=b.get("mvc_glob", ""),
    )

    cm = data.get("channel_mapping", {})
    channel_mapping = ChannelMapping(
        enabled=list(cm.get("enabled", [])),
        muscle=dict(cm.get("muscle", {})),
        mvc_file=dict(cm.get("mvc_file", {})),
    )

    ep = data.get("emg_processing", {})
    d = EMGProcessingParams()
    processing = EMGProcessingParams(
        dc_offset_enable=ep.get("dc_offset_enable", d.dc_offset_enable),
        bandpass_enable=ep.get("bandpass_enable", d.bandpass_enable),
        bandpass_cutoff_l=ep.get("bandpass_cutoff_l", d.bandpass_cutoff_l),
        bandpass_cutoff_h=ep.get("bandpass_cutoff_h", d.bandpass_cutoff_h),
        bandpass_order=ep.get("bandpass_order", d.bandpass_order),
        rectify_enable=ep.get("rectify_enable", d.rectify_enable),
        envelope_enable=ep.get("envelope_enable", d.envelope_enable),
        envelope_cutoff=ep.get("envelope_cutoff", d.envelope_cutoff),
        envelope_order=ep.get("envelope_order", d.envelope_order),
        normalization_enable=ep.get("normalization_enable", d.normalization_enable),
        normalization_type=ep.get("normalization_type", d.normalization_type),
    )

    return BatchConfig(layout=layout, channel_mapping=channel_mapping, processing=processing)


def _toml_scalar(v):
    """Render a Python scalar (bool/int/float/str) as TOML literal text."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return '"{}"'.format(escaped)
    raise TypeError("unsupported TOML scalar type: {}".format(type(v)))


def _toml_key(k):
    """Always emit a quoted key -- channel names may contain spaces/dots/etc,
    which bare TOML keys don't allow; quoted keys accept any string."""
    escaped = str(k).replace("\\", "\\\\").replace('"', '\\"')
    return '"{}"'.format(escaped)


def save_batch_config(path, cfg: BatchConfig):
    lines = [
        "[batch]",
        "task_type = {}".format(_toml_scalar(cfg.layout.task_type)),
        "participant_glob = {}".format(_toml_scalar(cfg.layout.participant_glob)),
        "emg_file = {}".format(_toml_scalar(cfg.layout.emg_file)),
        "mvc_glob = {}".format(_toml_scalar(cfg.layout.mvc_glob)),
        "",
        "[channel_mapping]",
        "enabled = [{}]".format(", ".join(_toml_scalar(c) for c in cfg.channel_mapping.enabled)),
        "",
        "[channel_mapping.muscle]",
    ]
    for chan, muscle in cfg.channel_mapping.muscle.items():
        lines.append("{} = {}".format(_toml_key(chan), _toml_scalar(muscle)))
    lines.append("")
    lines.append("[channel_mapping.mvc_file]")
    for chan, fname in cfg.channel_mapping.mvc_file.items():
        lines.append("{} = {}".format(_toml_key(chan), _toml_scalar(fname)))
    lines.append("")

    p = cfg.processing
    lines += [
        "[emg_processing]",
        "dc_offset_enable = {}".format(_toml_scalar(p.dc_offset_enable)),
        "bandpass_enable = {}".format(_toml_scalar(p.bandpass_enable)),
        "bandpass_cutoff_l = {}".format(_toml_scalar(p.bandpass_cutoff_l)),
        "bandpass_cutoff_h = {}".format(_toml_scalar(p.bandpass_cutoff_h)),
        "bandpass_order = {}".format(_toml_scalar(p.bandpass_order)),
        "rectify_enable = {}".format(_toml_scalar(p.rectify_enable)),
        "envelope_enable = {}".format(_toml_scalar(p.envelope_enable)),
        "envelope_cutoff = {}".format(_toml_scalar(p.envelope_cutoff)),
        "envelope_order = {}".format(_toml_scalar(p.envelope_order)),
        "normalization_enable = {}".format(_toml_scalar(p.normalization_enable)),
        "normalization_type = {}".format(_toml_scalar(p.normalization_type)),
        "",
    ]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def processing_to_emg_configure(p: EMGProcessingParams) -> emgConfigure:
    """Build a fresh emgConfigure from batch-config processing params.

    Only sets the fields the classical 6-step pipeline actually exposes;
    the returned object is otherwise a normal emgConfigure, fully compatible
    with BatchEMGWorker / processWithConfigure.
    """
    cfg = emgConfigure()
    cfg[0].enable = p.dc_offset_enable

    cfg[1].enable = p.bandpass_enable
    cfg[1].type = emgFilterEnum.BAND_PASS
    cfg[1].cutoff_l = p.bandpass_cutoff_l
    cfg[1].cutoff_h = p.bandpass_cutoff_h
    cfg[1].order = p.bandpass_order

    cfg[2].enable = p.rectify_enable

    cfg[3].enable = p.envelope_enable
    cfg[3].type = emgFilterEnum.LOW_PASS
    cfg[3].cutoff_l = p.envelope_cutoff
    cfg[3].order = p.envelope_order

    cfg[4].enable = p.normalization_enable
    cfg[4].norm_type = (
        emgNormTypeEnum.TRIAL_MAX if p.normalization_type == "trial_max"
        else emgNormTypeEnum.MVC
    )
    return cfg


def emg_configure_to_processing(cfg: emgConfigure) -> EMGProcessingParams:
    """Inverse of processing_to_emg_configure -- used by the Edit Config
    dialog to pre-fill from an already-loaded/saved emgConfigure."""
    norm_type = getattr(cfg[4], "norm_type", emgNormTypeEnum.MVC)
    return EMGProcessingParams(
        dc_offset_enable=cfg[0].enable,
        bandpass_enable=cfg[1].enable,
        bandpass_cutoff_l=cfg[1].cutoff_l,
        bandpass_cutoff_h=cfg[1].cutoff_h,
        bandpass_order=cfg[1].order,
        rectify_enable=cfg[2].enable,
        envelope_enable=cfg[3].enable,
        envelope_cutoff=cfg[3].cutoff_l,
        envelope_order=cfg[3].order,
        normalization_enable=cfg[4].enable,
        normalization_type=(
            "trial_max" if int(norm_type) == int(emgNormTypeEnum.TRIAL_MAX) else "mvc"
        ),
    )
