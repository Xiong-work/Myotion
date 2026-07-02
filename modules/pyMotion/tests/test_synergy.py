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
from core.synergy import (
    syns_nmf, build_synergy_input_matrix, extract_synergies,
    cossim, CoA, FWHM, classify_kmeans, group_synergy_summary,
)
from core.batch_dataset import BatchTrial
from core.timeSeriesTable import timeSeriesTable


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    assert cond, label


# ---- cossim (R/cossim.R) ----------------------------------------------------
check("cossim of a vector with itself == 1",
      np.isclose(cossim([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]), 1.0))
check("cossim of orthogonal vectors == 0",
      np.isclose(cossim([1.0, 0.0], [0.0, 1.0]), 0.0))
check("cossim of opposite vectors == -1",
      np.isclose(cossim([1.0, 2.0, 3.0], [-1.0, -2.0, -3.0]), -1.0))

# ---- FWHM (R/FWHM.R) ---------------------------------------------------------
# 4 of 10 samples are the peak (==1, everything else 0) -> after sub_minimum
# (no-op here) and /max normalization, exactly those 4 exceed 0.5.
fwhm_signal = np.array([0, 0, 0, 1, 1, 1, 1, 0, 0, 0], dtype=float)
check("FWHM counts samples above half-max",
      FWHM(fwhm_signal) == 4)
check("FWHM of a flat (zero-range) signal is 0",
      FWHM(np.full(10, 5.0)) == 0)

# ---- CoA (R/CoA.R) ------------------------------------------------------------
# A delta at 0-based index i0 maps to CoA == i0 + 1 exactly (see CoA()'s
# derivation from the R source: alpha_i0 recovered exactly by atan2).
n_coa = 8
for i0 in range(n_coa):
    delta = np.zeros(n_coa)
    delta[i0] = 1.0
    coa = CoA(delta)
    check(f"CoA of a delta at index {i0} == {i0 + 1}", np.isclose(coa, i0 + 1))

check("CoA of a uniform (no concentration) signal is NaN",
      np.isnan(CoA(np.ones(n_coa))))

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

# ---- classify_kmeans recovers a consistent cross-trial labeling ------------
# The property that actually matters: each trial's syns_nmf() is run
# independently with its own random init, so "Syn1"/"Syn2" is arbitrarily
# ordered per trial (trial A's Syn1 could be trial B's Syn2). Build many
# synthetic trials from two known, distinguishable ground-truth synergies
# (an early-cycle bump and a late-cycle bump, each with its own muscle
# weighting), let syns_nmf scramble their per-trial order via independent
# random seeds, then check classify_kmeans() puts the SAME physiological
# synergy under the SAME label in every trial.
n_points_ck = 101
n_muscles_ck = 4
n_cycles_ck = 3
tt = np.linspace(0, 100, n_points_ck)


def _bump(center, width=10.0):
    return np.exp(-0.5 * ((tt - center) / width) ** 2)


P_early_true = _bump(25)   # ground truth: earlier in the cycle
P_late_true = _bump(75)    # ground truth: later in the cycle
M_early_true = np.array([1.0, 0.8, 0.05, 0.05])
M_late_true = np.array([0.05, 0.05, 1.0, 0.7])

ck_rng = np.random.default_rng(123)
n_trials_ck = 14
ck_results = {}
for ti in range(n_trials_ck):
    trial_name = f"T{ti + 1:02d}"
    Pa = np.clip(P_early_true + ck_rng.normal(0, 0.02, n_points_ck), 0, None)
    Pb = np.clip(P_late_true + ck_rng.normal(0, 0.02, n_points_ck), 0, None)
    Ma = np.clip(M_early_true + ck_rng.normal(0, 0.03, n_muscles_ck), 0.01, None)
    Mb = np.clip(M_late_true + ck_rng.normal(0, 0.03, n_muscles_ck), 0.01, None)

    Pa_full = np.clip(np.tile(Pa, n_cycles_ck) + ck_rng.normal(0, 0.01, n_points_ck * n_cycles_ck), 0, None)
    Pb_full = np.clip(np.tile(Pb, n_cycles_ck) + ck_rng.normal(0, 0.01, n_points_ck * n_cycles_ck), 0, None)

    V_ck = np.outer(Ma, Pa_full) + np.outer(Mb, Pb_full)
    V_ck_df = pd.DataFrame(V_ck.T, columns=[f"m{j}" for j in range(n_muscles_ck)])
    V_ck_df.insert(0, "time", np.arange(1, n_points_ck * n_cycles_ck + 1))

    # different seed per trial -> independent random NMF init -> Syn1/Syn2
    # order is naturally scrambled trial-to-trial, same as real independent runs
    ck_results[trial_name] = syns_nmf(V_ck_df, fixed_syns=2, runs=3, max_iter=400, seed=ti)

