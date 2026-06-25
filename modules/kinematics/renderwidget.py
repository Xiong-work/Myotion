import os as _os

from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout, QBoxLayout
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap

from .bodyrender import BodyRender

_LOGO_PATH = _os.path.normpath(
    _os.path.join(_os.path.dirname(__file__), "..", "..", "myotion_resources", "fulllogo.png")
)


class RenderWidget(QWidget):
    """a top widget for bodyrender and placeholder
    if there is no model set for render it should show placeholder
    otherwise, render the model in bodyrender and set it as central widget
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.bodyrender = BodyRender()

        # Placeholder: show branded logo instead of plain text
        self.placeholder = QLabel()
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setStyleSheet("background-color: #12141a;")
        _pix = QPixmap(_LOGO_PATH)
        if not _pix.isNull():
            self.placeholder.setPixmap(
                _pix.scaled(
                    320, 130,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self.placeholder.setText("No model to render")

        self.vblayout = QVBoxLayout()
        self.vblayout.setContentsMargins(0, 0, 0, 0)
        self.vblayout.addWidget(
            self.placeholder, alignment=Qt.AlignmentFlag.AlignCenter
        )
        self.setLayout(self.vblayout)

    def update(self):
        if self.bodyrender.model:
            self.vblayout.removeWidget(self.placeholder)
            self.vblayout.addWidget(self.bodyrender)
        else:
            self.vblayout.removeWidget(self.bodyrender)
            self.vblayout.addWidget(self.placeholder)

    def setModel(self, model):
        self.bodyrender.setModel(model)
        self.update()

    def setController(self, controller):
        self.controller = controller

    def notify(self, frame):
        self.bodyrender.setFrame(frame)
