from math import pi

import numpy as np
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QWidget
from OpenGL.GL import *

from .camera import Camera
from .items import AxesItem, GridItem, PointItem, ForceVectorItem, ForceWireItem
from .base import Base
from .movmentrig import MovementRig
from .object3d import Object3D
from .renderer import Renderer


class BodyRender(Base):
    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.model = None
        self.point = None
        self._plate_geo = []       # ForceWireItem instances currently in scene
        self._plate_geo_added = False  # deferred first-paint flag

    def initializeGL(self) -> None:
        super().initializeGL()
        self.currentFrame = 0
        self._force_items = []  # ForceVectorItem list, one per active force plate
        self.renderer = Renderer(self)
        self.scene = Object3D()
        self.camera = Camera(aspectRatio=1, far=50000)
        self.camera.setPosition([0, 2000, 3000])
        self.camera.lookAt([0, 0, 0])
        self.rig = MovementRig(unitsPerSecond=100)
        self.rig.add(self.camera)

        axes = AxesItem(axisLength=500)
        grid = GridItem(size=5000, gridColor=[0.15, 0.15, 0.15], centerColor=[0.4, 0.4, 0.0])
        grid.rotateX(-pi / 2)

        self.scene.add(axes)
        self.scene.add(grid)

    def paintGL(self) -> None:
        super().paintGL()

        # One-time: add static force plate wireframes to the scene
        if not self._plate_geo_added and self.model is not None:
            for fp in getattr(self.model, "force_plates", []):
                if fp.corners is not None:
                    wire = ForceWireItem(fp.corners)
                    self.scene.add(wire)
                    self._plate_geo.append(wire)
            self._plate_geo_added = True

        if self.point in self.scene.children:
            self.scene.remove(self.point)
        self.point = self.getFrame()
        self.scene.add(self.point)

        # Update force-vector overlays (recreated each frame, same pattern as PointItem)
        for fv in self._force_items:
            if fv in self.scene.children:
                self.scene.remove(fv)
        self._force_items = []
        for fp in getattr(self.model, "force_plates", []):
            fv = self._sample_force(fp)
            if fv is not None:
                self.scene.add(fv)
                self._force_items.append(fv)

        self.renderer.render(self.scene, self.camera)
        self.rig.update(self.input, self.deltaTime)

    def _sample_force(self, fp):
        """Return a ForceVectorItem anchored at the plate centre, or None."""
        try:
            point_fs = self.model.point_fs if self.model.point_fs > 0 else 1
            ratio = fp.fs / point_fs
            idx = max(0, min(int(self.currentFrame * ratio), len(fp.Fz) - 1))
            fz = float(fp.Fz[idx])
            if abs(fz) < 10.0:  # skip near-zero vertical force (swing phase)
                return None
            fx = float(fp.Fx[idx])
            fy = float(fp.Fy[idx])
            # Anchor vector at plate centre in scene coordinates.
            # C3D lab frame (Xc, Yc, Zc) → scene (Xc, Zc, Yc), same as getFrame().
            # Force plates are at floor level (C3D_Z ≈ 0), so scene_Y ≈ 0 plus a
            # small offset to avoid z-fighting with the floor grid.
            if fp.corners is not None:
                c = fp.corners.mean(axis=0)  # (Xc_mean, Yc_mean, Zc_mean) in C3D mm
                origin = [float(c[0]), float(c[2]) + 2.0, float(c[1])]
            else:
                origin = None  # ForceVectorItem defaults to [0, 2, 0]
            return ForceVectorItem(fx, fy, fz, origin=origin)
        except Exception:
            return None

    def getFrame(self):
        cf = []
        for joint in self.model.realpoints:
            cf.append(
                [
                    self.model.realpoints[joint][self.currentFrame].xyz[0],
                    self.model.realpoints[joint][self.currentFrame].xyz[2],
                    self.model.realpoints[joint][self.currentFrame].xyz[1],
                ]
            )
        return PointItem(cf)

    def setModel(self, model):
        self.model = model
        self.currentFrame = 0
        # Clear stale plate wireframes so the next paintGL re-adds them for the new model
        if hasattr(self, "scene"):
            for wire in self._plate_geo:
                if wire in self.scene.children:
                    self.scene.remove(wire)
        self._plate_geo = []
        self._plate_geo_added = False

    def setFrame(self, frame):
        self.currentFrame = frame

    def paintEvent(self, event):
        """GL rendering (via super) then QPainter overlay for force plate labels."""
        super().paintEvent(event)
        if not hasattr(self, "camera") or self.model is None:
            return
        fps = getattr(self.model, "force_plates", [])
        if not fps:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor(0, 200, 200))
        font = QFont("Segoe UI", 9)
        font.setBold(True)
        painter.setFont(font)
        for fp in fps:
            if fp.corners is None:
                continue
            c = fp.corners.mean(axis=0)
            # Map C3D lab frame → scene coords (same as getFrame / ForceWireItem)
            scene_center = [float(c[0]), float(c[2]), float(c[1])]
            pos = self._project_to_screen(scene_center)
            if pos:
                painter.drawText(pos[0] + 6, pos[1], "Plate {}".format(fp.plate_id))
        painter.end()

    def _project_to_screen(self, scene_pos):
        """Project a 3D scene-space position to 2D screen pixel coordinates.

        Returns (x, y) or None when the point is outside the view frustum.
        """
        try:
            self.camera.updateViewMatrix()
            mvp = self.camera.projectionMatrix @ self.camera.viewMatrix
            p = np.array([scene_pos[0], scene_pos[1], scene_pos[2], 1.0])
            clip = mvp @ p
            if abs(clip[3]) < 1e-6 or clip[3] < 0:
                return None
            ndc = clip[:3] / clip[3]
            if not (-1.0 <= ndc[0] <= 1.0 and -1.0 <= ndc[1] <= 1.0):
                return None
            w, h = self.width(), self.height()
            sx = int((ndc[0] + 1.0) / 2.0 * w)
            sy = int((1.0 - ndc[1]) / 2.0 * h)
            return sx, sy
        except Exception:
            return None
