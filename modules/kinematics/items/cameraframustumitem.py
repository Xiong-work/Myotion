from ..mesh import Mesh
from ..geometry import Geometry
from ..materials.linematerial import LineMaterial

DEFAULT_FRUSTUM_COLOR = [1.0, 0.65, 0.0]  # orange, matches the app's marker/event accent color


def lab_to_scene(p):
    """Vicon .xcp positions/vectors are in the same lab frame (Z-up) as C3D,
    so the same (X_lab, Z_lab, Y_lab) -> scene (X, Y_up, Z) remap used
    throughout bodyrender.py / ForceWireItem is applied here for a
    consistent-looking scene. Valid for both points and free vectors (pure
    axis permutation, no translation)."""
    return [float(p[0]), float(p[2]), float(p[1])]


def frustum_scene_points(camera):
    """(apex, [4 base corners]) for camera, remapped to scene coordinates --
    shared by CameraFrustumItem and callers that need the same points for
    click hit-testing (see widgets/playground/camera_calib_dialog.py)."""
    apex_raw, corners_raw = camera.frustum_corners()
    return lab_to_scene(apex_raw), [lab_to_scene(c) for c in corners_raw]


class CameraFrustumItem(Mesh):
    """Wireframe pyramid (apex + 4 base edges + 4 side edges) representing
    one calibrated camera's position/orientation -- built from a
    modules/playground/camera_calib.Camera via frustum_scene_points()."""

    def __init__(self, camera, color=DEFAULT_FRUSTUM_COLOR):
        apex, corners = frustum_scene_points(camera)

        positions = []
        # 4 side edges: apex -> each base corner
        for c in corners:
            positions += [apex, c]
        # 4 base edges: corner[i] -> corner[i+1]
        for i in range(4):
            positions += [corners[i], corners[(i + 1) % 4]]

        colors = [color] * len(positions)

        geo = Geometry()
        geo.addAttribute("vec3", "vertexPosition", positions)
        geo.addAttribute("vec3", "vertexColor", colors)
        geo.countVertices()
        mat = LineMaterial({"lineWidth": 2, "useVertexColors": True, "lineType": "segments"})
        super().__init__(geo, mat)
