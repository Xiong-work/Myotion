"""widgets/playground_dialog.py -- launcher for the Playground utility tools:
EMG signal-quality check, camera-calibration viewer, OpenSim model viewer,
and C3D -> TRC/MOT conversion. Each tool is a self-contained dialog operating
on whatever file the user picks -- none of them touch the main app's shared
trial/workspace state.
"""

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QDialog, QGridLayout, QLabel, QPushButton, QVBoxLayout

from widgets.playground.emg_snr_dialog import EmgSnrDialog
from widgets.playground.camera_calib_dialog import CameraCalibDialog
from widgets.playground.opensim_viewer_dialog import OpenSimViewerDialog
from widgets.playground.c3d_convert_dialog import C3dConvertDialog
from widgets.playground.gap_fill_dialog import GapFillDialog

_CARD_STYLE = """
QPushButton {
    background-color: rgba(0,0,0,0.05);
    border: 1px solid rgba(0,0,0,0.15);
    border-radius: 10px;
    padding: 12px;
    font-size: 13px;
    color: black;
    text-align: center;
}
QPushButton:hover { background-color: rgba(0,0,0,0.1); }
QPushButton:pressed { background-color: rgba(0,0,0,0.15); }
"""


def _recolored_icon(path, color=QColor("black")):
    """These tool icons are the app's CoreUI sidebar set (light/white glyphs
    meant for a dark background) -- this dialog's cards are light, so render
    every icon as a solid-color silhouette (alpha channel kept, RGB
    replaced) instead of shipping separate dark-colored icon assets."""
    pixmap = QPixmap(path)
    if pixmap.isNull():
        return QIcon(path)
    tinted = QPixmap(pixmap.size())
    tinted.fill(Qt.GlobalColor.transparent)
    painter = QPainter(tinted)
    painter.drawPixmap(0, 0, pixmap)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(tinted.rect(), color)
    painter.end()
    return QIcon(tinted)


class PlaygroundDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Playground"))
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMinimizeButtonHint
                             | Qt.WindowType.WindowMaximizeButtonHint)
        self.setMinimumSize(480, 360)

        layout = QVBoxLayout(self)
        title = QLabel(self.tr("Playground"))
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)
        subtitle = QLabel(self.tr("Standalone tools -- pick a file, get an answer."))
        subtitle.setStyleSheet("color: rgba(0,0,0,0.6);")
        layout.addWidget(subtitle)

        grid = QGridLayout()
        grid.setSpacing(12)
        layout.addLayout(grid)

        tools = [
            (self.tr("EMG Signal\nQuality (SNR)"), "images/icons/cil-equalizer.png", self._open_emg_snr),
            (self.tr("Camera\nCalibration Viewer"), "images/icons/cil-camera.png", self._open_camera_calib),
            (self.tr("OpenSim\nModel Viewer"), "myotion_resources/icons/stick_figure.png", self._open_opensim_viewer),
            (self.tr("C3D -> TRC/MOT\nConverter"), "images/icons/cil-transfer.png", self._open_c3d_convert),
            (self.tr("C3D Gap Fill"), "myotion_resources/icons/puzzle.png", self._open_gap_fill),
        ]
        for i, (label, icon_path, handler) in enumerate(tools):
            btn = QPushButton(label)
            btn.setIcon(_recolored_icon(icon_path))
            btn.setIconSize(QSize(36, 36))
            btn.setMinimumSize(180, 110)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(_CARD_STYLE)
            btn.clicked.connect(handler)
            grid.addWidget(btn, i // 2, i % 2)

        layout.addStretch()

    def _open_emg_snr(self):
        EmgSnrDialog(self).exec()

    def _open_camera_calib(self):
        CameraCalibDialog(self).exec()

    def _open_opensim_viewer(self):
        OpenSimViewerDialog(self).exec()

    def _open_c3d_convert(self):
        C3dConvertDialog(self).exec()

    def _open_gap_fill(self):
        GapFillDialog(self).exec()
