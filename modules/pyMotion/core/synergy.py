"""
modules/pyMotion/core/synergy.py — muscle synergy analysis (NMF).

The NMF algorithm (syns_nmf) is adapted from F:\\AccMov_dev\\Myotion_dev\\musclesynergies_py
(Lee & Seung 1999 multiplicative updates, L2-column normalization per
Fevotte & Idier 2011, automatic rank selection via a linear MSE criterion
on the R^2 curve). Copied in rather than imported cross-repo so Myotion
does not depend on an unpackaged sibling project at runtime.

Two-step, explicit pipeline (kept separate on purpose -- see
emg-analysis-guard rules on signal-state separation):
  1. prepare_synergy_input()      -- rectify/envelope/normalize a BatchTrial
  2. build_synergy_input_matrix() -- time-normalize cycles into one "time"+
                                      muscle DataFrame
  3. syns_nmf() / extract_synergies() -- factorize into M (weighting) and
                                      P (timing) matrices

cossim(), CoA(), FWHM(), classify_kmeans() are ports of the same-named
functions from the R package this module is based on
(alesantuz/musclesyneRgies, R/cossim.R, R/CoA.R, R/FWHM.R, R/classify_kmeans.R
on GitHub) -- translated line-for-line from the literal R source, not
re-derived. Still missing from this port: subsetEMG, normEMG's multi-phase
(cycle_div) mode, HFD, Hurst, sMLE.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
from scipy.signal import butter, filtfilt
from scipy.cluster.vq import kmeans as _vq_kmeans, vq as _vq_assign

from .batch_dataset import BatchTrial


@dataclass
class MusclesyneRgies:
    """Output of NMF decomposition for a single trial.

    M : np.ndarray, shape (n_muscles, n_syns) -- muscle weighting matrix
    P : pd.DataFrame, shape (n_time_points, n_syns + 1) -- activation
        (timing) patterns; first column is "time" (1-based sample index)
    R2 : pd.DataFrame -- columns "synergies", "R2"; one row per tested rank
    classification : "none" until classify_kmeans() has relabeled this
        trial's synergies against a batch of other trials, then "k-means"
    """

    syns: int
    M: np.ndarray
    P: pd.DataFrame
    V: np.ndarray
    Vr: np.ndarray
    iterations: int
    R2: pd.DataFrame
    rank_type: str = "variable"
    muscle_names: list = field(default_factory=list)
    syn_names: list = field(default_factory=list)
    classification: str = "none"


# ---------------------------------------------------------------------------
# Synergy characterization metrics (R/cossim.R, R/CoA.R, R/FWHM.R)
# ---------------------------------------------------------------------------

def cossim(x, y) -> float:
    """Cosine similarity between two equal-length vectors, in [-1, 1].

    Direct port of R/cossim.R: crossprod(x, y) / sqrt(crossprod(x) * crossprod(y)).
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    denom = np.sqrt(np.dot(x, x) * np.dot(y, y))
    if denom == 0:
        return np.nan
    return float(np.dot(x, y) / denom)


def CoA(x, tol_rel: float = 1e-6) -> float:
    """Centre of activity of a periodic time series, via circular statistics.

    Direct port of R/CoA.R. `x` is treated as n samples spanning one full
    cycle (e.g. a synergy's activation pattern over 0-100%); returns a
    scalar in (0, n] marking where activity is concentrated on the 1..n
    axis, or NaN if the signal has no meaningful concentration (mean
    resultant length <= tol_rel, e.g. a flat or uniform signal).
    """
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n == 0:
        raise ValueError("x must be non-empty")

    # Angles at bin edges: 0, 2*pi/n, ..., 2*pi*(n-1)/n
    alpha = 2 * np.pi * (np.arange(n) / n)

    C = float(np.sum(x * np.cos(alpha)))
    S = float(np.sum(x * np.sin(alpha)))
    R = np.sqrt(C * C + S * S)

    denom = float(np.sum(np.abs(x)))
    if not np.isfinite(R) or not np.isfinite(denom) or denom == 0:
        return np.nan

    r = R / denom
    if r <= tol_rel:
        return np.nan

    ang = np.arctan2(S, C)  # [-pi, pi]
    if ang < 0:
        ang += 2 * np.pi

    # Map to 1-based coordinate (bin centres at integers 1..n), with wrap
    position = ang / (2 * np.pi) * n + 1
    position = ((position - 1) % n) + 1
    return float(position)


