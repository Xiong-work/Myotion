"""widgets/gait_report_dialog.py -- the "Create Report..." popup for Gait
Analysis (see gait_analysis_dialog.py). Builds a clinically-styled report
(header/logo/participant info, gait phase/spatial/time parameters, a gait-
cycle illustration, joint-angle curves across the gait cycle, EMG activity,
muscle co-contraction) as a matplotlib Figure shown live in the dialog,
exportable as a PDF. Layout is modeled on a reference clinical gait report
template (grouped Left/Right/Diff% parameter rows with a shaded literature
reference band).

Every section is conditional on having something real to show -- there is no
simulated/placeholder content anywhere in this report. Joint-angle curves
only appear for a joint with a recognizable real Model Output (Angle) on at
least one side (see gait_events.guess_joint_angle_label); EMG activity only
appears if there are EMG channels with cycle data; Co-contraction Index rows
only appear for pairs (picked ahead of time via GaitAnalysisDialog's
"Co-contraction..." button, see _CCIPairsDialog) that yield a real value.
The gait-cycle illustration is built from this trial's own averaged phase
percentages, so it's skipped too when phase percentages couldn't be computed.
"""

import os

import numpy as np

from PySide6.QtCore import QDate
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QPushButton, QLabel,
    QLineEdit, QComboBox, QSpinBox, QDateEdit, QSplitter, QWidget, QFileDialog,
    QMessageBox, QScrollArea,
)

import matplotlib.image as mpimg
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

from modules.pyMotion.core import gait_events as _gait

_LOGO_PREVIEW_SIZE = 90
_JOINTS = ["Hip", "Knee", "Ankle"]
_SIDE_COLOR = {"Right": "#5b9bd5", "Left": "#f2a154"}
_CCI_METHODS = [("Rudolph et al. (2000)", "rudolph"), ("Falconer & Winter (1985)", "falconer_winter")]
_CCI_METHOD_LABELS = {key: label for label, key in _CCI_METHODS}
# Trial Max listed (and thus defaulted to) first -- Rudolph's formula scales
# with the signals' raw amplitude, so left at "None" (raw envelope, ~1e-5 V)
# it reads as an indistinguishable-from-zero number by default; Trial Max
# is the sane out-of-the-box choice, "None" is there for when a raw-scale
# comparison is actually wanted.
_CCI_NORMALIZE_OPTIONS = [("Trial Max", "trial_max"), ("None (raw envelope)", "none")]
_CCI_NORMALIZE_LABELS = {key: label for label, key in _CCI_NORMALIZE_OPTIONS}

# Same default branding used elsewhere in the app (see renderwidget.py's
# placeholder watermark) -- pre-filled here so a report doesn't start blank;
# "Choose Logo..." still lets the user swap in something else.
_DEFAULT_LOGO_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "myotion_resources", "myotion_logo_origin.png")
)

# Rough literature reference ranges for healthy adult overground walking
# (commonly cited textbook figures, e.g. Perry's Gait Analysis / Whittle's
# Gait Analysis: An Introduction). These are NOT validated for any specific
# patient population and are shown purely as visual context -- see the
# caption drawn under each parameter group in the report.
_REFERENCE_RANGES = {
    "stance_pct": (60.0, 62.0),
    "swing_pct": (38.0, 40.0),
    "loading_response_pct": (10.0, 12.0),
    "pre_swing_pct": (10.0, 12.0),
    "single_support_pct": (38.0, 40.0),
    "double_support_pct": (20.0, 24.0),
    "step_length_m": (0.55, 0.80),
    "stride_length_m": (1.10, 1.60),
    "step_width_m": (0.07, 0.11),
    "velocity_m_s": (1.00, 1.60),
    "cadence_spm": (100.0, 130.0),
    "step_time_s": (0.45, 0.60),
    "stride_time_s": (0.90, 1.20),
}
_REFERENCE_CAPTION = (
    "Shaded band = typical healthy-adult reference range (literature). "
    "Context only -- not a diagnostic threshold or validated for this patient."
)


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


