"""
widgets/batch_config_dialog.py — structured-form editor for a batch-import
TOML config's [batch] and [emg_processing] sections.

Per project convention, channel_mapping is edited live via
ChannelMappingPanel during Batch Import (it needs real channel names from a
scanned participant), not here -- this dialog only covers the two things the
config is meant to control: "how the batch is structured" and "EMG
processing". Any existing channel_mapping on the config passed in is carried
through unchanged by get_config().
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, QCheckBox, QComboBox,
    QDoubleSpinBox, QSpinBox, QGroupBox, QDialogButtonBox,
)

from modules.pyMotion.core.batch_config import BatchConfig, BatchLayout, EMGProcessingParams


class BatchConfigDialog(QDialog):
    def __init__(self, cfg: BatchConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Batch Config"))
        self._channel_mapping = cfg.channel_mapping  # passthrough, not edited here

        layout = QVBoxLayout(self)

        layout_group = QGroupBox(self.tr("Batch Layout"))
        form1 = QFormLayout(layout_group)
        self.task_type_edit = QLineEdit(cfg.layout.task_type)
        form1.addRow(self.tr("Task type:"), self.task_type_edit)
        self.participant_glob_edit = QLineEdit(cfg.layout.participant_glob)
        form1.addRow(self.tr("Participant folder glob:"), self.participant_glob_edit)
        self.emg_file_edit = QLineEdit(cfg.layout.emg_file)
        form1.addRow(self.tr("Task file glob:"), self.emg_file_edit)
        self.mvc_glob_edit = QLineEdit(cfg.layout.mvc_glob)
        form1.addRow(self.tr("MVC file glob (optional):"), self.mvc_glob_edit)
        layout.addWidget(layout_group)

        proc_group = QGroupBox(self.tr("EMG Processing"))
        form2 = QFormLayout(proc_group)
        p = cfg.processing

        self.dc_offset_check = QCheckBox(self.tr("Enable"))
        self.dc_offset_check.setChecked(p.dc_offset_enable)
        form2.addRow(self.tr("Remove DC Offset:"), self.dc_offset_check)

        self.bandpass_check = QCheckBox(self.tr("Enable"))
        self.bandpass_check.setChecked(p.bandpass_enable)
        form2.addRow(self.tr("Band-pass Filter:"), self.bandpass_check)
        self.bandpass_l = QDoubleSpinBox()
        self.bandpass_l.setRange(0, 5000)
        self.bandpass_l.setValue(p.bandpass_cutoff_l)
        form2.addRow(self.tr("  Cutoff low (Hz):"), self.bandpass_l)
        self.bandpass_h = QDoubleSpinBox()
        self.bandpass_h.setRange(0, 5000)
        self.bandpass_h.setValue(p.bandpass_cutoff_h)
        form2.addRow(self.tr("  Cutoff high (Hz):"), self.bandpass_h)
        self.bandpass_order = QSpinBox()
        self.bandpass_order.setRange(2, 4)
        self.bandpass_order.setValue(p.bandpass_order)
        form2.addRow(self.tr("  Order:"), self.bandpass_order)

        self.rectify_check = QCheckBox(self.tr("Enable"))
        self.rectify_check.setChecked(p.rectify_enable)
        form2.addRow(self.tr("Full-Wave Rectification:"), self.rectify_check)

        self.envelope_check = QCheckBox(self.tr("Enable"))
        self.envelope_check.setChecked(p.envelope_enable)
        form2.addRow(self.tr("Linear Envelope (low-pass):"), self.envelope_check)
        self.envelope_cutoff = QDoubleSpinBox()
        self.envelope_cutoff.setRange(0, 5000)
        self.envelope_cutoff.setValue(p.envelope_cutoff)
        form2.addRow(self.tr("  Cutoff (Hz):"), self.envelope_cutoff)
        self.envelope_order = QSpinBox()
        self.envelope_order.setRange(2, 4)
        self.envelope_order.setValue(p.envelope_order)
        form2.addRow(self.tr("  Order:"), self.envelope_order)

        self.norm_check = QCheckBox(self.tr("Enable"))
        self.norm_check.setChecked(p.normalization_enable)
        form2.addRow(self.tr("Normalization:"), self.norm_check)
        self.norm_type_combo = QComboBox()
        self.norm_type_combo.addItems([self.tr("MVC"), self.tr("Trial Max")])
        self.norm_type_combo.setCurrentIndex(1 if p.normalization_type == "trial_max" else 0)
        form2.addRow(self.tr("  Type:"), self.norm_type_combo)

        layout.addWidget(proc_group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_config(self) -> BatchConfig:
        layout = BatchLayout(
            task_type=self.task_type_edit.text().strip(),
            participant_glob=self.participant_glob_edit.text().strip() or "*",
            emg_file=self.emg_file_edit.text().strip() or "*.c3d",
            mvc_glob=self.mvc_glob_edit.text().strip(),
        )
        processing = EMGProcessingParams(
            dc_offset_enable=self.dc_offset_check.isChecked(),
            bandpass_enable=self.bandpass_check.isChecked(),
            bandpass_cutoff_l=self.bandpass_l.value(),
            bandpass_cutoff_h=self.bandpass_h.value(),
            bandpass_order=self.bandpass_order.value(),
            rectify_enable=self.rectify_check.isChecked(),
            envelope_enable=self.envelope_check.isChecked(),
            envelope_cutoff=self.envelope_cutoff.value(),
            envelope_order=self.envelope_order.value(),
            normalization_enable=self.norm_check.isChecked(),
            normalization_type=(
                "trial_max" if self.norm_type_combo.currentIndex() == 1 else "mvc"
            ),
        )
        return BatchConfig(layout=layout, channel_mapping=self._channel_mapping, processing=processing)
