"""widgets/playground/emg_snr_dialog.py -- load a c3d/mat/csv EMG file and
check each channel's signal quality (RMS-ratio SNR)."""

import os

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QComboBox,
    QFileDialog, QTableWidget, QTableWidgetItem, QMessageBox, QSplitter,
    QWidget, QHeaderView,
)

from modules.playground.emg_snr import load_signal, compute_snr, EmgSnrError, DEFAULT_WINDOW_S

_BASELINE_COLOR = "#3498db"
_ACTIVE_COLOR = "#e67e22"

# General surface-EMG SNR quality bands (amplitude-ratio dB, see
# modules.playground.emg_snr.compute_snr) -- a widely-cited rule of thumb
# for RMS-ratio SNR, not a validated diagnostic threshold: below ~10 dB the
# signal is dominated by noise/crosstalk, 10-20 dB is usable but marginal,
# >= 20 dB is generally considered good quality.
_SNR_GOOD_DB = 20.0
_SNR_FAIR_DB = 10.0
_SNR_GOOD_COLOR = QColor("#2e7d32")
_SNR_FAIR_COLOR = QColor("#b8860b")
_SNR_POOR_COLOR = QColor("#c0392b")


def _snr_quality(snr_db):
    """(label, QColor) for snr_db against the good/fair/poor bands above."""
    if snr_db >= _SNR_GOOD_DB:
        return "Good", _SNR_GOOD_COLOR
    if snr_db >= _SNR_FAIR_DB:
        return "Fair", _SNR_FAIR_COLOR
    return "Poor", _SNR_POOR_COLOR


class EmgSnrDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("EMG Signal Quality (SNR)"))
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMinimizeButtonHint
                             | Qt.WindowType.WindowMaximizeButtonHint)
        self.resize(900, 560)

        self._tst = None
        self._results = {}  # channel -> result dict from compute_snr

        layout = QVBoxLayout(self)

        top_row = QHBoxLayout()
        self._load_btn = QPushButton(self.tr("Load File..."))
        self._load_btn.clicked.connect(self._on_load)
        top_row.addWidget(self._load_btn)
        self._file_label = QLabel(self.tr("No file loaded"))
        top_row.addWidget(self._file_label, 1)
        layout.addLayout(top_row)

        snr_legend = QLabel(
            self.tr(
                'Good SNR: <span style="color:{good};font-weight:bold">&ge; {good_db:.0f} dB</span>'
                '&nbsp;&nbsp;Fair: <span style="color:{fair};font-weight:bold">{fair_db:.0f}-{good_db:.0f} dB</span>'
                '&nbsp;&nbsp;Poor: <span style="color:{poor};font-weight:bold">&lt; {fair_db:.0f} dB</span>'
                ' (general guideline, not a validated diagnostic threshold)'
            ).format(
                good=_SNR_GOOD_COLOR.name(), fair=_SNR_FAIR_COLOR.name(), poor=_SNR_POOR_COLOR.name(),
                good_db=_SNR_GOOD_DB, fair_db=_SNR_FAIR_DB,
            )
        )
        snr_legend.setStyleSheet("color: rgba(0,0,0,0.7); font-size: 11px;")
        layout.addWidget(snr_legend)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter, 1)

        # Left: channel table
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels([self.tr("Channel"), self.tr("SNR (dB)"), self.tr("Quality")])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setMinimumSectionSize(90)
        # A splitter can otherwise squeeze this panel down until the header
        # text itself is clipped (e.g. "Channel" -> "Channe") -- give it a
        # floor wide enough for both headers plus real column content.
        self._table.setMinimumWidth(280)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.itemSelectionChanged.connect(self._on_row_selected)
        splitter.addWidget(self._table)

        # Right: plot + recompute controls
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self._plot = pg.PlotWidget()
        self._plot.setBackground("#e5ecf6")
        self._plot.setLabel("bottom", self.tr("Time (s)"))
        self._curve = self._plot.plot([], [], pen=pg.mkPen(color="#586cdb", width=1))
        self._baseline_region = pg.LinearRegionItem(brush=pg.mkBrush(_BASELINE_COLOR + "40"))
        self._active_region = pg.LinearRegionItem(brush=pg.mkBrush(_ACTIVE_COLOR + "40"))
        self._plot.addItem(self._baseline_region)
        self._plot.addItem(self._active_region)
        right_layout.addWidget(self._plot, 1)

        legend_row = QHBoxLayout()
        legend_row.addWidget(QLabel(f'<span style="color:{_BASELINE_COLOR}">■</span> ' + self.tr("Baseline (drag to adjust)")))
        legend_row.addWidget(QLabel(f'<span style="color:{_ACTIVE_COLOR}">■</span> ' + self.tr("Active burst (drag to adjust)")))
        legend_row.addStretch()
        self._recompute_btn = QPushButton(self.tr("Recompute SNR for this channel"))
        self._recompute_btn.setEnabled(False)
        self._recompute_btn.clicked.connect(self._on_recompute)
        legend_row.addWidget(self._recompute_btn)
        right_layout.addLayout(legend_row)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

    def _on_load(self):
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Load EMG File"), "",
            self.tr("EMG files (*.c3d *.mat *.csv);;All files (*)"),
        )
        if not path:
            return
        try:
            self._tst = load_signal(path)
        except EmgSnrError as e:
            QMessageBox.warning(self, self.tr("Load Failed"), str(e))
            return
        except Exception as e:
            QMessageBox.warning(self, self.tr("Load Failed"), self.tr(f"Could not load file: {e}"))
            return

        self._file_label.setText(os.path.basename(path))
        self._results = {}
        self._table.setRowCount(0)
        for ch in self._tst.labels:
            try:
                result = compute_snr(self._tst, ch, window_s=DEFAULT_WINDOW_S)
            except EmgSnrError:
                continue
            self._results[ch] = result
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._table.setItem(row, 0, QTableWidgetItem(ch))
            self._set_snr_columns(row, result["snr_db"])

        if self._table.rowCount() > 0:
            self._table.selectRow(0)

    def _set_snr_columns(self, row, snr_db):
        """Fill the SNR (dB) and Quality columns for *row*, colored by the
        good/fair/poor bands (see _snr_quality) -- shared by _on_load and
        _on_recompute so both stay in sync."""
        label, color = _snr_quality(snr_db)
        snr_item = QTableWidgetItem(f"{snr_db:.2f}")
        snr_item.setForeground(color)
        self._table.setItem(row, 1, snr_item)
        quality_item = QTableWidgetItem(label)
        quality_item.setForeground(color)
        self._table.setItem(row, 2, quality_item)

    def _on_row_selected(self):
        rows = self._table.selectionModel().selectedRows()
        if not rows or self._tst is None:
            return
        channel = self._table.item(rows[0].row(), 0).text()
        result = self._results.get(channel)
        if result is None:
            return

        data = self._tst[channel]
        t = self._tst.getLinspace()
        self._curve.setData(t, data)
        self._plot.setTitle(channel)
        self._baseline_region.setRegion(result["baseline_window"])
        self._active_region.setRegion(result["active_window"])
        self._recompute_btn.setEnabled(True)
        self._recompute_btn.setProperty("channel", channel)

    def _on_recompute(self):
        rows = self._table.selectionModel().selectedRows()
        if not rows or self._tst is None:
            return
        row = rows[0].row()
        channel = self._table.item(row, 0).text()
        try:
            result = compute_snr(
                self._tst, channel,
                baseline_window=tuple(self._baseline_region.getRegion()),
                active_window=tuple(self._active_region.getRegion()),
            )
        except EmgSnrError as e:
            QMessageBox.warning(self, self.tr("Recompute Failed"), str(e))
            return
        self._results[channel] = result
        self._set_snr_columns(row, result["snr_db"])
