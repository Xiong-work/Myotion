import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QScrollArea, QSizePolicy

# ── Pen / color constants ─────────────────────────────────────────────────────
_EVENT_PEN  = pg.mkPen(color="#ffa500", width=1.5)
_ONSET_PEN  = pg.mkPen(color="#55cc77", width=1.5, style=Qt.PenStyle.DashLine)
_OFFSET_PEN = pg.mkPen(color="#cc77cc", width=1.5, style=Qt.PenStyle.DashLine)
_CURSOR_PEN = pg.mkPen(color="#e63946", width=2)
_TRACE_PEN  = pg.mkPen(color="#586cdb", width=1.5)

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
        self.setStyleSheet("background-color: #21242b;")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Scroll area so many subplots don't get squashed
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(
            "QScrollArea { border: none; background: #21242b; }"
            "QScrollBar:vertical { background: #2c3039; width: 8px; }"
            "QScrollBar::handle:vertical { background: #495474; border-radius: 4px; }"
        )
        outer.addWidget(self._scroll)

        # Inner container that holds the plot widgets
        self._container = QWidget()
        self._container.setStyleSheet("background-color: #21242b;")
        self.lo = QVBoxLayout(self._container)
        self.lo.setContentsMargins(4, 4, 4, 4)
        self.lo.setSpacing(6)
        self._scroll.setWidget(self._container)

        # State
        self.playline      = []   # {'line': InfiniteLine, 'rate': float}
        self._plot_refs    = []   # {'widget': PlotWidget,  'rate': float}
        self._event_markers = []  # {'event': TrialEvent, 'items': [(plt, line)…]}
        self._onset_markers = []  # (plt, InfiniteLine)

    # ── Public API ────────────────────────────────────────────────────────

    def add_line(self, x, y, name, type="emg", rate=1):
        x_label = "Frame" if type == "marker" else "Time (s)"
        plt = _make_plot(name, x_label)

        plt.plot(x, y, pen=_TRACE_PEN, autoDownsample=False)
        plt.setXRange(0, max(x) if len(x) and max(x) > 0 else 1)

        cursor = plt.addLine(x=0, pen=_CURSOR_PEN)
        self.playline.append({"line": cursor, "rate": float(rate)})
        self._plot_refs.append({"widget": plt, "rate": float(rate)})

        # Draw events that were already registered before this subplot was added
        for em in self._event_markers:
            line = plt.addLine(x=em["event"].time_s * rate, pen=_EVENT_PEN)
            em["items"].append((plt, line))

        self.lo.addWidget(plt)

    def update(self, frame: int):
        """Move all playback cursors to the given kinematic frame."""
        for entry in self.playline:
            entry["line"].setValue(frame / entry["rate"])

    def add_event(self, event):
        items = []
        for pr in self._plot_refs:
            line = pr["widget"].addLine(x=event.time_s * pr["rate"], pen=_EVENT_PEN)
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
        """Remove all subplots and reset state."""
        while self.lo.count():
            item = self.lo.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.playline.clear()
        self._plot_refs.clear()
        self._event_markers.clear()
        self._onset_markers.clear()
