"""
Headless regression check for muscle synergy extraction (synergy.py).

Uses a synthetic, exactly-2-synergy V matrix (no noise) so NMF should
recover a near-perfect reconstruction (R^2 close to 1) at rank 2. Uses
fixed_syns + a seed to keep the run fast and deterministic-ish.
"""
import sys
sys.path.insert(0, '../')

import numpy as np
import pandas as pd
from core.synergy import syns_nmf, build_synergy_input_matrix, extract_synergies
from core.batch_dataset import BatchTrial
from core.timeSeriesTable import timeSeriesTable


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    assert cond, label


# ---- syns_nmf recovers a known low-rank structure --------------------------
rng = np.random.default_rng(0)
n_muscles, n_time, true_rank = 6, 200, 2

M_true = rng.uniform(0.1, 1.0, size=(n_muscles, true_rank))
P_true = rng.uniform(0.1, 1.0, size=(true_rank, n_time))
V_mat = M_true @ P_true  # exact rank-2, non-negative, no noise

V_df = pd.DataFrame(V_mat.T, columns=[f"m{i}" for i in range(n_muscles)])
V_df.insert(0, "time", np.arange(1, n_time + 1))

result = syns_nmf(V_df, fixed_syns=true_rank, runs=3, max_iter=500, seed=0)

check("syns_nmf returns requested fixed rank",
      result.syns == true_rank)
check("syns_nmf M has shape (n_muscles, rank)",
      result.M.shape == (n_muscles, true_rank))
check("syns_nmf P has time + rank columns",
      list(result.P.columns) == ["time", "Syn1", "Syn2"])
check("syns_nmf reconstructs an exact rank-2 matrix with R2 > 0.99",
      float(1.0 - np.sum((result.V - result.Vr) ** 2) / np.sum((result.V - result.V.mean()) ** 2)) > 0.99)

# missing 'time' column raises
try:
    syns_nmf(pd.DataFrame({"m0": [1, 2, 3]}), fixed_syns=1)
    check("missing 'time' column raises ValueError", False)
except ValueError:
    check("missing 'time' column raises ValueError", True)

# ---- build_synergy_input_matrix / extract_synergies on a BatchTrial --------
fs = 100.0
n = 1000
labels = ["A", "B", "C"]
# simple non-negative synthetic envelopes
data = {lbl: np.abs(np.sin(np.linspace(0, 10, n) + i)) for i, lbl in enumerate(labels)}
tst = timeSeriesTable(fs, labels, data)
cycles = [(1.0, 3.0), (4.0, 6.0), (7.0, 9.0)]
trial = BatchTrial("synthetic", tst, cycles)

V = build_synergy_input_matrix(trial, n_points=51)
check("build_synergy_input_matrix has 'time' + one column per muscle",
      list(V.columns) == ["time"] + labels)
check("build_synergy_input_matrix row count == n_cycles * n_points",
      len(V) == len(cycles) * 51)

syn_result = extract_synergies(trial, n_points=51, fixed_syns=2, runs=2, max_iter=300, seed=1)
check("extract_synergies returns requested fixed rank on a BatchTrial",
      syn_result.syns == 2)

print("\nAll synergy regression checks passed.")
