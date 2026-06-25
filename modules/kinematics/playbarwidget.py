from enum import Enum

from PySide6.QtGui import QMouseEvent, QPainter, QIcon, QAction
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
from PySide6.QtCore import Qt, QPoint, QRect, Signal


class State(Enum):
    STOP = 0
    PLAY = 1
    PAUSE = 2


buttonStyle = """
    QPushButton {
        background-color: #343b48;
        border: 1px solid #495474;
        border-radius: 5px;
        padding: 5px 12px;
        color: #ddd;
        font: bold 10pt "Segoe UI";
        height: 25px;
        min-width: 60px;
    }
    QPushButton:hover {
        background-color: #495474;
        border: 1px solid #bd93f9;
    }
    QPushButton:pressed {
        background-color: #566388;
        padding-top: 6px;
    }
    QPushButton:on {
        background-color: #566388;
        border: 1px solid #bd93f9;
    }
    QPushButton:disabled {
        background-color: #2c3039;
        color: #666;
        border: 1px solid #444;
    }
"""

_stepStyle = (
    "QComboBox {"
    " background-color: #343b48;"
    " color: #ddd;"
    " border: 1px solid #495474;"
    " border-radius: 5px;"
    " padding: 5px 8px;"
    " height: 25px;"
    " min-width: 80px;"
    " font: bold 10pt 'Segoe UI';"
    "}"
    "QComboBox:hover { border: 1px solid #bd93f9; }"
    "QComboBox::drop-down {"
    " subcontrol-origin: padding;"
    " subcontrol-position: top right;"
    " width: 20px;"
    " border-left: 1px solid #495474;"
    " border-top-right-radius: 4px;"
    " border-bottom-right-radius: 4px;"
    "}"
    "QComboBox QAbstractItemView {"
    " background-color: #343b48;"
    " color: #ddd;"
    " border: 1px solid #495474;"
    " selection-background-color: #495474;"
    " selection-color: #f4f4f4;"
    "}"
)


class SliderWidget(QSlider):
    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setMinimumWidth(300)
        self.setMinimum(0)
        self.setMaximum(100)
        self.setValue(0)
        self.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.setTickInterval(10)
        self.setSingleStep(1)
        self.setStyleSheet(
            """
            QSlider {
                padding-left: 0px;
                padding-right: 10px;
                padding-bottom: 20px;
            }
            QSlider::groove:horizontal {
                background: #343b48;
                height: 10px;
                border-radius: 5px;
            }
            QSlider::sub-page:horizontal {
                background: qlineargradient(x1: 0, y1: 0.2, x2: 1, y2: 1,
                    stop: 0 #ea995a, stop: 1 #fb6c05);
                height: 10px;
                border-radius: 5px;
            }
            QSlider::add-page:horizontal {
                background: #343b48;
                height: 10px;
                border-radius: 5px;
            }
            QSlider::handle:horizontal {
                background: #ddd;
                border: 1px solid #888;
                width: 14px;
                height: 14px;
                margin-top: -2px;
                margin-bottom: -2px;
                border-radius: 7px;
            }
            QSlider::handle:horizontal:hover {
                background: #fff;
                border: 1px solid #bd93f9;
            }
            QSlider::sub-page:horizontal:disabled {
                background: #444;
            }
            QSlider::add-page:horizontal:disabled {
                background: #2c3039;
            }
            QSlider::handle:horizontal:disabled {
                background: #555;
                border: 1px solid #444;
            }
            """
        )
        self.left_margin = 0
        self.top_margin = 10
        self.right_margin = 10
        self.bottom_margin = 10
        levels = range(0, 110, 10)
        self.levels = list(zip(levels, map(str, levels)))

    def paintEvent(self, event):
        super().paintEvent(event)
        style = self.style()
        painter = QPainter(self)
        st_slider = QStyleOptionSlider()
        st_slider.initFrom(self)

        length = style.pixelMetric(QStyle.PixelMetric.PM_SliderLength, st_slider, self)
        available = style.pixelMetric(
            QStyle.PixelMetric.PM_SliderSpaceAvailable, st_slider, self
        )

        for v, v_str in self.levels:
            rect = painter.drawText(QRect(), Qt.TextFlag.TextDontPrint, v_str)
            x_loc = (
                QStyle.sliderPositionFromValue(
                    self.minimum(), self.maximum(), v, available
                )
                + length // 2
            )
            left = x_loc - rect.width() - self.left_margin
            bottom = self.rect().bottom()
            if v == self.minimum():
                if left <= 0:
                    self.left_margin = rect.width() // 2 - x_loc
                if self.bottom_margin <= rect.height():
                    self.bottom_margin = rect.height()
                self.setContentsMargins(
                    self.left_margin,
                    self.top_margin,
                    self.right_margin,
                    self.bottom_margin,
                )
            pos = QPoint(left, bottom)
            painter.drawText(pos, v_str)
        return

    def setRange(self, min, max):
        self.setMinimum(min)
        self.setMaximum(max)
        interval = (max - min) // 10
        # Ensure there is at least one interval.
        if interval == 0:
            interval = 1
            
        self.levels = list(
            zip(range(min, max + 1, interval), 
            map(str, range(min, max + 1, interval))))
        self.repaint()

    def mousePressEvent(self, event):
        self.setValue(
            self.minimum()
            + (self.maximum() - self.minimum()) * event.x() / self.width()
        )

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:
        # if mouse is pressed and moving change value of slider
        if ev.buttons() == Qt.LeftButton:
            self.setValue(
                self.minimum()
                + (self.maximum() - self.minimum()) * ev.x() / self.width()
            )

    def get_value(self):
        return self.value()

    def setController(self, controller):
        self.valueChanged.connect(controller.slider_valuechange)


