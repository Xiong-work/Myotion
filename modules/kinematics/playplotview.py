import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout


_EVENT_PEN  = pg.mkPen(color="#ffa500", width=1)
_ONSET_PEN  = pg.mkPen(color="#55cc77", width=1.2, style=Qt.PenStyle.DashLine)
_OFFSET_PEN = pg.mkPen(color="#cc77cc", width=1.2, style=Qt.PenStyle.DashLine)


class PlayPlotWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.lo = QVBoxLayout()
        self.playline = []        # {line: InfiniteLine, rate: float} — playback cursor per plot
        self._plot_refs = []      # {widget: PlotWidget, rate: float} — track for event placement
        self._event_markers = []  # {event: TrialEvent, items: [(widget, InfiniteLine)…]}
        self._onset_markers = []  # (widget, InfiniteLine) for onset/offset detection lines
        self.setLayout(self.lo)
        self.pen = pg.mkPen(color="#586cdb", width=2)

    def add_line(self, x, y, name, type="emg", rate=1):
        plt = pg.PlotWidget()

        # --- Plotly-style appearance ---
        # Inner ViewBox background matches EMG plot background
        plt.setBackground("#e5ecf6")
        # Outer widget area (axis margins) also light so there's no dark border
        plt.setStyleSheet("background-color: #f0f4fb; border: none;")

        # Dark axis pens and labels on the light background
        _text_pen = pg.mkPen(color="#444")
        _axis_pen = pg.mkPen(color="#bbb", width=1)
        for ax_name in ("left", "bottom", "right", "top"):
            ax = plt.getAxis(ax_name)
            ax.setPen(_axis_pen)
            ax.setTextPen(_text_pen)
        plt.getAxis("right").setStyle(showValues=False)
        plt.getAxis("top").setStyle(showValues=False)

        # Channel name as subplot title (e.g. "BF-L.x")
        plt.setTitle(name, color="#444", size="9pt")

        x_label = "Frame" if type == "marker" else "Time(s)"
        plt.setLabel("left", "Magnitude", color="#555", size="9pt")
        plt.setLabel("bottom", x_label, color="#555", size="9pt")

        # Subtle grid to match Plotly's light gridlines
        plt.showGrid(x=True, y=True, alpha=0.18)

        # Plot the data
        plt.plot(x, y, name=name, pen=self.pen, autoDownsample=True)
        plt.setXRange(0, max(x) if len(x) and max(x) > 0 else 1)

        # Playback cursor (red vertical line)
        cursor = plt.addLine(x=0, pen=pg.mkPen(color="r", width=2))
        self.playline.append({'line': cursor, 'rate': rate})
        self._plot_refs.append({'widget': plt, 'rate': rate})

        # Draw any already-registered events onto this new plot
        for em in self._event_markers:
            x_pos = em['event'].time_s * rate
            line = plt.addLine(x=x_pos, pen=_EVENT_PEN)
            em['items'].append((plt, line))

        self.lo.addWidget(plt)

    def update(self, frame):
        for line in self.playline:
            line['line'].setValue(frame / line['rate'])

    def add_event(self, event):
        """Draw an orange vertical marker for event on all active plots."""
        items = []
        for pr in self._plot_refs:
            x_pos = event.time_s * pr['rate']
            line = pr['widget'].addLine(x=x_pos, pen=_EVENT_PEN)
            items.append((pr['widget'], line))
        self._event_markers.append({'event': event, 'items': items})

    def remove_event(self, event):
        """Remove a specific event's marker from all plots."""
        for em in list(self._event_markers):
            if em['event'] is event:
                for plt, line in em['items']:
                    try:
                        plt.removeItem(line)
                    except Exception:
                        pass
                self._event_markers.remove(em)
                return

    def clear_events(self):
        """Remove all event markers from all plots."""
        for em in self._event_markers:
            for plt, line in em['items']:
                try:
                    plt.removeItem(line)
                except Exception:
                    pass
        self._event_markers.clear()

    def clear_onset_offset(self):
        """Remove onset/offset dashed lines from all plots without clearing the plots."""
        for plt, line in self._onset_markers:
            try:
                plt.removeItem(line)
            except Exception:
                pass
        self._onset_markers.clear()

    def add_onset_offset(self, onset_times_s, offset_times_s):
        """Draw dashed onset (green) and offset (purple) lines on all active plots.

        Times are in seconds, matching the EMG x-axis produced by
        np.arange(len(signal)) / fs.  Lines are tracked in _onset_markers
        and removed automatically by clear().
        """
        for t in onset_times_s:
            for pr in self._plot_refs:
                line = pr['widget'].addLine(x=t, pen=_ONSET_PEN)
                self._onset_markers.append((pr['widget'], line))
        for t in offset_times_s:
            for pr in self._plot_refs:
                line = pr['widget'].addLine(x=t, pen=_OFFSET_PEN)
                self._onset_markers.append((pr['widget'], line))

    def clear(self):
        """Remove all plots and reset tracking state."""
        while self.lo.count():
            item = self.lo.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.playline.clear()
        self._plot_refs.clear()
        self._event_markers.clear()
        self._onset_markers.clear()
