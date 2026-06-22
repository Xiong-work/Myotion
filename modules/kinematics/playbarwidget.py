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
    QCheckBox,
)
from PySide6.QtCore import Qt, QPoint, QRect, Signal


class State(Enum):
    STOP = 0
    PLAY = 1
    PAUSE = 2


buttonStyle = """
            QPushButton:hover{
                background: qlineargradient(x1 : 0, y1 : 0, x2 : 0, y2 : 1,
                stop :   0.0 #343b48,
                stop :   0.5 #59667c,
                stop :   0.55 #59667c,
                stop :   1.0 #7585a2);
            }
            QPushButton {
                border: 1px solid #343b48;
                border-radius: 2px;
                padding: 5px 10px 2px 5px;
                color: #fff;
                font: bold large \"Arial\";
                height: 25px;
                width: 80px;
            }
            QPushButton:pressed { background: qlineargradient(x1 : 0, y1 : 0, x2 : 0, y2 : 1,
                stop :   0.0 #424b5b,
                stop :   0.5 #64718a,
                stop :   0.55 #99abca,
                stop :   1.0 #9dafcf);
                padding-top: 6px;
                padding-left: 10px;
            }
            QPushButton:on { 
                background: #545f74
            }
            QPushButton:disabled {
                background: transparent #e5e9ee;
                padding-top: 2px;
                padding-left: 3px;
                color: black;
            }
        """


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
                border: 1px solid #bbb;
                background: white;
                height: 15px;
                border-radius: 8px;
            }
            QSlider::sub-page:horizontal {
     			background: qlineargradient(x1: 0, y1: 0,    x2: 0, y2: 1,
                stop: 0 #fb6c05, stop: 1 #ea995a);
                background: qlineargradient(x1: 0, y1: 0.2, x2: 1, y2: 1,
                stop: 0 #ea995a, stop: 1 #fb6c05);
                border: 1px solid #777;
                height: 15px;
                border-radius: 8px;
				border-top-right-radius:1px;
				border-bottom-right-radius:1px;
            }
            QSlider::add-page:horizontal {
                background: #fff;
                border: 1px solid #777;
                height: 15px;
                border-radius: 8px;
				 border-top-left-radius:1px;
				border-bottom-left-radius:1px;
            }
            QSlider::handle:horizontal {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 #eee, stop:1 #ccc);
                border: 1px solid #777;
                width: 15px;
                margin-top: -2px;
                margin-bottom: -2px;
                border-radius: 8px;
            }
            QSlider::handle:horizontal:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 #fff, stop:1 #ddd);
                border: 1px solid #444;
                border-radius: 8px;
            }
            QSlider::sub-page:horizontal:disabled {
                background: #bbb;
                border-color: #999;
            }
            QSlider::add-page:horizontal:disabled {
                background: #eee;
                border-color: #999;
            }
            QSlider::handle:horizontal:disabled {
                background: #eee;
                border: 1px solid #aaa;
                border-radius: 4px;
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
        self.slider = SliderWidget(parent)
        self.current_frame_label = QLabel(self.tr("Current Frame:"), parent)
        self.current_frame_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
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

        self.markEventButton = QPushButton(self.tr("Mark Event"), self.button_group)
        self.markEventButton.setStyleSheet(buttonStyle)
        self.markEventButton.clicked.connect(self.eventMarkRequested)

        self.filterCheck = QCheckBox(self.tr("Filter"), self.button_group)
        self.filterCheck.setChecked(True)  # filtering on by default

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
