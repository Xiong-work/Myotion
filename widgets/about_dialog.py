"""
widgets/about_dialog.py -- "About Myotion" dialog: logo, version, and a short
credit line for the team/company. Shown from the Help menu button.
"""

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QDialogButtonBox

from modules.app_settings import Settings

_LOGO_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "myotion_resources", "myotion_logo_origin.png")
)


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("About Myotion"))
        self.setFixedSize(380, 300)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(10)

        logo_label = QLabel()
        pix = QPixmap(_LOGO_PATH)
        if not pix.isNull():
            logo_label.setPixmap(
                pix.scaledToHeight(64, Qt.TransformationMode.SmoothTransformation)
            )
        logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(logo_label)

        name_label = QLabel("Myotion")
        name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_label.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(name_label)

        version_label = QLabel(self.tr("Version {0}").format(Settings.APP_VERSION))
        version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(version_label)

        credit_label = QLabel(
            self.tr("Developed by the AccMov team.\nBiomechanics and movement analysis software.")
        )
        credit_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        credit_label.setWordWrap(True)
        layout.addWidget(credit_label)

        email_label = QLabel("info@accmov.com")
        email_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(email_label)

        layout.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    @staticmethod
    def show_about(parent=None):
        AboutDialog(parent).exec()
