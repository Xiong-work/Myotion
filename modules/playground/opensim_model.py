"""Parse an OpenSim .osim model file plus its Geometry folder and compute
each body's mesh triangles positioned at the model's *default pose* (all
joint coordinates at their default_value) in ground (world) coordinates.

Scope / known limitation: joint kinematics here assumes each SpatialTransform
rotation coordinate is at/near 0 -- true for essentially every OpenSim
model's neutral/calibration pose (confirmed against the Body25 reference
model, where every rotational default_value is ~0 and the one meaningful
non-zero default is a translation, pelvis_ty). Composing the 3 rotation axes
in listed order is exact when those coordinates are exactly 0 (rotation is
the identity regardless of axis/order); it is only an approximation for a
model whose *default* pose has genuinely large rotational coordinates. This
loader does not animate over a .mot/IK trajectory -- only the static default
pose is rendered.

.osim is plain XML (OpenSimDocument format), parsed with the stdlib parser
-- no new dependency. Meshes are read from .stl where available (a small
local STL reader), falling back to a body's .vtp (VTK PolyData XML) via a
small local reader supporting the common ASCII-encoded-DataArray case that
OpenSim's distributed Geometry folders use -- this avoids a VTK dependency
for either format.
"""

import os
import struct
import xml.etree.ElementTree as ET

import numpy as np

_GROUND_NAME = "ground"


class OpenSimModelError(Exception):
    pass


class BodyMesh:
    def __init__(self, name, triangles):
        self.name = name
        self.triangles = triangles  # (n_triangles, 3, 3) ndarray, ground frame, meters


class Marker:
    def __init__(self, name, position):
        self.name = name
        self.position = position  # (3,) ndarray, ground frame, meters


class Model:
    def __init__(self, name, bodies, markers, warnings, missing_mesh_count=0):
        self.name = name
        self.bodies = bodies      # list[BodyMesh]
        self.markers = markers    # list[Marker]
        self.warnings = warnings  # list[str] -- missing meshes, unhandled joint types, etc.
        self.missing_mesh_count = missing_mesh_count  # meshes not found/unreadable in geometry_dir --
                                                       # lets callers compare candidate Geometry folders


# ---------------------------------------------------------------------------
# STL loading
# ---------------------------------------------------------------------------

def load_stl_triangles(path):
    """Return (n_triangles, 3, 3) ndarray of vertex positions from a binary
    or ASCII STL file."""
    with open(path, "rb") as f:
        header = f.read(80)
        rest = f.read()

    # Binary STL: 80-byte header (not required to say anything in particular)
    # + uint32 triangle count + 50 bytes/triangle. ASCII STL starts with the
    # literal text "solid" and has no fixed-size binary body -- use the
    # byte-count check (binary's declared size must match the file exactly)
    # to disambiguate rather than trusting the header text alone (some
    # exporters write "solid ..." as the first 80 bytes of a binary file).
    if len(rest) >= 4:
        n_tri = struct.unpack("<I", rest[0:4])[0]
        expected_len = 4 + n_tri * 50
        if len(rest) == expected_len:
            return _parse_binary_stl(rest, n_tri)

    return _parse_ascii_stl(header + rest)


def _parse_binary_stl(data, n_tri):
    triangles = np.empty((n_tri, 3, 3), dtype=np.float64)
    offset = 4
    for i in range(n_tri):
        # 12 bytes normal (skipped) + 3x12 bytes vertices + 2 bytes attribute
        vertex_bytes = data[offset + 12: offset + 12 + 36]
        verts = struct.unpack("<9f", vertex_bytes)
        triangles[i] = np.array(verts, dtype=np.float64).reshape(3, 3)
        offset += 50
    return triangles


def _parse_ascii_stl(data):
    text = data.decode("ascii", errors="ignore")
    verts = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("vertex"):
            parts = line.split()[1:4]
            verts.append([float(v) for v in parts])
    if len(verts) % 3 != 0:
        raise OpenSimModelError(f"Malformed ASCII STL: vertex count {len(verts)} not divisible by 3.")
    return np.array(verts, dtype=np.float64).reshape(-1, 3, 3)


