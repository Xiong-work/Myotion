import pyqtgraph as pg
from PySide6.QtWidgets import QWidget, QVBoxLayout


_EVENT_PEN = pg.mkPen(color="#ffa500", width=1)


class PlayPlotWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.lo = QVBoxLayout()
        self.playline = []     # {line: InfiniteLine, rate: float} — playback cursor per plot
        self._plot_refs = []   # {widget: PlotWidget, rate: float} — track for event placement
        self._event_markers = []  # {event: TrialEvent, items: [(widget, InfiniteLine)…]}
        self.setLayout(self.lo)
        self.pen = pg.mkPen(color="#586cdb", width=2)

    def add_line(self, x, y, name, type="emg", rate=1):
        plt = pg.plot(x, y, name=name, pen=self.pen, clear=True, autoDownsample=True)
        self.playline.append({'line': plt.addLine(x=5, pen=pg.mkPen(color="r", width=2)), 'rate': rate})
        self._plot_refs.append({'widget': plt, 'rate': rate})
        plt.setBackground("#e5ecf6")
        plt.setRange(xRange=[0, max(x)])
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

    def clear(self):
        """Remove all plots and reset tracking state."""
        while self.lo.count():
            widget = self.lo.itemAt(0).widget()
            self.lo.removeWidget(widget)
            widget.close()
        self.playline.clear()
        self._plot_refs.clear()
        self._event_markers.clear()
