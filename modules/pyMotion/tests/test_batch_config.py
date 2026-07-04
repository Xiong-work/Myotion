"""
Headless regression check for batch_config.py (TOML batch-import config).

Uses synthetic data only (no sample files) so it can run standalone:
    cd modules/pyMotion/tests && python test_batch_config.py
"""
import sys
sys.path.insert(0, '../')

import os
import tempfile

from core.batch_config import (
    BatchConfig, BatchLayout, ChannelMapping, EMGProcessingParams,
    load_batch_config, save_batch_config,
    processing_to_emg_configure, emg_configure_to_processing,
)
from core.emg import emgConfigure, emgFilterEnum, emgNormTypeEnum


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    assert cond, label


# ---- save/load round-trip ---------------------------------------------------
cfg = BatchConfig(
    layout=BatchLayout(
        task_type="Gait",
        participant_glob="P*",
        emg_file="task1.c3d",
        mvc_glob="mvc*.c3d",
    ),
    channel_mapping=ChannelMapping(
        enabled=["EMG 1", "EMG 2"],
        muscle={"EMG 1": "TA-R", "EMG 2": "TA-L"},
        mvc_file={"EMG 1": "mvc_ta_r.c3d"},
    ),
    processing=EMGProcessingParams(
        dc_offset_enable=True,
        bandpass_enable=True, bandpass_cutoff_l=20, bandpass_cutoff_h=400, bandpass_order=4,
        rectify_enable=True,
        envelope_enable=True, envelope_cutoff=8, envelope_order=2,
        normalization_enable=True, normalization_type="trial_max",
    ),
)

with tempfile.TemporaryDirectory() as tmpdir:
    path = os.path.join(tmpdir, "batch_config.toml")
    save_batch_config(path, cfg)

    # File must actually be valid TOML that a stdlib parser accepts.
    import tomllib
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    check("saved file parses as valid TOML", isinstance(raw, dict))

    loaded = load_batch_config(path)
    check("layout round-trips", loaded.layout == cfg.layout)
    check("channel_mapping round-trips (incl. keys with spaces)", loaded.channel_mapping == cfg.channel_mapping)
    check("processing params round-trip", loaded.processing == cfg.processing)

# ---- channel names needing escaping (quotes, backslashes) ------------------
tricky = BatchConfig(
    channel_mapping=ChannelMapping(
        enabled=['EMG "1"', "EMG\\2"],
        muscle={'EMG "1"': "TA-R", "EMG\\2": "TA-L"},
    ),
)
with tempfile.TemporaryDirectory() as tmpdir:
    path = os.path.join(tmpdir, "tricky.toml")
    save_batch_config(path, tricky)
    loaded = load_batch_config(path)
    check("channel names with quotes/backslashes round-trip",
          loaded.channel_mapping.muscle == tricky.channel_mapping.muscle)

# ---- defaults when sections are missing entirely ----------------------------
with tempfile.TemporaryDirectory() as tmpdir:
    path = os.path.join(tmpdir, "empty.toml")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# nothing here\n")
    loaded = load_batch_config(path)
    check("missing [batch] falls back to BatchLayout defaults", loaded.layout == BatchLayout())
    check("missing [channel_mapping] is empty", loaded.channel_mapping.is_empty())
    check("missing [emg_processing] falls back to EMGProcessingParams defaults",
          loaded.processing == EMGProcessingParams())

# ---- ChannelMapping.is_empty() ----------------------------------------------
check("fresh ChannelMapping is empty", ChannelMapping().is_empty())
check("ChannelMapping with only muscle entries is not empty",
      not ChannelMapping(muscle={"EMG1": "TA-R"}).is_empty())

# ---- processing_to_emg_configure / emg_configure_to_processing round-trip --
p = EMGProcessingParams(
    dc_offset_enable=False,
    bandpass_enable=True, bandpass_cutoff_l=30, bandpass_cutoff_h=350, bandpass_order=3,
    rectify_enable=False,
    envelope_enable=True, envelope_cutoff=10, envelope_order=4,
    normalization_enable=True, normalization_type="trial_max",
)
built = processing_to_emg_configure(p)
check("returns a real emgConfigure with 6 classical steps", built.size() == 6)
check("dc_offset_enable applied", built[0].enable == p.dc_offset_enable)
check("bandpass step is BAND_PASS type", built[1].type == emgFilterEnum.BAND_PASS)
check("bandpass cutoffs/order applied",
      (built[1].cutoff_l, built[1].cutoff_h, built[1].order)
      == (p.bandpass_cutoff_l, p.bandpass_cutoff_h, p.bandpass_order))
check("rectify_enable applied", built[2].enable == p.rectify_enable)
check("envelope step is LOW_PASS type", built[3].type == emgFilterEnum.LOW_PASS)
check("envelope cutoff/order applied",
      (built[3].cutoff_l, built[3].order) == (p.envelope_cutoff, p.envelope_order))
check("normalization enable/type applied",
      built[4].enable == p.normalization_enable and built[4].norm_type == emgNormTypeEnum.TRIAL_MAX)

roundtrip = emg_configure_to_processing(built)
check("full round-trip through emgConfigure preserves all params", roundtrip == p)

# Default (mvc) normalization type also round-trips correctly.
default_cfg = processing_to_emg_configure(EMGProcessingParams())
check("default normalization_type='mvc' maps to emgNormTypeEnum.MVC",
      default_cfg[4].norm_type == emgNormTypeEnum.MVC)
check("emgConfigure() constructor and processing_to_emg_configure() defaults agree",
      emg_configure_to_processing(default_cfg) == EMGProcessingParams())

print("\nAll batch_config checks passed.")
