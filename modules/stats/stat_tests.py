import numpy as np
import scipy.stats as ss


def normality_test(groups: dict) -> dict:
    """
    Run Shapiro-Wilk per group.
    Returns {group_name: {"stat": float, "p": float, "normal": bool}}.
    Groups with fewer than 3 finite values are marked as inconclusive.
    """
    results = {}
    for name, vals in groups.items():
        arr = np.array(vals, dtype=float)
        arr = arr[np.isfinite(arr)]
        if len(arr) < 3:
            results[name] = {"stat": float("nan"), "p": float("nan"), "normal": None}
        else:
            stat, p = ss.shapiro(arr)
            results[name] = {"stat": float(stat), "p": float(p), "normal": p > 0.05}
    return results


def _cohens_d(a: np.ndarray, b: np.ndarray) -> float:
    """Cohen's d for two independent samples, pooled std (Hedges' original
    pooling, not bias-corrected -- fine at typical EMG-study sample sizes)."""
    n1, n2 = len(a), len(b)
    if n1 < 2 or n2 < 2:
        return float("nan")
    pooled_var = ((n1 - 1) * a.var(ddof=1) + (n2 - 1) * b.var(ddof=1)) / (n1 + n2 - 2)
    pooled_std = np.sqrt(pooled_var)
    if pooled_std == 0:
        return 0.0
    return float((a.mean() - b.mean()) / pooled_std)


def _rank_biserial(a: np.ndarray, b: np.ndarray, u_stat: float) -> float:
    """Rank-biserial correlation, the standard effect size for Mann-Whitney U."""
    n1, n2 = len(a), len(b)
    if n1 == 0 or n2 == 0:
        return float("nan")
    return float(1.0 - (2.0 * u_stat) / (n1 * n2))


def _eta_squared(vals: list) -> float:
    """Eta-squared (SS_between / SS_total) for a one-way ANOVA design."""
    all_vals = np.concatenate(vals)
    if len(all_vals) == 0:
        return float("nan")
    grand_mean = all_vals.mean()
    ss_between = sum(len(v) * (v.mean() - grand_mean) ** 2 for v in vals)
    ss_total = float(np.sum((all_vals - grand_mean) ** 2))
    return float(ss_between / ss_total) if ss_total > 0 else 0.0


def _epsilon_squared(h_stat: float, vals: list) -> float:
    """Epsilon-squared (eta-squared_H), the standard effect size for
    Kruskal-Wallis: (H - k + 1) / (n - k)."""
    n = sum(len(v) for v in vals)
    k = len(vals)
    if n - k <= 0:
        return float("nan")
    return float((h_stat - k + 1) / (n - k))


def run_comparison(groups: dict, alpha: float = 0.05) -> dict:
    """
    Auto-select and run the appropriate statistical test for the given groups.

    groups: {group_label: [values]}

    Returns a dict with:
      - test_name: str
      - statistic: float
      - p_value: float
      - significant: bool
      - normality: per-group Shapiro results
      - all_normal: bool
      - effect_size: float
      - effect_size_name: str -- "Cohen's d" (2-group, parametric),
            "Rank-biserial r" (2-group, non-parametric), "Eta-squared"
            (>2 groups, parametric), or "Epsilon-squared" (>2 groups,
            non-parametric)
      - post_hoc: list of {Group A, Group B, p-value, significant}  (ANOVA only)
      - error: str  (if something goes wrong)
    """
    # Filter to groups with data
    clean = {k: np.array(v, dtype=float) for k, v in groups.items()}
    clean = {k: v[np.isfinite(v)] for k, v in clean.items() if len(v) > 0}
    group_names = list(clean.keys())

    result = {"groups": group_names}

    if len(clean) < 2:
        result["error"] = "At least 2 groups with data are required."
        return result

    norm_results = normality_test({k: v.tolist() for k, v in clean.items()})
    result["normality"] = norm_results
    all_normal = all(
        info["normal"] is True for info in norm_results.values()
    )
    result["all_normal"] = all_normal

    vals = [clean[k] for k in group_names]

    if len(vals) == 2:
        if all_normal:
            stat, p = ss.ttest_ind(vals[0], vals[1])
            result["test_name"] = "Independent t-test"
            result["effect_size"] = round(_cohens_d(vals[0], vals[1]), 4)
            result["effect_size_name"] = "Cohen's d"
        else:
            stat, p = ss.mannwhitneyu(vals[0], vals[1], alternative="two-sided")
            result["test_name"] = "Mann-Whitney U"
            result["effect_size"] = round(_rank_biserial(vals[0], vals[1], float(stat)), 4)
            result["effect_size_name"] = "Rank-biserial r"
        result["statistic"] = float(stat)
        result["p_value"] = float(p)
    else:
        if all_normal:
            stat, p = ss.f_oneway(*vals)
            result["test_name"] = "One-way ANOVA"
            result["statistic"] = float(stat)
            result["p_value"] = float(p)
            result["post_hoc"] = _tukey_hsd(clean, group_names, alpha)
            result["effect_size"] = round(_eta_squared(vals), 4)
            result["effect_size_name"] = "Eta-squared"
        else:
            stat, p = ss.kruskal(*vals)
            result["test_name"] = "Kruskal-Wallis"
            result["statistic"] = float(stat)
            result["p_value"] = float(p)
            result["effect_size"] = round(_epsilon_squared(float(stat), vals), 4)
            result["effect_size_name"] = "Epsilon-squared"

    result["significant"] = result.get("p_value", 1.0) < alpha
    return result


def _tukey_hsd(clean: dict, group_names: list, alpha: float) -> list:
    """Pairwise Tukey HSD using scipy.stats.tukey_hsd (scipy >= 1.8)."""
    try:
        res = ss.tukey_hsd(*[clean[k] for k in group_names])
        rows = []
        for i in range(len(group_names)):
            for j in range(i + 1, len(group_names)):
                p = float(res.pvalue[i, j])
                rows.append({
                    "Group A": group_names[i],
                    "Group B": group_names[j],
                    "p-value": round(p, 4),
                    "significant": p < alpha,
                })
        return rows
    except Exception:
        return []


def describe_groups(groups: dict) -> dict:
    """
    Return descriptive stats per group: mean, std, n, median.
    """
    out = {}
    for name, vals in groups.items():
        arr = np.array(vals, dtype=float)
        arr = arr[np.isfinite(arr)]
        if len(arr) == 0:
            out[name] = {"n": 0, "mean": float("nan"), "std": float("nan"),
                         "median": float("nan")}
        else:
            out[name] = {
                "n":      int(len(arr)),
                "mean":   float(arr.mean()),
                "std":    float(arr.std(ddof=1)) if len(arr) > 1 else 0.0,
                "median": float(np.median(arr)),
            }
    return out
