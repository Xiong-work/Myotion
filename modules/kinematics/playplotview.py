import os as _os

import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
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
# Gait Analysis events (IC_L/TO_L/IC_R/TO_R, see gait_analysis_dialog.py) --
# one color per side so initial-contact and toe-off on the same foot are
# visually grouped, and easy to tell apart from the opposite foot's. Chosen
# to stay distinct from the playbar/trace colors and from each other.
_GAIT_LEFT_PEN  = pg.mkPen(color="#2e7d32", width=1.5)  # green — IC_L / TO_L
_GAIT_RIGHT_PEN = pg.mkPen(color="#000000", width=1.5)  # black — IC_R / TO_R
_ONSET_PEN  = pg.mkPen(color="#55cc77", width=1.5, style=Qt.PenStyle.DashLine)
_OFFSET_PEN = pg.mkPen(color="#cc77cc", width=1.5, style=Qt.PenStyle.DashLine)
_CURSOR_PEN = pg.mkPen(color="#e63946", width=2)
_TRACE_PEN  = pg.mkPen(color="#586cdb", width=1.5)
_PICK_CROSSHAIR_PEN = pg.mkPen(color="#00b8d4", width=1, style=Qt.PenStyle.DashLine)

# Axis colours — readable on the light (#e5ecf6) plot background
_AXIS_PEN  = pg.mkPen(color="#b0b8c8", width=1)
_TEXT_PEN  = pg.mkPen(color="#444444")

# Cycling palette for overlay mode — each additional same-kind trace layered
# onto the shared overlay subplot gets the next color, wrapping around.
_OVERLAY_COLORS = [
    "#586cdb", "#e07b39", "#2ecc71", "#e63946",
    "#9b59b6", "#17a2b8", "#f1c40f", "#8d6e63",
]

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


_CROP_REGION_BRUSH = pg.mkBrush(255, 165, 0, 60)   # translucent orange fill
_CROP_REGION_PEN   = pg.mkPen(color="#e07b39", width=1.5)


