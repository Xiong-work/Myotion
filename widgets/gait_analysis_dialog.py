"""widgets/gait_analysis_dialog.py -- Gait Analysis, a standalone full-screen
popup module reached from the Quick Start row (see main.py's pushButton_2).

Unlike Kinematics Inspection (a page tied to a Workspace participant), this
dialog operates directly on a picked .c3d file -- pick a file, get a trial
you can scrub through -- the same "no project required" spirit as the
Playground tools. Under the hood it builds a bare workspace.profile(emg, kin)
and reuses the exact same Model/Controller/RenderWidget/PlayBarWidget/
PlayPlotWidget stack Kinematics Inspection uses, so marker/force-plate
rendering and playback behave identically.
"""

import csv
import os

import numpy as np

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFileDialog,
    QMessageBox, QSplitter, QTreeWidget, QTreeWidgetItem,
    QAbstractItemView, QPlainTextEdit, QComboBox, QFormLayout, QDialogButtonBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QMenu, QInputDialog,
    QGroupBox, QDoubleSpinBox, QSpinBox, QCheckBox,
)

from modules.pyMotion.core.c3d import c3dFile, c3d_probe
from modules.pyMotion.core.kinematic import kinematic
from modules.pyMotion.core.emg import emg, _is_non_emg_channel
from modules.pyMotion.core.workspace import workspace
from modules.pyMotion.core.stitch import stitch_c3d, check_alignment, StitchError
from modules.pyMotion.core.trial import TrialEvent
from modules.pyMotion.core import gait_events as _gait
from modules.kinematics.model import Model
from modules.kinematics.controller import Controller
from modules.kinematics.renderwidget import RenderWidget
from modules.kinematics.playbarwidget import PlayBarWidget, buttonStyle
from modules.kinematics.playplotview import PlayPlotWidget
from widgets.gait_report_dialog import _GaitReportDialog


def _populate_label_tree(tree: QTreeWidget, profile) -> None:
    """Fill *tree* with Markers / Model Outputs / EMG / Force Plates / Events
    groups for *profile* (a workspace.profile). Mirrors main.py's
    populateKinematicTree() but for a single standalone profile with no
    Workspace/participant wrapper node.
    """
    tree.clear()
    tree.setColumnCount(1)
    k = profile.kinematic
    e = profile.emg

    if k.isValid():
        marker_group = QTreeWidgetItem(tree)
        marker_group.setText(0, "Markers ({})".format(len(k.reallabels)))
        for point in k.reallabels:
            QTreeWidgetItem(marker_group, [point])
        marker_group.setExpanded(False)

        if k.anglelabels:
            angle_group = QTreeWidgetItem(tree)
            angle_group.setText(0, "Model Outputs")
            angles_node = QTreeWidgetItem(angle_group)
            angles_node.setText(0, "Angles ({})".format(len(k.anglelabels)))
            for label in k.anglelabels:
                QTreeWidgetItem(angles_node, [label])
            angle_group.setExpanded(False)
            angles_node.setExpanded(False)

    # Raw Channels, not getChannels() -- this standalone dialog has no
    # enable/disable wizard step, and tree_item_select() (controller.py)
    # matches against Channels directly, not the enabled subset.
    emg_channels = e.Channels
    if emg_channels:
        emg_group = QTreeWidgetItem(tree)
        emg_group.setText(0, "EMG ({})".format(len(emg_channels)))
        for c in emg_channels:
            QTreeWidgetItem(emg_group, [c])
        emg_group.setExpanded(False)

    if k.force_plates:
        fp_group = QTreeWidgetItem(tree)
        fp_group.setText(0, "Force Plates ({})".format(len(k.force_plates)))
        for fp in k.force_plates:
            plate_node = QTreeWidgetItem(fp_group)
            plate_node.setText(0, "Plate {}".format(fp.plate_id))
            for comp in ("Fx", "Fy", "Fz"):
                QTreeWidgetItem(plate_node, ["Plate{} {}".format(fp.plate_id, comp)])
        fp_group.setExpanded(False)

    all_events = sorted(list(k.events) + list(profile.extra_events), key=lambda ev: ev.time_s)
    if all_events:
        ev_group = QTreeWidgetItem(tree)
        ev_group.setText(0, "Events ({})".format(len(all_events)))
        for ev in all_events:
            source = "" if ev in profile.extra_events else " [C3D]"
            QTreeWidgetItem(ev_group, [
                "{} | {} | {:.3f}s{}".format(ev.label, ev.context, ev.time_s, source)
            ])
        ev_group.setExpanded(False)

    tree.setHeaderItem(QTreeWidgetItem(["Trial"]))


_NO_MARKER = "(none)"


