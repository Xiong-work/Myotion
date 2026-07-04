"""
widgets/exit_confirm_dialog.py — "are you sure you want to exit?" prompt
shown from MainWindow.closeEvent(), with a short (~1s, plays once) irregular
signal-wave sweep under the Myotion logo instead of a static icon.
"""

import os
import random

from PySide6.QtCore import Qt, QEasingCurve, QVariantAnimation
from PySide6.QtGui import QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QWidget, QDialogButtonBox, QSizePolicy,
)

_LOGO_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "myotion_resources", "myotion_logo_origin.png")
)
_WAVE_COLOR = "#ffa500"  # orange -- matches the app's existing event-marker orange
_SWEEP_MS = 900
_WAVE_SEED = 7  # fixed seed -- same irregular shape every time, not fresh noise per repaint


class SignalWaveWidget(QWidget):
    """Draws an irregular, EMG-burst-like waveform (a smoothed random walk,
    not a clean sine -- an on-brand nod to the app's own signal plots),
    revealed left-to-right once per play(). Stops on its own, so it costs
    nothing while the dialog just sits there waiting for a click."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(56)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._progress = 0.0
        self._path = QPainterPath()
        self._path_size = None  # (w, h) the cached _path was built for
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(_SWEEP_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_value_changed)

    def _on_value_changed(self, value):
        self._progress = value
        self.update()

    def play(self):
        self._anim.stop()
        self._progress = 0.0
        self._anim.start()

    def _ensure_path(self, w, h):
        if self._path_size == (w, h):
            return
        self._path_size = (w, h)
        mid_y = h / 2.0
        amplitude = h * 0.42
        rng = random.Random(_WAVE_SEED)
        steps = 90

        path = QPainterPath()
        path.moveTo(0, mid_y)
        value = 0.0
        for i in range(1, steps + 1):
            x = w * i / steps
            # Smoothed random walk -- jagged like an EMG burst, but still a
            # continuous line rather than pure per-sample noise.
            value = 0.65 * value + 0.35 * rng.uniform(-1.0, 1.0)
            y = mid_y - amplitude * value
            path.lineTo(x, y)
        self._path = path

    def paintEvent(self, event):
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        self._ensure_path(w, h)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipRect(0, 0, int(w * self._progress), h)
        pen = QPen(_WAVE_COLOR)
        pen.setWidthF(2.2)
        painter.setPen(pen)
        painter.drawPath(self._path)
        painter.end()


class ExitConfirmDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Exit Myotion"))
        self.setFixedSize(380, 260)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)

        logo_label = QLabel()
        pix = QPixmap(_LOGO_PATH)
        if not pix.isNull():
            logo_label.setPixmap(
                pix.scaledToHeight(64, Qt.TransformationMode.SmoothTransformation)
            )
        logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(logo_label)

        self._wave = SignalWaveWidget()
        layout.addWidget(self._wave)

        message = QLabel(self.tr("Are you sure you want to exit Myotion?"))
        message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        message.setWordWrap(True)
        layout.addWidget(message)

        layout.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No
        )
        buttons.button(QDialogButtonBox.StandardButton.Yes).setText(self.tr("Exit"))
        buttons.button(QDialogButtonBox.StandardButton.No).setText(self.tr("Cancel"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # No isn't just the safer default in a Yes/No pair -- it's what
        # closing the window without answering (X button, Alt+F4) also
        # resolves to, and quitting is the harder-to-undo of the two.
        buttons.button(QDialogButtonBox.StandardButton.No).setDefault(True)
        buttons.button(QDialogButtonBox.StandardButton.No).setFocus()

    def showEvent(self, event):
        super().showEvent(event)
        self._wave.play()

    @staticmethod
    def confirm(parent=None) -> bool:
        """Show the dialog and return True iff the user chose to exit."""
        dlg = ExitConfirmDialog(parent)
        return dlg.exec() == QDialog.DialogCode.Accepted