def load_vtp_triangles(path):
    """Return (n_triangles, 3, 3) ndarray of vertex positions from a VTK
    PolyData (.vtp) mesh. Only ASCII-encoded <DataArray> content is
    supported (the case used by OpenSim's distributed Geometry folders);
    base64/binary-appended VTP raises OpenSimModelError rather than
    silently mis-parsing."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        raise OpenSimModelError(f"Failed to parse VTP file (not valid XML): {path}. {e}") from e

    piece = root.find("./PolyData/Piece")
    if piece is None:
        raise OpenSimModelError(f"No <PolyData><Piece> found in VTP file: {path}")

    points_arr = piece.find("./Points/DataArray")
    polys_el = piece.find("Polys")
    if points_arr is None or polys_el is None:
        raise OpenSimModelError(f"VTP file missing Points/Polys data: {path}")
    if points_arr.get("format", "ascii") != "ascii":
        raise OpenSimModelError(f"Unsupported VTP encoding (only ascii DataArrays are supported): {path}")

    points = np.array([float(v) for v in points_arr.text.split()], dtype=np.float64).reshape(-1, 3)

    connectivity_arr = polys_el.find("DataArray[@Name='connectivity']")
    offsets_arr = polys_el.find("DataArray[@Name='offsets']")
    if connectivity_arr is None or offsets_arr is None:
        raise OpenSimModelError(f"VTP <Polys> missing connectivity/offsets: {path}")
    connectivity = [int(v) for v in connectivity_arr.text.split()]
    offsets = [int(v) for v in offsets_arr.text.split()]

    triangles = []
    start = 0
    for end in offsets:
        face = connectivity[start:end]
        start = end
        # Fan-triangulate -- a plain triangle (the common OpenSim case) is
        # just the one fan triangle (0, 1, 2).
        for i in range(1, len(face) - 1):
            triangles.append([points[face[0]], points[face[i]], points[face[i + 1]]])

    if not triangles:
        raise OpenSimModelError(f"VTP file has no polygons: {path}")
    return np.array(triangles, dtype=np.float64)


# ---------------------------------------------------------------------------
# Small XML/geometry helpers
# ---------------------------------------------------------------------------

def _text(elem, tag, default=None):
    child = elem.find(tag)
    if child is None or child.text is None:
        return default
    return child.text.strip()

def _vec3(elem, tag, default=(0.0, 0.0, 0.0)):
    s = _text(elem, tag)
    if s is None or s == "":
        return np.array(default, dtype=float)
    return np.array([float(v) for v in s.split()], dtype=float)


def _body_fixed_xyz(rx, ry, rz):
    """OpenSim PhysicalOffsetFrame <orientation> convention: body-fixed
    (intrinsic) X-Y-Z Euler angles, i.e. R = Rx(rx) @ Ry(ry) @ Rz(rz)."""
    cx, sx = np.cos(rx), np.sin(rx)
    cy, sy = np.cos(ry), np.sin(ry)
    cz, sz = np.cos(rz), np.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rx @ Ry @ Rz


def _axis_angle_rotation(axis, angle):
    """Rodrigues' rotation formula about an arbitrary unit axis."""
    axis = np.asarray(axis, dtype=float)
    norm = np.linalg.norm(axis)
    if norm < 1e-12 or abs(angle) < 1e-12:
        return np.eye(3)
    axis = axis / norm
    K = np.array([
        [0, -axis[2], axis[1]],
        [axis[2], 0, -axis[0]],
        [-axis[1], axis[0], 0],
    ])
    return np.eye(3) + np.sin(angle) * K + (1 - np.cos(angle)) * (K @ K)


def _homogeneous(R, t):
    M = np.eye(4)
    M[:3, :3] = R
    M[:3, 3] = t
    return M


def _strip_body_path(path):
    """'/bodyset/femur_r' -> 'femur_r'; '/ground' -> 'ground'."""
    if path is None:
        return None
    name = path.rsplit("/", 1)[-1]
    return _GROUND_NAME if name == "ground" else name


# ---------------------------------------------------------------------------
# Model parsing
# ---------------------------------------------------------------------------

def _parse_mesh_elements(attached_geometry_el, local_translation, local_rotation, meshes_out):
    if attached_geometry_el is None:
        return
    for mesh_el in attached_geometry_el.findall("Mesh"):
        mesh_file = _text(mesh_el, "mesh_file")
        if not mesh_file:
            continue
        scale = _vec3(mesh_el, "scale_factors", (1.0, 1.0, 1.0))
        meshes_out.append((mesh_file, scale, local_translation, local_rotation))


def _parse_bodies(bodyset_el):
    """Returns {body_name: [(mesh_file, scale_factors, local_translation, local_rotation), ...]}.

    Most bodies attach their visual meshes directly under
    <Body><attached_geometry>, at the body's own origin (identity local
    transform). Some models (e.g. a multi-piece torso built from several
    vertebra meshes) instead own extra <components><PhysicalOffsetFrame>
    sub-frames, each at its own translation/orientation from the body
    origin, with the mesh(es) attached to *that* frame -- both forms are
    handled here so a body's rendered mesh isn't silently incomplete.
    """
    bodies = {}
    objects_el = bodyset_el.find("objects")
    if objects_el is None:
        return bodies
    identity_t, identity_r = np.zeros(3), np.eye(3)
    for body_el in objects_el.findall("Body"):
        name = body_el.get("name")
        meshes = []
        _parse_mesh_elements(body_el.find("attached_geometry"), identity_t, identity_r, meshes)

        components_el = body_el.find("components")
        if components_el is not None:
            for pof in components_el.findall("PhysicalOffsetFrame"):
                translation = _vec3(pof, "translation")
                rotation = _body_fixed_xyz(*_vec3(pof, "orientation"))
                _parse_mesh_elements(pof.find("attached_geometry"), translation, rotation, meshes)

        bodies[name] = meshes
    return bodies


