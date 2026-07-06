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

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFileDialog,
    QMessageBox, QSplitter, QTreeWidget, QTreeWidgetItem,
    QAbstractItemView, QPlainTextEdit, QComboBox, QFormLayout, QDialogButtonBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QMenu, QInputDialog,
    QGroupBox, QDoubleSpinBox, QSpinBox, QCheckBox, QLineEdit,
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
from widgets.gait_report_dialog import (
    _GaitReportDialog, _CCI_METHODS, _CCI_METHOD_LABELS,
    _CCI_NORMALIZE_OPTIONS, _CCI_NORMALIZE_LABELS,
)


def _populate_label_tree(tree: QTreeWidget, profile) -> None:
    """Fill *tree* with Markers / Model Outputs / EMG / Force Plates / Events
    groups for *profile* (a workspace.profile). Mirrors main.py's
    populateKinematicTree() but for a single standalone profile with no
    Workspace/participant wrapper node.

    Preserves each top-level group's expand/collapse state across the
    rebuild (matched by its label, e.g. "EMG (19)", which a rename doesn't
    change) -- without this, every call (e.g. after renaming an EMG
    channel, see _on_tree_context_menu) collapsed everything back, forcing
    the user to re-expand "EMG" before renaming the next channel.
    """
    expanded_labels = {
        tree.topLevelItem(i).text(0)
        for i in range(tree.topLevelItemCount())
        if tree.topLevelItem(i).isExpanded()
    }
    tree.clear()
    tree.setColumnCount(1)
    k = profile.kinematic
    e = profile.emg

    if k.isValid():
        marker_group = QTreeWidgetItem(tree)
        marker_group.setText(0, "Markers ({})".format(len(k.reallabels)))
        for point in k.reallabels:
            QTreeWidgetItem(marker_group, [point])
        marker_group.setExpanded(marker_group.text(0) in expanded_labels)

        if k.anglelabels:
            angle_group = QTreeWidgetItem(tree)
            angle_group.setText(0, "Model Outputs")
            angles_node = QTreeWidgetItem(angle_group)
            angles_node.setText(0, "Angles ({})".format(len(k.anglelabels)))
            for label in k.anglelabels:
                QTreeWidgetItem(angles_node, [label])
            angle_group.setExpanded(angle_group.text(0) in expanded_labels)
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
        emg_group.setExpanded(emg_group.text(0) in expanded_labels)

    if k.force_plates:
        fp_group = QTreeWidgetItem(tree)
        fp_group.setText(0, "Force Plates ({})".format(len(k.force_plates)))
        for fp in k.force_plates:
            plate_node = QTreeWidgetItem(fp_group)
            plate_node.setText(0, "Plate {}".format(fp.plate_id))
            for comp in ("Fx", "Fy", "Fz"):
                QTreeWidgetItem(plate_node, ["Plate{} {}".format(fp.plate_id, comp)])
        fp_group.setExpanded(fp_group.text(0) in expanded_labels)

    all_events = sorted(list(k.events) + list(profile.extra_events), key=lambda ev: ev.time_s)
    if all_events:
        ev_group = QTreeWidgetItem(tree)
        ev_group.setText(0, "Events ({})".format(len(all_events)))
        for ev in all_events:
            source = "" if ev in profile.extra_events else " [C3D]"
            item = QTreeWidgetItem(ev_group, [
                "{} | {} | {:.3f}s{}".format(ev.label, ev.context, ev.time_s, source)
            ])
            # Read back by _apply_crop_dim_to_event_tree to grey out rows
            # outside the current crop window without needing to re-parse
            # the display text.
            item.setData(0, Qt.ItemDataRole.UserRole, ev.time_s)
        ev_group.setExpanded(ev_group.text(0) in expanded_labels)

    tree.setHeaderItem(QTreeWidgetItem(["Trial"]))


def _apply_crop_dim_to_event_tree(tree: QTreeWidget, crop_range) -> None:
    """Grey out (never remove) Events-group rows whose time falls outside
    crop_range = (t0, t1), or restore normal color when crop_range is None
    -- keeps the full event list visible (crop stays non-destructive) while
    showing which ones the current crop window actually includes. Cheap
    enough to call on every crop-drag signal (see GaitAnalysisDialog.
    _sync_crop_visuals) since it only restyles existing items."""
    for i in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(i)
        if not top.text(0).startswith("Events"):
            continue
        for j in range(top.childCount()):
            child = top.child(j)
            t = child.data(0, Qt.ItemDataRole.UserRole)
            if t is None:
                continue
            in_range = crop_range is None or (crop_range[0] <= t <= crop_range[1])
            child.setForeground(0, QColor("#000000" if in_range else "#aaaaaa"))
        break


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
            "per side. Trial length: {:.3f} s. Rows shaded green were "
            "detected from a force plate (more reliable than the marker-only "
            "heuristic); saving here still produces plain manual events."
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

        for event_type, t, source in existing_rows:
            self._add_row(event_type, t, source)
        if not existing_rows:
            self._add_row(_GAIT_EVENT_TYPES[0], None)

        self.resize(380, 320)

    def _add_row(self, event_type=None, time_s=None, source=""):
        row = self.table.rowCount()
        self.table.insertRow(row)
        combo = QComboBox()
        combo.addItems(_GAIT_EVENT_TYPES)
        idx = combo.findText(event_type or _GAIT_EVENT_TYPES[0])
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.table.setCellWidget(row, 0, combo)
        time_txt = "{:.3f}".format(time_s) if time_s is not None else ""
        time_item = QTableWidgetItem(time_txt)
        self.table.setItem(row, 1, time_item)
        # Informational only, snapshotted when the dialog opened -- a
        # force-plate-detected contact (see gait_events._merge_side_events)
        # is more trustworthy than a marker-height guess, so it's worth
        # flagging before the user decides what to edit. Saving from this
        # dialog always produces fresh manual events with no source tag
        # (see _apply_gait_events), regardless of what's shown here.
        if source == "plate":
            combo.setStyleSheet("background-color: #dff0d8;")
            time_item.setBackground(QColor("#dff0d8"))

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


_EMG_SIDE_OPTIONS = ["Unspecified", "Right", "Left"]


