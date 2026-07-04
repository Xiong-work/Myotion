from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QVBoxLayout, QInputDialog, QMenu, QMessageBox, QTreeWidgetItem
import numpy as np
import scipy.signal as _sig

from modules.kinematics.model import Model
from modules.kinematics.playbarwidget import PlayBarWidget
from modules.kinematics.playplotview import PlayPlotWidget
from modules.kinematics.renderwidget import RenderWidget
from modules.kinematics.manual_cycles_dialog import ManualCyclesDialog
from modules.pyMotion.core.trial import TrialEvent
from modules.pyMotion.core.onset_detection import detect_emg_onsets
from modules.pyMotion.core.cycle_detection import (
    detect_gait_cycles, detect_sit_stand_cycles, detect_squat_cycles,
    detect_trunk_flexion_cycles, detect_lifting_cycles, detect_pointing_cycles,
    detect_peak_cycles, _cycles_from_events,
)

# Task type (playbar combo text) -> detector dispatch. Each takes
# (marker_z, marker_xyz, fs, fp_vertical, fp_fs) and returns
# list[(t_start_s, t_end_s)]. marker_z (single vertical axis) drives the
# gait/vertical-burst family; marker_xyz (full 3D trajectory) drives the
# reach family (lifting/pointing) since those motions aren't purely vertical.
_CYCLE_DETECTORS = {
    "Gait":                     lambda z, xyz, fs, fp, fp_fs: detect_gait_cycles(z, fs, fp_vertical=fp, fp_fs=fp_fs, mode="walk"),
    "Running":                  lambda z, xyz, fs, fp, fp_fs: detect_gait_cycles(z, fs, fp_vertical=fp, fp_fs=fp_fs, mode="run"),
    "Sit-to-Stand":             lambda z, xyz, fs, fp, fp_fs: detect_sit_stand_cycles(z, fs),
    "Squat":                    lambda z, xyz, fs, fp, fp_fs: detect_squat_cycles(z, fs),
    # CMJ (countermovement jump): crouch -> explode -> land is the same
    # single continuous vertical-velocity burst shape as squat/sit-to-stand,
    # so it reuses that detector rather than needing a new algorithm.
    "CMJ":                      lambda z, xyz, fs, fp, fp_fs: detect_squat_cycles(z, fs),
    "Trunk Flexion/Extension":  lambda z, xyz, fs, fp, fp_fs: detect_trunk_flexion_cycles(z, fs),
    "Lifting":                  lambda z, xyz, fs, fp, fp_fs: detect_lifting_cycles(xyz, fs),
    "Pointing":                 lambda z, xyz, fs, fp, fp_fs: detect_pointing_cycles(xyz, fs),
}


