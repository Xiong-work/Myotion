"""
modules/pyMotion/core/wavelet_analysis.py — time-resolved EMG frequency
content via Continuous Wavelet Transform (Morlet, through PyWavelets).

This project's existing frequency-domain metrics (timeSeriesTable.meanFreq/
medFreq) compute one FFT over a whole segment -- one number, assuming the
signal's frequency content is roughly constant over time. This module gives
a *time-resolved* view instead: an instantaneous median-frequency curve
using the same "cumulative power crosses 50% of total" definition medFreq
already uses, just evaluated per time-slice of a CWT power surface rather
than once over a whole-segment FFT.

Two-step pipeline (mirrors synergy.py's shape, for the same reason -- see
emg-analysis-guard rules on signal-state separation):
  1. prepare_wavelet_input()          -- bandpass-filter only (NOT rectify/
                                          envelope/normalize -- frequency-
                                          domain analysis must stay off the
                                          enveloped/normalized signal path).
  2. instantaneous_median_frequency() -- CWT power surface -> one
                                          median-frequency value per sample.
  3. wavelet_medfreq_curve()          -- time-normalize each cycle's curve
                                          to n_points and average across
                                          cycles, via the same
                                          timeSeriesTable.timeNormalizeCycles
                                          CCI/Synergy already use.
"""

from __future__ import annotations

import numpy as np

from .batch_dataset import BatchTrial
from .timeSeriesTable import timeSeriesTable


def prepare_wavelet_input(
    trial: BatchTrial,
    bp_low: float = 20.0,
    bp_high: float = 450.0,
    bp_order: int = 4,
    demean: bool = True,
) -> BatchTrial:
    """Bandpass-filtered (only) EMG for frequency-domain analysis. Returns a
    new BatchTrial; does not mutate the input, and does not rectify/envelope/
    normalize -- keeping this signal path separate from prepare_synergy_input's
    enveloped path is required, not optional (see module docstring)."""
    out = trial.emg.copy()
    for ch in out.labels:
        data = np.asarray(out[ch], dtype=float)
        if demean:
            data = data - data.mean()
        out[ch] = data
        if bp_low and bp_high:
            out[ch] = out.bandpass(ch, bp_low, bp_high, bp_order)
    return BatchTrial(trial.name, out, trial.cycles, group=trial.group)


def instantaneous_median_frequency(
    data: np.ndarray,
    fs: float,
    freq_low: float = 20.0,
    freq_high: float = 450.0,
    n_freqs: int = 40,
    wavelet: str = "cmor1.5-1.0",
) -> tuple[np.ndarray, np.ndarray]:
    """Time-resolved median frequency of a 1-D signal via CWT.

    Log-spaced analysis frequencies from freq_low to freq_high Hz (log
    spacing matches how EMG spectral content is usually inspected, and
    keeps scale counts reasonable across a wide band). At each time sample,
    the median frequency is the lowest analysis frequency whose cumulative
    wavelet power (summed from freq_low upward) reaches half the total
    power at that sample -- the direct per-sample analogue of
    timeSeriesTable.medFreq's whole-segment "cumulative FFT power crosses
    50%" rule.

    Returns (freqs_hz, instantaneous_medfreq), both length n_freqs and
    len(data) respectively.
    """
    import pywt  # deferred: only this analysis needs it

    freqs_hz = np.geomspace(freq_low, freq_high, n_freqs)
    center_freq = pywt.central_frequency(wavelet)
    scales = center_freq * fs / freqs_hz

    coefs, actual_freqs = pywt.cwt(data, scales, wavelet, sampling_period=1.0 / fs)
    power = np.abs(coefs) ** 2  # (n_freqs, n_samples)

    # Sort ascending by frequency so cumulative power is monotonic in
    # frequency, matching medFreq's use of a frequency-ascending FFT output.
    order = np.argsort(actual_freqs)
    power = power[order]
    actual_freqs = actual_freqs[order]

    cum = np.cumsum(power, axis=0)
    half = cum[-1] / 2.0
    # (cum <= half).sum(axis=0) reproduces np.searchsorted(col, half, side="right")
    # per column, vectorized across all time samples at once.
    idx = np.clip((cum <= half[np.newaxis, :]).sum(axis=0), 0, len(actual_freqs) - 1)
    inst_medfreq = actual_freqs[idx]
    return actual_freqs, inst_medfreq


def wavelet_medfreq_curve(
    trial: BatchTrial,
    channel: str,
    n_points: int = 101,
    freq_low: float = 20.0,
    freq_high: float = 450.0,
    n_freqs: int = 40,
    wavelet: str = "cmor1.5-1.0",
) -> np.ndarray:
    """One trial's instantaneous median-frequency curve for one channel,
    time-normalized to n_points per cycle and averaged across cycles --
    same shape convention as Synergy's cycle-averaged activation pattern,
    so it's directly comparable across trials/groups with the same
    downstream tools (mean/SD summary, cosine similarity, SPM).

    `trial` must already be prepared (see prepare_wavelet_input) -- this
    function does not filter the signal itself.
    """
    if len(trial.cycles) == 0:
        raise ValueError(f"trial '{trial.name}' has no cycles to normalize")

    data = np.asarray(trial.emg[channel], dtype=float)
    fs = trial.emg.fs
    _freqs, inst = instantaneous_median_frequency(
        data, fs, freq_low=freq_low, freq_high=freq_high, n_freqs=n_freqs, wavelet=wavelet,
    )
    synthetic = timeSeriesTable(fs, [channel], {channel: inst})
    normalized = synthetic.timeNormalizeCycles(channel, trial.cycles, n_points)  # (n_cycles, n_points)
    return normalized.mean(axis=0)
