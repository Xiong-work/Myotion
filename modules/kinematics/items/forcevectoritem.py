from ..mesh import Mesh
from ..geometry import Geometry
from ..materials.linematerial import LineMaterial

# Scene units appear to be millimetres (camera at y=2000, grid 5000 wide).
# 0.3 means 1000 N → 300 mm arrow — visible but not overwhelming.
_GRF_SCALE = 0.3


class ForceVectorItem(Mesh):
    """Orange GRF line from the force plate centre to the resultant force endpoint.

    Coordinate mapping matches bodyrender.getFrame():
        scene X = C3D X direction
        scene Y = C3D Z direction (vertical, up)
        scene Z = C3D Y direction

    Args:
        fx, fy, fz: force components in C3D lab frame (N)
        origin: base position in *scene* coordinates (mm).  Defaults to a small
                Y offset above the floor so the line does not z-fight the grid.
    """

    def __init__(self, fx: float, fy: float, fz: float, origin=None):
        if origin is None:
            origin = [0.0, 2.0, 0.0]
        # Negate only Fz: C3D stores vertical force as body-on-plate (downward),
        # so negating gives the GRF (plate-on-body, upward). Fx/Fy sign is already correct.
        tip = [
            origin[0] + fx * _GRF_SCALE,
            origin[1] - fz * _GRF_SCALE,
            origin[2] + fy * _GRF_SCALE,
        ]
        color = [1.0, 0.5, 0.0]  # orange
        geo = Geometry()
        geo.addAttribute("vec3", "vertexPosition", [list(origin), tip])
        geo.addAttribute("vec3", "vertexColor", [color, color])
        geo.countVertices()
        mat = LineMaterial({"lineWidth": 3, "useVertexColors": True, "lineType": "segments"})
        super().__init__(geo, mat)
