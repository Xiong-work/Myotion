"""
modules/stats/chart_view.py — shared plotly-in-Qt chart surface for the
Stats module. Used by both stats_widget.py (workspace data) and
imported_panel.py (externally imported data) so the QWebEngineView/profile
plumbing isn't duplicated.
"""

from PySide6.QtCore import QUrl, QByteArray, QBuffer, QIODevice
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import (
    QWebEngineProfile, QWebEnginePage,
    QWebEngineUrlSchemeHandler, QWebEngineUrlRequestJob,
)

from .chart_builder import figure_to_html, empty_html


class _HtmlSchemeHandler(QWebEngineUrlSchemeHandler):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._data = b"<html><body></body></html>"

    def set_html(self, html: str):
        self._data = html.encode("utf-8")

    def requestStarted(self, job: QWebEngineUrlRequestJob):
        buf = QBuffer(job)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        buf.write(self._data)
        job.reply(QByteArray(b"text/html"), buf)


class StatsChartView(QWebEngineView):
    """Renders plotly figures using the same local:// scheme as QPlotView."""

    def __init__(self, parent=None, placeholder: str = "Assign participants to groups, then select a metric."):
        super().__init__(parent)
        # No Qt parent for profile — Python reference-counting controls its
        # lifetime.  Handler parented to profile (must outlive the profile).
        # setPage() reparents the page into self's Qt child tree, so page is
        # destroyed before the profile when the view is torn down.
        self._profile = QWebEngineProfile()
        self._handler = _HtmlSchemeHandler(self._profile)
        self._profile.installUrlSchemeHandler(b"local", self._handler)
        page = QWebEnginePage(self._profile)
        self.setPage(page)
        self._url = QUrl("local://stats-chart")
        self.show_placeholder(placeholder)

    def show_figure(self, fig):
        self._handler.set_html(figure_to_html(fig))
        self.setUrl(self._url)

    def show_placeholder(self, msg: str = ""):
        self._handler.set_html(empty_html(msg))
        self.setUrl(self._url)

    def showEvent(self, event):
        """Stats tabs are built once at app start, often while still hidden
        inside a QStackedWidget/QSplitter with a zero-size viewport -- Plotly
        can end up laying out its chart into that zero-size container and
        never repainting once the tab actually becomes visible. Nudging the
        page with a resize event once we're actually shown makes Plotly
        recompute its layout against the real (non-zero) size."""
        super().showEvent(event)
        self.page().runJavaScript(
            "window.dispatchEvent(new Event('resize'));"
            "if (window.Plotly) {"
            "  document.querySelectorAll('.js-plotly-plot').forEach("
            "    function(gd) { Plotly.Plots.resize(gd); }"
            "  );"
            "}"
        )
