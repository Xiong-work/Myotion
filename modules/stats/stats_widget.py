"""
modules/stats/stats_widget.py — Python-native Statistics Module.

Replaces the R/Shiny stats tab. Reads _summary.csv files written at
report-save time, supports interactive group assignment, auto-selects
the appropriate statistical test (with Shapiro-Wilk normality checking),
and renders interactive plotly charts via a local QWebEngineView.
"""

import os as _os
import csv as _csv
import json as _json
import pandas as pd

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QSplitter, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QComboBox, QLineEdit, QListWidget, QListWidgetItem,
    QTableWidget, QTableWidgetItem, QHeaderView, QTabWidget,
    QAbstractItemView, QFileDialog, QMessageBox, QDialog, QStackedWidget,
)

from .summary_reader import (
    load_workspace_summary, load_workspace_cycle_summary,
    available_metrics, available_channels, METRIC_LABELS,
)
from .stat_tests import run_comparison, describe_groups
from .chart_builder import build_chart
from .chart_view import StatsChartView
from .dataset import read_table, infer_columns
from .import_dialog import ImportColumnDialog
from .imported_panel import ImportedAnalysisPanel
from modules.pyMotion.core.batch_io import compute_cycle_td_summaries


# ── Main widget ───────────────────────────────────────────────────────────────

