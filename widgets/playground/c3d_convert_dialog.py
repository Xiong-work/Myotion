"""widgets/playground/c3d_convert_dialog.py -- batch-convert .c3d files to
OpenSim .trc (markers) and .mot (ground reaction forces, when present)."""

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QPushButton, QLabel,
    QLineEdit, QCheckBox, QDoubleSpinBox, QFileDialog, QListWidget,
    QListWidgetItem, QMessageBox,
)

from modules.playground.c3d_convert import find_c3d_files, convert_c3d_batch


class C3dConvertDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("C3D -> TRC/MOT Converter"))
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMinimizeButtonHint
                             | Qt.WindowType.WindowMaximizeButtonHint)
        self.resize(720, 560)

        self._input_folder = None
        self._output_folder = None

        layout = QVBoxLayout(self)

        # Input / output folder pickers
        io_form = QFormLayout()
        in_row = QHBoxLayout()
        self._input_edit = QLineEdit()
        self._input_edit.setReadOnly(True)
        in_btn = QPushButton(self.tr("Browse..."))
        in_btn.clicked.connect(self._pick_input_folder)
        in_row.addWidget(self._input_edit, 1)
        in_row.addWidget(in_btn)
        io_form.addRow(self.tr("Input folder:"), in_row)

        out_row = QHBoxLayout()
        self._output_edit = QLineEdit()
        self._output_edit.setReadOnly(True)
        out_btn = QPushButton(self.tr("Browse..."))
        out_btn.clicked.connect(self._pick_output_folder)
        out_row.addWidget(self._output_edit, 1)
        out_row.addWidget(out_btn)
        io_form.addRow(self.tr("Output folder:"), out_row)
        layout.addLayout(io_form)

        # Options
        options_form = QFormLayout()
        self._recursive_check = QCheckBox(self.tr("Search subfolders"))
        options_form.addRow("", self._recursive_check)

        self._yaw_spin = QDoubleSpinBox()
        self._yaw_spin.setRange(-180, 180)
        self._yaw_spin.setSuffix(" deg")
        options_form.addRow(self.tr("Yaw correction:"), self._yaw_spin)

        self._mot_threshold_spin = QDoubleSpinBox()
        self._mot_threshold_spin.setRange(0, 500)
        self._mot_threshold_spin.setValue(20.0)
        self._mot_threshold_spin.setSuffix(" N")
        options_form.addRow(self.tr("MOT force threshold:"), self._mot_threshold_spin)

        self._mot_rate_spin = QDoubleSpinBox()
        self._mot_rate_spin.setRange(0, 10000)
        self._mot_rate_spin.setSpecialValueText(self.tr("(native rate)"))
        options_form.addRow(self.tr("MOT output rate:"), self._mot_rate_spin)
        layout.addLayout(options_form)

        # Convert button
        convert_row = QHBoxLayout()
        convert_row.addStretch()
        self._convert_btn = QPushButton(self.tr("Convert"))
        self._convert_btn.clicked.connect(self._on_convert)
        convert_row.addWidget(self._convert_btn)
        layout.addLayout(convert_row)

        # Log
        self._log = QListWidget()
        layout.addWidget(self._log, 1)

    def _pick_input_folder(self):
        folder = QFileDialog.getExistingDirectory(self, self.tr("Select Input Folder"))
        if folder:
            self._input_folder = folder
            self._input_edit.setText(folder)
            if not self._output_folder:
                default_out = os.path.join(folder, "OpenSim")
                self._output_folder = default_out
                self._output_edit.setText(default_out)

    def _pick_output_folder(self):
        folder = QFileDialog.getExistingDirectory(self, self.tr("Select Output Folder"))
        if folder:
            self._output_folder = folder
            self._output_edit.setText(folder)

    def _on_convert(self):
        if not self._input_folder:
            QMessageBox.warning(self, self.tr("No Input Folder"), self.tr("Pick an input folder first."))
            return
        if not self._output_folder:
            QMessageBox.warning(self, self.tr("No Output Folder"), self.tr("Pick an output folder first."))
            return

        c3d_files = find_c3d_files(self._input_folder, recursive=self._recursive_check.isChecked())
        if not c3d_files:
            QMessageBox.information(self, self.tr("No Files"), self.tr("No .c3d files found in that folder."))
            return

        self._log.clear()
        self._convert_btn.setEnabled(False)
        mot_rate = self._mot_rate_spin.value() or None

        def on_progress(idx, total, c3d_path, trc_result, mot_result):
            name = os.path.basename(c3d_path)
            trc_ok, trc_info = trc_result
            mot_ok, mot_info = mot_result
            trc_text = self.tr("TRC ok") if trc_ok else self.tr("TRC failed: {0}").format(trc_info)
            mot_text = self.tr("MOT ok") if mot_ok else self.tr("MOT skipped/failed: {0}").format(mot_info)
            item = QListWidgetItem(f"[{idx}/{total}] {name} -- {trc_text}; {mot_text}")
            self._log.addItem(item)
            self._log.scrollToBottom()

        try:
            results = convert_c3d_batch(
                c3d_files, self._output_folder,
                yaw_correction_deg=self._yaw_spin.value(),
                mot_threshold=self._mot_threshold_spin.value(),
                mot_rate=mot_rate,
                progress_cb=on_progress,
            )
        finally:
            self._convert_btn.setEnabled(True)

        trc_ok_count = sum(1 for _, trc, _ in results if trc[0])
        mot_ok_count = sum(1 for _, _, mot in results if mot[0])
        self._log.addItem(QListWidgetItem(
            self.tr("Done: {0} files. TRC ok={1}, MOT ok={2}.").format(len(results), trc_ok_count, mot_ok_count)
        ))
