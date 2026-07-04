"""
modules/stats/imported_panel.py — analysis panel for externally imported data.

Rendered in place of the workspace-summary controls (see stats_widget.py's
QStackedWidget) once a file has been imported and its subject/within/between/
DV roles chosen via import_dialog.ImportColumnDialog. Runs
pingouin_tests.run_analysis() and renders the omnibus + pairwise tables.
Independent from the workspace pipeline's manual group-tagging model, since
imported data already carries its own factor columns.
"""

import csv as _csv
import pandas as pd

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QSplitter, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QComboBox, QTableWidget, QTableWidgetItem,
    QAbstractItemView, QFileDialog, QMessageBox,
)

from .dataset import ExternalDataset
from .pingouin_tests import run_analysis
from .chart_builder import build_generic_chart
from .chart_view import StatsChartView

CHART_TYPES = ["Box", "Bar (Mean±SD)", "Violin", "Strip"]


def _lbl(text, bold=False, color="#f8f8f2", size=12) -> QLabel:
    l = QLabel(text)
    l.setStyleSheet(f"color:{color};font-weight:{'bold' if bold else 'normal'};font-size:{size}px;")
    return l


def _style_combo(cb: QComboBox):
    cb.setStyleSheet(
        "QComboBox{background:#44475a;color:#f8f8f2;border-radius:4px;"
        "padding:4px 8px;font-size:12px;border:none;}"
        "QComboBox::drop-down{border:none;}"
        "QComboBox QAbstractItemView{background:#44475a;color:#f8f8f2;"
        "selection-background-color:#6272a4;border:none;}"
    )


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.4f}" if pd.notna(v) else ""
    return "" if v is None else str(v)


