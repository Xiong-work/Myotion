"""widgets/playground_gl_view.py -- minimal reusable orbit/fly 3D viewport
for Playground tools, built on the same OpenGL engine (Renderer/Camera/
Object3D/MovementRig) already driving the kinematics 3D viewer
(modules/kinematics/bodyrender.py). WASD+drag+wheel navigation, plus
middle-drag panning, come for free from MovementRig, same as the kinematics
viewer.

Also provides simple click-to-select picking: addPickableItem() registers a
scene item alongside a few representative 3D points used for click hit-
testing, and selectByName()/itemPicked let a dialog sync selection with e.g.
a sidebar list in both directions. Hovering the mouse over a pickable (with
no button held) shows its name as a tooltip, using the same hit-testing.
"""

from math import pi

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QToolTip

from modules.kinematics.base import Base
from modules.kinematics.camera import Camera
from modules.kinematics.items import AxesItem, GridItem
from modules.kinematics.movmentrig import MovementRig
from modules.kinematics.object3d import Object3D
from modules.kinematics.renderer import Renderer

_CLICK_DRAG_THRESHOLD_SQ = 5 * 5   # px^2 -- press/release closer than this counts as a click
_POINT_PICK_THRESHOLD_SQ = 15 * 15  # px^2 -- max screen distance to a point-kind pickable


