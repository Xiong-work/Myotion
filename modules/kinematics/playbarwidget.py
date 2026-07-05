from enum import Enum

from PySide6.QtGui import QMouseEvent, QPainter, QColor, QPen, QFont, QPalette
from PySide6.QtWidgets import (
    QSlider,
    QPushButton,
    QStyle,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QStyleOptionSlider,
    QLabel,
    QFrame,
    QComboBox,
    QCompleter,
    QDialog,
    QFormLayout,
)
from PySide6.QtCore import Qt, Signal


class State(Enum):
    STOP = 0
    PLAY = 1
    PAUSE = 2


# ── Mokka-inspired light theme ────────────────────────────────────────────────
_BG        = "#f5f6f7"
_GROOVE    = "#c8c8c8"
_FILLED    = "#93b8e0"
_CURSOR    = "#1a73e8"
_TICK      = "#888888"
_LABEL_CLR = "#444444"

buttonStyle = """
    QPushButton {
        background-color: #f0f1f2;
        border: 1px solid #c0c0c0;
        border-radius: 4px;
        padding: 2px 10px;
        color: #333333;
        font: 9pt "Segoe UI";
        height: 26px;
        min-width: 48px;
    }
    QPushButton:hover {
        background-color: #dceaf8;
        border: 1px solid #1a73e8;
        color: #1a73e8;
    }
    QPushButton:pressed {
        background-color: #c8dff5;
    }
    QPushButton:on {
        background-color: #c8dff5;
        border: 1px solid #1a73e8;
        color: #1a73e8;
    }
    QPushButton:disabled {
        background-color: #e8e8e8;
        color: #aaaaaa;
        border: 1px solid #d0d0d0;
    }
"""

_stepStyle = """
    QComboBox {
        background-color: #f0f1f2;
        color: #333333;
        border: 1px solid #c0c0c0;
        border-radius: 4px;
        padding: 2px 8px;
        height: 26px;
        min-width: 70px;
        font: 9pt "Segoe UI";
    }
    QComboBox:hover { border: 1px solid #1a73e8; }
    QComboBox:disabled { background-color: #e8e8e8; color: #aaa; }
    QComboBox::drop-down {
        subcontrol-origin: padding;
        subcontrol-position: top right;
        width: 18px;
        border-left: 1px solid #c0c0c0;
        border-top-right-radius: 4px;
        border-bottom-right-radius: 4px;
    }
    QComboBox QAbstractItemView {
        background-color: #ffffff;
        color: #333333;
        border: 1px solid #c0c0c0;
        selection-background-color: #dceaf8;
        selection-color: #1a73e8;
    }
"""

_SLIDER_SS = """
    QSlider {
        background-color: #f5f6f7;
        padding-left:   4px;
        padding-right:  4px;
        padding-top:    8px;
        padding-bottom: 32px;
    }
    QSlider::groove:horizontal {
        background: #c8c8c8;
        height: 2px;
        border-radius: 1px;
    }
    QSlider::sub-page:horizontal {
        background: #93b8e0;
        height: 2px;
        border-radius: 1px;
    }
    QSlider::add-page:horizontal {
        background: #c8c8c8;
        height: 2px;
        border-radius: 1px;
    }
    QSlider::handle:horizontal {
        background: #1a73e8;
        border: none;
        width: 3px;
        height: 24px;
        margin-top:    -11px;
        margin-bottom: -11px;
        border-radius: 1px;
    }
    QSlider::handle:horizontal:hover {
        background: #0d5bbf;
    }
    QSlider::handle:horizontal:disabled {
        background: #b8b8b8;
    }
"""