def FWHM(x, sub_minimum: bool = True) -> int:
    """Full width at half maximum: count of samples above 50% of peak.

    Direct port of R/FWHM.R. `sub_minimum` subtracts the signal's minimum
    before normalizing to its max, so the "half maximum" threshold is
    relative to the signal's own range rather than to zero.
    """
    x = np.asarray(x, dtype=float)
    if sub_minimum:
        x = x - x.min()
    peak = x.max()
    if peak == 0:
        return 0
    x = x / peak
    return int(np.sum(x > 0.5))


# ---------------------------------------------------------------------------
# Cross-trial synergy classification (R/classify_kmeans.R)
# ---------------------------------------------------------------------------

def _cycle_average_pattern(P: pd.DataFrame, n_points: int) -> np.ndarray:
    """Average a trial's concatenated-cycle activation patterns down to one
    representative cycle per synergy, amplitude-normalized to peak = 1.

    Port of the first `lapply(P, ...)` block in R/classify_kmeans.R -- R
    recovers `points` (points per cycle) from `max(y$time)`, which works
    there because normEMG() writes a *repeating* 1..n_points time column
    (confirmed from its source: the final `emg_interp$time <- 1:sum(cycle_div)`
    relies on R's vector recycling to repeat across all concatenated
    cycles). This Python port's build_synergy_input_matrix() deliberately
    uses a running 1..n_rows index instead, so the chart X-axis reads as one
    continuous multi-cycle timeline -- so n_points is passed in explicitly
    here rather than re-deriving it from a "time" column that means
    something different in this codebase.

    Returns shape (n_syns, n_points) -- one row per synergy.
    """
    syn_cols = [c for c in P.columns if c != "time"]
    arr = P[syn_cols].to_numpy(dtype=float)
    n_rows, n_syns = arr.shape
    if n_rows % n_points != 0:
        raise ValueError(f"P has {n_rows} rows, not a multiple of n_points={n_points}")
    n_cycles = n_rows // n_points
    mean_cycle = arr.reshape(n_cycles, n_points, n_syns).mean(axis=0)  # (n_points, n_syns)
    peak = mean_cycle.max(axis=0)
    peak = np.where(peak > 0, peak, 1.0)
    return (mean_cycle / peak).T  # (n_syns, n_points)


def _filter_activation_row(y: np.ndarray, n_points: int) -> np.ndarray:
    """4th-order zero-phase lowpass (cutoff 10, Nyquist = n_points/2), floor
    zeros to the smallest positive value, amplitude-normalize.

    Direct port of the per-row filtering step inside classify_kmeans()'s
    `data_P <- t(apply(data_P, 1, function(y) {...}))` block.
    """
    b, a = butter(4, 10 / (n_points / 2), btype="low")
    y = filtfilt(b, a, y)
    y = np.clip(y, 0, None)
    y = y - y.min()
    positive = y[y > 0]
    floor_val = positive.min() if len(positive) else 0.0
    y = np.where(y == 0, floor_val, y)
    peak = y.max()
    return y / peak if peak > 0 else y


def _kmeans_restarts(data: np.ndarray, k: int, n_init: int, seed):
    """k-means with multiple restarts, keeping the lowest-distortion run.

    R uses stats::kmeans(..., nstart = 20, algorithm = "Hartigan-Wong").
    scikit-learn (which offers more algorithm choices) is not a dependency
    of this project; scipy already is, and scipy.cluster.vq.kmeans's `iter`
    argument already means "run this many times, keep the lowest-distortion
    codebook" -- the same role as R's nstart, just with Lloyd's algorithm
    instead of Hartigan-Wong. Returns (labels, centroids); labels are
    1-based to match R's kmeans()$cluster.
    """
    centroids, _distortion = _vq_kmeans(data, k, iter=n_init, seed=seed)
    if len(centroids) < k:
        # A cluster collapsed to empty during Lloyd's iterations -- more
        # likely with scipy's algorithm than R's Hartigan-Wong, especially
        # on small test datasets. Pad by duplicating the last centroid so
        # callers always see exactly k clusters (that pair is then simply
        # redundant rather than causing a shape mismatch downstream).
        pad = np.tile(centroids[-1:], (k - len(centroids), 1))
        centroids = np.vstack([centroids, pad])
    labels, _dist = _vq_assign(data, centroids)
    return labels + 1, centroids


