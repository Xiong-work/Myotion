import os as _os

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QScrollArea, QSizePolicy, QLabel, QGraphicsOpacityEffect,
)

# Same branded "no data" watermark as renderwidget.py's 3D-viewport placeholder
# and the EMG module's idle plot canvas (qplotview.py), for a consistent look.
_LOGO_PATH = _os.path.normpath(
    _os.path.join(_os.path.dirname(__file__), "..", "..", "myotion_resources", "myotion_logo_origin.png")
)
_PLACEHOLDER_LOGO_OPACITY = 0.12

# ── Pen / color constants ─────────────────────────────────────────────────────
_EVENT_PEN  = pg.mkPen(color="#ffa500", width=1.5)
_CYCLE_START_PEN = pg.mkPen(color="#2ecc71", width=1.5)  # green — CycleStart_*
_CYCLE_END_PEN   = pg.mkPen(color="#e74c3c", width=1.5)  # red   — CycleEnd_*
_ONSET_PEN  = pg.mkPen(color="#55cc77", width=1.5, style=Qt.PenStyle.DashLine)
_OFFSET_PEN = pg.mkPen(color="#cc77cc", width=1.5, style=Qt.PenStyle.DashLine)
_CURSOR_PEN = pg.mkPen(color="#e63946", width=2)
_TRACE_PEN  = pg.mkPen(color="#586cdb", width=1.5)
_PICK_CROSSHAIR_PEN = pg.mkPen(color="#00b8d4", width=1, style=Qt.PenStyle.DashLine)

# Axis colours — readable on the light (#e5ecf6) plot background
_AXIS_PEN  = pg.mkPen(color="#b0b8c8", width=1)
_TEXT_PEN  = pg.mkPen(color="#444444")

# Minimum pixel height for each subplot row
_MIN_ROW_H = 160


def _make_plot(name: str, x_label: str) -> pg.PlotWidget:
    """Create a consistently styled PlotWidget."""
    plt = pg.PlotWidget()

    # Inner ViewBox background
    plt.setBackground("#e5ecf6")
    # Outer axis-margin area (no dark border leak)
    plt.setStyleSheet("background-color: #edf2f8; border: none;")

    for ax in ("left", "bottom", "right", "top"):
        plt.getAxis(ax).setPen(_AXIS_PEN)
        plt.getAxis(ax).setTextPen(_TEXT_PEN)
        plt.getAxis(ax).setStyle(tickFont=pg.QtGui.QFont("Segoe UI", 8))

    plt.getAxis("right").setStyle(showValues=False)
    plt.getAxis("top").setStyle(showValues=False)

    plt.setTitle(name, color="#444", size="9pt")
    plt.setLabel("left",   "Magnitude", color="#555", size="9pt")
    plt.setLabel("bottom", x_label,     color="#555", size="9pt")
    plt.showGrid(x=True, y=True, alpha=0.22)

    plt.setMinimumHeight(_MIN_ROW_H)
    plt.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    return plt


