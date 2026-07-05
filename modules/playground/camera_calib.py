"""Parse a camera-calibration file (Vicon .xcp or Qualisys .qca.txt) and
produce enough per-camera geometry (position, orientation, a simple
view-frustum) to plot the camera rig in 3D space.

Both formats are plain XML (confirmed against real Nexus- and QTM-exported
files), so this uses only the stdlib parser -- no new dependency.
"""

import xml.etree.ElementTree as ET

import numpy as np

# Default frustum visualization size (mm) -- these files are typically in mm
# (Vicon lab-frame convention); this is a fixed viz size, not derived from
# the calibration itself, since a true-to-scale frustum would need the
# capture volume extent to look reasonable at any rig scale.
_FRUSTUM_DEPTH = 300.0


class CalibrationError(Exception):
    pass


class Camera:
    def __init__(self, device_id, camera_type, sensor_size, position, rotation,
                 focal_length, image_error, frame, category="Optical"):
        self.device_id = device_id
        self.camera_type = camera_type
        self.category = category        # "Optical" or "Video" -- for the viewer's type filter
        self.sensor_size = sensor_size  # (width_px, height_px)
        self.position = position        # (3,) ndarray, mm, lab/world frame
        self.rotation = rotation        # (3,3) ndarray, camera axes expressed in world frame
        self.focal_length = focal_length
        self.image_error = image_error
        self.frame = frame

    @property
    def forward(self):
        """Unit vector the camera points along, in world coordinates.

        Assumes the Vicon convention that the camera's local +Z axis (third
        rotation-matrix column) is the viewing direction. This is a
        visualization aid, not a precision-critical computation -- flip the
        sign here if a rendered rig looks like it's facing backwards for a
        given system.
        """
        return self.rotation[:, 2]

    @property
    def up(self):
        return self.rotation[:, 1]

    @property
    def right(self):
        return self.rotation[:, 0]

    def frustum_corners(self, depth=_FRUSTUM_DEPTH):
        """Return (apex, [4 base corners]) for a simple pyramid frustum,
        sized by the camera's sensor aspect ratio and a fixed viz depth."""
        w, h = self.sensor_size
        aspect = (h / w) if w else 1.0
        half_w = depth * 0.35
        half_h = half_w * aspect

        apex = self.position
        center = self.position + self.forward * depth
        right, up = self.right, self.up
        corners = [
            center + right * half_w + up * half_h,
            center - right * half_w + up * half_h,
            center - right * half_w - up * half_h,
            center + right * half_w - up * half_h,
        ]
        return apex, corners


def _parse_quaternion(qstr):
    """Vicon ORIENTATION="x y z w" -> 3x3 rotation matrix."""
    x, y, z, w = (float(v) for v in qstr.split())
    n = x * x + y * y + z * z + w * w
    if n < 1e-12:
        raise CalibrationError(f"Degenerate quaternion: {qstr}")
    s = 2.0 / n
    return np.array([
        [1 - s * (y * y + z * z), s * (x * y - w * z), s * (x * z + w * y)],
        [s * (x * y + w * z), 1 - s * (x * x + z * z), s * (y * z - w * x)],
        [s * (x * z - w * y), s * (y * z + w * x), 1 - s * (x * x + y * y)],
    ])


