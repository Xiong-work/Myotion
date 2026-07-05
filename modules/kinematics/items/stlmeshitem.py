from ..mesh import Mesh
from ..geometry import Geometry
from ..materials.surfacematerial import SurfaceMaterial

_DEFAULT_COLOR = [0.78, 0.74, 0.68]  # bone-ish neutral tan


class StlMeshItem(Mesh):
    """Solid triangle mesh from an already-loaded, already-positioned STL
    (see modules/playground/opensim_model.py's StlMesh) -- one instance per
    OpenSim body. This engine's SurfaceMaterial is unlit (flat/vertex color
    only, see BasicMaterial's shader), so per-triangle shading comes entirely
    from the supplied color, not from vertex normals.
    """

    def __init__(self, triangles, color=_DEFAULT_COLOR):
        """triangles: (n_triangles, 3, 3) array-like -- n_triangles rows of
        3 vertices (already in the coordinate frame to render in), each a
        (x, y, z) position."""
        positions = [list(vertex) for tri in triangles for vertex in tri]
        colors = [color] * len(positions)

        geo = Geometry()
        geo.addAttribute("vec3", "vertexPosition", positions)
        geo.addAttribute("vec3", "vertexColor", colors)
        geo.countVertices()
        mat = SurfaceMaterial({"useVertexColors": True, "doubleSide": True})
        super().__init__(geo, mat)