class SliderWidget(QSlider):
    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setMinimumWidth(300)
        self.setMinimum(0)
        self.setMaximum(100)
        self.setValue(0)
        self.setTickPosition(QSlider.TickPosition.NoTicks)
        self.setSingleStep(1)
        self.setStyleSheet(_SLIDER_SS)
        self.levels = list(zip(range(0, 110, 10), map(str, range(0, 110, 10))))

    def paintEvent(self, event):
        super().paintEvent(event)

        style  = self.style()
        opt    = QStyleOptionSlider()
        opt.initFrom(self)

        handle_len = style.pixelMetric(QStyle.PixelMetric.PM_SliderLength, opt, self)
        available  = style.pixelMetric(QStyle.PixelMetric.PM_SliderSpaceAvailable, opt, self)

        groove = style.subControlRect(
            QStyle.ComplexControl.CC_Slider, opt,
            QStyle.SubControl.SC_SliderGroove, self,
        )
        groove_cy = groove.center().y()
        tick_y0   = groove_cy + 5
        tick_y1   = tick_y0 + 7

        painter = QPainter(self)
        font = QFont("Segoe UI", 8)
        painter.setFont(font)
        fm = painter.fontMetrics()
        label_y = tick_y1 + fm.ascent() + 3

        for v, v_str in self.levels:
            x = (
                QStyle.sliderPositionFromValue(
                    self.minimum(), self.maximum(), v, available
                )
                + handle_len // 2
            )

            painter.setPen(QPen(QColor(_TICK), 1))
            painter.drawLine(x, tick_y0, x, tick_y1)

            text_w  = fm.horizontalAdvance(v_str)
            label_x = max(0, min(x - text_w // 2, self.width() - text_w))
            painter.setPen(QPen(QColor(_LABEL_CLR)))
            painter.drawText(label_x, label_y, v_str)

        painter.end()

    def setRange(self, min_val, max_val):
        self.setMinimum(min_val)
        self.setMaximum(max_val)
        interval = (max_val - min_val) // 10
        if interval == 0:
            interval = 1
        self.levels = list(
            zip(
                range(min_val, max_val + 1, interval),
                map(str, range(min_val, max_val + 1, interval)),
            )
        )
        self.repaint()

    def mousePressEvent(self, event):
        self.setValue(
            self.minimum()
            + int((self.maximum() - self.minimum()) * event.position().x() / self.width())
        )

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:
        if ev.buttons() == Qt.MouseButton.LeftButton:
            self.setValue(
                self.minimum()
                + int((self.maximum() - self.minimum()) * ev.position().x() / self.width())
            )

    def get_value(self):
        return self.value()

    def setController(self, controller):
        self.valueChanged.connect(controller.slider_valuechange)


class PlayBarWidget(QWidget):
    eventMarkRequested    = Signal()
    exportEventsRequested = Signal()
    onsetDetectionToggled = Signal(bool)
    cycleDetectionToggled = Signal(bool)
    manualCyclesRequested = Signal()
    taskTypeChanged       = Signal(str)
    cycleMarkersVisibilityToggled = Signal(bool)
    clearPlotRequested    = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fps = 0.0
        self.initui(parent)
        self._set_controls_enabled(False)

    def initui(self, parent=None):
        # Persistent light background — survives any parent setStyleSheet() calls
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(_BG))
        self.setPalette(pal)
        self.setAutoFillBackground(True)

        self.setMinimumHeight(110)

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(6, 6, 6, 4)
        vbox.setSpacing(4)

        # ── Ruler / timeline ──────────────────────────────────────────────
        self.slider = SliderWidget(parent)
        self.slider.valueChanged.connect(self._on_frame_changed)

        # ── Controls row ──────────────────────────────────────────────────
        ctrl = QFrame()
        ctrl.setStyleSheet(f"background-color: {_BG};")
        hbox = QHBoxLayout(ctrl)
        hbox.setContentsMargins(0, 0, 0, 0)
        hbox.setSpacing(6)

        _transport_ss = (
            buttonStyle
            + "QPushButton { min-width: 34px; max-width: 42px; font: 12pt 'Segoe UI'; }"
        )

        self.prevFrameButton = QPushButton("◀")
        self.prevFrameButton.setToolTip(self.tr("Previous frame"))
        self.prevFrameButton.setStyleSheet(_transport_ss)
        self.prevFrameButton.clicked.connect(self.on_frame_button_clicked)

        self.playbutton = QPushButton("▶")
        self.playbutton.setToolTip(self.tr("Play / Pause"))
        self.playbutton.setCheckable(True)
        self.playbutton.setStyleSheet(_transport_ss)
        self.playbutton.clicked.connect(self.on_play_button_clicked)
        self.state = State.STOP

        self.nextFrameButton = QPushButton("▶▶")
        self.nextFrameButton.setToolTip(self.tr("Next frame"))
        self.nextFrameButton.setStyleSheet(_transport_ss)
        self.nextFrameButton.clicked.connect(self.on_frame_button_clicked)

        self.current_frame_label = QLabel("Frame: 0   Time: 0.000 s")
        self.current_frame_label.setStyleSheet(
            f"background-color: {_BG}; color: #222222;"
            " font: 9pt 'Segoe UI'; min-width: 180px;"
        )
        self.current_frame_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.step = QComboBox()
        self.step.addItems([self.tr("Increment"), "5", "10", "20", "50", "100"])
        self.step.setStyleSheet(_stepStyle)
        self.step.setToolTip(self.tr("Step size"))

        self.filterCheck = QPushButton(self.tr("Filter"))
        self.filterCheck.setCheckable(True)
        self.filterCheck.setChecked(True)
        self.filterCheck.setStyleSheet(buttonStyle)

        # Markers/Angles have X/Y/Z components plotted as 3 subplots by
        # default -- "All" keeps that, or restrict to just one axis for
        # easier visual inspection (e.g. only the vertical component).
        # Has no effect on EMG channels or force-plate components, which are
        # already single-axis per tree row.
        self.axisFilterCombo = QComboBox()
        self.axisFilterCombo.addItems(["All", "X", "Y", "Z"])
        self.axisFilterCombo.setStyleSheet(_stepStyle)
        self.axisFilterCombo.setToolTip(
            self.tr("Which axis to plot for Markers/Angles (Signals panel)")
        )

        # Layer newly-selected signals on top of the current plot (same kind
        # only -- see Controller.tree_item_select / PlayPlotWidget.can_overlay)
        # instead of replacing it.
        self.overlayCheck = QPushButton(self.tr("Overlay"))
        self.overlayCheck.setCheckable(True)
        self.overlayCheck.setChecked(False)
        self.overlayCheck.setStyleSheet(buttonStyle)
        self.overlayCheck.setToolTip(
            self.tr(
                "Layer newly selected signals on top of the current plot "
                "(same kind only) instead of replacing it"
            )
        )

        self.clearPlotButton = QPushButton(self.tr("Clear Plot"))
        self.clearPlotButton.setStyleSheet(buttonStyle)
        self.clearPlotButton.setToolTip(self.tr("Clear all traces from the signal plot"))
        self.clearPlotButton.clicked.connect(self.clearPlotRequested)

        self.markEventButton = QPushButton(self.tr("Mark Event"))
        self.markEventButton.setStyleSheet(buttonStyle)
        self.markEventButton.clicked.connect(self.eventMarkRequested)

        self.exportEventsButton = QPushButton(self.tr("Export Events"))
        self.exportEventsButton.setStyleSheet(buttonStyle)
        self.exportEventsButton.clicked.connect(self.exportEventsRequested)

        self.onsetDetectionButton = QPushButton(self.tr("Onset"))
        self.onsetDetectionButton.setCheckable(True)
        self.onsetDetectionButton.setChecked(False)
        self.onsetDetectionButton.setStyleSheet(buttonStyle)
        self.onsetDetectionButton.setToolTip(self.tr("Onset Detection"))
        self.onsetDetectionButton.clicked.connect(
            lambda checked: self.onsetDetectionToggled.emit(checked)
        )

        # ── Task-type cycle detection ───────────────────────────────────────
        _task_types = [
            self.tr("Gait"),
            self.tr("Running"),
            self.tr("Sit-to-Stand"),
            self.tr("Squat"),
            self.tr("CMJ"),
            self.tr("Trunk Flexion/Extension"),
            self.tr("Lifting"),
            self.tr("Pointing"),
        ]
        self.taskTypeCombo = QComboBox()
        self.taskTypeCombo.addItems(_task_types)
        # Editable so a task without a built-in detector can still be typed
        # in by name -- "Detect Cycles" only works for the built-in types
        # (see controller.py's _CYCLE_DETECTORS), but "Manual Cycles…" works
        # for any task_type, so a custom name isn't a dead end.
        self.taskTypeCombo.setEditable(True)
        # A typed custom name is kept in the dropdown afterward (this session),
        # not just accepted ad hoc -- so naming one is a real, reusable option.
        self.taskTypeCombo.setInsertPolicy(QComboBox.InsertPolicy.InsertAtBottom)
        _task_type_completer = QCompleter(_task_types, self.taskTypeCombo)
        _task_type_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        _task_type_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.taskTypeCombo.setCompleter(_task_type_completer)
        self.taskTypeCombo.setStyleSheet(_stepStyle)
        self.taskTypeCombo.setToolTip(
            self.tr("Functional task type (type a custom name if not listed)")
        )
        self.taskTypeCombo.currentTextChanged.connect(self.taskTypeChanged)

        # ── Leveled source selection: category (Markers/Angles/Force Plates)
        # -> item within that category -> axis/component. Replaces a flat
        # marker-only combo -- cycle detection can run off a marker axis, a
        # model-output Angle, or a force-plate component (Fz by default,
        # e.g. vertical GRF for jump/gait-style tasks).
        self._source_items = {"Markers": [], "Angles": [], "Force Plates": []}
        self._source_axes = {
            "Markers": ["X", "Y", "Z"],
            "Angles": ["X", "Y", "Z"],
            "Force Plates": ["Fx", "Fy", "Fz"],
        }
        self._source_default_axis = {"Markers": "Z", "Angles": "X", "Force Plates": "Fz"}

        self.sourceCategoryCombo = QComboBox()
        self.sourceCategoryCombo.addItems(["Markers", "Angles", "Force Plates"])
        self.sourceCategoryCombo.setStyleSheet(_stepStyle)
        self.sourceCategoryCombo.setToolTip(self.tr("Kind of signal to detect cycles from"))
        self.sourceCategoryCombo.currentTextChanged.connect(self._onSourceCategoryChanged)

        self.sourceItemCombo = QComboBox()
        self.sourceItemCombo.setStyleSheet(_stepStyle)
        self.sourceItemCombo.setToolTip(self.tr("Specific marker / angle / force plate"))
        self.sourceItemCombo.setMinimumWidth(130)

        self.sourceAxisCombo = QComboBox()
        self.sourceAxisCombo.addItems(self._source_axes["Markers"])
        self.sourceAxisCombo.setStyleSheet(_stepStyle)
        self.sourceAxisCombo.setToolTip(self.tr("Axis / component (vertical Fz is the usual default)"))

        self.methodCombo = QComboBox()
        self.methodCombo.addItems([self.tr("Recommended"), self.tr("Peak-based (generic)")])
        self.methodCombo.setStyleSheet(_stepStyle)
        self.methodCombo.setToolTip(
            self.tr(
                "Recommended = task-specific detector (falls back to Peak-based "
                "for a task with no built-in detector). Peak-based works for "
                "any task/source but is a simpler, less noise-robust method."
            )
        )

        self.detectCyclesButton = QPushButton(self.tr("Detect Cycles"))
        self.detectCyclesButton.setCheckable(True)
        self.detectCyclesButton.setChecked(False)
        self.detectCyclesButton.setStyleSheet(buttonStyle)
        self.detectCyclesButton.setToolTip(self.tr("Detect repetition cycles from the selected marker"))
        self.detectCyclesButton.clicked.connect(
            lambda checked: self.cycleDetectionToggled.emit(checked)
        )

        # Manual cycle entry — always enabled, unlike Detect Cycles which
        # needs kinematics + a marker. Covers EMG-only trials where the user
        # has external notes for the rep boundaries and no marker to detect from.
        self.manualCyclesButton = QPushButton(self.tr("Manual Cycles…"))
        self.manualCyclesButton.setStyleSheet(buttonStyle)
        self.manualCyclesButton.setToolTip(
            self.tr("Type in repetition boundaries by hand (works without kinematics)")
        )
        self.manualCyclesButton.clicked.connect(self.manualCyclesRequested)

        # Show/hide the CycleStart_/CycleEnd_ markers already drawn on the
        # plot without deleting them -- lets the user declutter the view
        # while still keeping the detected/typed cycles around.
        self.showCycleMarkersButton = QPushButton(self.tr("Show Cycles"))
        self.showCycleMarkersButton.setCheckable(True)
        self.showCycleMarkersButton.setChecked(True)
        self.showCycleMarkersButton.setStyleSheet(buttonStyle)
        self.showCycleMarkersButton.setToolTip(
            self.tr("Show/hide cycle start (green) / end (red) markers on the plot")
        )
        self.showCycleMarkersButton.clicked.connect(self.cycleMarkersVisibilityToggled)

        # Single button on the main row -- opens a popup housing the 4
        # widgets above instead of crowding the toolbar with all of them.
        self.cycleDetectionButton = QPushButton(self.tr("Cycle Detection…"))
        self.cycleDetectionButton.setStyleSheet(buttonStyle)
        self.cycleDetectionButton.setToolTip(
            self.tr("Task type, source marker, and cycle detection")
        )
        self.cycleDetectionButton.clicked.connect(self._openCycleDetectionDialog)
        self._cycleDialog = None  # built lazily on first click

        hbox.addWidget(self.prevFrameButton)
        hbox.addWidget(self.playbutton)
        hbox.addWidget(self.nextFrameButton)
        hbox.addWidget(self.current_frame_label)
        hbox.addWidget(self.step)
        hbox.addStretch()
        hbox.addWidget(self.filterCheck)
        hbox.addWidget(self.axisFilterCombo)
        hbox.addWidget(self.overlayCheck)
        hbox.addWidget(self.clearPlotButton)
        hbox.addWidget(self.markEventButton)
        hbox.addWidget(self.exportEventsButton)
        hbox.addWidget(self.onsetDetectionButton)
        hbox.addWidget(self.cycleDetectionButton)

        vbox.addWidget(self.slider)
        vbox.addWidget(ctrl)

    def _openCycleDetectionDialog(self):
        """Lazily build (once), then show/raise, the Cycle Detection popup --
        houses the task type / leveled source selection / detection method /
        Detect Cycles / Manual Cycles controls. Non-modal (.show(), not
        .exec()) so the timeline stays interactive while trying different
        sources/task types."""
        if self._cycleDialog is None:
            dlg = QDialog(self)
            dlg.setWindowTitle(self.tr("Cycle Detection"))
            form = QFormLayout(dlg)
            form.addRow(self.tr("Task type:"), self.taskTypeCombo)

            source_row = QHBoxLayout()
            source_row.addWidget(self.sourceCategoryCombo)
            source_row.addWidget(self.sourceItemCombo)
            source_row.addWidget(self.sourceAxisCombo)
            form.addRow(self.tr("Source:"), source_row)

            form.addRow(self.tr("Detection method:"), self.methodCombo)

            btn_row = QHBoxLayout()
            btn_row.addWidget(self.detectCyclesButton)
            btn_row.addWidget(self.manualCyclesButton)
            btn_row.addWidget(self.showCycleMarkersButton)
            form.addRow(btn_row)
            self._cycleDialog = dlg
        self._cycleDialog.show()
        self._cycleDialog.raise_()
        self._cycleDialog.activateWindow()

    def _onSourceCategoryChanged(self, category):
        """Repopulate the item/axis combos for the newly-selected category
        (Markers/Angles/Force Plates), defaulting the axis to the sensible
        choice for that category (e.g. Fz for Force Plates)."""
        self.sourceItemCombo.blockSignals(True)
        self.sourceItemCombo.clear()
        self.sourceItemCombo.addItems(self._source_items.get(category, []))
        self.sourceItemCombo.blockSignals(False)

        self.sourceAxisCombo.blockSignals(True)
        self.sourceAxisCombo.clear()
        self.sourceAxisCombo.addItems(self._source_axes.get(category, []))
        default_axis = self._source_default_axis.get(category)
        if default_axis:
            idx = self.sourceAxisCombo.findText(default_axis)
            if idx >= 0:
                self.sourceAxisCombo.setCurrentIndex(idx)
        self.sourceAxisCombo.blockSignals(False)

    # ── Public API ────────────────────────────────────────────────────────

    def set_frame_rate(self, fps: float):
        """Set the frame rate used to compute time-in-seconds display."""
        self._fps = fps

    def enable_playback(self):
        """Enable all playback controls — call once kinematics data is loaded."""
        self._set_controls_enabled(True)

    def set_source_options(self, marker_labels, angle_labels, plate_ids):
        """Populate the leveled source picker. Call once per participant load.

        marker_labels: real marker names (kinematic.reallabels).
        angle_labels: model-output Angle names (kinematic.anglelabels).
        plate_ids: list of force plate IDs (fp.plate_id, in display order) --
            IDs rather than a bare count so the combo matches the same
            "Plate N" numbering used elsewhere (bodyrender.py, the
            participant tree), even if IDs aren't sequential from 1.
        """
        self._source_items["Markers"] = list(marker_labels)
        self._source_items["Angles"] = list(angle_labels)
        self._source_items["Force Plates"] = ["Plate {}".format(pid) for pid in plate_ids]
        # Refresh whichever category is currently selected
        self._onSourceCategoryChanged(self.sourceCategoryCombo.currentText())

    def current_task_type(self):
        return self.taskTypeCombo.currentText()

    def is_overlay_enabled(self):
        """True if newly-plotted signals should be layered on the current
        plot (same kind only) instead of replacing it."""
        return self.overlayCheck.isChecked()

    def current_axis_filter(self):
        """Return "All"/"X"/"Y"/"Z" -- which axis to plot for a Marker/Angle
        double-clicked in the Signals tree (see controller.tree_item_select)."""
        return self.axisFilterCombo.currentText()

    def set_current_source(self, category, item, axis=None):
        """Sync the Cycle Detection source picker to match a signal the user
        just plotted in the Signals panel -- so auto-detection defaults to
        "whatever I'm already looking at" instead of needing a separate,
        easy-to-forget reselection. Called by the controller after every
        Signals-tree plot (marker/angle/force-plate row).

        category: "marker" / "angle" / "force_plate".
        item: marker/angle name, or a "Plate N" label.
        axis: "X"/"Y"/"Z" or "Fx"/"Fy"/"Fz" -- None/"All" leaves the axis at
            that category's default (set by _onSourceCategoryChanged).
        """
        cat_label = {
            "marker": "Markers", "angle": "Angles", "force_plate": "Force Plates",
        }.get(category)
        if cat_label is None:
            return
        if self.sourceCategoryCombo.currentText() != cat_label:
            self.sourceCategoryCombo.setCurrentText(cat_label)  # fires _onSourceCategoryChanged
        else:
            self._onSourceCategoryChanged(cat_label)

        idx = self.sourceItemCombo.findText(item)
        if idx >= 0:
            self.sourceItemCombo.setCurrentIndex(idx)

        if axis and axis != "All":
            idx = self.sourceAxisCombo.findText(axis)
            if idx >= 0:
                self.sourceAxisCombo.setCurrentIndex(idx)

    def current_source(self):
        """Return (category, item, axis) for the current Cycle Detection
        source selection -- category is "marker" / "angle" / "force_plate";
        item is the marker/angle name or a "Plate N" label; axis is "X"/"Y"/"Z"
        for markers/angles or "Fx"/"Fy"/"Fz" for a force plate."""
        category = {
            "Markers": "marker", "Angles": "angle", "Force Plates": "force_plate",
        }.get(self.sourceCategoryCombo.currentText(), "marker")
        return category, self.sourceItemCombo.currentText(), self.sourceAxisCombo.currentText()

    def current_detection_method(self):
        """Return "recommended" (task-specific detector) or "peak" (generic,
        works for any task/source)."""
        return "peak" if self.methodCombo.currentIndex() == 1 else "recommended"

    def set_method_recommendation(self, has_dedicated_detector: bool):
        """Default the method combo based on whether the current task type
        has a registered detector -- "Recommended" when it does, "Peak-based"
        (the only thing that actually works) when it doesn't. Called by the
        controller in response to taskTypeChanged; the user can still
        override manually afterward."""
        self.methodCombo.setCurrentIndex(0 if has_dedicated_detector else 1)

    # ── Internal helpers ──────────────────────────────────────────────────

    def _set_controls_enabled(self, enabled: bool):
        self.slider.setEnabled(enabled)
        self.prevFrameButton.setEnabled(enabled)
        self.playbutton.setEnabled(enabled)
        self.nextFrameButton.setEnabled(enabled)

    def _on_frame_changed(self, frame: int):
        t = frame / self._fps if self._fps > 0 else 0.0
        self.current_frame_label.setText(f"Frame: {frame}   Time: {t:.3f} s")

    # ── Playback state ────────────────────────────────────────────────────

    def on_frame_button_clicked(self):
        if self.state == State.PLAY:
            self.state = State.PAUSE

    def on_play_button_clicked(self):
        if self.state in (State.STOP, State.PAUSE):
            self.state = State.PLAY
            self.playbutton.setText("⏸")
        else:
            self.state = State.PAUSE
            self.playbutton.setText("▶")

    def is_playing(self):
        return self.state == State.PLAY

    def notify(self, frame):
        self.slider.setValue(frame)

    def setController(self, controller):
        self.slider.setController(controller)
