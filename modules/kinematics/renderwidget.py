import os as _os

from PySide6.QtWidgets import QWidget, QLabel, QStackedLayout
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QPainter

from .bodyrender import BodyRender

_LOGO_PATH = _os.path.normpath(
    _os.path.join(_os.path.dirname(__file__), "..", "..", "myotion_resources", "myotion_logo_origin.png")
)

# Placeholder watermark should read as a faint mark, not compete with "no data" messaging
_PLACEHOLDER_LOGO_OPACITY = 0.12


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
        self.placeholder.setStyleSheet("background-color: #ffffff;")
        _pix = QPixmap(_LOGO_PATH)
        if not _pix.isNull():
            _scaled = _pix.scaled(
                220, 220,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            # Bake the faint look into the pixmap itself rather than a
            # QGraphicsOpacityEffect on the whole label -- that effect dims
            # everything the label paints, including its white background
            # stylesheet, which read as near-black once composited over the
            # dark ancestor frames (kinematics_left_top / kinematics_render).
            _faded = QPixmap(_scaled.size())
            _faded.fill(Qt.GlobalColor.transparent)
            _painter = QPainter(_faded)
            _painter.setOpacity(_PLACEHOLDER_LOGO_OPACITY)
            _painter.drawPixmap(0, 0, _scaled)
            _painter.end()
            self.placeholder.setPixmap(_faded)
        else:
            self.placeholder.setText("No model to render")

        # Both widgets live in this stack from construction -- switching which
        # one is visible via setCurrentWidget() avoids repeatedly reparenting
        # bodyrender (a QOpenGLWidget) in and out of the layout on every
        # setModel() call. Reparenting a native OpenGL surface into an
        # already-shown top-level window is what caused the whole window to
        # visibly flicker/redraw on the first file load (e.g. Gait Analysis,
        # opened maximized before any model is set).
        self.stack = QStackedLayout()
        self.stack.setContentsMargins(0, 0, 0, 0)
        # No AlignCenter override here: let the label stretch to fill the
        # available area so its white background actually shows, instead of
        # sizing down to just the pixmap and exposing the dark parent frame
        # around it. The label's own setAlignment(AlignCenter) above still
        # keeps the logo centered within that filled area.
        self.stack.addWidget(self.placeholder)
        self.stack.addWidget(self.bodyrender)
        self.setLayout(self.stack)

    def update(self):
        if self.bodyrender.model:
            self.stack.setCurrentWidget(self.bodyrender)
        else:
            self.stack.setCurrentWidget(self.placeholder)

    def setModel(self, model):
        self.bodyrender.setModel(model)
        self.update()

    def setController(self, controller):
        self.controller = controller

    def notify(self, frame):
        self.bodyrender.setFrame(frame)
