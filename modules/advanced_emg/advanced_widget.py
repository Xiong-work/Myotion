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
    QPushButton, QSpinBox, QDoubleSpinBox, QStackedWidget, QFileDialog, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QMessageBox, QApplication,
    QLineEdit, QCheckBox, QColorDialog, QDialog, QListWidget, QTabWidget,
)

import os

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from modules.pyMotion.core.batch_io import load_external_folder, load_external_groups
from modules.pyMotion.core.batch_dataset import BatchDataset, subset_cycles
from modules.pyMotion.core.synergy import (
    prepare_synergy_input, extract_synergies, classify_kmeans, group_synergy_summary,
    within_group_cossim, cross_group_cossim, similarity_summary, spm_compare,
    within_group_cossim_curves, cross_group_cossim_curves, spm_compare_curves,
)
from modules.pyMotion.core.wavelet_analysis import prepare_wavelet_input, wavelet_medfreq_curve
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

# Synthetic entry injected into the Participant combo (Synergy analysis
# only, only once classification has run) so the group-level consensus plot
# is reachable from the same dropdown used to pick an individual participant.
GROUP_SUMMARY_LABEL = "— Group Summary (all participants) —"

# Default entry in the Group filter combo -- shows every loaded participant
# regardless of which comparison group (Control/LBP/...) they belong to.
ALL_GROUPS_LABEL = "— All Groups —"

# Synthetic entry injected into the Participant combo for Wavelet Analysis
# once a "Compare Groups" run has produced a result -- same idea as
# GROUP_SUMMARY_LABEL, just for Wavelet's self-contained group comparison
# (see synergy.py's *_curves() helpers) rather than Synergy's classified one.
WAVELET_COMPARE_LABEL = "— Group Comparison (Wavelet) —"


class AdvancedAnalysisWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._dataset: BatchDataset | None = None
        self._cycles_path: str | None = None
        self._emg_path: str | None = None
        self._prepared_cache = {}    # trial_name -> enveloped BatchTrial
        self._cci_results = {}       # trial_name -> (scalar, curve, signal_a, signal_b)
        self._cci_muscles = None     # (chan_a, chan_b) for the current _cci_results
        self._synergy_results = {}   # trial_name -> MusclesyneRgies (raw, per-trial NMF)
        self._synergy_classified = {}  # trial_name -> MusclesyneRgies (classify_kmeans output, if run)
        self._synergy_n_points = None  # points/cycle actually used for the last synergy run (shared across trials)
        self._cossim_result = None     # SimilarityMatrix for the last cosine-similarity run
        self._cossim_title = ""        # human-readable description of what _cossim_result compares
        self._spm_result = None        # SPMResult for the last SPM run
        self._spm_title = ""           # human-readable description of what _spm_result compares
        self._wavelet_prepared_cache = {}  # (trial_name, freq_low, freq_high) -> bandpass-filtered BatchTrial
        self._wavelet_results = {}     # trial_name -> instantaneous-median-frequency curve (np.ndarray)
        self._wavelet_channel = None   # muscle channel the current _wavelet_results was computed for
        self._wavelet_n_points = None  # points/cycle used for the last wavelet run
        self._wavelet_compare_result = None  # SimilarityMatrix or SPMResult, from "Compare Groups"
        self._wavelet_compare_kind = None    # "cossim" or "spm", disambiguates the above
        self._wavelet_compare_title = ""     # human-readable description of what _wavelet_compare_result compares
        # User-editable plot appearance; empty title/xlabel/ylabel fall back to
        # the analysis-computed defaults. Applied to whichever chart is current.
        # xlabel/ylabel are for CCI's single-plot chart; Synergy's dual-subplot
        # (weight + activation) chart has its own axis pair per subplot, since
        # the two panels have unrelated axes (muscle name vs. time, a.u. vs.
        # a.u.) that a single X/Y label pair can't sensibly describe.
        self._chart_style = {
            "title": "", "show_title": True, "xlabel": "", "ylabel": "",
            "syn_weight_xlabel": "", "syn_weight_ylabel": "",
            "syn_activation_xlabel": "", "syn_activation_ylabel": "",
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
        outer_vl = QVBoxLayout(panel)
        outer_vl.setContentsMargins(0, 0, 0, 0)
        outer_vl.setSpacing(0)

        tabs = QTabWidget()
        tabs.setStyleSheet(
            "QTabWidget::pane{border:none;background:#282a36;}"
            "QTabBar::tab{background:#1e1f28;color:#6272a4;padding:6px 16px;"
            "font-size:12px;}"
            "QTabBar::tab:selected{background:#282a36;color:#f8f8f2;}"
            "QTabBar::tab:hover{color:#f8f8f2;}"
        )
        tabs.addTab(self._build_data_tab(), "Data")
        tabs.addTab(self._build_processing_tab(), "Processing")
        outer_vl.addWidget(tabs)
        return panel

    def _build_data_tab(self):
        """Data Source + which cycles to include -- everything about what's
        loaded, kept separate from analysis params/style so this panel
        doesn't get too crowded to read at the sidebar's narrow width."""
        page = QWidget()
        vl = QVBoxLayout(page)
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

        vl.addWidget(self._lbl("Cycles to include", bold=True))
        row_cyc = QHBoxLayout()
        row_cyc.addWidget(self._lbl("Start", size=11, color="#6272a4"))
        self._cycle_start_spin = QSpinBox()
        self._cycle_start_spin.setRange(1, 999)
        self._cycle_start_spin.setValue(1)
        row_cyc.addWidget(self._cycle_start_spin)
        row_cyc.addWidget(self._lbl("Max", size=11, color="#6272a4"))
        self._cycle_max_spin = QSpinBox()
        self._cycle_max_spin.setRange(0, 999)
        self._cycle_max_spin.setValue(0)
        self._cycle_max_spin.setSpecialValueText("All")
        row_cyc.addWidget(self._cycle_max_spin)
        vl.addLayout(row_cyc)
        self._cycle_range_note = self._lbl("", size=10, color="#6272a4")
        self._cycle_range_note.setWordWrap(True)
        vl.addWidget(self._cycle_range_note)

        vl.addStretch()
        return page

    def _build_processing_tab(self):
        """Analysis selection/params/Run + plot style -- everything about
        what to compute and how to display it, once data is loaded."""
        page = QWidget()
        vl = QVBoxLayout(page)
        vl.setContentsMargins(10, 10, 10, 10)
        vl.setSpacing(8)

        vl.addWidget(self._lbl("Analysis", bold=True))
        self._analysis_combo = QComboBox()
        self._analysis_combo.addItems([
            "Co-contraction Index",
            "Muscle Synergy (NMF)",
            "Cosine Similarity (Synergy)",
            "Wavelet Analysis (CWT)",
            "Statistical Parametric Mapping (SPM)",
        ])
        self._style_combo(self._analysis_combo)
        self._analysis_combo.currentIndexChanged.connect(self._on_analysis_changed)
        vl.addWidget(self._analysis_combo)

        self._param_stack = QStackedWidget()
        self._param_stack.addWidget(self._build_cci_params_page())
        self._param_stack.addWidget(self._build_synergy_params_page())
        self._param_stack.addWidget(self._build_cossim_params_page())
        self._param_stack.addWidget(self._build_wavelet_params_page())
        self._param_stack.addWidget(self._build_spm_params_page())
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
        return page

    def _build_single_axis_labels_page(self):
        page = QWidget()
        vl = QVBoxLayout(page)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(6)

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

        return page

    def _build_synergy_axis_labels_page(self):
        page = QWidget()
        vl = QVBoxLayout(page)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(4)

        vl.addWidget(self._lbl("Weight (M) axes", size=10, color="#6272a4"))
        row_w = QHBoxLayout()
        self._style_syn_weight_xlabel_edit = QLineEdit()
        self._style_syn_weight_xlabel_edit.setPlaceholderText("X (auto)")
        self._style_syn_weight_xlabel_edit.editingFinished.connect(self._on_style_changed)
        row_w.addWidget(self._style_syn_weight_xlabel_edit)
        self._style_syn_weight_ylabel_edit = QLineEdit()
        self._style_syn_weight_ylabel_edit.setPlaceholderText("Y (auto)")
        self._style_syn_weight_ylabel_edit.editingFinished.connect(self._on_style_changed)
        row_w.addWidget(self._style_syn_weight_ylabel_edit)
        vl.addLayout(row_w)

        vl.addWidget(self._lbl("Activation (P) axes", size=10, color="#6272a4"))
        row_a = QHBoxLayout()
        self._style_syn_activation_xlabel_edit = QLineEdit()
        self._style_syn_activation_xlabel_edit.setPlaceholderText("X (auto)")
        self._style_syn_activation_xlabel_edit.editingFinished.connect(self._on_style_changed)
        row_a.addWidget(self._style_syn_activation_xlabel_edit)
        self._style_syn_activation_ylabel_edit = QLineEdit()
        self._style_syn_activation_ylabel_edit.setPlaceholderText("Y (auto)")
        self._style_syn_activation_ylabel_edit.editingFinished.connect(self._on_style_changed)
        row_a.addWidget(self._style_syn_activation_ylabel_edit)
        vl.addLayout(row_a)

        return page

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

        # CCI is one plot -> one X/Y label pair. Synergy is two subplots
        # (weight vs. activation) with unrelated axes -> its own pair each.
        # Switched by _on_analysis_changed() to match the active analysis.
        self._style_axis_stack = QStackedWidget()
        self._style_axis_stack.addWidget(self._build_single_axis_labels_page())
        self._style_axis_stack.addWidget(self._build_synergy_axis_labels_page())
        vl.addWidget(self._style_axis_stack)

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
        self._chart_style["syn_weight_xlabel"] = self._style_syn_weight_xlabel_edit.text().strip()
        self._chart_style["syn_weight_ylabel"] = self._style_syn_weight_ylabel_edit.text().strip()
        self._chart_style["syn_activation_xlabel"] = self._style_syn_activation_xlabel_edit.text().strip()
        self._chart_style["syn_activation_ylabel"] = self._style_syn_activation_ylabel_edit.text().strip()
        self._chart_style["show_legend"] = self._style_legend_check.isChecked()
        dash_map = {0: "solid", 1: "dash", 2: "dot", 3: "dashdot"}
        self._chart_style["line_dash"] = dash_map[self._style_line_combo.currentIndex()]
        self._update_chart()

    def _build_external_source_page(self):
        page = QWidget()
        vl = QVBoxLayout(page)
        vl.setContentsMargins(0, 6, 0, 6)
        vl.setSpacing(6)

        vl.addWidget(self._lbl("Group name", size=11, color="#6272a4"))
        self._group_name_edit = QLineEdit()
        self._group_name_edit.setPlaceholderText("auto-filled from folder name")
        vl.addWidget(self._group_name_edit)

        vl.addWidget(self._lbl("cycles/ folder", size=11, color="#6272a4"))
        row1 = QHBoxLayout()
        self._cycles_path_label = self._lbl("(not set)", size=11)
        self._cycles_path_label.setWordWrap(False)
        self._cycles_path_label.setToolTip("Hover to see the full path once set.")
        btn_cycles = QPushButton("Browse...")
        self._style_button(btn_cycles)
        btn_cycles.clicked.connect(self._browse_cycles_folder)
        row1.addWidget(self._cycles_path_label, stretch=1)
        row1.addWidget(btn_cycles)
        vl.addLayout(row1)

        vl.addWidget(self._lbl("emg/ folder", size=11, color="#6272a4"))
        row2 = QHBoxLayout()
        self._emg_path_label = self._lbl("(not set)", size=11)
        self._emg_path_label.setWordWrap(False)
        self._emg_path_label.setToolTip("Hover to see the full path once set.")
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

        btn_add_group = QPushButton("Add Group")
        self._style_button(btn_add_group, accent=True)
        btn_add_group.clicked.connect(self._add_group_from_paths)
        vl.addWidget(btn_add_group)

        vl.addSpacing(4)
        line = QLabel()
        line.setFixedHeight(1)
        line.setStyleSheet("background:#44475a;")
        vl.addWidget(line)

        bulk_note = self._lbl(
            "Or load every comparison group at once, one subfolder per "
            "group (each with its own emg/ + cycles/), e.g. "
            "Adv_Analyses/Control, Adv_Analyses/LBP:",
            size=10, color="#6272a4",
        )
        bulk_note.setWordWrap(True)
        vl.addWidget(bulk_note)
        btn_load_parent = QPushButton("Load Parent Folder (auto-detect)...")
        self._style_button(btn_load_parent)
        btn_load_parent.clicked.connect(self._load_parent_groups_folder)
        vl.addWidget(btn_load_parent)

        vl.addWidget(self._lbl("Loaded groups", size=11, color="#6272a4"))
        self._groups_list = QListWidget()
        self._groups_list.setMaximumHeight(90)
        self._groups_list.setStyleSheet(
            "QListWidget{background:#1e1f28;color:#f8f8f2;border:none;"
            "border-radius:4px;font-size:11px;}"
        )
        vl.addWidget(self._groups_list)

        btn_clear_groups = QPushButton("Clear All Groups")
        self._style_button(btn_clear_groups)
        btn_clear_groups.clicked.connect(self._clear_all_groups)
        vl.addWidget(btn_clear_groups)

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

    def _build_cci_params_page(self):
        page = QWidget()
        vl = QVBoxLayout(page)
        vl.setContentsMargins(0, 6, 0, 6)
        vl.setSpacing(6)

        row_muscles = QHBoxLayout()
        col_a = QVBoxLayout()
        col_a.addWidget(self._lbl("Muscle A", size=11, color="#6272a4"))
        self._cci_muscle_a = QComboBox()
        self._style_combo(self._cci_muscle_a)
        col_a.addWidget(self._cci_muscle_a)
        row_muscles.addLayout(col_a)

        col_b = QVBoxLayout()
        col_b.addWidget(self._lbl("Muscle B", size=11, color="#6272a4"))
        self._cci_muscle_b = QComboBox()
        self._style_combo(self._cci_muscle_b)
        col_b.addWidget(self._cci_muscle_b)
        row_muscles.addLayout(col_b)
        vl.addLayout(row_muscles)

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

        self._btn_synergy_settings = QPushButton("Synergy Settings...")
        self._style_button(self._btn_synergy_settings)
        self._btn_synergy_settings.clicked.connect(self._open_synergy_settings_dialog)
        vl.addWidget(self._btn_synergy_settings)

        self._syn_summary_label = self._lbl("", size=10, color="#6272a4")
        self._syn_summary_label.setWordWrap(True)
        vl.addWidget(self._syn_summary_label)

        self._build_synergy_settings_dialog()
        self._update_synergy_summary()

        vl.addStretch()
        return page

    def _build_synergy_settings_dialog(self):
        """Rank/classification parameters, in their own popup instead of the
        (too narrow to be usable) left panel -- same rationale as the Plot
        Colors popup. Defaults match what a fresh run would previously use
        inline, so opening the dialog isn't required before running."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Synergy Settings")
        dlg.setModal(False)
        dlg.setStyleSheet("background:#282a36;")
        vl = QVBoxLayout(dlg)
        vl.setContentsMargins(12, 12, 12, 12)
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
        self._syn_fixed_rank_spin.valueChanged.connect(self._update_synergy_summary)
        vl.addWidget(self._syn_fixed_rank_spin)

        vl.addWidget(self._lbl("Points per cycle (0-100%)", size=11, color="#6272a4"))
        self._syn_npoints_spin = QSpinBox()
        self._syn_npoints_spin.setRange(11, 501)
        self._syn_npoints_spin.setValue(101)
        self._syn_npoints_spin.valueChanged.connect(self._update_synergy_summary)
        vl.addWidget(self._syn_npoints_spin)

        note = self._lbl(
            "Automatic rank selection re-runs NMF for every candidate rank "
            "and can take a while for larger batches.",
            size=10, color="#6272a4",
        )
        note.setWordWrap(True)
        vl.addWidget(note)

        vl.addSpacing(4)
        self._syn_classify_check = QCheckBox("Classify synergies across participants")
        self._syn_classify_check.setChecked(True)
        self._syn_classify_check.setStyleSheet("color:#f8f8f2;font-size:12px;")
        self._syn_classify_check.stateChanged.connect(self._update_synergy_summary)
        vl.addWidget(self._syn_classify_check)

        row_clust = QHBoxLayout()
        row_clust.addWidget(self._lbl("Clusters", size=11, color="#6272a4"))
        self._syn_clusters_spin = QSpinBox()
        self._syn_clusters_spin.setRange(0, 20)
        self._syn_clusters_spin.setValue(0)
        self._syn_clusters_spin.setSpecialValueText("Auto")
        self._syn_clusters_spin.valueChanged.connect(self._update_synergy_summary)
        row_clust.addWidget(self._syn_clusters_spin)
        vl.addLayout(row_clust)

        classify_note = self._lbl(
            "Each trial's synergies are numbered independently, so \"Syn1\" "
            "in one trial isn't necessarily the same synergy as \"Syn1\" in "
            "another. Classification relabels them consistently across all "
            "loaded participants; needs a decent number of trials (10+) to "
            "work well. Synergies that can't be matched confidently are "
            "labeled \"Syncombined\".",
            size=10, color="#6272a4",
        )
        classify_note.setWordWrap(True)
        vl.addWidget(classify_note)
        vl.addStretch()

        btn_close = QPushButton("Close")
        self._style_button(btn_close)
        btn_close.clicked.connect(dlg.hide)
        vl.addWidget(btn_close)

        dlg.setMinimumWidth(260)
        self._synergy_settings_dialog = dlg

    def _open_synergy_settings_dialog(self):
        self._synergy_settings_dialog.show()
        self._synergy_settings_dialog.raise_()
        self._synergy_settings_dialog.activateWindow()

    def _update_synergy_summary(self, *_args):
        rank = "Automatic" if self._syn_rank_combo.currentIndex() == 0 else f"Fixed ({self._syn_fixed_rank_spin.value()})"
        classify = "on" if self._syn_classify_check.isChecked() else "off"
        clusters = "auto" if self._syn_clusters_spin.value() == 0 else str(self._syn_clusters_spin.value())
        self._syn_summary_label.setText(
            f"Rank: {rank}  |  Points/cycle: {self._syn_npoints_spin.value()}\n"
            f"Classify: {classify} (clusters: {clusters})"
        )

    def _build_cossim_params_page(self):
        """Params for cosine-similarity comparison of classified synergies --
        within one group, or between two groups (see synergy.py's
        within_group_cossim/cross_group_cossim). Requires Muscle Synergy
        analysis to have already been run with classification enabled,
        since this compares classified (cross-participant-labeled) synergies,
        not raw per-trial NMF output."""
        page = QWidget()
        vl = QVBoxLayout(page)
        vl.setContentsMargins(0, 6, 0, 6)
        vl.setSpacing(6)

        vl.addWidget(self._lbl("Signal", size=11, color="#6272a4"))
        self._cossim_signal_combo = QComboBox()
        self._cossim_signal_combo.addItems(["Muscle weighting (M)", "Activation pattern (P)"])
        self._style_combo(self._cossim_signal_combo)
        vl.addWidget(self._cossim_signal_combo)

        vl.addWidget(self._lbl("Group A", size=11, color="#6272a4"))
        self._cossim_group_a_combo = QComboBox()
        self._style_combo(self._cossim_group_a_combo)
        self._cossim_group_a_combo.currentIndexChanged.connect(self._on_cossim_group_a_changed)
        vl.addWidget(self._cossim_group_a_combo)

        vl.addWidget(self._lbl("Synergy A", size=11, color="#6272a4"))
        self._cossim_syn_a_combo = QComboBox()
        self._style_combo(self._cossim_syn_a_combo)
        vl.addWidget(self._cossim_syn_a_combo)

        self._cossim_cross_check = QCheckBox("Compare across two groups")
        self._cossim_cross_check.setStyleSheet("color:#f8f8f2;font-size:12px;")
        self._cossim_cross_check.stateChanged.connect(self._on_cossim_cross_toggled)
        vl.addWidget(self._cossim_cross_check)

        self._cossim_group_b_label = self._lbl("Group B", size=11, color="#6272a4")
        vl.addWidget(self._cossim_group_b_label)
        self._cossim_group_b_combo = QComboBox()
        self._style_combo(self._cossim_group_b_combo)
        self._cossim_group_b_combo.currentIndexChanged.connect(self._on_cossim_group_b_changed)
        vl.addWidget(self._cossim_group_b_combo)

        self._cossim_syn_b_label = self._lbl("Synergy B", size=11, color="#6272a4")
        vl.addWidget(self._cossim_syn_b_label)
        self._cossim_syn_b_combo = QComboBox()
        self._style_combo(self._cossim_syn_b_combo)
        vl.addWidget(self._cossim_syn_b_combo)

        self._cossim_group_b_label.setVisible(False)
        self._cossim_group_b_combo.setVisible(False)
        self._cossim_syn_b_label.setVisible(False)
        self._cossim_syn_b_combo.setVisible(False)

        note = self._lbl(
            "Requires Muscle Synergy analysis to have been run first with "
            "classification enabled (Synergy Settings) -- each group is "
            "classified independently, so a synergy label only means the "
            "same thing within its own group.",
            size=10, color="#6272a4",
        )
        note.setWordWrap(True)
        vl.addWidget(note)

        vl.addStretch()
        return page

    def _on_cossim_cross_toggled(self, _state):
        show = self._cossim_cross_check.isChecked()
        self._cossim_group_b_label.setVisible(show)
        self._cossim_group_b_combo.setVisible(show)
        self._cossim_syn_b_label.setVisible(show)
        self._cossim_syn_b_combo.setVisible(show)

    def _synergy_labels_for_group(self, group_name):
        """Classified synergy labels ("Syn1", "Syn2", ...) actually present
        among this group's trials, excluding "Syncombined_*" (those have no
        stable cross-trial identity to compare by construction)."""
        if not self._synergy_classified or self._dataset is None or not group_name:
            return []
        labels = set()
        for trial in self._dataset.trials_in(group_name):
            result = self._synergy_classified.get(trial.name)
            if result:
                labels.update(n for n in result.syn_names if "combined" not in n)
        return sorted(labels, key=lambda s: int(s.replace("Syn", "")))

    def _refresh_cossim_syn_combo(self, group_combo, syn_combo):
        group_name = group_combo.currentText()
        labels = self._synergy_labels_for_group(group_name)
        current = syn_combo.currentText()
        syn_combo.blockSignals(True)
        syn_combo.clear()
        syn_combo.addItems(labels)
        if current in labels:
            syn_combo.setCurrentText(current)
        syn_combo.blockSignals(False)

    def _on_cossim_group_a_changed(self, _idx):
        self._refresh_cossim_syn_combo(self._cossim_group_a_combo, self._cossim_syn_a_combo)

    def _on_cossim_group_b_changed(self, _idx):
        self._refresh_cossim_syn_combo(self._cossim_group_b_combo, self._cossim_syn_b_combo)

    def _refresh_cossim_group_combos(self):
        groups = self._dataset.group_names if self._dataset else []
        for combo in (self._cossim_group_a_combo, self._cossim_group_b_combo):
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(groups)
            if current in groups:
                combo.setCurrentText(current)
            combo.blockSignals(False)
        self._refresh_cossim_syn_combo(self._cossim_group_a_combo, self._cossim_syn_a_combo)
        self._refresh_cossim_syn_combo(self._cossim_group_b_combo, self._cossim_syn_b_combo)

    def _classified_for_group(self, group_name):
        names = {t.name for t in self._dataset.trials_in(group_name)}
        return {n: r for n, r in self._synergy_classified.items() if n in names}

    def _build_spm_params_page(self):
        """Params for two-sample SPM{t} (spm1d) comparing one synergy
        activation pattern from group A against one from group B -- unlike
        Cosine Similarity, SPM always compares two groups (comparing a
        group to itself isn't a meaningful hypothesis test), so there's no
        within/cross toggle here."""
        page = QWidget()
        vl = QVBoxLayout(page)
        vl.setContentsMargins(0, 6, 0, 6)
        vl.setSpacing(6)

        vl.addWidget(self._lbl("Group A", size=11, color="#6272a4"))
        self._spm_group_a_combo = QComboBox()
        self._style_combo(self._spm_group_a_combo)
        self._spm_group_a_combo.currentIndexChanged.connect(
            lambda _i: self._refresh_cossim_syn_combo(self._spm_group_a_combo, self._spm_syn_a_combo)
        )
        vl.addWidget(self._spm_group_a_combo)

        vl.addWidget(self._lbl("Synergy A", size=11, color="#6272a4"))
        self._spm_syn_a_combo = QComboBox()
        self._style_combo(self._spm_syn_a_combo)
        vl.addWidget(self._spm_syn_a_combo)

        vl.addWidget(self._lbl("Group B", size=11, color="#6272a4"))
        self._spm_group_b_combo = QComboBox()
        self._style_combo(self._spm_group_b_combo)
        self._spm_group_b_combo.currentIndexChanged.connect(
            lambda _i: self._refresh_cossim_syn_combo(self._spm_group_b_combo, self._spm_syn_b_combo)
        )
        vl.addWidget(self._spm_group_b_combo)

        vl.addWidget(self._lbl("Synergy B", size=11, color="#6272a4"))
        self._spm_syn_b_combo = QComboBox()
        self._style_combo(self._spm_syn_b_combo)
        vl.addWidget(self._spm_syn_b_combo)

        row_alpha = QHBoxLayout()
        row_alpha.addWidget(self._lbl("Alpha", size=11, color="#6272a4"))
        self._spm_alpha_spin = QDoubleSpinBox()
        self._spm_alpha_spin.setRange(0.001, 0.5)
        self._spm_alpha_spin.setSingleStep(0.01)
        self._spm_alpha_spin.setDecimals(3)
        self._spm_alpha_spin.setValue(0.05)
        row_alpha.addWidget(self._spm_alpha_spin)
        vl.addLayout(row_alpha)

        self._spm_two_tailed_check = QCheckBox("Two-tailed")
        self._spm_two_tailed_check.setChecked(True)
        self._spm_two_tailed_check.setStyleSheet("color:#f8f8f2;font-size:12px;")
        vl.addWidget(self._spm_two_tailed_check)

        note = self._lbl(
            "Requires Muscle Synergy analysis to have been run first with "
            "classification enabled. Compares each group's cycle-averaged "
            "activation pattern (P) via a two-sample SPM{t} test (spm1d) "
            "-- the same test the reference R/MATLAB workflow uses.",
            size=10, color="#6272a4",
        )
        note.setWordWrap(True)
        vl.addWidget(note)

        vl.addStretch()
        return page

    def _refresh_spm_group_combos(self):
        groups = self._dataset.group_names if self._dataset else []
        combos = (self._spm_group_a_combo, self._spm_group_b_combo)
        for i, combo in enumerate(combos):
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(groups)
            if current in groups:
                combo.setCurrentText(current)
            elif i == 1 and len(groups) > 1:
                # Default Group B to the second loaded group (rather than
                # mirroring Group A) so a fresh run compares two different
                # groups by default instead of a group against itself.
                combo.setCurrentIndex(1)
            combo.blockSignals(False)
        self._refresh_cossim_syn_combo(self._spm_group_a_combo, self._spm_syn_a_combo)
        self._refresh_cossim_syn_combo(self._spm_group_b_combo, self._spm_syn_b_combo)

    def _build_wavelet_params_page(self):
        """Params for Wavelet Analysis -- a per-trial, per-cycle
        instantaneous median-frequency curve (Continuous Wavelet Transform,
        Morlet) for one muscle channel, plus a self-contained "Compare
        Groups" section (cosine similarity or SPM) so this analysis doesn't
        need Muscle Synergy's classification step -- a muscle channel's
        identity is already stable across trials/groups, unlike a synergy
        label, so within/cross-group comparison works directly on the
        per-trial curves via synergy.py's *_curves() helpers."""
        page = QWidget()
        vl = QVBoxLayout(page)
        vl.setContentsMargins(0, 6, 0, 6)
        vl.setSpacing(6)

        vl.addWidget(self._lbl("Muscle", size=11, color="#6272a4"))
        self._wavelet_channel_combo = QComboBox()
        self._style_combo(self._wavelet_channel_combo)
        vl.addWidget(self._wavelet_channel_combo)

        row_freq = QHBoxLayout()
        col_low = QVBoxLayout()
        col_low.addWidget(self._lbl("Freq low (Hz)", size=10, color="#6272a4"))
        self._wavelet_freq_low_spin = QSpinBox()
        self._wavelet_freq_low_spin.setRange(1, 999)
        self._wavelet_freq_low_spin.setValue(20)
        col_low.addWidget(self._wavelet_freq_low_spin)
        row_freq.addLayout(col_low)
        col_high = QVBoxLayout()
        col_high.addWidget(self._lbl("Freq high (Hz)", size=10, color="#6272a4"))
        self._wavelet_freq_high_spin = QSpinBox()
        self._wavelet_freq_high_spin.setRange(2, 2000)
        self._wavelet_freq_high_spin.setValue(450)
        col_high.addWidget(self._wavelet_freq_high_spin)
        row_freq.addLayout(col_high)
        vl.addLayout(row_freq)

        vl.addWidget(self._lbl("Points per cycle (0-100%)", size=11, color="#6272a4"))
        self._wavelet_npoints_spin = QSpinBox()
        self._wavelet_npoints_spin.setRange(11, 501)
        self._wavelet_npoints_spin.setValue(101)
        vl.addWidget(self._wavelet_npoints_spin)

        note = self._lbl(
            "Instantaneous median frequency per % of cycle, averaged "
            "across included cycles -- uses the bandpass-filtered signal "
            "only (not the enveloped/normalized signal CCI/Synergy use).",
            size=10, color="#6272a4",
        )
        note.setWordWrap(True)
        vl.addWidget(note)

        vl.addSpacing(6)
        line = QLabel()
        line.setFixedHeight(1)
        line.setStyleSheet("background:#44475a;")
        vl.addWidget(line)

        vl.addWidget(self._lbl("Compare Groups", bold=True))
        vl.addWidget(self._lbl("Group A", size=11, color="#6272a4"))
        self._wavelet_group_a_combo = QComboBox()
        self._style_combo(self._wavelet_group_a_combo)
        vl.addWidget(self._wavelet_group_a_combo)

        self._wavelet_cross_check = QCheckBox("Compare across two groups")
        self._wavelet_cross_check.setStyleSheet("color:#f8f8f2;font-size:12px;")
        self._wavelet_cross_check.stateChanged.connect(self._on_wavelet_cross_toggled)
        vl.addWidget(self._wavelet_cross_check)

        self._wavelet_group_b_label = self._lbl("Group B", size=11, color="#6272a4")
        vl.addWidget(self._wavelet_group_b_label)
        self._wavelet_group_b_combo = QComboBox()
        self._style_combo(self._wavelet_group_b_combo)
        vl.addWidget(self._wavelet_group_b_combo)
        self._wavelet_group_b_label.setVisible(False)
        self._wavelet_group_b_combo.setVisible(False)

        vl.addWidget(self._lbl("Method", size=11, color="#6272a4"))
        self._wavelet_method_combo = QComboBox()
        self._wavelet_method_combo.addItems(["Cosine Similarity", "SPM"])
        self._style_combo(self._wavelet_method_combo)
        self._wavelet_method_combo.currentIndexChanged.connect(self._on_wavelet_method_changed)
        vl.addWidget(self._wavelet_method_combo)

        self._btn_wavelet_compare = QPushButton("Compare Groups")
        self._style_button(self._btn_wavelet_compare)
        self._btn_wavelet_compare.clicked.connect(self._run_wavelet_compare)
        vl.addWidget(self._btn_wavelet_compare)

        compare_note = self._lbl(
            "Requires Wavelet Analysis to have been run first (Run "
            "Analysis, above) -- SPM always needs two groups.",
            size=10, color="#6272a4",
        )
        compare_note.setWordWrap(True)
        vl.addWidget(compare_note)

        vl.addStretch()
        return page

    def _on_wavelet_cross_toggled(self, _state):
        show = self._wavelet_cross_check.isChecked()
        self._wavelet_group_b_label.setVisible(show)
        self._wavelet_group_b_combo.setVisible(show)

    def _on_wavelet_method_changed(self, _idx):
        # SPM always compares two groups -- comparing a group to itself
        # isn't a meaningful hypothesis test, so force+lock the checkbox
        # rather than let the user hit an avoidable error at Compare time.
        is_spm = self._wavelet_method_combo.currentText() == "SPM"
        if is_spm:
            self._wavelet_cross_check.setChecked(True)
        self._wavelet_cross_check.setEnabled(not is_spm)

    def _refresh_wavelet_group_combos(self):
        groups = self._dataset.group_names if self._dataset else []
        combos = (self._wavelet_group_a_combo, self._wavelet_group_b_combo)
        for i, combo in enumerate(combos):
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(groups)
            if current in groups:
                combo.setCurrentText(current)
            elif i == 1 and len(groups) > 1:
                combo.setCurrentIndex(1)
            combo.blockSignals(False)

    def _wavelet_curves_for_group(self, group_name):
        names = {t.name for t in self._dataset.trials_in(group_name)}
        return {n: c for n, c in self._wavelet_results.items() if n in names}

    def _build_right_panel(self):
        panel = QWidget()
        panel.setStyleSheet("background:#282a36;")
        vl = QVBoxLayout(panel)
        vl.setContentsMargins(10, 10, 10, 10)
        vl.setSpacing(8)

        top = QHBoxLayout()
        top.addWidget(self._lbl("Group:"))
        self._group_filter_combo = QComboBox()
        self._group_filter_combo.setMinimumWidth(100)
        self._style_combo(self._group_filter_combo)
        self._group_filter_combo.addItem(ALL_GROUPS_LABEL)
        self._group_filter_combo.currentIndexChanged.connect(self._on_group_filter_changed)
        top.addWidget(self._group_filter_combo)

        top.addWidget(self._lbl("Participant:"))
        self._trial_combo = QComboBox()
        self._trial_combo.setMinimumWidth(140)
        self._style_combo(self._trial_combo)
        self._trial_combo.currentIndexChanged.connect(self._on_trial_selected)
        top.addWidget(self._trial_combo)

        top.addWidget(self._lbl("Repetition:"))
        self._rep_combo = QComboBox()
        self._rep_combo.setMinimumWidth(90)
        self._style_combo(self._rep_combo)
        self._rep_combo.addItem("All")
        self._rep_combo.setEnabled(False)
        self._rep_combo.currentIndexChanged.connect(self._on_repetition_selected)
        top.addWidget(self._rep_combo)
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
            self._cycles_path_label.setText(os.path.basename(path.rstrip("/\\")) or path)
            self._cycles_path_label.setToolTip(path)
            self._maybe_autofill_group_name(path)

    def _browse_emg_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select emg/ folder")
        if path:
            self._emg_path = path
            self._emg_path_label.setText(os.path.basename(path.rstrip("/\\")) or path)
            self._emg_path_label.setToolTip(path)
            self._maybe_autofill_group_name(path)

    def _maybe_autofill_group_name(self, folder_path):
        """Suggest a group name from the selected folder's parent directory
        (e.g. picking .../Control/emg suggests "Control") -- only when the
        field is still empty, so it never clobbers a name the user typed."""
        if self._group_name_edit.text().strip():
            return
        parent_name = os.path.basename(os.path.dirname(folder_path.rstrip("/\\")))
        if parent_name:
            self._group_name_edit.setText(parent_name)

    def _add_group_from_paths(self):
        """Load one emg/+cycles/ folder pair as one comparison group,
        appending it to whatever is already loaded (or starting a fresh
        dataset if nothing is loaded yet). Repeatable -- there's no cap on
        how many groups can be added this way."""
        if not self._cycles_path or not self._emg_path:
            QMessageBox.information(
                self, "Add Group", "Select both the cycles/ and emg/ folders first."
            )
            return

        if self._cycle_mode_combo.currentIndex() == 1:
            QMessageBox.information(
                self, "Not Implemented",
                "Continuous gait-cycle folders are not supported yet -- only "
                "discrete-repetition (start, end) cycle files are implemented.",
            )
            return

        existing_groups = self._dataset.group_names if self._dataset else []
        group_name = self._group_name_edit.text().strip() or f"Group {len(existing_groups) + 1}"

        try:
            dataset = load_external_folder(
                self._cycles_path, self._emg_path, cycle_mode="discrete",
                group=group_name, dataset=self._dataset,
            )
        except Exception as e:
            QMessageBox.critical(self, "Load Failed", str(e))
            return

        self._on_dataset_loaded(dataset)

        self._cycles_path = None
        self._emg_path = None
        self._cycles_path_label.setText("(not set)")
        self._cycles_path_label.setToolTip("")
        self._emg_path_label.setText("(not set)")
        self._emg_path_label.setToolTip("")
        self._group_name_edit.clear()

    def _load_parent_groups_folder(self):
        """Bulk-load every group found directly under one parent folder
        (one subfolder per group, each with its own emg/+cycles/), replacing
        whatever is currently loaded -- matches this project's sample data
        layout (e.g. Adv_Analyses/Control, Adv_Analyses/LBP)."""
        if self._cycle_mode_combo.currentIndex() == 1:
            QMessageBox.information(
                self, "Not Implemented",
                "Continuous gait-cycle folders are not supported yet -- only "
                "discrete-repetition (start, end) cycle files are implemented.",
            )
            return

        parent = QFileDialog.getExistingDirectory(
            self, "Select parent folder (one subfolder per group, each with emg/ + cycles/)"
        )
        if not parent:
            return

        try:
            dataset = load_external_groups(parent, cycle_mode="discrete")
        except Exception as e:
            QMessageBox.critical(self, "Load Failed", str(e))
            return

        self._on_dataset_loaded(dataset)

    def _clear_all_groups(self):
        self._on_dataset_loaded(None)

    def _refresh_groups_list(self):
        self._groups_list.clear()
        if self._dataset is None:
            return
        for group_name in self._dataset.group_names:
            n = len(self._dataset.trials_in(group_name))
            self._groups_list.addItem(f"{group_name}  ({n} participant{'s' if n != 1 else ''})")

    def _refresh_group_filter_combo(self):
        self._group_filter_combo.blockSignals(True)
        self._group_filter_combo.clear()
        self._group_filter_combo.addItem(ALL_GROUPS_LABEL)
        if self._dataset is not None:
            self._group_filter_combo.addItems(self._dataset.group_names)
        self._group_filter_combo.setCurrentIndex(0)
        self._group_filter_combo.blockSignals(False)

    def _refresh_trial_combo(self):
        """Populate the Participant combo from whichever group is selected
        in the Group filter (or every participant, for the default "All
        Groups" entry)."""
        group_filter = self._group_filter_combo.currentText()
        if self._dataset is None:
            names = []
        elif group_filter and group_filter != ALL_GROUPS_LABEL:
            names = [t.name for t in self._dataset.trials_in(group_filter)]
        else:
            names = self._dataset.names

        self._trial_combo.blockSignals(True)
        self._trial_combo.clear()
        self._trial_combo.addItems(names)
        self._trial_combo.blockSignals(False)
        self._sync_group_summary_option()
        self._sync_wavelet_compare_option()
        self._refresh_rep_combo()

    def _on_group_filter_changed(self, _idx):
        self._refresh_trial_combo()
        self._update_chart()

    def _on_dataset_loaded(self, dataset):
        """Common bookkeeping after (re)loading, adding to, or clearing the
        dataset -- any change invalidates cached preprocessing/results since
        cycle/rep composition may have changed."""
        self._dataset = dataset
        self._prepared_cache.clear()
        self._wavelet_prepared_cache.clear()
        self._cci_results.clear()
        self._synergy_results.clear()
        self._synergy_classified.clear()
        self._cossim_result = None
        self._cossim_title = ""
        self._spm_result = None
        self._spm_title = ""
        self._wavelet_results.clear()
        self._wavelet_channel = None
        self._wavelet_n_points = None
        self._wavelet_compare_result = None
        self._wavelet_compare_kind = None
        self._wavelet_compare_title = ""

        self._refresh_groups_list()
        self._refresh_group_filter_combo()
        self._refresh_cossim_group_combos()
        self._refresh_spm_group_combos()
        self._refresh_wavelet_group_combos()

        if dataset is None or len(dataset) == 0:
            self._trial_combo.blockSignals(True)
            self._trial_combo.clear()
            self._trial_combo.blockSignals(False)
            for combo in (self._cci_muscle_a, self._cci_muscle_b, self._wavelet_channel_combo):
                combo.blockSignals(True)
                combo.clear()
                combo.blockSignals(False)
            self._status_label.setText("No data loaded.")
            self._cycle_range_note.setText("")
            self._btn_run.setEnabled(False)
            self._chart_view.show_placeholder("Load a data source, then run an analysis.")
            self._result_table.setRowCount(0)
            self._result_table.setColumnCount(0)
            return

        self._refresh_trial_combo()

        first_trial = dataset[dataset.names[0]]
        muscles = first_trial.emg.labels
        for combo in (self._cci_muscle_a, self._cci_muscle_b, self._wavelet_channel_combo):
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(muscles)
            combo.blockSignals(False)
        if len(muscles) > 1:
            self._cci_muscle_b.setCurrentIndex(1)

        n_groups = len(dataset.group_names)
        group_note = f" across {n_groups} group(s)" if n_groups > 1 else ""
        self._status_label.setText(
            f"Loaded {len(dataset)} trial(s){group_note}, {len(muscles)} channel(s) each."
        )

        cycle_counts = [len(t.cycles) for t in dataset]
        min_cyc, max_cyc = min(cycle_counts), max(cycle_counts)
        self._cycle_start_spin.blockSignals(True)
        self._cycle_start_spin.setValue(1)
        self._cycle_start_spin.blockSignals(False)
        self._cycle_max_spin.blockSignals(True)
        self._cycle_max_spin.setValue(0)  # "All"
        self._cycle_max_spin.blockSignals(False)
        if min_cyc == max_cyc:
            self._cycle_range_note.setText(f"Each trial has {min_cyc} cycle(s).")
        else:
            self._cycle_range_note.setText(
                f"Trials have {min_cyc}-{max_cyc} cycles each -- Start/Max are "
                "clipped per trial if a trial has fewer than requested."
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
        self._refresh_rep_combo()
        self._sync_group_summary_option()
        self._sync_wavelet_compare_option()
        self._style_axis_stack.setCurrentIndex(1 if idx == 1 else 0)
        if idx == 2:
            self._refresh_cossim_group_combos()
        elif idx == 3:
            self._refresh_wavelet_group_combos()
        elif idx == 4:
            self._refresh_spm_group_combos()
        self._update_chart()

    def _active_group_for_summary(self):
        """Which comparison group the synthetic "Group Summary" entry
        should summarize -- Synergy classification runs independently per
        group (see _run_synergy), so averaging across groups would silently
        mix unrelated synergy labels; Wavelet has no classification step,
        but a single group-mean curve across groups would still hide
        exactly the group difference the user loaded multiple groups to
        see, so the same one-group-at-a-time rule applies to it too.
        Resolves to the Group filter's current selection; None (summary
        hidden) if "All Groups" is selected while more than one group is
        loaded, since there's no single group to summarize in that case.
        With zero or one group loaded (including the legacy single-folder/
        workspace path), the filter is irrelevant and this just resolves to
        that one group."""
        if self._dataset is None:
            return None
        groups = self._dataset.group_names
        if len(groups) <= 1:
            return groups[0] if groups else None
        current = self._group_filter_combo.currentText()
        return current if current and current != ALL_GROUPS_LABEL else None

    def _sync_group_summary_option(self):
        """Add/remove the synthetic "Group Summary" entry from the
        Participant combo -- shown for Synergy once a classified result
        exists, or for Wavelet once a wavelet run exists, for the currently
        active group (see _active_group_for_summary)."""
        analysis_idx = self._analysis_combo.currentIndex()
        if analysis_idx == 1:
            has_results = bool(self._synergy_classified)
        elif analysis_idx == 3:
            has_results = bool(self._wavelet_results)
        else:
            has_results = False
        show_group = has_results and self._active_group_for_summary() is not None
        idx = self._trial_combo.findText(GROUP_SUMMARY_LABEL)
        if show_group and idx < 0:
            self._trial_combo.blockSignals(True)
            self._trial_combo.insertItem(0, GROUP_SUMMARY_LABEL)
            self._trial_combo.setCurrentIndex(0)
            self._trial_combo.blockSignals(False)
        elif not show_group and idx >= 0:
            self._trial_combo.blockSignals(True)
            self._trial_combo.removeItem(idx)
            if self._trial_combo.count() > 0:
                self._trial_combo.setCurrentIndex(0)
            self._trial_combo.blockSignals(False)

    def _sync_wavelet_compare_option(self):
        """Add/remove the synthetic "Group Comparison" entry from the
        Participant combo -- shown for Wavelet Analysis once a "Compare
        Groups" run has produced a result. Inserted after
        _sync_group_summary_option so, when both are present, Group
        Comparison sits above Group Summary (both insert at index 0;
        calling this second puts it on top) -- consistent ordering rather
        than depending on call order elsewhere."""
        show = (
            self._analysis_combo.currentIndex() == 3
            and self._wavelet_compare_result is not None
        )
        idx = self._trial_combo.findText(WAVELET_COMPARE_LABEL)
        if show and idx < 0:
            self._trial_combo.blockSignals(True)
            self._trial_combo.insertItem(0, WAVELET_COMPARE_LABEL)
            self._trial_combo.setCurrentIndex(0)
            self._trial_combo.blockSignals(False)
        elif not show and idx >= 0:
            self._trial_combo.blockSignals(True)
            self._trial_combo.removeItem(idx)
            if self._trial_combo.count() > 0:
                self._trial_combo.setCurrentIndex(0)
            self._trial_combo.blockSignals(False)

    def _on_rank_mode_changed(self, idx):
        self._syn_fixed_rank_spin.setEnabled(idx == 1)
        self._update_synergy_summary()

    def _get_prepared(self, trial):
        if trial.name not in self._prepared_cache:
            self._prepared_cache[trial.name] = prepare_synergy_input(trial)
        return self._prepared_cache[trial.name]

    def _get_prepared_subset(self, trial):
        """Preprocessed trial, restricted to the "Cycles to include" range.

        Preprocessing (filter/envelope/normalize) always runs on the full
        trial -- only which cycles feed the downstream analysis is
        restricted here, applied fresh on every run so Start/Max can be
        changed without reloading the folder.
        """
        prepared = self._get_prepared(trial)
        cy_start = self._cycle_start_spin.value()
        cy_max = self._cycle_max_spin.value()  # 0 == "All"
        subset = subset_cycles(prepared, cy_start, cy_max)
        if len(subset.cycles) == 0:
            raise ValueError(
                f"trial '{trial.name}' has no cycles left after applying "
                f"Start={cy_start}/Max={cy_max or 'All'}"
            )
        return subset

    def _get_wavelet_prepared(self, trial, freq_low, freq_high):
        """Bandpass-filtered-only trial for Wavelet Analysis -- deliberately
        NOT prepare_synergy_input's enveloped/normalized signal (frequency-
        domain analysis must stay off that path). Cached separately from
        _prepared_cache, keyed also on the freq range since that changes
        the filter."""
        cache_key = (trial.name, freq_low, freq_high)
        if cache_key not in self._wavelet_prepared_cache:
            self._wavelet_prepared_cache[cache_key] = prepare_wavelet_input(
                trial, bp_low=freq_low, bp_high=freq_high,
            )
        return self._wavelet_prepared_cache[cache_key]

    def _get_wavelet_prepared_subset(self, trial, freq_low, freq_high):
        prepared = self._get_wavelet_prepared(trial, freq_low, freq_high)
        cy_start = self._cycle_start_spin.value()
        cy_max = self._cycle_max_spin.value()  # 0 == "All"
        subset = subset_cycles(prepared, cy_start, cy_max)
        if len(subset.cycles) == 0:
            raise ValueError(
                f"trial '{trial.name}' has no cycles left after applying "
                f"Start={cy_start}/Max={cy_max or 'All'}"
            )
        return subset

    # ── run analysis ─────────────────────────────────────────────────────

    def _run_analysis(self):
        if self._dataset is None:
            return

        analysis_idx = self._analysis_combo.currentIndex()
        if analysis_idx == 0:
            self._run_cci()
        elif analysis_idx == 1:
            self._run_synergy()
        elif analysis_idx == 2:
            self._run_cossim()
        elif analysis_idx == 3:
            self._run_wavelet()
        elif analysis_idx == 4:
            self._run_spm()
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
                prepared = self._get_prepared_subset(trial)
                a = prepared.emg.timeNormalizeCycles(chan_a, prepared.cycles, n_points).flatten()
                b = prepared.emg.timeNormalizeCycles(chan_b, prepared.cycles, n_points).flatten()
                tmp = timeSeriesTable(1.0, [chan_a, chan_b], {chan_a: a, chan_b: b})
                curve = tmp.cocontractionCurve(chan_a, chan_b, method_key)
                self._cci_results[trial.name] = (float(np.mean(curve)), curve, a, b, len(prepared.cycles))
        except Exception as e:
            QMessageBox.critical(self, "Co-contraction Failed", str(e))
            self._status_label.setText("Co-contraction analysis failed.")
            return

        self._status_label.setText(f"Co-contraction computed for {len(self._cci_results)} trial(s).")
        self._populate_cci_table(chan_a, chan_b)
        self._refresh_rep_combo()
        self._update_chart()

    def _populate_cci_table(self, chan_a, chan_b):
        cols = ["Trial", f"CCI ({chan_a} vs {chan_b})"]
        self._result_table.setColumnCount(len(cols))
        self._result_table.setHorizontalHeaderLabels(cols)
        names = sorted(self._cci_results.keys())
        self._result_table.setRowCount(len(names))
        for r, name in enumerate(names):
            scalar, _curve, _a, _b, _n_reps = self._cci_results[name]
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
        self._synergy_classified.clear()
        try:
            for trial in self._dataset:
                prepared = self._get_prepared_subset(trial)
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
        self._synergy_n_points = n_points

        base_status = f"Synergy analysis computed for {len(self._synergy_results)} trial(s)."
        if self._syn_classify_check.isChecked() and len(self._synergy_results) >= 2:
            self._status_label.setText(base_status + " Classifying within each group...")
            QApplication.processEvents()
            try:
                clusters = self._syn_clusters_spin.value() or None
                classified = {}
                for group_name in self._dataset.group_names:
                    group_results = {
                        t.name: self._synergy_results[t.name]
                        for t in self._dataset.trials_in(group_name)
                        if t.name in self._synergy_results
                    }
                    # Classification needs >= 2 trials to mean anything; a
                    # group with only one trial (or none run) is left
                    # unclassified rather than raising.
                    if len(group_results) < 2:
                        continue
                    classified.update(
                        classify_kmeans(group_results, n_points=n_points, clusters=clusters)
                    )
                self._synergy_classified = classified
                total = sum(r.syns for r in self._synergy_results.values())
                combined = sum(
                    sum(1 for s in r.syn_names if "combined" in s)
                    for r in self._synergy_classified.values()
                )
                n_groups = len(self._dataset.group_names)
                group_note = f" (per group, {n_groups} group(s))" if n_groups > 1 else ""
                self._status_label.setText(
                    f"{base_status} Classified{group_note} "
                    f"({total - combined}/{total} synergies matched, {combined} combined)."
                )
            except Exception as e:
                self._synergy_classified.clear()
                self._status_label.setText(base_status + " Classification failed -- showing per-trial results.")
                QMessageBox.warning(
                    self, "Classification Failed",
                    f"Synergy extraction succeeded but cross-trial classification failed:\n{e}\n\n"
                    "Showing per-trial (unclassified) results instead.",
                )
        else:
            self._status_label.setText(base_status)

        self._populate_synergy_table()
        self._sync_group_summary_option()
        self._refresh_rep_combo()
        self._refresh_cossim_group_combos()
        self._update_chart()

    def _populate_synergy_table(self):
        has_classification = bool(self._synergy_classified)
        cols = ["Trial", "# Synergies", "R2"] + (["Combined"] if has_classification else [])
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
            if has_classification:
                classified = self._synergy_classified.get(name)
                n_combined = sum(1 for s in classified.syn_names if "combined" in s) if classified else 0
                self._result_table.setItem(r, 3, QTableWidgetItem(str(n_combined)))
        self._result_table.resizeColumnsToContents()

    def _run_cossim(self):
        if not self._synergy_classified:
            QMessageBox.information(
                self, "Cosine Similarity",
                "Run Muscle Synergy analysis with classification enabled first.",
            )
            return

        syn_type = "M" if self._cossim_signal_combo.currentIndex() == 0 else "P"
        group_a = self._cossim_group_a_combo.currentText()
        syn_a = self._cossim_syn_a_combo.currentText()
        if not group_a or not syn_a:
            QMessageBox.information(self, "Cosine Similarity", "Choose Group A and Synergy A.")
            return

        classified_a = self._classified_for_group(group_a)
        cross = self._cossim_cross_check.isChecked()

        self._status_label.setText("Computing cosine similarity...")
        QApplication.processEvents()

        try:
            if cross:
                group_b = self._cossim_group_b_combo.currentText()
                syn_b = self._cossim_syn_b_combo.currentText()
                if not group_b or not syn_b:
                    QMessageBox.information(self, "Cosine Similarity", "Choose Group B and Synergy B.")
                    self._status_label.setText("")
                    return
                classified_b = self._classified_for_group(group_b)
                result = cross_group_cossim(
                    classified_a, classified_b, syn_a, syn_b,
                    syn_type=syn_type, n_points=self._synergy_n_points,
                )
                title = f"{group_a}/{syn_a} vs {group_b}/{syn_b} ({syn_type}), cross-group"
            else:
                result = within_group_cossim(
                    classified_a, syn_a, syn_type=syn_type, n_points=self._synergy_n_points,
                )
                title = f"{group_a}/{syn_a} ({syn_type}), within-group"
        except Exception as e:
            QMessageBox.critical(self, "Cosine Similarity Failed", str(e))
            self._status_label.setText("Cosine similarity failed.")
            return

        self._cossim_result = result
        self._cossim_title = title
        summary = similarity_summary(result)
        self._populate_cossim_table(result)
        if summary["n_pairs"]:
            self._status_label.setText(
                f"Cosine similarity computed ({title}): mean={summary['mean']:.3f}, "
                f"sd={summary['sd']:.3f} over {summary['n_pairs']} pair(s)."
            )
        else:
            self._status_label.setText(f"Cosine similarity computed ({title}): no overlapping pairs.")
        self._update_chart()

    def _populate_cossim_table(self, result):
        cols = ["Participant"] + result.col_labels
        self._result_table.setColumnCount(len(cols))
        self._result_table.setHorizontalHeaderLabels(cols)
        self._result_table.setRowCount(len(result.row_labels))
        for r, row_label in enumerate(result.row_labels):
            self._result_table.setItem(r, 0, QTableWidgetItem(row_label))
            for c in range(len(result.col_labels)):
                val = result.matrix[r, c]
                text = "" if np.isnan(val) else f"{val:.3f}"
                self._result_table.setItem(r, c + 1, QTableWidgetItem(text))
        self._result_table.resizeColumnsToContents()

    def _run_wavelet(self):
        channel = self._wavelet_channel_combo.currentText()
        if not channel:
            QMessageBox.information(self, "Wavelet Analysis", "Choose a muscle channel.")
            return
        freq_low = self._wavelet_freq_low_spin.value()
        freq_high = self._wavelet_freq_high_spin.value()
        if freq_low >= freq_high:
            QMessageBox.information(self, "Wavelet Analysis", "Freq low must be less than Freq high.")
            return
        n_points = self._wavelet_npoints_spin.value()

        self._status_label.setText("Running wavelet analysis (this can take a while)...")
        QApplication.processEvents()

        self._wavelet_results.clear()
        self._wavelet_compare_result = None
        self._wavelet_compare_kind = None
        try:
            for trial in self._dataset:
                prepared = self._get_wavelet_prepared_subset(trial, freq_low, freq_high)
                curve = wavelet_medfreq_curve(
                    prepared, channel, n_points=n_points, freq_low=freq_low, freq_high=freq_high,
                )
                self._wavelet_results[trial.name] = curve
                self._status_label.setText(f"Wavelet analysis: {trial.name} done.")
                QApplication.processEvents()
        except Exception as e:
            QMessageBox.critical(self, "Wavelet Analysis Failed", str(e))
            self._status_label.setText("Wavelet analysis failed.")
            return

        self._wavelet_channel = channel
        self._wavelet_n_points = n_points
        self._status_label.setText(f"Wavelet analysis computed for {len(self._wavelet_results)} trial(s).")
        self._populate_wavelet_table()
        self._sync_group_summary_option()
        self._sync_wavelet_compare_option()
        self._refresh_rep_combo()
        self._update_chart()

    def _populate_wavelet_table(self):
        cols = ["Trial", "Mean median freq (Hz)"]
        self._result_table.setColumnCount(len(cols))
        self._result_table.setHorizontalHeaderLabels(cols)
        names = sorted(self._wavelet_results.keys())
        self._result_table.setRowCount(len(names))
        for r, name in enumerate(names):
            curve = self._wavelet_results[name]
            self._result_table.setItem(r, 0, QTableWidgetItem(name))
            self._result_table.setItem(r, 1, QTableWidgetItem(f"{curve.mean():.2f}"))
        self._result_table.resizeColumnsToContents()

    def _run_wavelet_compare(self):
        if not self._wavelet_results:
            QMessageBox.information(self, "Compare Groups", "Run Wavelet Analysis first.")
            return

        group_a = self._wavelet_group_a_combo.currentText()
        if not group_a:
            QMessageBox.information(self, "Compare Groups", "Choose Group A.")
            return
        curves_a = self._wavelet_curves_for_group(group_a)
        if len(curves_a) < 2:
            QMessageBox.information(
                self, "Compare Groups",
                f"Group '{group_a}' needs at least 2 trials with wavelet results.",
            )
            return

        method = self._wavelet_method_combo.currentText()
        cross = self._wavelet_cross_check.isChecked()

        self._status_label.setText("Computing group comparison...")
        QApplication.processEvents()
        try:
            if cross:
                group_b = self._wavelet_group_b_combo.currentText()
                if not group_b:
                    QMessageBox.information(self, "Compare Groups", "Choose Group B.")
                    self._status_label.setText("")
                    return
                curves_b = self._wavelet_curves_for_group(group_b)
                if len(curves_b) < 2:
                    QMessageBox.information(
                        self, "Compare Groups",
                        f"Group '{group_b}' needs at least 2 trials with wavelet results.",
                    )
                    self._status_label.setText("")
                    return
                if method == "SPM":
                    result = spm_compare_curves(curves_a, curves_b)
                    kind = "spm"
                else:
                    result = cross_group_cossim_curves(curves_a, curves_b)
                    kind = "cossim"
                title = f"{group_a} vs {group_b} ({self._wavelet_channel}), cross-group"
            else:
                if method == "SPM":
                    QMessageBox.information(
                        self, "Compare Groups",
                        "SPM requires two groups -- check 'Compare across two groups'.",
                    )
                    self._status_label.setText("")
                    return
                result = within_group_cossim_curves(curves_a)
                kind = "cossim"
                title = f"{group_a} ({self._wavelet_channel}), within-group"
        except Exception as e:
            QMessageBox.critical(self, "Compare Groups Failed", str(e))
            self._status_label.setText("Group comparison failed.")
            return

        self._wavelet_compare_result = result
        self._wavelet_compare_kind = kind
        self._wavelet_compare_title = title
        if kind == "cossim":
            summary = similarity_summary(result)
            self._populate_cossim_table(result)
            if summary["n_pairs"]:
                self._status_label.setText(
                    f"Comparison computed ({title}): mean={summary['mean']:.3f}, "
                    f"sd={summary['sd']:.3f} over {summary['n_pairs']} pair(s)."
                )
            else:
                self._status_label.setText(f"Comparison computed ({title}): no overlapping pairs.")
        else:
            self._populate_spm_table(result)
            verdict = "significant difference detected" if result.h0reject else "no significant difference"
            self._status_label.setText(f"Comparison computed ({title}): {verdict}.")

        self._sync_wavelet_compare_option()
        self._update_chart()

    def _run_spm(self):
        if not self._synergy_classified:
            QMessageBox.information(
                self, "SPM",
                "Run Muscle Synergy analysis with classification enabled first.",
            )
            return

        group_a = self._spm_group_a_combo.currentText()
        syn_a = self._spm_syn_a_combo.currentText()
        group_b = self._spm_group_b_combo.currentText()
        syn_b = self._spm_syn_b_combo.currentText()
        if not all((group_a, syn_a, group_b, syn_b)):
            QMessageBox.information(self, "SPM", "Choose Group A/Synergy A and Group B/Synergy B.")
            return

        alpha = self._spm_alpha_spin.value()
        two_tailed = self._spm_two_tailed_check.isChecked()
        classified_a = self._classified_for_group(group_a)
        classified_b = self._classified_for_group(group_b)

        self._status_label.setText("Running SPM (two-sample t-test)...")
        QApplication.processEvents()
        try:
            result = spm_compare(
                classified_a, classified_b, syn_a, syn_b,
                n_points=self._synergy_n_points, alpha=alpha, two_tailed=two_tailed,
            )
        except Exception as e:
            QMessageBox.critical(self, "SPM Failed", str(e))
            self._status_label.setText("SPM failed.")
            return

        self._spm_result = result
        self._spm_title = f"{group_a}/{syn_a} (n={result.n_a}) vs {group_b}/{syn_b} (n={result.n_b})"
        self._populate_spm_table(result)
        verdict = "significant difference detected" if result.h0reject else "no significant difference"
        self._status_label.setText(f"SPM computed ({self._spm_title}): {verdict} (alpha={alpha}).")
        self._update_chart()

    def _populate_spm_table(self, result):
        cols = ["Cluster start (%)", "Cluster end (%)", "p-value"]
        self._result_table.setColumnCount(len(cols))
        self._result_table.setHorizontalHeaderLabels(cols)
        self._result_table.setRowCount(len(result.clusters))
        for r, (start, end, p) in enumerate(result.clusters):
            self._result_table.setItem(r, 0, QTableWidgetItem(f"{start:.1f}"))
            self._result_table.setItem(r, 1, QTableWidgetItem(f"{end:.1f}"))
            self._result_table.setItem(r, 2, QTableWidgetItem(f"{p:.4f}"))
        self._result_table.resizeColumnsToContents()

    # ── chart ────────────────────────────────────────────────────────────

    def _on_trial_selected(self, _idx):
        self._refresh_rep_combo()
        self._update_chart()

    def _on_repetition_selected(self, _idx):
        self._update_chart()

    def _refresh_rep_combo(self):
        """Populate Repetition with "All" + one entry per rep actually used
        for the selected participant's last run. For Synergy, this only
        affects the activation-pattern (P) subplot when displayed -- the
        muscle-weighting matrix (M) is a single fit across all reps and has
        no per-rep meaning, so it always shows the full-trial fit regardless
        of Repetition. Disabled for the synthetic Group Summary entry, which
        is itself an average across every participant's every rep."""
        trial_name = self._trial_combo.currentText()
        n_reps = None
        analysis_idx = self._analysis_combo.currentIndex()
        if analysis_idx == 0 and trial_name in self._cci_results:
            n_reps = self._cci_results[trial_name][4]
        elif (
            analysis_idx == 1 and trial_name != GROUP_SUMMARY_LABEL
            and trial_name in self._synergy_results and self._synergy_n_points
        ):
            result = self._synergy_classified.get(trial_name) or self._synergy_results[trial_name]
            n_reps = len(result.P) // self._synergy_n_points

        self._rep_combo.blockSignals(True)
        self._rep_combo.clear()
        self._rep_combo.addItem("All")
        if n_reps:
            for i in range(1, n_reps + 1):
                self._rep_combo.addItem(f"Rep {i}")
            self._rep_combo.setEnabled(True)
        else:
            self._rep_combo.setEnabled(False)
        self._rep_combo.setCurrentIndex(0)
        self._rep_combo.blockSignals(False)

    def _clear_chart(self):
        self._chart_view.show_placeholder("Load a data source, then run an analysis.")
        self._refresh_series_swatches([])

    def _update_chart(self):
        analysis_idx = self._analysis_combo.currentIndex()
        if analysis_idx == 2:
            # Cosine Similarity is a group-level comparison, not tied to
            # whichever participant happens to be selected in the
            # Participant combo -- render it regardless of that selection.
            self._show_cossim_chart()
            return
        if analysis_idx == 4:
            # SPM is likewise a two-group comparison, independent of the
            # Participant combo's selection.
            self._show_spm_chart()
            return

        trial_name = self._trial_combo.currentText()
        if not trial_name:
            self._chart_view.show_placeholder("Load a data source first.")
            self._refresh_series_swatches([])
            return

        if analysis_idx == 0 and trial_name in self._cci_results:
            self._show_cci_chart(trial_name)
        elif analysis_idx == 1 and trial_name == GROUP_SUMMARY_LABEL and self._synergy_classified:
            self._show_group_synergy_chart()
        elif analysis_idx == 1 and trial_name in self._synergy_results:
            self._show_synergy_chart(trial_name)
        elif analysis_idx == 3 and trial_name == WAVELET_COMPARE_LABEL and self._wavelet_compare_result is not None:
            self._show_wavelet_compare_chart()
        elif analysis_idx == 3 and trial_name == GROUP_SUMMARY_LABEL and self._wavelet_results:
            self._show_wavelet_group_chart()
        elif analysis_idx == 3 and trial_name in self._wavelet_results:
            self._show_wavelet_chart(trial_name)
        else:
            self._chart_view.show_placeholder("Run an analysis to see results here.")
            self._refresh_series_swatches([])

    @staticmethod
    def _bg_luminance(hex_color):
        h = hex_color.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return (0.299 * r + 0.587 * g + 0.114 * b) / 255

    @staticmethod
    def _hex_to_rgba(hex_color, alpha):
        h = hex_color.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return f"rgba({r},{g},{b},{alpha})"

    def _apply_common_style(self, fig, default_title, xaxis_title=None, yaxis_title=None,
                             xaxis2_title=None, yaxis2_title=None):
        """Apply the user-editable title/legend/background/grid to `fig`,
        plus whichever axis titles the caller resolved. xaxis2/yaxis2 only
        matter for dual-subplot figures (Synergy's weight+activation
        layout) -- callers resolve "user override or computed default"
        themselves (see _chart_style's per-analysis-type label fields)
        rather than this method reaching into style state by a fixed key."""
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
        if xaxis_title:
            layout_kwargs["xaxis_title"] = xaxis_title
        if yaxis_title:
            layout_kwargs["yaxis_title"] = yaxis_title
        if xaxis2_title:
            layout_kwargs["xaxis2_title"] = xaxis2_title
        if yaxis2_title:
            layout_kwargs["yaxis2_title"] = yaxis2_title
        fig.update_layout(**layout_kwargs)
        # No grid by default; user can toggle via the injected modebar button.
        fig.update_xaxes(showgrid=False)
        fig.update_yaxes(showgrid=False)

    def _show_cci_chart(self, trial_name):
        scalar_all, curve_all, a_all, b_all, n_reps = self._cci_results[trial_name]
        chan_a, chan_b = self._cci_muscles
        st = self._chart_style
        dash = None if st["line_dash"] == "solid" else st["line_dash"]

        # rep_idx: 0 = "All" (every included cycle, concatenated); 1..n_reps
        # slices out just that one cycle (same n_points block used to build
        # the concatenated curve/a/b in _run_cci) -- CCI is recomputed from
        # that slice, not just re-displayed, so the value matches the curve.
        rep_idx = self._rep_combo.currentIndex() if self._rep_combo.isEnabled() else 0
        if rep_idx > 0 and n_reps:
            n_points = len(curve_all) // n_reps
            i = rep_idx - 1
            sl = slice(i * n_points, (i + 1) * n_points)
            curve, a, b = curve_all[sl], a_all[sl], b_all[sl]
            scalar = float(np.mean(curve))
            title_suffix = f"rep {rep_idx}/{n_reps}"
        else:
            curve, a, b, scalar = curve_all, a_all, b_all, scalar_all
            title_suffix = f"all {n_reps} rep(s)" if n_reps else "all reps"

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
        st = self._chart_style
        self._apply_common_style(
            fig,
            default_title=f"Co-contraction — {trial_name}, {title_suffix} (CCI = {scalar:.4f})",
            xaxis_title=st["xlabel"] or "sample (normalized cycle)",
            yaxis_title=st["ylabel"] or "EMG amplitude",
        )
        self._chart_view.show_figure(fig, filename_stem=f"{trial_name}_CCI")

    def _show_synergy_chart(self, trial_name):
        result = self._synergy_classified.get(trial_name) or self._synergy_results[trial_name]
        st = self._chart_style
        dash = None if st["line_dash"] == "solid" else st["line_dash"]
        self._refresh_series_swatches(result.syn_names)

        # rep_idx: 0 = "All" (full concatenated activation pattern); 1..n_reps
        # slices out just that cycle's segment. Only affects the activation
        # (P) subplot -- M is a single whole-trial fit, shown unchanged
        # regardless of Repetition (see _refresh_rep_combo's docstring).
        rep_idx = self._rep_combo.currentIndex() if self._rep_combo.isEnabled() else 0
        n_rows = len(result.P)
        if rep_idx > 0 and self._synergy_n_points:
            n_reps = n_rows // self._synergy_n_points
            i = rep_idx - 1
            sl = slice(i * self._synergy_n_points, (i + 1) * self._synergy_n_points)
            p_x = np.arange(self._synergy_n_points)
            rep_suffix = f", rep {rep_idx}/{n_reps}"
        else:
            sl = slice(None)
            p_x = result.P["time"]
            rep_suffix = ""

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
                    x=p_x, y=result.P[result.syn_names[s]].to_numpy()[sl],
                    mode="lines", name=result.syn_names[s],
                    line=dict(color=color, dash=dash),
                    legendgroup=group, showlegend=False,
                ),
                row=1, col=2,
            )
        classified_note = " -- classified across participants" if result.classification == "k-means" else ""
        default_p_xlabel = "sample (normalized cycle)" if rep_suffix else "sample (concatenated cycles)"
        self._apply_common_style(
            fig,
            default_title=f"Muscle synergies — {trial_name}{rep_suffix} ({result.syns} synergies){classified_note}",
            xaxis_title=st["syn_weight_xlabel"] or None,
            yaxis_title=st["syn_weight_ylabel"] or "Weighting (a.u.)",
            xaxis2_title=st["syn_activation_xlabel"] or default_p_xlabel,
            yaxis2_title=st["syn_activation_ylabel"] or "Activation (a.u.)",
        )
        self._chart_view.show_figure(fig, filename_stem=f"{trial_name}_synergy")

    def _show_group_synergy_chart(self):
        """Group-level consensus plot: mean muscle weighting + mean activation
        pattern per classified synergy label, averaged across every
        participant that contributed a (non-"combined") instance of it --
        the "what does Syn1 look like across this cohort" view.

        Scoped to one comparison group (see _active_group_for_summary) --
        classification runs independently per group, so a synergy label
        only means the same thing within one group, never across groups."""
        active_group = self._active_group_for_summary()
        classified_subset = {
            t.name: self._synergy_classified[t.name]
            for t in self._dataset.trials_in(active_group)
            if t.name in self._synergy_classified
        } if active_group else {}

        n_points = self._syn_npoints_spin.value()
        summary = group_synergy_summary(classified_subset, n_points)
        if not summary:
            self._chart_view.show_placeholder(
                "No consistently-classified synergies to summarize for this group."
            )
            self._refresh_series_swatches([])
            return

        labels = list(summary.keys())
        st = self._chart_style
        dash = None if st["line_dash"] == "solid" else st["line_dash"]
        self._refresh_series_swatches(labels)

        fig = make_subplots(
            rows=1, cols=2,
            subplot_titles=("Mean muscle weighting (M) ± SD", "Mean activation pattern (P) ± SD"),
        )
        x = np.arange(n_points)
        for i, label in enumerate(labels):
            info = summary[label]
            color = self._series_color(label, i)
            group_id = f"grp{i}"

            # Weighting: dot at the mean + error bar for SD, one point per
            # muscle -- instead of a plain bar, so mean and spread across
            # participants are both visible without stacking extra traces.
            fig.add_trace(
                go.Scatter(
                    x=info["muscle_names"], y=info["mean_M"], mode="markers",
                    marker=dict(color=color, size=9),
                    error_y=dict(type="data", array=info["sd_M"], visible=True, color=color),
                    name=f"{label} (n={info['n_trials']})",
                    legendgroup=group_id, showlegend=True,
                ),
                row=1, col=1,
            )

            # Activation: mean line + shaded +/-SD band (upper/lower bound
            # traces are invisible lines; the fill between them is the band).
            upper, lower = info["mean_P"] + info["sd_P"], info["mean_P"] - info["sd_P"]
            fig.add_trace(go.Scatter(
                x=x, y=upper, mode="lines", line=dict(width=0),
                legendgroup=group_id, showlegend=False, hoverinfo="skip",
            ), row=1, col=2)
            fig.add_trace(go.Scatter(
                x=x, y=lower, mode="lines", line=dict(width=0),
                fill="tonexty", fillcolor=self._hex_to_rgba(color, 0.25),
                legendgroup=group_id, showlegend=False, hoverinfo="skip",
            ), row=1, col=2)
            fig.add_trace(go.Scatter(
                x=x, y=info["mean_P"], mode="lines", name=label,
                line=dict(color=color, dash=dash),
                legendgroup=group_id, showlegend=False,
            ), row=1, col=2)

        total_trials = len(classified_subset)
        group_suffix = f" ({active_group})" if len(self._dataset.group_names) > 1 else ""
        st = self._chart_style
        self._apply_common_style(
            fig,
            default_title=f"Group synergy summary{group_suffix} — {total_trials} participant(s), {len(labels)} synergies",
            xaxis_title=st["syn_weight_xlabel"] or None,
            yaxis_title=st["syn_weight_ylabel"] or "Weighting (a.u.)",
            xaxis2_title=st["syn_activation_xlabel"] or "sample (normalized cycle)",
            yaxis2_title=st["syn_activation_ylabel"] or "Activation (a.u.)",
        )
        self._chart_view.show_figure(fig, filename_stem="group_synergy_summary")

    def _show_wavelet_chart(self, trial_name):
        curve = self._wavelet_results[trial_name]
        st = self._chart_style
        dash = None if st["line_dash"] == "solid" else st["line_dash"]
        label = self._wavelet_channel
        self._refresh_series_swatches([label])
        color = self._series_color(label, 0)
        x = np.arange(len(curve))

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=x, y=curve, mode="lines", name=label,
            line=dict(color=color, width=2, dash=dash),
        ))
        self._apply_common_style(
            fig,
            default_title=f"Wavelet median frequency — {trial_name} ({label})",
            xaxis_title=st["xlabel"] or "sample (normalized cycle)",
            yaxis_title=st["ylabel"] or "Median frequency (Hz)",
        )
        self._chart_view.show_figure(fig, filename_stem=f"{trial_name}_wavelet")

    def _show_wavelet_group_chart(self):
        """Group-level consensus plot: mean +/- SD instantaneous
        median-frequency curve across every trial in the active group (see
        _active_group_for_summary) -- simpler than Synergy's group summary
        since there's no per-label dimension, just one curve per trial."""
        active_group = self._active_group_for_summary()
        curves = [
            self._wavelet_results[t.name]
            for t in self._dataset.trials_in(active_group)
            if t.name in self._wavelet_results
        ] if active_group else []
        if not curves:
            self._chart_view.show_placeholder("No wavelet results for this group.")
            self._refresh_series_swatches([])
            return

        arr = np.array(curves)
        mean = arr.mean(axis=0)
        sd = arr.std(axis=0, ddof=1) if len(curves) > 1 else np.zeros_like(mean)

        st = self._chart_style
        dash = None if st["line_dash"] == "solid" else st["line_dash"]
        label = self._wavelet_channel
        self._refresh_series_swatches([label])
        color = self._series_color(label, 0)
        x = np.arange(len(mean))
        upper, lower = mean + sd, mean - sd

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=x, y=upper, mode="lines", line=dict(width=0),
            showlegend=False, hoverinfo="skip",
        ))
        fig.add_trace(go.Scatter(
            x=x, y=lower, mode="lines", line=dict(width=0),
            fill="tonexty", fillcolor=self._hex_to_rgba(color, 0.25),
            showlegend=False, hoverinfo="skip",
        ))
        fig.add_trace(go.Scatter(
            x=x, y=mean, mode="lines", name=f"{label} (n={len(curves)})",
            line=dict(color=color, dash=dash),
        ))

        group_suffix = f" ({active_group})" if len(self._dataset.group_names) > 1 else ""
        self._apply_common_style(
            fig,
            default_title=f"Wavelet group summary{group_suffix} — {len(curves)} participant(s), {label}",
            xaxis_title=st["xlabel"] or "sample (normalized cycle)",
            yaxis_title=st["ylabel"] or "Median frequency (Hz)",
        )
        self._chart_view.show_figure(fig, filename_stem="wavelet_group_summary")

    def _show_wavelet_compare_chart(self):
        """Dispatches to whichever render helper matches the last "Compare
        Groups" run's method -- the same heatmap/SPM panel Cosine
        Similarity/SPM (Synergy) use, since within_group_cossim_curves/
        cross_group_cossim_curves/spm_compare_curves return the same
        SimilarityMatrix/SPMResult shapes."""
        if self._wavelet_compare_kind == "spm":
            self._render_spm_chart(self._wavelet_compare_result, self._wavelet_compare_title)
        else:
            self._render_cossim_heatmap(self._wavelet_compare_result, self._wavelet_compare_title)

    def _show_cossim_chart(self):
        if self._cossim_result is None:
            self._chart_view.show_placeholder(
                "Choose group(s)/synergy signal, then click Run Analysis."
            )
            self._refresh_series_swatches([])
            return
        self._render_cossim_heatmap(self._cossim_result, self._cossim_title)

    def _render_cossim_heatmap(self, result, title):
        """Heatmap of a within-/cross-group cosine-similarity result --
        Plotly equivalent of the R reference scripts' ggplot geom_tile
        heatmap (R/plot_similarity_heatmaps.R), including the same
        mean/mean+1SD threshold suggestion the R script prints. Shared by
        Cosine Similarity (Synergy) and Wavelet's own group comparison,
        which both produce the same SimilarityMatrix shape."""
        z = result.matrix
        text = [[("" if np.isnan(v) else f"{v:.2f}") for v in row] for row in z]
        fig = go.Figure(data=go.Heatmap(
            z=z, x=result.col_labels, y=result.row_labels,
            zmin=-1, zmax=1, zmid=0,
            colorscale=[[0, "#3d5afe"], [0.5, "#f8f8f2"], [1, "#ff5555"]],
            text=text, texttemplate="%{text}",
            colorbar=dict(title="cos sim"),
        ))
        fig.update_yaxes(autorange="reversed")  # first participant at the top, matching the table

        summary = similarity_summary(result)
        subtitle = (
            f" (mean={summary['mean']:.2f}, mean+1sd={summary['threshold']:.2f})"
            if summary["n_pairs"] else " (no overlapping pairs)"
        )
        self._refresh_series_swatches([])
        st = self._chart_style
        self._apply_common_style(
            fig,
            default_title=f"Cosine similarity — {title}{subtitle}",
            xaxis_title=st["xlabel"] or "Participant",
            yaxis_title=st["ylabel"] or "Participant",
        )
        self._chart_view.show_figure(fig, filename_stem="cosine_similarity")

    def _show_spm_chart(self):
        if self._spm_result is None:
            self._chart_view.show_placeholder(
                "Choose Group A/B and Synergy A/B, then click Run Analysis."
            )
            self._refresh_series_swatches([])
            return
        self._render_spm_chart(self._spm_result, self._spm_title)

    def _render_spm_chart(self, result, title):
        """Two-panel SPM view -- mean +/- SD curve per group (spm1d's
        plot_meanSD equivalent) alongside the SPM{t} statistic curve with
        its RFT threshold and shaded significant cluster(s) (spmi.plot() +
        plot_threshold_label() + plot_p_values() equivalent), matching this
        project's R/MATLAB reference (MS_SPM.m). Shared by SPM (Synergy)
        and Wavelet's own group comparison, which both produce the same
        SPMResult shape."""
        st = self._chart_style
        dash = None if st["line_dash"] == "solid" else st["line_dash"]
        labels = ["Group A", "Group B"]
        self._refresh_series_swatches(labels)
        color_a = self._series_color(labels[0], 0)
        color_b = self._series_color(labels[1], 1)

        n_points = len(result.mean_a)
        x = np.arange(n_points)

        fig = make_subplots(
            rows=1, cols=2,
            subplot_titles=("Mean activation pattern (P) ± SD", f"SPM{{t}} (alpha={result.alpha})"),
        )

        for mean, sd, color, label, n in (
            (result.mean_a, result.sd_a, color_a, labels[0], result.n_a),
            (result.mean_b, result.sd_b, color_b, labels[1], result.n_b),
        ):
            upper, lower = mean + sd, mean - sd
            group_id = f"spm_{label}"
            fig.add_trace(go.Scatter(
                x=x, y=upper, mode="lines", line=dict(width=0),
                legendgroup=group_id, showlegend=False, hoverinfo="skip",
            ), row=1, col=1)
            fig.add_trace(go.Scatter(
                x=x, y=lower, mode="lines", line=dict(width=0),
                fill="tonexty", fillcolor=self._hex_to_rgba(color, 0.25),
                legendgroup=group_id, showlegend=False, hoverinfo="skip",
            ), row=1, col=1)
            fig.add_trace(go.Scatter(
                x=x, y=mean, mode="lines", name=f"{label} (n={n})",
                line=dict(color=color, dash=dash),
                legendgroup=group_id, showlegend=True,
            ), row=1, col=1)

        z_x = np.linspace(0, n_points - 1, len(result.z))
        fig.add_trace(go.Scatter(
            x=z_x, y=result.z, mode="lines", name="SPM{t}",
            line=dict(color="#f8f8f2", width=2), showlegend=False,
        ), row=1, col=2)
        for bound in (result.zstar, -result.zstar):
            fig.add_trace(go.Scatter(
                x=[z_x[0], z_x[-1]], y=[bound, bound], mode="lines",
                line=dict(color="#ff5555", dash="dash", width=1),
                showlegend=False, hoverinfo="skip",
            ), row=1, col=2)
        for start, end, _p in result.clusters:
            fig.add_vrect(x0=start, x1=end, row=1, col=2, fillcolor="#ff5555", opacity=0.2, line_width=0)

        verdict = "significant" if result.h0reject else "not significant"
        self._apply_common_style(
            fig,
            default_title=f"SPM — {title} ({verdict}, alpha={result.alpha})",
            xaxis_title=st["xlabel"] or "sample (normalized cycle)",
            yaxis_title=st["ylabel"] or "Activation (a.u.)",
            xaxis2_title="sample (normalized cycle)",
            yaxis2_title="SPM{t}",
        )
        self._chart_view.show_figure(fig, filename_stem="spm")

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