class PlayPlotWidget(QWidget):
    """
    pyqtgraph-based signal viewer for the kinematics inspection panel.
    Renders instantly with a frame-accurate playback cursor.
    """

    def __init__(self):
        super().__init__()
        self.setStyleSheet("background-color: #ffffff;")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Scroll area so many subplots don't get squashed
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(
            "QScrollArea { border: none; background: #ffffff; }"
            "QScrollBar:vertical { background: #edf2f8; width: 8px; }"
            "QScrollBar::handle:vertical { background: #b0b8c8; border-radius: 4px; }"
        )
        outer.addWidget(self._scroll)

        # Inner container that holds the plot widgets
        self._container = QWidget()
        self._container.setStyleSheet("background-color: #ffffff;")
        self.lo = QVBoxLayout(self._container)
        self.lo.setContentsMargins(4, 4, 4, 4)
        self.lo.setSpacing(6)
        self._scroll.setWidget(self._container)

        # Placeholder watermark shown when there's nothing to plot yet --
        # same branded "no data" look as the 3D viewport and EMG's plots.
        self.placeholder = QLabel()
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _pix = QPixmap(_LOGO_PATH)
        if not _pix.isNull():
            self.placeholder.setPixmap(
                _pix.scaled(
                    220, 220,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            _opacity_effect = QGraphicsOpacityEffect(self.placeholder)
            _opacity_effect.setOpacity(_PLACEHOLDER_LOGO_OPACITY)
            self.placeholder.setGraphicsEffect(_opacity_effect)
        else:
            self.placeholder.setText("No data to plot")
        self.lo.addWidget(self.placeholder)

        # State
        self.playline      = []   # {'line': InfiniteLine, 'rate': float}
        self._plot_refs    = []   # {'widget': PlotWidget,  'rate': float}
        self._event_markers = []  # {'event': TrialEvent, 'items': [(plt, line)…]}
        self._onset_markers = []  # (plt, InfiniteLine)
        self._cycle_markers_visible = True  # toggled by set_cycle_markers_visible()
        self._pick_callback = None    # armed by enable_time_picking(), see below
        self._pick_cancel_callback = None  # called on a right-click cancel
        self._pick_crosshairs = []    # (widget, InfiniteLine) -- one per subplot, follows the mouse
        self._pick_markers = []       # [[(widget, InfiniteLine), …], …] -- persisted picked points
        # Trial-wide kinematic frame rate -- the one fixed quantity needed to
        # convert a TrialEvent's time_s (always seconds) into each subplot's
        # own x-axis units (frames for "marker" plots, seconds for
        # "channel" plots). See _event_x() for why this can't just reuse the
        # per-subplot "rate" stored in playline/_plot_refs.
        self._kinematic_fps = 1.0

    # ── Public API ────────────────────────────────────────────────────────

    def set_kinematic_fps(self, fps):
        """Call once per participant load (Controller does this) so event
        markers land on the right x position regardless of subplot type."""
        self._kinematic_fps = float(fps) if fps else 1.0

    def add_line(self, x, y, name, type="emg", rate=1):
        if self.placeholder.isVisible():
            self.lo.removeWidget(self.placeholder)
            self.placeholder.hide()

        x_label = "Frame" if type == "marker" else "Time (s)"
        plt = _make_plot(name, x_label)

        plt.plot(x, y, pen=_TRACE_PEN, autoDownsample=False)
        plt.setXRange(0, max(x) if len(x) and max(x) > 0 else 1)

        cursor = plt.addLine(x=0, pen=_CURSOR_PEN)
        self.playline.append({"line": cursor, "rate": float(rate)})
        self._plot_refs.append({"widget": plt, "rate": float(rate)})

        # Draw events that were already registered before this subplot was added
        for em in self._event_markers:
            line = plt.addLine(x=self._event_x(em["event"].time_s, rate), pen=self._pen_for_event(em["event"]))
            if self._is_cycle_event(em["event"]) and not self._cycle_markers_visible:
                line.setVisible(False)
            em["items"].append((plt, line))

        self.lo.addWidget(plt)

    def _event_x(self, time_s, rate):
        """Convert a TrialEvent's time_s (always seconds) to this subplot's
        own x-axis units.

        "rate" is playline/_plot_refs's cursor-update rate. Its contract is
        fixed by update() (frame / rate), which must stay correct: rate=1
        for a "marker" subplot (x-axis is frame index, so the raw frame
        count IS the x position) or rate=kinematic_fps for a "channel"
        subplot (x-axis is seconds, so frame / fps = elapsed seconds).
        That's the opposite of what a seconds-based event needs, so convert
        through the trial's one fixed kinematic fps instead of using rate
        directly:
            x = time_s * kinematic_fps / rate
        marker (rate=1):               x = time_s * kinematic_fps  (seconds -> frame index)
        channel (rate=kinematic_fps):  x = time_s                  (already seconds)
        """
        return time_s * self._kinematic_fps / (rate or 1.0)

    @staticmethod
    def _is_cycle_event(event):
        label = event.label or ""
        return label.startswith("CycleStart_") or label.startswith("CycleEnd_")

    @staticmethod
    def _pen_for_event(event):
        label = event.label or ""
        if label.startswith("CycleStart_"):
            return _CYCLE_START_PEN
        if label.startswith("CycleEnd_"):
            return _CYCLE_END_PEN
        return _EVENT_PEN

    def set_cycle_markers_visible(self, visible):
        """Show/hide CycleStart_/CycleEnd_ markers without deleting them --
        wired to the playbar's "Show Cycles" toggle."""
        self._cycle_markers_visible = visible
        for em in self._event_markers:
            if self._is_cycle_event(em["event"]):
                for _, line in em["items"]:
                    line.setVisible(visible)

    def update(self, frame: int):
        """Move all playback cursors to the given kinematic frame."""
        for entry in self.playline:
            entry["line"].setValue(frame / entry["rate"])

    def add_event(self, event):
        pen = self._pen_for_event(event)
        hide = self._is_cycle_event(event) and not self._cycle_markers_visible
        items = []
        for pr in self._plot_refs:
            line = pr["widget"].addLine(x=self._event_x(event.time_s, pr["rate"]), pen=pen)
            if hide:
                line.setVisible(False)
            items.append((pr["widget"], line))
        self._event_markers.append({"event": event, "items": items})

    def remove_event(self, event):
        for em in list(self._event_markers):
            if em["event"] is event:
                for plt, line in em["items"]:
                    try:
                        plt.removeItem(line)
                    except Exception:
                        pass
                self._event_markers.remove(em)
                return

    def clear_events(self):
        for em in self._event_markers:
            for plt, line in em["items"]:
                try:
                    plt.removeItem(line)
                except Exception:
                    pass
        self._event_markers.clear()

    def enable_time_picking(self, callback, on_cancel=None):
        """Arm single-shot ginput-style time picking: the next left-click on
        any currently-plotted subplot calls callback(time_s) with the
        clicked x-position converted to seconds, then disarms itself. Used
        by ManualCyclesDialog's "Pick" buttons as an alternative to typing
        exact start/end numbers.

        A right-click instead cancels: disarms without calling callback,
        and calls on_cancel() (if given) so the caller can reset its own
        UI state (e.g. a "Start…"/"End…" button label).

        While armed, a vertical-only crosshair (time is all that matters
        for a cycle boundary, not the signal's magnitude) follows the mouse
        over whichever subplot it's hovering. The subplot's own right-click
        context menu is suppressed while armed so it doesn't pop up on top
        of the cancel gesture."""
        self.disable_time_picking()
        self._pick_callback = callback
        self._pick_cancel_callback = on_cancel
        for pr in self._plot_refs:
            widget = pr["widget"]
            widget.getViewBox().setMenuEnabled(False)
            crosshair = pg.InfiniteLine(angle=90, movable=False, pen=_PICK_CROSSHAIR_PEN)
            crosshair.setVisible(False)
            widget.addItem(crosshair)
            self._pick_crosshairs.append((widget, crosshair))
            widget.scene().sigMouseMoved.connect(self._on_pick_move)
            widget.scene().sigMouseClicked.connect(self._on_pick_click)

    def disable_time_picking(self):
        """Disarm time picking without invoking either callback (e.g. dialog closed)."""
        if self._pick_callback is None:
            return
        for pr in self._plot_refs:
            try:
                pr["widget"].scene().sigMouseMoved.disconnect(self._on_pick_move)
                pr["widget"].scene().sigMouseClicked.disconnect(self._on_pick_click)
            except (TypeError, RuntimeError):
                pass
            try:
                pr["widget"].getViewBox().setMenuEnabled(True)
            except Exception:
                pass
        for widget, crosshair in self._pick_crosshairs:
            try:
                widget.removeItem(crosshair)
            except Exception:
                pass
        self._pick_crosshairs = []
        self._pick_callback = None
        self._pick_cancel_callback = None

    def _on_pick_move(self, pos):
        for widget, crosshair in self._pick_crosshairs:
            vb = widget.getViewBox()
            if vb.sceneBoundingRect().contains(pos):
                crosshair.setPos(vb.mapSceneToView(pos).x())
                crosshair.setVisible(True)
            else:
                crosshair.setVisible(False)

    def _on_pick_click(self, ev):
        callback = self._pick_callback
        if callback is None:
            return
        if ev.button() == Qt.MouseButton.RightButton:
            cancel_cb = self._pick_cancel_callback
            self.disable_time_picking()
            if cancel_cb:
                cancel_cb()
            return
        pos = ev.scenePos()
        for pr in self._plot_refs:
            vb = pr["widget"].getViewBox()
            if vb.sceneBoundingRect().contains(pos):
                x_view = vb.mapSceneToView(pos).x()
                # Inverse of _event_x's x = time_s * kinematic_fps / rate
                time_s = x_view * pr["rate"] / (self._kinematic_fps or 1.0)
                self.disable_time_picking()
                callback(time_s)
                return

    def show_pick_marker(self, time_s, color):
        """Drop a persistent vertical marker (kept until clear_pick_markers()
        or the next enable_time_picking()) -- used by ManualCyclesDialog to
        show a just-picked start/end time so the user gets instant visual
        confirmation while working through a row."""
        lines = []
        pen = pg.mkPen(color=color, width=1.5)
        for pr in self._plot_refs:
            line = pr["widget"].addLine(x=self._event_x(time_s, pr["rate"]), pen=pen)
            lines.append((pr["widget"], line))
        self._pick_markers.append(lines)

    def clear_pick_markers(self):
        for lines in self._pick_markers:
            for widget, line in lines:
                try:
                    widget.removeItem(line)
                except Exception:
                    pass
        self._pick_markers = []

    def clear_onset_offset(self):
        for plt, line in self._onset_markers:
            try:
                plt.removeItem(line)
            except Exception:
                pass
        self._onset_markers.clear()

    def add_onset_offset(self, onset_times_s, offset_times_s):
        for t in onset_times_s:
            for pr in self._plot_refs:
                line = pr["widget"].addLine(x=t, pen=_ONSET_PEN)
                self._onset_markers.append((pr["widget"], line))
        for t in offset_times_s:
            for pr in self._plot_refs:
                line = pr["widget"].addLine(x=t, pen=_OFFSET_PEN)
                self._onset_markers.append((pr["widget"], line))

    def clear(self):
        """Remove all subplots, reset state, and restore the placeholder."""
        self.disable_time_picking()
        self._pick_markers.clear()  # their parent subplot widgets are about to be destroyed below
        while self.lo.count():
            item = self.lo.takeAt(0)
            w = item.widget()
            if w is not None and w is not self.placeholder:
                w.deleteLater()
        self.playline.clear()
        self._plot_refs.clear()
        self._event_markers.clear()
        self._onset_markers.clear()
        self.lo.addWidget(self.placeholder)
        self.placeholder.show()
