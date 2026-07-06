"""widgets/gait_report_dialog.py -- the "Create Report..." popup for Gait
Analysis (see gait_analysis_dialog.py). Builds a single-page, clinically-
styled report (header/logo/participant info, gait parameters, joint-angle
curves across the gait cycle, EMG activity) as a matplotlib Figure shown live
in the dialog, exportable as a PDF.

Joint-angle curves use real Model Output (Angle) data when the loaded trial
has a recognizable Hip/Knee/Ankle angle for that side (see
gait_events.guess_joint_angle_label); this app doesn't yet compute joint
angles from markers on its own, so a trial with none loaded falls back to a
clearly-labeled simulated example curve instead of leaving the panel blank.
EMG activity always uses this module's real per-cycle envelope data -- no
simulated fallback there, since that's already computed either way.
"""

import os

import numpy as np

from PySide6.QtCore import QDate
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QPushButton, QLabel,
    QLineEdit, QComboBox, QSpinBox, QDateEdit, QSplitter, QWidget, QFileDialog,
    QMessageBox,
)

import matplotlib.image as mpimg
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

from modules.pyMotion.core import gait_events as _gait

_LOGO_PREVIEW_SIZE = 90
_JOINTS = ["Hip", "Knee", "Ankle"]
_SIDE_COLOR = {"Right": "#1a73e8", "Left": "#2e7d32"}

# Same default branding used elsewhere in the app (see renderwidget.py's
# placeholder watermark) -- pre-filled here so a report doesn't start blank;
# "Choose Logo..." still lets the user swap in something else.
_DEFAULT_LOGO_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "myotion_resources", "myotion_logo_origin.png")
)


def _simulated_curve(joint, side):
    """A smooth, clearly-not-measured example curve shaped like a coarse
    approximation of the named joint's sagittal-plane trace -- used only when
    the loaded trial has no matching real Angle output (see module
    docstring). Not derived from any measurement; illustrative only."""
    pct = np.linspace(0, 100, 101)
    phase = 0.0 if side == "Right" else 6.0
    if joint == "Hip":
        y = 20.0 * np.sin(2 * np.pi * (pct - 10 + phase) / 100.0)
    elif joint == "Knee":
        y = 35.0 - 35.0 * np.cos(2 * np.pi * (pct + phase) / 100.0)
        y = np.clip(y, 0.0, 70.0)
    else:  # Ankle
        y = 10.0 * np.sin(2 * np.pi * (pct - 20 + phase) / 100.0)
    return pct, y


def _real_curve(kin, fs_k, hs_by_side, joint, side):
    """Mean +/- SD joint-angle curve across 0-100% of the gait cycle, from
    real Model Output data averaged over every detected cycle on *side*.
    Returns (pct, mean, std) or None if no matching angle / cycle exists."""
    label = _gait.guess_joint_angle_label(kin.anglelabels, joint, side)
    if label is None:
        return None
    arr = _gait.angle_xyz_array(kin, label)
    if arr is None:
        return None
    t = np.arange(len(arr)) / fs_k
    y = arr[:, 0]  # sagittal/flexion component is conventionally X in Plug-in-Gait-style Angle outputs
    cycles = _gait.cycles_from_hs(hs_by_side.get(side, []))
    curves = [_gait.resample_cycle(t, y, t0, t1, n_points=101) for t0, t1 in cycles]
    curves = [c for c in curves if not np.all(np.isnan(c))]
    if not curves:
        return None
    stacked = np.vstack(curves)
    pct = np.linspace(0, 100, 101)
    return pct, np.nanmean(stacked, axis=0), np.nanstd(stacked, axis=0)