def _withinss(data: np.ndarray, labels_1based: np.ndarray, centroids: np.ndarray) -> float:
    total = 0.0
    for k in range(len(centroids)):
        members = data[labels_1based == k + 1]
        if len(members):
            total += float(np.sum((members - centroids[k]) ** 2))
    return total


def classify_kmeans(
    results: dict[str, MusclesyneRgies],
    n_points: int,
    clusters: Optional[int] = None,
    MSE_lim: float = 1e-3,
    seed: Optional[int] = None,
) -> dict[str, MusclesyneRgies]:
    """Classify synergies across trials so the same label means the same
    physiological synergy everywhere.

    syns_nmf() runs independently per trial, so a trial's "Syn1"/"Syn2"
    ordering has no cross-trial meaning -- trial A's Syn1 might be trial B's
    Syn2. This clusters synergies by activation-pattern shape (k-means on
    the filtered, cycle-averaged pattern), separately clusters by muscle
    weights as a cross-check, reconciles the two via CoA/FWHM geometry, and
    renumbers every trial's synergies by ascending centre-of-activity
    (Syn1 = earliest-active). Synergies whose pattern- and weight-based
    clusters disagree, or that duplicate within one trial, are kept but
    labeled "Syncombined_<n>" (R marks all of these "Syncombined" and
    tolerates the resulting duplicate column names; pandas does not, hence
    the numeric suffix here to keep every trial's columns unique).

    Per the R docs: needs "a sufficient amount of trials" (typically 10+)
    for the clustering to be meaningful -- this is not enforced here, just
    worth knowing before trusting the result on a handful of trials.

    n_points: points-per-cycle used when every trial's V matrix was built
    (build_synergy_input_matrix's n_points) -- see _cycle_average_pattern
    for why this must be passed explicitly rather than inferred.
    """
    if len(results) == 0:
        raise ValueError("results must contain at least one trial")

    muscle_counts = {name: r.M.shape[0] for name, r in results.items()}
    if len(set(muscle_counts.values())) != 1:
        raise ValueError(f"not all trials have the same number of muscles: {muscle_counts}")
    n_muscles = next(iter(muscle_counts.values()))

    # ---- per-trial-synergy mean cycle pattern + row bookkeeping -----------
    row_trial: list[str] = []
    P_rows: list[np.ndarray] = []
    M_rows: list[np.ndarray] = []
    for trial_name, result in results.items():
        mean_cycles = _cycle_average_pattern(result.P, n_points)  # (n_syns, n_points)
        for s in range(result.syns):
            row_trial.append(trial_name)
            P_rows.append(mean_cycles[s])
            M_rows.append(result.M[:, s])

    data_P = np.array(P_rows)  # (total_syns, n_points)
    data_M = np.array(M_rows)  # (total_syns, n_muscles)
    row_trial_arr = np.array(row_trial)

    # Filter+normalize the activation patterns used for clustering/geometry.
    data_P_filt = np.array([_filter_activation_row(row, n_points) for row in data_P])

    # ---- number of clusters (elbow on within-cluster SS), or user-fixed ---
    if clusters is None:
        withinss = np.empty(n_muscles)
        labels_by_k = {}
        for k in range(1, n_muscles + 1):
            labels_k, centroids_k = _kmeans_restarts(data_P_filt, k, n_init=20, seed=seed)
            withinss[k - 1] = _withinss(data_P_filt, labels_k, centroids_k)
            labels_by_k[k] = labels_k
        withinss = withinss - withinss.min()
        peak = withinss.max()
        if peak > 0:
            withinss = withinss / peak

        MSE = 100.0
        itr = 0
        while MSE > MSE_lim:
            itr += 1
            if itr == n_muscles - 1:
                break
            xx = np.arange(1, n_muscles - itr + 2, dtype=float)
            yy = withinss[itr - 1:]
            coeffs = np.polyfit(xx, yy, 1)
            lin = np.polyval(coeffs, xx)
            MSE = float(np.sum((lin - yy) ** 2) / len(yy))
        clust_num = itr
        clusters_P = labels_by_k[clust_num]
    else:
        clust_num = int(clusters)
        clusters_P, _ = _kmeans_restarts(data_P_filt, clust_num, n_init=20, seed=seed)

    clusters_M, _ = _kmeans_restarts(data_M, clust_num, n_init=20, seed=seed)

    # ---- per-row geometry (used for reconciliation and final ordering) ----
    FWHM_P = np.array([FWHM(row) for row in data_P_filt])
    CoA_P = np.array([CoA(row) for row in data_P_filt])

    # ---- reconcile the M-based clustering's labels onto the P-based ones --
    cluster_ids = np.arange(1, clust_num + 1)
    score_P, score_M = {}, {}
    for c in cluster_ids:
        mask_p, mask_m = clusters_P == c, clusters_M == c
        score_P[c] = float(FWHM_P[mask_p].mean() * CoA_P[mask_p].mean()) if mask_p.any() else np.nan
        score_M[c] = float(FWHM_P[mask_m].mean() * CoA_P[mask_m].mean()) if mask_m.any() else np.nan

    pairs = [
        (p, m, (score_P[p] - score_M[m]) ** 2)
        for p in cluster_ids for m in cluster_ids
        if np.isfinite(score_P[p]) and np.isfinite(score_M[m])
    ]
    pairs.sort(key=lambda t: t[2])
    seen_old = set()
    kept = []
    for p, m, _resid in pairs:
        if p in seen_old:
            continue
        seen_old.add(p)
        kept.append((p, m))

    old_complete = len(seen_old) == clust_num
    new_values = [m for _p, m in kept]
    new_unique = len(set(new_values)) == len(new_values)

    if old_complete and new_unique:
        m_to_p = {m: p for p, m in kept}
        clusters_M_reconciled = np.array([m_to_p.get(m, m) for m in clusters_M])
        discordant = clusters_P != clusters_M_reconciled
        concordant_P_labels = set(clusters_P[~discordant])
        if len(concordant_P_labels) < clust_num:
            # M-based classification doesn't cover every P-cluster among the
            # rows where the two agree -- unreliable, discard it (matches R:
            # "activation pattern- and muscle weight-based classification
            # don't match! Muscle weight-based classification discarded").
            final_P = clusters_P.astype(object)
        else:
            final_P = clusters_P.astype(object)
            final_P[discordant] = "combined"
    else:
        final_P = clusters_P.astype(object)

    # ---- final cluster-mean patterns, ordered by centre of activity -------
    mean_P_by_cluster = {}
    for c in cluster_ids:
        rows = data_P_filt[final_P == c]
        m = rows.mean(axis=0) if len(rows) else np.zeros(n_points)
        m = m - m.min()
        peak = m.max()
        mean_P_by_cluster[c] = (m / peak) if peak > 0 else m

    coa_per_cluster = {c: CoA(mean_P_by_cluster[c]) for c in cluster_ids}
    # Clusters with an undefined (NaN) CoA (no rows assigned) sort last.
    sorted_clusters = sorted(cluster_ids, key=lambda c: (np.isnan(coa_per_cluster[c]), coa_per_cluster[c]))
    old_to_new = {old: new for new, old in enumerate(sorted_clusters, start=1)}

    final_P = np.array(
        [old_to_new[c] if c != "combined" else "combined" for c in final_P], dtype=object
    )
    mean_P_final = {old_to_new[c]: mean_P_by_cluster[c] for c in cluster_ids}

    # ---- resolve within-trial duplicate cluster assignments ---------------
    row_index = np.arange(len(row_trial))
    for trial_name in dict.fromkeys(row_trial):  # preserve first-seen order
        trial_rows = row_index[row_trial_arr == trial_name]
        for syn in range(1, clust_num + 1):
            dup_rows = [i for i in trial_rows if final_P[i] == syn]
            if len(dup_rows) <= 1:
                continue
            P2 = mean_P_final[syn]
            best_i, best_r2 = None, -np.inf
            for i in dup_rows:
                P1 = data_P_filt[i]
                SST = float(np.sum((P1 - P1.mean()) ** 2))
                r2 = 1.0 - float(np.sum((P1 - P2) ** 2)) / SST if SST > 0 else -np.inf
                if r2 > best_r2:
                    best_r2, best_i = r2, i
            for i in dup_rows:
                if i != best_i:
                    final_P[i] = "combined"

    # ---- write classified labels back onto each trial's MusclesyneRgies ---
    classified: dict[str, MusclesyneRgies] = {}
    for trial_name, result in results.items():
        trial_rows = [i for i, t in enumerate(row_trial) if t == trial_name]
        new_syn_names = []
        combined_count = 0
        for i in trial_rows:
            label = final_P[i]
            if label == "combined":
                combined_count += 1
                new_syn_names.append(f"Syncombined_{combined_count}")
            else:
                new_syn_names.append(f"Syn{label}")

        new_P = result.P.copy()
        new_P.columns = ["time"] + new_syn_names
        classified[trial_name] = dataclasses.replace(
            result, P=new_P, syn_names=new_syn_names, classification="k-means",
        )

    return classified


