# ///////////////////////////////////////////////////////////////
#
# BY: WANDERSON M.PIMENTA
# PROJECT MADE WITH: Qt Designer and PySide6
# V: 1.0.0
#
# This project can be used freely for all uses, as long as they maintain the
# respective credits only in the Python scripts, any information in the visual
# interface (GUI) can be modified without any implication.
#
# There are limitations on Qt licenses if you want to use your products
# commercially, I recommend reading them on the official website:
# https://doc.qt.io/qtforpython/licenses.html
#
# ///////////////////////////////////////////////////////////////

import sys
import os
import re
import subprocess
from pathlib import Path
import webbrowser
import argparse
import numpy as np

from PySide6.QtCore import Qt, Signal, Slot, QTranslator, QSignalBlocker, QThread, QSize, QSettings
from PySide6.QtGui import QIcon, QPalette, QFont
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QSizePolicy,
    QMessageBox,
    QDialog,
    QWidget,
    QFileDialog,
    QTableWidgetItem,
    QComboBox,
    QCheckBox,
    QFileSystemModel,
    QTreeWidget,
    QTreeWidgetItem,
    QHBoxLayout,
    QToolButton,
    QStyle,
    QDialogButtonBox,
    QProgressDialog,
    QVBoxLayout,
    QLabel,
)
from PySide6.QtWebEngineCore import QWebEngineUrlScheme

from miscWidgets import *
from path import *
from qplotview import QPlotView

# IMPORT / GUI AND MODULES AND WIDGETS
# ///////////////////////////////////////////////////////////////
from modules import *
from widgets import *
# _is_non_emg_channel is private and not re-exported by star imports; import
# explicitly.
from modules.pyMotion.core.emg import _is_non_emg_channel
from modules.pyMotion.core.muscle_guess import (
    _is_sync_channel, _detect_side, _tokenise_channel, _get_muscle_bases, _guess_muscle_from_channel,
)
from modules.pyMotion.core.stitch import stitch_c3d, check_alignment, StitchError, load_emg_source
from modules.kinematics.playplotview import PlayPlotWidget
from modules.pyMotion.core.batch_config import (
    BatchConfig, ChannelMapping, load_batch_config, save_batch_config,
    processing_to_emg_configure,
)
from modules.pyMotion.core.batch_scan import (
    scan_batch_folder, channel_signature_groups, build_participant, reassign_mvc_file,
)
from modules.pyMotion.core.batch_stitch import find_stitch_pairs, stitch_all

os.environ["QT_FONT_DPI"] = "96"  # FIX Problem for High DPI and Scale above 100%
# SET AS GLOBAL WIDGETS
# ///////////////////////////////////////////////////////////////
widgets = None


# Global Constant
# ///////////////////////////////////////////////////////////////


class DebounceTimer:
    """Single-shot QTimer that restarts on each .trigger() call, firing after delay_ms of silence."""
    def __init__(self, delay_ms, callback, parent=None):
        self._timer = QTimer(parent)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(callback)

    def trigger(self):
        self._timer.start()


class BatchEMGWorker(QThread):
    progress = Signal(int, str)
    finished = Signal()
    error = Signal(str)

    def __init__(self, workspace, people, configure, home, parent=None):
        super().__init__(parent)
        self._workspace = workspace
        self._people = people
        self._configure = configure
        self._home = home

    def run(self):
        for i, p in enumerate(self._people):
            if self.isInterruptionRequested():
                break
            try:
                self._workspace[p].emg.setProcessConfig(self._configure)
                self._workspace[p].emg.processWithConfigure(
                    crop_interval=self._workspace[p].crop_interval
                )
                self._workspace.genReport(p)
                self._workspace.saveReport(p, self._home)
                self.progress.emit(i + 1, p.name)
            except Exception as e:
                self.error.emit("{}: {}".format(p.name, str(e)))
        self.finished.emit()


class BatchImportWorker(QThread):
    """Loads and maps every candidate into the workspace.

    Deliberately does NOT run EMG processing itself -- that's the existing,
    already-proven BatchEMGWorker's job, kicked off separately (see
    MainWindow._onBatchImportFinished) once these participants are in the
    workspace. This worker's only job is "get raw data + mapping applied and
    added to the workspace".
    """
    progress = Signal(int, str)
    finished = Signal(list)  # list of person objects successfully added
    error = Signal(str)

    def __init__(self, workspace, candidates, mapping, parent=None):
        super().__init__(parent)
        self._workspace = workspace
        self._candidates = candidates
        self._mapping = mapping

    def run(self):
        added = []
        for i, cand in enumerate(self._candidates):
            if self.isInterruptionRequested():
                break
            try:
                p, e, kin = build_participant(cand, self._mapping)
                # workspace.hasParticipant() compares by object identity (person
                # has no __eq__), which would never match a freshly-built object
                # -- name-based lookup is what confirmBtnClicked's own duplicate
                # check uses (main.py), so mirror that here.
                if self._workspace.findParticipant(p.name) is not None:
                    self.error.emit(
                        "{}: already exists in the workspace, skipped".format(p.name)
                    )
                    self.progress.emit(i + 1, cand.name)
                    continue
                self._workspace.addParticipant(p, e, kin)
                added.append(p)
                self.progress.emit(i + 1, cand.name)
            except Exception as ex:
                self.error.emit("{}: {}".format(cand.name, str(ex)))
        self.finished.emit(added)


class BatchStitchWorker(QThread):
    """Runs stitch_all() one pair at a time so the progress dialog can show
    which participant/task is currently being merged. Mutates each
    StitchPair's status/out_path in place (see batch_stitch.py); never
    touches the original kinematics/EMG source files.
    """
    progress = Signal(int, str)
    finished = Signal(list)  # the same list of StitchPair, now resolved

    def __init__(self, pairs, parent=None):
        super().__init__(parent)
        self._pairs = pairs

    def run(self):
        for i, p in enumerate(self._pairs):
            if self.isInterruptionRequested():
                break
            stitch_all([p])
            self.progress.emit(i + 1, "{}/{}".format(p.participant, p.stem))
        self.finished.emit(self._pairs)


# ---------------------------------------------------------------------------
# Batch Stitch helpers
# ---------------------------------------------------------------------------
# (EMG channel -> muscle-name guessing (_is_sync_channel, _detect_side,
#  _tokenise_channel, _get_muscle_bases, _guess_muscle_from_channel) moved to
#  modules/pyMotion/core/muscle_guess.py -- widgets/channel_mapping_panel.py
#  needs the same logic and can't safely reach back into main.py for it; see
#  that module's docstring for why the old `from main import ...` deferred
#  import crashed with a real NameError the first time it actually ran.)


def _stitched_glob_for(emg_file):
    """Best-effort: point an exact-filename Batch Import emg_file target
    (e.g. "lift.c3d", the common case from "Detect from folder" proposing an
    exact basename) at its stitched counterpart ("lift_stitched.c3d").
    Returns None for wildcard globs (can't safely rewrite those) or if it's
    already pointing at a stitched file.
    """
    if any(ch in emg_file for ch in "*?[]"):
        return None
    base, _ext = os.path.splitext(emg_file)
    if base.endswith("_stitched"):
        return None
    return base + "_stitched.c3d"


class EMGAddWindow(QMainWindow):
    finished = Signal(tuple)  # Signal emitted when the window closes, returning results.
    batchImportRequested = Signal()  # user chose "Batch Import..." instead of a single-file add

    def __init__(self, workspace, home, width, height, parent=None):
        QMainWindow.__init__(self)

        self.ui = Ui_EMGImport()
        # Create a central widget and mount the UI layout on it.
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        self.ui.setupUi(central_widget)  # Attach the UI layout to the central widget.

        self.resize(width, height)
        self.setWindowTitle(self.tr("Add EMG File"))
        self.setWindowFlags(Qt.Window | Qt.WindowMinimizeButtonHint | Qt.WindowMaximizeButtonHint | Qt.WindowCloseButtonHint)

        self.widgets = self.ui
        self.workspace = workspace
        self.root = home
        self._emg_dir = None          # folder of the last loaded EMG file
        self.emg = None
        self.kinematic = None
        self.person = None
        self.channels = []
        self.mvcfiles = []
        self.mvcfilesMap = {}  # mapping mvc_file -> chan
        self.muscleMap = {}  # mapping chan -> muscle (short name)
        self.isEnabled = {}  # isEnabled[chan] = T/F

        self.widgets.import_btn.clicked.connect(self.importEMGBtnClicked)
        self.widgets.lineEdit.textChanged.connect(self.updateFilterText)
        self.widgets.lineEdit.setPlaceholderText(self.tr("Search channels…"))
        self.widgets.import_btn_2.clicked.connect(self.confirmBtnClicked)
        self.widgets.import_btn_3.clicked.connect(self.cancelBtnClicked)
        self.widgets.importMVC_btn.clicked.connect(self.importMVCBtnClicked)

        # Clicking the "Select" column header toggles all channel checkboxes
        hdr = self.widgets.tableWidget.horizontalHeader()
        hdr.setSectionResizeMode(1, QHeaderView.Fixed)
        hdr.sectionClicked.connect(self._onSelectHeaderClicked)

        self._batch_import_btn = QPushButton(self.tr("Batch Import..."))
        self._batch_import_btn.setCursor(Qt.PointingHandCursor)
        self._batch_import_btn.setStyleSheet(
            "color:#f4f4f4;\n"
            "background-color: #333b46;\n"
            "padding:8px 8px;\n"
            "marging:2px 2px;\n"
            "border-radius:8px;"
        )
        _batch_import_icon = QIcon()
        _batch_import_icon.addFile(
            "images/icons/cil-clone.png", QSize(), QIcon.Normal, QIcon.Off
        )
        self._batch_import_btn.setIcon(_batch_import_icon)
        # Own row below Import File / Import MVC Files, right-aligned to match.
        self._batch_import_row = QHBoxLayout()
        self._batch_import_row.addStretch()
        self._batch_import_row.addWidget(self._batch_import_btn)
        self.ui.verticalLayout_3.insertLayout(1, self._batch_import_row)
        self._batch_import_btn.clicked.connect(self._batchImportClicked)

    def validateEMGDataFile(self, file_path):
        if file_path is None or len(file_path.strip()) == 0:
            return False, self.tr("File path is empty")
        if not os.path.isfile(file_path):
            return False, self.tr("File does not exist: {}".format(file_path))

        ext = os.path.splitext(file_path)[1].lower()
        if ext not in [".c3d", ".mat"]:
            return False, self.tr("Unsupported file format: {}".format(ext))

        try:
            if ext == ".c3d":
                parsed = c3dFile(file_path)
                labels = parsed.analog.labels
            else:
                parsed = matFile(file_path)
                labels = parsed.labels
        except Exception as e:
            return False, self.tr("Failed to read {}: {}".format(os.path.basename(file_path), str(e)))

        if labels is None or len(labels) == 0:
            return False, self.tr("No channels found in file: {}".format(os.path.basename(file_path)))

        # For C3D files, also verify that at least one channel survives EMG filtering.
        # Files with only force plate / IMU data (no EMG) pass the label count check
        # but fail later in emg.setEMGFile() — catch this early with a clear message.
        if ext == ".c3d":
            emg_labels = [l for l in labels if not _is_non_emg_channel(l)]
            if not emg_labels:
                return False, self.tr(
                    "No EMG channels found in '{}'. "
                    "All {} analog channel(s) appear to be force plate or sensor data. "
                    "The file can still be used for kinematics viewing.".format(
                        os.path.basename(file_path), len(labels)
                    )
                )

        return True, ""

    def validateBatchEMGDataFiles(self, files):
        valid_files = []
        invalid_msgs = []
        for file_path in files:
            ok, msg = self.validateEMGDataFile(file_path)
            if ok:
                valid_files.append(file_path)
            else:
                invalid_msgs.append(msg)
        return valid_files, invalid_msgs

    def run(self):
        self.show()
        return self.person, self.emg, self.kinematic

    # update emg and mvc qtablewidget
    def updateChannelBox(self):
        self.widgets.tableWidget.clearContents()
        # column width
        w = self.frameGeometry().width()
        # fixed ratio
        self.widgets.tableWidget.setColumnWidth(0, w * 0.3)
        self.widgets.tableWidget.setColumnWidth(1, w * 0.1)
        self.widgets.tableWidget.setColumnWidth(2, w * 0.2)
        self.widgets.tableWidget.setColumnWidth(3, w * 0.4)

        n = len(self.channels)
        self.widgets.tableWidget.setRowCount(n)
        for i in range(0, n):
            chan = self.channels[i]
            q = QTableWidgetItem(chan)
            q.setTextAlignment(Qt.AlignLeading | Qt.AlignVCenter)
            q.setFlags(q.flags() ^ Qt.ItemIsEditable)
            self.widgets.tableWidget.setItem(i, 0, q)
            # control signal checkbox
            self.widgets.tableWidget.setCellWidget(
                i, 1, self.selectSignalCheckbox(chan)
            )
            # drop down selection
            self.widgets.tableWidget.setCellWidget(i, 2, self.jointComboBox(chan))
            # mvc file path
            self.widgets.tableWidget.setCellWidget(i, 3, self.mvcFileDisplay(chan))

        self.widgets.tableWidget.resizeColumnToContents(0)

    def jointComboBox(self, chan):
        comboBox = QComboBox()
        comboBox.setObjectName(chan)
        comboBox.setEditable(True)
        for j in muscleName.short:
            comboBox.addItem(muscleName.getConcatName(j))

        # Configure QCompleter and enable fuzzy matching.
        completer = QCompleter([muscleName.getConcatName(j) for j in muscleName.short], comboBox)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCaseSensitivity(Qt.CaseInsensitive)  # Case-insensitive matching.
        comboBox.setCompleter(completer)

        if chan in self.muscleMap:
            comboBox.setCurrentText(muscleName.getConcatName(self.muscleMap[chan]))
        else:
            comboBox.setCurrentIndex(-1)
        comboBox.currentIndexChanged.connect(self.jointBoxChanged)
        return comboBox

    def mvcFileDisplay(self, chan):
        comboBox = QComboBox()
        comboBox.setObjectName(chan)

        # only display file name instead of full path
        for f in self.mvcfiles:
            comboBox.addItem(os.path.basename(f))
        if chan in self.mvcfilesMap:
            comboBox.setCurrentIndex(self.mvcfilesMap[chan])
        else:
            comboBox.setCurrentIndex(-1)
        comboBox.currentIndexChanged.connect(self.MVCFilesChanged)
        return comboBox

    def selectSignalCheckbox(self, chan):
        checkbox = QCheckBox()
        checkbox.setObjectName(chan)
        # Set initial state BEFORE connecting the signal — connecting first would
        # cause setChecked() to fire selectedSignalChanged and toggle the state back.
        enabled = self.isEnabled.get(chan, False)
        self.isEnabled[chan] = enabled
        checkbox.setChecked(enabled)
        checkbox.stateChanged.connect(self.selectedSignalChanged)

        QWid = QWidget()
        QHBox = QHBoxLayout(QWid)
        QHBox.addWidget(checkbox)
        QHBox.setAlignment(Qt.AlignCenter)
        QHBox.setContentsMargins(0, 0, 0, 0)
        return QWid

    # SIGNALS AND SLOT
    ################################################
    def jointBoxChanged(self, index):
        jointbox = self.sender()
        chan = jointbox.objectName()
        self.muscleMap[chan] = muscleName.short[index]

    def MVCFilesChanged(self, index):
        mvcBox = self.sender()
        chan = mvcBox.objectName()
        # index = -1 means "no selection" (setCurrentIndex(-1) fires this slot).
        # Python's mvcfiles[-1] would silently return the last file — guard against it.
        if index < 0 or index >= len(self.mvcfiles):
            return
        self.mvcfilesMap[chan] = index
        # apply MVC
        try:
            self.emg.setMVCFile(chan, self.mvcfiles[index])
        except Exception as e:
            QMessageBox.critical(
                None,
                self.tr("error"),
                self.tr("Selected mvc file is invalid: {}".format(str(e))),
                QMessageBox.Ok,
            )
            return

    def selectedSignalChanged(self, state):
        checkbox = self.sender()
        chan = checkbox.objectName()
        # Use the actual checkbox state — never toggle, as that breaks when
        # updateChannelBox() recreates checkboxes and setChecked() fires this slot.
        enabled = bool(state)   # Qt.Checked = 2 → True; Qt.Unchecked = 0 → False
        self.isEnabled[chan] = enabled
        if enabled:
            self.emg.enableChannel(chan)
        else:
            self.emg.disableChannel(chan)

    def updateFilterText(self):
        text = self.widgets.lineEdit.text().strip().lower()
        all_chans = self.emg.getAllChannels() if self.emg else []
        self.channels = (
            [c for c in all_chans if text in c.lower()] if text else all_chans
        )
        self.updateChannelBox()

    def importEMGBtnClicked(self):
        # load EMG file
        file, extension = QFileDialog.getOpenFileName(
            None,
            caption="open EMG file",
            dir=self.root,
            filter="EMG Files (*.c3d *.mat)",
        )
        if file == "":
            return

        valid, err = self.validateEMGDataFile(file)
        if not valid:
            QMessageBox.critical(
                None,
                self.tr("error"),
                err,
                QMessageBox.Ok,
            )
            return

        self.file = file
        self._emg_dir = os.path.dirname(file)   # default dir for MVC dialog
        try:
            self.emg = emg(file)
        except Exception as e:
            QMessageBox.critical(
                None,
                self.tr("error"),
                self.tr("Selected emg file is invalid: {}".format(str(e))),
                QMessageBox.Ok,
            )
            return

        # exclude sync/trigger channels — they are never EMG signals
        self.channels = [
            c for c in self.emg.getAllChannels() if not _is_sync_channel(c)
        ]
        self.widgets.label_3.setText(file)
        self.applyFuzzMatchOnJoint()
        self.updateChannelBox()

    def importMVCBtnClicked(self):
        if len(self.channels) == 0:
            QMessageBox.critical(
                None,
                self.tr("error"),
                self.tr("Please select emg file before MVC file!"),
                QMessageBox.Ok,
            )
            return

        btn = self.sender()
        chan = btn.objectName()

        files, extension = QFileDialog.getOpenFileNames(
            None,
            caption="open MVC file",
            dir=self._emg_dir or self.root,
            filter="EMG Files (*.c3d *.mat)",
        )

        if len(files) == 0:
            return

        valid_files, invalid_msgs = self.validateBatchEMGDataFiles(files)
        if len(invalid_msgs) > 0:
            QMessageBox.warning(
                None,
                self.tr("warning"),
                self.tr("Some files were ignored:\n{}".format("\n".join(invalid_msgs))),
                QMessageBox.Ok,
            )

        if len(valid_files) == 0:
            QMessageBox.critical(
                None,
                self.tr("error"),
                self.tr("No valid MVC files selected"),
                QMessageBox.Ok,
            )
            return

        # Get all file names and join them with ","
        file_names = [os.path.basename(file) for file in valid_files]
        # Enable word-wrap on label_3.
        self.widgets.label_3.setWordWrap(True)
        # If there are too many files, limit the display and append an ellipsis.
        if len(file_names) > 5:
            displayed_files = file_names[:5]
            file_names_str = ",\n".join(displayed_files) + f",\n...{len(file_names)} files"
        else:
            file_names_str = ",\n".join(file_names)
            
        self.widgets.label_3.setText(file_names_str)
        # clear old, set new val
        self.mvcfilesMap.clear()
        self.mvcfiles = valid_files

        # auto apply fuzz matching on MVC mapping
        self.applyFuzzMatchOnMVC()
        self.updateChannelBox()

    def applyFuzzMatchOnMVC(self):
        if not self.mvcfiles:
            return

        filenames = [os.path.basename(f) for f in self.mvcfiles]

        # Single MVC file — covers all muscles; assign to every channel automatically
        if len(self.mvcfiles) == 1:
            for c in self.channels:
                self.mvcfilesMap[c] = 0
            logger.info("EMG ADD MVC: single MVC file, assigned to all channels")
            return

        # Multiple files — try filename fuzzy match per channel
        for c in self.channels:
            candidate_list = self.workspace.matchChanToMVCFile(
                c, filenames, lower_bound=50
            )
            if len(candidate_list) == 0:
                logger.info("EMG ADD MVC: no candidate found for {}".format(c))
                continue
            file, possibility = candidate_list[0]
            logger.info(
                "EMG ADD MVC: {} → {} ({:.0f}%)".format(c, file, possibility)
            )
            self.mvcfilesMap[c] = filenames.index(file)

    def applyFuzzMatchOnJoint(self):
        for c in self.channels:
            # 1. Try direct name-based guess first (strips brand, normalises separators)
            guess = _guess_muscle_from_channel(c)
            if guess is not None:
                logger.info(
                    "EMG Select Joint: name-guess {} → {}".format(c, guess)
                )
                self.muscleMap[c] = guess
                continue

            # 2. Fall back to workspace fuzz-match history (lower_bound 50 %)
            candidate_list = self.workspace.matchChanToJoint(
                c, muscleName.short, lower_bound=50
            )
            if candidate_list:
                joint, possibility = candidate_list[0]
                logger.info(
                    "EMG Select Joint: fuzz-match {} → {} ({:.0f}%)".format(
                        c, joint, possibility
                    )
                )
                self.muscleMap[c] = joint

    def _onSelectHeaderClicked(self, col):
        """Toggle all channel checkboxes when the 'Select' column header is clicked."""
        if col != 1 or not self.channels or self.emg is None:
            return
        all_on = all(self.isEnabled.get(c, False) for c in self.channels)
        new_state = not all_on
        for c in self.channels:
            self.isEnabled[c] = new_state
            if new_state:
                self.emg.enableChannel(c)
            else:
                self.emg.disableChannel(c)
        self.updateChannelBox()

    def sanity(self):
        # check emg file is selected
        if self.emg is None:
            QMessageBox.critical(
                None, self.tr("error"), self.tr("No EMG file selected!"), QMessageBox.Ok
            )
            return False
        # warn if no MVC files provided — user can still proceed without them,
        # but MVC normalization will not be available during processing
        if not self.emg.isMVCComplete():
            reply = QMessageBox.warning(
                None,
                self.tr("warning"),
                self.tr(
                    "No MVC files provided (or some channels are missing MVC data).\n"
                    "MVC normalization will not be available during processing.\n\n"
                    "Continue without MVC files?"
                ),
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return False
        # check pariticipant name is complete and no duplicates
        name = self.widgets.lineEdit_3.text()
        if name == "":
            QMessageBox.critical(
                None,
                self.tr("error"),
                self.tr("Name of pariticipant not set!"),
                QMessageBox.Ok,
            )
            return False
        if self.workspace.findParticipant(name) != None:
            QMessageBox.critical(
                None,
                self.tr("error"),
                self.tr("Name of pariticipant already exists!"),
                QMessageBox.Ok,
            )
            return False
        # at least one chan is selected
        if len(self.emg.getChannels()) == 0:
            QMessageBox.critical(
                None,
                self.tr("error"),
                self.tr("At least one Channel is selected!"),
                QMessageBox.Ok,
            )
            return False
        # check all joint names are selected
        for c in self.channels:
            if c not in self.muscleMap and self.isEnabled[c]:
                QMessageBox.critical(
                    None,
                    self.tr("error"),
                    self.tr("Joint of channel {} not set!").format(c),
                    QMessageBox.Ok,
                )
                return False
        # check joint name is unique
        used_joint = {}
        for chan, joint in self.muscleMap.items():
            if self.isEnabled[chan] and joint in used_joint:
                chan2 = used_joint[joint]
                if self.isEnabled[chan2]:
                    line1 = self.channels.index(chan2) + 1
                    line2 = self.channels.index(chan) + 1
                    QMessageBox.critical(
                        None,
                        self.tr("error"),
                        self.tr(
                            "Duplicated joint name founded, please check line {} and {}"
                        ).format(line1, line2),
                        QMessageBox.Ok,
                    )
                    return False
            used_joint[joint] = chan
        return True

    def confirmBtnClicked(self):
        if not self.sanity():
            return

        # creat person
        name = self.widgets.lineEdit_3.text()
        self.person = person(name, "N/A", "N/A")
        self.kinematic = kinematic(self.file)

        # filter and rename channels
        old = self.emg.getChannels()
        for c in old:
            if c not in self.channels:
                self.emg.removeChannel(c)

        for old, new in self.muscleMap.items():
            if self.isEnabled[old]:
                self.emg.renameChannel(old, new)

        # update MVC file name matching fuzz string
        for chan, index in self.mvcfilesMap.items():
            if self.isEnabled[chan]:
                self.workspace.addChanToMVCFileMap(
                    chan, os.path.basename(self.mvcfiles[index])
                )

        for chan, joint in self.muscleMap.items():
            if self.isEnabled[chan]:
                self.workspace.addChanToJointMap(chan, joint)

        # Emit a signal to notify close and pass results.
        self.finished.emit((self.person, self.emg, self.kinematic))
        self.close()

    def cancelBtnClicked(self):
        self.person = None
        self.emg = None
        self.finished.emit((self.person, self.emg, self.kinematic))  # Emit close signal and pass results.
        self.close()

    def _batchImportClicked(self):
        """Hand off to MainWindow's multi-participant Batch Import workflow.

        This wizard only knows how to add one participant at a time, so it
        can't run the batch flow itself -- it just signals the request and
        closes; MainWindow.addEMGButtonClick wires this to
        batchImportButtonClicked().
        """
        self.batchImportRequested.emit()
        self.close()


_SETTINGS_ORG = "AccMov"
_SETTINGS_APP = "Myotion"
# (code, display name) -- add more languages here as .qm translations are added.
_LANGUAGES = [
    ("en", "English"),
    ("cn", "中文"),
]


class ConfigWindow(QDialog):
    """Preferences dialog. Just a language selector for now -- add more rows
    here as more app-wide preferences are needed."""

    def __init__(self, width, height, parent=None):
        QDialog.__init__(self, parent)
        self.setWindowTitle(self.tr("Preferences"))
        self.setStyleSheet("background-color:#2c3039; color:#f4f4f4;")
        self.resize(min(width, 420), min(height, 220))
        self.setMinimumSize(360, 180)
        self.setSizeGripEnabled(True)

        self.person = None
        self.emg = None
        self.kinematic = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(14)

        lang_row = QHBoxLayout()
        lang_row.addWidget(QLabel(self.tr("Language")))
        self._lang_combo = QComboBox()
        for code, label in _LANGUAGES:
            self._lang_combo.addItem(label, code)
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        current_lang = settings.value("language", "en")
        idx = self._lang_combo.findData(current_lang)
        if idx >= 0:
            self._lang_combo.setCurrentIndex(idx)
        lang_row.addWidget(self._lang_combo, 1)
        layout.addLayout(lang_row)

        layout.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self.tr("Confirm"))
        buttons.accepted.connect(self.confirmBtnClicked)
        buttons.rejected.connect(self.cancelBtnClicked)
        layout.addWidget(buttons)

    def run(self):
        self.exec()
        return self.person, self.emg, self.kinematic

    def confirmBtnClicked(self):
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        old_lang = settings.value("language", "en")
        new_lang = self._lang_combo.currentData()
        settings.setValue("language", new_lang)
        if new_lang != old_lang:
            QMessageBox.information(
                self, self.tr("Language Changed"),
                self.tr("Restart Myotion for the new language to take effect."),
            )
        self.accept()

    def cancelBtnClicked(self):
        self.reject()


