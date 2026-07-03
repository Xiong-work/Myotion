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
        self.taskTypeCombo = QComboBox()
        self.taskTypeCombo.addItems([
            self.tr("Gait"),
            self.tr("Running"),
            self.tr("Sit-to-Stand"),
            self.tr("Squat"),
            self.tr("Trunk Flexion/Extension"),
            self.tr("Lifting"),
            self.tr("Pointing"),
        ])
        self.taskTypeCombo.setStyleSheet(_stepStyle)
        self.taskTypeCombo.setToolTip(self.tr("Functional task type"))

        self.sourceMarkerCombo = QComboBox()
        self.sourceMarkerCombo.setStyleSheet(_stepStyle)
        self.sourceMarkerCombo.setToolTip(self.tr("Kinematics source marker for cycle detection"))
        self.sourceMarkerCombo.setMinimumWidth(110)

        self.detectCyclesButton = QPushButton(self.tr("Detect Cycles"))
        self.detectCyclesButton.setCheckable(True)
        self.detectCyclesButton.setChecked(False)
        self.detectCyclesButton.setStyleSheet(buttonStyle)
        self.detectCyclesButton.setToolTip(self.tr("Detect repetition cycles from the selected marker"))
        self.detectCyclesButton.clicked.connect(
            lambda checked: self.cycleDetectionToggled.emit(checked)
        )

        hbox.addWidget(self.prevFrameButton)
        hbox.addWidget(self.playbutton)
        hbox.addWidget(self.nextFrameButton)
        hbox.addWidget(self.current_frame_label)
        hbox.addWidget(self.step)
        hbox.addStretch()
        hbox.addWidget(self.filterCheck)
        hbox.addWidget(self.markEventButton)
        hbox.addWidget(self.exportEventsButton)
        hbox.addWidget(self.onsetDetectionButton)
        hbox.addWidget(self.taskTypeCombo)
        hbox.addWidget(self.sourceMarkerCombo)
        hbox.addWidget(self.detectCyclesButton)

        vbox.addWidget(self.slider)
        vbox.addWidget(ctrl)

    # ── Public API ────────────────────────────────────────────────────────

    def set_frame_rate(self, fps: float):
        """Set the frame rate used to compute time-in-seconds display."""
        self._fps = fps

    def enable_playback(self):
        """Enable all playback controls — call once kinematics data is loaded."""
        self._set_controls_enabled(True)

    def set_marker_options(self, labels):
        """Populate the kinematics-source marker combo. Call once per participant load."""
        self.sourceMarkerCombo.blockSignals(True)
        self.sourceMarkerCombo.clear()
        self.sourceMarkerCombo.addItems(list(labels))
        self.sourceMarkerCombo.blockSignals(False)

    def current_task_type(self):
        return self.taskTypeCombo.currentText()

    def current_source_marker(self):
        return self.sourceMarkerCombo.currentText()

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