class PlayPlotWidget(QWidget):
    """
    pyqtgraph-based signal viewer for the kinematics inspection panel.
    Renders instantly with a frame-accurate playback cursor.
    """

    # Emitted (start_s, end_s) whenever the crop region is dragged on any
    # subplot -- see set_crop_mode(). Not emitted for set_crop_range_s()'s
    # own programmatic updates.
    cropRangeChanged = Signal(float, float)

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
        # Overlay mode: newly added traces of the same "kind" (marker/angle/
        # emg/force_plate — see Controller.tree_item_select) are layered onto
        # this one shared subplot in a new color instead of each getting its
        # own row. None when no overlay subplot exists yet (or after clear()).
        self._overlay_widget = None
        self._overlay_kind = None
        self._overlay_color_idx = 0
        self._overlay_items = {}      # trace name -> PlotDataItem, for replacing a re-plotted name
        # Trial-wide kinematic frame rate -- the one fixed quantity needed to
        # convert a TrialEvent's time_s (always seconds) into each subplot's
        # own x-axis units (frames for "marker" plots, seconds for
        # "channel" plots). See _event_x() for why this can't just reuse the
        # per-subplot "rate" stored in playline/_plot_refs.
        self._kinematic_fps = 1.0

        # Crop mode: a draggable pg.LinearRegionItem mirrored onto every
        # visible subplot (each converted to that subplot's own x units --
        # frame index for markers, seconds for channels -- via
        # _region_x_from_seconds/_region_seconds_from_x), kept in sync so
        # dragging any one of them moves them all. See set_crop_mode().
        self._crop_mode = False
        self._crop_regions = []       # [{"widget":, "region":, "rate":}, ...]
        self._crop_range_s = None     # (t0, t1) -- current crop bounds while active
        self._syncing_crop = False    # reentrancy guard for the sync loop below
        # Event-marker crop filter: (t0, t1) to hide markers outside that
        # window, or None to show all -- independent of _crop_mode/
        # _crop_range_s above (that's the draggable region shown only while
        # actively cropping; this stays in effect for as long as a crop is
        # applied, or previewed while dragging). See set_crop_event_filter().
        self._crop_event_filter = None

    # ── Public API ────────────────────────────────────────────────────────

    def set_kinematic_fps(self, fps):
        """Call once per participant load (Controller does this) so event
        markers land on the right x position regardless of subplot type."""
        self._kinematic_fps = float(fps) if fps else 1.0

    def can_overlay(self, kind):
        """True if a trace of *kind* may be layered onto the current overlay
        subplot -- either there isn't one yet, or it already holds that same
        kind. False means the caller should warn instead of calling
        add_line(overlay=True, ...) for it (mixing e.g. EMG onto a marker
        overlay would put mV and mm on one Y axis)."""
        return self._overlay_widget is None or self._overlay_kind == kind

    @property
    def overlay_kind(self):
        """The kind currently held by the overlay subplot, or None."""
        return self._overlay_kind

    def add_line(self, x, y, name, type="emg", rate=1, overlay=False, kind=None):
        """Add a trace. If overlay=True and can_overlay(kind) held true when
        the caller checked, the trace is layered onto the shared overlay
        subplot in the next palette color (replacing any existing trace of
        the same *name*) instead of creating a new subplot row.

        Returns True if a brand-new subplot widget was created, False if the
        trace was layered onto an already-existing overlay widget. The
        caller uses this to know whether event markers need (re)drawing on
        it -- an existing overlay widget already has them.
        """
        if self.placeholder.isVisible():
            self.lo.removeWidget(self.placeholder)
            self.placeholder.hide()

        if overlay and self._overlay_widget is not None and self._overlay_kind == kind:
            old_item = self._overlay_items.pop(name, None)
            if old_item is not None:
                try:
                    self._overlay_widget.removeItem(old_item)
                except Exception:
                    pass
            pen = pg.mkPen(color=_OVERLAY_COLORS[self._overlay_color_idx % len(_OVERLAY_COLORS)], width=1.5)
            self._overlay_color_idx += 1
            item = self._overlay_widget.plot(x, y, pen=pen, name=name, autoDownsample=False)
            self._overlay_items[name] = item
            return False

        x_label = "Frame" if type == "marker" else "Time (s)"
        # Overlay subplot holds several differently-named traces at once --
        # a title fixed to the first trace's name would be misleading once
        # more are layered on, so use a generic one and rely on the legend
        # (added below) to label each trace.
        plt = _make_plot("Overlay" if overlay else name, x_label)

        if overlay:
            plt.addLegend(offset=(10, 10))
            self._overlay_widget = plt
            self._overlay_kind = kind
            self._overlay_color_idx = 0
            self._overlay_items = {}
            pen = pg.mkPen(color=_OVERLAY_COLORS[0], width=1.5)
            self._overlay_color_idx = 1
            item = plt.plot(x, y, pen=pen, name=name, autoDownsample=False)
            self._overlay_items[name] = item
        else:
            plt.plot(x, y, pen=_TRACE_PEN, autoDownsample=False)
        plt.setXRange(0, max(x) if len(x) and max(x) > 0 else 1)

        cursor = plt.addLine(x=0, pen=_CURSOR_PEN)
        self.playline.append({"line": cursor, "rate": float(rate)})
        self._plot_refs.append({"widget": plt, "rate": float(rate)})

        if self._crop_mode and self._crop_range_s is not None:
            self._add_crop_region_to(plt, float(rate), *self._crop_range_s)

        # Draw events that were already registered before this subplot was added
        for em in self._event_markers:
            line = plt.addLine(x=self._event_x(em["event"].time_s, rate), pen=self._pen_for_event(em["event"]))
            if not self._marker_visible(em["event"]):
                line.setVisible(False)
            em["items"].append((plt, line))

        self.lo.addWidget(plt)
        return True

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
        if label.startswith("IC_L") or label.startswith("TO_L"):
            return _GAIT_LEFT_PEN
        if label.startswith("IC_R") or label.startswith("TO_R"):
            return _GAIT_RIGHT_PEN
        return _EVENT_PEN

    def _marker_visible(self, event):
        """Whether *event*'s marker line should currently be shown --
        combines the cycle-markers toggle (set_cycle_markers_visible) and
        the crop event filter (set_crop_event_filter); either one can hide
        it, neither deletes it."""
        if self._is_cycle_event(event) and not self._cycle_markers_visible:
            return False
        if self._crop_event_filter is not None:
            t0, t1 = self._crop_event_filter
            if not (t0 <= event.time_s <= t1):
                return False
        return True

    def _refresh_all_marker_visibility(self):
        for em in self._event_markers:
            visible = self._marker_visible(em["event"])
            for _, line in em["items"]:
                line.setVisible(visible)

    def set_cycle_markers_visible(self, visible):
        """Show/hide CycleStart_/CycleEnd_ markers without deleting them --
        wired to the playbar's "Show Cycles" toggle."""
        self._cycle_markers_visible = visible
        self._refresh_all_marker_visibility()

    def set_crop_event_filter(self, range_s):
        """Show/hide every event marker based on whether its time falls
        inside range_s = (t0, t1), or show all when range_s is None --
        keeps the plot's event lines in sync with an active or in-progress-
        drag crop range (see GaitAnalysisDialog's crop feature) without
        touching the events themselves. Cheap enough to call on every drag
        signal, unlike a full metrics recompute."""
        self._crop_event_filter = tuple(range_s) if range_s is not None else None
        self._refresh_all_marker_visibility()

    def update(self, frame: int):
        """Move all playback cursors to the given kinematic frame."""
        for entry in self.playline:
            entry["line"].setValue(frame / entry["rate"])

    def add_event(self, event):
        pen = self._pen_for_event(event)
        hide = not self._marker_visible(event)
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
        self._overlay_widget = None
        self._overlay_kind = None
        self._overlay_color_idx = 0
        self._overlay_items = {}
        # The region items themselves are children of the subplot widgets
        # just deleteLater()'d above -- just drop our own references.
        self._crop_mode = False
        self._crop_regions = []
        self._crop_range_s = None
        self._crop_event_filter = None
        self.lo.addWidget(self.placeholder)
        self.placeholder.show()

    # ── Crop region (mouse-drag) ─────────────────────────────────────────────

    def _region_x_from_seconds(self, t, rate):
        return t * self._kinematic_fps / (rate or 1.0)

    def _region_seconds_from_x(self, x, rate):
        return x * (rate or 1.0) / self._kinematic_fps

    def _add_crop_region_to(self, widget, rate, t0, t1):
        region = pg.LinearRegionItem(
            values=[self._region_x_from_seconds(t0, rate), self._region_x_from_seconds(t1, rate)],
            brush=_CROP_REGION_BRUSH, pen=_CROP_REGION_PEN,
        )
        region.setZValue(100)
        widget.addItem(region)
        entry = {"widget": widget, "region": region, "rate": rate}
        self._crop_regions.append(entry)
        region.sigRegionChanged.connect(lambda: self._on_crop_region_changed(entry))

    def _on_crop_region_changed(self, changed_entry):
        if self._syncing_crop:
            return
        self._syncing_crop = True
        try:
            x0, x1 = changed_entry["region"].getRegion()
            t0 = self._region_seconds_from_x(x0, changed_entry["rate"])
            t1 = self._region_seconds_from_x(x1, changed_entry["rate"])
            if t1 < t0:
                t0, t1 = t1, t0
            self._crop_range_s = (t0, t1)
            for entry in self._crop_regions:
                if entry is changed_entry:
                    continue
                entry["region"].setRegion([
                    self._region_x_from_seconds(t0, entry["rate"]),
                    self._region_x_from_seconds(t1, entry["rate"]),
                ])
            self.cropRangeChanged.emit(t0, t1)
        finally:
            self._syncing_crop = False

    def set_crop_mode(self, enabled, initial_range_s=None):
        """Show/hide the draggable crop region on every currently visible
        subplot (and any added afterward while still enabled -- see
        add_line()). initial_range_s: (t0, t1) in seconds, required when
        enabling."""
        if enabled == self._crop_mode and enabled:
            return
        if not enabled:
            for entry in self._crop_regions:
                try:
                    entry["widget"].removeItem(entry["region"])
                except Exception:
                    pass
            self._crop_regions = []
            self._crop_mode = False
            return
        self._crop_mode = True
        self._crop_range_s = tuple(initial_range_s) if initial_range_s else (0.0, 1.0)
        for entry in self._plot_refs:
            self._add_crop_region_to(entry["widget"], entry["rate"], *self._crop_range_s)

    def get_crop_range_s(self):
        """Current crop bounds in seconds, or None if crop mode is off or
        there's no subplot to read a region from."""
        if not self._crop_mode or not self._crop_regions:
            return None
        return self._crop_range_s

    def set_crop_range_s(self, t0, t1):
        """Programmatically move the crop region (e.g. loading a previously
        saved profile.crop_interval) without emitting cropRangeChanged."""
        self._crop_range_s = (t0, t1)
        self._syncing_crop = True
        try:
            for entry in self._crop_regions:
                entry["region"].setRegion([
                    self._region_x_from_seconds(t0, entry["rate"]),
                    self._region_x_from_seconds(t1, entry["rate"]),
                ])
        finally:
            self._syncing_crop = False
