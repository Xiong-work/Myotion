"""
widgets/splash_screen.py — big Myotion logo with a rotating highlight ring,
shown while MainWindow builds its UI (main.py's __main__ block: start()
right after QApplication is constructed, finish() right after
MainWindow(...) returns). finish() pads out the display time to at least
_MIN_SPLASH_S so a fast machine doesn't just flash it for an instant.
"""

import os
import time

from PySide6.QtCore import Qt, QEventLoop, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget, QVBoxLayout

_LOGO_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "myotion_resources", "myotion_logo_origin.png")
)
_HIGHLIGHT_COLOR = "#ffa500"  # same orange as the exit dialog's wave
_RING_DIAMETER = 260
_TICK_MS = 16          # ~60fps ring rotation
_ROTATION_STEP_DEG = 2.5
_MIN_SPLASH_S = 5.0


class _LoadingLogoWidget(QWidget):
    """Big centered logo with two opposite rotating highlight arcs around
    it -- a simple "something is happening" spinner (not a real progress
    percentage, since actual load time isn't tracked/predictable)."""

    def __init__(self, diameter=_RING_DIAMETER, parent=None):
        super().__init__(parent)
        self.setFixedSize(diameter, diameter)
        self._pix = QPixmap(_LOGO_PATH)
        self._angle = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._on_tick)

    def _on_tick(self):
        self._angle = (self._angle + _ROTATION_STEP_DEG) % 360.0
        self.update()

    def start(self):
        self._angle = 0.0
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        ring_rect = self.rect().adjusted(5, 5, -5, -5)
        pen = QPen(QColor(_HIGHLIGHT_COLOR))
        pen.setWidthF(5.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        span = 70 * 16  # QPainter angles are in 1/16th-of-a-degree units
        painter.drawArc(ring_rect, int(-self._angle * 16), span)
        painter.drawArc(ring_rect, int((-self._angle + 180) * 16), span)

        if not self._pix.isNull():
            logo_size = int(self.width() * 0.6)
            scaled = self._pix.scaled(
                logo_size, logo_size,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
            )
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)

        painter.end()


class MyotionSplashScreen(QWidget):
    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.SplashScreen
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setFixedSize(340, 340)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("background-color: transparent;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._logo_widget = _LoadingLogoWidget()
        layout.addWidget(self._logo_widget, alignment=Qt.AlignmentFlag.AlignCenter)

        self._start_time = None
        self._center_on_screen()

    def _center_on_screen(self):
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(
            geo.center().x() - self.width() // 2,
            geo.center().y() - self.height() // 2,
        )

    def start(self):
        """Show the splash and start the spinner. Call once, right before
        the (synchronous, potentially slow) MainWindow construction begins."""
        self.show()
        self._logo_widget.start()
        self._start_time = time.monotonic()

    def finish(self, min_seconds=_MIN_SPLASH_S):
        """Stop the spinner and close the splash, first waiting out
        whatever's left of min_seconds since start() so the splash reads as
        an intentional ~5s opening beat rather than a flash on fast
        hardware. Uses a local QEventLoop (not time.sleep) so the spinner
        keeps animating during the wait."""
        if self._start_time is not None:
            remaining_s = min_seconds - (time.monotonic() - self._start_time)
            if remaining_s > 0:
                loop = QEventLoop()
                QTimer.singleShot(int(remaining_s * 1000), loop.quit)
                loop.exec()
        self._logo_widget.stop()
        self.close()