def group_synergy_summary(classified: dict[str, MusclesyneRgies], n_points: int) -> dict:
    """Aggregate a classify_kmeans() batch into one mean M/P curve per
    synergy label -- "what does Syn1 look like across the whole cohort",
    the data this project's UI needs for a group-level plot (the Python
    equivalent of R/plot_classified_syns.R's data prep; this codebase
    renders its own Plotly charts rather than porting the ggplot calls).

    "Syncombined_*" labels are excluded: they don't have a consistent
    cross-trial identity to average by construction.

    Returns {label: {"muscle_names": [...], "mean_M"/"sd_M": np.ndarray
                      (n_muscles,), "mean_P"/"sd_P": np.ndarray (n_points,),
                      "n_trials": int}}, ordered by ascending synergy number.
    sd_* is all-zero when only one trial contributes that label (sample SD
    is undefined for n=1). Empty dict if `classified` has no non-"combined"
    labels at all.
    """
    real_labels = sorted(
        {name for r in classified.values() for name in r.syn_names if "combined" not in name},
        key=lambda s: int(s.replace("Syn", "")),
    )
    if not real_labels:
        return {}

    muscle_names = next(iter(classified.values())).muscle_names
    summary = {}
    for label in real_labels:
        M_rows, P_rows = [], []
        for result in classified.values():
            if label not in result.syn_names:
                continue
            s = result.syn_names.index(label)
            M_rows.append(result.M[:, s])
            P_rows.append(_cycle_average_pattern(result.P, n_points)[s])

        M_arr = np.array(M_rows)
        P_arr = np.array(P_rows)
        n_trials = len(M_rows)

        mean_M = M_arr.mean(axis=0)
        sd_M = M_arr.std(axis=0, ddof=1) if n_trials > 1 else np.zeros_like(mean_M)

        mean_P = P_arr.mean(axis=0)
        sd_P = P_arr.std(axis=0, ddof=1) if n_trials > 1 else np.zeros_like(mean_P)
        peak = mean_P.max()
        if peak > 0:
            # Scale sd_P by the same factor as mean_P so the shaded band
            # stays proportionally correct after peak-normalization.
            sd_P = sd_P / peak
            mean_P = mean_P / peak

        summary[label] = {
            "muscle_names": muscle_names,
            "mean_M": mean_M, "sd_M": sd_M,
            "mean_P": mean_P, "sd_P": sd_P,
            "n_trials": n_trials,
        }
    return summary


