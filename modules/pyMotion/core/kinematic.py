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


class kinematic:
    def __init__(self, file=""):
        self.kmtFile = file

        # label : array
        self.data = None
        self.realpoints = {}
        self.length = 0
        self.events = []        # list[TrialEvent] — from C3D EVENT:* params
        self.force_plates = []  # list[ForcePlateGroup] — from C3D analog channels

        if len(file):
            self.setFile(file)

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

    def setFile(self, f):
        self.kmtFile = f

        if not self.isC3D(f):
            logger.error("unsupported file format")
            return

        # remove old data
        self.clear()

        # load file
        try:
            c3d = c3dFile(f)
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

            self.reallabels = list(
                filter(lambda x: (self.manufacturer == 'VICON' and self.ismarker(x) and self.isrealmarker(x)) or self.manufacturer != 'VICON', self.labels)
            )
            for joint in self.reallabels:
                self.realpoints[joint] = self.data[joint]

            self.events = c3d.events  # list[TrialEvent], [] if none in file
            self.force_plates = self._load_force_plates(c3d)

        except:
            raise Exception(logger.errstr)

    def _load_force_plates(self, c3d_obj):
        """Group force plate analog channels into ForcePlateGroup objects.

        Handles labeled channels (Fx1/Fy1/…), prefixed (Force.Fx1/…), and
        duplicate-name channels without a plate number (Force.Fx appearing twice).
        Attaches corner geometry from FORCE_PLATFORM:CORNERS when available.
        Returns [] silently on any error.
        """
        try:
            analog = c3d_obj.analog
            corners_list = c3d_obj.force_corners  # list of [4×3] arrays in plate order
            plates = {}    # plate_id (int) → {comp: ndarray}
            seq_ctr = {}   # component name → next plate_id for numberless channels

            for label in analog.labels:
                m = _FP_CHANNEL_RE.match(label)
                if m is None:
                    continue
                comp = m.group(1).lower()  # fx / fy / fz / mx / my / mz
                num = m.group(2)
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