class _GaitReportDialog(QDialog):
    def __init__(self, spatio, hs_by_side, kin, fs_k, emg_means, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Create Gait Report"))
        self.resize(1100, 850)

        self._spatio = spatio
        self._hs_by_side = hs_by_side
        self._kin = kin
        self._fs_k = fs_k
        self._emg_means = emg_means
        self._logo_path = _DEFAULT_LOGO_PATH if os.path.isfile(_DEFAULT_LOGO_PATH) else None

        layout = QVBoxLayout(self)
        splitter = QSplitter()
        layout.addWidget(splitter, 1)

        form_widget = QWidget()
        form = QFormLayout(form_widget)

        logo_row = QHBoxLayout()
        self._logo_preview = QLabel()
        self._logo_preview.setFixedSize(_LOGO_PREVIEW_SIZE, _LOGO_PREVIEW_SIZE)
        self._logo_preview.setStyleSheet("border: 1px solid #c0c0c0; background: #ffffff;")
        self._logo_preview.setScaledContents(True)
        if self._logo_path:
            self._logo_preview.setPixmap(QPixmap(self._logo_path))
        logo_btn = QPushButton(self.tr("Choose Logo..."))
        logo_btn.clicked.connect(self._on_choose_logo)
        logo_row.addWidget(self._logo_preview)
        logo_row.addWidget(logo_btn)
        form.addRow(self.tr("Logo:"), logo_row)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText(self.tr("Participant name"))
        form.addRow(self.tr("Name:"), self._name_edit)

        self._sex_combo = QComboBox()
        self._sex_combo.addItems(["", self.tr("Male"), self.tr("Female"), self.tr("Other")])
        form.addRow(self.tr("Sex:"), self._sex_combo)

        self._age_spin = QSpinBox()
        self._age_spin.setRange(0, 120)
        self._age_spin.setSpecialValueText(" ")
        form.addRow(self.tr("Age:"), self._age_spin)

        self._date_edit = QDateEdit()
        self._date_edit.setCalendarPopup(True)
        self._date_edit.setDate(QDate.currentDate())
        form.addRow(self.tr("Date:"), self._date_edit)

        self._author_edit = QLineEdit()
        self._author_edit.setPlaceholderText(self.tr("Created by"))
        form.addRow(self.tr("Created by:"), self._author_edit)

        for w in (self._name_edit, self._author_edit):
            w.textChanged.connect(self._update_preview)
        self._sex_combo.currentTextChanged.connect(self._update_preview)
        self._age_spin.valueChanged.connect(self._update_preview)
        self._date_edit.dateChanged.connect(self._update_preview)

        save_pdf_btn = QPushButton(self.tr("Save as PDF..."))
        save_pdf_btn.clicked.connect(self._on_save_pdf)
        form.addRow(save_pdf_btn)

        close_btn = QPushButton(self.tr("Close"))
        close_btn.clicked.connect(self.accept)
        form.addRow(close_btn)

        splitter.addWidget(form_widget)

        self._figure = Figure(figsize=(8.5, 15))
        self._canvas = FigureCanvasQTAgg(self._figure)
        splitter.addWidget(self._canvas)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        self._update_preview()

    def _on_choose_logo(self):
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Choose Logo Image"), "", "Images (*.png *.jpg *.jpeg *.bmp)",
        )
        if not path:
            return
        self._logo_path = path
        self._logo_preview.setPixmap(QPixmap(path))
        self._update_preview()

    def _header_info(self):
        return {
            "name": self._name_edit.text().strip() or self.tr("(unnamed)"),
            "sex": self._sex_combo.currentText() or self.tr("(not specified)"),
            "age": "" if self._age_spin.value() == 0 else str(self._age_spin.value()),
            "date": self._date_edit.date().toString("yyyy-MM-dd"),
            "author": self._author_edit.text().strip() or self.tr("(not specified)"),
        }

    def _update_preview(self, *_args):
        fig = self._figure
        fig.clear()
        info = self._header_info()

        # Header: logo (if any) + title + participant info, on a light grid
        # spanning the top of the page. The three joint-angle rows get much
        # more height than header/table/EMG so each curve has real room to
        # read, and a generous hspace keeps adjacent panels from crowding.
        grid = fig.add_gridspec(
            6, 2, height_ratios=[0.7, 0.8, 2.4, 2.4, 2.4, 1.5], hspace=1.1, wspace=0.25,
        )
        fig.subplots_adjust(top=0.96, bottom=0.04, left=0.08, right=0.96)

        header_ax = fig.add_subplot(grid[0, :])
        header_ax.axis("off")
        if self._logo_path:
            try:
                img = mpimg.imread(self._logo_path)
                logo_ax = fig.add_axes([0.06, 0.905, 0.12, 0.085])
                logo_ax.imshow(img)
                logo_ax.axis("off")
            except Exception:
                pass
        header_ax.text(0.5, 0.7, self.tr("Gait Analysis Report"), ha="center", va="center",
                        fontsize=18, fontweight="bold", transform=header_ax.transAxes)
        info_line = self.tr("Name: {name}    Sex: {sex}    Age: {age}    Date: {date}    Created by: {author}").format(**info)
        header_ax.text(0.5, 0.15, info_line, ha="center", va="center", fontsize=10,
                        transform=header_ax.transAxes)

        # Gait parameters summary table
        table_ax = fig.add_subplot(grid[1, :])
        table_ax.axis("off")
        rows = [["Parameter", "Right", "Left"]]
        for label, key, fmt in (
            ("Stride length (m)", "stride_length_m", "{:.2f}"),
            ("Stride time (s)", "stride_time_s", "{:.2f}"),
            ("Cadence (strides/min)", "cadence_spm", "{:.1f}"),
            ("Velocity (m/s)", "velocity_m_s", "{:.2f}"),
        ):
            row = [label]
            for side in ("Right", "Left"):
                v = self._spatio["stride"][side][key]
                row.append("--" if np.isnan(v) else fmt.format(v))
            rows.append(row)
        tbl = table_ax.table(cellText=rows, loc="center", cellLoc="center")
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(9)
        tbl.scale(1, 1.4)

        # Joint-angle curves, one row per joint, Right/Left overlaid
        for i, joint in enumerate(_JOINTS):
            ax = fig.add_subplot(grid[2 + i, :])
            any_real = False
            for side in ("Right", "Left"):
                real = _real_curve(self._kin, self._fs_k, self._hs_by_side, joint, side)
                if real is not None:
                    pct, mean, std = real
                    ax.plot(pct, mean, color=_SIDE_COLOR[side], label="{} (measured)".format(side))
                    ax.fill_between(pct, mean - std, mean + std, color=_SIDE_COLOR[side], alpha=0.15)
                    any_real = True
                else:
                    pct, y = _simulated_curve(joint, side)
                    ax.plot(pct, y, color=_SIDE_COLOR[side], linestyle="--",
                            label="{} (simulated example)".format(side))
            ax.set_title("{} angle vs. gait cycle{}".format(joint, "" if any_real else " -- simulated example"),
                          fontsize=10)
            ax.set_xlabel("Gait cycle (%)", fontsize=8)
            ax.set_ylabel("Angle (deg)", fontsize=8)
            ax.tick_params(labelsize=7)
            ax.legend(fontsize=7, loc="upper right")
            ax.axhline(0, color="#cccccc", linewidth=0.8)

        # EMG activity summary
        emg_ax = fig.add_subplot(grid[5, :])
        if self._emg_means:
            chans = list(self._emg_means.keys())
            values = [self._emg_means[c] for c in chans]
            emg_ax.bar(range(len(chans)), values, color="#6a4fb3")
            emg_ax.set_xticks(range(len(chans)))
            emg_ax.set_xticklabels(chans, rotation=45, ha="right", fontsize=7)
            emg_ax.set_ylabel("Mean envelope", fontsize=8)
            emg_ax.set_title("EMG activity during gait cycles (measured)", fontsize=10)
            emg_ax.tick_params(labelsize=7)
        else:
            emg_ax.axis("off")
            emg_ax.text(
                0.5, 0.5,
                self.tr("No EMG activity to display (no EMG channels loaded, or no complete gait cycle detected)"),
                ha="center", va="center", fontsize=9, transform=emg_ax.transAxes,
            )

        self._canvas.draw_idle()

    def _on_save_pdf(self):
        path, _ = QFileDialog.getSaveFileName(
            self, self.tr("Save Report as PDF"), "", "PDF Files (*.pdf)",
        )
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        try:
            self._figure.savefig(path)
        except OSError as e:
            QMessageBox.critical(self, self.tr("error"), str(e), QMessageBox.Ok)
            return
        QMessageBox.information(
            self, self.tr("Save Report as PDF"),
            self.tr("Saved to '{}'.").format(path), QMessageBox.Ok,
        )