class _EMGReportChannelsDialog(QDialog):
    """One-off channel picker + display-name + side step shown right before
    building a report's EMG Activity section -- not remembered and not
    shared with the Co-contraction pair picker (see _CCIPairsDialog, which
    has its own Side column). Lets the user drop channels that aren't
    actually muscles (footswitches, sync bits, ...) but still passed the
    C3D EMG-channel filter, label the rest with a clinically readable name
    for this report only (use the tree's "Rename Channel..." for a
    permanent rename instead), and say which leg each sensor was actually
    on -- a channel's envelope only means something for gait cycles on the
    side it was recorded on; a single-leg EMG setup showing a "Left" bar
    computed from a Right-leg sensor would be meaningless."""

    def __init__(self, channels, parent=None, existing_sides=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("EMG Channels for Report"))
        self._channels = list(channels)
        existing_sides = existing_sides or {}

        layout = QVBoxLayout(self)
        hint = QLabel(self.tr(
            "Choose which EMG channels to include in this report's EMG Activity "
            "section, optionally rename them for display (e.g. \"EMG1.v\" -> "
            "\"Tibialis Anterior\"), and say which leg each sensor was on -- "
            "\"Unspecified\" shows both Right and Left bars for that channel."
        ))
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(len(self._channels), 3, self)
        self.table.setHorizontalHeaderLabels(
            [self.tr("Include"), self.tr("Side"), self.tr("Display Name")]
        )
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setToolTip(self.tr("Double-click \"Include\" to select/deselect all"))
        self.table.horizontalHeader().sectionDoubleClicked.connect(self._on_header_double_clicked)
        self._checks = []
        self._side_combos = []
        self._name_edits = []
        for row, chan in enumerate(self._channels):
            check = QCheckBox(chan)
            check.setChecked(True)
            self.table.setCellWidget(row, 0, check)

            side_combo = QComboBox()
            side_combo.addItems(_EMG_SIDE_OPTIONS)
            side_combo.setCurrentText(existing_sides.get(chan, "Unspecified"))
            self.table.setCellWidget(row, 1, side_combo)

            name_edit = QLineEdit(chan)
            self.table.setCellWidget(row, 2, name_edit)

            self._checks.append(check)
            self._side_combos.append(side_combo)
            self._name_edits.append(name_edit)
        layout.addWidget(self.table)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.resize(480, 360)

    def selection(self):
        """Returns [(original_name, display_name, side), ...] for checked
        rows only, in the original channel order. side is "Right"/"Left"/
        "Unspecified"."""
        result = []
        for chan, check, side_combo, name_edit in zip(
            self._channels, self._checks, self._side_combos, self._name_edits
        ):
            if check.isChecked():
                display = name_edit.text().strip() or chan
                result.append((chan, display, side_combo.currentText()))
        return result

    def sides(self):
        """{channel: side} for every row, checked or not -- used to persist
        the assignment across dialog reopens (see GaitAnalysisDialog.
        _emg_channel_side)."""
        return {chan: combo.currentText() for chan, combo in zip(self._channels, self._side_combos)}

    def _on_header_double_clicked(self, section):
        """Double-clicking the "Include" header toggles all rows at once --
        all checked -> uncheck all, otherwise -> check all (mirrors a
        standard header-checkbox pattern without adding a real one)."""
        if section != 0 or not self._checks:
            return
        all_checked = all(c.isChecked() for c in self._checks)
        for c in self._checks:
            c.setChecked(not all_checked)