def syns_nmf(
    V: pd.DataFrame,
    R2_target: float = 0.01,
    runs: int = 5,
    max_iter: int = 1000,
    last_iter: int = 20,
    MSE_min: float = 1e-4,
    fixed_syns: Optional[int] = None,
    seed: Optional[int] = None,
) -> MusclesyneRgies:
    """Extract muscle synergies via Non-Negative Matrix Factorization.

    Parameters
    ----------
    V : pd.DataFrame
        Time-normalized EMG (output of build_synergy_input_matrix).
        First column must be named "time".
    seed : optional int
        Seed for the random initializations, for reproducible tests.
        Left unseeded by default, matching normal (non-deterministic) use.
    """
    if not isinstance(V, pd.DataFrame):
        raise TypeError("V must be a pandas DataFrame")
    if "time" not in V.columns:
        raise ValueError("V must contain a 'time' column as its first column")

    time = V["time"].to_numpy(dtype=float)
    muscle_names = [c for c in V.columns if c != "time"]

    # (muscles x time) -- transpose of the input data frame
    Vmat = V[muscle_names].to_numpy(dtype=float).T.copy()

    # Floor non-positive values to smallest positive value
    pos_min = Vmat[Vmat > 0].min() if np.any(Vmat > 0) else 1e-10
    Vmat[Vmat <= 0] = pos_min

    m, n = Vmat.shape  # muscles, time points
    V_mean = Vmat.mean()
    V_SST = float(np.sum((Vmat - V_mean) ** 2))

    eps = np.finfo(float).eps

    if fixed_syns is None:
        min_syns = 1
        max_syns = m - round(m / 4)
        rank_type = "variable"
    else:
        min_syns = max_syns = int(fixed_syns)
        rank_type = "fixed"

    R2_cross: list[float] = []
    M_list: list[np.ndarray] = []
    P_list: list[np.ndarray] = []
    Vr_list: list[np.ndarray] = []
    iters_list: list[int] = []

    rng = np.random.default_rng(seed)

    for r in range(min_syns, max_syns + 1):
        R2_best = -np.inf
        M_best = P_best = Vr_best = None
        iter_best = 0

        for _run in range(runs):
            P = rng.uniform(Vmat.min(), Vmat.max(), size=(r, n))
            M = rng.uniform(Vmat.min(), Vmat.max(), size=(m, r))

            MtV = M.T @ Vmat
            MtM = M.T @ M
            P = P * MtV / np.maximum(MtM @ P, eps)
            VPt = Vmat @ P.T
            PPt = P @ P.T
            M = M * VPt / np.maximum(M @ PPt, eps)

            Vr = M @ P
            R2_arr = [1.0 - float(np.sum((Vmat - Vr) ** 2)) / V_SST]

            l2 = np.sqrt((M ** 2).sum(axis=0))
            l2[l2 == 0] = eps
            M = M / l2
            P = P * l2[:, None]

            converged_at = 1
            for it in range(1, max_iter):
                MtV = M.T @ Vmat
                MtM = M.T @ M
                P = P * MtV / np.maximum(MtM @ P, eps)
                VPt = Vmat @ P.T
                PPt = P @ P.T
                M = M * VPt / np.maximum(M @ PPt, eps)

                Vr = M @ P
                r2 = 1.0 - float(np.sum((Vmat - Vr) ** 2)) / V_SST
                R2_arr.append(r2)

                l2 = np.sqrt((M ** 2).sum(axis=0))
                l2[l2 == 0] = eps
                M = M / l2
                P = P * l2[:, None]

                converged_at = it
                if (
                    it > last_iter
                    and R2_arr[it] - R2_arr[it - last_iter]
                    < R2_arr[it] * R2_target / 100.0
                ):
                    break

            run_R2 = R2_arr[converged_at]
            if run_R2 > R2_best:
                R2_best = run_R2
                M_best = M.copy()
                P_best = P.copy()
                Vr_best = (M @ P).copy()
                iter_best = converged_at

        R2_cross.append(R2_best)
        M_list.append(M_best)
        P_list.append(P_best)
        Vr_list.append(Vr_best)
        iters_list.append(iter_best)

    if fixed_syns is None:
        k = 0
        MSE = 100.0
        R2_arr_np = np.array(R2_cross)

        while MSE > MSE_min:
            k += 1
            if k >= max_syns - 1:
                break
            R2_sub = R2_arr_np[k - 1:]
            xs = np.arange(1, len(R2_sub) + 1, dtype=float)
            coeffs = np.polyfit(xs, R2_sub, 1)
            lin_vals = np.polyval(coeffs, xs)
            MSE = float(np.sum((lin_vals - R2_sub) ** 2) / len(R2_sub))

        syns_R2 = k
        idx = syns_R2 - 1
    else:
        syns_R2 = int(fixed_syns)
        idx = 0

    M_choice = M_list[idx]
    P_mat = P_list[idx]
    Vr_choice = Vr_list[idx]

    syn_cols = [f"Syn{i+1}" for i in range(syns_R2)]

    P_df = pd.DataFrame(
        np.column_stack([time, P_mat.T]),
        columns=["time"] + syn_cols,
    )

    R2_df = pd.DataFrame(
        {
            "synergies": np.arange(min_syns, max_syns + 1),
            "R2": R2_cross,
        }
    )

    return MusclesyneRgies(
        syns=int(syns_R2),
        M=M_choice,
        P=P_df,
        V=Vmat,
        Vr=Vr_choice,
        iterations=iters_list[idx],
        R2=R2_df,
        rank_type=rank_type,
        muscle_names=muscle_names,
        syn_names=syn_cols,
    )