class ImportedAnalysisPanel(QWidget):
    """Self-contained analysis view for one imported ExternalDataset."""

    def __init__(self, dataset: ExternalDataset, parent=None):
        super().__init__(parent)
        self._dataset = dataset
        self._last_result: dict | None = None
        self._setup_ui()
        self._update_chart()

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self):
        self.setStyleSheet("background:#282a36;")
        vl = QVBoxLayout(self)
        vl.setContentsMargins(10, 10, 10, 10)
        vl.setSpacing(8)

        vl.addWidget(self._build_info_bar())
        vl.addWidget(self._build_controls_bar())

        inner = QSplitter(Qt.Vertical)
        inner.setHandleWidth(4)
        inner.addWidget(self._build_chart_area())
        inner.addWidget(self._build_results_area())
        inner.setSizes([450, 300])
        inner.setStretchFactor(0, 1)
        inner.setStretchFactor(1, 0)
        vl.addWidget(inner, stretch=1)

    def _build_info_bar(self) -> QLabel:
        ds = self._dataset
        text = (
            f"Subject: <b>{ds.subject_col}</b> &nbsp;|&nbsp; "
            f"Within: <b>{ds.within_col or '—'}</b> &nbsp;|&nbsp; "
            f"Between: <b>{ds.between_col or '—'}</b>"
        )
        lbl = QLabel(text)
        lbl.setStyleSheet("color:#6272a4;font-size:11px;")
        return lbl

    def _build_controls_bar(self) -> QWidget:
        bar = QWidget()
        bar.setStyleSheet("background:#1e1f28;border-radius:6px;")
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(10, 6, 10, 6)
        hl.setSpacing(10)

        hl.addWidget(_lbl("Outcome:"))
        self._dv_combo = QComboBox()
        self._dv_combo.setMinimumWidth(160)
        _style_combo(self._dv_combo)
        for dv in self._dataset.dv_cols:
            self._dv_combo.addItem(dv)
        self._dv_combo.currentIndexChanged.connect(self._update_chart)
        hl.addWidget(self._dv_combo)

        hl.addWidget(_lbl("Chart:"))
        self._chart_type_combo = QComboBox()
        for t in CHART_TYPES:
            self._chart_type_combo.addItem(t)
        _style_combo(self._chart_type_combo)
        self._chart_type_combo.currentIndexChanged.connect(self._update_chart)
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

    def _build_chart_area(self) -> QWidget:
        container = QWidget()
        vl = QVBoxLayout(container)
        vl.setContentsMargins(0, 0, 0, 0)
        self._chart_view = StatsChartView(
            container, placeholder="Select an outcome, then click Run Statistical Test."
        )
        vl.addWidget(self._chart_view)
        return container

    def _build_results_area(self) -> QWidget:
        container = QWidget()
        container.setStyleSheet("background:#1e1f28;border-radius:6px;")
        vl = QVBoxLayout(container)
        vl.setContentsMargins(10, 8, 10, 8)
        vl.setSpacing(6)

        top = QHBoxLayout()
        top.addWidget(_lbl("Statistical Results", bold=True))
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

        vl.addWidget(_lbl("Omnibus test", color="#6272a4", size=10))
        self._anova_table = self._make_table()
        vl.addWidget(self._anova_table, stretch=1)

        vl.addWidget(_lbl("Pairwise comparisons", color="#6272a4", size=10))
        self._posthoc_table = self._make_table()
        vl.addWidget(self._posthoc_table, stretch=1)

        return container

    @staticmethod
    def _make_table() -> QTableWidget:
        table = QTableWidget()
        table.setStyleSheet(
            "QTableWidget{background:#282a36;color:#f8f8f2;border:none;"
            "gridline-color:#44475a;font-size:11px;}"
            "QHeaderView::section{background:#1e1f28;color:#6272a4;"
            "border:none;padding:4px;}"
        )
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    # ── chart ─────────────────────────────────────────────────────────────────

    def _current_dv(self):
        return self._dv_combo.currentText() or None

    def _update_chart(self):
        dv = self._current_dv()
        ds = self._dataset
        if not dv:
            self._chart_view.show_placeholder("No outcome columns available.")
            return

        x_col = ds.within_col or ds.between_col
        color_col = ds.between_col if x_col == ds.within_col else None

        fig = build_generic_chart(
            df=ds.df, dv=dv, x_col=x_col, color_col=color_col,
            chart_type=self._chart_type_combo.currentText(),
        )
        if fig is None:
            self._chart_view.show_placeholder("No data for current selection.")
            return
        self._chart_view.show_figure(fig)

    # ── statistical test ──────────────────────────────────────────────────────

    def _run_stats(self):
        dv = self._current_dv()
        if not dv:
            self._result_summary.setText("No outcome column selected.")
            return

        ds = self._dataset
        result = run_analysis(
            ds.df, dv, subject=ds.subject_col, within=ds.within_col, between=ds.between_col,
        )
        self._last_result = {"result": result, "dv": dv}
        self._display_results(result)

    def _display_results(self, result: dict):
        if "error" in result:
            self._result_summary.setText(result["error"])
            self._anova_table.setRowCount(0)
            self._posthoc_table.setRowCount(0)
            return

        test = result.get("test_name", "")
        sig = result.get("significant", False)
        normal = result.get("all_normal", True)

        sig_html = (
            "<span style='color:#26de81'><b>✔ At least one effect significant</b></span>"
            if sig else
            "<span style='color:#fc5c65'><b>✘ Not significant</b></span>"
        )
        norm_note = (
            "Normality: all levels passed Shapiro-Wilk → parametric test."
            if normal else
            "Normality: ≥1 level failed Shapiro-Wilk → non-parametric test."
        )
        self._result_summary.setText(
            f"<b>{test}</b>  |  {sig_html}<br>"
            f"<span style='color:#6272a4;font-size:11px'>{norm_note}</span>"
        )

        self._fill_table(self._anova_table, result.get("anova", []))
        self._fill_table(self._posthoc_table, result.get("post_hoc", []))

    @staticmethod
    def _fill_table(table: QTableWidget, rows: list):
        if not rows:
            table.setRowCount(0)
            table.setColumnCount(0)
            return
        cols = list(rows[0].keys())
        table.setColumnCount(len(cols))
        table.setHorizontalHeaderLabels(cols)
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, col in enumerate(cols):
                table.setItem(r, c, QTableWidgetItem(_fmt(row.get(col))))
        table.resizeColumnsToContents()

    # ── export ────────────────────────────────────────────────────────────────

    def _export_results(self):
        if not self._last_result:
            QMessageBox.information(self, "Export", "Run a statistical test first.")
            return

        path, _ = QFileDialog.getSaveFileName(self, "Export Results", "", "CSV Files (*.csv)")
        if not path:
            return

        result = self._last_result["result"]
        dv = self._last_result["dv"]

        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = _csv.writer(f)
                w.writerow(["Outcome", dv])
                w.writerow(["Test", result.get("test_name", "")])
                w.writerow([])
                anova = result.get("anova", [])
                if anova:
                    w.writerow(["Omnibus test"])
                    w.writerow(list(anova[0].keys()))
                    for row in anova:
                        w.writerow(list(row.values()))
                    w.writerow([])
                post_hoc = result.get("post_hoc", [])
                if post_hoc:
                    w.writerow(["Pairwise comparisons"])
                    w.writerow(list(post_hoc[0].keys()))
                    for row in post_hoc:
                        w.writerow(list(row.values()))
        except Exception as e:
            QMessageBox.critical(self, "Export Error", str(e))