class _CCIPairsDialog(QDialog):
    """Define one or more muscle pairs (+ side + method + normalization) to
    compute Co-contraction Index for in the report -- a separate, reusable
    step rather than a one-shot picker buried inside the report dialog,
    since a trial can have more than one antagonist pair worth checking
    (e.g. ankle and knee).

    CCI is single-sided: it's only computed for the leg the pair is marked
    as being on, using that leg's own gait cycles -- comparing a Right-leg
    sensor's envelope against a Left-leg sensor's during "Right" cycles (or
    vice versa) isn't a meaningful co-contraction index, so there's no
    Right+Left pair of numbers here like the other report sections."""

    def __init__(self, channels, existing_pairs, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Co-contraction Index Pairs"))
        self._channels = list(channels)

        layout = QVBoxLayout(self)
        hint = QLabel(self.tr(
            "Pick one or more same-side muscle pairs to compute Co-contraction "
            "Index for in the report. Always computed from the enveloped EMG "
            "within that side's own detected gait cycles, never the whole trial. "
            "Defaults to Trial Max normalization -- Rudolph's index scales with "
            "the signals' raw amplitude, so left at \"None\" it reads as ~0 for "
            "typical raw-EMG-scale envelopes."
        ))
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels([
            self.tr("Muscle A"), self.tr("Muscle B"), self.tr("Side"),
            self.tr("Method"), self.tr("Normalize"),
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
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

        for a, b, side, method_key, norm_key in existing_pairs:
            self._add_row(a, b, side, method_key, norm_key)
        if not existing_pairs:
            self._add_row()

        self.resize(640, 320)

    def _add_row(self, a=None, b=None, side=None, method_key=None, norm_key=None):
        row = self.table.rowCount()
        self.table.insertRow(row)
        combo_a = QComboBox()
        combo_a.addItems(self._channels)
        combo_b = QComboBox()
        combo_b.addItems(self._channels)
        if a and a in self._channels:
            combo_a.setCurrentText(a)
        if b and b in self._channels:
            combo_b.setCurrentText(b)
        elif len(self._channels) > 1:
            combo_b.setCurrentIndex(1)
        self.table.setCellWidget(row, 0, combo_a)
        self.table.setCellWidget(row, 1, combo_b)

        combo_side = QComboBox()
        combo_side.addItems(["Right", "Left"])
        if side in ("Right", "Left"):
            combo_side.setCurrentText(side)
        self.table.setCellWidget(row, 2, combo_side)

        combo_method = QComboBox()
        for label, _key in _CCI_METHODS:
            combo_method.addItem(label)
        if method_key:
            for i, (_label, key) in enumerate(_CCI_METHODS):
                if key == method_key:
                    combo_method.setCurrentIndex(i)
                    break
        self.table.setCellWidget(row, 3, combo_method)

        combo_norm = QComboBox()
        for label, _key in _CCI_NORMALIZE_OPTIONS:
            combo_norm.addItem(label)
        if norm_key:
            for i, (_label, key) in enumerate(_CCI_NORMALIZE_OPTIONS):
                if key == norm_key:
                    combo_norm.setCurrentIndex(i)
                    break
        self.table.setCellWidget(row, 4, combo_norm)

    def _remove_selected_rows(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def pairs(self):
        """Returns [(muscle_a, muscle_b, side, method_key, normalize_key), ...],
        skipping rows where A and B are the same channel."""
        result = []
        for row in range(self.table.rowCount()):
            combo_a = self.table.cellWidget(row, 0)
            combo_b = self.table.cellWidget(row, 1)
            combo_side = self.table.cellWidget(row, 2)
            combo_method = self.table.cellWidget(row, 3)
            combo_norm = self.table.cellWidget(row, 4)
            a, b = combo_a.currentText(), combo_b.currentText()
            if not a or not b or a == b:
                continue
            side = combo_side.currentText()
            method_key = _CCI_METHODS[combo_method.currentIndex()][1]
            norm_key = _CCI_NORMALIZE_OPTIONS[combo_norm.currentIndex()][1]
            result.append((a, b, side, method_key, norm_key))
        return result


class GaitAnalysisDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Gait Analysis"))
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMinimizeButtonHint
                             | Qt.WindowType.WindowMaximizeButtonHint)

        self._profile = None
        self._model = None
        self._controller = None
        # Non-modal Manual Gait Events dialog currently open, or None (see
        # _on_manual_gait_events) -- tracked so a second click raises the
        # existing one instead of opening a duplicate, and so a fresh trial
        # load can close it out.
        self._manual_events_dlg = None
        # Header info from the last "Detect Gait Events" run (source/forward
        # axis/warnings/markers used) -- None until a detection has run;
        # _build_results_text needs this to render anything (see _load).
        self._last_detect_meta = None

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

        # Crop -- left of "Detect Gait Events". Non-destructive: drags a
        # shaded region on whatever's plotted below (see PlayPlotWidget.
        # set_crop_mode), Apply just stores (t0, t1) on the shared
        # profile.crop_interval the app already uses elsewhere (see
        # workspace.profile); nothing is deleted or resampled. Downstream
        # calculations (_compute_gait_metrics) filter to this window when set.
        self._crop_btn = QPushButton(self.tr("Crop"))
        self._crop_btn.setStyleSheet(buttonStyle)
        self._crop_btn.setCheckable(True)
        self._crop_btn.setEnabled(False)
        self._crop_btn.toggled.connect(self._on_crop_toggled)
        _detect_idx = self._playbar.toolbar_layout.indexOf(self._detect_btn)
        self._playbar.toolbar_layout.insertWidget(_detect_idx, self._crop_btn)
        # Connected once, directly to the button, rather than via the shared
        # manualCyclesRequested signal Controller listens on -- _load()
        # disconnects each fresh Controller's own listener so only this
        # module's manual gait-event editor opens, not the generic one.
        self._playbar.manualCyclesButton.clicked.connect(self._on_manual_gait_events)
        self._playbar.manualCyclesButton.setEnabled(False)

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
        self._filter_settings_btn.setEnabled(False)
        self._filter_settings_btn.clicked.connect(self._on_filter_settings)
        _filter_check_idx = self._playbar.toolbar_layout.indexOf(self._playbar.filterCheck)
        self._playbar.toolbar_layout.insertWidget(_filter_check_idx + 1, self._filter_settings_btn)

        # Co-contraction Index pairs -- defined here (left of Filter Settings)
        # rather than inside the report itself, since more than one pair can
        # be worth checking (e.g. ankle and knee antagonists) and picking
        # them ahead of time keeps the report a pure "review what I set up"
        # step. Reset on every new load (see _load).
        self._cci_pairs = []
        # {channel: "Right"/"Left"/"Unspecified"} -- persisted across
        # _EMGReportChannelsDialog reopens within one load so a single-leg
        # EMG setup only needs to be described once. Reset on every new load.
        self._emg_channel_side = {}
        self._cci_btn = QPushButton(self.tr("Co-contraction..."))
        self._cci_btn.setStyleSheet(buttonStyle)
        self._cci_btn.setEnabled(False)
        self._cci_btn.clicked.connect(self._on_cci_pairs)
        _filter_settings_idx = self._playbar.toolbar_layout.indexOf(self._filter_settings_btn)
        self._playbar.toolbar_layout.insertWidget(_filter_settings_idx, self._cci_btn)


        top_row = QHBoxLayout()
        self._open_btn = QPushButton(self.tr("Open C3D..."))
        self._open_btn.clicked.connect(self._on_open)
        top_row.addWidget(self._open_btn)
        self._file_label = QLabel(self.tr("No trial loaded"))
        # Stretch=1 on the file label pushes everything after it (Save /
        # Create Report) to the far right of the row.
        top_row.addWidget(self._file_label, 1)
        # Off by default -- Save/Create Report average over every detected
        # cycle unless this is checked, in which case only cycles whose
        # initial contact came from a force plate (more accurate than the
        # marker height-threshold heuristic, see gait_events._merge_side_
        # events) are used; a side with none ends up with real NaN/empty
        # results rather than a fabricated number.
        self._verified_only_check = QCheckBox(self.tr("Force-plate-verified cycles only"))
        self._verified_only_check.stateChanged.connect(self._show_cci_results)
        top_row.addWidget(self._verified_only_check)
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

        # Shown/hidden by _on_crop_toggled while the Crop button is active;
        # Clear Crop stays visible whenever profile.crop_interval is set so
        # a crop from a previous session (or Apply) can be removed without
        # re-entering crop mode.
        crop_row = QHBoxLayout()
        self._crop_status_label = QLabel(self.tr("Crop: none"))
        crop_row.addWidget(self._crop_status_label)
        self._crop_apply_btn = QPushButton(self.tr("Apply Crop"))
        self._crop_apply_btn.clicked.connect(self._on_crop_apply)
        self._crop_apply_btn.setVisible(False)
        crop_row.addWidget(self._crop_apply_btn)
        self._crop_cancel_btn = QPushButton(self.tr("Cancel"))
        self._crop_cancel_btn.clicked.connect(self._on_crop_cancel)
        self._crop_cancel_btn.setVisible(False)
        crop_row.addWidget(self._crop_cancel_btn)
        self._crop_clear_btn = QPushButton(self.tr("Clear Crop"))
        self._crop_clear_btn.clicked.connect(self._on_crop_clear)
        self._crop_clear_btn.setVisible(False)
        crop_row.addWidget(self._crop_clear_btn)
        crop_row.addStretch(1)
        layout.addLayout(crop_row)

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
        # Live-preview the results panel while the crop region is dragged --
        # connected once here since self._plot is reused across loads (see
        # _load), rather than reconnected per trial.
        self._plot.cropRangeChanged.connect(self._on_crop_range_dragged)
        # Debounces the (heavy) results recompute triggered by dragging the
        # crop region -- see _on_crop_range_dragged.
        self._pending_crop_preview = None
        self._crop_preview_timer = QTimer(self)
        self._crop_preview_timer.setSingleShot(True)
        self._crop_preview_timer.timeout.connect(self._apply_pending_crop_preview)
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

        # Set the maximized state without showing yet -- main.py's caller
        # shows this dialog via exec(), which makes it application-modal;
        # calling showMaximized() here would show it non-modal first and
        # force Qt to redo the native window (hide/re-show) to switch it to
        # modal once exec() runs, which is what caused a brief small-window
        # flicker on open.
        self.setWindowState(Qt.WindowState.WindowMaximized)

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
        if self._manual_events_dlg is not None:
            self._manual_events_dlg.close()
            self._manual_events_dlg = None

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
        self._filter_settings_btn.setEnabled(True)
        self._playbar.manualCyclesButton.setEnabled(True)
        self._crop_btn.setEnabled(True)
        self._cci_btn.setEnabled(bool(emg_obj.Channels))
        self._cci_pairs = []
        self._emg_channel_side = {}
        self._last_marker_xyz_by_label = {}
        self._last_heel_toe_labels = {}
        self._last_detect_meta = None
        self._crop_btn.setChecked(False)
        self._update_crop_status_label()
        self._sync_crop_visuals()
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

        self._apply_gait_events(result["HS"], result["TO"], result.get("event_sources"))

        # Header info for the results panel that only a fresh detection run
        # produces (source/forward axis/warnings/markers used) -- cached so
        # _build_results_text can keep rebuilding the rest of the panel
        # (crop/verified-only/CCI-aware) without re-running detection.
        self._last_detect_meta = dict(
            source=result["source"], forward_axis=result["forward_axis"],
            warnings=result.get("warnings", []),
            right_heel=right_heel or _NO_MARKER, left_heel=left_heel or _NO_MARKER,
            right_toe=right_toe or _NO_MARKER, left_toe=left_toe or _NO_MARKER,
        )
        self._refresh_results()

    def _apply_gait_events(self, hs_by_side, to_by_side, event_sources=None):
        """Replace this trial's Gait-context events with a newly detected or
        manually-edited set, and refresh the timeline/tree to match. Shared
        by _on_detect_gait_events and _on_manual_gait_events so both go
        through one code path.

        hs_by_side / to_by_side: {"Right": [t, ...], "Left": [t, ...]}.
        event_sources: gait_events.detect_gait_events()'s "event_sources"
        ({side: {"HS": {t: "plate"/"marker"}, "TO": {...}}}), or None for a
        manual edit (no detector produced these -- TrialEvent.source stays
        "" rather than guessing)."""
        for ev in [e for e in self._model.extra_events if e.context == "Gait"]:
            self._model.extra_events.remove(ev)
            if ev in self._model.events:
                self._model.events.remove(ev)
            self._controller.top.remove_event(ev)

        event_sources = event_sources or {}
        side_abbr = {"Right": "R", "Left": "L"}
        for side in ("Right", "Left"):
            hs_src = event_sources.get(side, {}).get("HS", {})
            to_src = event_sources.get(side, {}).get("TO", {})
            for n, t in enumerate(sorted(hs_by_side.get(side, [])), start=1):
                ev = TrialEvent(t, "IC_{} #{}".format(side_abbr[side], n), "Gait",
                                 source=hs_src.get(t, ""))
                self._model.extra_events.append(ev)
                self._model.events.append(ev)
                self._controller.top.add_event(ev)
            for n, t in enumerate(sorted(to_by_side.get(side, [])), start=1):
                ev = TrialEvent(t, "TO_{} #{}".format(side_abbr[side], n), "Gait",
                                 source=to_src.get(t, ""))
                self._model.extra_events.append(ev)
                self._model.events.append(ev)
                self._controller.top.add_event(ev)
        self._model.events.sort(key=lambda e: e.time_s)
        self._controller._refresh_event_tree()
        # _refresh_event_tree() just rebuilt the Events node from scratch,
        # losing any crop dimming it had -- reapply it to match whatever
        # crop is currently active (or none).
        self._sync_crop_visuals()

    def _current_hs_to_by_side(self):
        """Read the trial's current Gait-context events (whichever mix of
        auto-detected and manually-edited is live right now) back into
        {"Right": [t, ...], "Left": [t, ...]} shape for HS and TO -- the
        source of truth Save/Create Report build their per-cycle
        calculations from, instead of a possibly-stale cached detection
        result. Also returns hs_source_by_side/to_source_by_side ({"Right":
        {t: "plate"/"marker"/""}, "Left": {...}}) for the verified-cycles-
        only toggle, the Manual Cycles editor's row coloring, and the HS-to-
        opposite-TO fallback cycle (see gait_events.cycles_from_hs_or_
        fallback)."""
        hs_by_side = {"Right": [], "Left": []}
        to_by_side = {"Right": [], "Left": []}
        hs_source_by_side = {"Right": {}, "Left": {}}
        to_source_by_side = {"Right": {}, "Left": {}}
        if self._model is None:
            return hs_by_side, to_by_side, hs_source_by_side, to_source_by_side
        for ev in self._model.extra_events:
            if ev.context != "Gait":
                continue
            base = ev.label.split(" #")[0]
            if base not in _GAIT_EVENT_TYPES:
                continue
            kind, abbr = base.split("_")
            side = "Right" if abbr == "R" else "Left"
            (hs_by_side if kind == "IC" else to_by_side)[side].append(ev.time_s)
            if kind == "IC":
                hs_source_by_side[side][ev.time_s] = ev.source
            else:
                to_source_by_side[side][ev.time_s] = ev.source
        for d in (hs_by_side, to_by_side):
            for side in d:
                d[side].sort()
        return hs_by_side, to_by_side, hs_source_by_side, to_source_by_side

    # ── Manual gait-event correction ─────────────────────────────────────────

    def _on_manual_gait_events(self):
        if self._model is None:
            return
        if self._manual_events_dlg is not None:
            self._manual_events_dlg.raise_()
            self._manual_events_dlg.activateWindow()
            return

        existing = []
        for ev in sorted(self._model.extra_events, key=lambda e: e.time_s):
            if ev.context != "Gait":
                continue
            event_type = ev.label.split(" #")[0]
            if event_type in _GAIT_EVENT_TYPES:
                existing.append((event_type, ev.time_s, ev.source))

        dlg = _ManualGaitEventsDialog(existing, self._model.total_time(), self)
        # Non-modal and shown rather than exec()'d: exec() would disable this
        # whole window (playbar included) until the dialog closes, but the
        # point of this editor is to let the user play/scrub the trial below
        # and type in event times as they go.
        dlg.setModal(False)
        dlg.finished.connect(self._on_manual_gait_events_finished)
        self._manual_events_dlg = dlg
        dlg.show()

    def _on_manual_gait_events_finished(self, result):
        dlg = self._manual_events_dlg
        self._manual_events_dlg = None
        if dlg is None or result != QDialog.DialogCode.Accepted:
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
        if self._last_detect_meta is not None:
            # A prior "Detect Gait Events" run already cached the marker
            # arrays/heel-toe labels _compute_gait_metrics needs, so the
            # full panel (steps/stride/phase/CCI) can be rebuilt right away
            # instead of just showing the raw edited event list.
            self._refresh_results()
        else:
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

    # ── Co-contraction Index pairs ───────────────────────────────────────────

    def _on_cci_pairs(self):
        if self._profile is None:
            return
        dlg = _CCIPairsDialog(self._profile.emg.Channels, self._cci_pairs, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._cci_pairs = dlg.pairs()
        self._show_cci_results()

    def _show_cci_results(self):
        """Historical name for "rebuild the results panel" -- kept as the
        slot wired to the CCI-pairs-picker and the verified-only-cycles
        checkbox (see __init__/_on_cci_pairs) since Co-contraction Index is
        just one section of _build_results_text now."""
        self._refresh_results()

    # ── Crop ──────────────────────────────────────────────────────────────────

    def _on_crop_toggled(self, checked):
        if self._model is None:
            self._crop_btn.setChecked(False)
            return
        if checked:
            t0, t1 = self._profile.crop_interval or (0.0, self._model.total_time())
            self._plot.set_crop_mode(True, (t0, t1))
            self._crop_status_label.setText(self.tr(
                "Drag the shaded region on the plot(s) below, then Apply Crop or Cancel."
            ))
            self._crop_apply_btn.setVisible(True)
            self._crop_cancel_btn.setVisible(True)
            self._crop_clear_btn.setVisible(False)
            self._sync_crop_visuals((t0, t1))
            self._refresh_results(crop_override=(t0, t1))
        else:
            self._crop_preview_timer.stop()
            self._pending_crop_preview = None
            self._plot.set_crop_mode(False)
            self._crop_apply_btn.setVisible(False)
            self._crop_cancel_btn.setVisible(False)
            self._update_crop_status_label()
            self._sync_crop_visuals()
            self._refresh_results()

    def _on_crop_range_dragged(self, t0, t1):
        """Live preview while the crop region is being dragged (see
        PlayPlotWidget.cropRangeChanged, connected once in __init__) --
        recomputes and shows results for the range being dragged without
        touching profile.crop_interval, so Cancel can still discard it.

        The event-line/tree-dimming sync (_sync_crop_visuals) happens right
        away, every time -- just restyling existing items, cheap regardless
        of drag frequency. The results-panel recompute is debounced via
        _crop_preview_timer instead: pyqtgraph emits this on every mouse-
        move while dragging, and _compute_gait_metrics/CCI (which filters
        full EMG envelopes) is heavy enough that running it synchronously on
        each one blocked the main thread long enough to make the region's
        own drag rendering visibly lag."""
        if not self._crop_btn.isChecked():
            return
        self._sync_crop_visuals((t0, t1))
        self._pending_crop_preview = (t0, t1)
        self._crop_preview_timer.start(120)

    def _apply_pending_crop_preview(self):
        if self._pending_crop_preview is not None:
            self._refresh_results(crop_override=self._pending_crop_preview)

    def _sync_crop_visuals(self, range_override=None):
        """Keep the plot's event-line visibility and the tree's Events-group
        dimming in sync with the current (or previewed) crop range -- see
        PlayPlotWidget.set_crop_event_filter / _apply_crop_dim_to_event_tree.
        Cheap enough to run on every drag signal, unlike the heavier
        results-panel recompute (_on_crop_range_dragged/_crop_preview_timer)."""
        rng = range_override if range_override is not None else (
            self._profile.crop_interval if self._profile is not None else None
        )
        self._plot.set_crop_event_filter(rng)
        _apply_crop_dim_to_event_tree(self._label_tree, rng)

    def _on_crop_apply(self):
        rng = self._plot.get_crop_range_s()
        if rng is not None:
            self._profile.crop_interval = rng
        self._crop_btn.setChecked(False)

    def _on_crop_cancel(self):
        self._crop_btn.setChecked(False)

    def _on_crop_clear(self):
        self._profile.crop_interval = None
        self._update_crop_status_label()
        self._sync_crop_visuals()
        self._refresh_results()

    def _update_crop_status_label(self):
        ci = self._profile.crop_interval if self._profile is not None else None
        if ci:
            self._crop_status_label.setText(self.tr("Crop: {:.3f}s - {:.3f}s").format(ci[0], ci[1]))
            self._crop_clear_btn.setVisible(True)
        else:
            self._crop_status_label.setText(self.tr("Crop: none"))
            self._crop_clear_btn.setVisible(False)

    @staticmethod
    def _filter_hs_by_crop(hs_by_side, crop_interval):
        """Keep only HS times inside profile.crop_interval, non-destructively
        -- detection still runs (and Manual Cycles still shows) the whole
        trial; only the calculated parameters are restricted to the cropped
        segment. No-op when no crop is set."""
        if not crop_interval:
            return hs_by_side
        t0, t1 = crop_interval
        return {side: [t for t in times if t0 <= t <= t1] for side, times in hs_by_side.items()}

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
        # Co-contraction pairs and per-channel side assignments are cached
        # by channel name (see self._cci_pairs / self._emg_channel_side) --
        # without this they'd keep pointing at a name that no longer exists,
        # silently going stale (or making _CCIPairsDialog fall back to a
        # different channel next time it's reopened) instead of following
        # the rename like the tree does.
        self._cci_pairs = [
            (new_name if a == old_name else a, new_name if b == old_name else b,
             side, method_key, norm_key)
            for a, b, side, method_key, norm_key in self._cci_pairs
        ]
        if old_name in self._emg_channel_side:
            self._emg_channel_side[new_name] = self._emg_channel_side.pop(old_name)
        self._show_cci_results()

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

    @staticmethod
    def _mean_sd_list(values):
        return (float(np.mean(values)), float(np.std(values))) if values else (float("nan"), float("nan"))

    @staticmethod
    def _filter_verified_hs(hs_by_side, hs_source_by_side):
        """Keep only HS times whose initial contact came from a force plate
        (see gait_events._merge_side_events) -- used when the "Force-plate-
        verified cycles only" checkbox is on. A side with no plate-sourced
        HS at all ends up with an empty list here, which downstream
        (pair_hs_to/cycles_from_hs) correctly turns into real NaN/empty
        results rather than a fabricated number -- a single- or two-plate
        lab often just doesn't have enough verified footfalls for a full
        cycle-to-cycle metric, and that's real, not a bug."""
        return {
            side: [t for t in times if hs_source_by_side.get(side, {}).get(t) == "plate"]
            for side, times in hs_by_side.items()
        }

    def _compute_gait_metrics(self, verified_only=False, crop_override=None):
        """Shared by Save, Create Report, and the live results panel: read
        the trial's current event set, recover real same-foot HS/TO pairs
        (see pair_hs_to -- needed for phase-percentage and toe-out-angle
        math, not just cadence/length), and compute spatiotemporal +
        phase-percentage + toe-out metrics.

        crop_override, when given, is used instead of profile.crop_interval
        -- lets the results panel preview a crop range still being dragged
        (see _on_crop_range_dragged) without committing it.

        Returns (hs_by_side, to_by_side, kin, fs_k, spatio, phases, toe_out,
        step_agg). to_by_side is included (crop/verified-only filtered the
        same way as hs_by_side) for the HS-to-opposite-TO fallback cycle
        used by EMG/CCI when a side has no verified same-foot HS-HS cycle
        (see gait_events.cycles_from_hs_or_fallback).
        """
        hs_by_side, to_by_side, hs_source_by_side, to_source_by_side = self._current_hs_to_by_side()
        crop = crop_override if crop_override is not None else self._profile.crop_interval
        hs_by_side = self._filter_hs_by_crop(hs_by_side, crop)
        to_by_side = self._filter_hs_by_crop(to_by_side, crop)
        if verified_only:
            hs_by_side = self._filter_verified_hs(hs_by_side, hs_source_by_side)
            to_by_side = self._filter_verified_hs(to_by_side, to_source_by_side)
        kin = self._profile.kinematic
        fs_k = kin.point_fs
        heel = self._last_heel_toe_labels
        events_by_side = {
            side: _gait.pair_hs_to(hs_by_side[side], to_by_side[side]) for side in ("Right", "Left")
        }
        spatio = _gait.compute_spatiotemporals(
            events_by_side, self._last_marker_xyz_by_label, fs_k,
            right_heel_label=heel.get("right_heel", "RHEE"),
            left_heel_label=heel.get("left_heel", "LHEE"),
        )
        phases = _gait.compute_phase_percentages(events_by_side, to_by_side)

        forward_axis_idx = 0 if spatio["forward_axis"] == "X" else 1
        toe_out = {}
        for side, heel_key, toe_key in (
            ("Right", "right_heel", "right_toe"), ("Left", "left_heel", "left_toe"),
        ):
            heel_label = heel.get(heel_key)
            toe_label = heel.get(toe_key)
            if heel_label and toe_label:
                toe_out[side] = _gait.compute_toe_out_angles(
                    events_by_side[side], self._last_marker_xyz_by_label, fs_k,
                    heel_label, toe_label, forward_axis_idx, mirror=(side == "Left"),
                )
            else:
                toe_out[side] = (float("nan"), float("nan"))

        step_agg = _gait.aggregate_steps(spatio["steps"])

        return hs_by_side, to_by_side, kin, fs_k, spatio, phases, toe_out, step_agg

    @staticmethod
    def _phases_all_nan(phases):
        for side in ("Right", "Left"):
            for key in ("stance_pct", "swing_pct", "loading_response_pct",
                        "pre_swing_pct", "single_support_pct"):
                if not np.isnan(phases[side][key][0]):
                    return False
        return True

    def _warn_if_verified_only_too_sparse(self, phases):
        """A 1-2-plate lab typically only verifies one footfall per foot --
        not enough to form a single HS-to-next-HS cycle. Gait Phase
        Parameters/the illustration fall back to the same approximate HS-to-
        opposite-foot-TO window EMG/CCI already use in that case (see
        gait_events.compute_phase_percentages's to_by_side fallback), so
        this only fires when even that isn't possible (e.g. no verified
        contact at all on one foot) -- without it, a mostly-empty report
        with "Force-plate-verified cycles only" checked looks like something
        broke rather than like real data ran out."""
        if self._verified_only_check.isChecked() and self._phases_all_nan(phases):
            QMessageBox.information(
                self, self.tr("Force-plate-verified cycles only"),
                self.tr(
                    "This trial doesn't have enough force-plate-verified footfalls "
                    "to compute Gait Phase Parameters or the gait-cycle illustration "
                    "for either foot -- not even the approximate HS-to-opposite-foot-"
                    "TO window EMG/CCI can fall back to. Step-level Spatial/Time "
                    "metrics (which need just one verified contact per foot) are "
                    "unaffected. Uncheck the box to use all detected cycles instead."
                ),
                QMessageBox.Ok,
            )

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

        hs_by_side, to_by_side, kin, fs_k, spatio, phases, toe_out, step_agg = self._compute_gait_metrics(
            verified_only=self._verified_only_check.isChecked(),
        )
        self._warn_if_verified_only_too_sparse(phases)

        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                self._write_gait_parameters_csv(writer, spatio, phases, toe_out, step_agg)
                self._write_emg_cycle_metrics_csv(writer, hs_by_side, to_by_side)
                self._write_cci_csv(writer, hs_by_side, to_by_side)
                self._write_joint_angle_csv(writer, hs_by_side, kin, fs_k)
        except OSError as e:
            QMessageBox.critical(self, self.tr("error"), str(e), QMessageBox.Ok)
            return

        QMessageBox.information(
            self, self.tr("Save Gait Analysis CSV"),
            self.tr("Saved to '{}'.").format(path), QMessageBox.Ok,
        )

    def _write_gait_parameters_csv(self, writer, spatio, phases, toe_out, step_agg):
        writer.writerow(["Gait Parameters -- Steps"])
        writer.writerow([
            "Side", "HS Time (s)", "Step Length (m)", "Step Width (m)",
            "Step Time (s)", "Cadence (steps/min)",
        ])
        for step in spatio["steps"]:
            writer.writerow([
                step["side"], "{:.3f}".format(step["hs_t"]),
                self._fmt(step["step_length_m"]), self._fmt(step["step_width_m"]),
                "{:.3f}".format(step["step_time_s"]), self._fmt(step["cadence_spm"]),
            ])
        writer.writerow([])

        writer.writerow(["Gait Parameters -- Stride (mean +/- SD across cycles)"])
        writer.writerow([
            "Side", "Stride Length Mean (m)", "Stride Length SD",
            "Stride Time Mean (s)", "Stride Time SD",
            "Cadence Mean (strides/min)", "Cadence SD",
            "Velocity Mean (m/s)", "Velocity SD",
        ])
        for side in ("Right", "Left"):
            s = spatio["stride"][side]
            row = [side]
            for key in ("stride_length_m", "stride_time_s", "cadence_spm", "velocity_m_s"):
                mean, sd = s[key]
                row.extend([self._fmt(mean), self._fmt(sd)])
            writer.writerow(row)
        writer.writerow([])

        writer.writerow(["Gait Phase Parameters (mean +/- SD across cycles, % of gait cycle)"])
        writer.writerow(["Parameter", "Right Mean", "Right SD", "Left Mean", "Left SD"])
        for key, label in (
            ("stance_pct", "Stance Phase"),
            ("loading_response_pct", "Loading Response"),
            ("single_support_pct", "Single Support"),
            ("pre_swing_pct", "Pre-Swing"),
            ("swing_pct", "Swing Phase"),
        ):
            r_mean, r_sd = phases["Right"][key]
            l_mean, l_sd = phases["Left"][key]
            writer.writerow([label, self._fmt(r_mean), self._fmt(r_sd), self._fmt(l_mean), self._fmt(l_sd)])
        ds_mean, ds_sd = phases["double_support_pct"]
        writer.writerow(["Double Support (combined, both feet)", self._fmt(ds_mean), self._fmt(ds_sd), "", ""])
        is_fallback = phases.get("is_fallback", {})
        for side in ("Right", "Left"):
            if is_fallback.get(side):
                writer.writerow([
                    "Note: {} phase percentages use an approximate HS-to-opposite-foot-TO "
                    "window (not a verified full HS-to-HS gait cycle)".format(side)
                ])
        writer.writerow([])

        writer.writerow(["Gait Spatial Parameters -- Step Summary (mean +/- SD across steps)"])
        writer.writerow([
            "Side", "Step Length Mean (m)", "Step Length SD",
            "Step Time Mean (s)", "Step Time SD",
            "Cadence Mean (steps/min)", "Cadence SD",
        ])
        for side in ("Right", "Left"):
            row = [side]
            for key in ("step_length_m", "step_time_s", "cadence_spm"):
                mean, sd = step_agg[side][key]
                row.extend([self._fmt(mean), self._fmt(sd)])
            writer.writerow(row)
        w_mean, w_sd = step_agg["step_width_m"]
        writer.writerow(["Step Width (combined)", self._fmt(w_mean), self._fmt(w_sd), "", "", "", ""])
        writer.writerow([])

        writer.writerow(["Gait Spatial Parameters -- Toe-out Angle (deg, mean +/- SD)"])
        writer.writerow(["Side", "Mean", "SD"])
        for side in ("Right", "Left"):
            mean, sd = toe_out[side]
            writer.writerow([side, self._fmt(mean), self._fmt(sd)])
        writer.writerow([])

    def _write_emg_cycle_metrics_csv(self, writer, hs_by_side, to_by_side):
        writer.writerow(["EMG Metrics per Gait Cycle"])
        writer.writerow(["Channel", "Side", "Cycle", "Cycle Start (s)", "Cycle End (s)",
                          "Mean Envelope", "Peak Envelope", "Approximate Cycle"])
        channels = self._profile.emg.Channels
        if not channels:
            writer.writerow(["(no EMG channels loaded for this trial)"])
            writer.writerow([])
            return
        for chan in channels:
            envelope, fs_emg = self._emg_envelope(chan)
            if len(envelope) == 0:
                continue
            chan_side = self._emg_channel_side.get(chan, "Unspecified")
            t = np.arange(len(envelope)) / fs_emg
            for side in ("Right", "Left"):
                if chan_side not in ("Unspecified", side):
                    continue
                opposite = "Left" if side == "Right" else "Right"
                cycles, is_fallback = _gait.cycles_from_hs_or_fallback(
                    hs_by_side[side], to_by_side[opposite],
                )
                for i, (t0, t1) in enumerate(cycles, start=1):
                    seg = envelope[(t >= t0) & (t < t1)]
                    if len(seg) == 0:
                        continue
                    writer.writerow([
                        chan, side, i, "{:.3f}".format(t0), "{:.3f}".format(t1),
                        "{:.5f}".format(seg.mean()), "{:.5f}".format(seg.max()),
                        "yes" if is_fallback else "",
                    ])
        writer.writerow([])

    def _write_cci_csv(self, writer, hs_by_side, to_by_side):
        writer.writerow(["Muscle Co-contraction Index"])
        writer.writerow(["Muscle A", "Muscle B", "Side", "Method", "Normalization",
                          "CCI", "Approximate Cycle"])
        if not self._cci_pairs:
            writer.writerow(["(no Co-contraction pairs configured -- see \"Co-contraction...\" button)"])
            writer.writerow([])
            return
        for a_name, b_name, side, method_key, norm_key in self._cci_pairs:
            a_env, a_fs = self._emg_envelope(a_name)
            b_env, b_fs = self._emg_envelope(b_name)
            cci, is_fallback = _gait.compute_cci_pair(
                a_name, a_env, a_fs, b_name, b_env, b_fs, hs_by_side, to_by_side,
                side, method_key, norm_key,
            )
            writer.writerow([
                a_name, b_name, side, _CCI_METHOD_LABELS.get(method_key, method_key),
                _CCI_NORMALIZE_LABELS.get(norm_key, norm_key),
                self._fmt(cci), "yes" if is_fallback else "",
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
        hs_by_side, to_by_side, kin, fs_k, spatio, phases, toe_out, step_agg = self._compute_gait_metrics(
            verified_only=self._verified_only_check.isChecked(),
        )
        self._warn_if_verified_only_too_sparse(phases)

        # Pre-computed once and handed to the report -- both the EMG activity
        # bar chart and the Co-contraction Index pairs (picked ahead of time
        # via the toolbar's "Co-contraction..." button, see _on_cci_pairs)
        # work off the same enveloped signals rather than recomputing per
        # channel.
        emg_envelopes = {}
        for chan in self._profile.emg.Channels:
            envelope, fs_emg = self._emg_envelope(chan)
            if len(envelope) > 0:
                emg_envelopes[chan] = (envelope, fs_emg)

        # A fresh, one-off pick every time -- not remembered, not shared with
        # the Co-contraction pair picker (which has its own Side column --
        # see _CCIPairsDialog). Channel->side IS persisted across reopens
        # (self._emg_channel_side) so re-generating a report doesn't make
        # you re-answer "which leg is this sensor on" every time.
        #
        # Kept per-side rather than pooling Right+Left cycles into one number
        # -- a "gait cycle" here is IC of one foot to the next IC of that
        # SAME foot (see cycles_from_hs), matching how phases/CCI/joint
        # angles are all computed elsewhere in this report; blending both
        # feet's cycles together would mix two different feet's muscle
        # activity into one meaningless average. A channel recorded on only
        # one leg (side != "Unspecified") only gets that side's bar -- the
        # other side is left NaN rather than computed from the wrong leg's
        # sensor.
        emg_means = {}
        if emg_envelopes:
            channel_dlg = _EMGReportChannelsDialog(
                list(emg_envelopes.keys()), self, existing_sides=self._emg_channel_side,
            )
            if channel_dlg.exec() != QDialog.DialogCode.Accepted:
                return
            self._emg_channel_side.update(channel_dlg.sides())
            selection = channel_dlg.selection()
            for orig_name, display_name, chan_side in selection:
                envelope, fs_emg = emg_envelopes[orig_name]
                t = np.arange(len(envelope)) / fs_emg
                side_means = {}
                for side in ("Right", "Left"):
                    if chan_side not in ("Unspecified", side):
                        side_means[side] = (float("nan"), float("nan"))
                        continue
                    opposite = "Left" if side == "Right" else "Right"
                    cycles, _is_fallback = _gait.cycles_from_hs_or_fallback(
                        hs_by_side[side], to_by_side[opposite],
                    )
                    cycle_means = []
                    for t0, t1 in cycles:
                        seg = envelope[(t >= t0) & (t < t1)]
                        if len(seg) > 0:
                            cycle_means.append(float(seg.mean()))
                    side_means[side] = self._mean_sd_list(cycle_means)
                emg_means[display_name] = side_means

        dlg = _GaitReportDialog(
            spatio, phases, toe_out, step_agg, hs_by_side, to_by_side, kin, fs_k,
            emg_means, emg_envelopes, self._cci_pairs, self,
        )
        dlg.exec()

    def _refresh_results(self, crop_override=None):
        """Rebuild the whole results panel from the trial's *current* live
        state -- called after detection, a manual edit, a rename, a crop
        change (applied or previewed), the verified-only toggle, or the CCI
        pairs picker, so the panel never shows numbers computed under a
        setting that no longer holds (see _build_results_text)."""
        self._results.setPlainText(self._build_results_text(crop_override))

    def _build_results_text(self, crop_override=None):
        """Full contents of the results panel: the header captured at the
        last "Detect Gait Events" run, plus spatiotemporal/phase/toe-out and
        Co-contraction Index recomputed fresh from whatever crop/verified-
        only/CCI-pairs settings are live right now -- same math and same
        crop_override preview mechanism Save/Create Report use (see
        _compute_gait_metrics). Returns "" before any detection has run."""
        if self._model is None or self._last_detect_meta is None:
            return ""
        meta = self._last_detect_meta
        hs_by_side, to_by_side, kin, fs_k, spatio, phases, toe_out, step_agg = self._compute_gait_metrics(
            verified_only=self._verified_only_check.isChecked(), crop_override=crop_override,
        )

        lines = [
            "Source: {}".format(meta["source"]),
            "Forward axis (inferred): {}".format(meta["forward_axis"]),
            "Markers used: RHeel={}, LHeel={}, RToe={}, LToe={}".format(
                meta["right_heel"], meta["left_heel"], meta["right_toe"], meta["left_toe"]
            ),
            "",
        ]
        for side in ("Right", "Left"):
            lines.append("{} IC ({}): {}".format(
                side, len(hs_by_side[side]),
                ", ".join("{:.3f}s".format(t) for t in hs_by_side[side]),
            ))
            lines.append("{} TO ({}): {}".format(
                side, len(to_by_side[side]),
                ", ".join("{:.3f}s".format(t) for t in to_by_side[side]),
            ))

        lines.append("")
        lines.append("Steps:")
        for step in spatio["steps"]:
            lines.append(
                "  {side} @ {hs_t:.3f}s -- length {step_length_m:.3f} m, "
                "time {step_time_s:.3f} s, cadence {cadence_spm:.1f} steps/min".format(**step)
            )

        lines.append("")
        lines.append("Stride (mean +/- SD across cycles):")
        for side in ("Right", "Left"):
            s = spatio["stride"][side]
            lines.append(
                "  {side}: length {0[0]:.3f}+/-{0[1]:.3f} m, time {1[0]:.3f}+/-{1[1]:.3f} s, "
                "cadence {2[0]:.1f}+/-{2[1]:.1f} strides/min, velocity {3[0]:.3f}+/-{3[1]:.3f} m/s".format(
                    s["stride_length_m"], s["stride_time_s"], s["cadence_spm"], s["velocity_m_s"],
                    side=side,
                )
            )

        lines.append("")
        lines.append("Gait Phase (% of cycle, mean +/- SD):")
        for side in ("Right", "Left"):
            p = phases[side]
            lines.append(
                "  {side}: stance {stance_pct[0]:.1f}+/-{stance_pct[1]:.1f}, "
                "swing {swing_pct[0]:.1f}+/-{swing_pct[1]:.1f}, "
                "loading resp. {loading_response_pct[0]:.1f}+/-{loading_response_pct[1]:.1f}, "
                "single support {single_support_pct[0]:.1f}+/-{single_support_pct[1]:.1f}, "
                "pre-swing {pre_swing_pct[0]:.1f}+/-{pre_swing_pct[1]:.1f}".format(side=side, **p)
            )
        ds_mean, ds_sd = phases["double_support_pct"]
        lines.append("  Double support (combined): {:.1f}+/-{:.1f}".format(ds_mean, ds_sd))

        lines.append("")
        lines.append("Toe-out angle (deg, mean +/- SD):")
        for side in ("Right", "Left"):
            mean, sd = toe_out[side]
            lines.append("  {}: {:.1f}+/-{:.1f}".format(side, mean, sd))

        if meta["warnings"]:
            lines.append("")
            lines.append("Warnings:")
            for w in meta["warnings"]:
                lines.append("  - " + w)

        if self._cci_pairs:
            lines.append("")
            lines.append("Co-contraction Index:")
            for a_name, b_name, side, method_key, norm_key in self._cci_pairs:
                a_env, a_fs = self._emg_envelope(a_name)
                b_env, b_fs = self._emg_envelope(b_name)
                cci, is_fallback = _gait.compute_cci_pair(
                    a_name, a_env, a_fs, b_name, b_env, b_fs, hs_by_side, to_by_side,
                    side, method_key, norm_key,
                )
                method_label = _CCI_METHOD_LABELS.get(method_key, method_key)
                norm_label = _CCI_NORMALIZE_LABELS.get(norm_key, norm_key)
                value_txt = "n/a" if np.isnan(cci) else "{:.3f}".format(cci)
                fallback_txt = (
                    " [approximate: HS-to-opposite-TO window, not a verified full cycle]"
                    if is_fallback else ""
                )
                lines.append("  {} vs {} ({}, {}, {}): {}{}".format(
                    a_name, b_name, side, method_label, norm_label, value_txt, fallback_txt,
                ))

        if crop_override is not None:
            lines.append("")
            lines.append(
                "(Previewing crop {:.3f}s - {:.3f}s -- \"Apply Crop\" to keep it, "
                "\"Cancel\" to discard)".format(*crop_override)
            )

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
