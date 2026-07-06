"""widgets/playground/opensim_viewer_dialog.py -- load an OpenSim .osim
model (+ Geometry folder) and render its static default pose. Each body
segment and virtual marker is clickable (in the 3D view or the sidebar
lists) to show/highlight its name."""

import os

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFileDialog,
    QListWidget, QMessageBox, QSplitter, QWidget,
)

from modules.kinematics.items import PointItem, StlMeshItem
from modules.playground.opensim_model import load_model, OpenSimModelError
from widgets.playground_gl_view import PlaygroundGLView

# The kinematics engine's camera/movement-rig speeds were tuned for the
# app's usual millimetre-scale scenes (see PlaygroundGLView / bodyrender.py
# defaults); OpenSim models are metres. Scale up so navigation (esp. mouse
# wheel dolly, which moves by a fixed raw amount per notch) feels the same.
_METERS_TO_SCENE = 1000.0

# A handful of visually-distinct flat colors, cycled per body so adjoining
# segments (e.g. femur/tibia) are easy to tell apart in this engine's unlit
# SurfaceMaterial (see StlMeshItem).
_BODY_COLORS = [
    [0.80, 0.62, 0.46], [0.55, 0.70, 0.85], [0.70, 0.80, 0.55],
    [0.85, 0.70, 0.55], [0.65, 0.55, 0.80], [0.80, 0.55, 0.65],
]
_MARKER_COLOR = [1.0, 0.3, 0.3]
_HIGHLIGHT_COLOR = [1.0, 0.85, 0.0]

# Bundled fallback Geometry library (standard OpenSim body meshes, merged
# from several real projects' Geometry folders -- see myotion_resources/
# opensim_geometry/) used when a model's own folder doesn't ship a
# "Geometry" directory alongside it -- lets most models load with no
# folder-picker prompt at all. Missing/unusual meshes still show up as
# per-body warnings after loading, same as an explicitly-picked folder.
_BUNDLED_GEOMETRY_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "myotion_resources", "opensim_geometry",
)


def _aabb_corners(triangles):
    """8 corner points of triangles' (n,3,3) axis-aligned bounding box --
    cheap, good-enough geometry for click hit-testing a whole body mesh
    (see PlaygroundGLView.addPickableItem's "mesh" kind)."""
    pts = triangles.reshape(-1, 3)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    return [
        [lo[0], lo[1], lo[2]], [hi[0], lo[1], lo[2]], [lo[0], hi[1], lo[2]], [lo[0], lo[1], hi[2]],
        [hi[0], hi[1], lo[2]], [hi[0], lo[1], hi[2]], [lo[0], hi[1], hi[2]], [hi[0], hi[1], hi[2]],
    ]


class OpenSimViewerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("OpenSim Model Viewer"))
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMinimizeButtonHint
                             | Qt.WindowType.WindowMaximizeButtonHint)
        self.resize(1000, 640)

        self._model = None
        self._updating_selection = False  # guards against list<->3D selection feedback loops

        layout = QVBoxLayout(self)

        top_row = QHBoxLayout()
        self._load_btn = QPushButton(self.tr("Load Model (.osim)..."))
        self._load_btn.clicked.connect(self._on_load)
        top_row.addWidget(self._load_btn)
        self._file_label = QLabel(self.tr("No model loaded"))
        top_row.addWidget(self._file_label, 1)
        layout.addLayout(top_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter, 1)

        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.addWidget(QLabel(self.tr("Bodies")))
        self._body_list = QListWidget()
        self._body_list.currentRowChanged.connect(self._on_body_row_changed)
        side_layout.addWidget(self._body_list, 2)
        side_layout.addWidget(QLabel(self.tr("Markers")))
        self._marker_list = QListWidget()
        self._marker_list.currentRowChanged.connect(self._on_marker_row_changed)
        side_layout.addWidget(self._marker_list, 1)
        splitter.addWidget(side)

        # Default pose is roughly a 1.7m-tall standing figure centered near
        # the scene origin -- position the camera to frame that comfortably.
        self._gl_view = PlaygroundGLView(
            camera_far=20000, camera_position=(0, 1200, 4000),
            grid_size=4000, axis_length=500,
        )
        self._gl_view.itemPicked.connect(self._on_gl_picked)
        splitter.addWidget(self._gl_view)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

        self._status_label = QLabel(self.tr("Nothing selected"))
        layout.addWidget(self._status_label)

        hint = QLabel(self.tr("Click a body or marker to select it • drag to orbit • scroll to zoom • "
                              "middle-drag to pan • WASD/RF to fly. Default pose only -- not animated."))
        hint.setStyleSheet("color: rgba(255,255,255,0.5); font-size: 11px;")
        layout.addWidget(hint)

    def _on_load(self):
        osim_path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Load OpenSim Model"), "", self.tr("OpenSim model (*.osim);;All files (*)"),
        )
        if not osim_path:
            return

        default_geometry = os.path.join(os.path.dirname(osim_path), "Geometry")
        if os.path.isdir(default_geometry):
            geometry_dir = default_geometry
        elif os.path.isdir(_BUNDLED_GEOMETRY_DIR):
            geometry_dir = _BUNDLED_GEOMETRY_DIR
        else:
            geometry_dir = QFileDialog.getExistingDirectory(
                self, self.tr("Select Geometry Folder (mesh files for this model)"),
            )
            if not geometry_dir:
                return

        try:
            model = load_model(osim_path, geometry_dir=geometry_dir)
        except OpenSimModelError as e:
            QMessageBox.warning(self, self.tr("Load Failed"), str(e))
            return
        except Exception as e:
            QMessageBox.warning(self, self.tr("Load Failed"), self.tr(f"Could not load model: {e}"))
            return

        self._model = model
        self._file_label.setText(f"{model.name} ({os.path.basename(osim_path)})")
        self._status_label.setText(self.tr("Nothing selected"))

        self._gl_view.clearItems()
        self._body_list.clear()
        self._marker_list.clear()

        for i, body in enumerate(model.bodies):
            triangles = body.triangles * _METERS_TO_SCENE
            color = _BODY_COLORS[i % len(_BODY_COLORS)]
            self._gl_view.addPickableItem(
                f"body:{body.name}", "mesh", _aabb_corners(triangles),
                factory=lambda color, tri=triangles: StlMeshItem(tri, color=color),
                normal_color=color, highlight_color=_HIGHLIGHT_COLOR,
            )
            self._body_list.addItem(f"{body.name}  ({len(body.triangles)} tri)")

        for marker in model.markers:
            position = list(marker.position * _METERS_TO_SCENE)
            self._gl_view.addPickableItem(
                f"marker:{marker.name}", "point", [position],
                factory=lambda color, pos=position: PointItem([pos], [color]),
                normal_color=_MARKER_COLOR, highlight_color=_HIGHLIGHT_COLOR,
            )
            self._marker_list.addItem(marker.name)

        if model.warnings:
            QMessageBox.information(
                self, self.tr("Loaded with Warnings"),
                self.tr("Model loaded, but some parts were skipped:\n\n") + "\n".join(model.warnings[:20]),
            )

    def _on_gl_picked(self, name):
        if self._updating_selection:
            return
        self._updating_selection = True
        if not name:
            self._body_list.setCurrentRow(-1)
            self._marker_list.setCurrentRow(-1)
            self._status_label.setText(self.tr("Nothing selected"))
        elif name.startswith("body:"):
            body_name = name[len("body:"):]
            self._marker_list.setCurrentRow(-1)
            for i, body in enumerate(self._model.bodies):
                if body.name == body_name:
                    self._body_list.setCurrentRow(i)
                    break
            self._status_label.setText(self.tr("Selected body: {0}").format(body_name))
        elif name.startswith("marker:"):
            marker_name = name[len("marker:"):]
            self._body_list.setCurrentRow(-1)
            for i, marker in enumerate(self._model.markers):
                if marker.name == marker_name:
                    self._marker_list.setCurrentRow(i)
                    break
            self._status_label.setText(self.tr("Selected marker: {0}").format(marker_name))
        self._updating_selection = False

    def _on_body_row_changed(self, row):
        if self._updating_selection:
            return
        self._updating_selection = True
        if self._model is not None and 0 <= row < len(self._model.bodies):
            self._marker_list.setCurrentRow(-1)
            body = self._model.bodies[row]
            self._gl_view.selectByName(f"body:{body.name}")
            self._status_label.setText(self.tr("Selected body: {0}").format(body.name))
        self._updating_selection = False

    def _on_marker_row_changed(self, row):
        if self._updating_selection:
            return
        self._updating_selection = True
        if self._model is not None and 0 <= row < len(self._model.markers):
            self._body_list.setCurrentRow(-1)
            marker = self._model.markers[row]
            self._gl_view.selectByName(f"marker:{marker.name}")
            self._status_label.setText(self.tr("Selected marker: {0}").format(marker.name))
        self._updating_selection = False