class _GaitMarkerMappingDialog(QDialog):
    """Lets the user confirm/override which loaded marker acts as each of
    the right/left heel and toe before gait event detection runs, pre-filled
    with guess_gait_markers()'s best-effort match. Shown on every "Detect
    Gait Events" click -- an explicit, user-controlled step rather than a
    silent guess, per the project's preference for transparent workflows.
    """

    def __init__(self, labels, guess, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Gait Markers"))

        form = QFormLayout(self)
        options = [_NO_MARKER] + list(labels)

        self._combos = {}
        for role, prompt in (
            ("RightHeel", self.tr("Right heel:")),
            ("LeftHeel", self.tr("Left heel:")),
            ("RightToe", self.tr("Right toe:")),
            ("LeftToe", self.tr("Left toe:")),
        ):
            combo = QComboBox()
            combo.addItems(options)
            idx = combo.findText(guess.get(role, _NO_MARKER))
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            form.addRow(prompt, combo)
            self._combos[role] = combo

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def selection(self):
        """Returns {role: label} for the four roles, omitting any role left
        at "(none)"."""
        result = {}
        for role, combo in self._combos.items():
            text = combo.currentText()
            if text != _NO_MARKER:
                result[role] = text
        return result


class _FilterSettingsDialog(QDialog):
    """Lets the user turn filtering on/off (replacing the standalone "Filter"
    toggle for this module) and override the low-pass cutoff/order used for
    Markers and Force Plates, and the EMG conditioning chain's parameters,
    when filtering is on. Defaults shown here match Kinematics Inspection's
    fixed defaults (6 Hz/2nd-order markers, 10 Hz/4th-order force plates per
    the project's filtering rules; 50-450 Hz/2nd-order band-pass + 6 Hz/
    2nd-order envelope for EMG, matching the app's existing EMG conditioning
    default), so opening and clicking OK without changes is a no-op.
    """

    def __init__(self, params, enabled, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Filter Settings"))
        layout = QVBoxLayout(self)

        self.enable_check = QCheckBox(self.tr("Apply filtering"))
        self.enable_check.setChecked(enabled)
        layout.addWidget(self.enable_check)

        marker_box = QGroupBox(self.tr("Markers (low-pass)"))
        marker_form = QFormLayout(marker_box)
        self.marker_cutoff = QDoubleSpinBox()
        self.marker_cutoff.setRange(0.1, 500.0)
        self.marker_cutoff.setSuffix(" Hz")
        self.marker_cutoff.setValue(params["marker_cutoff"])
        self.marker_order = QSpinBox()
        self.marker_order.setRange(1, 8)
        self.marker_order.setValue(params["marker_order"])
        marker_form.addRow(self.tr("Cutoff:"), self.marker_cutoff)
        marker_form.addRow(self.tr("Order:"), self.marker_order)
        layout.addWidget(marker_box)

        fp_box = QGroupBox(self.tr("Force Plates (low-pass)"))
        fp_form = QFormLayout(fp_box)
        self.fp_cutoff = QDoubleSpinBox()
        self.fp_cutoff.setRange(0.1, 500.0)
        self.fp_cutoff.setSuffix(" Hz")
        self.fp_cutoff.setValue(params["fp_cutoff"])
        self.fp_order = QSpinBox()
        self.fp_order.setRange(1, 8)
        self.fp_order.setValue(params["fp_order"])
        fp_form.addRow(self.tr("Cutoff:"), self.fp_cutoff)
        fp_form.addRow(self.tr("Order:"), self.fp_order)
        layout.addWidget(fp_box)

        # DC removal and full-wave rectification have no tunable parameters --
        # they're always applied as part of this chain when filtering is on.
        emg_box = QGroupBox(self.tr("EMG (DC removal + band-pass + rectify + envelope)"))
        emg_form = QFormLayout(emg_box)
        self.emg_cutoff_l = QDoubleSpinBox()
        self.emg_cutoff_l.setRange(0.1, 2000.0)
        self.emg_cutoff_l.setSuffix(" Hz")
        self.emg_cutoff_l.setValue(params["emg_cutoff_l"])
        self.emg_cutoff_h = QDoubleSpinBox()
        self.emg_cutoff_h.setRange(0.1, 2000.0)
        self.emg_cutoff_h.setSuffix(" Hz")
        self.emg_cutoff_h.setValue(params["emg_cutoff_h"])
        self.emg_order = QSpinBox()
        self.emg_order.setRange(1, 8)
        self.emg_order.setValue(params["emg_order"])
        self.emg_envelope_cutoff = QDoubleSpinBox()
        self.emg_envelope_cutoff.setRange(0.1, 100.0)
        self.emg_envelope_cutoff.setSuffix(" Hz")
        self.emg_envelope_cutoff.setValue(params["emg_envelope_cutoff"])
        self.emg_envelope_order = QSpinBox()
        self.emg_envelope_order.setRange(1, 8)
        self.emg_envelope_order.setValue(params["emg_envelope_order"])
        emg_form.addRow(self.tr("Band-pass low cutoff:"), self.emg_cutoff_l)
        emg_form.addRow(self.tr("Band-pass high cutoff:"), self.emg_cutoff_h)
        emg_form.addRow(self.tr("Band-pass order:"), self.emg_order)
        emg_form.addRow(self.tr("Envelope cutoff:"), self.emg_envelope_cutoff)
        emg_form.addRow(self.tr("Envelope order:"), self.emg_envelope_order)
        layout.addWidget(emg_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def enabled(self):
        return self.enable_check.isChecked()

    def values(self):
        return {
            "marker_cutoff": self.marker_cutoff.value(),
            "marker_order": self.marker_order.value(),
            "fp_cutoff": self.fp_cutoff.value(),
            "fp_order": self.fp_order.value(),
            "emg_cutoff_l": self.emg_cutoff_l.value(),
            "emg_cutoff_h": self.emg_cutoff_h.value(),
            "emg_order": self.emg_order.value(),
            "emg_envelope_cutoff": self.emg_envelope_cutoff.value(),
            "emg_envelope_order": self.emg_envelope_order.value(),
        }


# Canonical gait event names -- the only choices offered in the manual
# editor's "Event" column, and the prefixes _apply_gait_events()/
# playplotview.py's per-side coloring match against.
_GAIT_EVENT_TYPES = ["IC_L", "TO_L", "IC_R", "TO_R"]


class _ManualGaitEventsDialog(QDialog):
    """Manually add/edit/remove gait events (IC_L/TO_L/IC_R/TO_R) -- this
    module's replacement for the generic Manual Cycles dialog (which
    produces a different, task-based CycleStart_/CycleEnd_ event shape that
    doesn't fit per-foot IC/TO events). Pre-filled with whatever "Detect
    Gait Events" already found (the "#n" suffix stripped, since it's
    reassigned on save); if there's nothing to pre-fill, starts with one
    template row so the event type is always picked from the same four
    canonical names instead of typed freehand.
    """

    def __init__(self, existing_rows, total_time, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Manual Gait Events"))
        self._total_time = total_time

        layout = QVBoxLayout(self)
        hint = QLabel(self.tr(
            "Add, edit, or remove initial-contact (IC) / toe-off (TO) events "
            "per side. Trial length: {:.3f} s."
        ).format(total_time))
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(0, 2, self)
        self.table.setHorizontalHeaderLabels([self.tr("Event"), self.tr("Time (s)")])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 90)
        layout.addWidget(self.table)

        row_btns = QHBoxLayout()
        add_btn = QPushButton(self.tr("+ Row"))
        add_btn.clicked.connect(lambda: self._add_row())
        remove_btn = QPushButton(self.tr("− Row"))
        remove_btn.clicked.connect(self._remove_selected_rows)
        row_btns.addWidget(add_btn)
        row_btns.addWidget(remove_btn)
        row_btns.addStretch()
        layout.addLayout(row_btns)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        for event_type, t in existing_rows:
            self._add_row(event_type, t)
        if not existing_rows:
            self._add_row(_GAIT_EVENT_TYPES[0], None)

        self.resize(380, 320)

    def _add_row(self, event_type=None, time_s=None):
        row = self.table.rowCount()
        self.table.insertRow(row)
        combo = QComboBox()
        combo.addItems(_GAIT_EVENT_TYPES)
        idx = combo.findText(event_type or _GAIT_EVENT_TYPES[0])
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.table.setCellWidget(row, 0, combo)
        time_txt = "{:.3f}".format(time_s) if time_s is not None else ""
        self.table.setItem(row, 1, QTableWidgetItem(time_txt))

    def _remove_selected_rows(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def rows(self):
        """Parse and validate table rows.

        Returns (rows, errors): rows is list[(event_type, time_s)]; errors
        is list[str] describing skipped rows. A blank time is silently
        skipped (lets the user leave a trailing empty row); a non-numeric or
        out-of-trial-range time is reported and dropped.
        """
        result = []
        errors = []
        for row in range(self.table.rowCount()):
            combo = self.table.cellWidget(row, 0)
            time_item = self.table.item(row, 1)
            time_txt = time_item.text().strip() if time_item else ""
            if not time_txt:
                continue
            try:
                t = float(time_txt)
            except ValueError:
                errors.append(self.tr("Row {}: not a number").format(row + 1))
                continue
            if t < 0 or t > self._total_time:
                errors.append(
                    self.tr("Row {}: out of trial range (0 - {:.3f}s)").format(row + 1, self._total_time)
                )
                continue
            result.append((combo.currentText(), t))
        return result, errors


class GaitAnalysisDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Gait Analysis"))

        self._profile = None
        self._model = None
        self._controller = None

        layout = QVBoxLayout(self)

        # Built before top_row so its toolbar_layout is available to insert
        # this module's own buttons into (see below).
        self._playbar = PlayBarWidget()
        # This module's own "Detect Gait Events" replaces Kinematics
        # Inspection's generic Cycle Detection entirely -- and Mark Event /
        # Export Events aren't needed here either. Manual Cycles' only home
        # was inside the now-hidden Cycle Detection popup; it's rewired
        # below (see _load) to open this module's own manual gait-event
        # editor instead of the generic one. None of this touches
        # PlayBarWidget/Controller themselves, which Kinematics Inspection
        # still uses unmodified.
        self._playbar.cycleDetectionButton.hide()
        self._playbar.markEventButton.hide()
        self._playbar.exportEventsButton.hide()
        self._playbar.onsetDetectionButton.hide()
        # The on/off toggle moves inside Filter Settings (see below) instead
        # of sitting on the toolbar as its own button -- the widget itself
        # stays alive and is still what Controller.tree_item_select reads
        # (self.playbar.filterCheck.isChecked()); this dialog just drives its
        # checked state instead of the user clicking it directly.
        self._playbar.filterCheck.hide()

        self._detect_btn = QPushButton(self.tr("Detect Gait Events"))
        self._detect_btn.setStyleSheet(buttonStyle)
        self._detect_btn.setEnabled(False)
        self._detect_btn.clicked.connect(self._on_detect_gait_events)
        # Left of where "Filter" used to be, on the playbar's own toolbar row
        # rather than a separate row up top.
        _filter_idx = self._playbar.toolbar_layout.indexOf(self._playbar.filterCheck)
        self._playbar.toolbar_layout.insertWidget(_filter_idx, self._detect_btn)
        self._playbar.toolbar_layout.insertWidget(_filter_idx + 1, self._playbar.manualCyclesButton)
        # Connected once, directly to the button, rather than via the shared
        # manualCyclesRequested signal Controller listens on -- _load()
        # disconnects each fresh Controller's own listener so only this
        # module's manual gait-event editor opens, not the generic one.
        self._playbar.manualCyclesButton.clicked.connect(self._on_manual_gait_events)

        # Filter Settings replaces "Filter" on the toolbar -- opens a popup
        # with the on/off toggle plus the cutoff/order used for Markers/Force
        # Plates/EMG (see _FilterSettingsDialog). Defaults match the
        # project's standard filtering rules; pushed into each fresh
        # Controller in _load() since a new Controller resets to its own
        # defaults.
        self._filter_params = dict(
            marker_cutoff=6.0, marker_order=2,
            fp_cutoff=10.0, fp_order=4,
            emg_cutoff_l=50.0, emg_cutoff_h=450.0, emg_order=2,
            emg_envelope_cutoff=6.0, emg_envelope_order=2,
        )
        self._filter_settings_btn = QPushButton(self.tr("Filter Settings..."))
        self._filter_settings_btn.setStyleSheet(buttonStyle)
        self._filter_settings_btn.clicked.connect(self._on_filter_settings)
        _filter_check_idx = self._playbar.toolbar_layout.indexOf(self._playbar.filterCheck)
        self._playbar.toolbar_layout.insertWidget(_filter_check_idx + 1, self._filter_settings_btn)


        top_row = QHBoxLayout()
        self._open_btn = QPushButton(self.tr("Open C3D..."))
        self._open_btn.clicked.connect(self._on_open)
        top_row.addWidget(self._open_btn)
        self._file_label = QLabel(self.tr("No trial loaded"))
        # Stretch=1 on the file label pushes everything after it (Save /
        # Create Report) to the far right of the row.
        top_row.addWidget(self._file_label, 1)
        self._save_btn = QPushButton(self.tr("Save..."))
        self._save_btn.setStyleSheet(buttonStyle)
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._on_save_csv)
        top_row.addWidget(self._save_btn)
        self._report_btn = QPushButton(self.tr("Create Report..."))
        self._report_btn.setStyleSheet(buttonStyle)
        self._report_btn.setEnabled(False)
        self._report_btn.clicked.connect(self._on_create_report)
        top_row.addWidget(self._report_btn)
        layout.addLayout(top_row)

        # Cached from the last "Detect Gait Events" run -- reused by manual
        # edits (to keep step/stride length recomputed after a manual edit,
        # instead of only cadence/timing) and by Save/Create Report (spatio-
        # temporal + per-cycle EMG/joint-angle calculations need the same
        # marker data and heel-marker choice used for detection).
        self._last_marker_xyz_by_label = {}
        self._last_heel_toe_labels = {}

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter, 1)

        left_splitter = QSplitter(Qt.Orientation.Vertical)
        self._render = RenderWidget()
        self._plot = PlayPlotWidget()
        left_splitter.addWidget(self._render)
        left_splitter.addWidget(self._plot)
        left_splitter.setStretchFactor(0, 3)
        left_splitter.setStretchFactor(1, 2)
        splitter.addWidget(left_splitter)

        right_splitter = QSplitter(Qt.Orientation.Vertical)
        self._label_tree = QTreeWidget()
        self._label_tree.setHeaderItem(QTreeWidgetItem(["Trial"]))
        self._label_tree.setDragEnabled(True)
        self._label_tree.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        # Right-click an EMG channel to rename it -- lets the user assign a
        # clearer name (e.g. a muscle) for later reference. Controller wires
        # its own context menu on the same tree (crop/event actions only, see
        # Controller._on_tree_context_menu); that handler no-ops for EMG leaf
        # items, so this coexists without either overriding the other.
        self._label_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._label_tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        right_splitter.addWidget(self._label_tree)

        self._results = QPlainTextEdit()
        self._results.setReadOnly(True)
        self._results.setPlaceholderText(
            self.tr("Gait event / spatiotemporal results appear here after detection.")
        )
        right_splitter.addWidget(self._results)
        right_splitter.setStretchFactor(0, 2)
        right_splitter.setStretchFactor(1, 1)
        splitter.addWidget(right_splitter)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)

        layout.addWidget(self._playbar)

        self.showMaximized()

    def closeEvent(self, event):
        reply = QMessageBox.question(
            self, self.tr("Gait Analysis"), self.tr("Ready to step out?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            event.ignore()
            return
        if self._controller is not None:
            self._controller.stop()
        super().closeEvent(event)

    # ── Loading ──────────────────────────────────────────────────────────────

    def _on_open(self):
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Select a kinematics / force-plate C3D file"),
            "", "C3D Files (*.c3d)",
        )
        if not path:
            return
        self._load(path)

    def _load(self, path):
        try:
            has_points, analog_labels = c3d_probe(path)
        except ValueError as e:
            QMessageBox.critical(self, self.tr("error"), str(e), QMessageBox.Ok)
            return

        if not has_points:
            QMessageBox.critical(
                self, self.tr("No kinematics data"),
                self.tr(
                    "'{}' has no marker/force-plate data.\n\n"
                    "Gait Analysis needs a kinematics or force-plate C3D file."
                ).format(os.path.basename(path)),
                QMessageBox.Ok,
            )
            return

        has_emg = any(not _is_non_emg_channel(l) for l in analog_labels)
        if not has_emg:
            choice = self._prompt_no_emg()
            if choice == "cancel":
                return
            if choice == "stitch":
                stitched = self._stitch_emg_into(path)
                if stitched is None:
                    return
                path = stitched
                try:
                    _, analog_labels = c3d_probe(path)
                except ValueError as e:
                    QMessageBox.critical(self, self.tr("error"), str(e), QMessageBox.Ok)
                    return
                has_emg = any(not _is_non_emg_channel(l) for l in analog_labels)
            # "kinematics_only" falls through with has_emg == False

        try:
            preparsed = c3dFile(path)
            kin_obj = kinematic(path, preparsed_c3d=preparsed)
            emg_obj = emg(path, preparsed_c3d=preparsed) if has_emg else emg()
        except Exception as e:
            QMessageBox.critical(
                self, self.tr("error"),
                self.tr("Could not load '{}':\n\n{}").format(os.path.basename(path), str(e)),
                QMessageBox.Ok,
            )
            return

        if not kin_obj.isValid():
            QMessageBox.critical(
                self, self.tr("error"),
                self.tr("'{}' could not be read as kinematics data.").format(os.path.basename(path)),
                QMessageBox.Ok,
            )
            return

        if self._controller is not None:
            self._controller.stop()

        self._profile = workspace.profile(emg_obj, kin_obj)
        self._model = Model(self._profile)
        _populate_label_tree(self._label_tree, self._profile)
        self._controller = Controller(
            self._model, self._render, self._playbar, self._plot, None, self._label_tree,
        )
        # Controller.__init__ unconditionally wires manualCyclesRequested to
        # its own generic Manual Cycles popup -- disconnect that so only
        # this module's manual gait-event editor (wired directly to the
        # button in __init__) responds to a click.
        try:
            self._playbar.manualCyclesRequested.disconnect(self._controller._onManualCyclesRequested)
        except (TypeError, RuntimeError):
            pass
        self._apply_filter_settings_to_controller()
        self._file_label.setText(path)
        self._detect_btn.setEnabled(True)
        self._save_btn.setEnabled(True)
        self._report_btn.setEnabled(True)
        self._last_marker_xyz_by_label = {}
        self._last_heel_toe_labels = {}
        self._results.clear()

    # ── Gait event detection ─────────────────────────────────────────────────

    def _on_detect_gait_events(self):
        kin = self._profile.kinematic
        guess = _gait.guess_gait_markers(kin.reallabels)

        mapping_dlg = _GaitMarkerMappingDialog(kin.reallabels, guess, self)
        if mapping_dlg.exec() != QDialog.DialogCode.Accepted:
            return
        selection = mapping_dlg.selection()
        right_heel = selection.get("RightHeel")
        left_heel = selection.get("LeftHeel")
        right_toe = selection.get("RightToe")
        left_toe = selection.get("LeftToe")

        if not kin.force_plates and not right_toe and not left_toe:
            QMessageBox.warning(
                self, self.tr("Detect Gait Events"),
                self.tr(
                    "No force plates and no right/left toe marker selected -- "
                    "can't auto-detect gait events for this trial."
                ),
                QMessageBox.Ok,
            )
            return

        marker_xyz_by_label = {}
        for label in {right_heel, left_heel, right_toe, left_toe} - {None}:
            arr = _gait.marker_xyz_array(kin, label)
            if arr is not None:
                marker_xyz_by_label[label] = arr

        result = _gait.detect_gait_events(
            kin.force_plates, marker_xyz_by_label, kin.point_fs,
            right_heel_label=right_heel or "RHEE", left_heel_label=left_heel or "LHEE",
            right_toe_label=right_toe or "RTOE", left_toe_label=left_toe or "LTOE",
        )

        # Cached for Save/Create Report, and reused if the user manually
        # edits events afterward so step/stride length can still be
        # recomputed (see _current_hs_to_by_side / _on_manual_gait_events).
        self._last_marker_xyz_by_label = marker_xyz_by_label
        self._last_heel_toe_labels = dict(
            right_heel=right_heel or "RHEE", left_heel=left_heel or "LHEE",
            right_toe=right_toe or "RTOE", left_toe=left_toe or "LTOE",
        )

        self._apply_gait_events(result["HS"], result["TO"])

        self._results.setPlainText(
            self._format_results(
                result,
                right_heel or _NO_MARKER, left_heel or _NO_MARKER,
                right_toe or _NO_MARKER, left_toe or _NO_MARKER,
            )
        )

    def _apply_gait_events(self, hs_by_side, to_by_side):
        """Replace this trial's Gait-context events with a newly detected or
        manually-edited set, and refresh the timeline/tree to match. Shared
        by _on_detect_gait_events and _on_manual_gait_events so both go
        through one code path.

        hs_by_side / to_by_side: {"Right": [t, ...], "Left": [t, ...]}.
        """
        for ev in [e for e in self._model.extra_events if e.context == "Gait"]:
            self._model.extra_events.remove(ev)
            if ev in self._model.events:
                self._model.events.remove(ev)
            self._controller.top.remove_event(ev)

        side_abbr = {"Right": "R", "Left": "L"}
        for side in ("Right", "Left"):
            for n, t in enumerate(sorted(hs_by_side.get(side, [])), start=1):
                ev = TrialEvent(t, "IC_{} #{}".format(side_abbr[side], n), "Gait")
                self._model.extra_events.append(ev)
                self._model.events.append(ev)
                self._controller.top.add_event(ev)
            for n, t in enumerate(sorted(to_by_side.get(side, [])), start=1):
                ev = TrialEvent(t, "TO_{} #{}".format(side_abbr[side], n), "Gait")
                self._model.extra_events.append(ev)
                self._model.events.append(ev)
                self._controller.top.add_event(ev)
        self._model.events.sort(key=lambda e: e.time_s)
        self._controller._refresh_event_tree()

    def _current_hs_to_by_side(self):
        """Read the trial's current Gait-context events (whichever mix of
        auto-detected and manually-edited is live right now) back into
        {"Right": [t, ...], "Left": [t, ...]} shape for HS and TO -- the
        source of truth Save/Create Report build their per-cycle
        calculations from, instead of a possibly-stale cached detection
        result."""
        hs_by_side = {"Right": [], "Left": []}
        to_by_side = {"Right": [], "Left": []}
        if self._model is None:
            return hs_by_side, to_by_side
        for ev in self._model.extra_events:
            if ev.context != "Gait":
                continue
            base = ev.label.split(" #")[0]
            if base not in _GAIT_EVENT_TYPES:
                continue
            kind, abbr = base.split("_")
            side = "Right" if abbr == "R" else "Left"
            (hs_by_side if kind == "IC" else to_by_side)[side].append(ev.time_s)
        for d in (hs_by_side, to_by_side):
            for side in d:
                d[side].sort()
        return hs_by_side, to_by_side

    # ── Manual gait-event correction ─────────────────────────────────────────

    def _on_manual_gait_events(self):
        if self._model is None:
            return

        existing = []
        for ev in sorted(self._model.extra_events, key=lambda e: e.time_s):
            if ev.context != "Gait":
                continue
            event_type = ev.label.split(" #")[0]
            if event_type in _GAIT_EVENT_TYPES:
                existing.append((event_type, ev.time_s))

        dlg = _ManualGaitEventsDialog(existing, self._model.total_time(), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        rows, errors = dlg.rows()
        if errors:
            QMessageBox.warning(
                self, self.tr("Manual Gait Events"),
                self.tr("Some rows were invalid and skipped:\n{}").format("\n".join(errors)),
                QMessageBox.Ok,
            )

        hs_by_side = {"Right": [], "Left": []}
        to_by_side = {"Right": [], "Left": []}
        for event_type, t in rows:
            kind, abbr = event_type.split("_")
            side = "Right" if abbr == "R" else "Left"
            (hs_by_side if kind == "IC" else to_by_side)[side].append(t)

        self._apply_gait_events(hs_by_side, to_by_side)
        self._results.setPlainText(self._format_manual_results(hs_by_side, to_by_side))

    @staticmethod
    def _format_manual_results(hs_by_side, to_by_side):
        lines = ["Source: manual", ""]
        for side in ("Right", "Left"):
            lines.append("{} IC ({}): {}".format(
                side, len(hs_by_side[side]),
                ", ".join("{:.3f}s".format(t) for t in sorted(hs_by_side[side])),
            ))
            lines.append("{} TO ({}): {}".format(
                side, len(to_by_side[side]),
                ", ".join("{:.3f}s".format(t) for t in sorted(to_by_side[side])),
            ))
        lines.append("")
        lines.append(
            "Step/stride metrics not recomputed after a manual edit -- "
            "click \"Detect Gait Events\" to refresh them."
        )
        return "\n".join(lines)

    # ── Filter settings ──────────────────────────────────────────────────────

    def _on_filter_settings(self):
        dlg = _FilterSettingsDialog(self._filter_params, self._playbar.filterCheck.isChecked(), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._filter_params = dlg.values()
        self._playbar.filterCheck.setChecked(dlg.enabled())
        self._apply_filter_settings_to_controller()
        # Re-plot whatever's currently shown so the new parameters take
        # effect immediately instead of only on the next double-click.
        if self._controller is not None and self._controller._last_tree_item is not None:
            self._controller.tree_item_select(self._controller._last_tree_item)

    def _apply_filter_settings_to_controller(self):
        if self._controller is None:
            return
        p = self._filter_params
        self._controller.marker_filter_cutoff = p["marker_cutoff"]
        self._controller.marker_filter_order = p["marker_order"]
        self._controller.fp_filter_cutoff = p["fp_cutoff"]
        self._controller.fp_filter_order = p["fp_order"]
        self._controller.emg_filter_cutoff_l = p["emg_cutoff_l"]
        self._controller.emg_filter_cutoff_h = p["emg_cutoff_h"]
        self._controller.emg_filter_order = p["emg_order"]
        self._controller.emg_envelope_cutoff = p["emg_envelope_cutoff"]
        self._controller.emg_envelope_order = p["emg_envelope_order"]
        self._controller.emg_filter_enabled = True

    # ── EMG channel rename ───────────────────────────────────────────────────

    def _on_tree_context_menu(self, pos):
        """Right-click an EMG channel -> rename it. No-ops for any other item
        (Markers/Force Plates/Events), including the crop/event nodes
        Controller's own context menu on this same tree already handles."""
        item = self._label_tree.itemAt(pos)
        if item is None or item.parent() is None:
            return
        if not item.parent().text(0).startswith("EMG"):
            return

        menu = QMenu(self)
        rename_action = menu.addAction(self.tr("Rename Channel..."))
        action = menu.exec(self._label_tree.viewport().mapToGlobal(pos))
        if action != rename_action:
            return

        old_name = item.text(0)
        new_name, ok = QInputDialog.getText(
            self, self.tr("Rename EMG Channel"),
            self.tr("New name for '{}':").format(old_name),
            text=old_name,
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == old_name:
            return
        if new_name in self._profile.emg.Channels:
            QMessageBox.warning(
                self, self.tr("Rename Channel"),
                self.tr("'{}' is already in use by another channel.").format(new_name),
                QMessageBox.Ok,
            )
            return

        self._profile.emg.renameChannel(old_name, new_name)
        _populate_label_tree(self._label_tree, self._profile)

    # ── Save (CSV export) ────────────────────────────────────────────────────

    def _emg_envelope(self, chan):
        """This module's default-on EMG conditioning chain (DC removal ->
        band-pass -> rectify -> envelope, see Controller._apply_emg_pipeline)
        applied to *chan*'s full-trial display signal, using whatever cutoff/
        order Filter Settings currently holds. Returns (array, fs_emg)."""
        y = self._profile.emg.get_kinematics_display(chan)
        fs = self._profile.emg.getfs()
        p = self._filter_params
        y = Controller._apply_emg_pipeline(
            y, fs, p["emg_cutoff_l"], p["emg_cutoff_h"], p["emg_order"],
            p["emg_envelope_cutoff"], p["emg_envelope_order"],
        )
        return y, fs

    @staticmethod
    def _fmt(value):
        return "" if value is None or (isinstance(value, float) and np.isnan(value)) else "{:.4f}".format(value)

    def _on_save_csv(self):
        if self._model is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, self.tr("Save Gait Analysis CSV"), "", "CSV Files (*.csv)",
        )
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"

        hs_by_side, _to_by_side = self._current_hs_to_by_side()
        kin = self._profile.kinematic
        fs_k = kin.point_fs
        heel = self._last_heel_toe_labels
        # "to" isn't read by compute_spatiotemporals -- see its docstring --
        # so pairing each HS with itself is a safe stand-in when the current
        # event set came from a manual edit with no real TO pairing.
        events_by_side = {side: [(t, t) for t in hs_by_side[side]] for side in ("Right", "Left")}
        spatio = _gait.compute_spatiotemporals(
            events_by_side, self._last_marker_xyz_by_label, fs_k,
            right_heel_label=heel.get("right_heel", "RHEE"),
            left_heel_label=heel.get("left_heel", "LHEE"),
        )

        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                self._write_gait_parameters_csv(writer, spatio)
                self._write_emg_cycle_metrics_csv(writer, hs_by_side)
                self._write_joint_angle_csv(writer, hs_by_side, kin, fs_k)
        except OSError as e:
            QMessageBox.critical(self, self.tr("error"), str(e), QMessageBox.Ok)
            return

        QMessageBox.information(
            self, self.tr("Save Gait Analysis CSV"),
            self.tr("Saved to '{}'.").format(path), QMessageBox.Ok,
        )

    def _write_gait_parameters_csv(self, writer, spatio):
        writer.writerow(["Gait Parameters -- Steps"])
        writer.writerow(["Side", "HS Time (s)", "Step Length (m)", "Step Time (s)", "Cadence (steps/min)"])
        for step in spatio["steps"]:
            writer.writerow([
                step["side"], "{:.3f}".format(step["hs_t"]),
                self._fmt(step["step_length_m"]), "{:.3f}".format(step["step_time_s"]),
                self._fmt(step["cadence_spm"]),
            ])
        writer.writerow([])

        writer.writerow(["Gait Parameters -- Stride"])
        writer.writerow(["Side", "Stride Length (m)", "Stride Time (s)", "Cadence (strides/min)", "Velocity (m/s)"])
        for side in ("Right", "Left"):
            s = spatio["stride"][side]
            writer.writerow([
                side, self._fmt(s["stride_length_m"]), self._fmt(s["stride_time_s"]),
                self._fmt(s["cadence_spm"]), self._fmt(s["velocity_m_s"]),
            ])
        writer.writerow([])

    def _write_emg_cycle_metrics_csv(self, writer, hs_by_side):
        writer.writerow(["EMG Metrics per Gait Cycle"])
        writer.writerow(["Channel", "Side", "Cycle", "Cycle Start (s)", "Cycle End (s)",
                          "Mean Envelope", "Peak Envelope"])
        channels = self._profile.emg.Channels
        if not channels:
            writer.writerow(["(no EMG channels loaded for this trial)"])
            writer.writerow([])
            return
        for chan in channels:
            envelope, fs_emg = self._emg_envelope(chan)
            if len(envelope) == 0:
                continue
            t = np.arange(len(envelope)) / fs_emg
            for side in ("Right", "Left"):
                for i, (t0, t1) in enumerate(_gait.cycles_from_hs(hs_by_side[side]), start=1):
                    seg = envelope[(t >= t0) & (t < t1)]
                    if len(seg) == 0:
                        continue
                    writer.writerow([
                        chan, side, i, "{:.3f}".format(t0), "{:.3f}".format(t1),
                        "{:.5f}".format(seg.mean()), "{:.5f}".format(seg.max()),
                    ])
        writer.writerow([])

    def _write_joint_angle_csv(self, writer, hs_by_side, kin, fs_k):
        writer.writerow(["Joint Angles per Gait Cycle (0-100%, real Model Output data only)"])
        if not kin.anglelabels:
            writer.writerow(["(no Model Output angles loaded for this trial)"])
            writer.writerow([])
            return
        writer.writerow(["Angle", "Axis", "Side", "Cycle"] + ["{}%".format(p) for p in range(0, 101)])
        for label in kin.anglelabels:
            arr = _gait.angle_xyz_array(kin, label)
            if arr is None:
                continue
            t = np.arange(len(arr)) / fs_k
            for axis_i, axis_name in enumerate(("X", "Y", "Z")):
                y = arr[:, axis_i]
                for side in ("Right", "Left"):
                    for i, (t0, t1) in enumerate(_gait.cycles_from_hs(hs_by_side[side]), start=1):
                        curve = _gait.resample_cycle(t, y, t0, t1, n_points=101)
                        writer.writerow(
                            [label, axis_name, side, i]
                            + ["" if np.isnan(v) else "{:.2f}".format(v) for v in curve]
                        )
        writer.writerow([])

    # ── Create Report ────────────────────────────────────────────────────────

    def _on_create_report(self):
        if self._model is None:
            return
        hs_by_side, _to_by_side = self._current_hs_to_by_side()
        kin = self._profile.kinematic
        fs_k = kin.point_fs
        heel = self._last_heel_toe_labels
        events_by_side = {side: [(t, t) for t in hs_by_side[side]] for side in ("Right", "Left")}
        spatio = _gait.compute_spatiotemporals(
            events_by_side, self._last_marker_xyz_by_label, fs_k,
            right_heel_label=heel.get("right_heel", "RHEE"),
            left_heel_label=heel.get("left_heel", "LHEE"),
        )

        emg_means = {}
        for chan in self._profile.emg.Channels:
            envelope, fs_emg = self._emg_envelope(chan)
            if len(envelope) == 0:
                continue
            t = np.arange(len(envelope)) / fs_emg
            all_cycles = _gait.cycles_from_hs(hs_by_side["Right"]) + _gait.cycles_from_hs(hs_by_side["Left"])
            segs = [envelope[(t >= t0) & (t < t1)] for t0, t1 in all_cycles]
            segs = [s for s in segs if len(s) > 0]
            if segs:
                emg_means[chan] = float(np.mean([s.mean() for s in segs]))

        dlg = _GaitReportDialog(spatio, hs_by_side, kin, fs_k, emg_means, self)
        dlg.exec()

    @staticmethod
    def _format_results(result, right_heel, left_heel, right_toe, left_toe):
        lines = [
            "Source: {}".format(result["source"]),
            "Forward axis (inferred): {}".format(result["forward_axis"]),
            "Markers used: RHeel={}, LHeel={}, RToe={}, LToe={}".format(
                right_heel, left_heel, right_toe, left_toe
            ),
            "",
        ]
        for side in ("Right", "Left"):
            lines.append("{} IC ({}): {}".format(
                side, len(result["HS"][side]),
                ", ".join("{:.3f}s".format(t) for t in result["HS"][side]),
            ))
            lines.append("{} TO ({}): {}".format(
                side, len(result["TO"][side]),
                ", ".join("{:.3f}s".format(t) for t in result["TO"][side]),
            ))

        lines.append("")
        lines.append("Steps:")
        for step in result["steps"]:
            lines.append(
                "  {side} @ {hs_t:.3f}s -- length {step_length_m:.3f} m, "
                "time {step_time_s:.3f} s, cadence {cadence_spm:.1f} steps/min".format(**step)
            )

        lines.append("")
        lines.append("Stride:")
        for side in ("Right", "Left"):
            s = result["stride"][side]
            lines.append(
                "  {side}: length {stride_length_m:.3f} m, time {stride_time_s:.3f} s, "
                "cadence {cadence_spm:.1f} strides/min, velocity {velocity_m_s:.3f} m/s".format(
                    side=side, **s
                )
            )

        if result["warnings"]:
            lines.append("")
            lines.append("Warnings:")
            for w in result["warnings"]:
                lines.append("  - " + w)

        return "\n".join(lines)

    def _prompt_no_emg(self):
        """Returns 'stitch', 'kinematics_only', or 'cancel'."""
        box = QMessageBox(self)
        box.setWindowTitle(self.tr("No EMG data"))
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            self.tr(
                "This C3D has no EMG channels.\n\n"
                "Stitch in a separately-recorded EMG file, or continue with "
                "kinematics-only gait analysis?"
            )
        )
        stitch_btn = box.addButton(self.tr("Stitch EMG File..."), QMessageBox.ButtonRole.ActionRole)
        kin_only_btn = box.addButton(self.tr("Kinematics Only"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is stitch_btn:
            return "stitch"
        if clicked is kin_only_btn:
            return "kinematics_only"
        return "cancel"

    def _stitch_emg_into(self, kin_file):
        """Merge a separately-recorded EMG file into *kin_file*, mirroring
        main.py's stitchDataButtonClicked. Returns the stitched file's path,
        or None if the user cancelled or stitching failed."""
        emg_file, _ = QFileDialog.getOpenFileName(
            self, self.tr("Select EMG file to stitch in"),
            os.path.dirname(kin_file), "EMG Files (*.c3d *.mat)",
        )
        if not emg_file:
            return None

        try:
            offset_s, trusted, msg = check_alignment(kin_file, emg_file)
        except StitchError as e:
            QMessageBox.critical(self, self.tr("error"), str(e), QMessageBox.Ok)
            return None

        if not trusted:
            QMessageBox.warning(
                self, self.tr("Cannot auto-align"),
                self.tr(
                    "These files don't look like a hardware-synced pair:\n\n{}"
                ).format(msg),
                QMessageBox.Ok,
            )
            return None

        try:
            return stitch_c3d(kin_file, emg_file, offset_s=offset_s)
        except StitchError as e:
            QMessageBox.critical(
                self, self.tr("error"),
                self.tr("Could not stitch these files:\n\n{}").format(str(e)),
                QMessageBox.Ok,
            )
            return None
