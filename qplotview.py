import plotly
import plotly.express as px
from plotly.subplots import make_subplots
from plotly.graph_objects import Figure, Scatter, FigureWidget
from PySide6.QtCore import (
    QCoreApplication,
    QDate,
    QDateTime,
    QLocale,
    QMetaObject,
    QObject,
    QPoint,
    QRect,
    QByteArray,
    QBuffer,
    QIODevice,
    QSize,
    QTime,
    QUrl,
    Qt,
)
from PySide6.QtGui import QBrush, QColor, QCursor, QFont, QIcon, QPalette
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import (
    QWebEngineUrlScheme,
    QWebEngineUrlSchemeHandler,
    QWebEngineUrlRequestJob,
    QWebEngineProfile,
    QWebEnginePage,
    QWebEngineDownloadRequest,
)
import os
from PySide6.QtWidgets import (
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolBox,
    QSizePolicy,
    QHBoxLayout,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
    QTreeView,
    QScrollArea,
    QPushButton,
    QFileDialog,
    QLabel,
)
import math

from PySide6.QtWidgets import QVBoxLayout, QWidget, QStackedWidget
import pandas as pd

# from modules.pyMotion import logger as logger

# set max points
PLOTY_MAX_POINTS = -1
# url scheme name
URL_SCHEME = "local"
# axis label
X_LABEL = "Time(s)"
Y_LABEL = "Magnitude"

# Injected into every plot HTML: adds a small "Grid" toggle button to the Plotly modebar
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


# html load handler with custom scheme
class UrlSchemeHandler(QWebEngineUrlSchemeHandler):
    def __init__(self, parent):
        super(UrlSchemeHandler, self).__init__(parent)
        self.data = None

    def setHtml(self, data):
        self.data = str(data).encode()

    def requestStarted(self, job):
        mime = QByteArray(b"text/html")
        buffer = QBuffer(job)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        buffer.write(self.data)
        job.reply(mime, buffer)


