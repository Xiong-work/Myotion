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

import os
import re

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit, QCheckBox,
    QComboBox, QDoubleSpinBox, QSpinBox, QGroupBox, QDialogButtonBox,
    QPushButton, QFileDialog, QMessageBox, QInputDialog,
)

from modules.pyMotion.core.batch_config import (
    BatchConfig, BatchLayout, ChannelMapping, EMGProcessingParams,
)
from modules.pyMotion.core.batch_scan import detect_layout


def _task_type_from_glob(glob):
    """Derive a short, human-readable task name from a detected task-file
    glob, for pre-filling "Task type" and naming a saved config -- e.g.
    "Tasks/lift_stitched.c3d" -> "lift" ("_stitched" is our own stitching
    output suffix, not part of the task's real name)."""
    stem = os.path.splitext(os.path.basename(glob))[0]
    return re.sub(r'_stitched$', '', stem, flags=re.IGNORECASE)


class BatchConfigDialog(QDialog):
    def __init__(self, cfg: BatchConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Batch Config"))
        self._channel_mapping = cfg.channel_mapping  # passthrough, not edited here
        self._last_suggestion = None  # set by _onDetectFromFolder; feeds sibling_task_configs()

        layout = QVBoxLayout(self)

        layout_group = QGroupBox(self.tr("Batch Layout"))
        form1 = QFormLayout(layout_group)

        detect_row = QHBoxLayout()
        self.detect_btn = QPushButton(self.tr("Detect from folder…"))
        self.detect_btn.clicked.connect(self._onDetectFromFolder)
        detect_row.addWidget(self.detect_btn)
        detect_row.addStretch()
        form1.addRow(detect_row)

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

    def _onDetectFromFolder(self):
        """Inspect a real folder tree and pre-fill the layout fields below,
        instead of asking the user to hand-write glob patterns. Heuristic --
        every field it touches stays editable afterward, nothing is applied
        silently."""
        folder = QFileDialog.getExistingDirectory(
            self, self.tr("Select batch root folder to inspect"), ""
        )
        if not folder:
            return

        suggestion = detect_layout(folder)
        self._last_suggestion = suggestion

        if not suggestion.task_candidates and not suggestion.mvc_glob:
            QMessageBox.warning(
                self, self.tr("Detect from folder"),
                "\n".join(suggestion.warnings) or self.tr("Nothing usable was detected."),
            )
            return

        self.participant_glob_edit.setText(suggestion.participant_glob)
        self.mvc_glob_edit.setText(suggestion.mvc_glob)

        chosen_task_glob = ""
        if len(suggestion.task_candidates) == 1:
            chosen_task_glob = suggestion.task_candidates[0].glob
        elif len(suggestion.task_candidates) > 1:
            options = [
                "{}  ({}/{} participants, e.g. {})".format(
                    c.glob, c.coverage, suggestion.participant_count, c.example
                )
                for c in suggestion.task_candidates
            ]
            choice, ok = QInputDialog.getItem(
                self, self.tr("Select task file"),
                self.tr(
                    "Multiple task files were found per participant -- a batch "
                    "covers exactly one task. Pick the one this config is for:"
                ),
                options, 0, False,
            )
            if ok:
                chosen_task_glob = suggestion.task_candidates[options.index(choice)].glob
        if chosen_task_glob:
            self.emg_file_edit.setText(chosen_task_glob)
            if not self.task_type_edit.text().strip():
                self.task_type_edit.setText(_task_type_from_glob(chosen_task_glob))

        if suggestion.warnings:
            QMessageBox.information(
                self, self.tr("Detect from folder"),
                self.tr("Layout fields were pre-filled from {} -- review before saving.\n\n{}").format(
                    folder, "\n".join(suggestion.warnings)
                ),
            )

    def _read_processing(self) -> EMGProcessingParams:
        return EMGProcessingParams(
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

    def get_config(self) -> BatchConfig:
        layout = BatchLayout(
            task_type=self.task_type_edit.text().strip(),
            participant_glob=self.participant_glob_edit.text().strip() or "*",
            emg_file=self.emg_file_edit.text().strip() or "*.c3d",
            mvc_glob=self.mvc_glob_edit.text().strip(),
        )
        return BatchConfig(
            layout=layout, channel_mapping=self._channel_mapping, processing=self._read_processing()
        )

    def sibling_task_configs(self):
        """Other task candidates found by the last "Detect from folder" run,
        besides whichever one ended up in the Task file glob field above --
        one BatchConfig per remaining candidate, sharing this dialog's
        current participant/MVC glob and EMG processing settings but with an
        empty channel_mapping (mapped later, per task, during Batch Import).

        A batch root often holds several tasks recorded per participant
        (e.g. lift/squat/gait) but a BatchConfig is single-task by
        convention -- this lets the caller save a ready-to-use .toml for
        every detected task in one step, instead of only the one chosen
        here. Empty if detect was never run, or found just one task.
        """
        if not self._last_suggestion or len(self._last_suggestion.task_candidates) < 2:
            return []
        current_glob = self.emg_file_edit.text().strip()
        participant_glob = self.participant_glob_edit.text().strip() or "*"
        mvc_glob = self.mvc_glob_edit.text().strip()
        processing = self._read_processing()

        result = []
        for c in self._last_suggestion.task_candidates:
            if c.glob == current_glob:
                continue
            layout = BatchLayout(
                task_type=_task_type_from_glob(c.glob),
                participant_glob=participant_glob,
                emg_file=c.glob,
                mvc_glob=mvc_glob,
            )
            result.append(BatchConfig(layout=layout, channel_mapping=ChannelMapping(), processing=processing))
        return result