class EMGConfigWindow(QDialog):
    def __init__(self, cfg, is_edit_state=True, parent=None):
        QDialog.__init__(self, parent)
        self.ui = Ui_EMGConfigWindow()
        self.ui.setupUi(self)

        self.setMinimumSize(300, 440)
        self.setWindowFlag(Qt.WindowMaximizeButtonHint)
        self.setSizeGripEnabled(True)

        self.widgets = self.ui

        self.widgets.start.clicked.connect(self.confirmBtnClicked)
        self.widgets.cancel.clicked.connect(self.cancelBtnClicked)

        if is_edit_state:
            self.setWindowTitle("EMG Config")
            self.widgets.start.setText("Save")
        else:
            self.setWindowTitle("Batch Process")
            self.widgets.start.setText("Start")

        self._cfg = cfg
        self._state = False
        self._init()

    def run(self):
        self.exec()
        return self._state, self._cfg

    def confirmBtnClicked(self):
        self._state = True

        self._cfg[0].enable = self.ui.dc_offset.checkState() == Qt.CheckState.Checked
        self._cfg[2].enable = self.ui.full_wave_rectification.checkState() == Qt.CheckState.Checked
        self._cfg[4].enable = self.ui.normalization.checkState() == Qt.CheckState.Checked

        self._cfg[1].enable = self.ui.low_pass_switch.checkState() == Qt.CheckState.Checked
        self._cfg[1].order = self.ui.low_pass_order.currentIndex()
        self._cfg[1].cutoff_l = self.ui.low_pass_value.value()

        self._cfg[3].enable = self.ui.band_pass_switch.checkState() == Qt.CheckState.Checked
        self._cfg[3].order = self.ui.band_pass_order.currentIndex()
        self._cfg[3].cutoff_l = self.ui.band_pass_low.value()
        self._cfg[3].cutoff_h = self.ui.band_pass_high.value()

        self.close()

    def cancelBtnClicked(self):
        self.close()

    def _init(self):
        self.ui.dc_offset.setCheckState(Qt.CheckState.Checked if self._cfg[0].enable else Qt.CheckState.Unchecked)
        
        self.ui.full_wave_rectification.setCheckState(Qt.CheckState.Checked if self._cfg[2].enable else Qt.CheckState.Unchecked)
        
        self.ui.normalization.setCheckState(Qt.CheckState.Checked if self._cfg[4].enable else Qt.CheckState.Unchecked)

        lp = self._cfg[1]
        self.ui.low_pass_switch.setCheckState(Qt.CheckState.Checked if lp.enable else Qt.CheckState.Unchecked)
        self.ui.low_pass_order.setCurrentIndex(lp.order)
        self.ui.low_pass_value.setValue(lp.cutoff_l)

        bp = self._cfg[3]
        self.ui.band_pass_switch.setCheckState(Qt.CheckState.Checked if bp.enable else Qt.CheckState.Unchecked)
        self.ui.band_pass_order.setCurrentIndex(bp.order)
        self.ui.band_pass_low.setValue(bp.cutoff_l)
        self.ui.band_pass_high.setValue(bp.cutoff_h)


class EMGConfigEditDialog(QDialog):
    """View / edit a saved emgConfigure using the same pipeline panel the user
    sees during single-EMG processing — avoids the label-swap, range, and
    order-index bugs in the old Ui_EMGConfigWindow form."""

    def __init__(self, cfg, fs=2000, parent=None):
        super().__init__(parent)
        self.setWindowTitle("EMG Config")
        self.setMinimumSize(320, 520)
        self.setWindowFlag(Qt.WindowMaximizeButtonHint)
        self.setSizeGripEnabled(True)
        self._cfg = cfg
        self._accepted = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self._panel = EMGPipelinePanel(self)
        self._panel.load(cfg, fs)
        layout.addWidget(self._panel)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton(self.tr("Cancel"))
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton(self.tr("Save"))
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    def _on_save(self):
        self._accepted = True
        self.accept()

    def run(self):
        self.exec()
        return self._accepted, self._cfg


