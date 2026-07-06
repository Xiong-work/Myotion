import re
import numpy as np

from .timeSeriesTable import *
from .c3d import *
from .mat import *
from .xml import *
from .logger import *
from .trial import ForcePlateGroup

# Identifies a force plate analog channel and extracts its component + plate number.
# Matches: "Fx1", "Force.Fx1", "Moment.Mx2", "Torque.Mz", "Force.Fx", etc.
_FP_CHANNEL_RE = re.compile(
    r"^(?:(?:Force|Torque|Moment)\.)?([FM][xyz])(\d*)$",
    re.IGNORECASE,
)
# Same, but plate-number BEFORE the axis letter -- a naming convention some
# force-plate systems use instead of the above: "F1X", "F2Y", "M3Z", etc.
# No "Force."/"Moment."-prefixed variant of this order has been seen, so it
# isn't offered here.
_FP_CHANNEL_RE_ALT = re.compile(r"^([FM])(\d*)([xyz])$", re.IGNORECASE)

# Pre-computed center-of-pressure channels some systems export directly
# instead of (or alongside) raw Mx/My: "COP1X", "COP2Y", "COPX", etc.
_COP_CHANNEL_RE = re.compile(r"^COP(\d*)([xy])$", re.IGNORECASE)

# Vicon Plug-in-Gait (and similar) model outputs -- joint angles, forces,
# moments, powers -- are stored in the same POINT block as real 3D markers;
# C3D has no dedicated "model output" section, so the only way to tell them
# apart is the label naming convention (e.g. "LKneeAngles", "RHipMoment",
# "LNormalisedGRF", "CentreOfMassFloor"). The Kinematics Inspection module
# only needs Angles -- Forces/Moments/Powers/ground-reaction/centre-of-mass
# are recognised only so they can be excluded from the marker list, never
# rendered as 3D marker dots.
_MODEL_OUTPUT_RE = re.compile(r"(?:Angles?|Forces?|Moments?|Powers?|GRF)$", re.IGNORECASE)
_MODEL_OUTPUT_EXTRA_NAMES = {"centreofmass", "centreofmassfloor"}
_ANGLE_OUTPUT_RE = re.compile(r"Angles?$", re.IGNORECASE)


def _is_model_output(label):
    if _MODEL_OUTPUT_RE.search(label):
        return True
    return label.lower() in _MODEL_OUTPUT_EXTRA_NAMES


def _is_angle_output(label):
    return bool(_ANGLE_OUTPUT_RE.search(label))