# ---------------------------------------------------------------------------
# Myotion-side adapters: BatchTrial -> synergy-ready input
# ---------------------------------------------------------------------------

def prepare_synergy_input(
    trial: BatchTrial,
    bp_low=50.0,
    bp_high=450.0,
    bp_order=2,
    lp=6.0,
    lp_order=2,
    demean=True,
    amplitude_normalize=True,
) -> BatchTrial:
    """Rectify/envelope/normalize a BatchTrial's EMG for synergy extraction.

    Matches Myotion's own EMG envelope defaults (bandpass 50-450 Hz order 2,
    full-wave rectify, lowpass 6 Hz order 2), not musclesynergies_py's
    filt_emg defaults, to keep preprocessing consistent with the rest of the
    app. Returns a new BatchTrial; does not mutate the input.
    """
    out = trial.emg.copy()
    for ch in out.labels:
        data = np.asarray(out[ch], dtype=float)
        if demean:
            data = data - data.mean()
        out[ch] = data

        if bp_low and bp_high:
            out[ch] = out.bandpass(ch, bp_low, bp_high, bp_order)

        out[ch] = np.abs(out[ch])  # full-wave rectification

        if lp:
            out[ch] = out.lowpass(ch, lp, lp_order)

        # floor filtfilt ringing/overshoot below zero -- confirmed necessary
        # on real data (envelope can dip slightly negative near cycle edges)
        out[ch] = np.clip(out[ch], 0, None)

        if amplitude_normalize:
            peak = out[ch].max()
            if peak > 0:
                out[ch] = out[ch] / peak

    return BatchTrial(trial.name, out, trial.cycles)


def build_synergy_input_matrix(trial: BatchTrial, n_points: int = 101) -> pd.DataFrame:
    """Time-normalize every channel's cycles and assemble the "time"+muscle
    DataFrame that syns_nmf expects."""
    if len(trial.cycles) == 0:
        raise ValueError(f"trial '{trial.name}' has no cycles to normalize")

    columns = {
        ch: trial.emg.timeNormalizeCycles(ch, trial.cycles, n_points).flatten()
        for ch in trial.emg.labels
    }

    n_rows = len(trial.cycles) * n_points
    time_col = np.arange(1, n_rows + 1)
    return pd.DataFrame({"time": time_col, **columns})


def extract_synergies(trial: BatchTrial, n_points: int = 101, **nmf_kwargs) -> MusclesyneRgies:
    """Convenience: build_synergy_input_matrix() + syns_nmf().

    trial's EMG must already be prepared (see prepare_synergy_input) --
    this function does not preprocess the signal itself.
    """
    V = build_synergy_input_matrix(trial, n_points)
    return syns_nmf(V, **nmf_kwargs)
