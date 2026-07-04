"""
modules/stats/pingouin_tests.py — pingouin-backed analysis engine for
externally imported data (modules/stats/dataset.py's ExternalDataset).

Unlike modules/stats/stat_tests.py (independent groups only, used by the
workspace-summary pipeline), this engine understands within-subject
(repeated-measures), between-subjects, and mixed designs, auto-selecting a
parametric or non-parametric omnibus test from a normality check, with a
pairwise post-hoc table alongside it.
"""

import numpy as np
import pandas as pd
import pingouin as pg


def _levels(df: pd.DataFrame, col: str) -> list:
    return sorted(df[col].dropna().unique().tolist())


def normality_by(df: pd.DataFrame, dv: str, group_col: str) -> dict:
    """Shapiro-Wilk per level of group_col. Levels with <3 values are inconclusive."""
    out = {}
    for level, sub in df.groupby(group_col):
        vals = sub[dv].dropna().to_numpy(dtype=float)
        if len(vals) < 3:
            out[str(level)] = {"W": float("nan"), "pval": float("nan"), "normal": None}
        else:
            res = pg.normality(vals)
            out[str(level)] = {
                "W": float(res["W"].iloc[0]),
                "pval": float(res["pval"].iloc[0]),
                "normal": bool(res["normal"].iloc[0]),
            }
    return out


def describe_cells(df: pd.DataFrame, dv: str, factors: list) -> list:
    """Descriptive stats (n, mean, std, median) per combination of factor levels."""
    factors = [f for f in factors if f]
    if not factors:
        s = df[dv].dropna()
        return [{"cell": "All", "n": int(s.count()), "mean": float(s.mean()),
                 "std": float(s.std()) if s.count() > 1 else 0.0,
                 "median": float(s.median())}]
    rows = []
    for key, sub in df.groupby(factors):
        s = sub[dv].dropna()
        label = " / ".join(str(k) for k in key) if isinstance(key, tuple) else str(key)
        rows.append({
            "cell": label, "n": int(s.count()),
            "mean": float(s.mean()) if s.count() else float("nan"),
            "std": float(s.std()) if s.count() > 1 else 0.0,
            "median": float(s.median()) if s.count() else float("nan"),
        })
    return rows


def run_analysis(df: pd.DataFrame, dv: str, subject: str,
                  within: str | None = None, between: str | None = None,
                  alpha: float = 0.05) -> dict:
    """
    Auto-select and run the appropriate omnibus test + pairwise post-hoc for
    the given design, using pingouin. Returns a dict with:
      - design: "within" | "between" | "mixed"
      - test_name: str
      - anova: list[dict]      (omnibus test result rows)
      - post_hoc: list[dict]   (pairwise comparisons, may be empty)
      - normality: dict        (per-level Shapiro results)
      - all_normal: bool
      - error: str             (present if something went wrong / design invalid)
    """
    if within is None and between is None:
        return {"error": "Choose a within-subject and/or between-subjects factor to run a test."}

    work = df[[c for c in [subject, within, between, dv] if c] ].dropna(subset=[dv])
    if work.empty:
        return {"error": "No data available for the selected outcome column."}

    try:
        if within and between:
            design = "mixed"
            normality = normality_by(work, dv, within)
            all_normal = all(v["normal"] is True for v in normality.values())
            anova = pg.mixed_anova(data=work, dv=dv, within=within, between=between, subject=subject)
            test_name = "Mixed ANOVA (within + between)"
            post_hoc = pg.pairwise_tests(
                data=work, dv=dv, within=within, between=between, subject=subject,
                padjust="holm",
            )
        elif within:
            design = "within"
            normality = normality_by(work, dv, within)
            all_normal = all(v["normal"] is True for v in normality.values())
            if all_normal:
                anova = pg.rm_anova(data=work, dv=dv, within=within, subject=subject, detailed=True)
                test_name = "Repeated-measures ANOVA"
            else:
                anova = pg.friedman(data=work, dv=dv, within=within, subject=subject)
                test_name = "Friedman test (non-parametric)"
            post_hoc = pg.pairwise_tests(
                data=work, dv=dv, within=within, subject=subject,
                parametric=all_normal, padjust="holm",
            )
        else:
            design = "between"
            normality = normality_by(work, dv, between)
            all_normal = all(v["normal"] is True for v in normality.values())
            n_levels = len(_levels(work, between))
            if all_normal:
                anova = pg.anova(data=work, dv=dv, between=between, detailed=True)
                test_name = "One-way ANOVA" if n_levels > 2 else "Independent t-test (via ANOVA)"
            else:
                anova = pg.kruskal(data=work, dv=dv, between=between)
                test_name = "Kruskal-Wallis" if n_levels > 2 else "Mann-Whitney U (via Kruskal-Wallis)"
            post_hoc = pg.pairwise_tests(
                data=work, dv=dv, between=between, parametric=all_normal, padjust="holm",
            )
    except Exception as e:
        return {"error": f"Analysis failed: {e}"}

    p_col = next((c for c in ("p-unc", "p_unc", "p-corr") if c in anova.columns), None)
    significant = bool((anova[p_col] < alpha).any()) if p_col else False

    return {
        "design": design,
        "test_name": test_name,
        "anova": anova.reset_index(drop=True).to_dict("records"),
        "post_hoc": post_hoc.reset_index(drop=True).to_dict("records") if post_hoc is not None else [],
        "normality": normality,
        "all_normal": all_normal,
        "significant": significant,
    }
