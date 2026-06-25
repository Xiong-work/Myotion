from PySide6.QtCore import (
    QCoreApplication,
    QDate,
    QDateTime,
    QLocale,
    QMetaObject,
    QObject,
    QPoint,
    QRect,
    QSize,
    QTimer,
    QUrl,
    Qt,
    QEvent,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QConicalGradient,
    QCursor,
    QFont,
    QFontDatabase,
    QGradient,
    QIcon,
    QImage,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPalette,
    QPixmap,
    QRadialGradient,
    QTransform,
    QMovie
)
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMenu,
    QMenuBar,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QToolBar,
    QMessageBox,
    QDialog,
    QWidget,
    QFileDialog,
    QTableWidgetItem,
    QComboBox,
    QLineEdit,
    QCompleter,
    QCheckBox,
    QLabel,
)

from path import *


class ScaledImageLabel(QLabel):
    """QLabel that scales its pixmap to fill the available space while keeping aspect ratio."""

    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        self._source = pixmap
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(
            QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._source and not self._source.isNull():
            self.setPixmap(
                self._source.scaled(
                    self.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )


class LoadingGif(QLabel):
    loading = None

    def __init__(self, w, h, parent=None): 
        super(LoadingGif, self).__init__(parent)
        if self.loading is None:
            self.loading = QMovie("loader.gif")
  
        # Loading the GIF
        self.l = self.loading.scaled(w, h, Qt.KeepAspectRatio)
        self.label.setMovie(self.l) 
  
    def start(self): 
        self.movie.start()

    def stop(self): 
        self.movie.stop()

class STATUS():
    Failed = 0
    Passed = 1
    Loading = 2

class statusLED(QLabel):
    red = None
    green = None
    loadingPix = None
    loadingMov = None

    def __init__(self, w, h, status=STATUS.Failed, parent=None):
        super(statusLED, self).__init__(parent)

        if self.red is None:
            self.red = QPixmap(IconPath + "/redcross.png")
        if self.green is None:
            self.green = QPixmap(IconPath + "/greencheckmark.png")
        if self.loadingPix is None:
            self.loadingPix = QPixmap(IconPath + "/loading.gif")
        if self.loadingMov is None:
            self.loadingMov = QMovie(IconPath + "/loading.gif")


        self.g = self.green.scaled(w, h, Qt.KeepAspectRatio)
        self.r = self.red.scaled(w, h, Qt.KeepAspectRatio)
        self.lp = self.loadingPix.scaled(w, h, Qt.KeepAspectRatio)
        self.lm = self.loadingMov
        # use pixmap to get actual size for qmovie
        self.lm.setScaledSize(QSize(self.lp.width(),self.lp.height()))
        self.status = status
        self.show()

    def set(self, status):
        self.status = status
        self.show()

    def show(self):
        if self.status == STATUS.Failed:
            self.setPixmap(self.r)
        elif self.status == STATUS.Passed:
            self.setPixmap(self.g)
        else:
            self.setMovie(self.lm)
            self.lm.start()
