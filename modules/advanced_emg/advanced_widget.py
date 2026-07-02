"""
modules/advanced_emg/advanced_widget.py — Advanced EMG Analysis page.

First working draft: External-folder batch import (emg/ + cycles/, matching
musclesynergies_py's convention) + Co-contraction Index + Muscle Synergy
(NMF). Wavelet Analysis and SPM are listed as roadmap items but not
implemented yet.

Workspace-based batch import is not wired up yet either -- there is no
established convention anywhere in the app for turning kinematics/user
events into multiple discrete-rep cycle boundaries (see
core.batch_io.from_workspace's docstring).
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel, QComboBox,
    QPushButton, QSpinBox, QStackedWidget, QFileDialog, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QMessageBox, QApplication,
    QLineEdit, QCheckBox, QColorDialog, QDialog,
)

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from modules.pyMotion.core.batch_io import load_external_folder
from modules.pyMotion.core.batch_dataset import BatchDataset
from modules.pyMotion.core.synergy import prepare_synergy_input, extract_synergies
from modules.pyMotion.core.timeSeriesTable import timeSeriesTable
from .chart_view import AdvancedChartView


CCI_METHODS = [
    ("Rudolph et al. (2000)", "rudolph"),
    ("Falconer & Winter (1985)", "falconer_winter"),
]

# Deterministic, visually-distinct default palette for plot series (CCI's two
# muscles, or the N synergies) -- assigned by position, cycling if there are
# more series than colors. Individually overridable per series name via the
# "Series colors" swatches in the style panel.
_SERIES_PALETTE = [
    "#ff9f43", "#8be9fd", "#50fa7b", "#ff5555", "#bd93f9",
    "#f1fa8c", "#ffb86c", "#ff79c6", "#8be9c1", "#6272a4",
]


class AdvancedAnalysisWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._dataset: BatchDataset | None = None
        self._cycles_path: str | None = None
        self._emg_path: str | None = None
        self._prepared_cache = {}    # trial_name -> enveloped BatchTrial
        self._cci_results = {}       # trial_name -> (scalar, curve, signal_a, signal_b)
        self._cci_muscles = None     # (chan_a, chan_b) for the current _cci_results
        self._synergy_results = {}   # trial_name -> MusclesyneRgies
        # User-editable plot appearance; empty title/xlabel/ylabel fall back to
        # the analysis-computed defaults. Applied to whichever chart is current.
        self._chart_style = {
            "title": "", "show_title": True, "xlabel": "", "ylabel": "",
            "show_legend": True, "line_dash": "solid",
            "series_colors": {},   # series name -> explicit hex override
            "bg_color": "#282a36",
        }
        self._setup_ui()

    # ── UI construction ──────────────────────────────────────────────────

    def _setup_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        outer = QSplitter(Qt.Horizontal)
        outer.setHandleWidth(2)
        outer.addWidget(self._build_left_panel())
        outer.addWidget(self._build_right_panel())
        outer.setSizes([260, 900])
        outer.setStretchFactor(0, 0)
        outer.setStretchFactor(1, 1)
        root.addWidget(outer)

    @staticmethod
    def _lbl(text, bold=False, color="#f8f8f2", size=12):
        l = QLabel(text)
        l.setStyleSheet(
            f"color:{color};font-weight:{'bold' if bold else 'normal'};font-size:{size}px;"
        )
        return l

    @staticmethod
    def _style_combo(cb):
        cb.setStyleSheet(
            "QComboBox{background:#44475a;color:#f8f8f2;border-radius:4px;"
            "padding:4px 8px;font-size:12px;border:none;}"
            "QComboBox::drop-down{border:none;}"
            "QComboBox QAbstractItemView{background:#44475a;color:#f8f8f2;"
            "selection-background-color:#6272a4;border:none;}"
        )

    @staticmethod
    def _style_button(btn, accent=False):
        bg = "#6272a4" if accent else "#44475a"
        hover = "#7282b4" if accent else "#565a6e"
        btn.setStyleSheet(
            f"QPushButton{{background:{bg};color:#f8f8f2;border-radius:4px;"
            f"padding:5px 10px;font-size:12px;border:none;}}"
            f"QPushButton:hover{{background:{hover};}}"
        )
        btn.setCursor(Qt.PointingHandCursor)

    def _build_left_panel(self):
        panel = QWidget()
        panel.setMinimumWidth(230)
        panel.setMaximumWidth(300)
        panel.setStyleSheet("background:#282a36;")
        vl = QVBoxLayout(panel)
        vl.setContentsMargins(10, 10, 10, 10)
        vl.setSpacing(8)

        vl.addWidget(self._lbl("Data Source", bold=True))
        self._source_combo = QComboBox()
        self._source_combo.addItems([
            "External Folder (emg/ + cycles/)",
            "Current Workspace (coming soon)",
        ])
        self._style_combo(self._source_combo)
        self._source_combo.currentIndexChanged.connect(self._on_source_changed)
        vl.addWidget(self._source_combo)

        self._source_stack = QStackedWidget()
        self._source_stack.addWidget(self._build_external_source_page())
        self._source_stack.addWidget(self._build_workspace_source_page())
        vl.addWidget(self._source_stack)

        self._status_label = self._lbl("No data loaded.", color="#6272a4", size=11)
        self._status_label.setWordWrap(True)
        vl.addWidget(self._status_label)

        vl.addSpacing(6)
        line = QLabel()
        line.setFixedHeight(1)
        line.setStyleSheet("background:#44475a;")
        vl.addWidget(line)

        vl.addWidget(self._lbl("Analysis", bold=True))
        self._analysis_combo = QComboBox()
        self._analysis_combo.addItems([
            "Co-contraction Index",
            "Muscle Synergy (NMF)",
            "Wavelet Analysis (coming soon)",
            "Statistical Parametric Mapping (coming soon)",
        ])
        self._style_combo(self._analysis_combo)
        self._analysis_combo.currentIndexChanged.connect(self._on_analysis_changed)
        vl.addWidget(self._analysis_combo)

        self._param_stack = QStackedWidget()
        self._param_stack.addWidget(self._build_cci_params_page())
        self._param_stack.addWidget(self._build_synergy_params_page())
        self._param_stack.addWidget(
            self._build_coming_soon_page("Wavelet analysis is planned but not implemented yet.")
        )
        self._param_stack.addWidget(
            self._build_coming_soon_page("SPM is planned but not implemented yet.")
        )
        vl.addWidget(self._param_stack)

        self._btn_run = QPushButton("Run Analysis")
        self._style_button(self._btn_run, accent=True)
        self._btn_run.setEnabled(False)
        self._btn_run.clicked.connect(self._run_analysis)
        vl.addWidget(self._btn_run)

        vl.addSpacing(6)
        line2 = QLabel()
        line2.setFixedHeight(1)
        line2.setStyleSheet("background:#44475a;")
        vl.addWidget(line2)

        vl.addWidget(self._build_style_panel())

        vl.addStretch()
        return panel

    def _build_style_panel(self):
        page = QWidget()
        vl = QVBoxLayout(page)
        vl.setContentsMargins(0, 6, 0, 6)
        vl.setSpacing(6)
        vl.addWidget(self._lbl("Plot Style", bold=True))

        row_title = QHBoxLayout()
        row_title.addWidget(self._lbl("Title", size=11, color="#6272a4"), stretch=1)
        self._style_title_check = QCheckBox("Show")
        self._style_title_check.setChecked(True)
        self._style_title_check.setStyleSheet("color:#f8f8f2;font-size:11px;")
        self._style_title_check.stateChanged.connect(self._on_style_changed)
        row_title.addWidget(self._style_title_check)
        vl.addLayout(row_title)
        self._style_title_edit = QLineEdit()
        self._style_title_edit.setPlaceholderText("(auto)")
        self._style_title_edit.editingFinished.connect(self._on_style_changed)
        vl.addWidget(self._style_title_edit)

        vl.addWidget(self._lbl("X-axis label", size=11, color="#6272a4"))
        self._style_xlabel_edit = QLineEdit()
        self._style_xlabel_edit.setPlaceholderText("(auto)")
        self._style_xlabel_edit.editingFinished.connect(self._on_style_changed)
        vl.addWidget(self._style_xlabel_edit)

        vl.addWidget(self._lbl("Y-axis label", size=11, color="#6272a4"))
        self._style_ylabel_edit = QLineEdit()
        self._style_ylabel_edit.setPlaceholderText("(auto)")
        self._style_ylabel_edit.editingFinished.connect(self._on_style_changed)
        vl.addWidget(self._style_ylabel_edit)

        row_line = QHBoxLayout()
        self._style_line_combo = QComboBox()
        self._style_line_combo.addItems(["Solid", "Dash", "Dot", "Dash-Dot"])
        self._style_combo(self._style_line_combo)
        self._style_line_combo.currentIndexChanged.connect(self._on_style_changed)
        row_line.addWidget(self._style_line_combo, stretch=1)
        self._style_legend_check = QCheckBox("Legend")
        self._style_legend_check.setChecked(True)
        self._style_legend_check.setStyleSheet("color:#f8f8f2;font-size:12px;")
        self._style_legend_check.stateChanged.connect(self._on_style_changed)
        row_line.addWidget(self._style_legend_check)
        vl.addLayout(row_line)

        self._btn_edit_colors = QPushButton("Edit Colors...")
        self._style_button(self._btn_edit_colors)
        self._btn_edit_colors.clicked.connect(self._open_color_dialog)
        vl.addWidget(self._btn_edit_colors)

        self._build_color_dialog()

        return page

    def _build_color_dialog(self):
        """Background + per-series color pickers, in their own popup instead of
        the (too narrow to be usable) left panel -- can hold as many series
        swatches as a Synergy result needs without cramping everything else."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Plot Colors")
        dlg.setModal(False)
        dlg.setStyleSheet("background:#282a36;")
        vl = QVBoxLayout(dlg)
        vl.setContentsMargins(12, 12, 12, 12)
        vl.setSpacing(8)

        row_bg = QHBoxLayout()
        row_bg.addWidget(self._lbl("Background", size=11, color="#6272a4"))
        self._style_bg_btn = self._make_color_button(self._chart_style["bg_color"])
        self._style_bg_btn.clicked.connect(lambda: self._pick_style_color("bg_color", self._style_bg_btn))
        row_bg.addWidget(self._style_bg_btn)
        row_bg.addStretch()
        vl.addLayout(row_bg)

        # One swatch per series in whatever chart is currently shown -- CCI
        # always has exactly two (its two muscles), Synergy has as many as the
        # extracted rank. Rebuilt by _refresh_series_swatches() on every redraw.
        vl.addWidget(self._lbl("Series colors", bold=True))
        self._series_colors_layout = QVBoxLayout()
        self._series_colors_layout.setSpacing(6)
        vl.addLayout(self._series_colors_layout)
        vl.addStretch()

        btn_close = QPushButton("Close")
        self._style_button(btn_close)
        btn_close.clicked.connect(dlg.hide)
        vl.addWidget(btn_close)

        dlg.setMinimumWidth(220)
        self._color_dialog = dlg

    def _open_color_dialog(self):
        self._color_dialog.show()
        self._color_dialog.raise_()
        self._color_dialog.activateWindow()

    @staticmethod
    def _make_color_button(hex_color):
        btn = QPushButton()
        btn.setFixedSize(22, 18)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(
            f"background-color:{hex_color};border:1px solid #6272a4;border-radius:3px;"
        )
        return btn

    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            else:
                sub = item.layout()
                if sub is not None:
                    AdvancedAnalysisWidget._clear_layout(sub)

    def _series_color(self, name, index):
        """Resolve a series' color: explicit user override, else the default
        palette entry for its position (stable across redraws)."""
        return self._chart_style["series_colors"].get(
            name, _SERIES_PALETTE[index % len(_SERIES_PALETTE)]
        )

    def _refresh_series_swatches(self, names):
        """Rebuild the "Series colors" swatch list to match the series
        actually present in the chart currently being shown."""
        self._clear_layout(self._series_colors_layout)
        for i, name in enumerate(names):
            default_color = self._series_color(name, i)
            row = QHBoxLayout()
            row.setSpacing(6)
            btn = self._make_color_button(default_color)
            btn.clicked.connect(
                lambda _checked=False, n=name, d=default_color, b=btn: self._pick_series_color(n, d, b)
            )
            row.addWidget(btn)
            row.addWidget(self._lbl(name, size=10, color="#f8f8f2"))
            row.addStretch()
            self._series_colors_layout.addLayout(row)

    def _pick_series_color(self, name, current_hex, btn):
        color = QColorDialog.getColor(QColor(current_hex), self, f"Color for {name}")
        if not color.isValid():
            return
        hex_color = color.name()
        self._chart_style["series_colors"][name] = hex_color
        btn.setStyleSheet(
            f"background-color:{hex_color};border:1px solid #6272a4;border-radius:3px;"
        )
        self._update_chart()

    def _pick_style_color(self, key, btn):
        color = QColorDialog.getColor(QColor(self._chart_style[key]), self, "Choose Color")
        if not color.isValid():
            return
        hex_color = color.name()
        self._chart_style[key] = hex_color
        btn.setStyleSheet(
            f"background-color:{hex_color};border:1px solid #6272a4;border-radius:3px;"
        )
        self._update_chart()

    def _on_style_changed(self, *_args):
        self._chart_style["show_title"] = self._style_title_check.isChecked()
        self._style_title_edit.setEnabled(self._chart_style["show_title"])
        self._chart_style["title"] = self._style_title_edit.text().strip()
        self._chart_style["xlabel"] = self._style_xlabel_edit.text().strip()
        self._chart_style["ylabel"] = self._style_ylabel_edit.text().strip()
        self._chart_style["show_legend"] = self._style_legend_check.isChecked()
        dash_map = {0: "solid", 1: "dash", 2: "dot", 3: "dashdot"}
        self._chart_style["line_dash"] = dash_map[self._style_line_combo.currentIndex()]
        self._update_chart()

    def _build_external_source_page(self):
        page = QWidget()
        vl = QVBoxLayout(page)
        vl.setContentsMargins(0, 6, 0, 6)
        vl.setSpacing(6)

        vl.addWidget(self._lbl("cycles/ folder", size=11, color="#6272a4"))
        row1 = QHBoxLayout()
        self._cycles_path_label = self._lbl("(not set)", size=11)
        self._cycles_path_label.setWordWrap(True)
        btn_cycles = QPushButton("Browse...")
        self._style_button(btn_cycles)
        btn_cycles.clicked.connect(self._browse_cycles_folder)
        row1.addWidget(self._cycles_path_label, stretch=1)
        row1.addWidget(btn_cycles)
        vl.addLayout(row1)

        vl.addWidget(self._lbl("emg/ folder", size=11, color="#6272a4"))
        row2 = QHBoxLayout()
        self._emg_path_label = self._lbl("(not set)", size=11)
        self._emg_path_label.setWordWrap(True)
        btn_emg = QPushButton("Browse...")
        self._style_button(btn_emg)
        btn_emg.clicked.connect(self._browse_emg_folder)
        row2.addWidget(self._emg_path_label, stretch=1)
        row2.addWidget(btn_emg)
        vl.addLayout(row2)

        vl.addWidget(self._lbl("Cycle type", size=11, color="#6272a4"))
        self._cycle_mode_combo = QComboBox()
        self._cycle_mode_combo.addItems([
            "Discrete repetitions (e.g. Sit2Stand)",
            "Continuous gait cycles (coming soon)",
        ])
        self._style_combo(self._cycle_mode_combo)
        vl.addWidget(self._cycle_mode_combo)

        btn_load = QPushButton("Load Folder Pair")
        self._style_button(btn_load, accent=True)
        btn_load.clicked.connect(self._load_external_folder)
        vl.addWidget(btn_load)

        return page

    def _build_workspace_source_page(self):
        page = QWidget()
        vl = QVBoxLayout(page)
        note = self._lbl(
            "Batch import from the current workspace isn't wired up yet -- "
            "there's no established convention yet for turning kinematics/"
            "user events into multiple discrete-rep cycle boundaries. Use "
            "External Folder for now.",
            size=11, color="#6272a4",
        )
        note.setWordWrap(True)
        vl.addWidget(note)
        vl.addStretch()
        return page

    def _build_coming_soon_page(self, message):
        page = QWidget()
        vl = QVBoxLayout(page)
        note = self._lbl(message, size=11, color="#6272a4")
        note.setWordWrap(True)
        vl.addWidget(note)
        vl.addStretch()
        return page

    def _build_cci_params_page(self):
        page = QWidget()
        vl = QVBoxLayout(page)
        vl.setContentsMargins(0, 6, 0, 6)
        vl.setSpacing(6)

        vl.addWidget(self._lbl("Muscle A", size=11, color="#6272a4"))
        self._cci_muscle_a = QComboBox()
        self._style_combo(self._cci_muscle_a)
        vl.addWidget(self._cci_muscle_a)

        vl.addWidget(self._lbl("Muscle B", size=11, color="#6272a4"))
        self._cci_muscle_b = QComboBox()
        self._style_combo(self._cci_muscle_b)
        vl.addWidget(self._cci_muscle_b)

        vl.addWidget(self._lbl("Method", size=11, color="#6272a4"))
        self._cci_method_combo = QComboBox()
        for label, _key in CCI_METHODS:
            self._cci_method_combo.addItem(label)
        self._style_combo(self._cci_method_combo)
        vl.addWidget(self._cci_method_combo)

        vl.addStretch()
        return page

    def _build_synergy_params_page(self):
        page = QWidget()
        vl = QVBoxLayout(page)
        vl.setContentsMargins(0, 6, 0, 6)
        vl.setSpacing(6)

        vl.addWidget(self._lbl("Rank selection", size=11, color="#6272a4"))
        self._syn_rank_combo = QComboBox()
        self._syn_rank_combo.addItems(["Automatic", "Fixed"])
        self._style_combo(self._syn_rank_combo)
        self._syn_rank_combo.currentIndexChanged.connect(self._on_rank_mode_changed)
        vl.addWidget(self._syn_rank_combo)

        vl.addWidget(self._lbl("Fixed rank (# synergies)", size=11, color="#6272a4"))
        self._syn_fixed_rank_spin = QSpinBox()
        self._syn_fixed_rank_spin.setRange(1, 20)
        self._syn_fixed_rank_spin.setValue(3)
        self._syn_fixed_rank_spin.setEnabled(False)
        vl.addWidget(self._syn_fixed_rank_spin)

        vl.addWidget(self._lbl("Points per cycle (0-100%)", size=11, color="#6272a4"))
        self._syn_npoints_spin = QSpinBox()
        self._syn_npoints_spin.setRange(11, 501)
        self._syn_npoints_spin.setValue(101)
        vl.addWidget(self._syn_npoints_spin)

        note = self._lbl(
            "Automatic rank selection re-runs NMF for every candidate rank "
            "and can take a while for larger batches.",
            size=10, color="#6272a4",
        )
        note.setWordWrap(True)
        vl.addWidget(note)

        vl.addStretch()
        return page

    def _build_right_panel(self):
        panel = QWidget()
        panel.setStyleSheet("background:#282a36;")
        vl = QVBoxLayout(panel)
        vl.setContentsMargins(10, 10, 10, 10)
        vl.setSpacing(8)

        top = QHBoxLayout()
        top.addWidget(self._lbl("Trial:"))
        self._trial_combo = QComboBox()
        self._trial_combo.setMinimumWidth(140)
        self._style_combo(self._trial_combo)
        self._trial_combo.currentIndexChanged.connect(self._on_trial_selected)
        top.addWidget(self._trial_combo)
        top.addStretch()
        self._btn_clear_plot = QPushButton("Clear Plot")
        self._style_button(self._btn_clear_plot)
        self._btn_clear_plot.clicked.connect(self._clear_chart)
        top.addWidget(self._btn_clear_plot)
        self._btn_export = QPushButton("Export CSV")
        self._style_button(self._btn_export)
        self._btn_export.clicked.connect(self._export_results)
        top.addWidget(self._btn_export)
        vl.addLayout(top)

        inner = QSplitter(Qt.Vertical)
        inner.setHandleWidth(4)

        chart_container = QWidget()
        cvl = QVBoxLayout(chart_container)
        cvl.setContentsMargins(0, 0, 0, 0)
        self._chart_view = AdvancedChartView(chart_container)
        cvl.addWidget(self._chart_view)
        inner.addWidget(chart_container)

        table_container = QWidget()
        table_container.setStyleSheet("background:#1e1f28;border-radius:6px;")
        tvl = QVBoxLayout(table_container)
        tvl.setContentsMargins(10, 8, 10, 8)
        tvl.addWidget(self._lbl("Results", bold=True))
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
        tvl.addWidget(self._result_table, stretch=1)
        inner.addWidget(table_container)

        inner.setSizes([500, 220])
        inner.setStretchFactor(0, 1)
        inner.setStretchFactor(1, 0)
        vl.addWidget(inner, stretch=1)

        return panel

    # ── data source: external folder ────────────────────────────────────

    def _browse_cycles_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select cycles/ folder")
        if path:
            self._cycles_path = path
            self._cycles_path_label.setText(path)

    def _browse_emg_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select emg/ folder")
        if path:
            self._emg_path = path
            self._emg_path_label.setText(path)

    def _load_external_folder(self):
        if not self._cycles_path or not self._emg_path:
            QMessageBox.information(
                self, "Load Folder Pair", "Select both the cycles/ and emg/ folders first."
            )
            return

        if self._cycle_mode_combo.currentIndex() == 1:
            QMessageBox.information(
                self, "Not Implemented",
                "Continuous gait-cycle folders are not supported yet -- only "
                "discrete-repetition (start, end) cycle files are implemented.",
            )
            return

        try:
            dataset = load_external_folder(self._cycles_path, self._emg_path, cycle_mode="discrete")
        except Exception as e:
            QMessageBox.critical(self, "Load Failed", str(e))
            return

        self._dataset = dataset
        self._prepared_cache.clear()
        self._cci_results.clear()
        self._synergy_results.clear()

        self._trial_combo.blockSignals(True)
        self._trial_combo.clear()
        self._trial_combo.addItems(dataset.names)
        self._trial_combo.blockSignals(False)

        first_trial = dataset[dataset.names[0]]
        muscles = first_trial.emg.labels
        for combo in (self._cci_muscle_a, self._cci_muscle_b):
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(muscles)
            combo.blockSignals(False)
        if len(muscles) > 1:
            self._cci_muscle_b.setCurrentIndex(1)

        self._status_label.setText(
            f"Loaded {len(dataset)} trial(s), {len(muscles)} channel(s) each."
        )
        self._btn_run.setEnabled(True)
        self._chart_view.show_placeholder("Choose an analysis and click Run Analysis.")
        self._result_table.setRowCount(0)
        self._result_table.setColumnCount(0)

    def _on_source_changed(self, idx):
        self._source_stack.setCurrentIndex(idx)

    def _on_analysis_changed(self, idx):
        self._param_stack.setCurrentIndex(idx)
        self._result_table.setRowCount(0)
        self._result_table.setColumnCount(0)
        self._update_chart()

    def _on_rank_mode_changed(self, idx):
        self._syn_fixed_rank_spin.setEnabled(idx == 1)

    def _get_prepared(self, trial):
        if trial.name not in self._prepared_cache:
            self._prepared_cache[trial.name] = prepare_synergy_input(trial)
        return self._prepared_cache[trial.name]

    # ── run analysis ─────────────────────────────────────────────────────

    def _run_analysis(self):
        if self._dataset is None:
            return

        analysis_idx = self._analysis_combo.currentIndex()
        if analysis_idx == 0:
            self._run_cci()
        elif analysis_idx == 1:
            self._run_synergy()
        else:
            QMessageBox.information(self, "Not Implemented", "This analysis is not implemented yet.")

    def _run_cci(self):
        chan_a = self._cci_muscle_a.currentText()
        chan_b = self._cci_muscle_b.currentText()
        if not chan_a or not chan_b or chan_a == chan_b:
            QMessageBox.information(self, "Co-contraction Index", "Choose two different muscles.")
            return
        method_key = CCI_METHODS[self._cci_method_combo.currentIndex()][1]

        self._status_label.setText("Running co-contraction analysis...")
        QApplication.processEvents()

        self._cci_results.clear()
        self._cci_muscles = (chan_a, chan_b)
        n_points = 101
        try:
            for trial in self._dataset:
                prepared = self._get_prepared(trial)
                a = prepared.emg.timeNormalizeCycles(chan_a, prepared.cycles, n_points).flatten()
                b = prepared.emg.timeNormalizeCycles(chan_b, prepared.cycles, n_points).flatten()
                tmp = timeSeriesTable(1.0, [chan_a, chan_b], {chan_a: a, chan_b: b})
                curve = tmp.cocontractionCurve(chan_a, chan_b, method_key)
                self._cci_results[trial.name] = (float(np.mean(curve)), curve, a, b)
        except Exception as e:
            QMessageBox.critical(self, "Co-contraction Failed", str(e))
            self._status_label.setText("Co-contraction analysis failed.")
            return

        self._status_label.setText(f"Co-contraction computed for {len(self._cci_results)} trial(s).")
        self._populate_cci_table(chan_a, chan_b)
        self._update_chart()

    def _populate_cci_table(self, chan_a, chan_b):
        cols = ["Trial", f"CCI ({chan_a} vs {chan_b})"]
        self._result_table.setColumnCount(len(cols))
        self._result_table.setHorizontalHeaderLabels(cols)
        names = sorted(self._cci_results.keys())
        self._result_table.setRowCount(len(names))
        for r, name in enumerate(names):
            scalar, _curve, _a, _b = self._cci_results[name]
            self._result_table.setItem(r, 0, QTableWidgetItem(name))
            self._result_table.setItem(r, 1, QTableWidgetItem(f"{scalar:.4f}"))
        self._result_table.resizeColumnsToContents()

    def _run_synergy(self):
        rank_mode = self._syn_rank_combo.currentIndex()  # 0=Automatic, 1=Fixed
        fixed_syns = self._syn_fixed_rank_spin.value() if rank_mode == 1 else None
        n_points = self._syn_npoints_spin.value()

        self._status_label.setText("Running muscle synergy analysis (this can take a while)...")
        QApplication.processEvents()

        self._synergy_results.clear()
        try:
            for trial in self._dataset:
                prepared = self._get_prepared(trial)
                result = extract_synergies(prepared, n_points=n_points, fixed_syns=fixed_syns)
                self._synergy_results[trial.name] = result
                self._status_label.setText(
                    f"Synergy analysis: {trial.name} done ({result.syns} synergies)."
                )
                QApplication.processEvents()
        except Exception as e:
            QMessageBox.critical(self, "Synergy Analysis Failed", str(e))
            self._status_label.setText("Synergy analysis failed.")
            return

        self._status_label.setText(
            f"Synergy analysis computed for {len(self._synergy_results)} trial(s)."
        )
        self._populate_synergy_table()
        self._update_chart()

    def _populate_synergy_table(self):
        cols = ["Trial", "# Synergies", "R2"]
        self._result_table.setColumnCount(len(cols))
        self._result_table.setHorizontalHeaderLabels(cols)
        names = sorted(self._synergy_results.keys())
        self._result_table.setRowCount(len(names))
        for r, name in enumerate(names):
            result = self._synergy_results[name]
            r2 = 1.0 - np.sum((result.V - result.Vr) ** 2) / np.sum((result.V - result.V.mean()) ** 2)
            self._result_table.setItem(r, 0, QTableWidgetItem(name))
            self._result_table.setItem(r, 1, QTableWidgetItem(str(result.syns)))
            self._result_table.setItem(r, 2, QTableWidgetItem(f"{r2:.4f}"))
        self._result_table.resizeColumnsToContents()

    # ── chart ────────────────────────────────────────────────────────────

    def _on_trial_selected(self, _idx):
        self._update_chart()

    def _clear_chart(self):
        self._chart_view.show_placeholder("Load a data source, then run an analysis.")
        self._refresh_series_swatches([])

    def _update_chart(self):
        trial_name = self._trial_combo.currentText()
        if not trial_name:
            self._chart_view.show_placeholder("Load a data source first.")
            self._refresh_series_swatches([])
            return

        analysis_idx = self._analysis_combo.currentIndex()
        if analysis_idx == 0 and trial_name in self._cci_results:
            self._show_cci_chart(trial_name)
        elif analysis_idx == 1 and trial_name in self._synergy_results:
            self._show_synergy_chart(trial_name)
        else:
            self._chart_view.show_placeholder("Run an analysis to see results here.")
            self._refresh_series_swatches([])

    @staticmethod
    def _bg_luminance(hex_color):
        h = hex_color.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return (0.299 * r + 0.587 * g + 0.114 * b) / 255

    def _apply_common_style(self, fig, default_title, default_xlabel=None, default_ylabel=None):
        """Apply the user-editable title/labels/legend/background/grid to `fig`,
        falling back to analysis-computed defaults for anything left blank."""
        st = self._chart_style
        bg = st["bg_color"]
        dark = self._bg_luminance(bg) <= 0.6
        text_color = "#f8f8f2" if dark else "#1e1f28"

        title = (st["title"] or default_title) if st["show_title"] else ""
        layout_kwargs = dict(
            template="plotly_dark" if dark else "plotly_white",
            paper_bgcolor=bg, plot_bgcolor=bg,
            font=dict(color=text_color),
            title=title,
            showlegend=st["show_legend"],
            margin=dict(l=60, r=20, t=50, b=50),
        )
        xlabel = st["xlabel"] or default_xlabel
        ylabel = st["ylabel"] or default_ylabel
        if xlabel:
            layout_kwargs["xaxis_title"] = xlabel
        if ylabel:
            layout_kwargs["yaxis_title"] = ylabel
        fig.update_layout(**layout_kwargs)
        # No grid by default; user can toggle via the injected modebar button.
        fig.update_xaxes(showgrid=False)
        fig.update_yaxes(showgrid=False)

    def _show_cci_chart(self, trial_name):
        scalar, _curve, a, b = self._cci_results[trial_name]
        chan_a, chan_b = self._cci_muscles
        st = self._chart_style
        dash = None if st["line_dash"] == "solid" else st["line_dash"]
        self._refresh_series_swatches([chan_a, chan_b])
        color_a = self._series_color(chan_a, 0)
        color_b = self._series_color(chan_b, 1)
        x = np.arange(len(a))
        overlap = np.minimum(a, b)

        fig = go.Figure()
        # Shaded overlap (co-contraction) region, drawn first so both signal
        # lines render on top of it.
        fig.add_trace(go.Scatter(
            x=x, y=overlap, mode="lines", line=dict(width=0),
            fill="tozeroy", fillcolor="rgba(189,147,249,0.45)",
            name="Co-contraction (overlap)", hoverinfo="skip",
        ))
        fig.add_trace(go.Scatter(
            x=x, y=a, mode="lines", line=dict(color=color_a, width=2, dash=dash), name=chan_a,
        ))
        fig.add_trace(go.Scatter(
            x=x, y=b, mode="lines", line=dict(color=color_b, width=2, dash=dash), name=chan_b,
        ))
        self._apply_common_style(
            fig,
            default_title=f"Co-contraction — {trial_name} (mean CCI = {scalar:.4f})",
            default_xlabel="sample (concatenated normalized cycles)",
            default_ylabel="EMG amplitude",
        )
        self._chart_view.show_figure(fig, filename_stem=f"{trial_name}_CCI")

    def _show_synergy_chart(self, trial_name):
        result = self._synergy_results[trial_name]
        st = self._chart_style
        dash = None if st["line_dash"] == "solid" else st["line_dash"]
        self._refresh_series_swatches(result.syn_names)
        fig = make_subplots(
            rows=1, cols=2,
            subplot_titles=("Muscle weighting (M)", "Activation pattern (P)"),
        )
        for s in range(result.syns):
            color = self._series_color(result.syn_names[s], s)
            group = f"syn{s}"
            fig.add_trace(
                go.Bar(x=result.muscle_names, y=result.M[:, s], name=result.syn_names[s],
                       marker_color=color, legendgroup=group, showlegend=True),
                row=1, col=1,
            )
            # Same legendgroup + showlegend=False: the weighting bar's legend
            # entry represents both traces, so toggling it hides/shows the
            # matching activation line too (and vice versa via the group).
            fig.add_trace(
                go.Scatter(
                    x=result.P["time"], y=result.P[result.syn_names[s]],
                    mode="lines", name=result.syn_names[s],
                    line=dict(color=color, dash=dash),
                    legendgroup=group, showlegend=False,
                ),
                row=1, col=2,
            )
        self._apply_common_style(
            fig, default_title=f"Muscle synergies — {trial_name} ({result.syns} synergies)",
        )
        self._chart_view.show_figure(fig, filename_stem=f"{trial_name}_synergy")

    # ── export ───────────────────────────────────────────────────────────

    def _export_results(self):
        if self._result_table.rowCount() == 0:
            QMessageBox.information(self, "Export", "Run an analysis first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export Results", "", "CSV Files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                headers = [
                    self._result_table.horizontalHeaderItem(c).text()
                    for c in range(self._result_table.columnCount())
                ]
                f.write(",".join(headers) + "\n")
                for r in range(self._result_table.rowCount()):
                    row = [
                        self._result_table.item(r, c).text() if self._result_table.item(r, c) else ""
                        for c in range(self._result_table.columnCount())
                    ]
                    f.write(",".join(row) + "\n")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", str(e))
