"""
modules/advanced_emg/chart_view.py — Plotly rendering surface for Advanced
EMG Analysis, isolated from modules/stats/stats_widget.py's StatsChartView.

Registers its own "advemg" URL scheme (distinct from the app-wide "local"
scheme used by QPlotView/StatsChartView) so this new tab's WebEngine usage
cannot interact with any other tab's scheme/profile. Scheme registration
happens at import time, which is required to run before QApplication is
constructed -- satisfied here because main.py imports ui_main.py (and
therefore this module, via AdvancedAnalysisWidget) before creating the
QApplication.

Grid-toggle and camera-save behavior mirror qplotview.py's QPlotView (used by
the EMG Time Domain plots) so the interaction is consistent across the app.
"""

import os

from PySide6.QtCore import QUrl, QByteArray, QBuffer, QIODevice
from PySide6.QtWidgets import QFileDialog
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import (
    QWebEngineUrlScheme, QWebEngineProfile, QWebEnginePage,
    QWebEngineUrlSchemeHandler, QWebEngineUrlRequestJob,
)

_SCHEME = "advemg"

_scheme = QWebEngineUrlScheme(bytes(_SCHEME, "ascii"))
_scheme.setFlags(
    QWebEngineUrlScheme.Flag.SecureScheme
    | QWebEngineUrlScheme.Flag.LocalScheme
    | QWebEngineUrlScheme.Flag.LocalAccessAllowed
)
QWebEngineUrlScheme.registerScheme(_scheme)

# Injected into every plot HTML: adds a small "Grid" toggle button to the
# Plotly modebar (same behavior as qplotview.py's _GRID_TOGGLE_JS).
_GRID_TOGGLE_JS = """
<script>
(function() {
    function addGridButton() {
        var gds = document.querySelectorAll('.js-plotly-plot');
        if (!gds.length) { setTimeout(addGridButton, 250); return; }
        var allReady = true;
        gds.forEach(function(gd) {
            if (gd._myGridBtn) return;
            var modebar = gd.querySelector('.modebar-group');
            if (!modebar) { allReady = false; return; }
            var btn = document.createElement('a');
            btn.className = 'modebar-btn';
            btn.title = 'Toggle Grid';
            btn.style.cssText = 'cursor:pointer;font-size:13px;padding:2px 5px;opacity:0.4;';
            btn.textContent = '⋮⋯';
            btn._on = false;
            btn.addEventListener('click', function(e) {
                e.preventDefault();
                btn._on = !btn._on;
                btn.style.opacity = btn._on ? '1' : '0.4';
                Plotly.relayout(gd, {
                    'xaxis.showgrid': btn._on,
                    'yaxis.showgrid': btn._on
                });
            });
            modebar.insertBefore(btn, modebar.firstChild);
            gd._myGridBtn = btn;
        });
        if (!allReady) setTimeout(addGridButton, 250);
    }
    window.addEventListener('load', function() { setTimeout(addGridButton, 300); });
})();
</script>
"""


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


class AdvancedChartView(QWebEngineView):
    """Renders plotly figures via the isolated 'advemg' scheme."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._save_name = None  # suggested filename stem for the next camera-icon save

        self._profile = QWebEngineProfile()
        self._handler = _HtmlSchemeHandler(self._profile)
        self._profile.installUrlSchemeHandler(bytes(_SCHEME, "ascii"), self._handler)
        # Redirect Plotly camera-icon downloads to a native save dialog.
        self._profile.downloadRequested.connect(self._on_download_requested)
        # QWebEngineView.setPage() does not reparent the page (it only stores a
        # raw pointer), so a local `page` variable gets garbage-collected the
        # moment __init__ returns -- silently killing the page with no error.
        # Must keep a Python reference alive for the view's lifetime.
        self._page = QWebEnginePage(self._profile)
        self.setPage(self._page)
        self._url = QUrl(f"{_SCHEME}://advanced-emg-chart")
        self.show_placeholder("Load a data source, then run an analysis.")

    def show_figure(self, fig, filename_stem: str | None = None):
        from modules.stats.chart_builder import figure_to_html
        html = figure_to_html(fig)
        html = html.replace("</body>", _GRID_TOGGLE_JS + "</body>")
        self._handler.set_html(html)
        self._save_name = filename_stem
        self.setUrl(self._url)

    def show_placeholder(self, msg: str = ""):
        from modules.stats.chart_builder import empty_html
        self._handler.set_html(empty_html(msg))
        self._save_name = None
        self.setUrl(self._url)

    def _on_download_requested(self, download):
        """Intercept Plotly camera-icon downloads and show a save-as dialog,
        defaulting to the current working directory."""
        suggested_name = (self._save_name or "plot") + ".png"
        suggested_path = os.path.join(os.getcwd(), suggested_name)

        path, _ = QFileDialog.getSaveFileName(
            self, "Save Plot", suggested_path, "PNG Images (*.png)",
        )
        if not path:
            download.cancel()
            return

        save_dir = os.path.dirname(path)
        save_name = os.path.basename(path)
        os.makedirs(save_dir, exist_ok=True)
        download.setDownloadDirectory(save_dir)
        download.setDownloadFileName(save_name)
        download.accept()
