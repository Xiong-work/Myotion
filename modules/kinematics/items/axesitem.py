from ..mesh import Mesh
from ..geometry import Geometry
from ..materials.linematerial import LineMaterial


class AxesItem(Mesh):
    def __init__(
        self, axisLength=1, lineWidth=4, axisColors=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        origin=(0, 0, 0), directions=((1, 0, 0), (0, 1, 0), (0, 0, 1)),
    ):
        """origin: scene-space position the three axes are drawn from (mm) --
        lets one AxesItem represent a local frame elsewhere in the scene
        (e.g. a force plate's centre) instead of always sitting at [0,0,0].

        directions: 3 unit vectors (scene space) for the X/Y/Z lines --
        defaults to the standard identity axes; pass a plate's actual local
        frame (derived from its corner geometry) to draw its true orientation
        instead of assuming it's aligned with the lab/global axes."""
        ox, oy, oz = origin

        def _tip(direction):
            dx, dy, dz = direction
            return [ox + dx * axisLength, oy + dy * axisLength, oz + dz * axisLength]

        geo = Geometry()
        positionData = [
            [ox, oy, oz],
            _tip(directions[0]),
            [ox, oy, oz],
            _tip(directions[1]),
            [ox, oy, oz],
            _tip(directions[2]),
        ]
        colorData = [
            axisColors[0],
            axisColors[0],
            axisColors[1],
            axisColors[1],
            axisColors[2],
            axisColors[2],
        ]
        geo.addAttribute("vec3", "vertexPosition", positionData)
        geo.addAttribute("vec3", "vertexColor", colorData)
        geo.countVertices()
        mat = LineMaterial(
            {"lineWidth": lineWidth, "useVertexColors": True, "lineType": "segments"}
        )
        super().__init__(geo, mat)
