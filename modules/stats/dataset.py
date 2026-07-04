"""
modules/stats/dataset.py — generic external tabular data source for the Stats module.

Unlike the workspace-summary pipeline (one row per participant per channel,
compared via ad-hoc manual group tagging), externally prepared data commonly
already carries its own factor columns and may be a repeated-measures design
(e.g. multiple rows per subject for different time points). ExternalDataset
keeps that structure explicit instead of flattening it into Participant/Channel,
so it can be analyzed with pingouin's within/between-aware tests.
"""

import os
import pandas as pd


class ExternalDataset:
    """
    A loaded external file plus the user's column roles.

    subject_col: participant/row identifier (may repeat across rows if there
                 are repeated measurements per subject)
    within_col:  optional repeated-measures factor (e.g. "Stage": pre/mid/post)
    between_col: optional between-subjects grouping factor (e.g. "Position")
    dv_cols:     numeric outcome columns available to analyze
    """

    def __init__(self, df: pd.DataFrame, subject_col: str,
                 within_col: str | None, between_col: str | None,
                 dv_cols: list[str], source_label: str):
        self.df = df
        self.subject_col = subject_col
        self.within_col = within_col
        self.between_col = between_col
        self.dv_cols = dv_cols
        self.source_label = source_label

    def subjects(self) -> list:
        return sorted(self.df[self.subject_col].dropna().unique().tolist())


def read_table(path: str) -> pd.DataFrame:
    """Read a CSV or Excel file into a DataFrame."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xls"):
        return pd.read_excel(path)
    return pd.read_csv(path)


def infer_columns(df: pd.DataFrame) -> tuple[list[str], list[str]]:
    """
    Split df columns into (non_numeric_cols, numeric_cols), used to seed the
    import dialog's default subject/within/between/DV choices.
    """
    numeric = df.select_dtypes(include="number").columns.tolist()
    non_numeric = [c for c in df.columns if c not in numeric]
    return non_numeric, numeric
