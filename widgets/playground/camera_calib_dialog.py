"""widgets/playground/camera_calib_dialog.py -- load a Vicon .xcp calibration
file and plot the camera rig (position + view frustum + orientation axes) in
3D space. Clicking a camera (in the 3D view or the sidebar list) highlights
it in both places."""

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFileDialog,
    QListWidget, QListWidgetItem, QMessageBox, QSplitter, QCheckBox,
)

from modules.kinematics.items import AxesItem, CameraFrustumItem
from modules.kinematics.items.cameraframustumitem import DEFAULT_FRUSTUM_COLOR, frustum_scene_points, lab_to_scene
from modules.playground.camera_calib import load_calibration, CalibrationError
from widgets.playground_gl_view import PlaygroundGLView

_AXIS_LENGTH = 200.0
_CATEGORIES = ("Optical", "Video")


class CameraCalibDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Camera Calibration Viewer"))
        self.resize(1000, 640)

        self._cameras = []
        self._updating_selection = False  # guards against list<->3D selection feedback loops
        self._updating_checks = False     # guards against item.setCheckState triggering itself

        layout = QVBoxLayout(self)

        top_row = QHBoxLayout()
        self._load_btn = QPushButton(self.tr("Load Calibration (.xcp / .qca.txt)..."))
        self._load_btn.clicked.connect(self._on_load)
        top_row.addWidget(self._load_btn)
        self._file_label = QLabel(self.tr("No file loaded"))
        top_row.addWidget(self._file_label, 1)
        layout.addLayout(top_row)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel(self.tr("Show:")))
        self._category_checks = {}
        for category in _CATEGORIES:
            cb = QCheckBox(self.tr(category))
            cb.setChecked(True)
            cb.toggled.connect(lambda checked, c=category: self._on_category_toggled(c, checked))
            filter_row.addWidget(cb)
            self._category_checks[category] = cb
        filter_row.addStretch()
        layout.addLayout(filter_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter, 1)

        self._cam_list = QListWidget()
        self._cam_list.currentRowChanged.connect(self._on_list_row_changed)
        self._cam_list.itemChanged.connect(self._on_item_check_changed)
        splitter.addWidget(self._cam_list)

        self._gl_view = PlaygroundGLView(camera_far=50000, camera_position=(0, 3000, 6000))
        self._gl_view.itemPicked.connect(self._on_gl_picked)
        splitter.addWidget(self._gl_view)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

        self._status_label = QLabel(self.tr("No camera selected"))
        layout.addWidget(self._status_label)

        hint = QLabel(self.tr("Click a camera to select it • drag to orbit • scroll to zoom • "
                              "middle-drag to pan • WASD/RF to fly"))
        hint.setStyleSheet("color: rgba(255,255,255,0.5); font-size: 11px;")
        layout.addWidget(hint)

    def _on_load(self):
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Load Calibration File"), "",
            self.tr("Calibration files (*.xcp *.qca.txt);;Vicon (*.xcp);;Qualisys (*.qca.txt);;All files (*)"),
        )
        if not path:
            return
        try:
            self._cameras = load_calibration(path)
        except CalibrationError as e:
            QMessageBox.warning(self, self.tr("Load Failed"), str(e))
            return
        except Exception as e:
            QMessageBox.warning(self, self.tr("Load Failed"), self.tr(f"Could not load calibration: {e}"))
            return

        self._file_label.setText(os.path.basename(path))
        self._status_label.setText(self.tr("No camera selected"))
        self._gl_view.clearItems()
        self._cam_list.clear()
        self._updating_checks = True
        for cam in self._cameras:
            apex, corners = frustum_scene_points(cam)
            self._gl_view.addPickableItem(
                cam.device_id, "mesh", [apex] + corners,
                factory=lambda color, cam=cam: CameraFrustumItem(cam, color=color),
                normal_color=DEFAULT_FRUSTUM_COLOR,
                # The generic yellow-ish highlight default is too close to
                # this item's own orange to read as "different" -- use a
                # sharply-contrasting white instead.
                highlight_color=(1.0, 1.0, 1.0),
                # Orientation axes: a fixed companion, not itself clickable
                # or recolored, but shown/hidden together with the frustum.
                extra_factory=lambda cam=cam, origin=apex: AxesItem(
                    axisLength=_AXIS_LENGTH, origin=origin,
                    directions=(lab_to_scene(cam.right), lab_to_scene(cam.up), lab_to_scene(cam.forward)),
                ),
            )
            item = QListWidgetItem(
                f"{cam.device_id}  ({cam.camera_type}, {cam.category}, "
                f"f={cam.focal_length:.0f}, err={cam.image_error:.2f})"
            )
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, cam.device_id)
            self._cam_list.addItem(item)
        self._updating_checks = False
        for cb in self._category_checks.values():
            cb.setChecked(True)

    def _on_gl_picked(self, name):
        if self._updating_selection:
            return
        self._updating_selection = True
        if not name:
            self._cam_list.setCurrentRow(-1)
            self._status_label.setText(self.tr("No camera selected"))
        else:
            for i, cam in enumerate(self._cameras):
                if cam.device_id == name:
                    self._cam_list.setCurrentRow(i)
                    self._status_label.setText(self._camera_status_text(cam))
                    break
        self._updating_selection = False

    def _on_list_row_changed(self, row):
        if self._updating_selection:
            return
        self._updating_selection = True
        if 0 <= row < len(self._cameras):
            cam = self._cameras[row]
            self._gl_view.selectByName(cam.device_id)
            self._status_label.setText(self._camera_status_text(cam))
        else:
            self._gl_view.selectByName(None)
            self._status_label.setText(self.tr("No camera selected"))
        self._updating_selection = False

    def _camera_status_text(self, cam):
        return self.tr("Selected: {0}  ({1}, focal length={2:.0f}, image error={3:.2f})").format(
            cam.device_id, cam.camera_type, cam.focal_length, cam.image_error,
        )

    def _on_item_check_changed(self, item):
        if self._updating_checks:
            return
        device_id = item.data(Qt.ItemDataRole.UserRole)
        self._gl_view.setItemVisible(device_id, item.checkState() == Qt.CheckState.Checked)

    def _on_category_toggled(self, category, checked):
        """Bulk show/hide every camera of this category by driving each
        row's own checkbox -- the per-row checkbox stays the single source
        of truth for actual 3D visibility."""
        if self._updating_checks:
            return
        self._updating_checks = True
        for i, cam in enumerate(self._cameras):
            if cam.category != category:
                continue
            item = self._cam_list.item(i)
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self._gl_view.setItemVisible(cam.device_id, checked)
        self._updating_checks = False
