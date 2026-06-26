from ..mesh import Mesh
from ..geometry import Geometry
from ..materials.pointmaterial import PointMaterial


class PointItem(Mesh):
    def __init__(self, p=None, colors=None):
        self.geo = Geometry()
        self.geo.addAttribute("vec3", "vertexPosition", p)
        if colors is not None:
            self.geo.addAttribute("vec3", "vertexColor", colors)
        self.geo.countVertices()
        if colors is not None:
            mat = PointMaterial({"useVertexColors": True})
        else:
            mat = PointMaterial({"baseColor": [1, 1, 1]})
        super().__init__(self.geo, mat)