class StatsWidget(QWidget):
    """
    Python-native Statistical Analysis module.
    Replaces the R/Shiny stats tab with a fully local Qt + plotly workflow.

    Usage:
        widget.on_workspace_changed(path)  — call when a workspace is loaded/saved
    """

    CHART_TYPES = ["Box", "Bar (Mean±SD)", "Violin", "Strip"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._workspace_path: str | None = None
        self._df: pd.DataFrame | None = None
        self._data_source_label: str = "No data loaded"
        self._groups: dict[str, str] = {}           # {participant: user-typed group name}
        self._last_result: dict | None = None
        self._imported_panel: ImportedAnalysisPanel | None = None
        self._setup_ui()

    # ── public API ────────────────────────────────────────────────────────────

    def on_workspace_changed(self, path: str):
        """Called from main.py when a workspace is loaded or saved."""
        self._workspace_path = path
        self._reload_data()

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        outer = QSplitter(Qt.Horizontal)
        outer.setHandleWidth(2)
        outer.addWidget(self._build_left_panel())

        self._right_stack = QStackedWidget()
        self._right_stack.addWidget(self._build_right_panel())  # index 0: workspace view
        outer.addWidget(self._right_stack)

        outer.setSizes([230, 900])
        outer.setStretchFactor(0, 0)
        outer.setStretchFactor(1, 1)
        root.addWidget(outer)

    @staticmethod
    def _lbl(text: str, bold=False, color="#f8f8f2", size=12) -> QLabel:
        l = QLabel(text)
        l.setStyleSheet(
            f"color:{color};font-weight:{'bold' if bold else 'normal'};font-size:{size}px;"
        )
        return l

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(220)
        panel.setMaximumWidth(280)
        panel.setStyleSheet("background:#282a36;")
        vl = QVBoxLayout(panel)
        vl.setContentsMargins(8, 10, 8, 8)
        vl.setSpacing(6)

        vl.addWidget(self._lbl("Participants", bold=True))

        self._source_label = self._lbl(
            self._data_source_label, color="#6272a4", size=10
        )
        self._source_label.setWordWrap(True)
        vl.addWidget(self._source_label)

        tabs = QTabWidget()
        tabs.setStyleSheet(
            "QTabWidget::pane{border:none;background:#282a36;}"
            "QTabBar::tab{background:#1e1f28;color:#6272a4;padding:5px 10px;"
            "font-size:11px;}"
            "QTabBar::tab:selected{background:#44475a;color:#f8f8f2;}"
        )
        tabs.addTab(self._build_workspace_tab(), "Workspace")
        tabs.addTab(self._build_external_tab(), "External Data")
        vl.addWidget(tabs, stretch=1)

        return panel

    def _build_workspace_tab(self) -> QWidget:
        """Grouping + workspace-derived data actions: everything that reads
        from the currently loaded workspace path (self._workspace_path)."""
        tab = QWidget()
        vl = QVBoxLayout(tab)
        vl.setContentsMargins(0, 6, 0, 0)
        vl.setSpacing(6)

        group_hint = self._lbl(
            "Name a group, check which participants belong to it, then Add "
            "Group -- repeat for as many groups as needed. Participants "
            "never added to a group are left ungrouped.",
            color="#6272a4", size=10,
        )
        group_hint.setWordWrap(True)
        vl.addWidget(group_hint)

        self._group_name_edit = QLineEdit()
        self._group_name_edit.setPlaceholderText("Group name, e.g. Control")
        vl.addWidget(self._group_name_edit)

        vl.addWidget(self._lbl("Available participants", color="#6272a4", size=10))
        self._participant_list = QListWidget()
        self._participant_list.setStyleSheet(
            "QListWidget{background:#1e1f28;color:#f8f8f2;border:none;font-size:12px;}"
        )
        vl.addWidget(self._participant_list, stretch=1)

        btn_add_group = QPushButton("Add Group")
        btn_add_group.setStyleSheet(
            "QPushButton{background:#6272a4;color:#ffffff;border-radius:4px;"
            "padding:5px;font-size:12px;font-weight:bold;border:none;}"
            "QPushButton:hover{background:#7282b4;}"
        )
        btn_add_group.setCursor(Qt.PointingHandCursor)
        btn_add_group.clicked.connect(self._add_group)
        vl.addWidget(btn_add_group)

        vl.addWidget(self._lbl("Defined groups", color="#6272a4", size=10))
        self._groups_summary_list = QListWidget()
        self._groups_summary_list.setMaximumHeight(90)
        self._groups_summary_list.setStyleSheet(
            "QListWidget{background:#1e1f28;color:#f8f8f2;border:none;font-size:11px;}"
        )
        vl.addWidget(self._groups_summary_list)

        btn_clear_groups = QPushButton("Clear All Groups")
        btn_clear_groups.setStyleSheet(
            "QPushButton{background:#44475a;color:#f8f8f2;border-radius:4px;"
            "padding:5px;font-size:12px;border:none;}"
            "QPushButton:hover{background:#6272a4;}"
        )
        btn_clear_groups.setCursor(Qt.PointingHandCursor)
        btn_clear_groups.clicked.connect(self._clear_groups)
        vl.addWidget(btn_clear_groups)

        btn_refresh = QPushButton("Refresh Data")
        btn_refresh.setStyleSheet(
            "QPushButton{background:#44475a;color:#f8f8f2;border-radius:4px;"
            "padding:5px;font-size:12px;border:none;}"
            "QPushButton:hover{background:#6272a4;}"
        )
        btn_refresh.setCursor(Qt.PointingHandCursor)
        btn_refresh.clicked.connect(self._reload_data)
        vl.addWidget(btn_refresh)

        btn_cycle_summaries = QPushButton("Compute Cycle Summaries")
        btn_cycle_summaries.setStyleSheet(
            "QPushButton{background:#44475a;color:#f8f8f2;border-radius:4px;"
            "padding:5px;font-size:12px;border:none;}"
            "QPushButton:hover{background:#6272a4;}"
        )
        btn_cycle_summaries.setCursor(Qt.PointingHandCursor)
        btn_cycle_summaries.setToolTip(
            "Batch-compute per-cycle time-domain summaries (_summary_cycles.csv) "
            "for every participant folder in this workspace, using cycles/events "
            "exported from Kinematics Inspection. Required before Create Feature "
            "Table can include per-cycle data."
        )
        btn_cycle_summaries.clicked.connect(self._compute_cycle_summaries)
        vl.addWidget(btn_cycle_summaries)

        btn_feature_table = QPushButton("Create Feature Table…")
        btn_feature_table.setStyleSheet(
            "QPushButton{background:#44475a;color:#f8f8f2;border-radius:4px;"
            "padding:5px;font-size:12px;border:none;}"
            "QPushButton:hover{background:#6272a4;}"
        )
        btn_feature_table.setCursor(Qt.PointingHandCursor)
        btn_feature_table.clicked.connect(self._create_feature_table)
        vl.addWidget(btn_feature_table)

        return tab

    def _build_external_tab(self) -> QWidget:
        """Hand-imported external data files -- independent of any loaded
        workspace (see _import_external_file / _load_dataframe_into_import_flow)."""
        tab = QWidget()
        vl = QVBoxLayout(tab)
        vl.setContentsMargins(0, 6, 0, 0)
        vl.setSpacing(6)

        hint = self._lbl(
            "Import a CSV/Excel file with its own subject/within/between "
            "columns -- independent of any loaded workspace.",
            color="#6272a4", size=10,
        )
        hint.setWordWrap(True)
        vl.addWidget(hint)

        btn_import = QPushButton("Import Data File…")
        btn_import.setStyleSheet(
            "QPushButton{background:#44475a;color:#f8f8f2;border-radius:4px;"
            "padding:5px;font-size:12px;border:none;}"
            "QPushButton:hover{background:#6272a4;}"
        )
        btn_import.setCursor(Qt.PointingHandCursor)
        btn_import.clicked.connect(self._import_external_file)
        vl.addWidget(btn_import)

        vl.addWidget(self._lbl("Imported subjects", color="#6272a4", size=10))
        self._imported_subjects_list = QListWidget()
        self._imported_subjects_list.setStyleSheet(
            "QListWidget{background:#1e1f28;color:#f8f8f2;border:none;font-size:12px;}"
        )
        vl.addWidget(self._imported_subjects_list, stretch=1)

        return tab

    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
        panel.setStyleSheet("background:#282a36;")
        vl = QVBoxLayout(panel)
        vl.setContentsMargins(10, 10, 10, 10)
        vl.setSpacing(8)

        vl.addWidget(self._build_controls_bar())

        inner = QSplitter(Qt.Vertical)
        inner.setHandleWidth(4)
        inner.addWidget(self._build_chart_area())
        inner.addWidget(self._build_results_area())
        inner.setSizes([500, 200])
        inner.setStretchFactor(0, 1)
        inner.setStretchFactor(1, 0)
        vl.addWidget(inner, stretch=1)

        return panel

    def _build_controls_bar(self) -> QWidget:
        bar = QWidget()
        bar.setStyleSheet("background:#1e1f28;border-radius:6px;")
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(10, 6, 10, 6)
        hl.setSpacing(10)

        hl.addWidget(self._lbl("Metric:"))
        self._metric_combo = QComboBox()
        self._metric_combo.setMinimumWidth(140)
        self._style_combo(self._metric_combo)
        self._metric_combo.currentIndexChanged.connect(self._on_controls_changed)
        hl.addWidget(self._metric_combo)

        hl.addWidget(self._lbl("Channel:"))
        self._channel_combo = QComboBox()
        self._channel_combo.setMinimumWidth(150)
        self._style_combo(self._channel_combo)
        self._channel_combo.currentIndexChanged.connect(self._on_controls_changed)
        hl.addWidget(self._channel_combo)

        hl.addWidget(self._lbl("Chart:"))
        self._chart_type_combo = QComboBox()
        for t in self.CHART_TYPES:
            self._chart_type_combo.addItem(t)
        self._style_combo(self._chart_type_combo)
        self._chart_type_combo.currentIndexChanged.connect(self._on_controls_changed)
        hl.addWidget(self._chart_type_combo)

        hl.addStretch()

        self._btn_run = QPushButton("Run Statistical Test")
        self._btn_run.setStyleSheet(
            "QPushButton{background:#6272a4;color:#ffffff;border-radius:5px;"
            "padding:5px 14px;font-size:12px;font-weight:bold;border:none;}"
            "QPushButton:hover{background:#7282b4;}"
        )
        self._btn_run.setCursor(Qt.PointingHandCursor)
        self._btn_run.clicked.connect(self._run_stats)
        hl.addWidget(self._btn_run)

        return bar

    @staticmethod
    def _style_combo(cb: QComboBox):
        cb.setStyleSheet(
            "QComboBox{background:#44475a;color:#f8f8f2;border-radius:4px;"
            "padding:4px 8px;font-size:12px;border:none;}"
            "QComboBox::drop-down{border:none;}"
            "QComboBox QAbstractItemView{background:#44475a;color:#f8f8f2;"
            "selection-background-color:#6272a4;border:none;}"
        )

    def _build_chart_area(self) -> QWidget:
        container = QWidget()
        vl = QVBoxLayout(container)
        vl.setContentsMargins(0, 0, 0, 0)
        self._chart_view = StatsChartView(container)
        vl.addWidget(self._chart_view)
        return container

    def _build_results_area(self) -> QWidget:
        container = QWidget()
        container.setStyleSheet("background:#1e1f28;border-radius:6px;")
        vl = QVBoxLayout(container)
        vl.setContentsMargins(10, 8, 10, 8)
        vl.setSpacing(6)

        top = QHBoxLayout()
        top.addWidget(self._lbl("Statistical Results", bold=True))
        top.addStretch()
        self._btn_export = QPushButton("Export CSV")
        self._btn_export.setStyleSheet(
            "QPushButton{background:#44475a;color:#f8f8f2;border-radius:4px;"
            "padding:4px 10px;font-size:11px;border:none;}"
            "QPushButton:hover{background:#6272a4;}"
        )
        self._btn_export.setCursor(Qt.PointingHandCursor)
        self._btn_export.clicked.connect(self._export_results)
        top.addWidget(self._btn_export)
        vl.addLayout(top)

        self._result_summary = QLabel("")
        self._result_summary.setWordWrap(True)
        self._result_summary.setTextFormat(Qt.RichText)
        self._result_summary.setStyleSheet("color:#f8f8f2;font-size:12px;")
        vl.addWidget(self._result_summary)

        self._result_table = QTableWidget()
        self._result_table.setStyleSheet(
            "QTableWidget{background:#282a36;color:#f8f8f2;border:none;"
            "gridline-color:#44475a;font-size:11px;}"
            "QHeaderView::section{background:#1e1f28;color:#6272a4;"
            "border:none;padding:4px;}"
        )
        self._result_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._result_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._result_table.horizontalHeader().setStretchLastSection(True)
        vl.addWidget(self._result_table, stretch=1)

        return container

    # ── data ──────────────────────────────────────────────────────────────────

    def _reload_data(self):
        self._right_stack.setCurrentIndex(0)
        if not self._workspace_path:
            self._df = None
            self._groups.clear()
            self._set_source_label("No data loaded")
            self._refresh_participant_list()
            self._refresh_combos()
            self._chart_view.show_placeholder("No workspace loaded.")
            return
        self._df = load_workspace_summary(self._workspace_path)
        self._groups = self._load_persisted_groups()
        self._set_source_label(f"Workspace: {_os.path.basename(self._workspace_path)}")
        self._refresh_participant_list()
        self._refresh_combos()
        self._update_chart()

    # ── group persistence ────────────────────────────────────────────────────
    # Group assignments are saved to <workspace>/stats_groups.json on every
    # change and reloaded on _reload_data(), so the Add-Group work in this
    # tab survives across sessions instead of needing to be redone each time.

    def _groups_path(self):
        if not self._workspace_path:
            return None
        return _os.path.join(self._workspace_path, "stats_groups.json")

    def _load_persisted_groups(self) -> dict:
        path = self._groups_path()
        if not path or not _os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_persisted_groups(self):
        path = self._groups_path()
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(self._groups, f, indent=2)
        except Exception:
            pass

    def _set_source_label(self, text: str):
        self._data_source_label = text
        self._source_label.setText(text)

    def _import_external_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Data File", "", "Data Files (*.csv *.xlsx *.xls)"
        )
        if not path:
            return

        try:
            df = read_table(path)
        except Exception as e:
            QMessageBox.critical(self, "Import Error", str(e))
            return

        self._load_dataframe_into_import_flow(df, _os.path.basename(path))

    def _load_dataframe_into_import_flow(self, df: pd.DataFrame, source_label: str):
        """Shared tail end of _import_external_file() and
        _create_feature_table(): pick subject/within/between/DV roles via
        ImportColumnDialog, then hand the result to ImportedAnalysisPanel --
        so a feature table built in-memory from the workspace gets the same
        pingouin-backed within/between/mixed analysis (with effect sizes and
        Holm-corrected pairwise tests) as a hand-imported file, instead of a
        second parallel analysis path."""
        non_numeric, numeric = infer_columns(df)
        if not numeric:
            QMessageBox.warning(self, "Import", "No numeric columns found in this data.")
            return

        dialog = ImportColumnDialog(df, non_numeric, numeric, source_label, self)
        if dialog.exec() != QDialog.Accepted:
            return

        dataset = dialog.dataset()
        if dataset is None:
            return

        self._workspace_path = None
        self._df = None
        self._set_source_label(f"Imported: {dataset.source_label}")
        self._refresh_participant_list_for_subjects(dataset.subjects())

        if self._imported_panel is not None:
            self._right_stack.removeWidget(self._imported_panel)
            self._imported_panel.deleteLater()
        self._imported_panel = ImportedAnalysisPanel(dataset)
        self._right_stack.addWidget(self._imported_panel)
        self._right_stack.setCurrentWidget(self._imported_panel)

    def _compute_cycle_summaries(self):
        """Batch-compute per-cycle time-domain summaries for every
        participant folder in the current workspace (see
        batch_io.compute_cycle_td_summaries) -- purely disk-based, reads
        each participant's own already-exported _emg_processed.csv +
        _Events.csv and writes _summary_cycles.csv next to them. Doesn't
        touch _summary.csv or the chart/group-comparison view (self._df)."""
        if not self._workspace_path:
            QMessageBox.information(
                self, "Compute Cycle Summaries", "Load a workspace first.",
            )
            return

        saved_paths, warnings = compute_cycle_td_summaries(self._workspace_path)
        if warnings:
            QMessageBox.warning(self, "Compute Cycle Summaries", "\n".join(warnings))
        if saved_paths:
            QMessageBox.information(
                self, "Compute Cycle Summaries",
                "Per-cycle time-domain summary saved for {} participant(s).".format(
                    len(saved_paths)
                ),
            )
        elif not warnings:
            QMessageBox.information(
                self, "Compute Cycle Summaries",
                "No participants with a saved report (_emg_processed.csv) found.",
            )

    def _build_feature_table_from_cycle_summary(self, cycle_df: pd.DataFrame) -> pd.DataFrame:
        """Wide-format (SPSS-style) feature table: one row per Participant,
        one column per Channel x metric (e.g. "Ch1_rms", "Ch1_mav", ...) --
        or "Task_Channel_metric" when cycle_df spans more than one task --
        averaged across that participant's cycles/segments (see
        summary_reader.load_workspace_cycle_summary). Participants missing
        a given channel/metric get NaN there rather than being dropped."""
        metric_cols = available_metrics(cycle_df)
        has_task = "Task" in cycle_df.columns and cycle_df["Task"].nunique() > 1
        pivot_cols = ["Task", "Channel"] if has_task else ["Channel"]
        wide = cycle_df.pivot_table(index="Participant", columns=pivot_cols, values=metric_cols)
        if has_task:
            wide.columns = [
                "{}_{}_{}".format(task, chan, metric) for metric, task, chan in wide.columns
            ]
        else:
            wide.columns = ["{}_{}".format(chan, metric) for metric, chan in wide.columns]
        return wide.reset_index()

    def _merge_cci_export(self, wide_df: pd.DataFrame, path: str) -> pd.DataFrame:
        """Left-join one Advanced EMG CCI export (columns: Trial, "CCI (A vs
        B)") onto wide_df by Participant. Trial values are stripped of any
        "Group/" prefix (Advanced EMG's grouped-dataset trial naming) before
        joining, since the feature table is keyed by bare Participant name."""
        try:
            cci_df = pd.read_csv(path)
        except Exception as e:
            QMessageBox.warning(
                self, "Import CCI", "Could not read {}: {}".format(_os.path.basename(path), e)
            )
            return wide_df

        if "Trial" not in cci_df.columns:
            QMessageBox.warning(
                self, "Import CCI",
                "{} doesn't look like a CCI export (no 'Trial' column).".format(
                    _os.path.basename(path)
                ),
            )
            return wide_df

        cci_cols = [c for c in cci_df.columns if c != "Trial"]
        if not cci_cols:
            return wide_df

        cci_df = cci_df.copy()
        cci_df["Participant"] = cci_df["Trial"].astype(str).str.rsplit("/", n=1).str[-1]
        return wide_df.merge(cci_df[["Participant"] + cci_cols], on="Participant", how="left")

    def _create_feature_table(self):
        """Build a wide-format (SPSS-style) feature table straight from each
        participant's own on-disk exports -- _summary_cycles.csv (see
        "Compute Cycle Summaries") plus optional _freq_analysis_events.csv,
        averaged per participant (summary_reader.load_workspace_cycle_
        summary) -- not the whole-trial-only self._df, since cycles/events
        are what should actually be summarized here. Optionally merges in
        CCI export(s) from Advanced EMG, then hands the result to the same
        subject/within/between/DV role picker + pingouin-backed analysis
        panel a hand-imported file gets (see _load_dataframe_into_import_flow)."""
        if not self._workspace_path:
            QMessageBox.information(
                self, "Create Feature Table", "Load a workspace first (Refresh Data).",
            )
            return

        cycle_df = load_workspace_cycle_summary(self._workspace_path)
        if cycle_df.empty:
            QMessageBox.information(
                self, "Create Feature Table",
                "No _summary_cycles.csv files found. Run \"Compute Cycle "
                "Summaries\" first (requires each participant's saved report "
                "and exported cycle events from Kinematics Inspection).",
            )
            return

        feature_df = self._build_feature_table_from_cycle_summary(cycle_df)

        # Carry over the groups defined in the "Workspace" tab (Add Group) as
        # a real "Group" column, so it shows up as a Between-subjects factor
        # choice in the next dialog -- without this, groups assigned here
        # would otherwise have no way to reach the pingouin analysis.
        if self._groups:
            feature_df = feature_df.copy()
            feature_df["Group"] = feature_df["Participant"].map(self._groups).fillna("Ungrouped")

        reply = QMessageBox.question(
            self, "Create Feature Table",
            "Merge in Co-contraction Index (CCI) export(s) from Advanced EMG?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            paths, _ = QFileDialog.getOpenFileNames(
                self, "Select CCI Export CSV(s)", "", "CSV Files (*.csv)"
            )
            for path in paths:
                feature_df = self._merge_cci_export(feature_df, path)

        self._load_dataframe_into_import_flow(feature_df, "Feature Table (workspace)")

    def _refresh_participant_list(self):
        """Rebuild the "Available participants" checklist from the current
        workspace summary -- shows only participants not yet assigned to a
        group (an assigned one drops out once Add Group is clicked, so the
        next group's checklist doesn't re-offer it), and drops assignments
        for participant names no longer present in the data."""
        self._participant_list.clear()

        if self._df is None or self._df.empty:
            self._groups.clear()
            self._refresh_groups_summary()
            return

        participants = sorted(self._df["Participant"].dropna().unique().tolist())
        self._groups = {n: g for n, g in self._groups.items() if n in participants}
        for name in participants:
            if name in self._groups:
                continue
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self._participant_list.addItem(item)
        self._refresh_groups_summary()

    def _refresh_participant_list_for_subjects(self, subjects: list):
        """Read-only subject list shown in the "External Data" tab --
        grouping for imported data comes from the chosen within/between
        factor columns, not manual per-subject tagging, so this is display
        only and doesn't touch the "Workspace" tab's grouping state."""
        self._imported_subjects_list.clear()
        for name in subjects:
            item = QListWidgetItem(str(name))
            item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
            self._imported_subjects_list.addItem(item)

    def _refresh_groups_summary(self):
        self._groups_summary_list.clear()
        counts = {}
        for group in self._groups.values():
            counts[group] = counts.get(group, 0) + 1
        for group, n in counts.items():
            self._groups_summary_list.addItem(f"{group}  ({n} participant{'s' if n != 1 else ''})")

    def _add_group(self):
        group_name = self._group_name_edit.text().strip()
        if not group_name:
            QMessageBox.information(self, "Add Group", "Enter a group name first.")
            return

        checked = [
            self._participant_list.item(i).text()
            for i in range(self._participant_list.count())
            if self._participant_list.item(i).checkState() == Qt.Checked
        ]
        if not checked:
            QMessageBox.information(self, "Add Group", "Check at least one participant.")
            return

        for name in checked:
            self._groups[name] = group_name
        self._group_name_edit.clear()
        self._save_persisted_groups()
        self._refresh_participant_list()
        self._update_chart()

    def _clear_groups(self):
        self._groups.clear()
        self._save_persisted_groups()
        self._refresh_participant_list()
        self._update_chart()

    def _refresh_combos(self):
        if self._df is None or self._df.empty:
            self._metric_combo.clear()
            self._channel_combo.clear()
            return

        prev_metric = self._metric_combo.currentText()
        prev_channel = self._channel_combo.currentText()

        for cb in (self._metric_combo, self._channel_combo):
            cb.blockSignals(True)

        self._metric_combo.clear()
        for m in available_metrics(self._df):
            self._metric_combo.addItem(METRIC_LABELS.get(m, m), userData=m)
        idx = self._metric_combo.findText(prev_metric)
        if idx >= 0:
            self._metric_combo.setCurrentIndex(idx)

        self._channel_combo.clear()
        self._channel_combo.addItem("All Channels", userData=None)
        for ch in available_channels(self._df):
            self._channel_combo.addItem(ch, userData=ch)
        idx = self._channel_combo.findText(prev_channel)
        if idx >= 0:
            self._channel_combo.setCurrentIndex(idx)

        for cb in (self._metric_combo, self._channel_combo):
            cb.blockSignals(False)

    # ── event handlers ────────────────────────────────────────────────────────

    def _on_controls_changed(self):
        self._update_chart()

    # ── chart ─────────────────────────────────────────────────────────────────

    def _current_metric(self) -> str | None:
        return self._metric_combo.currentData()

    def _current_channels(self) -> list:
        ch = self._channel_combo.currentData()
        return [ch] if ch is not None else []

    def _df_with_groups(self) -> pd.DataFrame | None:
        if self._df is None or self._df.empty:
            return None
        df = self._df.copy()
        df["Group"] = df["Participant"].map(self._groups).fillna("None")
        return df

    def _update_chart(self):
        metric = self._current_metric()
        if not metric:
            self._chart_view.show_placeholder(
                "No metrics available — save a report first."
            )
            return

        df = self._df_with_groups()
        if df is None:
            self._chart_view.show_placeholder(
                "No data. Load a workspace and save participant reports."
            )
            return

        if df[df["Group"] != "None"].empty:
            self._chart_view.show_placeholder(
                "Assign participants to groups using the buttons on the left."
            )
            return

        fig = build_chart(
            df=df,
            metric=metric,
            channels=self._current_channels(),
            chart_type=self._chart_type_combo.currentText(),
        )
        if fig is None:
            self._chart_view.show_placeholder("No data for current selection.")
            return
        self._chart_view.show_figure(fig)

    # ── statistical test ──────────────────────────────────────────────────────

    def _run_stats(self):
        metric = self._current_metric()
        if not metric:
            self._result_summary.setText("No metric selected.")
            return

        df = self._df_with_groups()
        if df is None:
            return

        channels = self._current_channels()
        if channels:
            df = df[df["Channel"].isin(channels)]

        assigned = df[df["Group"] != "None"]
        if assigned.empty:
            self._result_summary.setText("Assign participants to groups first.")
            return

        groups: dict[str, list] = {}
        for _, row in assigned.iterrows():
            g = row["Group"]
            v = row.get(metric)
            if pd.notna(v):
                groups.setdefault(g, []).append(float(v))

        if len(groups) < 2:
            self._result_summary.setText(
                "Need at least 2 groups with data to run a test."
            )
            return

        result = run_comparison(groups)
        self._last_result = {
            "result": result,
            "metric": metric,
            "channels": channels,
            "groups": groups,
        }
        self._display_results(result, groups)

    def _display_results(self, result: dict, groups: dict):
        if "error" in result:
            self._result_summary.setText(result["error"])
            self._result_table.setRowCount(0)
            return

        test   = result.get("test_name", "")
        p      = result.get("p_value", float("nan"))
        stat   = result.get("statistic", float("nan"))
        sig    = result.get("significant", False)
        normal = result.get("all_normal", True)
        eff        = result.get("effect_size")
        eff_name   = result.get("effect_size_name", "")

        sig_html = (
            "<span style='color:#26de81'><b>✔ Significant</b></span>"
            if sig else
            "<span style='color:#fc5c65'><b>✘ Not significant</b></span>"
        )
        norm_note = (
            "Normality: all groups passed Shapiro-Wilk → parametric test."
            if normal else
            "Normality: ≥1 group failed Shapiro-Wilk → non-parametric test."
        )
        eff_line = ""
        if eff is not None:
            eff_line = f"<br>{eff_name} = {eff:.4f}"
        self._result_summary.setText(
            f"<b>{test}</b>: statistic = {stat:.4f},  p = {p:.4f}  |  {sig_html}{eff_line}<br>"
            f"<span style='color:#6272a4;font-size:11px'>{norm_note}</span>"
        )

        desc      = describe_groups(groups)
        post_hoc  = result.get("post_hoc", [])

        if post_hoc:
            cols = ["Group A", "Group B", "p-value", "Significant"]
            self._result_table.setColumnCount(len(cols))
            self._result_table.setHorizontalHeaderLabels(cols)
            self._result_table.setRowCount(len(post_hoc))
            for r, row in enumerate(post_hoc):
                self._result_table.setItem(r, 0, QTableWidgetItem(row["Group A"]))
                self._result_table.setItem(r, 1, QTableWidgetItem(row["Group B"]))
                self._result_table.setItem(r, 2, QTableWidgetItem(str(row["p-value"])))
                self._result_table.setItem(
                    r, 3, QTableWidgetItem("Yes" if row["significant"] else "No")
                )
        else:
            cols = ["Group", "N", "Mean", "Std Dev", "Median"]
            self._result_table.setColumnCount(len(cols))
            self._result_table.setHorizontalHeaderLabels(cols)
            self._result_table.setRowCount(len(desc))
            for r, (g, d) in enumerate(desc.items()):
                self._result_table.setItem(r, 0, QTableWidgetItem(g))
                self._result_table.setItem(r, 1, QTableWidgetItem(str(d["n"])))
                self._result_table.setItem(r, 2, QTableWidgetItem(f"{d['mean']:.4f}"))
                self._result_table.setItem(r, 3, QTableWidgetItem(f"{d['std']:.4f}"))
                self._result_table.setItem(r, 4, QTableWidgetItem(f"{d['median']:.4f}"))

        self._result_table.resizeColumnsToContents()

    # ── export ────────────────────────────────────────────────────────────────

    def _export_results(self):
        if not self._last_result:
            QMessageBox.information(
                self, "Export", "Run a statistical test first."
            )
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Results", "", "CSV Files (*.csv)"
        )
        if not path:
            return

        result   = self._last_result["result"]
        metric   = self._last_result["metric"]
        groups   = self._last_result["groups"]
        desc     = describe_groups(groups)
        post_hoc = result.get("post_hoc", [])
        label    = METRIC_LABELS.get(metric, metric)

        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = _csv.writer(f)
                w.writerow(["Metric", label])
                w.writerow(["Test", result.get("test_name", "")])
                w.writerow(["Statistic", f"{result.get('statistic', float('nan')):.6f}"])
                w.writerow(["p-value",   f"{result.get('p_value',   float('nan')):.6f}"])
                w.writerow(["Significant", "Yes" if result.get("significant") else "No"])
                if result.get("effect_size") is not None:
                    w.writerow([result.get("effect_size_name", "Effect size"),
                                f"{result['effect_size']:.6f}"])
                w.writerow([])
                w.writerow(["Group", "N", "Mean", "Std Dev", "Median"])
                for g, d in desc.items():
                    w.writerow([g, d["n"],
                                f"{d['mean']:.6f}",
                                f"{d['std']:.6f}",
                                f"{d['median']:.6f}"])
                if post_hoc:
                    w.writerow([])
                    w.writerow(["Post-hoc (Tukey HSD)"])
                    w.writerow(["Group A", "Group B", "p-value", "Significant"])
                    for row in post_hoc:
                        w.writerow([row["Group A"], row["Group B"],
                                    row["p-value"],
                                    "Yes" if row["significant"] else "No"])
        except Exception as e:
            QMessageBox.critical(self, "Export Error", str(e))