class StitchAlignmentDialog(QDialog):
    """Preview and manually adjust the offset between a kinematics/force-plate
    .c3d and a separately-recorded EMG file before merging them.

    For the case core.stitch.check_alignment() can't trust automatically (e.g.
    durations that don't line up, or a MAT file with no usable begin_time) —
    stitchDataButtonClicked() sends the user here instead of guessing. Plots one
    kinematics analog channel (force-plate Fz when available) against one EMG
    channel on a shared, offset-adjustable timeline so misalignment is visible,
    then writes the same combined .c3d the automatic path would via
    core.stitch.stitch_c3d() once the user is satisfied.
    """

    def __init__(self, kin_file="", emg_file="", lock_emg=False, on_saved=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Align & Stitch"))
        self.setMinimumSize(760, 560)
        self.setSizeGripEnabled(True)

        self._kin_file = kin_file
        self._emg_file = emg_file
        self._kin_labels = []      # kinematics analog channel labels
        self._kin_arrays = {}      # label -> 1D array, native kin analog rate
        self._kin_rate = 0.0
        self._emg_source = None    # core.stitch.EmgSource
        self._suggested_offset = 0.0
        self._out_path = None
        # Called with the stitched output path instead of showing the generic
        # "now use Load EMG Data" message — e.g. Kinematics Inspection's "attach
        # a kinematics file to this participant" flow uses this to update the
        # existing participant in place rather than treating it as a new trial.
        self._on_saved = on_saved

        layout = QVBoxLayout(self)

        kin_row = QHBoxLayout()
        kin_btn = QPushButton(self.tr("Kinematics .c3d..."))
        kin_btn.clicked.connect(self._pickKinFile)
        self._kin_label = QLineEdit(kin_file)
        self._kin_label.setReadOnly(True)
        kin_row.addWidget(kin_btn)
        kin_row.addWidget(self._kin_label, 1)
        layout.addLayout(kin_row)

        kin_chan_row = QHBoxLayout()
        kin_chan_row.addWidget(QLabel(self.tr("Kinematics channel to preview:")))
        self._kin_chan_combo = QComboBox()
        self._kin_chan_combo.currentIndexChanged.connect(self._redraw)
        kin_chan_row.addWidget(self._kin_chan_combo, 1)
        layout.addLayout(kin_chan_row)

        emg_row = QHBoxLayout()
        emg_btn = QPushButton(self.tr("EMG file..."))
        emg_btn.clicked.connect(self._pickEmgFile)
        if lock_emg:
            emg_btn.setEnabled(False)
            emg_btn.setToolTip(self.tr("Fixed to this participant's existing EMG file"))
        self._emg_label = QLineEdit(emg_file)
        self._emg_label.setReadOnly(True)
        emg_row.addWidget(emg_btn)
        emg_row.addWidget(self._emg_label, 1)
        layout.addLayout(emg_row)

        chan_row = QHBoxLayout()
        chan_row.addWidget(QLabel(self.tr("EMG channel to preview:")))
        self._chan_combo = QComboBox()
        self._chan_combo.currentIndexChanged.connect(self._redraw)
        chan_row.addWidget(self._chan_combo, 1)
        layout.addLayout(chan_row)

        offset_row = QHBoxLayout()
        offset_row.addWidget(QLabel(self.tr("Offset (s):")))
        self._offset_spin = QDoubleSpinBox()
        self._offset_spin.setRange(-3600.0, 3600.0)
        self._offset_spin.setDecimals(4)
        self._offset_spin.setSingleStep(0.01)
        self._offset_spin.valueChanged.connect(self._redraw)
        offset_row.addWidget(self._offset_spin)
        self._suggest_btn = QPushButton(self.tr("Use Suggested"))
        self._suggest_btn.clicked.connect(self._applySuggestedOffset)
        offset_row.addWidget(self._suggest_btn)
        offset_row.addStretch()
        layout.addLayout(offset_row)

        offset_desc = QLabel(self.tr(
            "Offset is the kinematics-clock time at which the EMG recording's own "
            "sample 0 occurred (kinematics time + offset = EMG time). 0 means both "
            "recordings started together; a positive value means EMG started that "
            "many seconds after the kinematics trigger."
        ))
        offset_desc.setWordWrap(True)
        offset_desc.setStyleSheet("color: #888; font-size: 9pt;")
        layout.addWidget(offset_desc)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #888;")
        layout.addWidget(self._status_label)

        self._plot = PlayPlotWidget()
        layout.addWidget(self._plot, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton(self.tr("Cancel"))
        cancel_btn.clicked.connect(self.reject)
        self._stitch_btn = QPushButton(self.tr("Save as Stitched C3D..."))
        self._stitch_btn.setDefault(True)
        self._stitch_btn.setEnabled(False)
        self._stitch_btn.clicked.connect(self._onStitch)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self._stitch_btn)
        layout.addLayout(btn_row)

        if self._kin_file:
            self._loadKinFile()
        if self._emg_file:
            self._loadEmgFile()
        self._maybeCheckAlignment()

    def _pickKinFile(self):
        f, _ = QFileDialog.getOpenFileName(
            self, self.tr("Select kinematics / force-plate file"),
            self._kin_file or "", "C3D Files (*.c3d)",
        )
        if f:
            self._kin_file = f
            self._kin_label.setText(f)
            self._loadKinFile()
            self._maybeCheckAlignment()

    def _pickEmgFile(self):
        f, _ = QFileDialog.getOpenFileName(
            self, self.tr("Select EMG file"),
            os.path.dirname(self._kin_file) if self._kin_file else "",
            "EMG Files (*.c3d *.mat)",
        )
        if f:
            self._emg_file = f
            self._emg_label.setText(f)
            self._loadEmgFile()
            self._maybeCheckAlignment()

    def _loadKinFile(self):
        """Load every analog channel from the kinematics file and populate the
        preview combo — the user picks which one to compare against EMG (a
        force-plate Fz is the most useful for hardware-sync alignment, so it's
        pre-selected when present, but any channel can be chosen)."""
        try:
            f = c3dFile(self._kin_file)
            analog = f.analog
            self._kin_labels = [l.strip() for l in analog.labels] if analog is not None else []
            self._kin_rate = float(analog.fs) if analog is not None else 0.0
            self._kin_arrays = (
                {l: np.asarray(analog[l], dtype=np.float32) for l in self._kin_labels}
                if analog is not None else {}
            )
        except Exception as e:
            self._status_label.setText(self.tr("Could not load kinematics file: {}").format(str(e)))
            self._kin_labels, self._kin_arrays, self._kin_rate = [], {}, 0.0

        self._kin_chan_combo.blockSignals(True)
        self._kin_chan_combo.clear()
        self._kin_chan_combo.addItems(self._kin_labels)
        default_idx = next(
            (i for i, l in enumerate(self._kin_labels)
             if l.lower() in ("force.fz1", "fz1", "force.fz", "fz")),
            0,
        )
        if self._kin_labels:
            self._kin_chan_combo.setCurrentIndex(default_idx)
        self._kin_chan_combo.blockSignals(False)
        self._redraw()

    def _loadEmgFile(self):
        try:
            self._emg_source = load_emg_source(self._emg_file)
        except Exception as e:
            self._status_label.setText(self.tr("Could not load EMG file: {}").format(str(e)))
            self._emg_source = None
            self._redraw()
            return

        self._chan_combo.blockSignals(True)
        self._chan_combo.clear()
        self._chan_combo.addItems(self._emg_source.labels)
        # Default to a sync/trigger-looking channel when one exists — it's the
        # cleanest visual reference for alignment.
        default_idx = next(
            (i for i, l in enumerate(self._emg_source.labels) if _is_sync_channel(l)),
            0,
        )
        self._chan_combo.setCurrentIndex(default_idx)
        self._chan_combo.blockSignals(False)
        self._redraw()

    def _maybeCheckAlignment(self):
        if not (self._kin_file and self._emg_file):
            return
        try:
            offset_s, trusted, msg = check_alignment(self._kin_file, self._emg_file)
        except StitchError as e:
            offset_s, trusted, msg = 0.0, False, str(e)
        self._suggested_offset = offset_s
        self._offset_spin.blockSignals(True)
        self._offset_spin.setValue(offset_s)
        self._offset_spin.blockSignals(False)
        self._status_label.setText(("Trusted: " if trusted else "Unconfirmed: ") + msg)
        self._stitch_btn.setEnabled(True)
        self._redraw()

    def _applySuggestedOffset(self):
        self._offset_spin.setValue(self._suggested_offset)

    def _redraw(self):
        self._plot.clear()
        if self._kin_arrays and self._kin_chan_combo.count():
            label = self._kin_chan_combo.currentText()
            y = self._kin_arrays.get(label)
            if y is not None and self._kin_rate > 0:
                x = np.arange(len(y)) / self._kin_rate
                self._plot.add_line(x, y, self.tr("Kinematics: {}").format(label), type="analog", rate=1)
        if self._emg_source is not None and self._chan_combo.count():
            chan = self._chan_combo.currentText()
            y = self._emg_source.arrays[chan]
            offset_s = self._offset_spin.value()
            x = np.arange(len(y)) / self._emg_source.rate + offset_s
            self._plot.add_line(x, y, self.tr("EMG: {}").format(chan), type="emg", rate=1)

    def _onStitch(self):
        offset_s = self._offset_spin.value()
        try:
            out_path = stitch_c3d(self._kin_file, self._emg_file, offset_s=offset_s)
        except StitchError as e:
            QMessageBox.critical(
                self, self.tr("error"),
                self.tr("Could not stitch these files:\n\n{}").format(str(e)),
                QMessageBox.Ok,
            )
            return

        self._out_path = out_path
        if self._on_saved is not None:
            self._on_saved(out_path)
        else:
            QMessageBox.information(
                self, self.tr("Stitched"),
                self.tr(
                    "Wrote a combined kinematics + EMG file:\n\n{}\n\n"
                    'Use "Load EMG Data" and select this file to add it as a participant.'
                ).format(out_path),
                QMessageBox.Ok,
            )
        self.accept()

    def run(self):
        self.exec()
        return self._out_path


class MainWindow(QMainWindow):
    # SIGNALS
    sigUpdateParticipants = Signal()
    sigAsyncLoadError = Signal(str)

    def __init__(self, language, sys_log, show_immediately=True):
        QMainWindow.__init__(self)

        self.language = language
        self._show_immediately = show_immediately

        # SET AS GLOBAL WIDGETS
        # ///////////////////////////////////////////////////////////////
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        global widgets
        widgets = self.ui

        # USE CUSTOM TITLE BAR | USE AS "False" FOR MAC OR LINUX
        # ///////////////////////////////////////////////////////////////
        Settings.ENABLE_CUSTOM_TITLE_BAR = True

        # APP NAME
        # ///////////////////////////////////////////////////////////////
        title = "MYOTION"
        description = "MYOTION"
        # APPLY TEXTS
        self.setWindowTitle(title)
        widgets.titleRightInfo.setText(description)

        # TOGGLE MENU
        # ///////////////////////////////////////////////////////////////
        widgets.toggleButton.clicked.connect(lambda: UIFunctions.toggleMenu(self, True))

        # SET UI DEFINITIONS
        # ///////////////////////////////////////////////////////////////
        UIFunctions.uiDefinitions(self)
        self._theme_mode = "dark"  # matches the QSS loaded at startup
        widgets.displayMenu.setText(self.tr("Light Mode"))
        self.applyModernWidgetStyle()
        self._setup_kinematics_splitters()
        self._setup_emg_splitters()
        self._setup_start_page_splitter()
        self._setup_emg_page_splitters()
        self._setup_kinematics_page_splitter()
        self._setup_frequency_page_splitters()
        self._replace_start_middle_with_logo()
        widgets.middle.hide()   # sign-in / sign-up removed (open-source build)
        self._generate_custom_icons()

        # Hide panels the user removed from the workflow:
        # 1. Participant List sidebar — workspace tree already shows all participants
        widgets.paticipant_list.hide()
        # 2. Configuration Log — duplicated by the pipeline step cards
        widgets.configuration_list.hide()

        # Fix plot panel labels — retranslateUi used a placeholder "Plot"
        widgets.label_12.setText(self.tr("Current Process"))

        # "Load EMG Data" buttons are disabled until a workspace is open
        self._set_add_emg_enabled(False)

        # "Clear Workspace" button — inserted above "New Project" (btn_new) in the sidebar
        self._btn_clear_ws = QPushButton(self.tr("Clear Workspace"))
        self._btn_clear_ws.setMinimumHeight(45)
        self._btn_clear_ws.setCursor(Qt.PointingHandCursor)
        self._btn_clear_ws.setStyleSheet(
            "background-image: url(:/icons/images/icons/cil-folder-remove.png);"
        )
        self._btn_clear_ws.clicked.connect(self.clearWorkspaceButtonClick)
        widgets.verticalLayout_11.insertWidget(0, self._btn_clear_ws)

        # "Stitch Your Data" / "Batch Stitch..." — side by side. Both merge a
        # separately-recorded EMG file with a kinematics/force-plate .c3d
        # into one combined .c3d (single pair vs. a whole batch folder), for
        # hardware-synced trials saved to two files instead of one. Inserted
        # above "Load EMG Data"; neither requires an open workspace since
        # they only write files to disk (loading results still goes through
        # the normal "Load EMG Data" / "Batch Import..." flow, which does).
        self._btn_stitch_data = QPushButton(self.tr("Stitch Your Data"))
        self._btn_stitch_data.setMinimumHeight(56)
        self._btn_stitch_data.setCursor(Qt.PointingHandCursor)
        stitch_icon = QIcon()
        stitch_icon.addFile(":/icons/images/icons/cil-link.png")
        self._btn_stitch_data.setIcon(stitch_icon)

        self._btn_batch_stitch = QPushButton(self.tr("Batch Stitch..."))
        self._btn_batch_stitch.setMinimumHeight(56)
        self._btn_batch_stitch.setCursor(Qt.PointingHandCursor)
        _batch_stitch_icon = QIcon()
        _batch_stitch_icon.addFile(":/icons/images/icons/cil-link.png")
        self._btn_batch_stitch.setIcon(_batch_stitch_icon)

        _stitch_row = QHBoxLayout()
        _stitch_row.addWidget(self._btn_stitch_data)
        _stitch_row.addWidget(self._btn_batch_stitch)
        widgets.verticalLayout_35.insertLayout(0, _stitch_row)

        # "Align & Stitch..." — manual-offset counterpart to "Stitch Your Data",
        # for pairs stitchDataButtonClicked() declines to merge automatically
        # (unmatched durations, missing/out-of-range MAT begin_time, etc).
        # Inserted above the participant tree on the Kinematics Inspection page.
        # kinematics_right's own QVBoxLayout was never kept as a named attribute
        # by setupUi(), but layout() still returns it at runtime.
        self._btn_align_stitch = QPushButton(self.tr("Align && Stitch..."))
        self._btn_align_stitch.setMinimumHeight(36)
        self._btn_align_stitch.setCursor(Qt.PointingHandCursor)
        self._btn_align_stitch.setStyleSheet(
            "color:#f4f4f4; background-color:#333b46; border-radius:6px; padding:4px 8px;"
        )
        self._btn_align_stitch.clicked.connect(self.alignStitchButtonClicked)
        widgets.kinematics_right.layout().insertWidget(0, self._btn_align_stitch)

        # QTableWidget PARAMETERS
        # ///////////////////////////////////////////////////////////////
        widgets.tableWidget.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        # BUTTONS CLICK
        # ///////////////////////////////////////////////////////////////

        # LEFT MENUS
        widgets.btn_start.clicked.connect(self.buttonClick)
        widgets.btn_emg.clicked.connect(self.buttonClick)
        widgets.btn_kinematic.clicked.connect(self.buttonClick)
        widgets.btn_frequency.clicked.connect(self.buttonClick)
        widgets.btn_advanced.clicked.connect(self.buttonClick)
        widgets.btn_stats.clicked.connect(self.buttonClick)

        # EXTRA LEFT BOX
        def openCloseLeftBox():
            UIFunctions.toggleLeftBox(self, True)

        widgets.toggleLeftBox.clicked.connect(openCloseLeftBox)
        widgets.extraCloseColumnBtn.clicked.connect(openCloseLeftBox)
        widgets.pushButton_17.clicked.connect(self.workspaceRemoveSelectedParticipant)
        widgets.pushButton_18.clicked.connect(self.addEMGButtonClick)
        widgets.pushButton_16.clicked.connect(self.emgPageRemoveSelectedParticipant)
        widgets.pushButton_161.clicked.connect(self.addEMGButtonClick)
        widgets.checkBox_3.stateChanged.connect(self.workspaceToggleSelectAllParticipant)
        widgets.listWidget_3.itemChanged.connect(self.checkWorkspaceParticipantSelectState)

        # EXTRA RIGHT BOX
        def openCloseRightBox():
            UIFunctions.toggleRightBox(self, True)

        widgets.settingsTopBtn.clicked.connect(openCloseRightBox)

        # Project
        widgets.btn_new.clicked.connect(self.newProjectButtonClick)
        widgets.btn_share.clicked.connect(lambda: self.saveProjectButtonClick(True))
        widgets.btn_adjustments.clicked.connect(self.loadProjectButtonClick)
        self.sigUpdateParticipants.connect(self.updateEMGParticipantBox)
        self.sigAsyncLoadError.connect(self.handleAsyncLoadError)
        widgets.treeView.doubleClicked.connect(self.handleTreeViewDoubleClick)
        widgets.treeView.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        widgets.treeView.customContextMenuRequested.connect(self.showTreeViewContextMenu)

        # Menu bar
        widgets.fileMenu.clicked.connect(self.fileMenuClick)
        widgets.displayMenu.clicked.connect(self.displayMenuClick)
        widgets.toolsMenu.clicked.connect(self.underDevelopmentClick)
        widgets.settingsMenu.clicked.connect(self.configButtonClick)
        widgets.helpMenu.clicked.connect(self.showAboutDialog)

        # Right settings panel (opened via settingsTopBtn) -- Profile is a
        # placeholder until a real user-account system exists.
        widgets.btn_print.clicked.connect(self.underDevelopmentClick)

        # Quick Start buttons on the start page.
        widgets.pushButton_2.clicked.connect(self.underDevelopmentClick)
        widgets.pushButton_3.clicked.connect(self.underDevelopmentClick)
        widgets.pushButton_4.clicked.connect(self.underDevelopmentClick)

        # EMG Page
        self._btn_stitch_data.clicked.connect(self.stitchDataButtonClicked)
        self._btn_batch_stitch.clicked.connect(self.batchStitchButtonClicked)
        widgets.pushButton_10.clicked.connect(self.addEMGButtonClick)
        widgets.pushButton_11.clicked.connect(self.singleEMGButtonClick)
        widgets.listWidget.itemDoubleClicked.connect(
            self.EMGConfigurationListDoubleClicked
        )
        widgets.checkBox_4.stateChanged.connect(self.EMGConfigureToggleConfiguration)
        widgets.checkBox_11.stateChanged.connect(self.EMGConfigureToggleConfiguration)
        widgets.checkBox_12.stateChanged.connect(self.EMGConfigureToggleConfiguration)
        widgets.checkBox_13.stateChanged.connect(self.EMGConfigureToggleConfiguration)
        widgets.comboBox_2.currentIndexChanged.connect(
            self.EMGChannelSelectorIndexChanged
        )
        widgets.toolBox.currentChanged.connect(self.EMGChannelToolBoxIndexChanged)
        widgets.pushButton_19.clicked.connect(self.EMGStepNextButtonClicked)
        widgets.pushButton_20.clicked.connect(self.EMGStepNextButtonClicked)
        widgets.pushButton_21.clicked.connect(self.EMGConfigureFilterConfiguration)
        widgets.pushButton_22.clicked.connect(self.EMGStepNextButtonClicked)
        widgets.pushButton_23.clicked.connect(self.EMGStepNextButtonClicked)
        widgets.pushButton_25.clicked.connect(self.EMGStepNextButtonClicked)
        widgets.pushButton_26.clicked.connect(self.EMGGenerateReportButtonClicked)
        widgets.pushButton_27.clicked.connect(self.EMGSaveConfigurationButtonClicked)
        widgets.pushButton_12.clicked.connect(self.EMGBatchProcessButtonClicked)
        widgets.pushButton_12.setEnabled(False)
        # "+" beside the existing "-" (pushButton_15) -- (re)adds a named
        # saved EMG config, e.g. after accidentally deleting the entry a
        # Batch Import created (Edit Config alone can't recreate the list
        # entry if it's gone; this is a direct, explicit way to do it).
        self._add_saved_config_btn = QPushButton()
        self._add_saved_config_btn.setCursor(Qt.PointingHandCursor)
        self._add_saved_config_btn.setStyleSheet(
            "background-color:rgba(255,255,255,0.15);\n" "margin:3px 2px;"
        )
        _add_saved_config_icon = QIcon()
        _add_saved_config_icon.addFile(
            "images/icons/cil-plus.png", QSize(), QIcon.Normal, QIcon.Off
        )
        self._add_saved_config_btn.setIcon(_add_saved_config_icon)
        self._add_saved_config_btn.clicked.connect(self._addSavedConfigButtonClicked)
        widgets.horizontalLayout_30.insertWidget(0, self._add_saved_config_btn)

        widgets.pushButton_15.clicked.connect(self.removeEMGConfig)
        widgets.lineEdit_3.textChanged.connect(self.updateFilterText)
        widgets.checkBox_2.stateChanged.connect(self.EMGParticipantSelectAllClicked)

        # Freqency Page
        widgets.pushButton_29.clicked.connect(self.addNewFFTtoFreqAnalysisFFTPanel)
        widgets.pushButton_28.clicked.connect(self.FFTPlotPrevPageClicked)
        widgets.pushButton_30.clicked.connect(self.FFTPlotNextPageClicked)
        widgets.pushButton_32.clicked.connect(self.FFTPlotClearAllClicked)
        widgets.comboBox_19.currentIndexChanged.connect(self.FFTPlotPerPageSelected)
        widgets.comboBox_20.currentIndexChanged.connect(self.FFTPlotPageIndexSelected)

        # Export button at the bottom of the participants panel
        self._btn_export_freq = QPushButton(self.tr("Export Freq Results"))
        self._btn_export_freq.setCursor(Qt.PointingHandCursor)
        self._btn_export_freq.setStyleSheet(
            "color:#f4f4f4; background-color:#6272a4;"
            " border-radius:6px; padding:6px 10px; font-weight:bold; margin:4px;"
        )
        self._btn_export_freq.clicked.connect(self.exportFreqAnalysisCSV)
        widgets.verticalLayout_140.addWidget(self._btn_export_freq)

        # start page
        # widgets.settingsTopBtn.hide()
        # if not BYPASS_LOGIN_FOR_DEV:
        #     widgets.signInButton.clicked.connect(self.login_click)
        # widgets.signUpButton.clicked.connect(
        #     lambda x: webbrowser.open("http://www.accmov.com")
        # )
        # widgets.btn_logout.clicked.connect(self.logout_click)

        # SHOW APP
        # ///////////////////////////////////////////////////////////////

        # self.show()
        # show_immediately=False (splash-screen startup path) defers this to
        # __main__, which calls showMaximized() itself only after the splash
        # has finished -- otherwise the maximized window becomes visible
        # right here, mid-construction, well before the splash closes.
        if self._show_immediately:
            self.showMaximized()

        # SET CUSTOM THEME
        # ///////////////////////////////////////////////////////////////
        useCustomTheme = False
        themeFile = "D:/Myotion/themes/py_dracula_dark.qss"

        # SET THEME AND HACKS
        if useCustomTheme:
            # LOAD AND APPLY STYLE
            UIFunctions.theme(self, themeFile, True)

            # SET HACKS
            AppFunctions.setThemeHack(self)

        # SET HOME PAGE AND SELECT MENU
        # ///////////////////////////////////////////////////////////////
        widgets.stackedWidget.setCurrentWidget(widgets.start_page)
        widgets.btn_start.setStyleSheet(
            UIFunctions.selectMenu(widgets.btn_start.styleSheet())
        )

        # APPLICATION LOGICS
        self.workspace = None
        self.home = None
        self.account = None
        # Clear Designer's placeholder table content and disable the
        # data-dependent controls (Signal Process / Select All) until a
        # workspace with participants is loaded.
        self.updateEMGParticipantBox()

        self.participant_filter = ""
        self.filesystemTree = (
            QFileSystemModel()
        )  # file system tree for workspace directory
        self.selectedParticipants = set()  # key of selected participants

        # Remembers the BatchConfig used by the most recent Batch Import, so
        # Edit Config / Edit Mapping act on "the same config file loaded
        # along with the batch" instead of always starting blank.
        self._current_batch_config = None
        self._current_batch_config_path = None  # None if never saved to/loaded from a .toml
        self._current_batch_config_name = None  # key in workspace.getEMGConfigures()
        self.singleEMG = (
            None,
            None,
            None,
        )  # sm for single EMG Process, (Participant, Steps, channel)
        self.inputBuffer = None  # buffer for single EMG process
        self.outputBuffer = None  # buffer for single EMG process

        # FrequencyAnalysis State Machine
        self.freqAnalysis = (None, None)  # (Participant, channel)
        self.freqAnalysisPlots = []  # plot diagram for frequency analysis
        self.plotsPerPage_list = [0, 1, 3, 5, 10]  # correspond to ui combox_19 setting


        # Configure validators for EMG filter input boxes.
        self.setupEMGFilterValidators()

        # Instant filter preview — debounce 300 ms so rapid typing doesn't spam re-renders
        self._filter_debounce = DebounceTimer(300, self._applyFilterPreview, self)
        widgets.lineEdit_10.textChanged.connect(self._onFilterInputChanged)
        widgets.lineEdit_11.textChanged.connect(self._onFilterInputChanged)
        widgets.lineEdit_12.textChanged.connect(self._onFilterInputChanged)
        widgets.comboBox_7.currentIndexChanged.connect(self._onFilterInputChanged)
        widgets.comboBox_8.currentIndexChanged.connect(self._onFilterInputChanged)

        # All-steps pipeline panel — replaces the one-at-a-time QToolBox
        widgets.toolBox.hide()
        self._pipeline_panel = EMGPipelinePanel(widgets.data_process_instruction)
        widgets.verticalLayout_42.insertWidget(0, self._pipeline_panel)
        self._pipeline_panel.configChanged.connect(self._onPipelineStepChanged)
        self._pipeline_panel.stepSelected.connect(self.selectSingleEMGStep)

        # Action bar — "Export Report & CSV" button shown below the pipeline cards
        # (pushButton_26/27 are buried in the hidden toolBox Summary page so they are inaccessible)
        self._emg_action_bar = QFrame(widgets.data_process_instruction)
        self._emg_action_bar.setStyleSheet("border-top: 1px solid rgba(255,255,255,0.1);")
        _abar_layout = QHBoxLayout(self._emg_action_bar)
        _abar_layout.setContentsMargins(8, 6, 8, 6)
        _abar_layout.setSpacing(6)
        _abar_layout.addStretch()
        self._btn_export_report = QPushButton(self.tr("Export Report && CSV"))
        self._btn_export_report.setCursor(Qt.PointingHandCursor)
        self._btn_export_report.setStyleSheet(
            "color:#f4f4f4; background-color:#2a9d8f;"
            " border-radius:6px; padding:6px 18px; font-weight:bold;"
        )
        self._btn_export_report.clicked.connect(self.EMGGenerateReportButtonClicked)
        _abar_layout.addWidget(self._btn_export_report)
        widgets.verticalLayout_42.addWidget(self._emg_action_bar)
        self._emg_action_bar.hide()

        # Manual crop group — compact widget at the top of the instruction panel
        _LBL_S = "color: #c8c8c8; font-size: 9pt;"
        _SPIN_S = "background-color: #333b46; color: #f4f4f4; border-radius:4px; padding:2px;"
        _BTN_S  = "color:#f4f4f4; background-color:#333b46; border-radius:4px; padding:3px 6px;"
        _GRP_S  = (
            "QGroupBox {"
            "  font-weight: bold; color: #c8c8c8;"
            "  border: 1px solid #444; border-radius: 6px;"
            "  margin-top: 8px; padding: 4px;"
            "}"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }"
        )
        self._crop_group = QGroupBox("")
        self._crop_group.setStyleSheet(_GRP_S)
        _cg_layout = QVBoxLayout(self._crop_group)
        _cg_layout.setContentsMargins(8, 8, 8, 4)
        _cg_layout.setSpacing(3)
        _crop_header = QHBoxLayout()
        _crop_header.setSpacing(4)
        _crop_title_lbl = QLabel("Analysis Segment")
        _crop_title_lbl.setStyleSheet("font-weight: bold; color: #c8c8c8; border: none;")
        _crop_header.addWidget(_crop_title_lbl)
        _crop_help_btn = QToolButton()
        _crop_help_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarContextHelpButton)
        )
        _crop_help_btn.setIconSize(QSize(14, 14))
        _crop_help_btn.setStyleSheet("QToolButton { border: none; background: transparent; }")
        _crop_help_btn.setToolTip(
            self.tr(
                "Sets the valid time window for this participant's EMG analysis. "
                "Original signal is unchanged; Clear reverts to the full trial.\n\n"
                "Single-participant use only — for batch processing, segment via "
                "the Kinematics Inspection module instead."
            )
        )
        _crop_header.addWidget(_crop_help_btn)
        _crop_header.addStretch()
        _cg_layout.addLayout(_crop_header)
        _crop_row = QHBoxLayout()
        _crop_row.setSpacing(4)
        _lbl_start = QLabel("Start (s):")
        _lbl_start.setStyleSheet(_LBL_S)
        _crop_row.addWidget(_lbl_start)
        self._crop_start_spin = QDoubleSpinBox()
        self._crop_start_spin.setRange(0.0, 9999.0)
        self._crop_start_spin.setDecimals(3)
        self._crop_start_spin.setSingleStep(0.1)
        self._crop_start_spin.setStyleSheet(_SPIN_S)
        _crop_row.addWidget(self._crop_start_spin)
        _lbl_end = QLabel("End (s):")
        _lbl_end.setStyleSheet(_LBL_S)
        _crop_row.addWidget(_lbl_end)
        self._crop_end_spin = QDoubleSpinBox()
        self._crop_end_spin.setRange(0.0, 9999.0)
        self._crop_end_spin.setDecimals(3)
        self._crop_end_spin.setSingleStep(0.1)
        self._crop_end_spin.setStyleSheet(_SPIN_S)
        _crop_row.addWidget(self._crop_end_spin)
        _crop_apply = QPushButton("Apply")
        _crop_apply.setMaximumWidth(55)
        _crop_apply.setStyleSheet(_BTN_S)
        _crop_apply.clicked.connect(self._onCropApply)
        _crop_row.addWidget(_crop_apply)
        _crop_clear = QPushButton("Clear")
        _crop_clear.setMaximumWidth(55)
        _crop_clear.setStyleSheet(_BTN_S)
        _crop_clear.clicked.connect(self._onCropClear)
        _crop_row.addWidget(_crop_clear)
        _cg_layout.addLayout(_crop_row)
        self._crop_status_label = QLabel("Full trial (no crop)")
        self._crop_status_label.setStyleSheet("color: #666; font-size: 10px;")
        _cg_layout.addWidget(self._crop_status_label)
        self._crop_group.setEnabled(False)
        widgets.verticalLayout_42.insertWidget(0, self._crop_group)

        # Pipeline overview button in the toolbar above the plots
        self._overview_btn = QPushButton("Pipeline View")
        self._overview_btn.setCursor(Qt.PointingHandCursor)
        self._overview_btn.setStyleSheet(
            "color:#f4f4f4; background-color:#333b46; border-radius:6px; padding:4px 8px;"
        )
        self._overview_btn.clicked.connect(self.showPipelineOverview)
        widgets.horizontalLayout_19.addWidget(self._overview_btn)

        # Edit Config / Edit Mapping — inserted side by side right after
        # BATCH PROCESS (pushButton_12), styled to match pushButton_10/11/12
        # (no explicit stylesheet, so both inherit the app-wide
        # "#pagesContainer QPushButton" rule from the theme .qss, same as
        # LOAD EMG DATA / SIGNAL PROCESS / BATCH PROCESS).
        # (Batch Import itself now lives in EMGAddWindow's wizard, replacing
        # the old "Scan Folder..." button, since it's an alternative entry
        # point into "Load EMG Data" rather than a separate top-level action.)
        self._batch_edit_config_btn = QPushButton(self.tr("Edit Config…"))
        self._batch_edit_config_btn.setCursor(Qt.PointingHandCursor)
        self._batch_edit_config_btn.setMinimumHeight(56)
        self._batch_edit_config_btn.setEnabled(False)
        self._batch_edit_config_btn.clicked.connect(self.editBatchConfigButtonClicked)

        self._batch_edit_mapping_btn = QPushButton(self.tr("Edit Mapping…"))
        self._batch_edit_mapping_btn.setCursor(Qt.PointingHandCursor)
        self._batch_edit_mapping_btn.setMinimumHeight(56)
        self._batch_edit_mapping_btn.setEnabled(False)
        self._batch_edit_mapping_btn.clicked.connect(self.editMappingButtonClicked)

        # verticalLayout_35 holds only pushButton_10/11/12 (its own trailing
        # spacer, verticalSpacer_9, actually belongs to the parent layout
        # verticalLayout_75, not this one) — appending lands right after
        # BATCH PROCESS as intended.
        _batch_edit_row = QHBoxLayout()
        _batch_edit_row.addWidget(self._batch_edit_config_btn)
        _batch_edit_row.addWidget(self._batch_edit_mapping_btn)
        widgets.verticalLayout_35.addLayout(_batch_edit_row)

        # self.test()

        # 添加自动保存定时器
        self.autosave_timer = QTimer(self)
        self.autosave_timer.timeout.connect(self.autoSaveHandler)
        self.autosave_interval = 60000  # 1 minute (milliseconds)

    # Palette tokens for each theme mode.
    _THEME_PALETTES = {
        "dark": {
            "label":            "rgb(222, 226, 234)",
            "title":            "rgb(245, 247, 250)",
            "subtitle":         "rgb(182, 188, 199)",
            "btn_bg":           "rgb(46,  52,  63)",
            "btn_border":       "rgb(62,  68,  82)",
            "btn_text":         "rgb(236, 239, 244)",
            "btn_hover":        "rgb(58,  65,  79)",
            "btn_pressed":      "rgb(38,  43,  54)",
            "btn_dis_text":     "rgb(145, 150, 162)",
            "btn_dis_bg":       "rgb(40,  44,  53)",
            "btn_dis_border":   "rgb(55,  60,  72)",
        },
        "light": {
            "label":            "#44475a",
            "title":            "#282a36",
            "subtitle":         "#6272a4",
            "btn_bg":           "#44475a",
            "btn_border":       "#282a36",
            "btn_text":         "#f8f8f2",
            "btn_hover":        "#6272a4",
            "btn_pressed":      "#282a36",
            "btn_dis_text":     "#f8f8f2",
            "btn_dis_bg":       "#9faeda",
            "btn_dis_border":   "#7d8fba",
        },
    }

    def applyModernWidgetStyle(self, mode: str = "dark"):
        self.setFont(QFont("Segoe UI", 10))

        p = self._THEME_PALETTES.get(mode, self._THEME_PALETTES["dark"])

        # Structural + color rules scoped to the content area.
        modern_style = f"""
#contentBottom QLabel {{
    font-family: "Segoe UI";
    font-size: 10pt;
    color: {p['label']};
}}

#contentBottom QLabel#title_label {{
    font-family: "Segoe UI Semibold";
    font-size: 20pt;
    color: {p['title']};
}}

#contentBottom QLabel#subtitle_label {{
    font-family: "Segoe UI";
    font-size: 11pt;
    color: {p['subtitle']};
}}

#contentBottom QPushButton {{
    font-family: "Segoe UI Semibold";
    font-size: 10pt;
    padding: 6px 12px;
    border-radius: 10px;
    border: 1px solid {p['btn_border']};
    background-color: {p['btn_bg']};
    color: {p['btn_text']};
}}

#contentBottom QPushButton:hover {{
    background-color: {p['btn_hover']};
}}

#contentBottom QPushButton:pressed {{
    background-color: {p['btn_pressed']};
}}

#contentBottom QPushButton:disabled {{
    color: {p['btn_dis_text']};
    background-color: {p['btn_dis_bg']};
    border: 1px solid {p['btn_dis_border']};
}}
"""
        # Append to whatever the current base QSS is so theme chrome is not lost.
        base_style = self.ui.styleSheet.styleSheet()
        self.ui.styleSheet.setStyleSheet(base_style + "\n" + modern_style)

    def handleAsyncLoadError(self, error_msg):
        """Handle errors raised during asynchronous loading."""
        logger.error(f"async load error: {error_msg}")
        QMessageBox.critical(
            None,
            self.tr("error"),
            self.tr(f"Wrong to load workspace: {error_msg}"),
            QMessageBox.Ok,
        )
        # Reset workspace state.
        self.reset()
        widgets.tableWidget_2.clearContents()

    def enableAutoSave(self, enable):
        """Enable or disable auto-save."""
        if enable:
            self.autosave_timer.start(self.autosave_interval)
            logger.info(f"AutoSave enabled, interval: {self.autosave_interval/1000}s")
        else:
            self.autosave_timer.stop()

    def autoSaveHandler(self):
        """Auto-save handler."""
        if self.workspace is None:
            return
        
        logger.info("Auto-saving workspace...")
        self.saveProjectButtonClick(show=False)
        logger.info("Auto-save completed")

    def test(self):
        self.newWorkSpace(os.getcwd(), "test")
        f = os.getcwd() + "/ERRPT.c3d"
        # "\\test\\Data\\lifting+bending\\LDH\\duchunguang\\2021-12-06-17-57_lift.mat"
        memg = emg(f)
        kin = kinematic(f)

        # add people
        p1 = person("Guo Chen", "1995/08/05", "male")

        # add data
        self.workspace.addParticipant(p1, memg, kin)

        self.updateEMGParticipantBox()
        self.updateWorkSpaceParticipantBox()

    # BUTTONS CLICK
    # Post here your functions for clicked buttons
    # ///////////////////////////////////////////////////////////////
    
    def workspaceRemoveSelectedParticipant (self):
        """Remove selected participants."""
        selected_items = widgets.listWidget_3.selectedItems()
        for item in selected_items:
            p_name = item.text()
            p = self.workspace.findParticipant(p_name)
            if p is not None:
                # Remove participant from workspace.
                self.workspace.participants.remove(p)
                del self.workspace.profileList[p.name]
            # Remove item from UI.
            widgets.listWidget_3.takeItem(widgets.listWidget_3.row(item))
        self.updateEMGParticipantBox()

    def emgPageRemoveSelectedParticipant(self):
        """Remove selected participants from the EMG page."""
        # Get selected participants from the EMG page.
        selected_participants = list(self.selectedParticipants)
        
        if not selected_participants:
            QMessageBox.critical(
                None,
                self.tr("error"),
                self.tr("No participant selected!"),
                QMessageBox.Ok,
            )
            return
            
        # Confirm deletion.
        reply = QMessageBox.question(
            None,
            self.tr("confirm"),
            self.tr("Are you sure to remove selected participant(s)?"),
            QMessageBox.Yes | QMessageBox.No,
        )
        
        if reply == QMessageBox.No:
            return
            
        # Perform deletion.
        for p_name in selected_participants:
            p = self.workspace.findParticipant(p_name)
            if p is not None:
            # Remove participant from workspace.
                self.workspace.participants.remove(p)
                del self.workspace.profileList[p.name]
                
        # Clear selection set.
        self.selectedParticipants.clear()
        
        # Refresh UI.
        self.updateEMGParticipantBox()
        self.updateWorkSpaceParticipantBox()

    def workspaceToggleSelectAllParticipant(self, state):
        """Select all or clear all participants."""
        # Temporarily disconnect listWidget_3 signal.
        widgets.listWidget_3.itemChanged.disconnect(self.checkWorkspaceParticipantSelectState)

        state = not not state  # Convert state to boolean.
        for i in range(widgets.listWidget_3.count()):
            item = widgets.listWidget_3.item(i)
            item.setCheckState(Qt.Checked if state else Qt.Unchecked)
        
        # Reconnect listWidget_3 signal.
        widgets.listWidget_3.itemChanged.connect(self.checkWorkspaceParticipantSelectState)

    def checkWorkspaceParticipantSelectState(self):
        # Temporarily disconnect checkBox_3 signal.
        widgets.checkBox_3.stateChanged.disconnect(self.workspaceToggleSelectAllParticipant)

        # Check whether any item is unchecked.
        all_checked = True
        for i in range(widgets.listWidget_3.count()):
            if widgets.listWidget_3.item(i).checkState() != Qt.Checked:
                all_checked = False
                break
        
        # If any item is unchecked, clear the select-all checkbox.
        if not all_checked:
            widgets.checkBox_3.setCheckState(Qt.Unchecked)
        else:
            widgets.checkBox_3.setCheckState(Qt.Checked)
        
        # Reconnect checkBox_3 signal.
        widgets.checkBox_3.stateChanged.connect(self.workspaceToggleSelectAllParticipant)


    def buttonClick(self):
        # GET BUTTON CLICKED
        btn = self.sender()
        btnName = btn.objectName()

        # SHOW START PAGE
        if btnName == "btn_start":
            widgets.stackedWidget.setCurrentWidget(widgets.start_page)
            UIFunctions.resetStyle(self, btnName)
            btn.setStyleSheet(UIFunctions.selectMenu(btn.styleSheet()))

        # SHOW EMG PAGE
        if btnName == "btn_emg":
            widgets.stackedWidget.setCurrentWidget(widgets.emg_page)
            UIFunctions.resetStyle(self, btnName)
            btn.setStyleSheet(UIFunctions.selectMenu(btn.styleSheet()))

        # SHOW STATS PAGE
        if btnName == "btn_stats":
            widgets.stackedWidget.setCurrentWidget(widgets.stats_page)  # SET PAGE
            UIFunctions.resetStyle(self, btnName)  # RESET ANOTHERS BUTTONS SELECTED
            btn.setStyleSheet(UIFunctions.selectMenu(btn.styleSheet()))  # SELECT MENU

        if btnName == "btn_kinematic":
            widgets.stackedWidget.setCurrentWidget(widgets.kinematics_page)  # SET PAGE
            UIFunctions.resetStyle(self, btnName)
            btn.setStyleSheet(UIFunctions.selectMenu(btn.styleSheet()))  # SELECT MENU
            self.preloadKinematicPage()
            return

        if btnName == "btn_frequency":
            widgets.stackedWidget.setCurrentWidget(widgets.frequency_page)  # SET PAGE
            UIFunctions.resetStyle(self, btnName)
            btn.setStyleSheet(UIFunctions.selectMenu(btn.styleSheet()))  # SELECT MENU
            self.preloadFreqAnalysisPage()

        # SHOW ADVANCED EMG ANALYSIS PAGE
        if btnName == "btn_advanced":
            widgets.stackedWidget.setCurrentWidget(widgets.advanced_page)  # SET PAGE
            UIFunctions.resetStyle(self, btnName)
            btn.setStyleSheet(UIFunctions.selectMenu(btn.styleSheet()))  # SELECT MENU

        if btnName == "btn_save":
            logger.info("Save BTN clicked!")

        # PRINT BTN NAME
        logger.info(f'Button "{btnName}" pressed!')

    def login_click(self):
        return

    def logout_click(self):
        return

    # RESIZE EVENTS
    # ///////////////////////////////////////////////////////////////
    def resizeEvent(self, event):
        # Update Size Grips
        UIFunctions.resize_grips(self)

    # MOUSE CLICK EVENTS
    # //////////////////////////////////////////////////////////////
    def mousePressEvent(self, event):
        # SET DRAG POS WINDOW
        self.dragPos = event.globalPosition().toPoint()

        # PRINT MOUSE EVENTS
        if event.buttons() == Qt.LeftButton:
            logger.info("Mouse click: LEFT CLICK")
        if event.buttons() == Qt.RightButton:
            logger.info("Mouse click: RIGHT CLICK")

    def on_emg_add_window_closed(self, result):
        # Read returned data from child window.
        p, emgdata, kinematic = result
        
        if p is None:
            return

        logger.info("added participate {}".format(p.name))

        # Add to workspace.
        self.workspace.addParticipant(p, emgdata, kinematic)

        # Refresh UI.
        self.updateEMGParticipantBox()
        self.updateWorkSpaceParticipantBox()

        # Trigger follow-up loading flow.
        # self.handle_emg_load_done(p.name)
        self.selectedParticipants.clear()
        self.selectedParticipants.add(p.name)
        self.singleEMGButtonClick()
        self.saveProjectButtonClick(show=False)

    def _set_add_emg_enabled(self, enabled: bool):
        """Enable or disable all 'Load EMG Data' buttons as a group."""
        for btn in (widgets.pushButton_18, widgets.pushButton_161,
                    widgets.pushButton_10):
            btn.setEnabled(enabled)
            btn.setToolTip(
                "" if enabled
                else self.tr("Create or load a workspace first.")
            )

    def stitchDataButtonClicked(self):
        """Merge a separately-recorded EMG file with a kinematics/force-plate
        .c3d into one combined .c3d, for hardware-synced trials that were saved
        to two files instead of one. Writes a new file next to the kinematics
        file; never modifies either source file. Does not require an open
        workspace — loading the result still goes through "Load EMG Data".
        """
        kin_file, _ = QFileDialog.getOpenFileName(
            self, self.tr("Select kinematics / force-plate file"),
            self.home or "", "C3D Files (*.c3d)",
        )
        if not kin_file:
            return

        emg_file, _ = QFileDialog.getOpenFileName(
            self, self.tr("Select EMG file to stitch in"),
            os.path.dirname(kin_file), "EMG Files (*.c3d *.mat)",
        )
        if not emg_file:
            return

        if os.path.normcase(os.path.abspath(kin_file)) == os.path.normcase(os.path.abspath(emg_file)):
            QMessageBox.warning(
                self, self.tr("warning"),
                self.tr("Please select two different files."),
                QMessageBox.Ok,
            )
            return

        try:
            offset_s, trusted, msg = check_alignment(kin_file, emg_file)
        except StitchError as e:
            QMessageBox.critical(self, self.tr("error"), str(e), QMessageBox.Ok)
            return

        if not trusted:
            QMessageBox.warning(
                self, self.tr("Cannot auto-align"),
                self.tr(
                    "These files don't look like a hardware-synced pair:\n\n{}\n\n"
                    "Stitching only proceeds automatically when the offset between "
                    "the two recordings is trustworthy. Use \"Align & Stitch...\" on "
                    "the Kinematics Inspection page to line them up manually instead."
                ).format(msg),
                QMessageBox.Ok,
            )
            return

        try:
            out_path = stitch_c3d(kin_file, emg_file, offset_s=offset_s)
        except StitchError as e:
            QMessageBox.critical(
                self, self.tr("error"),
                self.tr("Could not stitch these files:\n\n{}").format(str(e)),
                QMessageBox.Ok,
            )
            return

        QMessageBox.information(
            self, self.tr("Stitched"),
            self.tr(
                "Wrote a combined kinematics + EMG file:\n\n{}\n\n{}\n\n"
                'Use "Load EMG Data" and select this file to add it as a participant.'
            ).format(out_path, msg),
            QMessageBox.Ok,
        )

    def batchStitchButtonClicked(self):
        """Scan a batch root folder for participant/task pairs recorded as
        separate kinematics + EMG files (e.g. lift.c3d + lift.mat) and stitch
        every trusted pair in one pass -- the folder-wide counterpart to
        "Stitch Your Data". Doesn't require an open workspace; only writes
        new files to disk (loading the result still goes through Batch
        Import, which does require one).
        """
        root = QFileDialog.getExistingDirectory(
            self, self.tr("Select batch root folder to scan for unstitched pairs"),
            self.home or "",
        )
        if not root:
            return

        pairs = find_stitch_pairs(root)
        if not pairs:
            QMessageBox.information(
                self, self.tr("Batch Stitch"),
                self.tr("No separately-recorded kinematics/EMG pairs found in this folder."),
                QMessageBox.Ok,
            )
            return

        pending = [p for p in pairs if p.status == "pending"]
        ambiguous = [p for p in pairs if p.status == "ambiguous"]

        lines = [
            "{}/{}: {} + {}".format(
                p.participant, p.stem, os.path.basename(p.kin_path), os.path.basename(p.emg_path)
            )
            for p in pending
        ]
        msg = self.tr("Found {} pair(s) to stitch:\n\n{}").format(len(pending), "\n".join(lines))
        if ambiguous:
            amb_lines = ["{}/{}: {}".format(p.participant, p.stem, p.message) for p in ambiguous]
            msg += self.tr("\n\nSkipped ({} ambiguous -- review manually):\n{}").format(
                len(ambiguous), "\n".join(amb_lines)
            )

        if not pending:
            QMessageBox.warning(self, self.tr("Batch Stitch"), msg, QMessageBox.Ok)
            return

        reply = QMessageBox.question(
            self, self.tr("Batch Stitch"), msg + self.tr("\n\nStitch these now?"),
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self._startBatchStitchWorker(pending, self._onBatchStitchFinished)

    def _startBatchStitchWorker(self, pairs, on_finished):
        """Run BatchStitchWorker with a progress dialog. Shared by the
        standalone "Batch Stitch..." button and Batch Import's own
        stitch-first prompt (see batchImportButtonClicked) -- on_finished(
        pairs) is called once every pair has been attempted.
        """
        self._stitch_progress = QProgressDialog(
            self.tr("Stitching pairs..."), self.tr("Cancel"), 0, len(pairs), self
        )
        self._stitch_progress.setWindowTitle(self.tr("Myotion-ing"))
        self._stitch_progress.setWindowModality(Qt.WindowModal)
        self._stitch_progress.setMinimumDuration(0)
        self._stitch_progress.setValue(0)

        self._stitch_worker = BatchStitchWorker(pairs, self)
        self._stitch_worker.progress.connect(self._onBatchStitchProgress)
        self._stitch_worker.finished.connect(on_finished)
        self._stitch_progress.canceled.connect(self._stitch_worker.requestInterruption)
        self._stitch_worker.start()

    def _onBatchStitchProgress(self, count, name):
        self._stitch_progress.setValue(count)
        self._stitch_progress.setLabelText(self.tr("Stitching: {}").format(name))

    def _onBatchStitchFinished(self, pairs):
        self._stitch_progress.setValue(self._stitch_progress.maximum())
        self._stitch_progress.close()

        stitched = [p for p in pairs if p.status == "stitched"]
        failed = [p for p in pairs if p.status in ("untrusted", "error")]

        msg = self.tr("Stitched {} pair(s).").format(len(stitched))
        if stitched:
            msg += "\n\n" + "\n".join(
                "{}/{}: wrote {}".format(p.participant, p.stem, os.path.basename(p.out_path))
                for p in stitched
            )
        if failed:
            fail_lines = "\n".join("{}/{}: {}".format(p.participant, p.stem, p.message) for p in failed)
            msg += self.tr(
                '\n\n{} pair(s) need manual alignment ("Align && Stitch..." on the '
                "Kinematics Inspection page):\n{}"
            ).format(len(failed), fail_lines)
        if stitched:
            msg += self.tr(
                '\n\nUse "Batch Import..." next, pointing the task file at one of the '
                "_stitched.c3d files."
            )

        QMessageBox.information(self, self.tr("Batch Stitch"), msg, QMessageBox.Ok)

    def alignStitchButtonClicked(self):
        """Open the manual alignment/stitch dialog for kinematics + EMG file
        pairs that stitchDataButtonClicked() couldn't merge automatically."""
        dlg = StitchAlignmentDialog(parent=self)
        dlg.run()

    def addEMGButtonClick(self):
        if self.workspace is None:
            QMessageBox.warning(
                self, self.tr("warning"),
                self.tr(
                    "No workspace is open.\n"
                    "Please create a new project or load an existing one first."
                ),
                QMessageBox.Ok,
            )
            return
        # create person
        self.emg_add_window = EMGAddWindow(self.workspace, self.home, 1200, 800)
        
        # Connect window close signal to slot.
        self.emg_add_window.finished.connect(self.on_emg_add_window_closed)
        self.emg_add_window.batchImportRequested.connect(self.batchImportButtonClicked)

        # Show window.
        self.emg_add_window.run()  # No direct return needed; results are delivered via signal.


    def configButtonClick(self):
        rc = ConfigWindow(1200, 800).run()

    def underDevelopmentClick(self):
        """Show a notice for features that are not yet implemented."""
        mb = QMessageBox(self)
        mb.setWindowTitle(self.tr("Under Development"))
        mb.setIcon(QMessageBox.Icon.Information)
        mb.setText(self.tr("This feature is still under development."))
        mb.setInformativeText(self.tr("It will be available in a future release."))
        mb.setStandardButtons(QMessageBox.StandardButton.Ok)
        mb.exec()

    def fileMenuClick(self):
        """Drop-down under the File menu button."""
        menu = QMenu(self)
        menu.addAction(self.tr("New Workspace"),  self.newProjectButtonClick)
        menu.addAction(self.tr("Load Workspace"), self.loadProjectButtonClick)
        # Pop the menu directly below the button.
        btn = widgets.fileMenu
        menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def displayMenuClick(self):
        """Display menu button: toggles directly between dark/light -- only
        two states exist, so a dropdown menu is an unneeded extra click."""
        next_mode = "light" if self._theme_mode == "dark" else "dark"
        self.applyTheme(next_mode)

    def showAboutDialog(self):
        AboutDialog.show_about(self)

    def applyTheme(self, mode: str):
        """Load the QSS theme file then re-apply modern widget palette for contrast."""
        themes_dir = os.path.join(os.path.dirname(__file__), "themes")
        if mode == "dark":
            qss_path = os.path.join(themes_dir, "py_dracula_dark.qss")
        elif mode == "light":
            qss_path = os.path.join(themes_dir, "py_dracula_light.qss")
        else:
            return  # unknown mode — no-op
        try:
            with open(qss_path, "r", encoding="utf-8") as f:
                qss = f.read()
            # Set the theme base first, then append mode-aware contrast rules.
            self.ui.styleSheet.setStyleSheet(qss)
            self.applyModernWidgetStyle(mode)
            self._applyThemeWidgetOverrides(mode)
            self._theme_mode = mode
            # Label names the mode a click switches *into* (not the current one).
            widgets.displayMenu.setText(
                self.tr("Light Mode") if mode == "dark" else self.tr("Dark Mode")
            )
        except Exception as e:
            QMessageBox.warning(self, self.tr("Theme Error"),
                                self.tr(f"Could not load theme: {e}"))

    def _applyThemeWidgetOverrides(self, mode: str):
        """Override per-widget inline styles from setupUi that don't change with the QSS.

        setupUi() sets hardcoded dark-mode colors on individual widgets via setStyleSheet().
        Because a widget's own stylesheet beats its parent's, the QSS file alone can't fix
        them.  This method re-applies the correct values for each mode.
        """
        if mode == "light":
            # EMG processing panel: remove near-black background so the light page bg shows
            widgets.data_process.setStyleSheet("background-color: transparent; border: none;")
            widgets.data_process_instruction.setStyleSheet(
                "border-left: 1px solid rgba(0,0,0,0.12);"
            )
            widgets.toolBox.setStyleSheet(
                "color: rgba(0,0,0,0.7); border: none; font-weight: bold;"
            )
            widgets.label_11.setStyleSheet(
                "font-weight: bold; font-size: 12px; color: rgba(0,0,0,0.75);"
                " margin-left: 4px; border: none;"
            )
            widgets.label_12.setStyleSheet(
                "font-weight: bold; font-size: 12px; color: rgba(0,0,0,0.75);"
                " margin-left: 4px; border: none;"
            )
            # Top bar menu buttons: dark bg clashes with the purple header in light mode
            _menu_btn = (
                "background-color: rgba(255,255,255,0.15);"
                " border-radius: 24px; padding: 6px 0px; color: #f8f8f2;"
            )
            for btn in [widgets.fileMenu, widgets.displayMenu, widgets.toolsMenu,
                        widgets.settingsMenu, widgets.helpMenu]:
                btn.setStyleSheet(_menu_btn)
            # Start-page sign in / sign up buttons
            widgets.signInButton.setStyleSheet(
                "background-color: #6272a4; color: #f8f8f2;"
            )
            widgets.signUpButton.setStyleSheet(
                "background-color: #6272a4; color: #f8f8f2;"
            )
            # Workspace / participant sidebar (sidebar is dark purple in both themes)
            _sidebar_tree = (
                "font-size: 14px; color: rgba(255,255,255,0.85);"
                " background-color: #3a405c; border: none;"
            )
            widgets.treeView.setStyleSheet(_sidebar_tree)
            widgets.listWidget_3.setStyleSheet(
                "background-color: #3a405c; color: rgba(255,255,255,0.8); border: none;"
            )
            widgets.pushButton_17.setStyleSheet("background-color: #6272a4; margin: 3px 2px;")
            widgets.pushButton_18.setStyleSheet("background-color: #6272a4; margin: 3px 2px;")
            widgets.frame_55.setStyleSheet("border-top: 1px solid rgba(0,0,0,0.15);")
            widgets.frame_51.setStyleSheet("border-top: 1px solid rgba(0,0,0,0.15);")
            # Frequency page — restore light-mode backgrounds and label colors
            _freq_bg = "background-color: #f4f4f4; border: none;"
            widgets.frequency_top.setStyleSheet(_freq_bg)
            widgets.frequency_bottom.setStyleSheet(_freq_bg)
            widgets.frequency_right.setStyleSheet("background-color: #f4f4f4;")
            _freq_lbl = "font-weight: bold; font-size: 14px; color: rgba(0,0,0,0.4);"
            widgets.label_15.setStyleSheet(_freq_lbl)
            widgets.label_40.setStyleSheet(_freq_lbl)
            widgets.label_16.setStyleSheet(_freq_lbl)
            widgets.label_50.setStyleSheet(_freq_lbl)
            widgets.label_13.setStyleSheet("color: rgba(0,0,0,0.8); font-weight: bold;")
            widgets.label_14.setStyleSheet(
                "font-weight: bold; font-size: 14px; color: rgba(0,0,0,0.4);"
            )
            widgets.label.setStyleSheet("color: rgba(0,0,0,0.8); font-weight: bold;")
            _freq_input = (
                "background-color: rgb(255,255,255);"
                " color: rgba(0,0,0,0.6); font-weight: bold;"
                " border: 1px solid #c0c8dc; border-radius: 4px;"
            )
            widgets.lineEdit_4.setStyleSheet(_freq_input)
            widgets.lineEdit_5.setStyleSheet(_freq_input)
            widgets.lineEdit_6.setStyleSheet(_freq_input)
            _freq_combo = (
                "background-color: rgb(255,255,255); margin: 0px 10px;"
                " font-weight: bold; font-size: 14px; color: rgba(0,0,0,1);"
            )
            widgets.comboBox_19.setStyleSheet(_freq_combo)
            widgets.comboBox_20.setStyleSheet(_freq_combo)
            widgets.frequency_participants.setStyleSheet(
                "font-size: 11px; color: #44475a;"
            )
            # Frequency action buttons — use theme accent color in light mode
            _freq_btn = "background-color: #6272a4; color: #f8f8f2; margin: 3px 2px;"
            widgets.pushButton_28.setStyleSheet(_freq_btn)
            widgets.pushButton_30.setStyleSheet(_freq_btn)
            widgets.pushButton_29.setStyleSheet(
                "background-color: #6272a4; color: #f8f8f2;"
            )
            widgets.pushButton_32.setStyleSheet(
                "background-color: #6272a4; color: #f8f8f2;"
            )
            # EMG Configuration File panel — restore light-mode colors
            widgets.frame_25.setStyleSheet("background-color: #f4f4f4; border: none;")
            widgets.label_21.setStyleSheet(
                "font-weight: bold; font-size: 12px; color: rgba(0,0,0,0.8);"
                " margin-left: 4px; border: none;"
            )
            widgets.pushButton_15.setStyleSheet(
                "background-color: rgba(0,0,0,0.8); margin: 3px 2px;"
            )
            widgets.listWidget_2.setStyleSheet(
                "font-size: 11px; color: rgba(0,0,0,0.5);"
            )
        else:  # dark — restore the original dark-mode inline values
            widgets.data_process.setStyleSheet("background-color: #21242b; border: none;")
            widgets.data_process_instruction.setStyleSheet(
                "border-left: 1px solid rgba(255,255,255,0.1);"
            )
            widgets.toolBox.setStyleSheet(
                "color: rgba(255,255,255,0.75); border: none; font-weight: bold;"
            )
            widgets.label_11.setStyleSheet(
                "font-weight: bold; font-size: 12px; color: rgba(255,255,255,0.85);"
                " margin-left: 4px; border: none;"
            )
            widgets.label_12.setStyleSheet(
                "font-weight: bold; font-size: 12px; color: rgba(255,255,255,0.85);"
                " margin-left: 4px; border: none;"
            )
            _menu_btn = (
                "background-color: #2a2e37; border-radius: 24px; padding: 6px 0px;"
            )
            for btn in [widgets.fileMenu, widgets.displayMenu, widgets.toolsMenu,
                        widgets.settingsMenu, widgets.helpMenu]:
                btn.setStyleSheet(_menu_btn)
            widgets.signInButton.setStyleSheet("background-color: rgb(52, 59, 72);")
            widgets.signUpButton.setStyleSheet("background-color: rgb(52, 59, 72);")
            _sidebar_tree = (
                "font-size: 14px; color: rgba(255,255,255,0.85);"
                " background-color: #3a405c; border: none;"
            )
            widgets.treeView.setStyleSheet(_sidebar_tree)
            widgets.listWidget_3.setStyleSheet(
                "background-color: #3a405c; color: rgba(255,255,255,0.8); border: none;"
            )
            widgets.pushButton_17.setStyleSheet(
                "background-color: rgba(255,255,255,0.15); margin: 3px 2px;"
            )
            widgets.pushButton_18.setStyleSheet(
                "background-color: rgba(255,255,255,0.15); margin: 3px 2px;"
            )
            widgets.frame_55.setStyleSheet("border-top: 1px solid rgba(255,255,255,0.2);")
            widgets.frame_51.setStyleSheet("border-top: 1px solid rgba(255,255,255,0.2);")
            # Frequency page — restore dark-mode backgrounds and label colors
            _freq_bg = "background-color: #21242b; border: none;"
            widgets.frequency_top.setStyleSheet(_freq_bg)
            widgets.frequency_bottom.setStyleSheet(_freq_bg)
            widgets.frequency_right.setStyleSheet("background-color: #21242b;")
            _freq_lbl = "font-weight: bold; font-size: 14px; color: rgba(255,255,255,0.7);"
            widgets.label_15.setStyleSheet(_freq_lbl)
            widgets.label_40.setStyleSheet(_freq_lbl)
            widgets.label_16.setStyleSheet(_freq_lbl)
            widgets.label_50.setStyleSheet(_freq_lbl)
            widgets.label_13.setStyleSheet(
                "color: rgba(255,255,255,0.7); font-weight: bold;"
            )
            widgets.label_14.setStyleSheet(
                "font-weight: bold; font-size: 14px; color: rgba(255,255,255,0.6);"
            )
            widgets.label.setStyleSheet(
                "color: rgba(255,255,255,0.7); font-weight: bold;"
            )
            _freq_input = (
                "background-color: rgb(44,49,60);"
                " color: rgba(255,255,255,0.85); font-weight: bold;"
                " border: 1px solid rgba(255,255,255,0.2); border-radius: 4px;"
            )
            widgets.lineEdit_4.setStyleSheet(_freq_input)
            widgets.lineEdit_5.setStyleSheet(_freq_input)
            widgets.lineEdit_6.setStyleSheet(_freq_input)
            _freq_combo = (
                "background-color: rgb(33,37,43); margin: 0px 10px;"
                " font-weight: bold; font-size: 14px; color: rgba(255,255,255,0.85);"
            )
            widgets.comboBox_19.setStyleSheet(_freq_combo)
            widgets.comboBox_20.setStyleSheet(_freq_combo)
            widgets.frequency_participants.setStyleSheet(
                "font-size: 11px; color: #f4f4f4;"
            )
            widgets.pushButton_28.setStyleSheet(
                "background-color: rgba(255,255,255,0.15); margin: 3px 2px;"
            )
            widgets.pushButton_30.setStyleSheet(
                "background-color: rgba(255,255,255,0.15); margin: 3px 2px;"
            )
            widgets.pushButton_29.setStyleSheet("background-color: rgb(52, 59, 72);")
            widgets.pushButton_32.setStyleSheet("background-color: rgb(52, 59, 72);")
            # EMG Configuration File panel — restore dark-mode colors
            widgets.frame_25.setStyleSheet("background-color: #21242b; border: none;")
            widgets.label_21.setStyleSheet(
                "font-weight: bold; font-size: 12px; color: rgba(255,255,255,0.85);"
                " margin-left: 4px; border: none;"
            )
            widgets.pushButton_15.setStyleSheet(
                "background-color: rgba(255,255,255,0.15); margin: 3px 2px;"
            )
            widgets.listWidget_2.setStyleSheet(
                "font-size: 11px; color: rgba(255,255,255,0.7);"
            )

    def ifOldProjectOpened(self):
        if self.workspace is not None:
            relpy = QMessageBox.question(
                None,
                "warning",
                "Current workspace not closed, do you want to save and continue?",
                QMessageBox.Yes | QMessageBox.No,
            )

            if relpy == QMessageBox.Yes:
                self.saveWorkSpace()
                self.reset()
                return 0
            else:
                return -1
        return 0

    def clearWorkspaceButtonClick(self):
        """Close the current workspace in memory. Files on disk are NOT deleted."""
        if self.workspace is None:
            QMessageBox.information(
                self, self.tr("Clear Workspace"),
                self.tr("No workspace is currently open."),
                QMessageBox.Ok,
            )
            return
        reply = QMessageBox.warning(
            self,
            self.tr("Clear Workspace"),
            self.tr(
                "This will close '{}' and clear all in-memory data.\n"
                "Files on disk are NOT deleted — you can reload the project at any time.\n\n"
                "Continue?"
            ).format(self.workspace.name),
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self.reset()
        self._set_add_emg_enabled(False)
        self.updateWorkProjectTreeWidget()
        self.updateEMGParticipantBox()
        self.updateWorkSpaceParticipantBox()
        self.updateEMGSavedConfigureList()
        # Kinematics Inspection / EMG Frequency Domain participant lists are
        # only refreshed when the user navigates to those tabs, so clear them
        # here too rather than leaving stale entries until the next visit.
        widgets.kinematics_label_tree.clear()
        widgets.frequency_participants.clear()
        widgets.stats_page.on_workspace_changed(None)
        # singleEMG is now (None, None, None) after reset() — this hides the
        # Time Domain "Previous/Current Process" plots instead of leaving the
        # last-viewed channel's waveform on screen.
        self.updateEMGSignalProcessPanel()
        self.freqAnalysisPlots.clear()
        widgets.scrollArea_3.deleteAllPages()
        # EMG pipeline step cards (Analysis Segment's Step 1-6 panel)
        self._pipeline_panel.clear()
        # Kinematics Inspection 3D render + "Signals" waveform panel
        widgets.renderWidget.setModel(None)
        widgets.kinematic_analysis.clear()
        # Frequency Domain segment waveform + its Start/End Time fields
        widgets.freq_timedomain.hide()
        widgets.lineEdit_5.setText("")
        widgets.lineEdit_4.setText("")

    def newProjectButtonClick(self):
        if self.ifOldProjectOpened():
            return -1

        filename = QFileDialog.getSaveFileName(
            None,
            "New Project",
            self.home,
            "Project files (*.myo)",
            None,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks,
        )
        if filename[0] == "":
            return

        proj_full_name = os.path.basename(filename[0])
        dir = filename[0][: -len(proj_full_name)]

        proj_name = proj_full_name[: -len(PROJ_EXT)]
        if not checkValidPath(dir):
            QMessageBox.critical(
                None,
                self.tr("error"),
                self.tr("Selected path does not exist!"),
                QMessageBox.Ok,
            )

        p = Path(dir)
        if self.newWorkSpace(p, proj_name):
            QMessageBox.critical(
                None,
                self.tr("error"),
                self.tr("Failed to create new Workspace!"),
                QMessageBox.Ok,
            )

        logger.info("workspace path: {}".format(self.home))
        logger.info("workspace name: {}".format(self.workspace.name))

        # Jump to EMG page
        widgets.stackedWidget.setCurrentWidget(widgets.emg_page)

    def saveProjectButtonClick(self, show=True):
        if self.workspace is None:
            logger.info("workspace is empty, nothing to save")
            return

        if self.saveWorkSpace():
            QMessageBox.critical(
                None,
                self.tr("error"),
                self.tr("Failed to save Workspace!"),
                QMessageBox.Ok,
            )
            return
        
        if show:
            QMessageBox.information(None, "save", "Workspace saved!", QMessageBox.Ok)
        logger.info("workspace is saved")

    def loadProjectButtonClick(self):
        if self.ifOldProjectOpened():
            return -1

        filepath, extension = QFileDialog.getOpenFileNames(
            None,
            caption=self.tr("open Project file"),
            dir=MyotionPath,
            filter=self.tr("Project Files (*.myo)"),
        )

        if len(filepath) == 0:
            return

        file = os.path.basename(filepath[0])
        path = filepath[0][: -len(file)]

        try:
            if self.loadWorkSpace(path, file):
                QMessageBox.critical(
                    None,
                    self.tr("error"),
                    self.tr("Failed to load Workspace!"),
                    QMessageBox.Ok,
                )
                return
        except Exception as e:
            QMessageBox.critical(
                None,
                self.tr("error"),
                self.tr(f"Failed to load Workspace: {str(e)}"),
                QMessageBox.Ok,
            )
            return

        # Jump to EMG page
        widgets.stackedWidget.setCurrentWidget(widgets.emg_page)

    def singleEMGButtonClick(self):
        p, step, chan = self.singleEMG

        if len(self.selectedParticipants) == 0:
            QMessageBox.critical(
                None,
                self.tr("error"),
                self.tr("No participant selected!"),
                QMessageBox.Ok,
            )
            return

        if len(self.selectedParticipants) > 1:
            QMessageBox.critical(
                None,
                self.tr("error"),
                self.tr("Only one participant can be selected!"),
                QMessageBox.Ok,
            )
            return

        if p is not None:
            reply = QMessageBox.question(
                None,
                self.tr("Attention"),
                self.tr("Current EMG process is not finished! Do you want to start a new process?"),
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Cancel
            )
            if reply == QMessageBox.Yes:
                self.singleEMG = (None, None, None)
                self.inputBuffer = None
                self.outputBuffer = None
            else:
                return
        p_name = self.selectedParticipants.pop()
        p = self.workspace.findParticipant(p_name)
        self.startSingleEMGProcess(p)

    def participantCheckBoxChanged(self, state):
        sender = self.sender()
        p = sender.objectName()

        if Qt.CheckState(state) == Qt.Checked:
            self.selectedParticipants.add(p)
        else:
            if p in self.selectedParticipants:
                self.selectedParticipants.remove(p)

        # Sync listWidget_3 state.
        self.syncListWidgetWithSelectedParticipants()
        self.updateBatchProcessButtonState()
        
    def syncListWidgetWithSelectedParticipants(self):
        """Sync selectedParticipants to listWidget_3."""
        # Temporarily block signals to avoid recursive triggers.
        widgets.listWidget_3.blockSignals(True)
        
        # Update selection state in listWidget_3.
        for i in range(widgets.listWidget_3.count()):
            item = widgets.listWidget_3.item(i)
            p_name = item.text()
            if p_name in self.selectedParticipants:
                item.setCheckState(Qt.Checked)
            else:
                item.setCheckState(Qt.Unchecked)
                
        widgets.listWidget_3.blockSignals(False)

    def listWidgetItemChanged(self, item):
        """Handle state changes of listWidget_3 items."""
        p_name = item.text()
        
        if item.checkState() == Qt.Checked:
            self.selectedParticipants.add(p_name)
        else:
            if p_name in self.selectedParticipants:
                self.selectedParticipants.remove(p_name)
                
        # Sync tableWidget_2 state.
        self.syncTableWidgetWithSelectedParticipants()
        
    def syncTableWidgetWithSelectedParticipants(self):
        """Sync selectedParticipants to tableWidget_2."""
        # Traverse all checkboxes in the table.
        for i in range(widgets.tableWidget_2.rowCount()):
            cell_widget = widgets.tableWidget_2.cellWidget(i, 0)
            if cell_widget:
                checkbox = cell_widget.findChild(QCheckBox)
                if checkbox:
                    p_name = checkbox.objectName()
                    # Temporarily block signals to avoid recursive triggers.
                    checkbox.blockSignals(True)
                    checkbox.setChecked(p_name in self.selectedParticipants)
                    checkbox.blockSignals(False)

    def EMGConfigurationListDoubleClicked(self, item):
        curr = widgets.listWidget.currentRow()
        if curr == self.singleEMG[1]:
            return
        p, step, chan = self.singleEMG
        if p is None:
            return
        cfg = self.workspace[p].emg.getProcessConfig()
        if cfg is None:
            return

        idx = widgets.listWidget.currentRow()
        type, str = cfg.getTypeInfo(idx)
        self.selectSingleEMGStep(widgets.listWidget.currentRow())

    def EMGConfigureToggleConfiguration(self, state):
        p, step, chan = self.singleEMG
        if p is None:
            return
        cfg = self.workspace[p].emg.getProcessConfig()
        if cfg is None:
            return

        state = Qt.CheckState(state) == Qt.CheckState.Checked
        cfg[step].enable = state

        logger.info(
            "EMG process step {}, configuration {} set to {}".format(
                step, cfg.getStepStringList()[step], state
            )
        )
        self.__updateEMGRenderBuffer(prev=False)
        self.updateEMGSignalProcessPanel(prev=False)

    def setupEMGFilterValidators(self):
        """Configure validators for EMG filter inputs with bounded ranges."""
        # Start with a default max; update dynamically based on sampling rate.
        default_max = 1000  # default max value
        
        # Create integer validators with range 0..default_max.
        validator_band_high = QIntValidator(0, default_max, self)
        validator_band_low = QIntValidator(0, default_max, self)
        validator_low_pass = QIntValidator(0, default_max, self)
        
        # Apply validators to input fields.
        widgets.lineEdit_10.setValidator(validator_band_high)
        widgets.lineEdit_11.setValidator(validator_band_low)
        widgets.lineEdit_12.setValidator(validator_low_pass)
        
        # Keep validator references for later updates.
        self.validator_band_high = validator_band_high
        self.validator_band_low = validator_band_low
        self.validator_low_pass = validator_low_pass

    def updateEMGFilterValidators(self, p):
        """Update filter-input validator ranges based on sampling rate."""
        if p is None:
            return
            
        try:
            fs = self.workspace[p].emg.getfs()
            max_freq = fs / 2
            
            # Update validator ranges.
            self.validator_band_high.setTop(max_freq)
            self.validator_band_low.setTop(max_freq)
            self.validator_low_pass.setTop(max_freq)
            
            # Update placeholder text.
            widgets.lineEdit_10.setPlaceholderText(f"high: 0-{max_freq}")
            widgets.lineEdit_11.setPlaceholderText(f"low: 0-{max_freq}")
            widgets.lineEdit_12.setPlaceholderText(f"low: 0-{max_freq}")
            
            logger.info(f"EMG filter validators updated with max frequency: {max_freq}")
        except Exception as e:
            logger.error(f"Failed to update EMG filter validators: {e}")

    def EMGConfigureFilterConfiguration(self):
        p, step, chan = self.singleEMG
        if p is None:
            return
        cfg = self.workspace[p].emg.getProcessConfig()
        if cfg is None:
            return
        # according to UI layout
        filtertypename = {
            0: emgFilterEnum.BAND_PASS,
            1: emgFilterEnum.LOW_PASS,
        }
        filter_type = filtertypename[widgets.comboBox_7.currentIndex()]
        cutoff_b_h_text = widgets.lineEdit_10.text()
        cutoff_b_l_text = widgets.lineEdit_11.text()
        cutoff_l_l_text = widgets.lineEdit_12.text()
        # order set from 2,3,4
        order = widgets.comboBox_8.currentIndex() + 2
        cutoff_b_l = None
        cutoff_b_h = None
        cutoff_l_l = None
        if cutoff_b_l_text != "":
            cutoff_b_l = int(cutoff_b_l_text)
        if cutoff_b_h_text != "":
            cutoff_b_h = int(cutoff_b_h_text)
        if cutoff_l_l_text != "":
            cutoff_l_l = int(cutoff_l_l_text)

        fs = self.workspace[p].emg.getfs()
        # sanity
        if filter_type == emgFilterEnum.BAND_PASS:
            if cutoff_b_h is None or cutoff_b_l is None:
                QMessageBox.critical(
                    None,
                    self.tr("error"),
                    self.tr("cut off frequency is not complete!"),
                    QMessageBox.Ok,
                )
                return
            if (
                cutoff_b_h >= fs / 2
                or cutoff_b_h < 0
                or cutoff_b_l >= fs / 2
                or cutoff_b_l < 0
            ):
                QMessageBox.critical(
                    None,
                    self.tr("error"),
                    self.tr("cut off frequency has to be between 0 and {}!").format(
                        fs / 2
                    ),
                    QMessageBox.Ok,
                )
                return
            if cutoff_b_l >= cutoff_b_h:
                QMessageBox.critical(
                    None,
                    self.tr("error"),
                    self.tr("cut off low has to be smaller than cut off high!"),
                    QMessageBox.Ok,
                )
                return
        elif filter_type == emgFilterEnum.LOW_PASS:
            if cutoff_l_l is None:
                QMessageBox.critical(
                    None,
                    self.tr("error"),
                    self.tr("cut off frequency is not complete!"),
                    QMessageBox.Ok,
                )
                return
            if cutoff_l_l >= fs / 2 or cutoff_l_l < 0:
                QMessageBox.critical(
                    None,
                    self.tr("error"),
                    self.tr("cut off frequency has to be between 0 and {}!").format(
                        fs / 2
                    ),
                    QMessageBox.Ok,
                )
                return

        cfg[step].type = filter_type
        if filter_type == emgFilterEnum.BAND_PASS:
            cfg[step].cutoff_l = cutoff_b_l
            cfg[step].cutoff_h = cutoff_b_h
        elif filter_type == emgFilterEnum.LOW_PASS:
            cfg[step].cutoff_l = cutoff_l_l
        cfg[step].order = order

        logger.info(
            "EMG process step {}, configuration filter,"
            " type {}, high {}, low {}, order {}".format(
                step, filter_type, cutoff_b_h, cutoff_b_l, order
            )
        )

        self.__updateEMGRenderBuffer(prev=False)
        self.updateEMGSignalProcessPanel(prev=False)

    def _onFilterInputChanged(self):
        self._filter_debounce.trigger()

    def _applyFilterPreview(self):
        """Apply filter config from current UI values — silent on incomplete or invalid input."""
        p, step, chan = self.singleEMG
        if p is None:
            return
        cfg = self.workspace[p].emg.getProcessConfig()
        if cfg is None:
            return
        step_type, _ = cfg.getTypeInfo(step)
        if step_type != emgConfigEnum.FILTER:
            return
        filtertypename = {
            0: emgFilterEnum.BAND_PASS,
            1: emgFilterEnum.LOW_PASS,
        }
        filter_type = filtertypename[widgets.comboBox_7.currentIndex()]
        cutoff_b_h_text = widgets.lineEdit_10.text()
        cutoff_b_l_text = widgets.lineEdit_11.text()
        cutoff_l_l_text = widgets.lineEdit_12.text()
        order = widgets.comboBox_8.currentIndex() + 2
        try:
            cutoff_b_h = int(cutoff_b_h_text) if cutoff_b_h_text else None
            cutoff_b_l = int(cutoff_b_l_text) if cutoff_b_l_text else None
            cutoff_l_l = int(cutoff_l_l_text) if cutoff_l_l_text else None
        except ValueError:
            return
        fs = self.workspace[p].emg.getfs()
        if filter_type == emgFilterEnum.BAND_PASS:
            if cutoff_b_h is None or cutoff_b_l is None:
                return
            if cutoff_b_h >= fs / 2 or cutoff_b_h < 0 or cutoff_b_l >= fs / 2 or cutoff_b_l < 0:
                return
            if cutoff_b_l >= cutoff_b_h:
                return
        elif filter_type == emgFilterEnum.LOW_PASS:
            if cutoff_l_l is None:
                return
            if cutoff_l_l >= fs / 2 or cutoff_l_l < 0:
                return
        cfg[step].type = filter_type
        if filter_type == emgFilterEnum.BAND_PASS:
            cfg[step].cutoff_l = cutoff_b_l
            cfg[step].cutoff_h = cutoff_b_h
        elif filter_type == emgFilterEnum.LOW_PASS:
            cfg[step].cutoff_l = cutoff_l_l
        cfg[step].order = order
        self.__updateEMGRenderBuffer(prev=False)
        self.updateEMGSignalProcessPanel(prev=False)

    def EMGChannelSelectorIndexChanged(self, idx):
        p, step, chan = self.singleEMG
        if p is None:
            return
        newchan = widgets.comboBox_2.currentText()
        if chan == newchan:
            return
        logger.info("EMG channel selector index changed to {}".format(newchan))
        self.selectSingleEMGChannel(newchan)

    def EMGChannelToolBoxIndexChanged(self, idx):
        # do not allow user change
        p, step, chan = self.singleEMG
        if p is None:
            return
        cfg = self.workspace[p].emg.getProcessConfig()
        if cfg is None:
            return
        type, str = cfg.getTypeInfo(step)
        # self.selectSingleEMGStep(idx)

    def EMGStepNextButtonClicked(self):
        p, step, chan = self.singleEMG
        if p is None:
            return
        cfg = self.workspace[p].emg.getProcessConfig()
        if cfg is None:
            return

        if step + 1 >= cfg.size():
            QMessageBox.critical(
                None, self.tr("error"), self.tr("end of emg process!"), QMessageBox.Ok
            )
            return

        widgets.listWidget.setCurrentRow(step + 1)
        # equivent to double click on EMG configuration list
        self.EMGConfigurationListDoubleClicked(None)
        self.saveProjectButtonClick(show=False)
        self.EMGSaveConfigurationButtonClicked()

    def EMGGenerateReportButtonClicked(self):
        # sanity
        p, step, chan = self.singleEMG
        if p is None:
            return

        # apply configuration on all chans in EMG and MVC
        try:
            self.workspace[p].emg.processWithConfigure(
                crop_interval=self.workspace[p].crop_interval
            )
        except Exception as e:
            QMessageBox.critical(
                self, self.tr("error"),
                self.tr("Processing failed: {}").format(str(e)),
                QMessageBox.Ok,
            )
            return

        # persist config to saved_emgconfig so batch processing can use it
        self.EMGSaveConfigurationButtonClicked()

        # generate and save report + CSVs to participant folder
        save_ok = False
        try:
            self.workspace.genReport(p)
            self.workspace.saveReport(p, self.home)
            save_ok = True
        except Exception as e:
            QMessageBox.critical(
                self, self.tr("error"),
                self.tr("Failed to save report for {}: {}").format(p.name, str(e)),
                QMessageBox.Ok,
            )

        if save_ok:
            participant_dir = os.path.join(self.home, p.name)
            QMessageBox.information(
                self, self.tr("Saved"),
                self.tr(
                    "Processed EMG results have been saved to the workspace folder:\n{}"
                ).format(participant_dir),
                QMessageBox.Ok,
            )

        # exit single process stage
        self.singleEMG = (None, None, None)
        self._emg_action_bar.hide()
        self.updateEMGSignalProcessPanel()
        self.updateEMGConfigureList()

        self.selectedParticipants.clear()
        self.updateEMGParticipantBox()

    def EMGSaveConfigurationButtonClicked(self):
        p, step, chan = self.singleEMG
        if p is None:
            QMessageBox.critical(
                None,
                self.tr("error"),
                self.tr("Single EMG not started!"),
                QMessageBox.Ok,
            )
            return
        cfg = self.workspace[p].emg.getProcessConfig()
        if cfg is None:
            QMessageBox.critical(
                None,
                self.tr("error"),
                self.tr("EMG process file not available!"),
                QMessageBox.Ok,
            )
            return

        cfgname = p.name + "'s EMGConfig"
        self.workspace.saveEMGConfigure(p, cfgname)
        self.updateEMGSavedConfigureList()

    def EMGBatchProcessButtonClicked(self):
        # sanity for pariticpants
        if len(self.selectedParticipants) == 0:
            QMessageBox.critical(
                None,
                self.tr("error"),
                self.tr("Please select participants first!"),
                QMessageBox.Ok,
            )
            return

        listofpeople = []
        for p_name in self.selectedParticipants:
            p = self.workspace.findParticipant(p_name)
            if p is not None:
                listofpeople.append(p)

        logger.info(
            "batch process: selected participants {}".format(
                [",".join(p.name) for p in listofpeople]
            )
        )

        # if report has been generated, generate warning

        # check configure file
        configureList = self.workspace.getEMGConfigures()
        if len(configureList) == 0:
            QMessageBox.critical(
                None,
                self.tr("error"),
                self.tr(
                    "No saved configuration file found, please use single EMG to generate configure file!"
                ),
                QMessageBox.Ok,
            )
            return

        # get current selected one
        selectedConfigs = widgets.listWidget_2.selectedItems()
        if len(selectedConfigs) >= 1:
            config_name = selectedConfigs[0].text()
            config = configureList[config_name]
        else:
            # pick any one
            config_name = next(iter(configureList))
            config = configureList[config_name]

        logger.info("batch process: select configure {}".format(config_name))

        fs_map = {}
        for p in listofpeople:
            try:
                fs = self.workspace[p].emg.getfs()
                fs_map.setdefault(fs, []).append(p.name)
            except Exception as e:
                logger.error("batch process: failed to read fs for {}: {}".format(p.name, str(e)))

        if len(fs_map) > 1:
            lines = []
            for fs, plist in fs_map.items():
                lines.append("{} Hz: {}".format(fs, ", ".join(plist)))
            reply = QMessageBox.warning(
                None,
                self.tr("warning"),
                self.tr(
                    "Selected participants have mixed sampling rates:\n{}\n\nContinue batch processing?"
                ).format("\n".join(lines)),
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                logger.info("batch process: cancelled due to mixed sampling rates")
                return

        # Same config applies to every enabled channel identically (it is not
        # channel-specific), so participants with different enabled-channel
        # sets form a different cohort — warn before applying one config
        # across a mixed cohort.
        chan_map = {}
        for p in listofpeople:
            try:
                enabled = frozenset(self.workspace[p].emg.enabledChannels)
                chan_map.setdefault(enabled, []).append(p.name)
            except Exception as e:
                logger.error("batch process: failed to read channels for {}: {}".format(p.name, str(e)))

        if len(chan_map) > 1:
            lines = []
            for chans, plist in chan_map.items():
                chan_str = ", ".join(sorted(chans)) if chans else "(none)"
                lines.append("[{}]: {}".format(chan_str, ", ".join(plist)))
            reply = QMessageBox.warning(
                None,
                self.tr("warning"),
                self.tr(
                    "Selected participants have different enabled EMG channels:\n{}\n\nContinue batch processing?"
                ).format("\n".join(lines)),
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                logger.info("batch process: cancelled due to mismatched channel sets")
                return

        # Confirm batch start — show config name, pipeline steps, participant list.
        # (EMGConfigWindow used the old Ui_EMGConfigWindow which is incompatible
        # with the new pipeline config structure, so we bypass it entirely.)
        step_list = "\n".join(
            "  • {}".format(s) for s in config.getStepStringList()
        )
        participant_list = ", ".join(p.name for p in listofpeople)
        reply = QMessageBox.question(
            self,
            self.tr("Start batch processing?"),
            self.tr(
                "Config: {}\n\nPipeline:\n{}\n\nParticipants ({}): {}\n\n"
                "Results will be saved to each participant’s folder.\n\n"
                "Start?"
            ).format(config_name, step_list, len(listofpeople), participant_list),
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.startBatchEMGProcess(listofpeople, config)
        else:
            logger.info("batch process: cancelled")

    def EMGParticipantSelectAllClicked(self, state):
        """Handle select-all checkbox interactions."""
        # Update all table checkboxes.
        state = Qt.CheckState(state)
        widgets.checkBox_2.setCheckState(state)
        
        # Temporarily block individual checkbox signals.
        for i in range(widgets.tableWidget_2.rowCount()):
            checkbox = widgets.tableWidget_2.cellWidget(i, 0).findChild(QCheckBox)
            checkbox.blockSignals(True)
            checkbox.setCheckState(state)
            checkbox.blockSignals(False)

        participants = self.workspace.getFilteredParticipants(self.participant_filter)
        if Qt.CheckState(state) == Qt.Checked:
            for p in participants:
                self.selectedParticipants.add(p.name)
        else:
            for p in participants:
                if p.name in self.selectedParticipants:
                    self.selectedParticipants.remove(p.name)
        
        self.updateBatchProcessButtonState()

    # ------------------------------------------------------------------
    # Batch Import (TOML config + folder scan)
    # ------------------------------------------------------------------

    def batchImportButtonClicked(self):
        if self.workspace is None:
            QMessageBox.warning(
                self, self.tr("warning"),
                self.tr("No workspace is open.\nPlease create or load one first."),
                QMessageBox.Ok,
            )
            return

        # Ask up front rather than leading with a file-open dialog whose
        # "Cancel to pick a folder instead" fallback wasn't obvious -- an
        # explicit choice is clearer about what happens either way.
        choice = QMessageBox(self)
        choice.setWindowTitle(self.tr("Batch Import"))
        choice.setText(self.tr("Does this batch already have a saved config file (.toml)?"))
        with_btn = choice.addButton(self.tr("With config file(s)"), QMessageBox.ButtonRole.AcceptRole)
        without_btn = choice.addButton(self.tr("Without"), QMessageBox.ButtonRole.ActionRole)
        choice.addButton(QMessageBox.StandardButton.Cancel)
        choice.setDefaultButton(with_btn)
        choice.exec()
        clicked = choice.clickedButton()

        if clicked is with_btn:
            config_path, _ = QFileDialog.getOpenFileName(
                self, self.tr("Select batch config (.toml)"),
                self.home or "", "TOML Files (*.toml)",
            )
            if not config_path:
                return
            root = os.path.dirname(config_path)
            try:
                cfg = load_batch_config(config_path)
            except Exception as e:
                QMessageBox.critical(
                    self, self.tr("error"),
                    self.tr("Failed to load config: {}").format(str(e)),
                    QMessageBox.Ok,
                )
                return
            self._continueBatchImport(root, cfg, config_path)
        elif clicked is without_btn:
            root = QFileDialog.getExistingDirectory(
                self, self.tr("Select batch root folder"), self.home or ""
            )
            if not root:
                return
            self._offerUpfrontStitch(root)
        # else: Cancel -- do nothing

    def _offerUpfrontStitch(self, root):
        """Quick heads-up before making the user hand-build a config for a
        fresh batch root: kinematics/EMG recorded as separate files per task
        is common enough (see batch_stitch.py) that it's worth checking for
        upfront, rather than only discovering the need after the user has
        already picked a task-file glob that turns out to match nothing
        usable. Deliberately simple -- reuses find_stitch_pairs/stitch_all
        as-is, no new pairing heuristics:
          - "pending" pairs (unambiguous kin+EMG split, same stem) -> offer
            to stitch right now.
          - "ambiguous" pairs (e.g. two same-extension files with no shared
            stem, so nothing here can safely guess which two go together)
            -> just a heads-up pointing at manual "Align & Stitch...".
        """
        pairs = find_stitch_pairs(root)
        pending = [p for p in pairs if p.status == "pending"]
        ambiguous = [p for p in pairs if p.status == "ambiguous"]

        if ambiguous:
            preview = ", ".join("{}/{}".format(p.participant, p.stem) for p in ambiguous[:3])
            if len(ambiguous) > 3:
                preview += ", ..."
            QMessageBox.information(
                self, self.tr("Batch Import"),
                self.tr(
                    "{} item(s) look like they may need pairing but don't match a "
                    'recognizable kinematics/EMG naming pattern (e.g. {}). These '
                    'will need manual alignment via "Align & Stitch..." on the '
                    "Kinematics Inspection page."
                ).format(len(ambiguous), preview),
                QMessageBox.Ok,
            )

        if pending:
            preview = ", ".join("{}/{}".format(p.participant, p.stem) for p in pending[:3])
            if len(pending) > 3:
                preview += ", ..."
            reply = QMessageBox.question(
                self, self.tr("Batch Import"),
                self.tr(
                    "This folder has {} separately-recorded kinematics/EMG pair(s) "
                    "(e.g. {}). Stitch them now before setting up the batch config?"
                ).format(len(pending), preview),
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self._startBatchStitchWorker(
                    pending, lambda done: self._onUpfrontStitchFinished(done, root)
                )
            # Either way, don't fall through into building a config against
            # data that (per the user's own answer) may still need stitching
            # -- declining just cancels Batch Import back to whatever was
            # already open, same as Cancel on the folder picker itself.
            return

        self._buildNewBatchConfig(root)

    def _onUpfrontStitchFinished(self, pairs, root):
        self._stitch_progress.setValue(self._stitch_progress.maximum())
        self._stitch_progress.close()

        failed = [p for p in pairs if p.status in ("untrusted", "error")]
        if failed:
            fail_lines = "\n".join(
                "{}/{}: {}".format(p.participant, p.stem, p.message) for p in failed
            )
            QMessageBox.warning(
                self, self.tr("Batch Stitch"),
                self.tr(
                    '{} pair(s) need manual alignment ("Align && Stitch..." on the '
                    "Kinematics Inspection page):\n{}"
                ).format(len(failed), fail_lines),
                QMessageBox.Ok,
            )

        self._buildNewBatchConfig(root)

    def _buildNewBatchConfig(self, root):
        dlg = BatchConfigDialog(BatchConfig(), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        cfg = dlg.get_config()
        siblings = dlg.sibling_task_configs()
        config_path = self._saveDetectedBatchConfigs(root, cfg, siblings)
        self._continueBatchImport(root, cfg, config_path)

    def _saveDetectedBatchConfigs(self, root, cfg, siblings):
        """Persist a freshly-built BatchConfig -- plus any sibling per-task
        configs "Detect from folder" found alongside it (e.g. lift/squat/gait
        recorded for the same cohort) -- to the batch root as .toml files,
        so a later Batch Import for this folder can just pick a file instead
        of rebuilding everything from scratch. Returns the path saved for
        `cfg` itself (used as this session's config_path).

        Never overwrites an existing file at the target name -- if one is
        already there (e.g. re-running detection on the same folder), it's
        left untouched and reported as already existing.
        """
        def _safe_name(task_type):
            name = re.sub(r'[^\w\-]+', '_', task_type.strip()) if task_type else ""
            return "batch_config_{}".format(name) if name else "batch_config"

        chosen_path = None
        saved, skipped = [], []
        seen_paths = set()
        for c in [cfg] + list(siblings):
            path = os.path.join(root, "{}.toml".format(_safe_name(c.layout.task_type)))
            if c is cfg:
                chosen_path = path
            if path in seen_paths:
                # Two detected "tasks" collapsed to the same config name
                # (e.g. differing only by file extension) -- keep whichever
                # one got here first rather than re-checking/reporting the
                # same path twice.
                continue
            seen_paths.add(path)
            if os.path.isfile(path):
                skipped.append(path)
                continue
            try:
                save_batch_config(path, c)
                saved.append(path)
            except Exception as e:
                logger.error("Failed to save batch config {}: {}".format(path, e))

        if saved or skipped:
            parts = []
            if saved:
                parts.append(self.tr("Created:\n") + "\n".join(saved))
            if skipped:
                parts.append(self.tr("Already existed, left untouched:\n") + "\n".join(skipped))
            QMessageBox.information(
                self, self.tr("Batch Import"),
                self.tr("Saved batch config file(s) to {}:\n\n{}").format(root, "\n\n".join(parts)),
                QMessageBox.Ok,
            )
        return chosen_path

    def _continueBatchImport(self, root, cfg, config_path):
        candidates = scan_batch_folder(root, cfg.layout)
        ready = [c for c in candidates if c.status == "ready"]
        not_ready = [c for c in candidates if c.status != "ready"]

        if not ready:
            # This folder might just need stitching first -- e.g. a
            # participant/task pair recorded as separate kinematics + EMG
            # files, which scan_batch_folder correctly can't validate as one
            # usable trial. Offer to fix that instead of just failing.
            pending_stitch = [p for p in find_stitch_pairs(root) if p.status == "pending"]
            if pending_stitch:
                preview = ", ".join(
                    "{}/{}".format(p.participant, p.stem) for p in pending_stitch[:3]
                )
                if len(pending_stitch) > 3:
                    preview += ", ..."
                reply = QMessageBox.question(
                    self, self.tr("Batch Import"),
                    self.tr(
                        "No usable participants found with the current config, but this "
                        "folder looks like it has {} separately-recorded kinematics/EMG "
                        "pair(s) that need stitching first (e.g. {}).\n\n"
                        "Stitch them now and retry?"
                    ).format(len(pending_stitch), preview),
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply == QMessageBox.Yes:
                    self._startBatchStitchWorker(
                        pending_stitch,
                        lambda done: self._onInlineStitchFinished(done, root, cfg, config_path),
                    )
                    return
            QMessageBox.warning(
                self, self.tr("Batch Import"),
                self.tr("No usable participants found in this folder with the current config."),
                QMessageBox.Ok,
            )
            return

        # Same-cohort requirement: keep only the largest channel-signature
        # group (mirrors the mismatched-channel warning EMGBatchProcessButtonClicked
        # already applies to already-loaded participants).
        groups = channel_signature_groups(ready)
        cohort_sig = max(groups, key=lambda k: len(groups[k]))
        in_cohort = [c for c in ready if frozenset(c.channels) == cohort_sig]
        out_of_cohort = [c for c in ready if frozenset(c.channels) != cohort_sig]

        already_loaded = [c for c in in_cohort if self.workspace.findParticipant(c.name) is not None]
        already_loaded_names = {c.name for c in already_loaded}
        in_cohort = [c for c in in_cohort if c.name not in already_loaded_names]

        skip_lines = ["{}: {}".format(c.name, c.message) for c in not_ready]
        skip_lines += [
            "{}: different channel set than the majority cohort".format(c.name)
            for c in out_of_cohort
        ]
        skip_lines += [
            "{}: already in the workspace, skipped".format(c.name) for c in already_loaded
        ]

        if not in_cohort:
            QMessageBox.warning(
                self, self.tr("Batch Import"),
                self.tr("No participants left to import:\n{}").format("\n".join(skip_lines)),
                QMessageBox.Ok,
            )
            return

        # Channel mapping: map-once (empty config) or confirm (already mapped).
        if cfg.channel_mapping.is_empty():
            sample = in_cohort[0]
            map_dialog = QDialog(self)
            map_dialog.setWindowTitle(
                self.tr("Map Channels (applies to all {} participant(s))").format(len(in_cohort))
            )
            map_dialog.resize(750, 550)
            v = QVBoxLayout(map_dialog)
            panel = ChannelMappingPanel(
                sample.channels, [os.path.basename(f) for f in sample.mvc_files],
                self.workspace, parent=map_dialog,
            )
            v.addWidget(panel)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            )
            buttons.accepted.connect(map_dialog.accept)
            buttons.rejected.connect(map_dialog.reject)
            v.addWidget(buttons)
            if map_dialog.exec() != QDialog.DialogCode.Accepted:
                return
            enabled, muscle, mvc_file, errors = panel.get_mapping()
            if errors:
                QMessageBox.critical(self, self.tr("error"), "\n".join(errors), QMessageBox.Ok)
                return
            cfg.channel_mapping = ChannelMapping(
                enabled=list(enabled), muscle=muscle, mvc_file=mvc_file
            )

            # The "already mapped" branch below folds this into its confirm
            # question, but map-once never showed it at all -- silently
            # dropping participants (missing/ambiguous files, a different
            # channel set than the cohort, already loaded) with no visible
            # trace. Report it here so it isn't discovered only by counting.
            if skip_lines:
                QMessageBox.information(
                    self, self.tr("Batch Import"),
                    self.tr("Proceeding with {} participant(s). Skipped:\n{}").format(
                        len(in_cohort), "\n".join(skip_lines)
                    ),
                    QMessageBox.Ok,
                )

            if config_path:
                reply = QMessageBox.question(
                    self, self.tr("Save Mapping"),
                    self.tr("Save this channel mapping back to the config file for next time?"),
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply == QMessageBox.Yes:
                    try:
                        save_batch_config(config_path, cfg)
                    except Exception as e:
                        QMessageBox.warning(
                            self, self.tr("warning"),
                            self.tr("Failed to save config: {}").format(str(e)),
                            QMessageBox.Ok,
                        )
        else:
            mapping_summary = "\n".join(
                "  {} → {}".format(c, cfg.channel_mapping.muscle.get(c, "?"))
                for c in cfg.channel_mapping.enabled
            )
            skip_summary = ("\n\nSkipped:\n" + "\n".join(skip_lines)) if skip_lines else ""
            reply = QMessageBox.question(
                self, self.tr("Confirm Batch Import"),
                self.tr("Import {} participant(s) using this mapping?\n\n{}{}").format(
                    len(in_cohort), mapping_summary, skip_summary
                ),
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        mapping = (
            set(cfg.channel_mapping.enabled),
            dict(cfg.channel_mapping.muscle),
            dict(cfg.channel_mapping.mvc_file),
        )

        self._batch_import_progress = QProgressDialog(
            self.tr("Importing participants..."), self.tr("Cancel"), 0, len(in_cohort), self
        )
        self._batch_import_progress.setWindowTitle(self.tr("Myotion-ing"))
        self._batch_import_progress.setWindowModality(Qt.WindowModal)
        self._batch_import_progress.setMinimumDuration(0)
        self._batch_import_progress.setValue(0)

        self._batch_import_worker = BatchImportWorker(self.workspace, in_cohort, mapping, self)
        self._batch_import_worker.progress.connect(self._onBatchImportProgress)
        self._batch_import_worker.error.connect(
            lambda msg: logger.error("Batch import error: {}".format(msg))
        )
        self._batch_import_worker.finished.connect(
            lambda people: self._onBatchImportFinished(people, cfg, config_path)
        )
        self._batch_import_progress.canceled.connect(
            self._batch_import_worker.requestInterruption
        )
        self._batch_import_worker.start()

    def _onInlineStitchFinished(self, pairs, root, cfg, config_path):
        """Continuation of _continueBatchImport's stitch-first prompt: report
        the stitch outcome, then retry the scan with the freshly-stitched
        files present."""
        self._stitch_progress.setValue(self._stitch_progress.maximum())
        self._stitch_progress.close()

        stitched = [p for p in pairs if p.status == "stitched"]
        failed = [p for p in pairs if p.status in ("untrusted", "error")]

        if failed:
            fail_lines = "\n".join(
                "{}/{}: {}".format(p.participant, p.stem, p.message) for p in failed
            )
            QMessageBox.warning(
                self, self.tr("Batch Stitch"),
                self.tr(
                    '{} pair(s) need manual alignment ("Align && Stitch..." on the '
                    "Kinematics Inspection page):\n{}"
                ).format(len(failed), fail_lines),
                QMessageBox.Ok,
            )

        if not stitched:
            QMessageBox.warning(
                self, self.tr("Batch Import"),
                self.tr("No pairs could be stitched automatically; cannot continue with this config."),
                QMessageBox.Ok,
            )
            return

        # If the config was targeting one exact stem (the common case from
        # "Detect from folder" proposing an exact basename), point it at the
        # matching stitched output so the retry below succeeds without
        # asking the user to re-pick the task file. A wildcard glob (e.g.
        # "*.c3d") is left untouched -- rewriting it safely isn't possible,
        # the retry will simply reveal whether it still needs adjustment.
        new_emg_file = _stitched_glob_for(cfg.layout.emg_file)
        updated = bool(new_emg_file) and any(
            os.path.basename(p.out_path or "") == new_emg_file for p in stitched
        )
        if updated:
            cfg.layout.emg_file = new_emg_file
            QMessageBox.information(
                self, self.tr("Batch Import"),
                self.tr(
                    "Stitched {} pair(s). Task file updated to '{}' -- retrying import."
                ).format(len(stitched), new_emg_file),
                QMessageBox.Ok,
            )
        else:
            QMessageBox.information(
                self, self.tr("Batch Import"),
                self.tr("Stitched {} pair(s). Retrying import with the current config.").format(
                    len(stitched)
                ),
                QMessageBox.Ok,
            )

        self._continueBatchImport(root, cfg, config_path)

    def _onBatchImportProgress(self, count, name):
        self._batch_import_progress.setValue(count)
        self._batch_import_progress.setLabelText(self.tr("Loading: {}").format(name))

    def _uniqueSavedConfigName(self, base):
        """Avoid clobbering an existing saved EMG config with the same name
        (e.g. re-running Batch Import for the same task_type twice)."""
        existing = self.workspace.getEMGConfigures()
        if base not in existing:
            return base
        i = 2
        while "{} ({})".format(base, i) in existing:
            i += 1
        return "{} ({})".format(base, i)

    def _onBatchImportFinished(self, people, cfg, config_path=None):
        self._batch_import_progress.setValue(self._batch_import_progress.maximum())
        self._batch_import_progress.close()
        self.updateWorkSpaceParticipantBox()
        self.saveProjectButtonClick(show=False)

        if not people:
            self.updateEMGParticipantBox()
            QMessageBox.warning(
                self, self.tr("Batch Import"),
                self.tr("No participants were imported."),
                QMessageBox.Ok,
            )
            return

        # Save the batch's processing config as a named, reusable EMG config
        # (same store BATCH PROCESS already reads from) so re-running/tweaking
        # processing later doesn't require repeating the whole import -- just
        # select these participants and use BATCH PROCESS with this config.
        emg_configure = processing_to_emg_configure(cfg.processing)
        cfg_name = self._uniqueSavedConfigName(cfg.layout.task_type or "Batch Import")
        self.workspace.saveEMGConfigureObject(cfg_name, emg_configure)
        self.updateEMGSavedConfigureList()

        # Remember this as "the config loaded along with the batch" so
        # Edit Config / Edit Mapping act on it instead of starting blank.
        self._current_batch_config = cfg
        self._current_batch_config_path = config_path
        self._current_batch_config_name = cfg_name
        # Select it so BATCH PROCESS is enabled immediately, not just saved.
        matches = widgets.listWidget_2.findItems(cfg_name, Qt.MatchExactly)
        if matches:
            widgets.listWidget_2.setCurrentItem(matches[0])

        # Pre-select the just-imported participants so BATCH PROCESS is one
        # click away regardless of the answer below.
        self.selectedParticipants.update(p.name for p in people)
        self.updateEMGParticipantBox()
        self.updateBatchProcessButtonState()

        reply = QMessageBox.question(
            self, self.tr("Batch Import"),
            self.tr(
                "Imported {} participant(s) and saved their processing settings as "
                "'{}'. Start EMG processing now?\n\n"
                "(Or later: select these participants and use BATCH PROCESS with "
                "the '{}' config.)"
            ).format(len(people), cfg_name, cfg_name),
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.startBatchEMGProcess(people, emg_configure)

    def editBatchConfigButtonClicked(self):
        # Edit the config loaded along with the current batch, if any --
        # only fall back to a blank config when nothing's been imported yet.
        dlg = BatchConfigDialog(self._current_batch_config or BatchConfig(), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        edited = dlg.get_config()

        path = self._current_batch_config_path
        if path:
            reply = QMessageBox.question(
                self, self.tr("Save Config"),
                self.tr("Overwrite the batch config loaded with this batch?\n\n{}").format(path),
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                path = None  # fall through to "Save As" below

        if not path:
            path, _ = QFileDialog.getSaveFileName(
                self, self.tr("Save Batch Config"), self.home or "", "TOML Files (*.toml)"
            )
            if not path:
                return
            if not path.lower().endswith(".toml"):
                path += ".toml"

        try:
            save_batch_config(path, edited)
        except Exception as e:
            QMessageBox.critical(
                self, self.tr("error"),
                self.tr("Failed to save config: {}").format(str(e)),
                QMessageBox.Ok,
            )
            return

        self._current_batch_config = edited
        self._current_batch_config_path = path
        # Keep the saved processing config (used by BATCH PROCESS) in sync,
        # so re-processing afterward picks up the edit without re-importing.
        if self._current_batch_config_name and self.workspace is not None:
            self.workspace.saveEMGConfigureObject(
                self._current_batch_config_name, processing_to_emg_configure(edited.processing)
            )
            self.updateEMGSavedConfigureList()

        QMessageBox.information(
            self, self.tr("Saved"), self.tr("Config saved to:\n{}").format(path), QMessageBox.Ok
        )

    def editMappingButtonClicked(self):
        if len(self.selectedParticipants) == 0:
            QMessageBox.critical(
                None, self.tr("error"), self.tr("Please select participants first!"),
                QMessageBox.Ok,
            )
            return

        listofpeople = [self.workspace.findParticipant(n) for n in self.selectedParticipants]
        listofpeople = [p for p in listofpeople if p is not None]
        if not listofpeople:
            return

        chan_map = {}
        for p in listofpeople:
            chan_map.setdefault(frozenset(self.workspace[p].emg.Channels), []).append(p.name)
        if len(chan_map) > 1:
            lines = [
                "[{}]: {}".format(", ".join(sorted(chans)), ", ".join(names))
                for chans, names in chan_map.items()
            ]
            reply = QMessageBox.warning(
                None, self.tr("warning"),
                self.tr(
                    "Selected participants have different channel sets:\n{}\n\nContinue anyway?"
                ).format("\n".join(lines)),
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        def _resolve_raw_channel(e, current_name):
            """Best-effort original (pre-rename) channel name for
            current_name -- needed to (re)assign an MVC file after a channel
            has already been renamed once. Prefers the batch config
            remembered from this batch's import (authoritative for anything
            loaded via Batch Import); falls back to this emg object's own
            rename history (chanMap) for participants added another way."""
            cfg = self._current_batch_config
            if cfg is not None:
                for raw, muscle_name in cfg.channel_mapping.muscle.items():
                    if muscle_name == current_name:
                        return raw
            for old, new in e.chanMap.items():
                if new == current_name:
                    return old
            return current_name

        first_emg = self.workspace[listofpeople[0]].emg
        channels = list(first_emg.Channels)
        # Pre-fill from the current live state -- current channel names are
        # already the "muscle" labels if this participant was mapped before
        # (batch import or a prior Edit Mapping pass); channels never renamed
        # show their raw name as an editable placeholder.
        mvc_initial = {}
        all_mvc_basenames = set()
        for p in listofpeople:
            for path in self.workspace[p].emg.mvcFilesMap.values():
                all_mvc_basenames.add(os.path.basename(path))
        if self._current_batch_config is not None:
            all_mvc_basenames.update(self._current_batch_config.channel_mapping.mvc_file.values())
        for c in channels:
            raw = _resolve_raw_channel(first_emg, c)
            path = first_emg.mvcFilesMap.get(c) or first_emg.mvcFilesMap.get(raw)
            if path:
                mvc_initial[c] = os.path.basename(path)
            elif self._current_batch_config is not None:
                basename = self._current_batch_config.channel_mapping.mvc_file.get(raw)
                if basename:
                    mvc_initial[c] = basename

        initial = (
            set(first_emg.enabledChannels),
            {c: c for c in first_emg.Channels},
            mvc_initial,
        )

        map_dialog = QDialog(self)
        map_dialog.setWindowTitle(
            self.tr("Edit Mapping ({} participant(s))").format(len(listofpeople))
        )
        map_dialog.resize(750, 550)
        v = QVBoxLayout(map_dialog)
        panel = ChannelMappingPanel(
            channels, sorted(all_mvc_basenames), self.workspace, initial=initial, parent=map_dialog
        )
        v.addWidget(panel)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(map_dialog.accept)
        buttons.rejected.connect(map_dialog.reject)
        v.addWidget(buttons)
        if map_dialog.exec() != QDialog.DialogCode.Accepted:
            return

        enabled, muscle, mvc_file, errors = panel.get_mapping()
        if errors:
            QMessageBox.critical(self, self.tr("error"), "\n".join(errors), QMessageBox.Ok)
            return

        mvc_warnings = []
        for p in listofpeople:
            e = self.workspace[p].emg
            for c in list(e.Channels):
                if c in enabled:
                    e.enableChannel(c)
                elif c in e.enabledChannels:
                    e.disableChannel(c)
            for old, new in muscle.items():
                if old != new and old in e.Channels:
                    e.renameChannel(old, new)

            # Apply MVC (re)assignment -- only possible using an MVC file
            # already loaded for THIS participant (no disk re-scan here).
            basename_to_path = {os.path.basename(f): f for f in e.mvcFilesMap.values()}
            for c, basename in mvc_file.items():
                target_name = muscle.get(c, c)
                raw = _resolve_raw_channel(e, c)
                mvc_path = basename_to_path.get(basename)
                if mvc_path is None:
                    mvc_warnings.append(
                        "{}: MVC file '{}' isn't loaded for this participant".format(p.name, basename)
                    )
                    continue
                try:
                    reassign_mvc_file(e, target_name, raw, mvc_path)
                except ValueError as ex:
                    mvc_warnings.append("{}: {}".format(p.name, str(ex)))

        # Keep this in sync with "the same config file loaded along with the
        # batch" so a later Edit Config / re-import sees the same mapping.
        if self._current_batch_config is not None:
            new_enabled, new_muscle, new_mvc = [], {}, {}
            for c in channels:
                raw = _resolve_raw_channel(first_emg, c)
                if c in enabled:
                    new_enabled.append(raw)
                if c in muscle:
                    new_muscle[raw] = muscle[c]
                if c in mvc_file:
                    new_mvc[raw] = mvc_file[c]
            self._current_batch_config.channel_mapping = ChannelMapping(
                enabled=new_enabled, muscle=new_muscle, mvc_file=new_mvc
            )
            if self._current_batch_config_path:
                reply = QMessageBox.question(
                    self, self.tr("Save Mapping"),
                    self.tr(
                        "Overwrite the batch config's channel mapping with these changes?\n\n{}"
                    ).format(self._current_batch_config_path),
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply == QMessageBox.Yes:
                    try:
                        save_batch_config(self._current_batch_config_path, self._current_batch_config)
                    except Exception as e:
                        QMessageBox.warning(
                            self, self.tr("warning"),
                            self.tr("Failed to save config: {}").format(str(e)),
                            QMessageBox.Ok,
                        )

        self.updateEMGParticipantBox()
        self.saveProjectButtonClick(show=False)
        if mvc_warnings:
            QMessageBox.warning(
                self, self.tr("Edit Mapping"),
                self.tr("Some MVC assignments could not be applied:\n{}").format(
                    "\n".join(mvc_warnings)
                ),
                QMessageBox.Ok,
            )
        QMessageBox.information(
            self, self.tr("Edit Mapping"),
            self.tr(
                "Mapping updated for {} participant(s). Re-run BATCH PROCESS "
                "to apply it to processing/reports."
            ).format(len(listofpeople)),
            QMessageBox.Ok,
        )

    def FFTPlotClearAllClicked(self):
        widgets.scrollArea_3.deleteAllPages()
        self.updateFreqAnalysisFFTPanel()

    def _exportParticipantEvents(self, p):
        """Export events and detection results to <workspace>/<name>/<name>_Events.csv.

        Layout:
          Section 1 — user / C3D events (Time, Label), sorted by time.
          Section 2 — onset/offset detection results grouped by muscle,
                       showing paired (Onset, Offset) on each row.
        """
        import csv as _csv
        import re as _re
        from datetime import datetime as _dt

        profile = self.workspace[p]
        all_events = sorted(
            list(getattr(profile.kinematic, "events", [])) +
            list(getattr(profile, "extra_events", [])),
            key=lambda e: e.time_s,
        )

        # Split into named events and auto-detection events
        named_events = [e for e in all_events if e.context != "Detection"]
        detect_events = [e for e in all_events if e.context == "Detection"]

        if not named_events and not detect_events:
            QMessageBox.information(
                self, self.tr("Export Events"),
                self.tr("No events to export for {}.".format(p.name)),
                QMessageBox.Ok,
            )
            return

        # Parse detection events into {muscle: {activation_num: {onset|offset: time_s}}}
        _DET_RE = _re.compile(r'^(Onset|Offset)_(.+?)(?:\s+#(\d+))?$')
        detection_map = {}
        for ev in detect_events:
            m = _DET_RE.match(ev.label)
            if not m:
                continue
            kind   = m.group(1).lower()   # "onset" or "offset"
            muscle = m.group(2)
            num    = int(m.group(3)) if m.group(3) else 1
            detection_map.setdefault(muscle, {}).setdefault(num, {})[kind] = round(ev.time_s, 4)

        participant_dir = os.path.join(self.home, p.name)
        os.makedirs(participant_dir, exist_ok=True)
        out_path = os.path.join(participant_dir, "{}_Events.csv".format(p.name))

        try:
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                f.write("# Participant: {}\n".format(p.name))
                f.write("# Exported: {}\n".format(_dt.now().strftime("%Y-%m-%d %H:%M:%S")))

                # --- Section 1: named events ---
                if named_events:
                    f.write("#\n# Events\n")
                    w = _csv.DictWriter(f, fieldnames=["Time (s)", "Label"])
                    w.writeheader()
                    for ev in named_events:
                        w.writerow({"Time (s)": round(ev.time_s, 4), "Label": ev.label})

                # --- Section 2: detection results grouped by muscle ---
                if detection_map:
                    f.write("#\n# Onset/Offset Detection\n")
                    w = _csv.DictWriter(
                        f, fieldnames=["Muscle", "Activation", "Onset (s)", "Offset (s)"]
                    )
                    w.writeheader()
                    for muscle in sorted(detection_map):
                        for num in sorted(detection_map[muscle]):
                            pair = detection_map[muscle][num]
                            w.writerow({
                                "Muscle":      muscle,
                                "Activation":  num,
                                "Onset (s)":   pair.get("onset", ""),
                                "Offset (s)":  pair.get("offset", ""),
                            })

            QMessageBox.information(
                self, self.tr("Export Events"),
                self.tr("Events saved to:\n{}".format(out_path)),
                QMessageBox.Ok,
            )
        except Exception as e:
            QMessageBox.critical(
                self, self.tr("error"),
                self.tr("Failed to export events: {}".format(str(e))),
                QMessageBox.Ok,
            )

    def exportFreqAnalysisCSV(self):
        p, _ = self.freqAnalysis
        if p is None:
            QMessageBox.warning(
                self, self.tr("warning"),
                self.tr("Select a participant channel in the tree first."),
                QMessageBox.Ok,
            )
            return

        crop = self.workspace[p].crop_interval
        fs   = self.workspace[p].emg.getfs()
        chans = self.workspace[p].emg.getChannels()
        seg_start = crop[0] if crop is not None else 0.0

        # Read the window and split count that the user set in the freq UI —
        # these must match what the FFT plots show.
        try:
            left_abs  = float(widgets.lineEdit_5.text())
            right_abs = float(widgets.lineEdit_4.text())
        except Exception:
            left_abs, right_abs = seg_start, seg_start
        try:
            num_intervals = max(1, int(widgets.lineEdit_6.text()))
        except Exception:
            num_intervals = 1

        # Convert absolute trial times → segment-relative for fft()
        # (the freq-safe TST always starts at t = 0)
        seg_dur_ref = None  # filled from first channel
        interval_abs = (right_abs - left_abs) / num_intervals

        fieldnames = ["Channel", "Start (s)", "End (s)", "MNF (Hz)", "MDF (Hz)"]
        rows = []
        for chan in chans:
            arr = self.workspace[p].emg.getFreqSafeSegment(chan, crop)
            if len(arr) == 0:
                for i in range(num_intervals):
                    rows.append({
                        "Channel": chan,
                        "Start (s)": round(left_abs + i * interval_abs, 4),
                        "End (s)":   round(left_abs + (i + 1) * interval_abs, 4),
                        "MNF (Hz)": "", "MDF (Hz)": "",
                    })
                continue

            seg_dur = len(arr) / fs
            if seg_dur_ref is None:
                seg_dur_ref = seg_dur

            tst = timeSeriesTable(fs, [chan], [arr])

            for i in range(num_intervals):
                t_abs_start = left_abs  + i * interval_abs
                t_abs_end   = t_abs_start + interval_abs
                # relative times for fft()
                t_rel_start = max(0.0, min(t_abs_start - seg_start, seg_dur))
                t_rel_end   = max(0.0, min(t_abs_end   - seg_start, seg_dur))

                freq_lin, v_lin = tst.fft(chan, t_rel_start, t_rel_end)
                total = float(np.sum(v_lin))
                if total > 0:
                    mnf = float(np.dot(freq_lin, v_lin) / total)
                    cum = np.cumsum(v_lin)
                    idx = min(len(freq_lin) - 1,
                              int(np.searchsorted(cum, cum[-1] / 2, side="right")))
                    mdf = float(freq_lin[idx])
                else:
                    mnf = mdf = 0.0

                rows.append({
                    "Channel":   chan,
                    "Start (s)": round(t_abs_start, 4),
                    "End (s)":   round(t_abs_end,   4),
                    "MNF (Hz)":  round(mnf, 4),
                    "MDF (Hz)":  round(mdf, 4),
                })

        header = (
            "# Participant: {}\n"
            "# Sample frequency: {} Hz\n"
            "# Analysis window: {:.3f} s - {:.3f} s ({} interval{})\n"
        ).format(p.name, fs, left_abs, right_abs,
                 num_intervals, "s" if num_intervals > 1 else "")

        participant_dir = os.path.join(self.home, p.name)
        os.makedirs(participant_dir, exist_ok=True)
        save_path = os.path.join(participant_dir, p.name + "_freq_analysis.csv")

        import csv as _csv
        with open(save_path, "w", encoding="utf-8", newline="") as f:
            f.write(header)
            writer = _csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        QMessageBox.information(
            self, self.tr("Saved"),
            self.tr(
                "Frequency analysis results have been saved to the workspace folder:\n{}"
            ).format(participant_dir),
            QMessageBox.Ok,
        )

    def FFTPlotNextPageClicked(self):
        widgets.scrollArea_3.nextPage()
        self.updateFreqAnalysisFFTPanel()

    def FFTPlotPrevPageClicked(self):
        widgets.scrollArea_3.prevPage()
        self.updateFreqAnalysisFFTPanel()

    def FFTPlotPerPageSelected(self, index):
        self.updateFreqAnalysisFFTPanel()

    def FFTPlotPageIndexSelected(self, index):
        widgets.scrollArea_3.setCurrentPage(index)
        widgets.scrollArea_3.show()

    # WIDGET
    # //////////////////////////////////////////////////////////////
    def EMGCreateParticipantCheckBox(self, name):
        checkbox = QCheckBox(widgets.tableWidget_2)
        checkbox.setObjectName(name)

        # select state according to selectedpartipant
        if name in self.selectedParticipants:
            checkbox.setChecked(True)
        else:
            checkbox.setChecked(False)
        checkbox.stateChanged.connect(self.participantCheckBoxChanged)
        checkbox.stateChanged.connect(self.handleParticipantCheckState)
        return checkbox

    def handleParticipantCheckState(self, state):
        """Handle state changes of individual participant checkboxes."""
        # Temporarily block signals to avoid recursive updates.
        with QSignalBlocker(widgets.checkBox_2):
            # Check whether all are selected.
            all_checked = True
            for i in range(widgets.tableWidget_2.rowCount()):
                w = widgets.tableWidget_2.cellWidget(i, 0).findChild(QCheckBox)
                if not w.isChecked():
                    all_checked = False
                    break
                    
            widgets.checkBox_2.setChecked(all_checked)

    def EMGCreateHBox(self, w, parent=None):
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.addWidget(w)
        layout.setAlignment(Qt.AlignCenter)
        layout.setContentsMargins(0, 0, 0, 0)
        # w.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        return container

    def _get_freq_safe_tst(self, p, channel):
        """Single-channel TST of the cropped frequency-safe EMG segment.

        Uses DC removal + band-pass only (no rectification, no LP envelope,
        no normalization), then crops to profile.crop_interval.
        """
        crop = self.workspace[p].crop_interval
        arr = self.workspace[p].emg.getFreqSafeSegment(channel, crop)
        fs = self.workspace[p].emg.getfs()
        return timeSeriesTable(fs, [channel], [arr])

    # draw FFT
    # l, r are segment-relative seconds (0-based); seg_start offsets them for display/filenames
    def FreqAnalysisCreateQPlotView(self, p, channel, l, r, title, seg_start=0.0):
        pv = QPlotView()
        chan_slug = re.sub(r"[^a-zA-Z0-9]+", "_", channel).strip("_")
        participant_dir = os.path.join(self.home, p.name)
        # Ensure the folder exists so the save dialog opens directly in it
        os.makedirs(participant_dir, exist_ok=True)
        # Filename uses absolute trial times for clarity
        l_abs, r_abs = l + seg_start, r + seg_start
        pv.set_save_path(
            participant_dir,
            "{}_freq_{:.3f}s_{:.3f}s".format(chan_slug, l_abs, r_abs),
        )
        # Use frequency-safe signal: DC + band-pass only, no envelope, no normalization
        tst = self._get_freq_safe_tst(p, channel)
        freq, v = tst.fft_db(channel, l, r)

        # Compute mean and median frequency from the LINEAR amplitude spectrum.
        # fft_db returns 20*log10(amplitude) — using those dB values directly in
        # a weighted-sum formula produces nonsense (sum of negatives / sum of negatives).
        freq_lin, v_lin = tst.fft(channel, l, r)
        if len(freq_lin) > 0 and np.sum(v_lin) > 0:
            mean_freq = float(np.dot(freq_lin, v_lin) / np.sum(v_lin))
            cum = np.cumsum(v_lin)
            med_idx = min(len(freq_lin) - 1,
                          np.searchsorted(cum, cum[-1] / 2, side="right"))
            med_freq = float(freq_lin[med_idx])
        else:
            mean_freq = med_freq = 0.0

        title = title + ",  MeanF {:.1f} Hz  |  MedF {:.1f} Hz".format(
            mean_freq, med_freq
        )

        # Distinct amber colour — separates freq plots from time-domain blue/red
        pv.line(freq, v, channel, title=title, xlabel="Frequency (Hz)", ylabel="dB",
                color=["#ffb86c"])
        return pv

    # Signals
    # //////////////////////////////////////////////////////////////
    def emitPariticipantUpdate(self):
        self.sigUpdateParticipants.emit()

    def emitAsyncLoadError(self, str):
        self.sigAsyncLoadError.emit(str)

    # UPDATE UI EVENTS/Slots
    # //////////////////////////////////////////////////////////////
    def _updateEMGDataControlsEnabled(self):
        """Enable Signal Process / Select All only once a workspace has participants.

        Also guards EMGParticipantSelectAllClicked, which calls
        self.workspace.getFilteredParticipants() unconditionally -- without
        this, toggling Select All with no workspace loaded would crash.
        """
        has_data = self.workspace is not None and len(self.workspace.getParticipants()) > 0
        widgets.pushButton_11.setEnabled(has_data)
        widgets.checkBox_2.setEnabled(has_data)

    @Slot()
    def updateEMGParticipantBox(self):
        if self.workspace is None:
            widgets.tableWidget_2.clearContents()
            widgets.tableWidget_2.setRowCount(0)
            self._updateEMGDataControlsEnabled()
            return
        participants = self.workspace.getFilteredParticipants(self.participant_filter)
        n = len(participants)
        widgets.tableWidget_2.clearContents()
        widgets.tableWidget_2.setRowCount(n)
        for i in range(0, n):
            p = participants[i]
            name = p.name
            # checkbox
            chb = self.EMGCreateParticipantCheckBox(p.name)
            widgets.tableWidget_2.setCellWidget(i, 0, self.EMGCreateHBox(chb))
            # name
            q = QTableWidgetItem(name)
            q.setTextAlignment(Qt.AlignCenter)
            widgets.tableWidget_2.setItem(i, 1, q)
            # status
            h = widgets.tableWidget_2.rowHeight(i)
            col2w = widgets.tableWidget_2.columnWidth(2)
            col3w = widgets.tableWidget_2.columnWidth(3)
            ready = statusLED(
                col2w * 0.8,
                h * 0.8,
                STATUS.Loading if self.workspace[p].isLoading() else STATUS.Passed,
            )
            report = statusLED(
                col3w * 0.8,
                h * 0.8,
                STATUS.Passed if self.workspace[p].isReportReady() else STATUS.Failed,
            )
            widgets.tableWidget_2.setCellWidget(i, 2, self.EMGCreateHBox(ready))
            widgets.tableWidget_2.setCellWidget(i, 3, self.EMGCreateHBox(report))

        # Sync listWidget_3 state.
        self.syncListWidgetWithSelectedParticipants()
        self._updateEMGDataControlsEnabled()

    def updateWorkSpaceParticipantBox(self):
        # listwidget_3
        if self.workspace is None:
            widgets.listWidget_3.clear()
            return
        participants = self.workspace.getParticipants()
        n = len(participants)
        widgets.listWidget_3.clear()
        for i in range(0, n):
            p = participants[i]
            name = p.name
            # name
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            widgets.listWidget_3.addItem(item)
            widgets.listWidget_3.item(i).setForeground(Qt.black)

        # Connect signals.
        widgets.listWidget_3.itemChanged.connect(self.listWidgetItemChanged)

    # update waveform regarding to config step and user input metrics
    def updateEMGSignalProcessPanel(self, prev=True, post=True):
        p, step, chan = self.singleEMG

        if p is None:
            widgets.plot_input.hide()
            widgets.plot_output.hide()
            return

        x = self.workspace[p].emg.getLinspace()
        # push data to plot

        ci = self.workspace[p].crop_interval
        xrange = list(ci) if ci else None

        # build a filename-safe step slug for camera-icon saves
        cfg = self.workspace[p].emg.getProcessConfig()
        if cfg is not None:
            step_slug = re.sub(r"[^a-z0-9]+", "_", cfg.getStepStringList()[step].lower()).strip("_")
        else:
            step_slug = "step{}".format(step + 1)
        # sanitize channel name (may contain slashes, spaces, etc.)
        chan_slug = re.sub(r"[^a-zA-Z0-9]+", "_", chan).strip("_")
        participant_dir = os.path.join(self.home, p.name)

        if prev:
            widgets.plot_input.set_save_path(participant_dir, "{}_{}__pre".format(chan_slug, step_slug))
            widgets.plot_input.line(x, self.inputBuffer, chan, color=["#7b91d6"], xrange=xrange)
            widgets.plot_input.show()
        if post:
            widgets.plot_output.set_save_path(participant_dir, "{}_{}".format(chan_slug, step_slug))
            widgets.plot_output.line(x, self.outputBuffer, chan, color=["#e05c5c"], xrange=xrange)
            widgets.plot_output.show()

    def showPipelineOverview(self):
        p, step, chan = self.singleEMG
        if p is None:
            QMessageBox.information(
                self,
                self.tr("Pipeline View"),
                self.tr("Start single EMG processing first."),
            )
            return
        emg_obj = self.workspace[p].emg
        cfg = emg_obj.getProcessConfig()
        if cfg is None:
            return

        x = emg_obj.getLinspace()
        step_names = cfg.getStepStringList()
        crop = self.workspace[p].crop_interval

        dlg = QDialog(self)
        dlg.setWindowTitle(self.tr("Pipeline Overview — {}").format(chan))
        dlg.resize(1200, 900)

        scroll = QScrollArea(dlg)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        content = QWidget()
        vbox = QVBoxLayout(content)
        vbox.setContentsMargins(8, 8, 8, 8)
        vbox.setSpacing(4)

        for i in range(cfg.size()):
            lbl = QLabel("Step {}: {}".format(i + 1, step_names[i]))
            lbl.setStyleSheet("color:#c8c8c8; font-weight:bold; font-size:10pt;")
            vbox.addWidget(lbl)
            pv = QPlotView(content)
            pv.setMinimumHeight(320)
            try:
                y = emg_obj.tryConfigStepTo(chan, i, crop)
                pv.line(x, y, chan, title=step_names[i])
                pv.show()
            except Exception as e:
                logger.error("Pipeline overview step {}: {}".format(i, e))
            vbox.addWidget(pv)

        scroll.setWidget(content)

        outer = QVBoxLayout(dlg)
        outer.addWidget(scroll)
        close_btn = QPushButton(self.tr("Close"))
        close_btn.clicked.connect(dlg.accept)
        outer.addWidget(close_btn)

        dlg.exec()

    def updateEMGConfigureList(self):
        widgets.listWidget.clear()

        if self.workspace is None:
            return
        p, step, chan = self.singleEMG
        if p is None:
            return
        cfg = self.workspace[p].emg.getProcessConfig()
        if cfg is None:
            return
        cfgstrings = cfg.getStepStringList()
        # update configuration list
        n = len(cfgstrings)
        widgets.listWidget.clear()
        widgets.listWidget.setSortingEnabled(False)
        for i in range(0, n):
            widgets.listWidget.addItem(cfgstrings[i])
            widgets.listWidget.item(i).setForeground(Qt.black)

    def updateEMGToolBox(self, type):
        # update toolbox with current config
        p, step, chan = self.singleEMG
        if p is None:
            return
        cfg = self.workspace[p].emg.getProcessConfig()
        if cfg is None:
            return
        if type == emgConfigEnum.DC_OFFSET:
            widgets.checkBox_4.setCheckState(Qt.Checked if cfg[step].enable else Qt.Unchecked)
        elif type == emgConfigEnum.FULL_W_RECT:
            widgets.checkBox_11.setCheckState(Qt.Checked if cfg[step].enable else Qt.Unchecked)
        elif type == emgConfigEnum.FILTER:
            widgets.checkBox_13.setCheckState(Qt.Checked if cfg[step].enable else Qt.Unchecked)
            if cfg[step].type == emgFilterEnum.BAND_PASS:
                widgets.comboBox_7.setCurrentIndex(0)
                widgets.lineEdit_10.setText(str(cfg[step].cutoff_h))
                widgets.lineEdit_11.setText(str(cfg[step].cutoff_l))
                widgets.lineEdit_12.setText("")
            else:
                widgets.comboBox_7.setCurrentIndex(1)
                widgets.lineEdit_12.setText(str(cfg[step].cutoff_l))
                widgets.lineEdit_10.setText("")
                widgets.lineEdit_11.setText("")
        elif type == emgConfigEnum.NORMALIZATION:
            widgets.checkBox_12.setCheckState(Qt.Checked if cfg[step].enable else Qt.Unchecked)
        elif type == emgConfigEnum.SUMMARY:
            widgets.label_23.setText("{:.4f}".format(cfg[step].max))
            widgets.label_25.setText("{:.4f}".format(cfg[step].min))
            widgets.label_27.setText("{:.6f}".format(cfg[step].iemg))
            self._pipeline_panel.updateSummary(step, cfg[step])

    def updateEMGChannelSelectorContent(self):
        p, step, chan = self.singleEMG
        widgets.comboBox_2.clear()
        if p is None:
            return
        chan = self.workspace[p].emg.getChannels()
        widgets.comboBox_2.addItems(chan)

    def updateEMGChannelSelectorText(self, chan):
        widgets.comboBox_2.setCurrentText(chan)

    def updateWorkProjectTreeWidget(self):
        widgets.treeView.setForegroundRole(QPalette.Base)
        if self.home is not None:
            # load workspace file exploer
            self.filesystemTree.setRootPath(self.home)
            widgets.treeView.setModel(self.filesystemTree)
            widgets.treeView.setRootIndex(self.filesystemTree.index(self.home))
        else:
            widgets.treeView.setModel(None)

    def handleTreeViewDoubleClick(self, index):
        """Handle double-click events in the file tree."""
        # Get file path.
        file_path = self.filesystemTree.filePath(index)
        
        # Check file type.
        if not os.path.isfile(file_path):
            return
        
        # Handle file-type-specific actions.
        print(f"Double clicked: {file_path}")
        
        # Open .myo project files.
        if file_path.endswith(".myo"):
            # First check whether current workspace should be saved.
            if self.ifOldProjectOpened():
                return
            
            # Load project.
            self.loadWorkSpace(os.path.dirname(file_path), os.path.basename(file_path))

    def showTreeViewContextMenu(self, pos):
        """Right-click menu for the workspace file tree — 'Reveal in Explorer/Finder'."""
        index = widgets.treeView.indexAt(pos)
        if not index.isValid():
            return
        file_path = self.filesystemTree.filePath(index)
        if not file_path:
            return

        if sys.platform == "win32":
            label = self.tr("Reveal in Explorer")
        elif sys.platform == "darwin":
            label = self.tr("Reveal in Finder")
        else:
            label = self.tr("Open Containing Folder")

        menu = QMenu(self)
        menu.addAction(label, lambda: self.revealInFileExplorer(file_path))
        menu.exec(widgets.treeView.viewport().mapToGlobal(pos))

    def revealInFileExplorer(self, file_path):
        """Open the OS file browser with file_path selected, like other apps'
        'Reveal in Folder' / 'Show in Explorer' context-menu action."""
        file_path = os.path.normpath(file_path)
        if not os.path.exists(file_path):
            return
        if sys.platform == "win32":
            subprocess.run(["explorer", "/select,", file_path])
        elif sys.platform == "darwin":
            subprocess.run(["open", "-R", file_path])
        else:
            # No universal "select this file" support across Linux file
            # managers — open the containing folder instead.
            folder = file_path if os.path.isdir(file_path) else os.path.dirname(file_path)
            subprocess.run(["xdg-open", folder])

    def updateEMGSavedConfigureList(self):
        if self.workspace is None:
            widgets.listWidget_2.clear()
            return
        # update configuration list
        widgets.listWidget_2.clear()
        widgets.listWidget_2.setSortingEnabled(False)
        for key in self.workspace.getEMGConfigures().keys():
            widgets.listWidget_2.addItem(key)
        # Do not set per-item foreground — let the widget stylesheet handle colour
        # so dark/light themes work correctly without a hardcoded Qt.black override.

        widgets.listWidget_2.itemSelectionChanged.connect(self.updateBatchProcessButtonState)
        widgets.listWidget_2.itemDoubleClicked.connect(self.onListWidget2ItemDoubleClicked)

    def _addSavedConfigButtonClicked(self):
        """(Re)add a named saved EMG config to the list -- the counterpart
        to removeEMGConfig's "-" button. Prefers restoring the current
        batch's config (the common "I deleted it by accident" case, since
        Edit Config alone only edits self._current_batch_config in memory --
        it can't recreate a deleted list entry unless you also save through
        it); falls back to the existing single-EMG "Save Configuration"
        action if no batch is currently tracked this session.
        """
        if self.workspace is None:
            return

        if self._current_batch_config is not None and self._current_batch_config_name:
            self.workspace.saveEMGConfigureObject(
                self._current_batch_config_name,
                processing_to_emg_configure(self._current_batch_config.processing),
            )
            self.updateEMGSavedConfigureList()
            matches = widgets.listWidget_2.findItems(
                self._current_batch_config_name, Qt.MatchExactly
            )
            if matches:
                widgets.listWidget_2.setCurrentItem(matches[0])
            self.saveWorkSpace()
            return

        if self.singleEMG[0] is not None:
            self.EMGSaveConfigurationButtonClicked()
            return

        QMessageBox.information(
            self, self.tr("Add Configuration"),
            self.tr(
                "Nothing to add yet -- run Batch Import, or open a single "
                "participant's EMG signal processing and use Save "
                "Configuration there, then use + here."
            ),
            QMessageBox.Ok,
        )

    def removeEMGConfig(self):
        """Delete the selected saved config from the list and the workspace."""
        selected = widgets.listWidget_2.selectedItems()
        if not selected:
            return
        cfgname = selected[0].text()
        configs = self.workspace.getEMGConfigures()
        if cfgname in configs:
            del configs[cfgname]
        widgets.listWidget_2.takeItem(widgets.listWidget_2.row(selected[0]))
        self.saveWorkSpace()

    def onListWidget2ItemDoubleClicked(self, item):
        cfgname = item.text()
        configureList = self.workspace.getEMGConfigures()
        config = configureList[cfgname]

        # Resolve participant first so we can pass the correct fs to the dialog
        p = self.workspace.getParticipantWithName(
            self.extract_participant_name_from_configname(cfgname)
        )
        try:
            fs = self.workspace[p].emg.getfs() if p is not None else 2000
        except Exception:
            fs = 2000

        isSave, cfg = EMGConfigEditDialog(config, fs, parent=self).run()

        if not isSave:
            return

        if p is not None:
            self.workspace[p].emg.setProcessConfig(cfg)
            self.workspace.saveEMGConfigure(p, cfgname)
            self.saveWorkSpace()
        else:
            logger.error("participant name not found")
            return

    def extract_participant_name_from_configname(self, cfgname):
        """Extract participant name from a configuration filename.
        
        Args:
            cfgname: Configuration filename in the format "p.name's EMGConfig".
            
        Returns:
            Extracted participant name.
        """
        suffix = "'s EMGConfig"
        if cfgname.endswith(suffix):
            p_name = cfgname[: -len(suffix)]
            return p_name
        else:
            # If format does not match, return None.
            return None

    def updateFilterText(self):
        filter_str = widgets.lineEdit_3.text()
        # check valid regex string
        try:
            re.compile(filter_str)
        except re.error:
            logger.error("regex not valid")
            return

        if filter_str == self.participant_filter:
            return

        self.participant_filter = filter_str
        self.selectedParticipants.clear()
        self.updateEMGParticipantBox()

    def updateFreqAnalysisParticipantTree(self, participants):
        widgets.frequency_participants.clear()
        widgets.frequency_participants.setColumnCount(1)
        for p in participants:
            treeItem = QTreeWidgetItem()
            treeItem.setText(0, p.name)
            widgets.frequency_participants.addTopLevelItem(treeItem)
            emg = self.workspace[p].emg
            for c in emg.getChannels():
                treeItem2 = QTreeWidgetItem(treeItem)
                treeItem2.setText(0, c)  # channel name
                treeItem.addChild(treeItem2)
        # connect slots
        widgets.frequency_participants.itemDoubleClicked.connect(
            self.updateFreqAnalysisWaveformPanel
        )
        widgets.frequency_participants.setHeaderItem(QTreeWidgetItem(["Participant(s)"]))
        widgets.frequency_participants.addTopLevelItem(treeItem)

    def updateFreqAnalysisWaveformPanel(self, item, column):
        # if item is top level, return
        if item.parent() is None:
            return

        p_name = item.parent().text(column)
        channel = item.text(column)
        p = self.workspace.findParticipant(p_name)

        # set state machine
        logger.info(
            "Frequency Analysis - selecting {} channel {}".format(p.name, channel)
        )
        self.freqAnalysis = (p, channel)
        self.freqAnalysisPlots.clear()

        # Show frequency-safe signal (DC + band-pass only, no envelope, no normalization)
        # getFreqSafeSegment filters the FULL signal first, then crops — no edge artefacts.
        crop = self.workspace[p].crop_interval
        arr = self.workspace[p].emg.getFreqSafeSegment(channel, crop)
        fs = self.workspace[p].emg.getfs()
        seg_duration = len(arr) / fs if len(arr) > 0 else 0.0
        seg_start = crop[0] if crop is not None else 0.0
        seg_end   = seg_start + seg_duration
        # x-axis in absolute trial time so it matches the Start/End Time fields
        x = np.linspace(seg_start, seg_end, len(arr)) if len(arr) > 0 else np.array([])
        widgets.freq_timedomain.line(x, arr, channel)
        widgets.freq_timedomain.show()

        # Pre-populate Start / End Time fields with ABSOLUTE trial times.
        # Users enter times matching the waveform x-axis (e.g. 3.0 → 10.0).
        widgets.lineEdit_5.setText("{:.4f}".format(seg_start))
        widgets.lineEdit_4.setText("{:.4f}".format(seg_end))

        self.updateFreqAnalysisFFTPanel()

    def updateFreqAnalysisFFTPanel(self):
        # update control ui
        # get pages per frame
        plotsPerPage = self.plotsPerPage_list[widgets.comboBox_19.currentIndex()]
        widgets.scrollArea_3.setPlotsPerPage(plotsPerPage)
        # page index selector
        currentpage = widgets.scrollArea_3.currentPage()
        widgets.comboBox_20.clear()
        widgets.comboBox_20.addItems(
            [str(i + 1) for i in range(0, widgets.scrollArea_3.pages())]
        )

        widgets.scrollArea_3.setCurrentPage(currentpage)
        widgets.comboBox_20.setCurrentIndex(widgets.scrollArea_3.currentPage())
        widgets.scrollArea_3.show()
        logger.info(
            "Updating FFT Analysis figure, nums_per_page: {} total page: {}, total plots:{}, current page: {}".format(
                widgets.scrollArea_3.plotsPerPage(),
                widgets.scrollArea_3.pages(),
                widgets.scrollArea_3.size(),
                widgets.scrollArea_3.currentPage(),
            )
        )

    # Application Logic
    # ///////////////////////////////////////////////////////////////
    def reset(self):
        self.singleEMG = (None, None, None)
        self.inputBuffer = None
        self.outputBuffer = None
        self.workspace = None
        self.home = None
        self.model = None
        self.freqAnalysis = (None, None)
        self.filesystemTree = QFileSystemModel()
        self.selectedParticipants.clear()
        self._crop_group.setEnabled(False)
        self._crop_start_spin.setValue(0.0)
        self._crop_end_spin.setValue(0.0)
        self._crop_status_label.setText("Full trial (no crop)")
        self._crop_status_label.setStyleSheet("color: gray; font-size: 10px;")

    def newWorkSpace(self, fpath, name):
        # create new project
        self.workspace = workspace(str(fpath), name)
        self.home = str(fpath)
        self._set_add_emg_enabled(True)

        # clear GUI
        self.updateEMGSignalProcessPanel()
        self.updateEMGConfigureList()
        self.updateEMGParticipantBox()
        self.updateWorkSpaceParticipantBox()
        self.updateWorkProjectTreeWidget()
        self.updateEMGChannelSelectorContent()

        # auto save
        self.enableAutoSave(True)

        widgets.stats_page.on_workspace_changed(self.home)
        return 0

    def saveWorkSpace(self):
        self.workspace.saveWorkSpace(self.home)
        return 0

    def loadWorkSpace(self, path, file):
        self.workspace = workspace.loadWorkSpace(
            path, file, self.emitPariticipantUpdate, self.emitAsyncLoadError
        )
        if self.workspace == None:
            return -1
        self.home = self.workspace.fpath
        self._set_add_emg_enabled(True)

        # load workspace file exploer
        self.filesystemTree.setRootPath(self.home)

        # clear and load GUI
        self.updateEMGSignalProcessPanel()
        self.updateEMGConfigureList()
        self.updateEMGParticipantBox()
        self.updateWorkSpaceParticipantBox()
        self.updateWorkProjectTreeWidget()
        self.updateEMGChannelSelectorContent()
        self.updateEMGSavedConfigureList()

        # auto save
        self.enableAutoSave(True)

        widgets.stats_page.on_workspace_changed(self.home)
        return 0

    def populateKinematicTree(self, tree: QTreeWidget, participants):
        tree.clear()
        tree.setColumnCount(1)
        tree.setDragEnabled(True)
        tree.setDropIndicatorShown(True)
        tree.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        for p in participants:
            treeItem = QTreeWidgetItem()
            treeItem.setText(0, p.name)
            tree.addTopLevelItem(treeItem)
            person = self.workspace[p]
            emg = person.emg
            k = person.kinematic
            # k is None while this participant's async load hasn't finished yet
            # (see workspace.emgAsyncLoader) — treat that the same as "not
            # ready" rather than crashing the whole tree rebuild.
            if k is None:
                continue
            # Markers group — only participants with valid kinematics have
            # markers, but EMG/force-plate/events/crop below don't depend on
            # that, so an EMG-only participant still gets a browsable tree.
            if k.isValid():
                marker_labels = k.reallabels
                marker_group = QTreeWidgetItem(treeItem)
                marker_group.setText(0, "Markers ({})".format(len(marker_labels)))
                for point in marker_labels:
                    tr = QTreeWidgetItem(marker_group)
                    tr.setFlags(tr.flags() | Qt.ItemIsDragEnabled | Qt.ItemIsSelectable)
                    tr.setText(0, point)
                marker_group.setExpanded(False)
                treeItem.addChild(marker_group)
            # Model outputs group — Angles only (Forces/Moments/Powers/etc are
            # excluded entirely upstream, see kinematic.py's _is_model_output);
            # its own subtree, separate from Markers, since these aren't 3D
            # marker positions and are never rendered in the 3D viewport.
            if k.isValid() and k.anglelabels:
                angle_group = QTreeWidgetItem(treeItem)
                angle_group.setText(0, "Model Outputs")
                angles_node = QTreeWidgetItem(angle_group)
                angles_node.setText(0, "Angles ({})".format(len(k.anglelabels)))
                for label in k.anglelabels:
                    tr = QTreeWidgetItem(angles_node)
                    tr.setFlags(tr.flags() | Qt.ItemIsDragEnabled | Qt.ItemIsSelectable)
                    tr.setText(0, label)
                angle_group.setExpanded(False)
                angles_node.setExpanded(False)
                treeItem.addChild(angle_group)
            # EMG group
            emg_channels = emg.getChannels()
            if emg_channels:
                emg_group = QTreeWidgetItem(treeItem)
                emg_group.setText(0, "EMG ({})".format(len(emg_channels)))
                for c in emg_channels:
                    ch_node = QTreeWidgetItem(emg_group)
                    ch_node.setText(0, c)
                emg_group.setExpanded(False)
                treeItem.addChild(emg_group)
            # Force Plates group
            if k.force_plates:
                fp_group = QTreeWidgetItem(treeItem)
                fp_group.setText(0, "Force Plates ({})".format(len(k.force_plates)))
                for fp in k.force_plates:
                    plate_node = QTreeWidgetItem(fp_group)
                    plate_node.setText(0, "Plate {}".format(fp.plate_id))
                    for comp in ("Fx", "Fy", "Fz"):
                        comp_node = QTreeWidgetItem(plate_node)
                        comp_node.setText(0, "Plate{} {}".format(fp.plate_id, comp))
                fp_group.setExpanded(False)
                treeItem.addChild(fp_group)
            # Events group — C3D events from kinematic + user-created events from profile
            extra_evs = getattr(person, "extra_events", [])
            c3d_evs = getattr(k, "events", [])
            all_events = sorted(list(c3d_evs) + list(extra_evs), key=lambda e: e.time_s)
            if all_events:
                ev_group = QTreeWidgetItem(treeItem)
                ev_group.setText(0, "Events ({})".format(len(all_events)))
                for ev in all_events:
                    source = "" if ev in extra_evs else " [C3D]"
                    ch = QTreeWidgetItem(ev_group)
                    ch.setText(0, "{} | {} | {:.3f}s{}".format(
                        ev.label, ev.context, ev.time_s, source))
                ev_group.setExpanded(False)
                treeItem.addChild(ev_group)
            # Crop node — if a crop interval is saved for this participant
            ci = getattr(person, "crop_interval", None)
            if ci is not None:
                crop_node = QTreeWidgetItem(treeItem)
                crop_node.setText(0, "Crop: {:.3f} s → {:.3f} s".format(ci[0], ci[1]))
                treeItem.addChild(crop_node)

        # itemDoubleClicked isn't reset by tree.clear(), so reconnecting on every
        # call (this runs on every tab visit / participant-list refresh) would
        # stack up duplicate connections — each firing loadKinemtic once more,
        # which is why the "no kinematics" prompt could pop up several times
        # for a single double-click. Disconnect first so exactly one remains.
        try:
            tree.itemDoubleClicked.disconnect(self.loadKinemtic)
        except (TypeError, RuntimeError):
            pass  # nothing was connected yet
        tree.itemDoubleClicked.connect(self.loadKinemtic)
        tree.setHeaderItem(QTreeWidgetItem(["Participant(s)"]))

    def _setup_kinematics_splitters(self):
        """Replace the fixed HBox/VBox layouts in the kinematics page with QSplitters.

        horizontalLayout_37 originally holds kinematics_render and kinematics_graphs
        in a plain QHBoxLayout with no drag handle.  verticalLayout_44 holds the top
        area and the playbar frame with no drag handle either.  Both are replaced here
        so the user can resize the panes at runtime.
        """
        # 1. Horizontal splitter: 3D render | trajectory plot
        h_split = QSplitter(Qt.Orientation.Horizontal)
        h_split.setHandleWidth(4)
        h_split.setChildrenCollapsible(False)
        widgets.horizontalLayout_37.removeWidget(widgets.kinematics_render)
        widgets.horizontalLayout_37.removeWidget(widgets.kinematics_graphs)
        h_split.addWidget(widgets.kinematics_render)
        h_split.addWidget(widgets.kinematics_graphs)
        h_split.setStretchFactor(0, 1)
        h_split.setStretchFactor(1, 1)
        h_split.setSizes([600, 600])  # equal render and graph on first open
        widgets.horizontalLayout_37.addWidget(h_split)
        # Relax the hard minimum so the handle can actually be dragged left
        widgets.renderWidget.setMinimumWidth(150)

        # 2. Vertical splitter: top area (render + plot) | playbar
        v_split = QSplitter(Qt.Orientation.Vertical)
        v_split.setHandleWidth(4)
        v_split.setChildrenCollapsible(False)
        widgets.verticalLayout_44.removeWidget(widgets.kinematics_left_top)
        widgets.verticalLayout_44.removeWidget(widgets.kinematics_left_bottom)
        v_split.addWidget(widgets.kinematics_left_top)
        v_split.addWidget(widgets.kinematics_left_bottom)
        v_split.setStretchFactor(0, 4)  # render+plot area takes most vertical space
        v_split.setStretchFactor(1, 0)  # playbar stays compact
        v_split.setSizes([700, 100])    # playbar compact on first open
        widgets.verticalLayout_44.addWidget(v_split)

    def _setup_emg_splitters(self):
        """Replace the fixed layouts in the EMG processing page with QSplitters.

        horizontalLayout_18 holds data_process_graphic (left, plots) and
        data_process_instruction (right, pipeline/crop panel) in a plain QHBoxLayout.
        verticalLayout_39 holds data_process_graphic_top (input plot) and
        data_process_graphic_bottom (output plot) in a plain QVBoxLayout.
        Both are replaced here so the user can resize panes at runtime.
        """
        # 1. Horizontal splitter: plots area | pipeline/config panel
        h_split = QSplitter(Qt.Orientation.Horizontal)
        h_split.setHandleWidth(4)
        h_split.setChildrenCollapsible(False)
        widgets.horizontalLayout_18.removeWidget(widgets.data_process_graphic)
        widgets.horizontalLayout_18.removeWidget(widgets.data_process_instruction)
        h_split.addWidget(widgets.data_process_graphic)
        h_split.addWidget(widgets.data_process_instruction)
        h_split.setStretchFactor(0, 2)  # plots take more horizontal space
        h_split.setStretchFactor(1, 1)  # pipeline panel is narrower
        h_split.setSizes([700, 300])    # plots 70%, pipeline 30% on first open
        widgets.horizontalLayout_18.addWidget(h_split)
        widgets.data_process_instruction.setMinimumWidth(200)

        # 2. Vertical splitter: input plot | output plot
        v_split = QSplitter(Qt.Orientation.Vertical)
        v_split.setHandleWidth(4)
        v_split.setChildrenCollapsible(False)
        widgets.verticalLayout_39.removeWidget(widgets.data_process_graphic_top)
        widgets.verticalLayout_39.removeWidget(widgets.data_process_graphic_bottom)
        v_split.addWidget(widgets.data_process_graphic_top)
        v_split.addWidget(widgets.data_process_graphic_bottom)
        v_split.setStretchFactor(0, 1)
        v_split.setStretchFactor(1, 1)
        v_split.setSizes([500, 500])    # equal top and bottom plots on first open
        widgets.verticalLayout_39.addWidget(v_split)

    def _setup_start_page_splitter(self):
        """Replace the fixed HBoxLayout on the start page with a QSplitter.

        horizontalLayout_7 holds start_left (sidebar), start_middle (guide cards),
        and start_right (empty) in a plain QHBoxLayout with no drag handle.
        """
        h_split = QSplitter(Qt.Orientation.Horizontal)
        h_split.setHandleWidth(4)
        h_split.setChildrenCollapsible(False)
        widgets.horizontalLayout_7.removeWidget(widgets.start_left)
        widgets.horizontalLayout_7.removeWidget(widgets.start_middle)
        widgets.horizontalLayout_7.removeWidget(widgets.start_right)
        h_split.addWidget(widgets.start_left)
        h_split.addWidget(widgets.start_middle)
        # start_right is no longer needed — hide it so it takes no space
        widgets.start_right.hide()
        h_split.setStretchFactor(0, 3)
        h_split.setStretchFactor(1, 7)
        h_split.setSizes([300, 700])
        widgets.horizontalLayout_7.addWidget(h_split)

    def _setup_emg_page_splitters(self):
        """Add splitters to the EMG page outer layout and the left body vertical stack.

        horizontalLayout_16 holds emg_left_body (plots + log) and emg_right_body
        (actions + config table) in a plain QHBoxLayout.
        verticalLayout_33 holds data_process (processing area) and configuration_list
        (config log) in a plain QVBoxLayout.
        Both are replaced here so the user can resize the panes at runtime.
        """
        # 1. Horizontal splitter: EMG left body | EMG right body
        h_split = QSplitter(Qt.Orientation.Horizontal)
        h_split.setHandleWidth(4)
        h_split.setChildrenCollapsible(False)
        widgets.horizontalLayout_16.removeWidget(widgets.emg_left_body)
        widgets.horizontalLayout_16.removeWidget(widgets.emg_right_body)
        h_split.addWidget(widgets.emg_left_body)
        h_split.addWidget(widgets.emg_right_body)
        # Preserve the original 7:1 proportions from sizePolicy horizontal stretch values
        h_split.setStretchFactor(0, 7)
        h_split.setStretchFactor(1, 1)
        h_split.setSizes([700, 300])    # left body 70%, right body 30% on first open
        widgets.horizontalLayout_16.addWidget(h_split)

        # 2. Vertical splitter within emg_left_body: processing area | config log
        v_split = QSplitter(Qt.Orientation.Vertical)
        v_split.setHandleWidth(4)
        v_split.setChildrenCollapsible(False)
        widgets.verticalLayout_33.removeWidget(widgets.data_process)
        widgets.verticalLayout_33.removeWidget(widgets.configuration_list)
        v_split.addWidget(widgets.data_process)
        v_split.addWidget(widgets.configuration_list)
        v_split.setStretchFactor(0, 7)
        v_split.setStretchFactor(1, 3)
        v_split.setSizes([700, 300])    # processing 70%, config log 30% on first open
        widgets.verticalLayout_33.addWidget(v_split)

        # 3. Vertical splitter within emg_right_body: config table | config file panel
        rv_split = QSplitter(Qt.Orientation.Vertical)
        rv_split.setHandleWidth(4)
        rv_split.setChildrenCollapsible(False)
        widgets.verticalLayout_34.removeWidget(widgets.frame_23)
        widgets.verticalLayout_34.removeWidget(widgets.frame_25)
        rv_split.addWidget(widgets.frame_23)
        rv_split.addWidget(widgets.frame_25)
        rv_split.setStretchFactor(0, 7)
        rv_split.setStretchFactor(1, 3)
        rv_split.setSizes([700, 300])   # table 70%, config file 30% on first open
        widgets.verticalLayout_34.addWidget(rv_split)

    def _setup_kinematics_page_splitter(self):
        """Add a splitter between the kinematics main area and the right label tree.

        horizontalLayout_36 holds kinematics_left (3D view + playbar, already splitter-
        enabled internally) and kinematics_right (label tree) in a plain QHBoxLayout.
        """
        h_split = QSplitter(Qt.Orientation.Horizontal)
        h_split.setHandleWidth(4)
        h_split.setChildrenCollapsible(False)
        widgets.horizontalLayout_36.removeWidget(widgets.kinematics_left)
        widgets.horizontalLayout_36.removeWidget(widgets.kinematics_right)
        h_split.addWidget(widgets.kinematics_left)
        h_split.addWidget(widgets.kinematics_right)
        # Preserve the original 8:2 proportions from sizePolicy horizontal stretch values
        h_split.setStretchFactor(0, 8)
        h_split.setStretchFactor(1, 2)
        h_split.setSizes([800, 200])    # main area 80%, label tree 20% on first open
        widgets.horizontalLayout_36.addWidget(h_split)

    def _setup_frequency_page_splitters(self):
        """Add splitters to the frequency domain page.

        horizontalLayout_40 holds frequency_left (plots + controls) and frequency_right
        (participants tree) in a plain QHBoxLayout.
        verticalLayout_20 holds frequency_top (time-domain view) and frequency_bottom
        (frequency plots + page controls) in a plain QVBoxLayout.
        """
        # 1. Horizontal splitter: frequency left area | participants tree
        h_split = QSplitter(Qt.Orientation.Horizontal)
        h_split.setHandleWidth(4)
        h_split.setChildrenCollapsible(False)
        widgets.horizontalLayout_40.removeWidget(widgets.frequency_left)
        widgets.horizontalLayout_40.removeWidget(widgets.frequency_right)
        h_split.addWidget(widgets.frequency_left)
        h_split.addWidget(widgets.frequency_right)
        # Preserve the original 8:2 proportions
        h_split.setStretchFactor(0, 8)
        h_split.setStretchFactor(1, 2)
        h_split.setSizes([800, 200])    # left area 80%, participants tree 20% on first open
        widgets.horizontalLayout_40.addWidget(h_split)

        # 2. Vertical splitter: time-domain view | frequency plots + controls
        v_split = QSplitter(Qt.Orientation.Vertical)
        v_split.setHandleWidth(4)
        v_split.setChildrenCollapsible(False)
        widgets.verticalLayout_20.removeWidget(widgets.frequency_top)
        widgets.verticalLayout_20.removeWidget(widgets.frequency_bottom)
        v_split.addWidget(widgets.frequency_top)
        v_split.addWidget(widgets.frequency_bottom)
        v_split.setStretchFactor(0, 2)
        v_split.setStretchFactor(1, 4)
        v_split.setSizes([400, 400])    # equal top and bottom on first open
        widgets.verticalLayout_20.addWidget(v_split)

    def _replace_start_middle_with_logo(self):
        """Replace the guide-card scroll area with the full-width branding logo."""
        from PySide6.QtGui import QPixmap as _QPixmap

        logo_path = os.path.join(os.path.dirname(__file__), "myotion_resources", "fulllogo_transparent.png")
        pix = _QPixmap(logo_path)

        # Clear all widgets from the layout (removes scrollArea_2 + guide cards)
        layout = widgets.start_middle.layout()
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        # Neutral background so the transparent logo reads cleanly
        widgets.start_middle.setStyleSheet("background-color: #f4f4f4; border: none;")

        # ScaledImageLabel scales to fill the panel while keeping the logo's aspect ratio
        logo_label = ScaledImageLabel(pix)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addWidget(logo_label)

    def _generate_custom_icons(self):
        """Draw and cache custom nav icons; apply them to the relevant buttons.

        Icons are 20×20 white-on-transparent PNGs created with QPainter so no
        external image files are needed.  They are regenerated on every start so
        any future tweaks to the drawing code take effect immediately.
        """
        import math
        from PySide6.QtGui import (
            QPixmap, QPainter, QPen, QColor, QBrush, QPainterPath,
        )
        from PySide6.QtCore import Qt, QRectF, QPointF

        icons_dir = os.path.join(os.path.dirname(__file__), "myotion_resources", "icons")
        os.makedirs(icons_dir, exist_ok=True)

        W, H = 20, 20
        WHITE = QColor(255, 255, 255)

        # ── Stick figure (Kinematics Inspection) ──────────────────────────────
        pix = QPixmap(W, H)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(WHITE)
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)

        # Head — filled circle
        p.setBrush(QBrush(WHITE))
        p.drawEllipse(QRectF(7, 0, 6, 6))
        p.setBrush(Qt.BrushStyle.NoBrush)
        # Body
        p.drawLine(QPointF(10, 6), QPointF(10, 13))
        # Arms
        p.drawLine(QPointF(10, 8), QPointF(4,  12))
        p.drawLine(QPointF(10, 8), QPointF(16, 12))
        # Legs
        p.drawLine(QPointF(10, 13), QPointF(5,  20))
        p.drawLine(QPointF(10, 13), QPointF(15, 20))
        p.end()

        stick_path = os.path.join(icons_dir, "stick_figure.png")
        pix.save(stick_path, "PNG")

        # ── EMG action-potential wave (EMG Time Domain) ────────────────────────
        pix = QPixmap(W, H)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(WHITE)
        pen.setWidthF(1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)

        # Baseline → dip down → spike up → baseline  (action-potential shape)
        path = QPainterPath()
        path.moveTo(0, 10)
        path.lineTo(4, 10)                          # flat left
        path.cubicTo(5, 10,  6, 17,  7.5, 17)      # dip down
        path.cubicTo(9, 17,  10, 2,  12, 2)         # spike up
        path.cubicTo(13, 2,  14, 10, 15, 10)        # return to baseline
        path.lineTo(20, 10)                          # flat right
        p.drawPath(path)
        p.end()

        wave_path = os.path.join(icons_dir, "emg_wave.png")
        pix.save(wave_path, "PNG")

        # ── Workspace panel toggle (hide/show), drawn once then mirrored ───────
        pix = QPixmap(W, H)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(WHITE)
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)

        # Panel edge (vertical bar) with an arrow pointing away from it —
        # "hide" collapses the panel toward/off the left edge.
        p.drawLine(QPointF(15, 3), QPointF(15, 17))
        p.drawLine(QPointF(4, 10), QPointF(13, 10))    # shaft
        p.drawLine(QPointF(4, 10), QPointF(8, 6))       # arrowhead
        p.drawLine(QPointF(4, 10), QPointF(8, 14))
        p.end()

        workspace_hide_path = os.path.join(icons_dir, "workspace_hide.png")
        pix.save(workspace_hide_path, "PNG")

        workspace_show_path = os.path.join(icons_dir, "workspace_show.png")
        pix.toImage().mirrored(True, False).save(workspace_show_path, "PNG")

        # Panel starts closed (see extraLeftBox's initial max width of 0), so
        # the button should initially offer to show it; toggleLeftBox() swaps
        # between these two icons on each click.
        self._workspace_hide_icon_path = workspace_hide_path
        self._workspace_show_icon_path = workspace_show_path

        # ── Recolor provided glyph icons for the dark sidebar ──────────────────
        # advanced.png / statistical.png ship as black glyphs on an opaque
        # white canvas (not true alpha-transparent PNGs), so they'd render as
        # solid white squares on the dark menu. Invert luminance into the
        # alpha channel — dark strokes become opaque white, the light canvas
        # becomes transparent — then downscale to match the other nav icons.
        def _recolor_white(src_path, out_name):
            import numpy as _np
            from PySide6.QtGui import QImage

            src = QImage(src_path)
            if src.isNull():
                return None
            src = src.convertToFormat(QImage.Format.Format_ARGB32)
            w, h = src.width(), src.height()
            stride = src.bytesPerLine() // 4
            buf = src.constBits()
            if hasattr(buf, "setsize"):
                buf.setsize(h * src.bytesPerLine())
            arr = _np.frombuffer(buf, dtype=_np.uint8).reshape(h, stride, 4)[:, :w, :]
            b, g, r, a = (arr[..., i].astype(_np.float32) for i in range(4))
            luminance = 0.299 * r + 0.587 * g + 0.114 * b
            new_alpha = ((255.0 - luminance) * (a / 255.0)).clip(0, 255).astype(_np.uint8)

            out = _np.zeros((h, w, 4), dtype=_np.uint8)
            out[..., :3] = 255  # white
            out[..., 3] = new_alpha
            out_img = QImage(out.data, w, h, QImage.Format.Format_ARGB32).copy()
            out_img = out_img.scaled(
                W, H, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            out_path = os.path.join(icons_dir, out_name)
            out_img.save(out_path, "PNG")
            return out_path

        advanced_path = _recolor_white(
            os.path.join(icons_dir, "advanced.png"), "advanced_nav.png"
        )
        statistical_path = _recolor_white(
            os.path.join(icons_dir, "statistical.png"), "statistical_nav.png"
        )
        workspace_header_path = _recolor_white(
            os.path.join(icons_dir, "workspace.png"), "workspace_header_nav.png"
        )
        home_path = os.path.join(os.path.dirname(__file__), "images", "icons", "cil-home.png")

        # ── Apply to buttons via CSS file URL ─────────────────────────────────
        def _css_url(fp):
            return fp.replace("\\", "/")

        widgets.btn_kinematic.setStyleSheet(
            f"background-image: url({_css_url(stick_path)});"
        )
        widgets.btn_emg.setStyleSheet(
            f"background-image: url({_css_url(wave_path)});"
        )
        if os.path.exists(home_path):
            widgets.btn_start.setStyleSheet(
                f"background-image: url({_css_url(home_path)});"
            )
        if advanced_path:
            widgets.btn_advanced.setStyleSheet(
                f"background-image: url({_css_url(advanced_path)});"
            )
        if statistical_path:
            widgets.btn_stats.setStyleSheet(
                f"background-image: url({_css_url(statistical_path)});"
            )
        if workspace_header_path:
            widgets.extraIcon.setStyleSheet(
                f"background-image: url({_css_url(workspace_header_path)});"
                "background-position: center;"
                "background-repeat: no-repeat;"
            )
        widgets.toggleLeftBox.setStyleSheet(
            f"background-image: url({_css_url(workspace_show_path)});"
        )
        # The panel's own close button always hides it, so it always shows the
        # "hide" affordance — same icon as the sidebar toggle uses when open.
        widgets.extraCloseColumnBtn.setIcon(QIcon(workspace_hide_path))

    def preloadKinematicPage(self):
        if self.workspace is None:
            widgets.kinematics_label_tree.clear()
            return
        ps = self.workspace.getParticipants()
        self.populateKinematicTree(widgets.kinematics_label_tree, ps)

    def loadKinemtic(self, item, column):
        # if item is not top level, return
        if item.parent() != None:
            return

        p_name = item.text(column)
        p = self.workspace.findParticipant(p_name)
        profile = self.workspace[p]

        if profile.kinematic is None:
            # Still async-loading (see workspace.emgAsyncLoader) — offering to
            # attach a kinematics file here would race with the loader thread
            # about to set profile.kinematic itself.
            QMessageBox.information(
                self, self.tr("Still loading"),
                self.tr("'{}' is still loading. Try again in a moment.").format(p_name),
                QMessageBox.Ok,
            )
            return

        if not profile.kinematic.isValid():
            reply = QMessageBox.question(
                self, self.tr("No kinematics data"),
                self.tr(
                    "'{}' has EMG data but no kinematics/force-plate data.\n\n"
                    "Attach a kinematics file to this participant now?"
                ).format(p_name),
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self._attachKinematicsToParticipant(profile, p_name)
                return
            # No — fall through and open an EMG-only Signals/playback view
            # below instead of a dead end; Model/Controller both understand
            # a participant with no usable kinematics clock (see Model.has_kinematics).

        # Tear down the previously active controller first — each load builds
        # a new one against the same shared widgets, and without this the old
        # timer and signal connections (tree double-click, playbar, ...) keep
        # firing alongside whatever loads next (see Controller.stop()).
        if getattr(self, "_active_controller", None) is not None:
            self._active_controller.stop()

        self.model = Model(self.workspace[p])
        top = widgets.graph_top
        top.setModel(self.model, widgets.kinematics_label_tree)
        # Remove pre-populated Events and Crop nodes — Controller will rebuild them live
        for i in range(item.childCount() - 1, -1, -1):
            txt = item.child(i).text(0)
            if txt.startswith("Events") or txt.startswith("Crop:"):
                item.removeChild(item.child(i))
        self._active_controller = Controller(
            self.model,
            widgets.renderWidget,
            widgets.playSlider,
            widgets.kinematic_analysis,
            None,
            widgets.kinematics_label_tree,
            participant_item=item,
            save_callback=lambda: self.saveProjectButtonClick(show=False),
            export_events_callback=lambda: self._exportParticipantEvents(p),
        )

    def _attachKinematicsToParticipant(self, profile, p_name):
        """Open the Align & Stitch dialog to attach a kinematics/force-plate
        file to a participant that currently only has EMG data. The EMG side
        is fixed to this participant's already-loaded EMG file — only the
        kinematics file is picked. On success the participant is updated in
        place (kept as one entry, with its existing EMG channel/muscle
        mapping intact) rather than creating a new, duplicate participant.
        """
        def _on_saved(out_path):
            profile.kinematic_file = out_path
            profile.kinematic = kinematic(out_path)
            self.saveProjectButtonClick(show=False)
            QMessageBox.information(
                self, self.tr("Attached"),
                self.tr(
                    "Kinematics data attached to '{}'.\n\n"
                    "Double-click the participant again to view it."
                ).format(p_name),
                QMessageBox.Ok,
            )
            self.preloadKinematicPage()

        dlg = StitchAlignmentDialog(
            emg_file=profile.emg.emgFile, lock_emg=True, on_saved=_on_saved, parent=self,
        )
        dlg.run()

    def preloadFreqAnalysisPage(self):
        if self.workspace == None:
            widgets.frequency_participants.clear()
            self.freqAnalysisPlots.clear()
            widgets.scrollArea_3.deleteAllPages()
            return -1
        self.updateFreqAnalysisParticipantTree(self.workspace.getParticipants())
        self.freqAnalysisPlots.clear()

    def addNewFFTtoFreqAnalysisFFTPanel(self):
        p, chan = self.freqAnalysis
        # tst starts at t=0 (segment-relative); user fields use absolute trial time
        tst = self._get_freq_safe_tst(p, chan)
        crop = self.workspace[p].crop_interval
        seg_start = crop[0] if crop is not None else 0.0
        seg_dur   = tst.time

        try:
            left_abs  = float(widgets.lineEdit_5.text())
            right_abs = float(widgets.lineEdit_4.text())
        except Exception:
            left_abs  = seg_start
            right_abs = seg_start + seg_dur

        # Convert absolute times → segment-relative for fft_db, then clamp
        left_rel  = max(0.0, min(left_abs  - seg_start, seg_dur))
        right_rel = max(0.0, min(right_abs - seg_start, seg_dur))
        if right_rel <= left_rel:
            left_rel, right_rel = 0.0, seg_dur

        # Write clamped absolute times back so the user sees the accepted window
        widgets.lineEdit_5.setText("{:.4f}".format(left_rel  + seg_start))
        widgets.lineEdit_4.setText("{:.4f}".format(right_rel + seg_start))

        num_plots = 1
        if widgets.lineEdit_6.text() != "":
            try:
                num_plots = max(1, int(widgets.lineEdit_6.text()))
            except Exception:
                pass

        curr_rel = left_rel
        step = (right_rel - left_rel) / num_plots
        for i in range(0, num_plots):
            t_abs_start = curr_rel + seg_start
            t_abs_end   = curr_rel + step + seg_start
            title = "Frequency Analysis: {:.3f} s to {:.3f} s".format(
                t_abs_start, t_abs_end
            )
            newPlot = self.FreqAnalysisCreateQPlotView(
                p, chan, curr_rel, curr_rel + step,
                title=title, seg_start=seg_start,
            )
            self.freqAnalysisPlots.append(newPlot)
            widgets.scrollArea_3.append(newPlot)
            curr_rel += step
        self.updateFreqAnalysisFFTPanel()

    def startSingleEMGProcess(self, p):
        logger.info("started single EMG process for {}".format(p.name))
        if not self.workspace.hasParticipant(p):
            return -1

        # Update validator ranges for filter input boxes.
        self.updateEMGFilterValidators(p)

        # set fsm
        chan = self.workspace[p].emg.getChannels()[0]
        self.singleEMG = (p, 0, chan)
        self.workspace[p].emg.startProcess()
        self.updateEMGConfigureList()
        self.updateEMGChannelSelectorContent()
        self.updateEMGChannelSelectorText(chan)
        # Load the pipeline panel with the freshly created config
        cfg = self.workspace[p].emg.getProcessConfig()
        fs = self.workspace[p].emg.getfs()
        self._pipeline_panel.load(cfg, fs)
        self._emg_action_bar.show()
        # Sync crop widget to trial duration and any previously saved crop interval
        self._sync_crop_widget(p)
        self.selectSingleEMGStep(0)

    def __updateEMGRenderBuffer(self, prev=True, post=True):
        p, step, chan = self.singleEMG
        crop = self.workspace[p].crop_interval
        if prev:
            if step == 0:
                self.inputBuffer = self.workspace[p].emg[chan]
            else:
                self.inputBuffer = self.workspace[p].emg.tryConfigStepTo(
                    chan, step - 1, crop
                )
        if post:
            self.outputBuffer = self.workspace[p].emg.tryConfigStepTo(
                chan, step, crop
            )

    def selectSingleEMGChannel(self, chan):
        p, step, oldchan = self.singleEMG
        self.singleEMG = (p, step, chan)
        self.__updateEMGRenderBuffer()
        self.updateEMGSignalProcessPanel()

        # update summary toolbox
        cfg = self.workspace[p].emg.getProcessConfig()
        if cfg is None:
            return
        idx = widgets.listWidget.currentRow()
        type, str = cfg.getTypeInfo(idx)
        widgets.toolBox.setCurrentIndex(int(type))
        self.updateEMGToolBox(type)
        
    def selectSingleEMGStep(self, idx):
        p, step, chan = self.singleEMG
        if p is None:
            return
        cfg = self.workspace[p].emg.getProcessConfig()
        if cfg is None:
            return
        cfgstrings = cfg.getStepStringList()

        if idx > len(cfgstrings) or idx < 0:
            logger.info("single EMG process idx {} out of range".format(idx))

        logger.info("selecting EMG process step {}, {}".format(idx, cfgstrings[idx]))
        self.singleEMG = (p, idx, chan)
        logger.info("Current channel {}".format(chan))

        self.__updateEMGRenderBuffer()
        # select index for EMG config widget
        widgets.listWidget.setCurrentRow(idx)
        # update UI
        self.updateEMGSignalProcessPanel()
        type, str = cfg.getTypeInfo(idx)
        widgets.toolBox.setCurrentIndex(int(type))
        self.updateEMGToolBox(type)
        # sync pipeline panel
        self._pipeline_panel.highlightStep(idx)
        self._pipeline_panel.scrollToCard(idx)

    def _onPipelineStepChanged(self, step_idx):
        """Called when a pipeline card's config is modified by the user."""
        p, step, chan = self.singleEMG
        if p is None:
            return
        if step != step_idx:
            # Switch preview to the card that was just changed
            self.selectSingleEMGStep(step_idx)
        else:
            self.__updateEMGRenderBuffer(prev=False)
            self.updateEMGSignalProcessPanel(prev=False)

    def startBatchEMGProcess(self, people, configure):
        n = len(people)
        self._batch_progress = QProgressDialog(
            self.tr("Processing participants..."), self.tr("Cancel"), 0, n, self
        )
        self._batch_progress.setWindowTitle(self.tr("Myotion-ing"))
        self._batch_progress.setWindowModality(Qt.WindowModal)
        self._batch_progress.setMinimumDuration(0)
        self._batch_progress.setValue(0)

        self._batch_worker = BatchEMGWorker(
            self.workspace, people, configure, self.home, self
        )
        self._batch_worker.progress.connect(self._onBatchProgress)
        self._batch_worker.finished.connect(self._onBatchFinished)
        self._batch_worker.error.connect(self._onBatchError)
        self._batch_progress.canceled.connect(self._batch_worker.requestInterruption)
        self._batch_worker.start()

    def _onBatchProgress(self, count, name):
        self._batch_progress.setValue(count)
        self._batch_progress.setLabelText(self.tr("Processing: {}").format(name))

    def _onBatchFinished(self):
        self._batch_progress.setValue(self._batch_progress.maximum())
        self._batch_progress.close()
        self.selectedParticipants.clear()
        self.updateEMGParticipantBox()

    def _onBatchError(self, msg):
        logger.error("Batch process error: {}".format(msg))
        QMessageBox.warning(
            self,
            self.tr("Batch processing error"),
            self.tr("A participant failed to process:\n\n{}").format(msg),
            QMessageBox.Ok,
        )

    # ------------------------------------------------------------------
    # Manual EMG crop
    # ------------------------------------------------------------------

    def _sync_crop_widget(self, p):
        """Sync crop spinbox range and values to participant p's trial and profile."""
        emg = self.workspace[p].emg
        total = emg.rawTST.time if emg.rawTST is not None else emg.emgTST.time
        self._crop_start_spin.setRange(0.0, total)
        self._crop_end_spin.setRange(0.0, total)
        ci = self.workspace[p].crop_interval
        if ci is not None:
            self._crop_start_spin.setValue(ci[0])
            self._crop_end_spin.setValue(ci[1])
            self._crop_status_label.setText(
                "Active: {:.3f} s → {:.3f} s".format(ci[0], ci[1])
            )
            self._crop_status_label.setStyleSheet("color: #2a9d8f; font-size: 10px;")
        else:
            self._crop_start_spin.setValue(0.0)
            self._crop_end_spin.setValue(total)
            self._crop_status_label.setText("Full trial (no crop)")
            self._crop_status_label.setStyleSheet("color: gray; font-size: 10px;")
        self._crop_group.setEnabled(True)

    def _onCropApply(self):
        p, _, _ = self.singleEMG
        if p is None:
            return
        t_start = self._crop_start_spin.value()
        t_end = self._crop_end_spin.value()
        if t_end <= t_start:
            QMessageBox.warning(
                None,
                self.tr("Crop"),
                self.tr("End time must be greater than start time."),
            )
            return
        self.workspace[p].crop_interval = (t_start, t_end)
        self._crop_status_label.setText(
            "Active: {:.3f} s → {:.3f} s".format(t_start, t_end)
        )
        self._crop_status_label.setStyleSheet("color: #2a9d8f; font-size: 10px;")
        logger.info("EMG manual crop: {:.3f}s → {:.3f}s".format(t_start, t_end))
        # Re-render the plots zoomed to the selected segment
        self.__updateEMGRenderBuffer()
        self.updateEMGSignalProcessPanel()

    def _onCropClear(self):
        p, _, _ = self.singleEMG
        if p is None:
            return
        self.workspace[p].crop_interval = None
        self._sync_crop_widget(p)
        logger.info("EMG manual crop cleared for {}".format(p.name))
        # Re-render the plots showing the full trial again
        self.__updateEMGRenderBuffer()
        self.updateEMGSignalProcessPanel()


    def closeEvent(self, event):  # Window close event handler.
        if not ExitConfirmDialog.confirm(self):
            event.ignore()
            return

        # ── Qt WebEngine teardown fix ──────────────────────────────────────
        # Profiles for QPlotView / StatsChartView have no Qt parent (lifetime
        # is controlled by the Python wrapper's refcount).  During Python
        # shutdown the global `widgets` object is released, which releases the
        # Python wrappers — and with them `self.profile` — before Qt has had a
        # chance to destroy the C++ page objects that are still registered with
        # those profiles.  The result is the "Release of profile requested but
        # WebEnginePage still not deleted" warning.
        #
        # Fix: while the event loop is still running, replace each custom-
        # profile page with a blank default-profile page, detach and schedule
        # the old page for immediate deletion, then flush deleteLater().
        # The profile tracking list is empty by the time Python GC runs.
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        default_profile = QWebEngineProfile.defaultProfile()
        for view in self.findChildren(QWebEngineView):
            old_page = view.page()
            if old_page is None or old_page.profile() is default_profile:
                continue
            view.setPage(QWebEnginePage(view))  # give view a safe default page
            old_page.setParent(None)            # detach from Qt tree
            old_page.deleteLater()              # schedule immediate C++ deletion
        QApplication.processEvents()            # flush — pages are deleted now
        # ──────────────────────────────────────────────────────────────────

        self.deletePlots()
        self.logout_click()
        event.accept()

    def deletePlots(self):
        # Remove references that exist in both UI and Python layers.
        if hasattr(self, 'plot_input'):
            widgets.plot_input.deleteLater()
            del widgets.plot_input
        if hasattr(self, 'plot_output'):
            widgets.plot_output.deleteLater()
            del widgets.plot_output

    # Update batch-process button state.
    def updateBatchProcessButtonState(self):
        """Update whether the batch-process button is enabled."""
        # Check whether listWidget_2 has exactly one selected item.
        list_selected_count = len(widgets.listWidget_2.selectedItems())
        
        # Check number of selected participants in tableWidget_2.
        table_selected_count = len(self.selectedParticipants)
        
        # Set button enabled state.
        widgets.pushButton_12.setEnabled(list_selected_count == 1 and table_selected_count > 1)
        self._batch_edit_config_btn.setEnabled(table_selected_count >= 1)
        self._batch_edit_mapping_btn.setEnabled(table_selected_count >= 1)


# setting up Url Scheme string before app starts
# this is for qplotview setup
def QPlotViewSetup():
    scheme = QWebEngineUrlScheme(bytes("local", "ascii"))
    scheme.setFlags(
        QWebEngineUrlScheme.Flag.SecureScheme
        | QWebEngineUrlScheme.Flag.LocalScheme
        | QWebEngineUrlScheme.Flag.LocalAccessAllowed
    )
    QWebEngineUrlScheme.registerScheme(scheme)

def parse_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument('--lang', type=str, help='lanuage setting')
    return parser.parse_args()

if __name__ == "__main__":
    from PySide6.QtQuick import QQuickWindow, QSGRendererInterface

    # DO NOT REMOVE enforce pyside to use opengl for underlying graphics render.
    QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.OpenGL)

    # Setup Url scheme handler for WebEngineView
    QPlotViewSetup()

    # Setup appData path
    AppDataPath, LogPath = initializeAppDataFolder()

    # setup logfile
    sys_log = open(getSyslogFilename(LogPath), 'w', encoding="utf-8")
    logger.pipe = sys_log

    # argument parse
    args = parse_args(sys.argv[1:])

    qApp = QApplication(sys.argv)
    qApp.setWindowIcon(QIcon(":/images/Myotion_logo.png"))

    splash = MyotionSplashScreen()
    splash.start()
    qApp.processEvents()  # paint the splash before the (synchronous) UI build below

    # get language: --lang overrides, otherwise use the saved Preferences
    # choice, defaulting to en if nothing has been saved yet.
    language = args.lang
    if language is None:
        language = QSettings(_SETTINGS_ORG, _SETTINGS_APP).value("language", "en")

    # translator
    if language == "cn":
        translator = QTranslator(qApp)
        if translator.load(":/qm/CN.qm"):
            qApp.installTranslator(translator)

    window = MainWindow(language, sys_log, show_immediately=False)
    splash.finish()
    window.showMaximized()
    exit_code = qApp.exec()
    sys_log.close()
    sys.exit(exit_code)