def _parse_offset_frames(frames_el):
    """Returns {frame_name: (translation, rotation, parent_body_name)}."""
    frames = {}
    if frames_el is None:
        return frames
    for pof in frames_el.findall("PhysicalOffsetFrame"):
        name = pof.get("name")
        translation = _vec3(pof, "translation")
        orientation = _vec3(pof, "orientation")
        rotation = _body_fixed_xyz(*orientation)
        parent_body = _strip_body_path(_text(pof, "socket_parent"))
        frames[name] = (translation, rotation, parent_body)
    return frames


def _parse_spatial_transform(st_el):
    """Returns a list of 6 dicts (rotation1-3 then translation1-3), each
    {axis, coordinate, scale, offset}, or None entries for unspecified axes
    (a TransformAxis with no <coordinates> is a fixed, non-moving axis)."""
    if st_el is None:
        return None
    axes = []
    for axis_el in st_el.findall("TransformAxis"):
        coord = _text(axis_el, "coordinates")
        axis_vec = _vec3(axis_el, "axis", (0, 0, 0))
        func = axis_el.find("LinearFunction")
        if func is not None:
            coeffs = _text(func, "coefficients", "1 0")
            scale, offset = (float(v) for v in coeffs.split())
        else:
            scale, offset = 1.0, 0.0
        axes.append({"axis": axis_vec, "coordinate": coord, "scale": scale, "offset": offset})
    return axes


def _parse_joints(jointset_el):
    """Returns a list of dicts: name, joint_type, parent_frame, child_frame,
    frames (dict), coordinates (dict name -> default_value), spatial_transform."""
    joints = []
    objects_el = jointset_el.find("objects")
    if objects_el is None:
        return joints
    for joint_el in objects_el:
        coordinates = {}
        coords_el = joint_el.find("coordinates")
        if coords_el is not None:
            for coord_el in coords_el.findall("Coordinate"):
                cname = coord_el.get("name")
                default_value = float(_text(coord_el, "default_value", "0"))
                coordinates[cname] = default_value

        joints.append({
            "name": joint_el.get("name"),
            "joint_type": joint_el.tag,
            "parent_frame": _text(joint_el, "socket_parent_frame"),
            "child_frame": _text(joint_el, "socket_child_frame"),
            "frames": _parse_offset_frames(joint_el.find("frames")),
            "coordinates": coordinates,
            "spatial_transform": _parse_spatial_transform(joint_el.find("SpatialTransform")),
        })
    return joints


def _parse_markers(model_el):
    """Returns a list of (name, body_name, location) for each virtual/
    anatomical marker in the model's MarkerSet, if any."""
    markers = []
    markerset_el = model_el.find("MarkerSet")
    if markerset_el is None:
        return markers
    objects_el = markerset_el.find("objects")
    if objects_el is None:
        return markers
    for marker_el in objects_el.findall("Marker"):
        name = marker_el.get("name")
        body_name = _strip_body_path(_text(marker_el, "socket_parent_frame"))
        location = _vec3(marker_el, "location")
        if body_name is not None:
            markers.append((name, body_name, location))
    return markers


def _joint_coordinate_transform(joint):
    """The rigid transform contributed by a joint's coordinates at their
    default value (identity for a zero-DOF joint like WeldJoint, or one
    with no SpatialTransform)."""
    st = joint["spatial_transform"]
    if not st:
        return np.eye(4)

    def value_of(axis_def):
        coord = axis_def["coordinate"]
        q = joint["coordinates"].get(coord, 0.0) if coord else 0.0
        return axis_def["scale"] * q + axis_def["offset"]

    R = np.eye(3)
    for axis_def in st[:3]:
        R = R @ _axis_angle_rotation(axis_def["axis"], value_of(axis_def))

    t = np.zeros(3)
    for axis_def in st[3:6]:
        axis = np.asarray(axis_def["axis"], dtype=float)
        norm = np.linalg.norm(axis)
        if norm > 1e-12:
            t = t + (axis / norm) * value_of(axis_def)

    return _homogeneous(R, t)