classified = classify_kmeans(ck_results, n_points=n_points_ck, clusters=2, seed=0)

check("classify_kmeans returns one entry per trial",
      set(classified.keys()) == set(ck_results.keys()))

n_combined = 0
n_consistent = 0
for trial_name, result in classified.items():
    check(f"{trial_name}: classification marked 'k-means'", result.classification == "k-means")
    if any("combined" in s for s in result.syn_names):
        n_combined += 1
        continue
    check(f"{trial_name}: has both Syn1 and Syn2", "Syn1" in result.syn_names and "Syn2" in result.syn_names)
    syn1 = result.P["Syn1"].to_numpy(dtype=float)[:n_points_ck]
    syn2 = result.P["Syn2"].to_numpy(dtype=float)[:n_points_ck]
    corr_1_early = np.corrcoef(syn1, P_early_true)[0, 1]
    corr_1_late = np.corrcoef(syn1, P_late_true)[0, 1]
    corr_2_early = np.corrcoef(syn2, P_early_true)[0, 1]
    corr_2_late = np.corrcoef(syn2, P_late_true)[0, 1]
    # classify_kmeans orders by ascending centre-of-activity, so Syn1 should
    # consistently be the EARLY bump and Syn2 the LATE bump, across all trials
    if corr_1_early > corr_1_late and corr_2_late > corr_2_early:
        n_consistent += 1

check(f"classify_kmeans: no more than 1/{n_trials_ck} trials left as 'combined' (got {n_combined})",
      n_combined <= 1)
check(f"classify_kmeans: Syn1==early/Syn2==late consistently across all classified trials "
      f"({n_consistent}/{n_trials_ck - n_combined})",
      n_consistent == n_trials_ck - n_combined)

# ---- group_synergy_summary aggregates to the correct ground-truth shape ----
group = group_synergy_summary(classified, n_points=n_points_ck)
check("group_synergy_summary has an entry for Syn1 and Syn2",
      set(group.keys()) == {"Syn1", "Syn2"})
check("group_synergy_summary Syn1 contributed by (n_trials_ck - combined) trials",
      group["Syn1"]["n_trials"] == n_trials_ck - n_combined)

corr_grp1_early = np.corrcoef(group["Syn1"]["mean_P"], P_early_true)[0, 1]
corr_grp2_late = np.corrcoef(group["Syn2"]["mean_P"], P_late_true)[0, 1]
check("group_synergy_summary: group-mean Syn1 pattern matches the early ground truth",
      corr_grp1_early > 0.95)
check("group_synergy_summary: group-mean Syn2 pattern matches the late ground truth",
      corr_grp2_late > 0.95)
check("group_synergy_summary: group-mean Syn1 muscle weighting matches early ground truth muscles",
      np.argmax(group["Syn1"]["mean_M"]) in (0, 1))  # M_early_true's dominant muscles are indices 0,1
check("group_synergy_summary: group-mean Syn2 muscle weighting matches late ground truth muscles",
      np.argmax(group["Syn2"]["mean_M"]) in (2, 3))  # M_late_true's dominant muscles are indices 2,3

check("group_synergy_summary: sd_M has same shape as mean_M and is non-negative",
      group["Syn1"]["sd_M"].shape == group["Syn1"]["mean_M"].shape and np.all(group["Syn1"]["sd_M"] >= 0))
check("group_synergy_summary: sd_P has same shape as mean_P and is non-negative",
      group["Syn1"]["sd_P"].shape == group["Syn1"]["mean_P"].shape and np.all(group["Syn1"]["sd_P"] >= 0))
check("group_synergy_summary: sd is nonzero given multiple contributing trials with real noise",
      group["Syn1"]["sd_M"].sum() > 0 and group["Syn1"]["sd_P"].sum() > 0)

# n_trials=1 edge case: sd must be all-zero (sample SD undefined), not NaN/error
single_trial_dict = {"T01": classified["T01"]}
group_single = group_synergy_summary(single_trial_dict, n_points=n_points_ck)
check("group_synergy_summary: single-trial input gives all-zero sd (no NaN/crash)",
      all(np.all(v["sd_M"] == 0) and np.all(v["sd_P"] == 0) for v in group_single.values()))

check("group_synergy_summary excludes 'combined' entries and returns {} if none classify cleanly",
      group_synergy_summary({}, n_points=n_points_ck) == {})

print("\nAll synergy regression checks passed.")
