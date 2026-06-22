from ..mesh import Mesh
from ..geometry import Geometry
from ..materials.linematerial import LineMaterial

_WIRE_COLOR = [0.2, 0.8, 0.8]   # teal, matching the GRF vector item


class ForceWireItem(Mesh):
    """Static wireframe rectangle showing the physical outline of one force plate.

    Corners are supplied in C3D lab frame (mm) and remapped to scene coordinates
    using the same (X_c3d → X, Z_c3d → Y_up, Y_c3d → Z_depth) convention used
    throughout bodyrender.py.
    """

    def __init__(self, corners_c3d):
        """
        Args:
            corners_c3d: ndarray shape [4, 3] — four corner positions in C3D lab
                         frame (millimetres).  Order is assumed to be sequential
                         around the perimeter of the plate.
        """
        def s(p):
            """C3D lab frame → scene coordinates."""
            return [float(p[0]), float(p[2]), float(p[1])]

        c = [s(corners_c3d[i]) for i in range(4)]

        # Four edges as segment pairs (LineMaterial, lineType="segments")
        positions = [
            c[0], c[1],
            c[1], c[2],
            c[2], c[3],
            c[3], c[0],
        ]
        colors = [_WIRE_COLOR] * len(positions)

        geo = Geometry()
        geo.addAttribute("vec3", "vertexPosition", positions)
        geo.addAttribute("vec3", "vertexColor", colors)
        geo.countVertices()
        mat = LineMaterial({"lineWidth": 2, "useVertexColors": True, "lineType": "segments"})
        super().__init__(geo, mat)