def load_model(osim_path, geometry_dir=None):
    """Parse osim_path and return a Model with each body's mesh triangles
    already transformed into ground (world) coordinates at the model's
    default pose. Missing mesh files are skipped with a warning rather than
    failing the whole load."""
    try:
        tree = ET.parse(osim_path)
    except ET.ParseError as e:
        raise OpenSimModelError(f"Failed to parse OSIM file (not valid XML): {osim_path}. {e}") from e

    root = tree.getroot()
    model_el = root.find("Model")
    if model_el is None:
        raise OpenSimModelError(f"Not an OpenSim model file (no <Model> element): {osim_path}")
    model_name = model_el.get("name", os.path.splitext(os.path.basename(osim_path))[0])

    bodyset_el = model_el.find("BodySet")
    jointset_el = model_el.find("JointSet")
    if bodyset_el is None or jointset_el is None:
        raise OpenSimModelError(f"OSIM file is missing BodySet/JointSet: {osim_path}")

    body_meshes = _parse_bodies(bodyset_el)
    joints = _parse_joints(jointset_el)

    if geometry_dir is None:
        geometry_dir = os.path.join(os.path.dirname(osim_path), "Geometry")

    warnings = []

    # Build child_body -> joint lookup (each body has exactly one joint to its parent).
    joint_by_child = {}
    for joint in joints:
        frames = joint["frames"]
        child_frame = frames.get(joint["child_frame"])
        if child_frame is None:
            warnings.append(f"Joint '{joint['name']}': child frame '{joint['child_frame']}' not found; skipped.")
            continue
        child_body = child_frame[2]
        joint_by_child[child_body] = joint

    world_transform_cache = {_GROUND_NAME: np.eye(4)}

    def world_transform(body_name, _visiting=()):
        if body_name in world_transform_cache:
            return world_transform_cache[body_name]
        if body_name in _visiting:
            raise OpenSimModelError(f"Cyclic joint chain detected involving body '{body_name}'.")

        joint = joint_by_child.get(body_name)
        if joint is None:
            warnings.append(f"Body '{body_name}' has no joint connecting it to the tree; treated as fixed to ground.")
            world_transform_cache[body_name] = np.eye(4)
            return world_transform_cache[body_name]

        frames = joint["frames"]
        parent_translation, parent_rotation, parent_body = frames[joint["parent_frame"]]
        child_translation, child_rotation, _ = frames[joint["child_frame"]]

        parent_world = world_transform(parent_body, _visiting + (body_name,))
        parent_offset = _homogeneous(parent_rotation, parent_translation)
        child_offset = _homogeneous(child_rotation, child_translation)
        joint_coord_transform = _joint_coordinate_transform(joint)

        # body_world = parent_world @ parent_offset @ joint_transform @ inverse(child_offset)
        M = parent_world @ parent_offset @ joint_coord_transform @ np.linalg.inv(child_offset)
        world_transform_cache[body_name] = M
        return M

    bodies_out = []
    missing_mesh_count = 0
    for body_name, meshes in body_meshes.items():
        if not meshes:
            continue
        M = world_transform(body_name)
        R, t = M[:3, :3], M[:3, 3]

        all_triangles = []
        for mesh_file, scale, local_t, local_r in meshes:
            stl_path = os.path.join(geometry_dir, os.path.splitext(mesh_file)[0] + ".stl")
            vtp_path = os.path.join(geometry_dir, mesh_file)
            if os.path.isfile(stl_path):
                mesh_path = stl_path
            elif os.path.isfile(vtp_path):
                mesh_path = vtp_path
            else:
                warnings.append(f"Body '{body_name}': mesh file not found, skipped: {stl_path}")
                missing_mesh_count += 1
                continue
            try:
                triangles = (load_vtp_triangles(mesh_path) if mesh_path.lower().endswith(".vtp")
                             else load_stl_triangles(mesh_path))
            except Exception as e:
                warnings.append(f"Body '{body_name}': failed to read {mesh_path}: {e}")
                missing_mesh_count += 1
                continue
            triangles = triangles * scale  # per-axis scale in the mesh's own local frame
            triangles = triangles @ local_r.T + local_t  # local offset frame -> body frame
            triangles_world = triangles @ R.T + t         # body frame -> ground
            all_triangles.append(triangles_world)

        if all_triangles:
            bodies_out.append(BodyMesh(body_name, np.concatenate(all_triangles, axis=0)))

    if not bodies_out:
        warnings.append("No renderable body meshes found (check the Geometry folder path).")

    markers_out = []
    for marker_name, body_name, location in _parse_markers(model_el):
        M = world_transform_cache.get(body_name)
        if M is None:
            try:
                M = world_transform(body_name)
            except OpenSimModelError:
                warnings.append(f"Marker '{marker_name}': body '{body_name}' not found; skipped.")
                continue
        R, t = M[:3, :3], M[:3, 3]
        markers_out.append(Marker(marker_name, R @ location + t))

    return Model(model_name, bodies_out, markers_out, warnings, missing_mesh_count)