class PlaygroundGLView(Base):
    itemPicked = Signal(str)  # picked pickable's name, or "" when a click hits nothing

    def __init__(self, camera_far=50000, camera_position=(0, 2000, 3000),
                 grid_size=5000, axis_length=500, parent=None):
        super().__init__(parent)
        self._camera_far = camera_far
        self._camera_position = camera_position
        self._grid_size = grid_size
        self._axis_length = axis_length
        self._pending_items = []  # queued if addItem() is called before the GL context exists
        self._content_items = []  # items added via addItem/addPickableItem -- excludes axes/grid
        self._pickables = {}      # name -> {kind, world_points, factory, normal_color, highlight_color, item}
        self._selected_name = None
        self._press_pos = None

    def initializeGL(self):
        super().initializeGL()
        self.renderer = Renderer(self)
        self.scene = Object3D()
        aspect = self.width() / max(1, self.height())
        self.camera = Camera(aspectRatio=aspect, far=self._camera_far)
        self.camera.setPosition(list(self._camera_position))
        self.camera.lookAt([0, 0, 0])
        self.rig = MovementRig(unitsPerSecond=100)
        self.rig.add(self.camera)

        if self._axis_length:
            self.scene.add(AxesItem(axisLength=self._axis_length))
        if self._grid_size:
            grid = GridItem(size=self._grid_size, gridColor=[0.15, 0.15, 0.15], centerColor=[0.4, 0.4, 0.0])
            grid.rotateX(-pi / 2)
            self.scene.add(grid)

        for factory in self._pending_items:
            item = factory()
            self.scene.add(item)
            self._content_items.append(item)
        self._pending_items = []

    def addItem(self, factory):
        """Add a non-pickable scene item built by factory() -- a zero-arg
        callable that constructs and returns an Object3D. Building a Mesh
        does real OpenGL calls (VAO/buffer creation), which require this
        widget's GL context to be current; a factory (rather than an
        already-built item) lets addItem() guarantee that via
        makeCurrent()/doneCurrent() even when called from a button click
        handler outside the normal paintGL callback. Safe to call before the
        widget has painted for the first time -- the factory is queued and
        run once initializeGL() has made the context current."""
        if hasattr(self, "scene"):
            self.makeCurrent()
            item = factory()
            self.scene.add(item)
            self._content_items.append(item)
            self.doneCurrent()
        else:
            self._pending_items.append(factory)

    def addPickableItem(self, name, kind, world_points, factory,
                        normal_color=(1.0, 1.0, 1.0), highlight_color=(1.0, 0.85, 0.0),
                        extra_factory=None):
        """Add a scene item that can be clicked (in this view) or selected
        programmatically via selectByName() -- e.g. to sync with a sidebar
        list. Requires the GL context to already exist (call after the
        widget has been shown at least once; both Playground viewer dialogs
        show() the widget before loading any file).

        kind: "mesh" (world_points is a handful of representative points,
        e.g. bounding-box corners -- hit-tested as a 2D screen bbox) or
        "point" (world_points is a single 3D position -- hit-tested as
        nearest-within-threshold, and always wins over an overlapping mesh).
        factory: callable(color) -> Object3D, called with normal_color now
        and again with whichever color is needed whenever selection changes.
        extra_factory: optional zero-arg callable(-> Object3D) for a fixed
        (non-highlightable) companion item -- e.g. a camera's orientation
        axes -- that should show/hide together with this pickable via
        setItemVisible(), but isn't itself clickable or recolored.
        """
        self.makeCurrent()
        item = factory(normal_color)
        self.scene.add(item)
        self._content_items.append(item)
        extra_item = None
        if extra_factory is not None:
            extra_item = extra_factory()
            self.scene.add(extra_item)
            self._content_items.append(extra_item)
        self.doneCurrent()
        self._pickables[name] = {
            "kind": kind,
            "world_points": np.atleast_2d(np.asarray(world_points, dtype=float)),
            "factory": factory,
            "normal_color": normal_color,
            "highlight_color": highlight_color,
            "item": item,
            "extra_item": extra_item,
            "visible": True,
        }

    def setItemVisible(self, name, visible):
        """Show/hide a pickable (and its extra_factory companion, if any)
        without discarding it -- toggling a sidebar checkbox, say. Hidden
        pickables are skipped by click hit-testing."""
        entry = self._pickables.get(name)
        if entry is None or entry["visible"] == visible:
            return
        self.makeCurrent()
        for key in ("item", "extra_item"):
            obj = entry[key]
            if obj is None:
                continue
            in_scene = obj in self.scene.children
            if visible and not in_scene:
                self.scene.add(obj)
            elif not visible and in_scene:
                self.scene.remove(obj)
        entry["visible"] = visible
        self.doneCurrent()
        self.update()

    def clearItems(self):
        """Remove everything added via addItem()/addPickableItem() (axes/grid are kept)."""
        if hasattr(self, "scene"):
            for item in self._content_items:
                if item in self.scene.children:
                    self.scene.remove(item)
        self._content_items = []
        self._pending_items = []
        self._pickables = {}
        self._selected_name = None

    def selectByName(self, name):
        """Select (and highlight) the pickable called name, or clear the
        selection if name is None / not a registered pickable. Emits
        itemPicked either way, so a dialog can drive this from a sidebar
        list without special-casing "no match"."""
        if name == self._selected_name:
            return
        self.makeCurrent()
        if self._selected_name is not None and self._selected_name in self._pickables:
            self._rebuild_item(self._selected_name, self._pickables[self._selected_name]["normal_color"])
        if name is not None and name in self._pickables:
            self._rebuild_item(name, self._pickables[name]["highlight_color"])
            self._selected_name = name
        else:
            self._selected_name = None
        self.doneCurrent()
        self.update()
        self.itemPicked.emit(name or "")

    def _rebuild_item(self, name, color):
        entry = self._pickables[name]
        old_item = entry["item"]
        if old_item in self.scene.children:
            self.scene.remove(old_item)
        if old_item in self._content_items:
            self._content_items.remove(old_item)
        new_item = entry["factory"](color)
        if entry["visible"]:
            self.scene.add(new_item)
        self._content_items.append(new_item)
        entry["item"] = new_item

    def paintGL(self):
        super().paintGL()
        if hasattr(self, "renderer"):
            self.renderer.render(self.scene, self.camera)
            self.rig.update(self.input, self.deltaTime)

    # -- click-to-select picking (left click, no drag) ----------------------

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = (event.x(), event.y())

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton and self._press_pos is not None:
            dx = event.x() - self._press_pos[0]
            dy = event.y() - self._press_pos[1]
            if dx * dx + dy * dy < _CLICK_DRAG_THRESHOLD_SQ:
                self._handle_click_pick(event.x(), event.y())
        self._press_pos = None

    def _project_to_screen(self, pos):
        """3D world position -> (x, y) screen pixels, or None if behind the
        camera or outside the view frustum."""
        try:
            self.camera.updateViewMatrix()
            mvp = self.camera.projectionMatrix @ self.camera.viewMatrix
            clip = mvp @ np.array([pos[0], pos[1], pos[2], 1.0])
            if abs(clip[3]) < 1e-9 or clip[3] < 0:
                return None
            ndc = clip[:3] / clip[3]
            if not (-1.0 <= ndc[0] <= 1.0 and -1.0 <= ndc[1] <= 1.0):
                return None
            w, h = self.width(), self.height()
            return (ndc[0] + 1.0) / 2.0 * w, (1.0 - ndc[1]) / 2.0 * h
        except Exception:
            return None

    def _handle_click_pick(self, sx, sy):
        self.selectByName(self._find_pickable_at(sx, sy))

    def _find_pickable_at(self, sx, sy):
        """Return the name of the pickable at screen position (sx, sy), or
        None -- shared by click-to-select and hover-tooltip lookups."""
        best_name, best_score = None, None
        for name, entry in self._pickables.items():
            if not entry["visible"]:
                continue
            screen_pts = [p for p in (self._project_to_screen(pt) for pt in entry["world_points"]) if p is not None]
            if not screen_pts:
                continue
            xs = [p[0] for p in screen_pts]
            ys = [p[1] for p in screen_pts]

            if entry["kind"] == "point":
                d2 = (xs[0] - sx) ** 2 + (ys[0] - sy) ** 2
                if d2 < _POINT_PICK_THRESHOLD_SQ and (best_score is None or d2 < best_score):
                    best_score, best_name = d2, name
            else:  # "mesh": 2D bounding-box hit test; smallest-area match wins ties
                x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
                margin = 4
                if x0 - margin <= sx <= x1 + margin and y0 - margin <= sy <= y1 + margin:
                    # Offset well above any point-kind score so a marker
                    # always outranks an overlapping mesh.
                    score = 1e9 + max(1.0, (x1 - x0) * (y1 - y0))
                    if best_score is None or score < best_score:
                        best_score, best_name = score, name
        return best_name

    # -- hover tooltip --------------------------------------------------------

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if self.input.mousePressed or self.input.middlePressed:
            # Currently orbiting/panning -- skip hit-testing and don't leave a
            # stale tooltip up while dragging.
            QToolTip.hideText()
            return
        name = self._find_pickable_at(event.x(), event.y())
        if name:
            QToolTip.showText(event.globalPos(), name, self)
        else:
            QToolTip.hideText()
