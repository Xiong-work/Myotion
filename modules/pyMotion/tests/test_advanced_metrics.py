"""
Headless regression check for the Advanced EMG Analysis building blocks:
co-contraction index (Rudolph / Falconer-Winter) and time-normalization.

Uses synthetic data only (no sample files, no plotting) so it can run
standalone: python test_advanced_metrics.py
"""
import sys
sys.path.insert(0, '../')

import numpy as np
from core.timeSeriesTable import timeSeriesTable


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    assert cond, label


# ---- co-contraction index -------------------------------------------------
fs = 100.0
n = 200
labels = ["A", "B"]

# fully overlapping, fully active envelopes (normalized to 1) -> max co-contraction
tst = timeSeriesTable(fs, labels, {"A": np.ones(n), "B": np.ones(n)})
check("rudolph CCI == 2 for identical max-normalized signals",
      np.isclose(tst.cocontraction("A", "B", method="rudolph"), 2.0))
check("falconer_winter CCI == 1 for identical max-normalized signals",
      np.isclose(tst.cocontraction("A", "B", method="falconer_winter"), 1.0))

# no overlap (one channel silent) -> zero co-contraction
tst2 = timeSeriesTable(fs, labels, {"A": np.ones(n), "B": np.zeros(n)})
check("rudolph CCI == 0 when one channel is silent",
      np.isclose(tst2.cocontraction("A", "B", method="rudolph"), 0.0))
check("falconer_winter CCI == 0 when one channel is silent",
      np.isclose(tst2.cocontraction("A", "B", method="falconer_winter"), 0.0))

# curve variant matches scalar mean
curve = tst.cocontractionCurve("A", "B", method="rudolph")
check("cocontraction() equals mean(cocontractionCurve())",
      np.isclose(tst.cocontraction("A", "B", method="rudolph"), np.mean(curve)))

# mismatched-length channels raise
try:
    bad = timeSeriesTable(fs, labels, {"A": np.ones(n), "B": np.ones(n)})
    bad.data["B"] = np.ones(n - 1)
    bad.cocontraction("A", "B")
    check("mismatched-length channels raise ValueError", False)
except ValueError:
    check("mismatched-length channels raise ValueError", True)

# raw (signed, non-enveloped) EMG must be rejected -- CCI is only meaningful
# on rectified/enveloped signal (see check_batch_io.py finding: feeding raw
# bandpass-filtered EMG silently produced a negative, meaningless CCI before
# this guard was added)
try:
    signed = timeSeriesTable(fs, labels, {"A": np.array([-1.0] * n), "B": np.ones(n)})
    signed.cocontraction("A", "B")
    check("negative-valued (non-enveloped) input raises ValueError", False)
except ValueError:
    check("negative-valued (non-enveloped) input raises ValueError", True)

# ---- time normalization ----------------------------------------------------
# 10s ramp from 0 to 100 at fs=100 (1000 samples)
ramp_fs = 100.0
ramp = np.linspace(0, 100, 1000)
tst3 = timeSeriesTable(ramp_fs, ["ramp"], {"ramp": ramp})

# normalize the middle third of the ramp (t=3s..7s -> values ~30..70) to 101 points
normalized = tst3.timeNormalizeCycle("ramp", 3.0, 7.0, n_points=101)
check("timeNormalizeCycle returns requested number of points",
      len(normalized) == 101)
check("timeNormalizeCycle preserves start value of the cycle",
      np.isclose(normalized[0], ramp[300], atol=0.5))
check("timeNormalizeCycle preserves end value of the cycle",
      np.isclose(normalized[-1], ramp[700], atol=0.5))
check("timeNormalizeCycle midpoint (50%) matches expected ramp value",
      np.isclose(normalized[50], (ramp[300] + ramp[700]) / 2, atol=0.5))

# multiple cycles all resample to the same length
cycles = [(0.0, 2.0), (2.0, 5.0), (5.0, 9.9)]
stacked = tst3.timeNormalizeCycles("ramp", cycles, n_points=101)
check("timeNormalizeCycles returns shape (n_cycles, n_points)",
      stacked.shape == (3, 101))

# degenerate cycle bounds raise
try:
    tst3.timeNormalizeCycle("ramp", 5.0, 5.0)
    check("t_end <= t_start raises ValueError", False)
except ValueError:
    check("t_end <= t_start raises ValueError", True)

print("\nAll advanced-metrics regression checks passed.")