def _diff_pct(left, right):
    if np.isnan(left) or np.isnan(right):
        return float("nan")
    avg = (left + right) / 2.0
    return ((right - left) / avg * 100.0) if avg != 0 else float("nan")


def _row_has_data(row):
    if "single" in row:
        return not np.isnan(row["single"][0])
    return not (np.isnan(row["left"][0]) and np.isnan(row["right"][0]))


class _GaitReportDialog(QDialog):
    def __init__(self, spatio, phases, toe_out, step_agg, hs_by_side, to_by_side, kin, fs_k,
                 emg_means, emg_envelopes, cci_pairs, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Create Gait Report"))
        self.resize(1200, 900)

        self._spatio = spatio
        self._phases = phases
        self._toe_out = toe_out
        self._step_agg = step_agg
        self._hs_by_side = hs_by_side
        self._to_by_side = to_by_side
        self._kin = kin
        self._fs_k = fs_k
        self._emg_means = emg_means
        self._emg_envelopes = emg_envelopes
        self._cci_pairs = cci_pairs
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

        # Report content keeps growing (phase/spatial/time parameter groups,
        # joint-angle curves, EMG, CCI) -- a fixed-size canvas in a scroll
        # area, rather than one that keeps shrinking every row to fit the
        # dialog, is what keeps each row readable instead of squeezed thin.
        self._figure = Figure(figsize=(8.5, 24))
        self._canvas = FigureCanvasQTAgg(self._figure)
        self._canvas.setMinimumSize(850, 2400)
        scroll = QScrollArea()
        scroll.setWidget(self._canvas)
        scroll.setWidgetResizable(False)
        splitter.addWidget(scroll)
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

    # ── Parameter row specs ──────────────────────────────────────────────────

    def _phase_rows(self):
        p = self._phases
        rows = []
        for key, label in (
            ("stance_pct", "Stance Phase"),
            ("loading_response_pct", "Loading Response"),
            ("single_support_pct", "Single Support"),
            ("pre_swing_pct", "Pre-Swing"),
            ("swing_pct", "Swing Phase"),
        ):
            rows.append({
                "label": label, "unit": "%", "left": p["Left"][key], "right": p["Right"][key],
                "ref": _REFERENCE_RANGES.get(key),
            })
        rows.append({
            "label": "Double Support", "unit": "%", "single": p["double_support_pct"],
            "ref": _REFERENCE_RANGES.get("double_support_pct"),
        })
        return [r for r in rows if _row_has_data(r)]

    def _spatial_rows(self):
        sa = self._step_agg
        stride = self._spatio["stride"]
        rows = [
            {"label": "Step Length", "unit": "m",
             "left": sa["Left"]["step_length_m"], "right": sa["Right"]["step_length_m"],
             "ref": _REFERENCE_RANGES.get("step_length_m")},
            {"label": "Stride Length", "unit": "m",
             "left": stride["Left"]["stride_length_m"], "right": stride["Right"]["stride_length_m"],
             "ref": _REFERENCE_RANGES.get("stride_length_m")},
            {"label": "Step Width", "unit": "m", "single": sa["step_width_m"],
             "ref": _REFERENCE_RANGES.get("step_width_m")},
            {"label": "Velocity", "unit": "m/s",
             "left": stride["Left"]["velocity_m_s"], "right": stride["Right"]["velocity_m_s"],
             "ref": _REFERENCE_RANGES.get("velocity_m_s")},
            {"label": "Toe-out Angle", "unit": "deg",
             "left": self._toe_out["Left"], "right": self._toe_out["Right"], "ref": None},
        ]
        return [r for r in rows if _row_has_data(r)]

    def _time_rows(self):
        sa = self._step_agg
        stride = self._spatio["stride"]
        rows = [
            {"label": "Step Time", "unit": "s",
             "left": sa["Left"]["step_time_s"], "right": sa["Right"]["step_time_s"],
             "ref": _REFERENCE_RANGES.get("step_time_s")},
            {"label": "Stride Time", "unit": "s",
             "left": stride["Left"]["stride_time_s"], "right": stride["Right"]["stride_time_s"],
             "ref": _REFERENCE_RANGES.get("stride_time_s")},
            {"label": "Cadence", "unit": "strides/min",
             "left": stride["Left"]["cadence_spm"], "right": stride["Right"]["cadence_spm"],
             "ref": _REFERENCE_RANGES.get("cadence_spm")},
        ]
        return [r for r in rows if _row_has_data(r)]

    def _phase_averages(self):
        """Average of Left/Right for the gait-cycle illustration, built from
        this trial's own numbers (never fabricated -- see module docstring).
        Returns ("full", lr, ss, ps, sw, is_fallback) when the double-
        support breakdown is available (needs both feet's events),
        ("simple", stance, swing, is_fallback) when only the coarser
        stance/swing split is (only needs one foot's own HS/TO), or None if
        neither could be computed. is_fallback is True if either side's
        numbers came from the approximate HS-to-opposite-foot-TO window
        (see gait_events.compute_phase_percentages) rather than a verified
        full gait cycle."""
        p = self._phases
        any_fallback = p.get("is_fallback", {}).get("Right", False) or p.get("is_fallback", {}).get("Left", False)

        def avg(key):
            vals = [v for v in (p["Left"][key][0], p["Right"][key][0]) if not np.isnan(v)]
            return float(np.mean(vals)) if vals else float("nan")

        lr, ss, ps, sw = avg("loading_response_pct"), avg("single_support_pct"), \
            avg("pre_swing_pct"), avg("swing_pct")
        if not any(np.isnan(v) for v in (lr, ss, ps, sw)):
            return "full", lr, ss, ps, sw, any_fallback
        stance, swing = avg("stance_pct"), avg("swing_pct")
        if not (np.isnan(stance) or np.isnan(swing)):
            return "simple", stance, swing, any_fallback
        return None

    # ── Co-contraction Index ─────────────────────────────────────────────────

    def _compute_cci_pair(self, a_name, b_name, side, method_key, norm_key):
        """Returns (cci_or_nan, is_fallback), or None if either channel has
        no enveloped signal. CCI is single-sided -- see gait_events.
        compute_cci_pair for the actual math (shared with the immediate
        calculation GaitAnalysisDialog does right after the Co-contraction
        pair picker is accepted) and the HS-to-opposite-TO fallback cycle."""
        if a_name not in self._emg_envelopes or b_name not in self._emg_envelopes:
            return None
        a_env, a_fs = self._emg_envelopes[a_name]
        b_env, b_fs = self._emg_envelopes[b_name]
        return _gait.compute_cci_pair(a_name, a_env, a_fs, b_name, b_env, b_fs,
                                       self._hs_by_side, self._to_by_side, side, method_key, norm_key)

    def _valid_cci_results(self):
        """[(a, b, side, method_key, norm_key, cci, is_fallback), ...] for
        every configured pair that yields a real (non-NaN) value."""
        results = []
        for a_name, b_name, side, method_key, norm_key in self._cci_pairs:
            res = self._compute_cci_pair(a_name, b_name, side, method_key, norm_key)
            if res is None:
                continue
            cci, is_fallback = res
            if not np.isnan(cci):
                results.append((a_name, b_name, side, method_key, norm_key, cci, is_fallback))
        return results

    # ── Drawing ──────────────────────────────────────────────────────────────

    def _add_section_title(self, fig, gs_cell, text):
        ax = fig.add_subplot(gs_cell)
        ax.axis("off")
        ax.text(0.0, 0.3, text, ha="left", va="center", fontsize=13, fontweight="bold",
                 transform=ax.transAxes)
        return ax

    def _add_caption(self, fig, gs_cell, text):
        ax = fig.add_subplot(gs_cell)
        ax.axis("off")
        ax.text(0.0, 0.5, text, ha="left", va="center", fontsize=7, style="italic",
                 color="#666666", transform=ax.transAxes)

    def _add_param_row(self, fig, gs_cell, spec):
        ax = fig.add_subplot(gs_cell)
        # Narrower than the full row width (unlike titles/captions/joint/EMG/
        # CCI, which use the figure's default wide margins) -- this reserves
        # room on the left for the parameter name, drawn outside the axes.
        pos = ax.get_position()
        ax.set_position([0.32, pos.y0, 0.60, pos.height])
        ref = spec.get("ref")

        # Bar tips/whiskers/reference band decide the axis span rather than
        # always starting at 0 -- most parameters here are non-negative, but
        # a few (e.g. Toe-out Angle) are legitimately signed, and a fixed
        # 0-start axis would clip a negative bar and its label off-screen.
        if "single" in spec:
            mean, sd = spec["single"]
            sd = 0.0 if np.isnan(sd) else sd
            lo, hi = min(0.0, mean - sd), max(0.0, mean + sd)
            if ref:
                lo, hi = min(lo, ref[0]), max(hi, ref[1])
            span = (hi - lo) or 1.0
            xlim_lo, xlim_hi = lo - span * 0.05, hi + span * 0.45
            if ref:
                ax.axvspan(ref[0], ref[1], color="#8bc34a", alpha=0.20, zorder=0)
            if lo < 0:
                ax.axvline(0, color="#cccccc", linewidth=0.6, zorder=0)
            ax.barh([0], [mean], xerr=[sd], color="#43a047", height=0.55, capsize=3,
                    error_kw={"elinewidth": 1})
            ax.text(hi + span * 0.05, 0, "{:.2f}±{:.2f}".format(mean, sd),
                    va="center", ha="left", fontsize=6.5)
            ax.set_yticks([])
            ax.set_ylim(-0.7, 0.7)
            ax.set_xlim(xlim_lo, xlim_hi)
        else:
            l_mean, l_sd = spec["left"]
            r_mean, r_sd = spec["right"]
            l_sd = 0.0 if np.isnan(l_sd) else l_sd
            r_sd = 0.0 if np.isnan(r_sd) else r_sd
            lo_candidates, hi_candidates = [0.0], [0.0]
            if not np.isnan(l_mean):
                lo_candidates.append(l_mean - l_sd)
                hi_candidates.append(l_mean + l_sd)
            if not np.isnan(r_mean):
                lo_candidates.append(r_mean - r_sd)
                hi_candidates.append(r_mean + r_sd)
            if ref:
                lo_candidates.append(ref[0])
                hi_candidates.append(ref[1])
            lo, hi = min(lo_candidates), max(hi_candidates)
            span = (hi - lo) or 1.0
            xlim_lo, xlim_hi = lo - span * 0.05, hi + span * 0.55
            if ref:
                ax.axvspan(ref[0], ref[1], color="#8bc34a", alpha=0.20, zorder=0)
            if lo < 0:
                ax.axvline(0, color="#cccccc", linewidth=0.6, zorder=0)
            if not np.isnan(l_mean):
                ax.barh([1], [l_mean], xerr=[l_sd], color=_SIDE_COLOR["Left"], height=0.36, capsize=3)
                ax.text(hi + span * 0.05, 1, "L {:.2f}±{:.2f}".format(l_mean, l_sd),
                        va="center", ha="left", fontsize=6.5)
            if not np.isnan(r_mean):
                ax.barh([0], [r_mean], xerr=[r_sd], color=_SIDE_COLOR["Right"], height=0.36, capsize=3)
                ax.text(hi + span * 0.05, 0, "R {:.2f}±{:.2f}".format(r_mean, r_sd),
                        va="center", ha="left", fontsize=6.5)
            diff = _diff_pct(l_mean, r_mean)
            if not np.isnan(diff):
                ax.text(hi + span * 0.30, 0.5, "Diff {:+.1f}%".format(diff),
                        va="center", ha="left", fontsize=6.5, color="#666666")
            ax.set_yticks([])
            ax.set_ylim(-0.7, 1.7)
            ax.set_xlim(xlim_lo, xlim_hi)

        label_text = "{} ({})".format(spec["label"], spec["unit"]) if spec.get("unit") else spec["label"]
        ax.text(-0.02, 0.5, label_text, transform=ax.transAxes, ha="right", va="center", fontsize=8)
        ax.tick_params(axis="x", labelsize=6)
        for spine in ("top", "right", "left"):
            ax.spines[spine].set_visible(False)
        ax.spines["bottom"].set_alpha(0.4)

    def _draw_gait_cycle_illustration(self, fig, gs_cell, averages):
        """A labeled timeline of this trial's own averaged phase segments --
        gives the parameter rows below it a visual reference for what each
        phase name means, using the same numbers rather than a generic stock
        diagram. Draws the full Loading Response/Single Support/Pre-Swing/
        Swing breakdown when double-support data is available, or a coarser
        Stance/Swing-only version when it isn't (see _phase_averages)."""
        kind = averages[0]
        ax = fig.add_subplot(gs_cell)
        if kind == "full":
            _, lr, ss, ps, sw, is_fallback = averages
            segments = [
                ("Loading\nResponse", lr, "#f9a97a"),
                ("Single\nSupport", ss, "#f2c14e"),
                ("Pre-\nSwing", ps, "#f9a97a"),
                ("Swing", sw, "#8ecae6"),
            ]
        else:
            _, stance, swing, is_fallback = averages
            segments = [
                ("Stance", stance, "#f2c14e"),
                ("Swing", swing, "#8ecae6"),
            ]
        x = 0.0
        for label, width, color in segments:
            if width <= 0:
                continue
            ax.barh(0, width, left=x, height=0.6, color=color, edgecolor="white")
            if width > 4.0:  # skip the label if the segment is too thin to hold it
                ax.text(x + width / 2, 0, label, ha="center", va="center", fontsize=7)
            x += width
        stance_end = (lr + ss + ps) if kind == "full" else stance
        total = max(x, 1e-6)
        for boundary, linestyle in ((0.0, "-"), (stance_end, "--"), (total, "-")):
            ax.axvline(boundary, color="black", linewidth=1, linestyle=linestyle)
        for boundary in (0.0, stance_end, total):
            ax.text(boundary, 0.55, "Heel Strike" if boundary != stance_end else "Toe Off",
                    ha="center", va="bottom", fontsize=6.5)
        ax.text(stance_end / 2, -0.5, "Stance Phase", ha="center", va="top", fontsize=8, fontweight="bold")
        ax.text((stance_end + total) / 2, -0.5, "Swing Phase", ha="center", va="top", fontsize=8, fontweight="bold")
        if is_fallback:
            ax.text(total / 2, 1.0, "Approximate -- HS-to-opposite-foot-TO window, not a verified full cycle",
                    ha="center", va="bottom", fontsize=6.5, style="italic", color="#a05a2c")
        ax.set_xlim(0, total)
        ax.set_ylim(-0.9, 1.1)
        ax.axis("off")

    def _update_preview(self, *_args):
        fig = self._figure
        fig.clear()
        info = self._header_info()

        phase_rows = self._phase_rows()
        spatial_rows = self._spatial_rows()
        time_rows = self._time_rows()
        illustration = self._phase_averages() if phase_rows else None
        joint_rows = [
            j for j in _JOINTS
            if any(_real_curve(self._kin, self._fs_k, self._hs_by_side, j, side) is not None
                   for side in ("Right", "Left"))
        ]
        cci_results = self._valid_cci_results()

        # One flattened single-column grid: section titles/captions get a
        # short row, each parameter gets its own small bar-chart row (own
        # x-scale -- units differ wildly between e.g. % and meters), joint
        # curves and the activity/CCI charts get generous room. Every
        # section is only included when there's something real to show --
        # see module docstring.
        sections = [("header", None)]
        if phase_rows:
            sections.append(("title", "Gait Phase Parameters"))
            if illustration is not None:
                sections.append(("illustration", illustration))
            sections += [("param", r) for r in phase_rows] + [("caption", None)]
        if spatial_rows:
            sections += [("title", "Gait Spatial Parameters")] + [("param", r) for r in spatial_rows] \
                + [("caption", None)]
        if time_rows:
            sections += [("title", "Gait Time Parameters")] + [("param", r) for r in time_rows] \
                + [("caption", None)]
        if joint_rows:
            sections.append(("title", "Joint Angles vs. Gait Cycle"))
            sections += [("joint", (j, j == joint_rows[-1])) for j in joint_rows]
        if self._emg_means:
            sections += [("title", "EMG Activity"), ("emg", None)]
        if cci_results:
            sections.append(("title", "Muscle Co-contraction Index"))
            sections += [("cci", item) for item in cci_results]

        height_map = {
            "header": 1.4, "title": 0.5, "param": 1.0, "caption": 0.4, "illustration": 1.6,
            "joint": 3.2, "emg": 1.8, "cci": 1.8,
        }
        height_ratios = [height_map[kind] for kind, _payload in sections]
        grid = fig.add_gridspec(len(sections), 1, height_ratios=height_ratios, hspace=1.6)
        # Full-width margins by default (titles/captions/header/joint/EMG/CCI
        # all use this); _add_param_row narrows its own axes afterward to
        # reserve room for the parameter name -- see its comment.
        fig.subplots_adjust(top=0.985, bottom=0.01, left=0.05, right=0.97)

        for i, (kind, payload) in enumerate(sections):
            cell = grid[i, 0]
            if kind == "header":
                self._draw_header(fig, cell, info)
            elif kind == "title":
                self._add_section_title(fig, cell, payload)
            elif kind == "caption":
                self._add_caption(fig, cell, _REFERENCE_CAPTION)
            elif kind == "illustration":
                self._draw_gait_cycle_illustration(fig, cell, payload)
            elif kind == "param":
                self._add_param_row(fig, cell, payload)
            elif kind == "joint":
                joint, is_last = payload
                self._draw_joint_row(fig, cell, joint, is_last)
            elif kind == "emg":
                self._draw_emg_row(fig, cell)
            elif kind == "cci":
                self._draw_cci_row(fig, cell, payload)

        self._canvas.draw_idle()

    def _draw_header(self, fig, gs_cell, info):
        ax = fig.add_subplot(gs_cell)
        ax.axis("off")
        if self._logo_path:
            try:
                img = mpimg.imread(self._logo_path)
                # Small and semi-transparent so it reads as a watermark
                # rather than competing with (or covering) the title/info
                # text -- a bigger, fully opaque logo used to run into the
                # "Name/Sex/Age/..." line.
                logo_ax = fig.add_axes([0.045, 0.965, 0.06, 0.022])
                logo_ax.imshow(img, alpha=0.55)
                logo_ax.axis("off")
            except Exception:
                pass
        ax.text(0.5, 0.7, self.tr("Gait Analysis Report"), ha="center", va="center",
                 fontsize=18, fontweight="bold", transform=ax.transAxes)
        info_line = self.tr(
            "Name: {name}    Sex: {sex}    Age: {age}    Date: {date}    Created by: {author}"
        ).format(**info)
        ax.text(0.5, 0.15, info_line, ha="center", va="center", fontsize=10, transform=ax.transAxes)

    def _draw_joint_row(self, fig, gs_cell, joint, is_last):
        ax = fig.add_subplot(gs_cell)
        for side in ("Right", "Left"):
            real = _real_curve(self._kin, self._fs_k, self._hs_by_side, joint, side)
            if real is None:
                continue
            pct, mean, std = real
            ax.plot(pct, mean, color=_SIDE_COLOR[side], label="{} (measured)".format(side))
            ax.fill_between(pct, mean - std, mean + std, color=_SIDE_COLOR[side], alpha=0.15)
        ax.set_title("{} angle vs. gait cycle".format(joint), fontsize=10)
        # Only the bottom-most joint panel gets an x-axis label -- all three
        # share the same 0-100% axis, so repeating it on every panel just
        # collided with the title of the panel below it.
        if is_last:
            ax.set_xlabel("Gait cycle (%)", fontsize=8)
        else:
            ax.set_xticklabels([])
        ax.set_ylabel("Angle (deg)", fontsize=8)
        ax.tick_params(labelsize=7)
        ax.legend(fontsize=7, loc="upper right")
        ax.axhline(0, color="#cccccc", linewidth=0.8)

    def _draw_emg_row(self, fig, gs_cell):
        """Grouped Right/Left bars, mean +/- SD across each side's own gait
        cycles (IC of one foot to the next IC of that SAME foot -- see
        gait_events.cycles_from_hs) -- not one number blended across both
        feet's cycles, which would mix two different feet's muscle activity
        into a meaningless average."""
        ax = fig.add_subplot(gs_cell)
        chans = list(self._emg_means.keys())
        x = np.arange(len(chans))
        width = 0.35

        def side_values(side):
            # NaN mean (channel wasn't recorded on this side -- see
            # GaitAnalysisDialog._emg_channel_side) is left as NaN rather
            # than zeroed: matplotlib simply skips drawing that bar, instead
            # of showing a misleading "0 activity" for a leg with no sensor.
            means, sds = [], []
            for c in chans:
                m, s = self._emg_means[c][side]
                means.append(m)
                sds.append(0.0 if np.isnan(s) else s)
            return means, sds

        r_means, r_sds = side_values("Right")
        l_means, l_sds = side_values("Left")
        ax.bar(x - width / 2, r_means, width, yerr=r_sds, capsize=3,
               color=_SIDE_COLOR["Right"], label="Right")
        ax.bar(x + width / 2, l_means, width, yerr=l_sds, capsize=3,
               color=_SIDE_COLOR["Left"], label="Left")
        ax.set_xticks(x)
        ax.set_xticklabels(chans, rotation=45, ha="right", fontsize=7)
        ax.set_ylabel("Mean envelope", fontsize=8)
        ax.set_title("Mean EMG activity per gait cycle, by side (measured)", fontsize=10)
        ax.legend(fontsize=7)
        ax.tick_params(labelsize=7)

    def _draw_cci_row(self, fig, gs_cell, item):
        """One bar -- CCI is single-sided (see _CCIPairsDialog): a pair is
        only ever computed for the leg it was configured for, using that
        leg's own gait cycles, so there's no Right+Left pair of numbers
        here like the other report sections."""
        a_name, b_name, side, method_key, norm_key, cci, is_fallback = item
        ax = fig.add_subplot(gs_cell)
        ax.barh([0], [cci], color=_SIDE_COLOR[side], height=0.5)
        ax.text(cci, 0, " {:.3f}".format(cci), va="center", ha="left", fontsize=8)
        ax.set_yticks([])
        ax.set_ylim(-0.7, 0.7)
        vmax = max(cci, 0.0) * 1.35 + 1e-6
        vmin = min(cci, 0.0) * 1.35 - 1e-6 if cci < 0 else 0.0
        ax.set_xlim(vmin, vmax)
        ax.set_xlabel("CCI", fontsize=8)
        fallback_note = " -- approximate (HS-to-opposite-TO window)" if is_fallback else ""
        ax.set_title(
            "Co-contraction Index: {} vs {} -- {} ({}, {}){}".format(
                a_name, b_name, side, _CCI_METHOD_LABELS.get(method_key, method_key),
                _CCI_NORMALIZE_LABELS.get(norm_key, norm_key), fallback_note,
            ),
            fontsize=10,
        )
        ax.tick_params(labelsize=8)

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
