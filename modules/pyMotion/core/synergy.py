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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from .batch_dataset import BatchTrial


@dataclass
class MusclesyneRgies:
    """Output of NMF decomposition for a single trial.

    M : np.ndarray, shape (n_muscles, n_syns) -- muscle weighting matrix
    P : pd.DataFrame, shape (n_time_points, n_syns + 1) -- activation
        (timing) patterns; first column is "time" (1-based sample index)
    R2 : pd.DataFrame -- columns "synergies", "R2"; one row per tested rank
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
