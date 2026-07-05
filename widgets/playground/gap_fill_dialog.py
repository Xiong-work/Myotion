"""widgets/playground/gap_fill_dialog.py -- load a .c3d file, inspect marker
gaps (per-marker gap list, an X/Y/Z trajectory plot with the selected gap
highlighted, and a 3D preview with a frame scrubber -- the same information
Vicon Nexus's own gap-fill tools show), fill gaps via one of five methods
(Spline/Woltring, Pattern Fill, Rigid Body, Cyclic Fill, Linear Fill), and
export a new .c3d file. The original file is never modified.

Markers can be multi-selected (ctrl/shift-click) in the list or clicked
directly in the 3D view; both drive the same selection state, which controls
3D highlighting and which markers are overlaid on the trajectory plot. The
reference/donor marker(s) picked for a Rigid Body or Pattern fill get their
own highlight color, distinct from the "selected for inspection" set. A
dashed playhead line on the trajectory plot tracks the frame scrubber/3D
view so the two stay easy to follow together.
"""

import os

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFileDialog,
    QListWidget, QMessageBox, QSplitter, QWidget, QComboBox, QSlider,
    QAbstractItemView, QCheckBox, QInputDialog,
)

from modules.kinematics.items import PointItem
from modules.kinematics.items.cameraframustumitem import lab_to_scene
from modules.playground.gap_fill import (
    load_c3d_markers, detect_gaps, spline_fill, pattern_fill, rigid_body_fill,
    cyclic_fill, linear_fill, rename_marker, export_c3d, GapFillError, DEFAULT_SPLINE_MAX_GAP,
)
from widgets.playground_gl_view import PlaygroundGLView

_PRESENT_COLOR = [0.3, 0.8, 0.3]           # marker visible this frame, not selected/reference
_SELECTED_COLOR = [1.0, 0.85, 0.0]         # yellow -- selected in the marker list or clicked in 3D
_REFERENCE_COLOR = [0.2, 0.85, 0.9]        # cyan -- picked as a Rigid Body fill reference marker
_PLOT_COLORS = [
    "#e74c3c", "#27ae60", "#2980b9", "#f39c12", "#8e44ad", "#16a085", "#d35400", "#2c3e50",
]

_METHOD_SPLINE = "Spline (Woltring)"
_METHOD_PATTERN = "Pattern Fill"
_METHOD_RIGID = "Rigid Body"
_METHOD_CYCLIC = "Cyclic Fill"
_METHOD_LINEAR = "Linear Fill"
_METHODS_NEEDING_LONG_GAP_WARNING = {_METHOD_SPLINE, _METHOD_LINEAR}


class GapFillDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("C3D Gap Fill"))
        self.resize(1250, 750)

        self._marker_data = None
        self._marker_points = None  # working copy -- mutated in place as gaps get filled
        self._gaps = {}             # marker name -> [Gap, ...], recomputed after every fill
        self._current_marker = None  # drives the Gap List / Fill target
        self._current_gap = None
        self._loaded_path = None

        self._play_timer = QTimer(self)
        self._play_timer.setInterval(33)
        self._play_timer.timeout.connect(self._advance_frame)

        layout = QVBoxLayout(self)

        top_row = QHBoxLayout()
        self._load_btn = QPushButton(self.tr("Load C3D..."))
        self._load_btn.clicked.connect(self._on_load)
        top_row.addWidget(self._load_btn)
        self._file_label = QLabel(self.tr("No file loaded"))
        top_row.addWidget(self._file_label, 1)
        self._export_btn = QPushButton(self.tr("Export Filled C3D..."))
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._on_export)
        top_row.addWidget(self._export_btn)
        layout.addLayout(top_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter, 1)

        # Left: marker list (multi-select) + gap list for the "current" marker
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel(self.tr("Markers (ctrl/shift-click for multiple)")))
        self._marker_list = QListWidget()
        self._marker_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._marker_list.currentRowChanged.connect(self._on_marker_current_changed)
        self._marker_list.itemSelectionChanged.connect(self._on_marker_selection_changed)
        left_layout.addWidget(self._marker_list, 2)
        self._rename_btn = QPushButton(self.tr("Rename Marker..."))
        self._rename_btn.setEnabled(False)
        self._rename_btn.clicked.connect(self._on_rename_marker)
        left_layout.addWidget(self._rename_btn)
        left_layout.addWidget(QLabel(self.tr("Gaps for current marker")))
        self._gap_list = QListWidget()
        self._gap_list.currentRowChanged.connect(self._on_gap_selected)
        left_layout.addWidget(self._gap_list, 1)
        splitter.addWidget(left)

        # Middle: X/Y/Z trajectory subplots + fill controls
        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)

        axis_toggle_row = QHBoxLayout()
        axis_toggle_row.addWidget(QLabel(self.tr("Show axes:")))
        self._axis_checks = {}
        for axis_name in "XYZ":
            cb = QCheckBox(axis_name)
            cb.setChecked(True)
            cb.toggled.connect(lambda checked, a=axis_name: self._plots[a].setVisible(checked))
            axis_toggle_row.addWidget(cb)
            self._axis_checks[axis_name] = cb
        axis_toggle_row.addStretch()
        middle_layout.addLayout(axis_toggle_row)

        self._plots = {}        # axis -> PlotWidget
        self._curves = {}       # axis -> {marker_name: PlotDataItem}
        self._gap_regions = {}  # axis -> LinearRegionItem
        self._playheads = {}    # axis -> InfiniteLine, kept in sync with the frame scrubber/3D view
        for axis_i, axis_name in enumerate("XYZ"):
            plot = pg.PlotWidget()
            plot.setBackground("#e5ecf6")
            plot.setLabel("left", axis_name)
            if axis_i == 2:
                plot.setLabel("bottom", self.tr("Frame"))
            if axis_i > 0:
                plot.setXLink(self._plots["X"])
            region = pg.LinearRegionItem(movable=False, brush=pg.mkBrush("#e74c3c30"))
            region.setVisible(False)
            plot.addItem(region)
            playhead = pg.InfiniteLine(pos=0, angle=90, pen=pg.mkPen("#333333", width=1, style=Qt.PenStyle.DashLine))
            plot.addItem(playhead)
            self._plots[axis_name] = plot
            self._gap_regions[axis_name] = region
            self._playheads[axis_name] = playhead
            self._curves[axis_name] = {}
            middle_layout.addWidget(plot, 1)

        method_row = QHBoxLayout()
        method_row.addWidget(QLabel(self.tr("Method:")))
        self._method_combo = QComboBox()
        self._method_combo.addItems([
            self.tr(_METHOD_SPLINE), self.tr(_METHOD_PATTERN), self.tr(_METHOD_RIGID),
            self.tr(_METHOD_CYCLIC), self.tr(_METHOD_LINEAR),
        ])
        self._method_combo.currentIndexChanged.connect(self._on_method_changed)
        method_row.addWidget(self._method_combo)
        method_row.addStretch()
        self._clear_plot_btn = QPushButton(self.tr("Clear Plot"))
        self._clear_plot_btn.clicked.connect(self._marker_list.clearSelection)
        method_row.addWidget(self._clear_plot_btn)
        middle_layout.addLayout(method_row)

        self._ref_label = QLabel("")
        self._ref_label.setVisible(False)
        middle_layout.addWidget(self._ref_label)
        self._ref_list = QListWidget()
        self._ref_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._ref_list.setVisible(False)
        self._ref_list.setMaximumHeight(110)
        self._ref_list.itemSelectionChanged.connect(self._update_3d_frame)
        middle_layout.addWidget(self._ref_list)

        fill_row = QHBoxLayout()
        self._fill_btn = QPushButton(self.tr("Fill Selected Gap"))
        self._fill_btn.setEnabled(False)
        self._fill_btn.clicked.connect(self._on_fill_gap)
        fill_row.addWidget(self._fill_btn)
        self._spline_all_btn = QPushButton(
            self.tr("Spline-Fill All Short Gaps (<= {0} frames)").format(DEFAULT_SPLINE_MAX_GAP)
        )
        self._spline_all_btn.setEnabled(False)
        self._spline_all_btn.clicked.connect(self._on_spline_fill_all_short)
        fill_row.addWidget(self._spline_all_btn)
        middle_layout.addLayout(fill_row)

        splitter.addWidget(middle)

        # Right: 3D preview + frame scrubber
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self._gl_view = PlaygroundGLView(
            camera_far=8000, camera_position=(0, 1500, 3000), grid_size=3000, axis_length=300,
        )
        self._gl_view.itemPicked.connect(self._on_gl_picked)
        right_layout.addWidget(self._gl_view, 1)
        scrub_row = QHBoxLayout()
        self._play_btn = QPushButton(self.tr("Play"))
        self._play_btn.setEnabled(False)
        self._play_btn.clicked.connect(self._on_play_toggled)
        scrub_row.addWidget(self._play_btn)
        self._frame_slider = QSlider(Qt.Orientation.Horizontal)
        self._frame_slider.setEnabled(False)
        self._frame_slider.valueChanged.connect(self._on_frame_changed)
        scrub_row.addWidget(self._frame_slider, 1)
        self._frame_label = QLabel("0 / 0")
        scrub_row.addWidget(self._frame_label)
        right_layout.addLayout(scrub_row)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setStretchFactor(2, 2)

        self._status_label = QLabel(self.tr("Load a C3D file to begin."))
        layout.addWidget(self._status_label)

        hint = QLabel(self.tr("3D preview: green = present, yellow = selected, cyan = reference/donor marker. "
                              "The dashed line on the plot tracks the current frame. "
                              "Click a marker to select it • drag to orbit • scroll to zoom • middle-drag to pan."))
        hint.setStyleSheet("color: rgba(255,255,255,0.5); font-size: 11px;")
        layout.addWidget(hint)

    # -- load -----------------------------------------------------------------

    def _on_load(self):
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Load C3D File"), "", self.tr("C3D files (*.c3d);;All files (*)"),
        )
        if not path:
            return
        try:
            marker_data = load_c3d_markers(path)
        except GapFillError as e:
            QMessageBox.warning(self, self.tr("Load Failed"), str(e))
            return
        except Exception as e:
            QMessageBox.warning(self, self.tr("Load Failed"), self.tr(f"Could not load file: {e}"))
            return

        self._marker_data = marker_data
        self._marker_points = marker_data.marker_points.copy()
        self._loaded_path = path
        self._file_label.setText(os.path.basename(path))
        self._export_btn.setEnabled(True)
        self._spline_all_btn.setEnabled(True)
        self._play_btn.setEnabled(True)
        self._frame_slider.setEnabled(True)
        self._rename_btn.setEnabled(True)

        self._ref_list.clear()
        self._ref_list.addItems(marker_data.labels)

        n_frames = self._marker_points.shape[2]
        self._frame_slider.blockSignals(True)
        self._frame_slider.setRange(0, max(0, n_frames - 1))
        self._frame_slider.setValue(0)
        self._frame_slider.blockSignals(False)
        self._frame_label.setText(f"0 / {n_frames - 1}")

        self._refresh_gaps()
        self._refresh_marker_list()
        if self._marker_list.count() > 0:
            self._marker_list.setCurrentRow(0)
        self._update_3d_frame()
        self._status_label.setText(
            self.tr("Loaded {0} markers, {1} frames @ {2:.0f} Hz.").format(
                len(marker_data.labels), n_frames, marker_data.frame_rate,
            )
        )

    def _refresh_gaps(self):
        self._gaps = detect_gaps(self._marker_points, self._marker_data.labels)

    def _refresh_marker_list(self):
        prev_row = self._marker_list.currentRow()
        self._marker_list.blockSignals(True)
        self._marker_list.clear()
        for label in self._marker_data.labels:
            glist = self._gaps.get(label, [])
            if glist:
                n_missing = sum(g.length for g in glist)
                text = f"{label}  ({len(glist)} gap{'s' if len(glist) != 1 else ''}, {n_missing} frames)"
            else:
                text = f"{label}  (complete)"
            self._marker_list.addItem(text)
            self._marker_list.item(self._marker_list.count() - 1).setData(Qt.ItemDataRole.UserRole, label)
        self._marker_list.blockSignals(False)
        if 0 <= prev_row < self._marker_list.count():
            self._marker_list.setCurrentRow(prev_row)

    def _on_rename_marker(self):
        if self._current_marker is None:
            QMessageBox.information(self, self.tr("Rename Marker"), self.tr("Select a marker first."))
            return
        old_name = self._current_marker
        new_name, ok = QInputDialog.getText(
            self, self.tr("Rename Marker"), self.tr("New name for '{0}':").format(old_name), text=old_name,
        )
        if not ok:
            return
        new_name = new_name.strip()
        if new_name == old_name:
            return
        if not new_name:
            QMessageBox.warning(self, self.tr("Rename Failed"), self.tr("Marker name cannot be empty."))
            return
        if new_name in self._marker_data.labels:
            QMessageBox.warning(
                self, self.tr("Rename Failed"), self.tr("A marker named '{0}' already exists.").format(new_name),
            )
            return

        reply = QMessageBox.warning(
            self, self.tr("Rename Marker"),
            self.tr("Renaming '{0}' to '{1}' changes the marker name written to the exported C3D. Downstream "
                    "scripts/pipelines (e.g. OpenSim scaling, rigid-body cluster definitions) that expect the "
                    "original name '{0}' may no longer recognize this marker. Continue?").format(old_name, new_name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            rename_marker(self._marker_data, old_name, new_name)
        except GapFillError as e:
            QMessageBox.warning(self, self.tr("Rename Failed"), str(e))
            return

        # cascade the rename through dialog-side bookkeeping that keys on marker name
        if old_name in self._gaps:
            gap_list = self._gaps.pop(old_name)
            for g in gap_list:
                g.marker = new_name
            self._gaps[new_name] = gap_list
        if self._current_marker == old_name:
            self._current_marker = new_name
        for i in range(self._ref_list.count()):
            if self._ref_list.item(i).text() == old_name:
                self._ref_list.item(i).setText(new_name)
                break
        for curves in self._curves.values():
            if old_name in curves:
                curves[new_name] = curves.pop(old_name)

        self._refresh_marker_list()
        self._refresh_gap_list()
        self._update_trajectory_plot()
        self._update_3d_frame()
        self._status_label.setText(self.tr("Renamed '{0}' to '{1}'.").format(old_name, new_name))

    # -- marker / gap selection -------------------------------------------------

    def _selected_marker_names(self):
        return [item.data(Qt.ItemDataRole.UserRole) for item in self._marker_list.selectedItems()]

    def _reference_marker_names(self):
        if not self._ref_list.isVisible():
            return []
        return [item.text() for item in self._ref_list.selectedItems()]

    def _on_marker_current_changed(self, row):
        """Drives the Gap List / Fill target -- separate from the (possibly
        multi-row) selection used for 3D highlight + plot overlay."""
        if row < 0 or self._marker_data is None:
            return
        self._current_marker = self._marker_data.labels[row]
        self._refresh_gap_list()

    def _on_marker_selection_changed(self):
        selected = self._selected_marker_names()
        if selected:
            self._status_label.setText(self.tr("Selected: {0}").format(", ".join(selected)))
        self._update_trajectory_plot()
        self._update_3d_frame()

    def _refresh_gap_list(self):
        self._gap_list.blockSignals(True)
        self._gap_list.clear()
        for g in self._gaps.get(self._current_marker, []):
            self._gap_list.addItem(f"frames {g.start}-{g.end}  ({g.length} frames)")
        self._gap_list.blockSignals(False)
        self._current_gap = None
        self._fill_btn.setEnabled(False)
        for region in self._gap_regions.values():
            region.setVisible(False)

    def _on_gap_selected(self, row):
        glist = self._gaps.get(self._current_marker, [])
        if row < 0 or row >= len(glist):
            self._current_gap = None
            self._fill_btn.setEnabled(False)
            for region in self._gap_regions.values():
                region.setVisible(False)
            return
        self._current_gap = glist[row]
        self._fill_btn.setEnabled(True)
        for region in self._gap_regions.values():
            region.setRegion((self._current_gap.start, self._current_gap.end))
            region.setVisible(True)

    def _on_method_changed(self, _index):
        method = self._method_combo.currentText()
        self._ref_list.clearSelection()
        if method == self.tr(_METHOD_RIGID):
            self._ref_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            self._ref_label.setText(self.tr("Reference markers (rigid body -- ctrl/shift-click to pick 2+):"))
            self._ref_label.setVisible(True)
            self._ref_list.setVisible(True)
        elif method == self.tr(_METHOD_PATTERN):
            self._ref_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self._ref_label.setText(self.tr("Donor marker (pattern fill -- pick 1):"))
            self._ref_label.setVisible(True)
            self._ref_list.setVisible(True)
        else:
            self._ref_label.setVisible(False)
            self._ref_list.setVisible(False)
        self._update_3d_frame()  # reference/donor highlighting only applies while the ref list is visible

    def _update_trajectory_plot(self):
        selected = self._selected_marker_names()
        for axis_i, axis_name in enumerate("XYZ"):
            curves = self._curves[axis_name]
            # drop curves for markers no longer selected
            for name in list(curves.keys()):
                if name not in selected:
                    self._plots[axis_name].removeItem(curves.pop(name))
            for i, name in enumerate(selected):
                idx = self._marker_data.labels.index(name)
                series = self._marker_points[axis_i, idx, :]
                frames = np.arange(series.shape[0])
                color = _PLOT_COLORS[i % len(_PLOT_COLORS)]
                if name in curves:
                    curves[name].setData(frames, series)
                else:
                    curves[name] = self._plots[axis_name].plot(
                        frames, series, pen=pg.mkPen(color, width=1), name=name,
                    )
        title = ", ".join(selected) if selected else self.tr("(no marker selected)")
        self._plots["X"].setTitle(title)

    # -- filling ----------------------------------------------------------------

    def _on_fill_gap(self):
        if self._current_gap is None:
            return
        g = self._current_gap
        method = self._method_combo.currentText()

        if method in (self.tr(m) for m in _METHODS_NEEDING_LONG_GAP_WARNING) and g.length > DEFAULT_SPLINE_MAX_GAP:
            reply = QMessageBox.question(
                self, self.tr("Long Gap"),
                self.tr("This gap is {0} frames. {1} over long gaps can look physically implausible for "
                        "cyclic motion (e.g. gait) -- Rigid Body or Cyclic fill is usually better for gaps "
                        "this long. Continue anyway?").format(g.length, method),
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        try:
            if method == self.tr(_METHOD_RIGID):
                ref_labels = self._reference_marker_names()
                self._marker_points = rigid_body_fill(
                    self._marker_points, self._marker_data.labels, g.marker, ref_labels, g.start, g.end,
                )
            elif method == self.tr(_METHOD_PATTERN):
                donors = self._reference_marker_names()
                if not donors:
                    QMessageBox.warning(self, self.tr("Fill Failed"), self.tr("Pick a donor marker first."))
                    return
                self._marker_points = pattern_fill(
                    self._marker_points, self._marker_data.labels, g.marker, donors[0], g.start, g.end,
                )
            elif method == self.tr(_METHOD_CYCLIC):
                self._marker_points = cyclic_fill(self._marker_points, g.marker_idx, g.start, g.end)
            elif method == self.tr(_METHOD_LINEAR):
                self._marker_points = linear_fill(self._marker_points, g.marker_idx, g.start, g.end)
            else:
                self._marker_points = spline_fill(self._marker_points, g.marker_idx, g.start, g.end)
        except GapFillError as e:
            QMessageBox.warning(self, self.tr("Fill Failed"), str(e))
            return

        self._refresh_gaps()
        self._refresh_marker_list()
        self._refresh_gap_list()
        self._update_trajectory_plot()
        self._update_3d_frame()
        self._status_label.setText(self.tr("Filled {0}: frames {1}-{2}.").format(g.marker, g.start, g.end))

    def _on_spline_fill_all_short(self):
        if self._marker_data is None:
            return
        filled_count, failed_count = 0, 0
        for label in self._marker_data.labels:
            # Re-detect per marker as we go -- filling one gap can only ever
            # shrink/remove entries for THIS marker, so re-running detect_gaps
            # for the whole array once per marker keeps indices correct
            # without re-scanning markers already processed.
            for g in list(detect_gaps(self._marker_points, self._marker_data.labels).get(label, [])):
                if g.length > DEFAULT_SPLINE_MAX_GAP:
                    continue
                try:
                    self._marker_points = spline_fill(self._marker_points, g.marker_idx, g.start, g.end)
                    filled_count += 1
                except GapFillError:
                    failed_count += 1

        self._refresh_gaps()
        self._refresh_marker_list()
        self._refresh_gap_list()
        self._update_trajectory_plot()
        self._update_3d_frame()
        self._status_label.setText(
            self.tr("Spline-filled {0} short gap(s); {1} could not be filled (touch the start/end of the trial).")
            .format(filled_count, failed_count)
        )

    # -- 3D preview ---------------------------------------------------------------

    def _on_frame_changed(self, value):
        n_frames = self._marker_points.shape[2] if self._marker_points is not None else 0
        self._frame_label.setText(f"{value} / {max(0, n_frames - 1)}")
        for playhead in self._playheads.values():
            playhead.setPos(value)
        self._update_3d_frame()

    def _on_play_toggled(self):
        if self._play_timer.isActive():
            self._play_timer.stop()
            self._play_btn.setText(self.tr("Play"))
        else:
            self._play_timer.start()
            self._play_btn.setText(self.tr("Pause"))

    def _advance_frame(self):
        n_frames = self._marker_points.shape[2]
        self._frame_slider.setValue((self._frame_slider.value() + 1) % n_frames)

    def _on_gl_picked(self, name):
        """Clicking a marker in the 3D view selects it (replacing the
        current selection) -- mirrors clicking it in the marker list."""
        if not name or self._marker_data is None:
            return
        try:
            row = self._marker_data.labels.index(name)
        except ValueError:
            return
        self._marker_list.clearSelection()
        item = self._marker_list.item(row)
        item.setSelected(True)
        self._marker_list.setCurrentItem(item)

    def _update_3d_frame(self):
        if self._marker_points is None:
            return
        frame = self._frame_slider.value()
        selected = set(self._selected_marker_names())
        references = set(self._reference_marker_names())

        self._gl_view.clearItems()
        for m, label in enumerate(self._marker_data.labels):
            pos = self._marker_points[:, m, frame]
            if np.isnan(pos).any():
                continue
            scene_pos = lab_to_scene(pos)
            if label in selected:
                color = _SELECTED_COLOR
            elif label in references:
                color = _REFERENCE_COLOR
            else:
                color = _PRESENT_COLOR
            self._gl_view.addPickableItem(
                label, "point", [scene_pos],
                factory=lambda color=color, pos=scene_pos: PointItem([pos], [color]),
                normal_color=color,
            )

    # -- export -----------------------------------------------------------------

    def _on_export(self):
        if self._marker_data is None:
            return
        default_path = os.path.splitext(self._loaded_path)[0] + "_filled.c3d"
        path, _ = QFileDialog.getSaveFileName(
            self, self.tr("Export Filled C3D"), default_path, self.tr("C3D files (*.c3d)"),
        )
        if not path:
            return
        try:
            export_c3d(self._marker_data, self._marker_points, path)
        except GapFillError as e:
            QMessageBox.warning(self, self.tr("Export Failed"), str(e))
            return
        QMessageBox.information(self, self.tr("Exported"), self.tr("Saved gap-filled C3D to:\n{0}").format(path))
