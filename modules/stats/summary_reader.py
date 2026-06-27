import os
import pandas as pd

SUMMARY_SUFFIX = "_summary.csv"
FREQ_SUFFIX = "_freq_analysis.csv"

# Human-readable labels for each metric column
METRIC_LABELS = {
    "min":      "Minimum",
    "max":      "Maximum",
    "mean":     "Mean",
    "median":   "Median",
    "std":      "Std Dev",
    "var":      "Variance",
    "ptp":      "Peak-to-Peak",
    "zc":       "Zero Crossings",
    "auc":      "AUC (IEMG)",
    "rms":      "RMS",
    "mav":      "MAV",
    "skewness": "Skewness",
    "kurtosis": "Kurtosis",
    "mnf":      "MNF (Hz)",
    "mdf":      "MDF (Hz)",
}

TD_METRICS = ["min", "max", "mean", "median", "std", "var",
              "ptp", "zc", "auc", "rms", "mav", "skewness", "kurtosis"]
FD_METRICS = ["mnf", "mdf"]


def load_workspace_summary(workspace_path: str) -> pd.DataFrame:
    """
    Scan workspace_path for participant subfolders containing _summary.csv.
    Merges _freq_analysis.csv (MNF/MDF averaged over analysis intervals) when present.
    Returns a combined DataFrame:
      Participant, Channel, min, max, mean, median, std, var, ptp, zc,
      auc, rms, mav, skewness, kurtosis [, mnf, mdf]
    Returns an empty DataFrame if no data found.
    """
    if not workspace_path or not os.path.isdir(workspace_path):
        return pd.DataFrame()

    frames = []
    for entry in sorted(os.scandir(workspace_path), key=lambda e: e.name):
        if not entry.is_dir():
            continue
        name = entry.name
        summary_path = os.path.join(entry.path, name + SUMMARY_SUFFIX)
        if not os.path.isfile(summary_path):
            continue
        try:
            df = pd.read_csv(summary_path, comment="#")
        except Exception:
            continue

        # Merge freq analysis if available: average MNF/MDF per channel
        freq_path = os.path.join(entry.path, name + FREQ_SUFFIX)
        if os.path.isfile(freq_path):
            try:
                fdf = pd.read_csv(freq_path, comment="#")
                # Column names written by exportFreqAnalysisCSV are "MNF (Hz)" / "MDF (Hz)"
                col_map = {}
                for col in fdf.columns:
                    lc = col.strip().lower()
                    if "mnf" in lc:
                        col_map[col] = "mnf"
                    elif "mdf" in lc:
                        col_map[col] = "mdf"
                    elif "channel" in lc:
                        col_map[col] = "Channel"
                fdf = fdf.rename(columns=col_map)
                needed = [c for c in ["Channel", "mnf", "mdf"] if c in fdf.columns]
                if "Channel" in needed and len(needed) > 1:
                    freq_avg = fdf.groupby("Channel")[
                        [c for c in needed if c != "Channel"]
                    ].mean().reset_index()
                    df = df.merge(freq_avg, on="Channel", how="left")
            except Exception:
                pass

        frames.append(df)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def available_metrics(df: pd.DataFrame) -> list[str]:
    """Return metric columns present in df, in display order."""
    order = TD_METRICS + FD_METRICS
    return [m for m in order if m in df.columns]


def available_channels(df: pd.DataFrame) -> list[str]:
    if df is None or df.empty or "Channel" not in df.columns:
        return []
    return sorted(df["Channel"].dropna().unique().tolist())
