"""
modules/stats/stats_widget.py — Python-native Statistics Module.

Replaces the R/Shiny stats tab. Reads _summary.csv files written at
report-save time, supports interactive group assignment, auto-selects
the appropriate statistical test (with Shapiro-Wilk normality checking),
and renders interactive plotly charts via a local QWebEngineView.
"""

import csv as _csv
import pandas as pd

from PySide6.QtCore import Qt, QUrl, QByteArray, QBuffer, QIODevice, Signal
from PySide6.QtWidgets import (
    QWidget, QSplitter, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QComboBox, QTreeWidget, QTreeWidgetItem,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QFileDialog, QMessageBox,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import (
    QWebEngineProfile, QWebEnginePage,
    QWebEngineUrlSchemeHandler, QWebEngineUrlRequestJob,
)

from .summary_reader import (
    load_workspace_summary, available_metrics, available_channels, METRIC_LABELS,
)
from .stat_tests import run_comparison, describe_groups
from .chart_builder import build_chart, figure_to_html, empty_html


# ── HTML scheme handler (mirrors QPlotView's UrlSchemeHandler) ────────────────

class _HtmlSchemeHandler(QWebEngineUrlSchemeHandler):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._data = b"<html><body></body></html>"

    def set_html(self, html: str):
        self._data = html.encode("utf-8")

    def requestStarted(self, job: QWebEngineUrlRequestJob):
        buf = QBuffer(job)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        buf.write(self._data)
        job.reply(QByteArray(b"text/html"), buf)


# ── Plotly chart surface ──────────────────────────────────────────────────────

class StatsChartView(QWebEngineView):
    """Renders plotly figures using the same local:// scheme as QPlotView."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._profile = QWebEngineProfile(self)
        self._handler = _HtmlSchemeHandler(self)
        # "local" scheme is registered globally in QPlotViewSetup() at startup
        self._profile.installUrlSchemeHandler(b"local", self._handler)
        page = QWebEnginePage(self._profile, self)
        self.setPage(page)
        self._url = QUrl("local://stats-chart")
        self.show_placeholder("Assign participants to groups, then select a metric.")

    def show_figure(self, fig):
        self._handler.set_html(figure_to_html(fig))
        self.setUrl(self._url)

    def show_placeholder(self, msg: str = ""):
        self._handler.set_html(empty_html(msg))
        self.setUrl(self._url)


# ── Group assignment button ───────────────────────────────────────────────────

class GroupButton(QPushButton):
    """Cycles through group assignments on each click."""

    GROUPS = ["None", "Group 1", "Group 2", "Group 3", "Group 4"]
    _BG = {
        "None":    "#44475a",
        "Group 1": "#6272a4",
        "Group 2": "#ff9f43",
        "Group 3": "#26de81",
        "Group 4": "#fc5c65",
    }
    _FG = {
        "None":    "#888888",
        "Group 1": "#ffffff",
        "Group 2": "#ffffff",
        "Group 3": "#1a1a1a",
        "Group 4": "#ffffff",
    }

    groupChanged = Signal(str, str)  # (participant_name, new_group)

    def __init__(self, participant: str, group: str = "None", parent=None):
        super().__init__(parent)
        self._participant = participant
        self._idx = self.GROUPS.index(group) if group in self.GROUPS else 0
        self._apply()
        self.clicked.connect(self._cycle)
        self.setFixedWidth(82)
        self.setFixedHeight(22)
        self.setCursor(Qt.PointingHandCursor)

    def _cycle(self):
        self._idx = (self._idx + 1) % len(self.GROUPS)
        self._apply()
        self.groupChanged.emit(self._participant, self.current_group())

    def _apply(self):
        g = self.current_group()
        self.setText(g)
        self.setStyleSheet(
            f"QPushButton{{background:{self._BG[g]};color:{self._FG[g]};"
            f"border-radius:3px;font-size:11px;padding:1px 6px;border:none;}}"
        )

    def current_group(self) -> str:
        return self.GROUPS[self._idx]

    def set_group(self, group: str):
        if group in self.GROUPS:
            self._idx = self.GROUPS.index(group)
            self._apply()


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
        self._groups: dict[str, str] = {}           # {participant: group_label}
        self._group_buttons: dict[str, GroupButton] = {}
        self._last_result: dict | None = None
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
        outer.addWidget(self._build_right_panel())
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
        panel.setMinimumWidth(200)
        panel.setMaximumWidth(260)
        panel.setStyleSheet("background:#282a36;")
        vl = QVBoxLayout(panel)
        vl.setContentsMargins(8, 10, 8, 8)
        vl.setSpacing(6)

        vl.addWidget(self._lbl("Participants", bold=True))

        self._participant_tree = QTreeWidget()
        self._participant_tree.setHeaderLabels(["Name", "Group"])
        hdr = self._participant_tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.Fixed)
        hdr.resizeSection(1, 90)
        self._participant_tree.setSelectionMode(QAbstractItemView.NoSelection)
        self._participant_tree.setRootIsDecorated(False)
        self._participant_tree.setStyleSheet(
            "QTreeWidget{background:#1e1f28;color:#f8f8f2;border:none;font-size:12px;}"
            "QHeaderView::section{background:#282a36;color:#6272a4;"
            "border:none;padding:4px;font-size:11px;}"
        )
        vl.addWidget(self._participant_tree, stretch=1)

        hint = self._lbl(
            "Click group chip to cycle:  None → G1 → G2 → G3 → G4",
            color="#6272a4", size=10,
        )
        hint.setWordWrap(True)
        vl.addWidget(hint)

        btn_refresh = QPushButton("Refresh Data")
        btn_refresh.setStyleSheet(
            "QPushButton{background:#44475a;color:#f8f8f2;border-radius:4px;"
            "padding:5px;font-size:12px;border:none;}"
            "QPushButton:hover{background:#6272a4;}"
        )
        btn_refresh.setCursor(Qt.PointingHandCursor)
        btn_refresh.clicked.connect(self._reload_data)
        vl.addWidget(btn_refresh)

        return panel

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
        if not self._workspace_path:
            self._chart_view.show_placeholder("No workspace loaded.")
            return
        self._df = load_workspace_summary(self._workspace_path)
        self._refresh_participant_list()
        self._refresh_combos()
        self._update_chart()

    def _refresh_participant_list(self):
        self._participant_tree.clear()
        self._group_buttons.clear()

        if self._df is None or self._df.empty:
            return

        participants = sorted(self._df["Participant"].dropna().unique().tolist())
        for name in participants:
            item = QTreeWidgetItem(self._participant_tree, [name, ""])
            btn = GroupButton(name, self._groups.get(name, "None"))
            btn.groupChanged.connect(self._on_group_changed)
            self._participant_tree.setItemWidget(item, 1, btn)
            self._group_buttons[name] = btn

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

    def _on_group_changed(self, participant: str, group: str):
        self._groups[participant] = group
        self._update_chart()

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
        self._result_summary.setText(
            f"<b>{test}</b>: statistic = {stat:.4f},  p = {p:.4f}  |  {sig_html}<br>"
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
