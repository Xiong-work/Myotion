"""
modules/kinematics/manual_cycles_dialog.py — type in repetition boundaries by hand.

Companion to cycle_detection.py's auto-detectors: used both when there's no
kinematics/marker to detect from (EMG-only trials with externally-noted
segment times) and to fine-tune auto-detected boundaries with exact typed
numbers instead of scrubbing the timeline. Each row also has a "Pick"
button for a ginput-style alternative: click it, then click twice on a
signal already plotted in the kinematics inspection panel (start, then
end) instead of typing numbers -- a vertical-only crosshair (only time
matters here, not the signal's magnitude) follows the mouse while armed,
and each click drops a persistent green (start) / red (end) marker so the
picked values are confirmed visually as well as in the table. Re-clicking
the armed "Start…"/"End…" button, or right-clicking the plot, cancels the
pick in progress. With "Auto-step" checked, finishing a row's End
automatically adds and arms the next row -- right-click the plot to stop
the chain (the unused trailing row is dropped).

Non-modal by design (shown via .show(), not .exec()) -- picking from the
plot requires the main window (which owns the plot) to stay interactive
while this dialog is open.
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QDialogButtonBox, QLabel, QHeaderView, QCheckBox,
)


class ManualCyclesDialog(QDialog):
    def __init__(self, plot_widget, parent=None):
        super().__init__(parent)
        self._plot = plot_widget
        self.task_type = None
        self._total_time = 0.0
        # Row/stage currently waiting for a plot click ("start" then "end")
        self._armed_row = None
        self._armed_stage = None
        self._armed_button = None

        layout = QVBoxLayout(self)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels([self.tr("Start (s)"), self.tr("End (s)"), ""])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        # Fixed (not ResizeToContents) -- otherwise a narrow dialog squeezes
        # this column until the button label is clipped.
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(2, 90)
        layout.addWidget(self.table)

        row_btns = QHBoxLayout()
        add_btn = QPushButton(self.tr("+ Row"))
        add_btn.clicked.connect(lambda: self._add_row())
        remove_btn = QPushButton(self.tr("− Row"))
        remove_btn.clicked.connect(self._remove_selected_rows)
        row_btns.addWidget(add_btn)
        row_btns.addWidget(remove_btn)
        row_btns.addStretch()
        self.autoStepCheck = QCheckBox(self.tr("Auto-step"))
        self.autoStepCheck.setToolTip(
            self.tr(
                "After picking a row's End on the plot, automatically add a new "
                "row and arm it for picking too -- keep clicking through reps "
                "without touching \"Pick\" again. Right-click the plot to stop."
            )
        )
        row_btns.addWidget(self.autoStepCheck)
        layout.addLayout(row_btns)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.resize(440, 320)

    def set_task(self, task_type, existing_pairs, total_time):
        """(Re)populate the dialog for *task_type*. Called every time the
        controller shows this (cached, reused) dialog instance."""
        self._cancel_pick()
        self._plot.clear_pick_markers()
        self.task_type = task_type
        self._total_time = total_time
        self.setWindowTitle(self.tr("Manual Cycles — {}").format(task_type))
        self.hint.setText(
            self.tr(
                "Enter start/end time (s) for each repetition, or click \"Pick\" and "
                "then click twice on a signal already plotted in the panel above "
                "(first click = start, second = end). Click \"Pick\"/the plot's "
                "right mouse button to cancel a pick in progress. Trial length: {:.3f} s."
            ).format(total_time)
        )
        self.table.setRowCount(0)
        for t_start, t_end in existing_pairs:
            self._add_row(t_start, t_end)
        if not existing_pairs:
            self._add_row()

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

        pick_btn = QPushButton(self.tr("Pick"))
        pick_btn.setToolTip(
            self.tr(
                "Click, then click twice on a plotted signal above -- "
                "first click sets Start (green marker), second sets End (red marker)."
            )
        )
        pick_btn.clicked.connect(lambda checked=False, b=pick_btn: self._arm_pick(b))
        self.table.setCellWidget(row, 2, pick_btn)

    def _row_of_button(self, button):
        for row in range(self.table.rowCount()):
            if self.table.cellWidget(row, 2) is button:
                return row
        return None

    def _remove_selected_rows(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            if r == self._armed_row:
                self._cancel_pick()
            self.table.removeRow(r)

    def _arm_pick(self, button):
        row = self._row_of_button(button)
        if row is None:
            return
        if self._armed_row == row:
            # Clicking the already-armed row's own button again cancels
            # picking instead of restarting it (mirrors the plot's
            # right-click cancel gesture).
            self._cancel_pick_discard_if_blank()
            return
        self._cancel_pick()
        self._plot.clear_pick_markers()  # fresh row -- drop the previous row's preview
        self._armed_row = row
        self._armed_stage = "start"
        self._armed_button = button
        button.setText(self.tr("Start…"))
        self._plot.enable_time_picking(self._on_plot_time_picked, on_cancel=self._cancel_pick_discard_if_blank)

    def _cancel_pick(self):
        """Disarm picking and reset the button label. Leaves table rows alone."""
        if self._armed_row is None:
            return
        self._plot.disable_time_picking()
        if self._armed_button is not None:
            try:
                self._armed_button.setText(self.tr("Pick"))
            except RuntimeError:
                pass  # underlying C++ button/row may already be gone
        self._armed_row = None
        self._armed_stage = None
        self._armed_button = None

    def _cancel_pick_discard_if_blank(self):
        """Cancel picking (re-clicking the armed button, or right-clicking
        the plot), and if nothing had been picked yet for that row and it's
        otherwise blank, drop the row -- cleans up an Auto-step row the
        user decided not to use after all."""
        row, stage = self._armed_row, self._armed_stage
        self._cancel_pick()
        if stage == "start" and row is not None and row < self.table.rowCount():
            start_item = self.table.item(row, 0)
            end_item = self.table.item(row, 1)
            if not (start_item and start_item.text().strip()) and not (end_item and end_item.text().strip()):
                self.table.removeRow(row)

    def _on_plot_time_picked(self, time_s):
        row, stage, button = self._armed_row, self._armed_stage, self._armed_button
        if row is None or row >= self.table.rowCount():
            return
        col = 0 if stage == "start" else 1
        self.table.setItem(row, col, QTableWidgetItem("{:.3f}".format(time_s)))
        # Instant visual confirmation, matching the colors _apply_cycle_pairs's
        # real CycleStart_/CycleEnd_ events get once the dialog is accepted.
        self._plot.show_pick_marker(time_s, "#2ecc71" if stage == "start" else "#e74c3c")
        if stage == "start":
            self._armed_stage = "end"
            button.setText(self.tr("End…"))
            self._plot.enable_time_picking(self._on_plot_time_picked, on_cancel=self._cancel_pick_discard_if_blank)
        else:
            self._cancel_pick()
            if self.autoStepCheck.isChecked():
                self._add_row()
                new_row = self.table.rowCount() - 1
                self._arm_pick(self.table.cellWidget(new_row, 2))

    def reject(self):
        self._cancel_pick()
        self._plot.clear_pick_markers()
        super().reject()

    def accept(self):
        self._cancel_pick()
        self._plot.clear_pick_markers()
        super().accept()

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