# Plot timerSeriesTable using plotly
class QPlotView(QWebEngineView):
    def __init__(self, parent=None):
        super(QPlotView, self).__init__(parent)
        self.parent = parent
        self.fig = None

        # save context — set via set_save_path() before user clicks the camera icon
        self._save_dir = None
        self._save_name = None

        # Profile has NO Qt parent — its lifetime is managed by Python's reference
        # count on self.profile.  This guarantees the destruction order:
        #   1. Qt destroys the view → deletes its Qt children (page, reparented
        #      here by setPage) → page is removed from the profile's page list.
        #   2. Python releases the view wrapper → self.profile refcount hits 0
        #      → profile C++ object is deleted → page list is already empty
        #      → no "Release of profile requested but WebEnginePage still not
        #         deleted" warning.
        # The scheme handler is parented to the profile so it is always alive
        # while the profile is alive (Qt docs require handler to outlive profile).
        self.profile = QWebEngineProfile()

        # install handler — parent = profile, not self
        self.schemeHandler = UrlSchemeHandler(self.profile)
        self.schemeHandler.setHtml("<html><body></body></html>")
        self.profile.installUrlSchemeHandler(
            bytes(URL_SCHEME, "ascii"), self.schemeHandler
        )

        # redirect Plotly camera-icon downloads to the participant folder
        self.profile.downloadRequested.connect(self._on_download_requested)

        # create new page with no Qt parent; setPage() reparents it into self
        self._page = QWebEnginePage(self.profile)
        self.setPage(self._page)

        # set URL
        self.url = QUrl("any_url_works_to_trigger_handler")
        self.url.setScheme(URL_SCHEME)
        self.setUrl(self.url)

    def set_save_path(self, directory, filename_stem):
        """Set the default directory and suggested filename for the next camera-icon save.

        Args:
            directory: absolute path to the participant folder (created on demand)
            filename_stem: suggested base name without extension (e.g. 'L-Ant_full_wave_rect')
        """
        self._save_dir = directory
        self._save_name = filename_stem

    def _on_download_requested(self, download):
        """Intercept Plotly camera-icon downloads and show a save-as dialog."""
        suggested_dir = self._save_dir or os.path.expanduser("~")
        suggested_name = (self._save_name or "plot") + ".png"
        suggested_path = os.path.join(suggested_dir, suggested_name)

        path, _ = QFileDialog.getSaveFileName(
            None,
            "Save Plot",
            suggested_path,
            "PNG Images (*.png)",
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

    # style setting
    def update_layout(self, y_label=Y_LABEL, xrange=None):
        self.fig.update_layout(
            legend=dict(
                yanchor="bottom", xanchor="center", y=-0.5, x=0.95,
                orientation="h", title="channel",
            )
        )
        self.fig.update_layout(yaxis_title=y_label)
        # No grid by default; user can toggle via the injected button
        self.fig.update_xaxes(showgrid=False)
        self.fig.update_yaxes(showgrid=False)
        if xrange is not None:
            self.fig.update_layout(xaxis_range=xrange)

    # bar, plot by timeSeriesTable
    def bar(self, tst, channel, title="", xlabel=X_LABEL, ylabel=Y_LABEL, color=[]):
        chans = []
        if type(channel) is not list:
            chans = [channel]

        for c in chans:
            if not tst.hasChannel(c):
                return -1

        df = tst.toPandasFrame()
        df[xlabel] = tst.getLinspace()
        df = df[:PLOTY_MAX_POINTS]
        self.fig = px.bar(
            df, x=xlabel, y=chans, barmode="relative", title=title, markers=True
        )
        self.update_layout(ylabel)
        return 0

    # bar, plot by list
    def bar(self, x_, y_, channel, title="", xlabel=X_LABEL, ylabel=Y_LABEL, color=[]):
        chans = []
        data = []
        # type and sanity check
        if type(channel) is not list:
            chans = [channel]
        else:
            chans = channel
        if len(y_) == 0:
            return -1
        if type(y_[0]) is list:
            n = len(y_[0])
            m = len(y_)
            data = y_
        else:
            n = len(y_)
            m = 1
            data = [y_]
        if n != len(x_):
            return -1
        if m != len(chans):
            return -1
        table = {}
        for i in range(0, m):
            table[chans[i]] = data[i]
        df = pd.DataFrame(table)
        df[xlabel] = x_
        df = df[:PLOTY_MAX_POINTS]
        self.fig = px.bar(df, x=xlabel, y=chans, title=title, barmode="relative")
        self.update_layout(ylabel)
        return 0

    # line, plot by timeSeriesTable
    def line(self, tst, channel, title="", xlabel=X_LABEL, ylabel=Y_LABEL, color=[]):
        chans = []
        if type(channel) is not list:
            chans = [channel]

        for c in chans:
            if not tst.hasChannel(c):
                return -1

        df = tst.toPandasFrame()
        df[xlabel] = tst.getLinspace()
        df = df[:PLOTY_MAX_POINTS]
        self.fig = px.line(
            df, x=xlabel, y=chans, title=title, markers=False, ender_mode="webgl"
        )
        self.update_layout(ylabel)
        return 0

    # line, plot by list
    def line(self, x_, y_, channel, title="", xlabel=X_LABEL, ylabel=Y_LABEL, color=[], xrange=None):
        chans = []
        data = []
        # type and sanity check
        if type(channel) is not list:
            chans = [channel]
        else:
            chans = channel
        if len(y_) == 0:
            return -1
        if type(y_[0]) is list:
            n = len(y_[0])
            m = len(y_)
            data = y_
        else:
            n = len(y_)
            m = 1
            data = [y_]
        if n != len(x_):
            return -1
        if m != len(chans):
            return -1
        table = {}
        for i in range(0, m):
            table[chans[i]] = data[i]
        df = pd.DataFrame(table)
        df[xlabel] = x_
        df = df[:PLOTY_MAX_POINTS]
        self.fig = px.line(
            df, x=xlabel, y=chans, title=title, markers=False, render_mode="webgl",
            color_discrete_sequence=color if color else None,
        )
        self.update_layout(ylabel, xrange=xrange)
        return 0

    # display on webEngine
    def show(self):
        html = plotly.io.to_html(self.fig, include_plotlyjs=True)
        # Inject grid-toggle button into the Plotly modebar
        html = html.replace("</body>", _GRID_TOGGLE_JS + "</body>")
        self.schemeHandler.setHtml(html)

        self.setUrl(self.url)
        self.update()

    def hide(self):
        html = "<html><body></body></html>"
        self.setHtml(html)
        self.update()


# QPlotViews with subpages in scrollAreas
class QPlotMultiViewSubPages(QStackedWidget):
    def __init__(self, scroll_=True, parent=None):
        self.parent = parent
        super(QPlotMultiViewSubPages, self).__init__(parent)
        self.plots = []  # list of plots
        self.stacked_widgets = []  # list of stacked_widget

        self.scroll = scroll_
        self.plot_per_page = 0  # 0 means display all in on page
        self.num_page = 0
        self.currentpage = -1

        self.repaint = True
        self.del_icon = QIcon()
        self.del_icon.addFile(
            ":/icons/images/icons/cil-x.png", QSize(), QIcon.Normal, QIcon.Off
        )

    def clear(self):
        for i in range(0, self.count()):
            sc = self.widget(i)
            self.removeWidget(sc)
            sc.deleteLater()
        self.plots.clear()

    def show(self):
        if self.repaint:
            # pop out plots and delete old widget
            for p in self.plots:
                p.setParent(None)
            for w in self.stacked_widgets:
                w.deleteLater()
                self.removeWidget(w)
            self.stacked_widgets.clear()

            # create widgets for stacked
            idx = 0
            for i in range(0, self.num_page):
                sc = QScrollArea(self)
                sc.setStyleSheet(
                    "QScrollBar::handle:vertical{\n" "	background-color:#595c64;\n" "}"
                )
                sc.setWidgetResizable(True)
                content = QWidget()
                content.setGeometry(QRect(0, 0, 811, 622))
                vLayout = QVBoxLayout(content)
                vLayout.setContentsMargins(10, 10, 10, 10)

                if self.plot_per_page:
                    plots_max_idx = min(len(self.plots), idx + self.plot_per_page)
                else:
                    plots_max_idx = len(self.plots)

                for j in range(idx, plots_max_idx):
                    # line widget (plot + del_btn)
                    line = QWidget()
                    hLayout = QHBoxLayout(line)
                    hLayout.addWidget(self.plots[j])
                    # add delete button
                    del_btn = QPushButton()
                    del_btn.setObjectName("{}".format(j))
                    del_btn.setCursor(QCursor(Qt.PointingHandCursor))
                    del_btn.setStyleSheet(
                        "background-color:rgba(0,0,0,0.8);\n" "margin:3px 2px;"
                    )
                    del_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
                    del_btn.setIcon(self.del_icon)
                    del_btn.clicked.connect(self.__btnDeletePage)
                    hLayout.addWidget(del_btn)

                    vLayout.addWidget(line)

                idx += self.plot_per_page
                # add page index
                page_index = QLabel()
                page_index.setStyleSheet(
                    "font-weight: bold;\n" "font-size:16px;color:rgba(0,0,0,0.4);"
                )
                page_index.setText("{}/{}".format(i + 1, self.num_page))
                vLayout.addWidget(page_index, alignment=Qt.AlignHCenter)

                sc.setWidget(content)

                self.stacked_widgets.append(sc)
                self.addWidget(sc)

            self.repaint = False

        # update display index
        self.setCurrentIndex(self.currentpage)

    def __updatePageSetting(self):
        if self.plot_per_page == 0:
            if self.size():
                self.num_page = 1
            else:
                self.num_page = 0
        else:
            self.num_page = math.ceil(len(self.plots) / self.plot_per_page)

    def append(self, plot):
        plot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        plot.show()
        self.plots.append(plot)
        self.repaint = True
        self.__updatePageSetting()

    def setPlotsPerPage(self, num):
        if num == self.plot_per_page:
            return
        self.plot_per_page = num
        self.repaint = True
        self.__updatePageSetting()

    def plotsPerPage(self):
        return self.plot_per_page

    def currentPage(self):
        return self.currentpage

    def setScroll(self, st):
        self.scroll = st
        self.repaint = True

    def pages(self):
        return self.num_page

    def size(self):
        return len(self.plots)

    def setCurrentPage(self, index):
        if index == self.currentPage:
            return
        self.currentpage = index
        if self.currentpage >= self.num_page or self.currentpage < 0:
            self.currentpage = 0

    def nextPage(self):
        if self.currentpage + 1 < self.num_page:
            self.currentpage += 1

    def prevPage(self):
        if self.currentpage > 0:
            self.currentpage -= 1

    def __btnDeletePage(self):
        del_btn = self.sender()
        idx = int(del_btn.objectName())
        self.deletePage(idx)
        self.show()

    def deletePage(self, index):
        if index < 0 or index >= self.size():
            return
        del self.plots[index]
        self.repaint = True
        self.__updatePageSetting()

    def deleteAllPages(self):
        if self.size() == 0:
            return
        self.plots.clear()
        self.repaint = True
        self.__updatePageSetting()
