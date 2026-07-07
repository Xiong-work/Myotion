"""
modules/stats/import_dialog.py — column-role picker for externally imported
data files (CSV/Excel) used by the Stats module's "Import Data File" action.

Roles are chosen explicitly by the user rather than auto-guessed, per the
app's preference for transparent, user-controlled workflows over hidden
automatic behavior:
  - Subject column: participant/row identifier
  - Within-subject factor (optional): repeated-measures factor, e.g. a
    pre/mid/post time point column, where each subject has multiple rows
  - Between-subjects factor (optional): a grouping factor that is constant
    per subject, e.g. team position
  - DV columns: numeric outcome(s) to analyze

Within/between combos list every column (not just non-numeric ones) since
factors are sometimes coded numerically (e.g. a 0/1 flag).
"""

import pandas as pd

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLabel, QComboBox, QCheckBox,
    QListWidget, QListWidgetItem, QDialogButtonBox, QMessageBox,
)

from .dataset import ExternalDataset

_STYLE = """
QDialog { background: #282a36; color: #f8f8f2; }
QLabel { color: #f8f8f2; }
QComboBox, QListWidget {
    background: #44475a; color: #f8f8f2; border-radius: 4px; border: none;
}
QComboBox QAbstractItemView {
    background: #44475a; color: #f8f8f2; selection-background-color: #6272a4;
}
"""

_NONE = "(none)"


class ImportColumnDialog(QDialog):
    """Modal dialog: pick subject/within/between column roles and DV columns."""

    def __init__(self, df: pd.DataFrame, non_numeric_cols: list, numeric_cols: list,
                 source_label: str, parent=None):
        super().__init__(parent)
        self._df = df
        self._source_label = source_label
        self.setWindowTitle("Import Data File")
        self.setStyleSheet(_STYLE)
        self.setMinimumWidth(380)

        all_cols = list(df.columns)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<b>{source_label}</b> — {len(df)} rows"))

        form = QFormLayout()

        self._subject_combo = QComboBox()
        self._subject_combo.addItems(all_cols)
        if non_numeric_cols:
            self._subject_combo.setCurrentText(non_numeric_cols[0])
        form.addRow("Subject column:", self._subject_combo)

        self._within_combo = QComboBox()
        self._within_combo.addItem(_NONE, userData=None)
        for c in all_cols:
            self._within_combo.addItem(c, userData=c)
        form.addRow("Within-subject factor:", self._within_combo)

        self._between_combo = QComboBox()
        self._between_combo.addItem(_NONE, userData=None)
        for c in all_cols:
            self._between_combo.addItem(c, userData=c)
        form.addRow("Between-subjects factor:", self._between_combo)

        layout.addLayout(form)

        layout.addWidget(QLabel("Outcome (DV) columns — numeric:"))

        self._select_all_dvs = QCheckBox("Select all")
        self._select_all_dvs.stateChanged.connect(self._on_select_all_toggled)
        layout.addWidget(self._select_all_dvs)

        self._dv_list = QListWidget()
        self._dv_list.setSelectionMode(QListWidget.NoSelection)
        for c in numeric_cols:
            item = QListWidgetItem(c)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self._dv_list.addItem(item)
        self._dv_list.itemChanged.connect(self._on_dv_item_changed)
        layout.addWidget(self._dv_list)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._dataset: ExternalDataset | None = None
        self._syncing_select_all = False

    def _on_select_all_toggled(self, state):
        if self._syncing_select_all:
            return
        check = Qt.Checked if state != 0 else Qt.Unchecked
        self._syncing_select_all = True
        for i in range(self._dv_list.count()):
            self._dv_list.item(i).setCheckState(check)
        self._syncing_select_all = False

    def _on_dv_item_changed(self, _item):
        if self._syncing_select_all:
            return
        all_checked = all(
            self._dv_list.item(i).checkState() == Qt.Checked
            for i in range(self._dv_list.count())
        )
        self._syncing_select_all = True
        self._select_all_dvs.setCheckState(Qt.Checked if all_checked else Qt.Unchecked)
        self._syncing_select_all = False

    def _checked_dvs(self) -> list:
        return [
            self._dv_list.item(i).text()
            for i in range(self._dv_list.count())
            if self._dv_list.item(i).checkState() == Qt.Checked
        ]

    def _on_accept(self):
        subject_col = self._subject_combo.currentText()
        within_col = self._within_combo.currentData()
        between_col = self._between_combo.currentData()
        dvs = self._checked_dvs()

        roles = [subject_col, within_col, between_col]
        non_none_roles = [r for r in roles if r is not None]
        if len(non_none_roles) != len(set(non_none_roles)):
            QMessageBox.warning(
                self, "Import", "Subject / within / between columns must all be different."
            )
            return
        if within_col is None and between_col is None:
            QMessageBox.warning(
                self, "Import",
                "Choose at least one of within-subject or between-subjects factor."
            )
            return
        if not dvs:
            QMessageBox.warning(self, "Import", "Select at least one outcome (DV) column.")
            return
        if any(dv in non_none_roles for dv in dvs):
            QMessageBox.warning(
                self, "Import",
                "Outcome columns cannot also be used as the subject/within/between column."
            )
            return

        self._dataset = ExternalDataset(
            df=self._df, subject_col=subject_col, within_col=within_col,
            between_col=between_col, dv_cols=dvs, source_label=self._source_label,
        )
        self.accept()

    def dataset(self) -> ExternalDataset | None:
        """Available after the dialog is accepted."""
        return self._dataset
