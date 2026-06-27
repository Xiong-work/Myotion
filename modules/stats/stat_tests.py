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
        else:
            stat, p = ss.mannwhitneyu(vals[0], vals[1], alternative="two-sided")
            result["test_name"] = "Mann-Whitney U"
        result["statistic"] = float(stat)
        result["p_value"] = float(p)
    else:
        if all_normal:
            stat, p = ss.f_oneway(*vals)
            result["test_name"] = "One-way ANOVA"
            result["statistic"] = float(stat)
            result["p_value"] = float(p)
            result["post_hoc"] = _tukey_hsd(clean, group_names, alpha)
        else:
            stat, p = ss.kruskal(*vals)
            result["test_name"] = "Kruskal-Wallis"
            result["statistic"] = float(stat)
            result["p_value"] = float(p)

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