def load_cameras(xcp_path, frame=None):
    """Parse an .xcp file and return a list of Camera objects, one per
    <Camera> that has at least one <KeyFrame>. If frame is given, use the
    KeyFrame with that FRAME value when a camera has more than one
    (multi-keyframe calibration files); otherwise use each camera's first
    KeyFrame.
    """
    try:
        tree = ET.parse(xcp_path)
    except ET.ParseError as e:
        raise CalibrationError(f"Failed to parse XCP file (not valid XML): {xcp_path}. {e}") from e

    root = tree.getroot()
    cameras = []
    for cam_el in root.findall("Camera"):
        keyframes = cam_el.find("KeyFrames")
        if keyframes is None:
            continue
        kf_els = keyframes.findall("KeyFrame")
        if not kf_els:
            continue

        kf_el = kf_els[0]
        if frame is not None:
            for candidate in kf_els:
                if candidate.get("FRAME") == str(frame):
                    kf_el = candidate
                    break

        try:
            position = np.array([float(v) for v in kf_el.get("POSITION").split()])
            rotation = _parse_quaternion(kf_el.get("ORIENTATION"))
            focal_length = float(kf_el.get("FOCAL_LENGTH", "0"))
            image_error = float(kf_el.get("IMAGE_ERROR", "0"))
            sensor_size_str = cam_el.get("SENSOR_SIZE", "1 1")
            sensor_size = tuple(float(v) for v in sensor_size_str.split())
        except (AttributeError, ValueError) as e:
            raise CalibrationError(
                f"Camera '{cam_el.get('DEVICEID', '?')}' in {xcp_path} is missing expected "
                f"POSITION/ORIENTATION/FOCAL_LENGTH attributes."
            ) from e

        # ISDV ("is digital video") is Vicon's own optical-vs-video flag:
        # "0" for tracking (Vantage/Vero) cameras, nonzero for Vue video-
        # reference cameras -- confirmed against a real multi-camera rig
        # (Vantage5V/VeroV22 -> ISDV=0, Vue_VIDEO -> ISDV=2).
        category = "Optical" if cam_el.get("ISDV", "0") == "0" else "Video"

        cameras.append(Camera(
            device_id=cam_el.get("DEVICEID", "?"),
            camera_type=cam_el.get("TYPE", cam_el.get("DISPLAY_TYPE", "?")),
            category=category,
            sensor_size=sensor_size,
            position=position,
            rotation=rotation,
            focal_length=focal_length,
            image_error=image_error,
            frame=kf_el.get("FRAME"),
        ))

    if not cameras:
        raise CalibrationError(f"No cameras with calibrated KeyFrames found in {xcp_path}.")
    return cameras


def load_cameras_qca(qca_path):
    """Parse a Qualisys .qca.txt calibration file and return a list of
    Camera objects, one per <camera> under <cameras> that has a <transform>.

    Qualisys's schema doesn't expose a distinct video-reference-camera flag
    the way Vicon's ISDV does, so every camera here is categorized as
    "Optical" (QTM's video-only add-on cameras, if present, are not part of
    the <cameras> calibration block in the files this was tested against).
    """
    try:
        tree = ET.parse(qca_path)
    except ET.ParseError as e:
        raise CalibrationError(f"Failed to parse QCA file (not valid XML): {qca_path}. {e}") from e

    root = tree.getroot()
    cameras_el = root.find("cameras")
    if cameras_el is None:
        raise CalibrationError(f"No <cameras> block found in {qca_path}.")

    cameras = []
    for cam_el in cameras_el.findall("camera"):
        transform = cam_el.find("transform")
        if transform is None:
            continue
        intrinsic = cam_el.find("intrinsic")
        fov = cam_el.find("fov_video")
        if fov is None:
            fov = cam_el.find("fov_video_max")

        try:
            position = np.array([float(transform.get(k)) for k in ("x", "y", "z")])
            rotation = np.array([
                [float(transform.get("r11")), float(transform.get("r12")), float(transform.get("r13"))],
                [float(transform.get("r21")), float(transform.get("r22")), float(transform.get("r23"))],
                [float(transform.get("r31")), float(transform.get("r32")), float(transform.get("r33"))],
            ])
        except (AttributeError, TypeError, ValueError) as e:
            raise CalibrationError(
                f"Camera '{cam_el.get('serial', '?')}' in {qca_path} has a malformed <transform>."
            ) from e

        focal_length = float(intrinsic.get("focallength", "0")) if intrinsic is not None else 0.0
        image_error = float(cam_el.get("avg-residual", "0"))
        if fov is not None:
            width = float(fov.get("right", "1")) - float(fov.get("left", "0"))
            height = float(fov.get("bottom", "1")) - float(fov.get("top", "0"))
            sensor_size = (max(width, 1.0), max(height, 1.0))
        else:
            sensor_size = (1.0, 1.0)

        model_name = cam_el.get("model")
        camera_type = model_name if model_name and model_name.lower() != "none" else "Qualisys"

        cameras.append(Camera(
            device_id=cam_el.get("serial", "?"),
            camera_type=camera_type,
            category="Optical",
            sensor_size=sensor_size,
            position=position,
            rotation=rotation,
            focal_length=focal_length,
            image_error=image_error,
            frame=None,
        ))

    if not cameras:
        raise CalibrationError(f"No calibrated cameras found in {qca_path}.")
    return cameras


def load_calibration(path):
    """Dispatch to the Vicon (.xcp) or Qualisys (.qca.txt) loader by
    filename."""
    lower = path.lower()
    if lower.endswith(".qca.txt") or lower.endswith(".qca"):
        return load_cameras_qca(path)
    return load_cameras(path)