class Controller:
    """
    controller for kinematics module
    which receive the model and responsible for storing all states of the model
    """

    def __init__(
        self,
        model: Model,
        render: RenderWidget,
        playbar: PlayBarWidget,
        top: PlayPlotWidget,
        bottom: QVBoxLayout,
        labeltree,
        participant_item=None,
        save_callback=None,
        export_events_callback=None,
    ) -> None:
        self.model = model
        self.frame = 0
        self.render = render
        self.playbar = playbar
        self.top = top
        self.bottom = bottom
        self.labeltree = labeltree
        self.participant_item = participant_item  # QTreeWidgetItem for the active participant
        self._save_callback = save_callback
        self._export_events_callback = export_events_callback

        # No markers to render for an EMG-only participant — leave the 3D pane
        # showing its "no model" placeholder rather than an empty scene.
        self.render.setModel(model.kinematic if model.has_kinematics else None)
        self.render.setController(self)

        self.playbar.setController(self)
        self.playbar.slider.setRange(0, model.kinematic_frames() - 1)
        self.playbar.set_frame_rate(model.kinematic_frame_rate())
        self.playbar.enable_playback()
        self.top.set_kinematic_fps(model.kinematic_frame_rate())

        self.playbar.slider.valueChanged.connect(self.slider_valuechange)
        self.playbar.playbutton.clicked.connect(self.on_play_button_clicked)
        self.playbar.prevFrameButton.clicked.connect(self.on_prev_frame_button_clicked)
        self.playbar.nextFrameButton.clicked.connect(self.on_next_frame_button_clicked)
        self.playbar.step.currentTextChanged.connect(self.on_combo_box_changed)
        self.playbar.eventMarkRequested.connect(self._onMarkEvent)
        self.playbar.exportEventsRequested.connect(self._onExportEvents)
        self.playbar.onsetDetectionToggled.connect(self._onOnsetDetectionToggled)
        self.playbar.cycleDetectionToggled.connect(self._onCycleDetectionToggled)
        self.playbar.manualCyclesRequested.connect(self._onManualCyclesRequested)
        self.playbar.taskTypeChanged.connect(self._onTaskTypeChanged)
        self.playbar.cycleMarkersVisibilityToggled.connect(self.top.set_cycle_markers_visible)

        # Cache the last-selected EMG channel so the onset button can act without re-click
        self._current_emg_channel = None
        self._current_emg_arr = None
        self._current_emg_fs = None
        # Task type last run through cycle detection — needed to know which
        # CycleStart_/CycleEnd_ events to remove when the button is unchecked.
        self._current_cycle_task = None
        # Cached, reused, non-modal (see ManualCyclesDialog docstring) — built
        # lazily on first "Manual Cycles…" click.
        self._manualCyclesDialog = None
        self.playbar.set_source_options(
            self.model.kinematic.reallabels if self.model.has_kinematics else [],
            self.model.kinematic.anglelabels if self.model.has_kinematics else [],
            [fp.plate_id for fp in self.model.force_plates],
        )
        self._onTaskTypeChanged(self.playbar.current_task_type())
        self.step = 1
        self.labeltree.itemDoubleClicked.connect(self.tree_item_select)
        # Last item plotted in Signals — replayed by _onAxisFilterChanged so
        # switching the Axis combo re-plots instantly instead of only taking
        # effect on the next double-click.
        self._last_tree_item = None
        self.playbar.axisFilterCombo.currentTextChanged.connect(self._onAxisFilterChanged)

        # Right-click on label tree → delete event
        self.labeltree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.labeltree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self._event_tree_root = None
        self._crop_tree_item = None

        self.timer = QTimer()
        self.timer.start(int(1000 / self.model.kinematic_frame_rate()))
        self.timer.timeout.connect(self.update)

        # Populate event tree and draw C3D events already in model
        self._refresh_event_tree()
        for ev in self.model.events:
            self.top.add_event(ev)
        # Show crop interval if one was previously saved for this participant
        self._refresh_crop_display()

        # Build force-channel lookup so tree_item_select() can find plate data.
        # Tree nodes are added by populateKinematicTree() in main.py under the
        # participant item, so the controller never writes to the tree directly.
        self._force_channels = {}
        for fp in self.model.force_plates:
            for comp in ("Fx", "Fy", "Fz"):
                self._force_channels["Plate{} {}".format(fp.plate_id, comp)] = (fp, comp)

    def stop(self):
        """Disconnect every signal this controller connected and stop its timer.

        Each participant load builds a brand new Controller against the same
        shared widgets (playbar, renderWidget, kinematics_label_tree) without
        ever tearing down the previous one — left alone, old timers keep
        ticking and old handlers keep firing (e.g. duplicate tree_item_select
        calls, or an old timer repainting the render widget with a stale
        frame) on top of whatever loads next. main.py.loadKinemtic() calls
        this on the previously-active controller before creating a new one.
        """
        self.timer.stop()
        for signal, slot in (
            (self.timer.timeout, self.update),
            (self.playbar.slider.valueChanged, self.slider_valuechange),
            (self.playbar.playbutton.clicked, self.on_play_button_clicked),
            (self.playbar.prevFrameButton.clicked, self.on_prev_frame_button_clicked),
            (self.playbar.nextFrameButton.clicked, self.on_next_frame_button_clicked),
            (self.playbar.step.currentTextChanged, self.on_combo_box_changed),
            (self.playbar.eventMarkRequested, self._onMarkEvent),
            (self.playbar.exportEventsRequested, self._onExportEvents),
            (self.playbar.onsetDetectionToggled, self._onOnsetDetectionToggled),
            (self.playbar.cycleDetectionToggled, self._onCycleDetectionToggled),
            (self.playbar.manualCyclesRequested, self._onManualCyclesRequested),
            (self.playbar.taskTypeChanged, self._onTaskTypeChanged),
            (self.playbar.cycleMarkersVisibilityToggled, self.top.set_cycle_markers_visible),
            (self.playbar.axisFilterCombo.currentTextChanged, self._onAxisFilterChanged),
            (self.labeltree.itemDoubleClicked, self.tree_item_select),
            (self.labeltree.customContextMenuRequested, self._on_tree_context_menu),
        ):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass  # already disconnected

        # Owned by this controller (unlike the shared playbar/labeltree
        # widgets above) — close it rather than leaving a hidden dialog
        # parented to labeltree for every participant that ever used it.
        if self._manualCyclesDialog is not None:
            self._manualCyclesDialog.close()
            self._manualCyclesDialog.deleteLater()
            self._manualCyclesDialog = None

    def update(self):
        if self.playbar.is_playing():
            frames = self.model.kinematic_frames()
            rate = self.model.kinematic_frame_rate()
            self.frame += 1
            if self.frame >= frames:
                self.frame = 0
        self.render.bodyrender.setFrame(self.frame)
        self.notify()

    def on_play_button_clicked(self):
        self.notify()

    def on_prev_frame_button_clicked(self):
        self.frame -= self.step
        if self.frame < 0:
            self.frame = 0
        self.notify()

    def on_next_frame_button_clicked(self):
        self.frame += self.step
        if self.frame >= self.model.kinematic_frames():
            self.frame = self.model.kinematic_frames() - 1
        self.notify()

    def on_combo_box_changed(self):
        print(self.playbar.step.currentText())
        if self.playbar.step.currentText() == "Increment":
            self.step = 1
        else:
            self.step = int(self.playbar.step.currentText())

    def slider_valuechange(self, value):
        self.render.bodyrender.setFrame(value)
        self.playbar.slider.setValue(value)
        self.frame = value
        self.notify()

    def notify(self):
        self.render.notify(self.frame)
        self.playbar.notify(self.frame)
        self.top.update(self.frame)
        # self.bottom.notify(self.frame)

    def tree_item_select(self, index):
        # skip if its a parent item
        if index.parent() == None:
            return
        self._last_tree_item = index
        self.top.clear()
        name = index.text(0)
        do_filter = self.playbar.filterCheck.isChecked()
        if self.model.has_kinematics and name in self.model.kinematic.data.data:
            d = self.model.kinematic.data[name]
            xs, ys, zs = [], [], []
            for p in d:
                xs.append(p.xyz[0])
                ys.append(p.xyz[2])
                zs.append(p.xyz[1])
            fs = self.model.kinematic.point_fs
            if do_filter:
                xs = self._apply_filter(xs, fs, cutoff_hz=6.0, order=2)
                ys = self._apply_filter(ys, fs, cutoff_hz=6.0, order=2)
                zs = self._apply_filter(zs, fs, cutoff_hz=6.0, order=2)
            # x-axis here is frame index, not seconds -- leave rate at its
            # default (1: the playback cursor's update() does frame / rate,
            # and a frame-index axis needs frame / 1 = frame). Event/pick
            # markers still land correctly because PlayPlotWidget._event_x()
            # converts through the trial's kinematic fps (set below via
            # set_kinematic_fps) instead of reusing this rate for that.
            axis_filter = self.playbar.current_axis_filter()  # "All"/"X"/"Y"/"Z"
            for suffix, data in (("x", xs), ("y", ys), ("z", zs)):
                if axis_filter != "All" and axis_filter.lower() != suffix:
                    continue
                self.top.add_line(
                    np.arange(0, len(data)), data, name + "." + suffix, type="marker"
                )

            # Keep the Cycle Detection source picker in sync with whatever
            # the user is already looking at here.
            category = "angle" if name in self.model.kinematic.anglelabels else "marker"
            self.playbar.set_current_source(category, name, axis_filter)
        if name in self.model.emg.Channels:
            # Replay the user's pipeline (DC offset, filters, rectification) on the
            # full raw signal — no crop, no normalization — matching Time Domain analysis.
            y_arr = self.model.emg.get_kinematics_display(name)
            fs_emg = self.model.emg.getfs()
            x = list(np.arange(len(y_arr)) / fs_emg)
            rate = self.model.kinematic_frame_rate()
            self.top.add_line(x, list(y_arr), name, 'channel', rate)
            # Cache for onset detection button
            self._current_emg_channel = name
            self._current_emg_arr = y_arr
            self._current_emg_fs = fs_emg
            # If onset detection is already on, run it for the new channel
            if self.playbar.onsetDetectionButton.isChecked():
                self._run_onset_detection(name, y_arr, fs_emg)
        # Force plate channel click — plot the selected component over time
        if name in self._force_channels:
            fp, attr = self._force_channels[name]
            data = np.asarray(getattr(fp, attr), dtype=float)
            if do_filter:
                data = self._apply_filter(data, fp.fs, cutoff_hz=10.0, order=4)
            x_time = list(np.arange(len(data)) / fp.fs)
            point_fs = self.model.kinematic_frame_rate()
            self.top.add_line(x_time, list(data), name, "channel", point_fs)
            self.playbar.set_current_source("force_plate", "Plate {}".format(fp.plate_id), attr)
        # Re-draw events on the freshly populated plots
        for ev in self.model.events:
            self.top.add_event(ev)

    def _onAxisFilterChanged(self, _text):
        """Re-plot the currently-shown Signals item so switching the Axis
        combo (All/X/Y/Z) takes effect instantly instead of only on the
        next double-click."""
        if self._last_tree_item is not None:
            self.tree_item_select(self._last_tree_item)

    @staticmethod
    def _apply_filter(data, fs, cutoff_hz, order):
        """Zero-phase Butterworth low-pass filter.

        Returns filtered data as a numpy array, or the original data unchanged
        if the signal is too short, the sampling rate is unknown, or the cutoff
        is at or above the Nyquist frequency.

        Args:
            data:       array-like signal samples
            fs:         sampling frequency in Hz
            cutoff_hz:  cutoff frequency in Hz
            order:      filter order
        """
        arr = np.asarray(data, dtype=float)
        if fs <= 0 or len(arr) < 3 * (order + 1):
            return arr
        wn = cutoff_hz / (fs / 2.0)
        if wn >= 1.0:
            return arr  # cutoff at or above Nyquist — nothing to filter
        try:
            b, a = _sig.butter(order, wn, btype='low')
            return _sig.filtfilt(b, a, arr)
        except Exception:
            return arr

    # ------------------------------------------------------------------
    # Event creation / deletion
    # ------------------------------------------------------------------

    def _run_onset_detection(self, chan, y_arr, fs_emg):
        """Run TKE onset detection for *chan*, update events and plot."""
        crop_start = None
        ci = getattr(self.model.profile, 'crop_interval', None)
        if ci is not None:
            crop_start = ci[0]

        try:
            pairs = detect_emg_onsets(y_arr, fs_emg, crop_start_s=crop_start)
        except Exception:
            return

        # Remove previous detection events for this channel before adding new ones
        on_prefix  = "Onset_" + chan
        off_prefix = "Offset_" + chan
        stale = [e for e in self.model.extra_events
                 if e.label.startswith(on_prefix) or e.label.startswith(off_prefix)]
        for e in stale:
            self.model.extra_events.remove(e)
            if e in self.model.events:
                self.model.events.remove(e)

        if not pairs:
            self._refresh_event_tree()
            return

        # Add new detection TrialEvents
        multi = len(pairs) > 1
        for i, (t_on, t_off) in enumerate(pairs):
            suffix = " #{}".format(i + 1) if multi else ""
            on_ev  = TrialEvent(t_on,  "Onset_{}{}" .format(chan, suffix), "Detection")
            off_ev = TrialEvent(t_off, "Offset_{}{}".format(chan, suffix), "Detection")
            self.model.extra_events.append(on_ev)
            self.model.extra_events.append(off_ev)
            self.model.events.append(on_ev)
            self.model.events.append(off_ev)
        self.model.events.sort(key=lambda e: e.time_s)

        self._refresh_event_tree()
        self.top.add_onset_offset(
            [p[0] for p in pairs],
            [p[1] for p in pairs],
        )
        if self._save_callback:
            self._save_callback()

    def _onOnsetDetectionToggled(self, checked):
        if not checked:
            # Turning off — clear lines and remove detection events for current channel
            self.top.clear_onset_offset()
            if self._current_emg_channel is not None:
                prefix_on  = "Onset_"  + self._current_emg_channel
                prefix_off = "Offset_" + self._current_emg_channel
                stale = [e for e in self.model.extra_events
                         if e.label.startswith(prefix_on) or e.label.startswith(prefix_off)]
                for e in stale:
                    self.model.extra_events.remove(e)
                    if e in self.model.events:
                        self.model.events.remove(e)
                if stale:
                    self._refresh_event_tree()
            return

        # Turning on — validate then run
        if self._current_emg_channel is None:
            QMessageBox.warning(
                None,
                self.labeltree.tr("Onset Detection"),
                self.labeltree.tr("Select an EMG channel in the tree first."),
            )
            self.playbar.onsetDetectionButton.setChecked(False)
            return

        if not self.model.emg.is_envelope_configured():
            QMessageBox.warning(
                None,
                self.labeltree.tr("Onset Detection"),
                self.labeltree.tr(
                    "Onset detection requires a linear envelope pipeline.\n\n"
                    "Enable Rectification and a Low-Pass filter in the EMG "
                    "Time Domain configuration, then re-process."
                ),
            )
            self.playbar.onsetDetectionButton.setChecked(False)
            return

        self._run_onset_detection(
            self._current_emg_channel,
            self._current_emg_arr,
            self._current_emg_fs,
        )

    _AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2}

    def _extract_source_signal(self, category, item, axis):
        """Resolve a Cycle Detection source selection (see
        PlayBarWidget.current_source()) into the arrays the detectors need.

        Returns (signal, fs, xyz, fp_vertical, fp_fs):
          - signal: 1D array for the chosen axis/component -- always used.
          - xyz: (n, 3) array, only for marker/angle sources (reach-style
            Lifting/Pointing detectors need the full 3D trajectory, not one
            axis) -- None for a force-plate source.
          - fp_vertical/fp_fs: populated only when the source itself is a
            force plate (also usable as gait's optional GRF fallback).
        All fields are None if the selection can't be resolved (e.g. no
        kinematics, unknown item).
        """
        if category in ("marker", "angle"):
            if not self.model.has_kinematics or item not in self.model.kinematic.data.data:
                return None, None, None, None, None
            pts = self.model.kinematic.data[item]
            axis_idx = self._AXIS_INDEX.get(axis, 2)
            signal = [p.xyz[axis_idx] for p in pts]
            xyz = [p.xyz for p in pts]
            fs = self.model.kinematic.point_fs
            return signal, fs, xyz, None, None

        if category == "force_plate":
            try:
                plate_id = int(item.split()[-1])
                plate = next(p for p in self.model.force_plates if p.plate_id == plate_id)
            except (ValueError, IndexError, StopIteration):
                return None, None, None, None, None
            signal = getattr(plate, axis, plate.Fz)
            return signal, plate.fs, None, signal, plate.fs

        return None, None, None, None, None

    def _run_cycle_detection(self, task_type, method, category, item, axis):
        """Run cycle detection for the given source selection, update events and tree."""
        signal, fs, xyz, fp_vertical, fp_fs = self._extract_source_signal(category, item, axis)
        if signal is None or not fs:
            return

        try:
            if method == "peak":
                pairs = detect_peak_cycles(signal, fs)
            else:
                detector = _CYCLE_DETECTORS.get(task_type)
                if detector is None:
                    return
                pairs = detector(signal, xyz if xyz is not None else signal, fs, fp_vertical, fp_fs)
        except Exception:
            pairs = []

        self._apply_cycle_pairs(task_type, pairs)

    def _apply_cycle_pairs(self, task_type, pairs):
        """Replace task_type's CycleStart_/CycleEnd_ events with *pairs*.

        Shared by auto-detection (_run_cycle_detection) and manual cycle
        entry (_onManualCyclesRequested) -- both just need to produce a
        list[(t_start_s, t_end_s)] and hand it here. pairs=[] clears the
        task's events without adding new ones (used by both "detection
        found nothing" and "user cleared all rows in the manual dialog").
        """
        # Remove this task's previous events before adding new ones
        start_prefix = "CycleStart_" + task_type
        end_prefix = "CycleEnd_" + task_type
        stale = [e for e in self.model.extra_events
                 if e.label.startswith(start_prefix) or e.label.startswith(end_prefix)]
        for e in stale:
            self.model.extra_events.remove(e)
            if e in self.model.events:
                self.model.events.remove(e)
                self.top.remove_event(e)

        if not pairs:
            self._refresh_event_tree()
            if stale and self._save_callback:
                self._save_callback()
            return

        multi = len(pairs) > 1
        for i, (t_start, t_end) in enumerate(pairs):
            suffix = " #{}".format(i + 1) if multi else ""
            start_ev = TrialEvent(t_start, "CycleStart_{}{}".format(task_type, suffix), "Cycle")
            end_ev = TrialEvent(t_end, "CycleEnd_{}{}".format(task_type, suffix), "Cycle")
            self.model.extra_events.append(start_ev)
            self.model.extra_events.append(end_ev)
            self.model.events.append(start_ev)
            self.model.events.append(end_ev)
            self.top.add_event(start_ev)
            self.top.add_event(end_ev)
        self.model.events.sort(key=lambda e: e.time_s)

        self._refresh_event_tree()
        if self._save_callback:
            self._save_callback()

    def _onCycleDetectionToggled(self, checked):
        if not checked:
            # Turning off — remove the detection events for the last-run task
            if self._current_cycle_task is not None:
                start_prefix = "CycleStart_" + self._current_cycle_task
                end_prefix = "CycleEnd_" + self._current_cycle_task
                stale = [e for e in self.model.extra_events
                         if e.label.startswith(start_prefix) or e.label.startswith(end_prefix)]
                for e in stale:
                    self.model.extra_events.remove(e)
                    if e in self.model.events:
                        self.model.events.remove(e)
                        self.top.remove_event(e)
                if stale:
                    self._refresh_event_tree()
                    if self._save_callback:
                        self._save_callback()
            return

        # Turning on — validate then run
        if not self.model.has_kinematics:
            QMessageBox.warning(
                None,
                self.labeltree.tr("Cycle Detection"),
                self.labeltree.tr("This participant has no kinematics/marker data."),
            )
            self.playbar.detectCyclesButton.setChecked(False)
            return

        category, item, axis = self.playbar.current_source()
        if not item:
            QMessageBox.warning(
                None,
                self.labeltree.tr("Cycle Detection"),
                self.labeltree.tr("Select a cycle-detection source first."),
            )
            self.playbar.detectCyclesButton.setChecked(False)
            return

        task_type = self.playbar.current_task_type()
        method = self.playbar.current_detection_method()
        self._current_cycle_task = task_type
        self._run_cycle_detection(task_type, method, category, item, axis)

    def _onTaskTypeChanged(self, task_type):
        """Default the detection-method combo based on whether *task_type*
        has a registered detector -- "Recommended" when it does, "Peak-based"
        (the only method that actually works for it) when it doesn't, e.g. a
        freshly-typed custom task name."""
        self.playbar.set_method_recommendation(task_type in _CYCLE_DETECTORS)

    def _onManualCyclesRequested(self):
        """Open (or re-show) the manual cycle-entry dialog for the selected
        task type.

        Available regardless of has_kinematics -- unlike auto-detection,
        typing in boundaries by hand needs no marker/force-plate data, so
        this is the only way to define reps for an EMG-only participant.
        Pre-fills from that task's existing Cycle* events (if any), so it
        also works as a fine-tune tool (by typed numbers or by picking on
        the plot) after auto-detect.

        Non-modal (.show(), not .exec()): the dialog's "Pick on plot" rows
        need clicks on self.top's plot to reach it, which a modal dialog
        would block. Cached on self so re-clicking the button just re-shows
        the same instance instead of rebuilding it.
        """
        task_type = self.playbar.current_task_type()
        existing = _cycles_from_events(self.model.extra_events, task_type)
        if self._manualCyclesDialog is None:
            self._manualCyclesDialog = ManualCyclesDialog(self.top, parent=self.labeltree)
            self._manualCyclesDialog.accepted.connect(self._onManualCyclesAccepted)
        self._manualCyclesDialog.set_task(task_type, existing, self.model.total_time())
        self._manualCyclesDialog.show()
        self._manualCyclesDialog.raise_()
        self._manualCyclesDialog.activateWindow()

    def _onManualCyclesAccepted(self):
        dlg = self._manualCyclesDialog
        pairs, errors = dlg.get_pairs()
        if errors:
            QMessageBox.warning(
                None,
                self.labeltree.tr("Manual Cycles"),
                self.labeltree.tr("Some rows were invalid and skipped:\n{}")
                .format("\n".join(errors)),
            )

        self._current_cycle_task = dlg.task_type
        self._apply_cycle_pairs(dlg.task_type, pairs)

    def _onExportEvents(self):
        if self._export_events_callback:
            self._export_events_callback()

    def _onMarkEvent(self):
        """Create a new TrialEvent at the current playback position."""
        fps = self.model.kinematic_frame_rate()
        time_s = self.frame / fps

        label, ok = QInputDialog.getText(
            None,
            self.labeltree.tr("Mark Event"),
            self.labeltree.tr("Label (at {:.3f} s):").format(time_s),
            text="Event",
        )
        if not ok or not label.strip():
            return

        ctx, ok2 = QInputDialog.getItem(
            None,
            self.labeltree.tr("Event Context"),
            self.labeltree.tr("Context:"),
            ["General", "Left", "Right"],
            0,
            False,
        )
        if not ok2:
            ctx = "General"

        event = TrialEvent(time_s, label.strip(), ctx)
        self.model.extra_events.append(event)
        self.model.events.append(event)
        self.model.events.sort(key=lambda e: e.time_s)
        self.top.add_event(event)
        self._refresh_event_tree()
        if self._save_callback:
            self._save_callback()

    def _delete_event(self, event):
        """Remove an event from the model and the plot."""
        if event in self.model.extra_events:
            self.model.extra_events.remove(event)
        if event in self.model.events:
            self.model.events.remove(event)
        self.top.remove_event(event)
        self._refresh_event_tree()
        if self._save_callback:
            self._save_callback()

    def _refresh_event_tree(self):
        """Rebuild the 'Events' node inside the participant's tree item."""
        # Remove old node — it is now a child of participant_item, not a top-level item
        if self._event_tree_root is not None:
            parent = self._event_tree_root.parent()
            if parent is not None:
                parent.removeChild(self._event_tree_root)
            else:
                # Fallback: might be top-level if participant_item was None
                idx = self.labeltree.indexOfTopLevelItem(self._event_tree_root)
                if idx >= 0:
                    self.labeltree.takeTopLevelItem(idx)
            self._event_tree_root = None

        if not self.model.events:
            return

        root = QTreeWidgetItem(["Events ({})".format(len(self.model.events))])
        for ev in self.model.events:
            source = "" if ev in self.model.extra_events else " [C3D]"
            child_label = "{} | {} | {:.3f}s{}".format(
                ev.label, ev.context, ev.time_s, source
            )
            QTreeWidgetItem(root, [child_label])

        if self.participant_item is not None:
            self.participant_item.addChild(root)
        else:
            self.labeltree.addTopLevelItem(root)  # graceful fallback

        root.setExpanded(True)
        self._event_tree_root = root

    def _on_tree_context_menu(self, pos):
        """Right-click in label tree: crop and delete actions for events."""
        item = self.labeltree.itemAt(pos)
        if item is None:
            return

        # Right-click on the crop display node → offer Clear
        if item is self._crop_tree_item:
            if self.model.profile.crop_interval is not None:
                menu = QMenu()
                clear_action = menu.addAction("Clear Crop Interval")
                action = menu.exec(self.labeltree.viewport().mapToGlobal(pos))
                if action == clear_action:
                    self.model.profile.crop_interval = None
                    self._refresh_crop_display()
                    if self._save_callback:
                        self._save_callback()
            return

        # Right-click on event items
        if self._event_tree_root is None or item.parent() is not self._event_tree_root:
            return
        idx = self._event_tree_root.indexOfChild(item)
        if idx < 0 or idx >= len(self.model.events):
            return
        event = self.model.events[idx]

        menu = QMenu()
        set_start_action = menu.addAction("Set Crop Start  ({:.3f} s)".format(event.time_s))
        set_end_action = menu.addAction("Set Crop End  ({:.3f} s)".format(event.time_s))
        delete_action = None
        if event in self.model.extra_events:
            menu.addSeparator()
            delete_action = menu.addAction("Delete: {}".format(event.label))

        action = menu.exec(self.labeltree.viewport().mapToGlobal(pos))
        if action is None:
            return
        if action == set_start_action:
            self._set_crop_start(event.time_s)
        elif action == set_end_action:
            self._set_crop_end(event.time_s)
        elif delete_action is not None and action == delete_action:
            self._delete_event(event)

    def _set_crop_start(self, t_start):
        """Set crop start to t_start; keeps existing end or defaults to trial end."""
        existing = self.model.profile.crop_interval
        if existing is not None and existing[1] > t_start:
            t_end = existing[1]
        else:
            t_end = self.model.total_time()
        self.model.profile.crop_interval = (t_start, t_end)
        self._refresh_crop_display()
        if self._save_callback:
            self._save_callback()

    def _set_crop_end(self, t_end):
        """Set crop end to t_end; keeps existing start or defaults to 0."""
        existing = self.model.profile.crop_interval
        if existing is not None and existing[0] < t_end:
            t_start = existing[0]
        else:
            t_start = 0.0
        self.model.profile.crop_interval = (t_start, t_end)
        self._refresh_crop_display()
        if self._save_callback:
            self._save_callback()

    def _refresh_crop_display(self):
        """Add or update the Crop node under the participant tree item."""
        if self._crop_tree_item is not None:
            parent = self._crop_tree_item.parent()
            if parent is not None:
                parent.removeChild(self._crop_tree_item)
            else:
                idx = self.labeltree.indexOfTopLevelItem(self._crop_tree_item)
                if idx >= 0:
                    self.labeltree.takeTopLevelItem(idx)
            self._crop_tree_item = None

        ci = self.model.profile.crop_interval
        if ci is None:
            return

        crop_item = QTreeWidgetItem(["Crop: {:.3f} s → {:.3f} s".format(ci[0], ci[1])])
        if self.participant_item is not None:
            self.participant_item.addChild(crop_item)
        else:
            self.labeltree.addTopLevelItem(crop_item)
        self._crop_tree_item = crop_item