class kinematic:
    # preparsed_c3d: optional already-loaded c3dFile for *file* -- lets a
    # caller that also needs an emg() from the same combined C3D (e.g.
    # batch_scan.build_participant()) parse it once instead of twice.
    def __init__(self, file="", preparsed_c3d=None):
        self.kmtFile = file

        # label : array
        self.data = None
        self.realpoints = {}
        self.anglelabels = []   # Model-output Angle labels (e.g. "LKneeAngles")
        self.anglepoints = {}   # same shape as realpoints, kept separate -- never rendered as 3D markers
        self.length = 0
        self.events = []        # list[TrialEvent] — from C3D EVENT:* params
        self.force_plates = []  # list[ForcePlateGroup] — from C3D analog channels

        if len(file):
            self.setFile(file, c3d_obj=preparsed_c3d)

    def isValid(self):
        return not self.data == None

    # VICON only
    def isrealmarker(self, label):
        """check if a marker is a real marker by replacing the last character with 'L' 'O' 'A' 'P'

        Args:
            rawlabels (list of str): all labels in the c3d file
            label (str): current label being investigated

        Returns:
            bool: True if the marker is a real marker
        """

        if (
            label.endswith("L")
            or label.endswith("O")
            or label.endswith("A")
            or label.endswith("P")
        ):
            return not (
                label[:-1] + "L" in self.labels
                and label[:-1] + "O" in self.labels
                and label[:-1] + "A" in self.labels
                and label[:-1] + "P" in self.labels
            )
        return True

    def ismarker(self, label):
        """check if label is a marker

        Args:
            points (dict of listof str): dictionary of all points in the c3d file
            label (str): current label name

        Returns:
            bool : if label is a marker
        """
        return len(label) < 6

    def clear(self):
        return

    def isC3D(self, f):
        return f.endswith(".c3d")

    def setFile(self, f, c3d_obj=None):
        self.kmtFile = f

        if not self.isC3D(f):
            logger.error("unsupported file format")
            return

        # remove old data
        self.clear()

        # load file
        try:
            c3d = c3d_obj if c3d_obj is not None else c3dFile(f)
            self.data = c3d.points
            self.point_fs = c3d.point_fs
            self.analog_fs = c3d.analog_fs
            self.frame_number = c3d.frame_number
            self.labels = c3d.point_labels
            self.first_frame = c3d.reader.first_frame
            self.last_frame = c3d.reader.last_frame
            self.length = c3d.reader.frame_count
            self.manufacturer = c3d.manufacturer.string_value if c3d.manufacturer else ""
            self.software = c3d.software.string_value if c3d.software else ""

            # Real markers only -- excludes both Plug-in-Gait virtual markers
            # (segment-axis O/A/L/P quads) and model-output points (Angles/
            # Forces/Moments/Powers/etc, see _is_model_output()). Applied
            # regardless of manufacturer -- these are naming conventions, not
            # something only Vicon-labeled files can have, and the previous
            # exact-case "VICON" check silently disabled all filtering for
            # any file whose manufacturer string wasn't that exact case
            # (e.g. real files report "Vicon", not "VICON").
            self.anglelabels = [x for x in self.labels if _is_angle_output(x)]
            self.reallabels = [
                x for x in self.labels
                if not _is_model_output(x) and self.isrealmarker(x)
            ]
            for joint in self.reallabels:
                self.realpoints[joint] = self.data[joint]
            self.anglepoints = {joint: self.data[joint] for joint in self.anglelabels}

            self.events = c3d.events  # list[TrialEvent], [] if none in file
            self.force_plates = self._load_force_plates(c3d)

        except:
            raise Exception(logger.errstr)

    def _load_force_plates(self, c3d_obj):
        """Group force plate analog channels into ForcePlateGroup objects.

        Handles labeled channels (Fx1/Fy1/…), prefixed (Force.Fx1/…),
        number-before-axis channels (F1X/M2Z/…), pre-computed COP channels
        (COP1X/COP1Y/…), and duplicate-name channels without a plate number
        (Force.Fx appearing twice). Attaches corner geometry from
        FORCE_PLATFORM:CORNERS when available. Returns [] silently on any error.
        """
        try:
            analog = c3d_obj.analog
            corners_list = c3d_obj.force_corners  # list of [4×3] arrays in plate order
            plates = {}    # plate_id (int) → {comp: ndarray}
            seq_ctr = {}   # component name → next plate_id for numberless channels

            def _assign(comp, num, label):
                if num:
                    plate_id = int(num)
                else:
                    if comp not in seq_ctr:
                        seq_ctr[comp] = 1
                    plate_id = seq_ctr[comp]
                    seq_ctr[comp] += 1
                if plate_id not in plates:
                    plates[plate_id] = {}
                if comp not in plates[plate_id]:  # first occurrence wins
                    plates[plate_id][comp] = np.array(analog[label], dtype=np.float32)

            for label in analog.labels:
                m = _FP_CHANNEL_RE.match(label)
                if m is not None:
                    _assign(m.group(1).lower(), m.group(2), label)  # fx / fy / fz / mx / my / mz
                    continue
                m = _FP_CHANNEL_RE_ALT.match(label)
                if m is not None:
                    _assign((m.group(1) + m.group(3)).lower(), m.group(2), label)
                    continue
                m = _COP_CHANNEL_RE.match(label)
                if m is not None:
                    _assign("cop" + m.group(2).lower(), m.group(1), label)

            zeros = np.zeros(1, dtype=np.float32)
            result = []
            for idx, pid in enumerate(sorted(plates.keys())):
                d = plates[pid]
                if not ("fx" in d and "fy" in d and "fz" in d):
                    continue  # skip incomplete plates
                # Match corners by sequential index (first plate → corners[0], etc.)
                corners = corners_list[idx] if idx < len(corners_list) else None
                result.append(ForcePlateGroup(
                    plate_id=pid,
                    fs=analog.fs,
                    Fx=d["fx"],
                    Fy=d["fy"],
                    Fz=d["fz"],
                    Mx=d.get("mx", zeros),
                    My=d.get("my", zeros),
                    Mz=d.get("mz", zeros),
                    corners=corners,
                    Cx=d.get("copx"),
                    Cy=d.get("copy"),
                ))
            if result:
                logger.info("C3D force plates loaded: {}".format(result))
            return result
        except Exception as ex:
            logger.warning("Force plate loading failed (non-fatal): {}".format(ex))
            return []

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def __delitem__(self, key):
        return

    def __missing__(self, key):
        return