PlayIcon = ":/icons/images/icons/cil-media-play.png"
ForwardIcon = ":/icons/images/icons/cil-media-skip-forward.png"
BackwardIcon = ":/icons/images/icons/cil-media-skip-backward.png"
PauseIcon = ":/icons/images/icons/cil-media-pause.png"


class PlayBarWidget(QWidget):
    eventMarkRequested = Signal()  # emitted when user clicks "Mark Event"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.initui(parent)

    def initui(self, parent=None):
        vboxlayout = QVBoxLayout()
        self.setLayout(vboxlayout)
        self.setStyleSheet("background-color: #2c3039; color: #ddd;")
        self.slider = SliderWidget(parent)
        self.current_frame_label = QLabel(self.tr("Current Frame:"), parent)
        self.current_frame_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.current_frame_label.setStyleSheet("color: #aaa; font-size: 9pt;")
        self.slider.valueChanged.connect(
            lambda x: self.current_frame_label.setText(self.tr("Current Frame: ") + str(x))
        )
        self.button_group = QFrame(parent)
        self.playbutton = QPushButton(self.tr("Play"), self.button_group)
        self.playbutton.clicked.connect(self.on_play_button_clicked)
        self.state = State.STOP
        self.playbutton.setIcon(QIcon(PlayIcon))
        self.playbutton.setCheckable(True)
        self.playbutton.setStyleSheet(buttonStyle)

        self.prevFrameButton = QPushButton(self.tr("Prev"), self.button_group)
        self.prevFrameButton.clicked.connect(self.on_frame_button_clicked)
        self.prevFrameButton.setStyleSheet(buttonStyle)

        self.prevFrameButton.setIcon(QIcon(BackwardIcon))

        self.nextFrameButton = QPushButton(self.tr("Next"), self.button_group)
        self.nextFrameButton.clicked.connect(self.on_frame_button_clicked)
        self.nextFrameButton.setIcon(QIcon(ForwardIcon))
        self.nextFrameButton.setStyleSheet(buttonStyle)

        self.step = QComboBox(self.button_group)
        self.step.addItems([self.tr("Increment"), "5", "10", "20", "50", "100"])
        self.step.setStyleSheet(_stepStyle)

        self.markEventButton = QPushButton(self.tr("Mark Event"), self.button_group)
        self.markEventButton.setStyleSheet(buttonStyle)
        self.markEventButton.clicked.connect(self.eventMarkRequested)

        self.filterCheck = QPushButton(self.tr("Filter"), self.button_group)
        self.filterCheck.setCheckable(True)
        self.filterCheck.setChecked(True)  # filtering on by default
        self.filterCheck.setStyleSheet(buttonStyle)

        hboxlayout = QHBoxLayout(self.button_group)
        hboxlayout.addStretch()
        hboxlayout.addWidget(self.prevFrameButton)
        hboxlayout.addWidget(self.playbutton)
        hboxlayout.addWidget(self.nextFrameButton)
        hboxlayout.addWidget(self.step)
        hboxlayout.addWidget(self.markEventButton)
        hboxlayout.addWidget(self.filterCheck)
        hboxlayout.addStretch()
        vboxlayout.addWidget(self.slider)
        vboxlayout.addWidget(self.current_frame_label)
        vboxlayout.addWidget(self.button_group)

    def on_frame_button_clicked(self):
        if self.state == State.PLAY:
            self.state = State.PAUSE

    def on_play_button_clicked(self):
        if self.state == State.STOP or self.state == State.PAUSE:
            self.state = State.PLAY
            self.playbutton.setIcon(QIcon(PauseIcon))
            self.playbutton.setText(self.tr("Pause"))
        elif self.state == State.PLAY:
            self.state = State.PAUSE
            self.playbutton.setIcon(QIcon(PlayIcon))
            self.playbutton.setText(self.tr("Play"))

    def is_playing(self):
        return self.state == State.PLAY

    def notify(self, frame):
        self.slider.setValue(frame)

    def setController(self, controller):
        self.slider.setController(controller)
