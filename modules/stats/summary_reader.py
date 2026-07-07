import os
import pandas as pd

SUMMARY_SUFFIX = "_summary.csv"
FREQ_SUFFIX = "_freq_analysis.csv"
CYCLE_SUMMARY_SUFFIX = "_summary_cycles.csv"
FREQ_EVENTS_SUFFIX = "_freq_analysis_events.csv"

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


def _freq_avg_by_channel(freq_path: str) -> pd.DataFrame | None:
    """Read a freq-analysis CSV (columns include a Channel column and MNF/MDF
    columns, whatever their exact header text -- both exportFreqAnalysisCSV's
    "_freq_analysis.csv" and the "Apply Events" batch's
    "_freq_analysis_events.csv" qualify) and average MNF/MDF per channel.
    Returns None if the file is missing/unreadable or lacks usable columns.
    """
    if not os.path.isfile(freq_path):
        return None
    try:
        fdf = pd.read_csv(freq_path, comment="#")
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
        if "Channel" not in needed or len(needed) < 2:
            return None
        return fdf.groupby("Channel")[[c for c in needed if c != "Channel"]].mean().reset_index()
    except Exception:
        return None


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

        freq_avg = _freq_avg_by_channel(os.path.join(entry.path, name + FREQ_SUFFIX))
        if freq_avg is not None:
            df = df.merge(freq_avg, on="Channel", how="left")

        frames.append(df)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def load_workspace_cycle_summary(workspace_path: str) -> pd.DataFrame:
    """
    Cycle-aware alternative to load_workspace_summary(): scan workspace_path
    for participant subfolders containing _summary_cycles.csv (per-cycle TD
    metrics, written by the Stats module's "Compute Cycle Summaries" button
    -- see batch_io.compute_cycle_td_summaries), averaging each participant's
    per-cycle rows down to one row per (Participant, Task, Channel) -- i.e.
    this reflects the actual cycles from Kinematics Inspection rather than
    the whole trial. Does not read or affect _summary.csv in any way.

    Merges _freq_analysis_events.csv (MNF/MDF per event segment, from the
    Frequency Analysis "Apply Events" batch action) the same way, averaged
    per (Participant, Channel).

    Returns a DataFrame: Participant, Task, Channel, min, max, mean, ...
    [, mnf, mdf]. Returns an empty DataFrame if no _summary_cycles.csv files
    are found.
    """
    if not workspace_path or not os.path.isdir(workspace_path):
        return pd.DataFrame()

    frames = []
    for entry in sorted(os.scandir(workspace_path), key=lambda e: e.name):
        if not entry.is_dir():
            continue
        name = entry.name
        cycles_path = os.path.join(entry.path, name + CYCLE_SUMMARY_SUFFIX)
        if not os.path.isfile(cycles_path):
            continue
        try:
            cdf = pd.read_csv(cycles_path, comment="#")
        except Exception:
            continue
        if "Channel" not in cdf.columns:
            continue

        group_cols = [c for c in ["Task", "Channel"] if c in cdf.columns]
        metric_cols = [c for c in TD_METRICS if c in cdf.columns]
        if not metric_cols:
            continue
        avg = cdf.groupby(group_cols)[metric_cols].mean().reset_index()
        avg.insert(0, "Participant", name)

        freq_avg = _freq_avg_by_channel(os.path.join(entry.path, name + FREQ_EVENTS_SUFFIX))
        if freq_avg is not None:
            avg = avg.merge(freq_avg, on="Channel", how="left")

        frames.append(avg)

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
