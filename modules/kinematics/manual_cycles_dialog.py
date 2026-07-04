"""
modules/kinematics/manual_cycles_dialog.py — type in repetition boundaries by hand.

Companion to cycle_detection.py's auto-detectors: used both when there's no
kinematics/marker to detect from (EMG-only trials with externally-noted
segment times) and to fine-tune auto-detected boundaries with exact typed
numbers instead of scrubbing the timeline.
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QDialogButtonBox, QLabel,
)


class ManualCyclesDialog(QDialog):
    def __init__(self, task_type, existing_pairs, total_time, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Manual Cycles — {}").format(task_type))
        self._total_time = total_time

        layout = QVBoxLayout(self)
        hint = QLabel(
            self.tr(
                "Enter start/end time (s) for each repetition. Trial length: {:.3f} s."
            ).format(total_time)
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(0, 2, self)
        self.table.setHorizontalHeaderLabels([self.tr("Start (s)"), self.tr("End (s)")])
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        for t_start, t_end in existing_pairs:
            self._add_row(t_start, t_end)
        if not existing_pairs:
            self._add_row()

        row_btns = QHBoxLayout()
        add_btn = QPushButton(self.tr("+ Row"))
        add_btn.clicked.connect(lambda: self._add_row())
        remove_btn = QPushButton(self.tr("− Row"))
        remove_btn.clicked.connect(self._remove_selected_rows)
        row_btns.addWidget(add_btn)
        row_btns.addWidget(remove_btn)
        row_btns.addStretch()
        layout.addLayout(row_btns)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.resize(320, 320)

    def _add_row(self, t_start=None, t_end=None):
        """Add a row. None (the default, used by the "+ Row" button) leaves
        both cells blank rather than pre-filling "0.000" -- a blank row is
        silently skipped by get_pairs(), while "0.000" would be a real,
        invalid (end <= start) entry."""
        row = self.table.rowCount()
        self.table.insertRow(row)
        start_txt = "{:.3f}".format(t_start) if t_start is not None else ""
        end_txt = "{:.3f}".format(t_end) if t_end is not None else ""
        self.table.setItem(row, 0, QTableWidgetItem(start_txt))
        self.table.setItem(row, 1, QTableWidgetItem(end_txt))

    def _remove_selected_rows(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def get_pairs(self):
        """Parse and validate table rows.

        Blank rows (both cells empty) are silently skipped -- lets the user
        leave a trailing empty row without it becoming an error. Any other
        malformed row (non-numeric, end <= start, outside trial length) is
        dropped and reported back to the caller.

        Returns (pairs, errors): pairs is a list[(t_start_s, t_end_s)]
        sorted by start time; errors is a list[str] describing skipped rows.
        """
        pairs = []
        errors = []
        for row in range(self.table.rowCount()):
            start_item = self.table.item(row, 0)
            end_item = self.table.item(row, 1)
            start_txt = start_item.text().strip() if start_item else ""
            end_txt = end_item.text().strip() if end_item else ""
            if not start_txt and not end_txt:
                continue
            try:
                t_start = float(start_txt)
                t_end = float(end_txt)
            except ValueError:
                errors.append(self.tr("Row {}: not a number").format(row + 1))
                continue
            if t_end <= t_start:
                errors.append(self.tr("Row {}: end must be after start").format(row + 1))
                continue
            if t_start < 0 or t_end > self._total_time:
                errors.append(
                    self.tr("Row {}: out of trial range (0 - {:.3f}s)")
                    .format(row + 1, self._total_time)
                )
                continue
            pairs.append((t_start, t_end))
        pairs.sort(key=lambda p: p[0])
        return pairs, errors
