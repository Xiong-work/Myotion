import c3d
import numpy as np
from .logger import *
from .timeSeriesTable import *
from .trial import TrialEvent


class point:
    def __init__(self, data):
        """
        format
            coordinate:  (x, y, z) tuple
            eta_error:   estimate error
            cam_num:     camera number
        """
        self.data = {
            "xyz": data[0:3],
            "error": data[3],
            "camera": data[4],
        }

    def __getattr__(self, key):
        if key in self.data.keys():
            return self.data[key]

    def str(self):
        str = "({},{},{})".format(
            self.data["xyz"], self.data["error"], self.data["camera"]
        )
        return str


class points:
    def __init__(self, labels, fs):
        assert len(labels)
        self.labels = labels
        self.fs = fs
        self.metadata = {"label": labels, "fs": fs}
        self.data = {}
        for label in labels:
            self.data[label] = []

    def insertPoint(self, channel, data):
        if channel not in self.labels:
            return
        self.data[channel].append(point(data))

    def channels(self):
        return len(self.data)

    def size(self):
        if self.channels():
            return len(self.data[self.labels[0]])
        else:
            return 0

    def str(self):
        str = "============  Points  ==============="
        for l in self.labels:
            str += "channel {}:".format(l)
            for p in self.data[l]:
                str += p.str()
        return str

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, obj):
        self.data[key] = obj

    def __delitem__(self, key):
        return

    def __missing__(self, key):
        return

    def convertToTST(self):
        if self.channels() == 0:
            return timeSeriesTable()

        data = [self.data[key] for key in self.labels]  # convert to list
        return timeSeriesTable(self.fs, self.label, data)


"""
Anlog class that contains data of sampling from c3d file

analog_data: a matrix of
                          column ->            each line has N number of samples
                          row    ->            each line is for one analog channel
                          [
                              [ a, b, c ,  ... ]  
                              [ ... ]
                              [ ... ]
                          ]

"""


class AnalogData:
    def __init__(self, labels, fs):
        assert len(labels)
        self.labels = labels
        self.fs = fs
        self.metadata = {"labels": labels, "fs": fs}
        self.analog_data = {}
        for label in labels:
            self.analog_data[label] = []

    def __getattr__(self, key):
        if key in self.metadata:
            return self.metadata[key]

    # expect data to be a vector of numbers for the channel
    def insertData(self, channel, data):
        if channel not in self.labels:
            return
        self.analog_data[channel].extend(data)

    def channels(self):
        return len(self.analog_data)

    def size(self):
        if self.channels():
            return len(self.analog_data[self.labels[0]])
        else:
            return 0

    def str(self):
        str = "============  Analog  ==============="
        for l in self.labels:
            str += "channel {}:{}".format(l, self.analog_data[l])
        return str

    def __getitem__(self, key):
        return self.analog_data[key]

    def __setitem__(self, key, obj):
        self.analog_data[key] = obj

    def __delitem__(self, key):
        return

    def __missing__(self, key):
        return

    def convertToTST(self):
        if self.channels() == 0:
            return None

        data = [self.analog_data[key] for key in self.labels]  # convert to list
        return timeSeriesTable(self.fs, self.labels, data)


def c3d_probe(file):
    """Read only a C3D file's point/analog header info -- no per-frame data --
    for fast role classification ("does it have real marker data? what are
    its analog channel labels?"). c3dFile's constructor is correct but always
    pays for a full frame-by-frame parse (self.reader.read_frames()), which is
    unnecessary and can take several seconds per file when only the header is
    needed (e.g. batch_stitch.py classifying dozens of files before offering
    to stitch, or batch_scan.py validating a candidate's channel list).

    Returns (has_points: bool, analog_labels: list[str]), using the exact
    same has_points rule as c3dFile (point_used > 0, has labels, positive
    point_rate).
    """
    try:
        with open(file, "rb") as fh:
            reader = c3d.Reader(fh)
            point_used = reader.point_used
            point_rate = reader.point_rate
            point_labels = [s.strip() for s in reader.point_labels]
            analog_labels = [s.strip() for s in reader.analog_labels]
    except Exception as e:
        raise ValueError("Failed to open C3D file {}: {}".format(file, str(e)))

    has_points = point_used > 0 and len(point_labels) > 0 and point_rate > 0
    return has_points, analog_labels


class c3dFile:
    def __init__(self, file):
        self.file = file
        try:
            self._fh = open(file, "rb")
            self.reader = c3d.Reader(self._fh)
        except Exception as e:
            logger.error("failed to open c3d file {}: {}".format(file, str(e)))
            raise ValueError("Failed to open C3D file {}: {}".format(file, str(e)))

        # get metadata
        self.attr = {
            "analog_rate": getattr(self.reader, "analog_rate"),
            "analog_labels": getattr(self.reader, "analog_labels"),
            "analog_used": getattr(self.reader, "analog_used"),
            "point_labels": getattr(self.reader, "point_labels"),
            "point_rate": getattr(self.reader, "point_rate"),
            "point_scale": getattr(self.reader, "point_scale"),
            "point_used": getattr(self.reader, "point_used"),
        }
        self.manufacturer = self.reader.get('MANUFACTURER:COMPANY')
        self.software = self.reader.get('MANUFACTURER:SOFTWARE')

        self.analog_fs = self.attr["analog_rate"]
        self.point_fs = self.attr["point_rate"]

        analog_labels = self.attr["analog_labels"]
        analog_channel_num = self.attr["analog_used"]
        point_labels = self.attr["point_labels"]
        point_number = self.attr["point_used"]

        # strip white space
        point_labels = [s.strip() for s in point_labels]
        analog_labels = [s.strip() for s in analog_labels]

        # Some files (e.g. EMG-only) have no 3D marker data.
        # Guard against zero point_fs and empty point label list.
        has_points = point_number > 0 and len(point_labels) > 0 and self.point_fs > 0
        if has_points:
            ratio = int(self.analog_fs / self.point_fs)
            all_points = points(point_labels, self.point_fs)
        else:
            ratio = 1
            all_points = None

        self.analogdata = AnalogData(analog_labels, self.analog_fs)

        frame_number = 0
        # load data
        for frame_no, p, analog_data in self.reader.read_frames():
            if has_points:
                for i in range(0, point_number):
                    all_points.insertPoint(point_labels[i], p[i])
            for j in range(0, analog_channel_num):
                self.analogdata.insertData(analog_labels[j], analog_data[j])
            frame_number = frame_no

        if all_points is not None and self.point_fs > 0:
            total_time = all_points.size() / self.point_fs
        elif self.analog_fs > 0 and self.analogdata.size() > 0:
            total_time = self.analogdata.size() / self.analog_fs
        else:
            total_time = 0.0

        self.data = {
            "point_fs": self.attr["point_rate"],  # point sample freq
            "analog_fs": self.attr["analog_rate"],  # data sample freq
            "point_number": self.attr["point_used"],  # number of points
            "channel_number": self.attr["analog_used"],  # number of channels
            "point_labels": point_labels,  # label of points
            "channel_labels": analog_labels,  # label of channels
            "frame_number": frame_number,  # frame number in c3d file
            "time": total_time,  # total time of sampling
            "points": all_points,  # collection of points (None if no marker data)
            "analog": self.analogdata,  # collection of analogdata
        }

        # Trial events embedded in the C3D file (always safe; returns [] if absent)
        self.events = self._extract_events()
        # Force plate corner geometry — list of [4×3] arrays, one per plate in order
        self.force_corners = self._extract_force_corners()

    def _extract_force_corners(self):
        """Read FORCE_PLATFORM:CORNERS and return a list of [4×3] corner arrays.

        Returns [] if the parameter is absent or unrecognised — never raises.
        Each entry is a float64 ndarray of shape (4, 3): four corners in C3D lab
        frame (mm).

        Implementation note: param.float_value in the c3d library only reads a
        single scalar.  The raw bytes + param.dimensions are used instead with
        Fortran-order (column-major) reshape, matching C3D parameter storage.
        """
        try:
            param = self.reader.get("FORCE_PLATFORM:CORNERS")
            if param is None or not param.dimensions:
                return []
            total = 1
            for d in param.dimensions:
                total *= d
            raw = np.frombuffer(param.bytes, dtype=np.float32)
            if raw.size < total:
                return []
            # Fortran-order reshape: dimensions=[3, 4, N] → shape (3, 4, N)
            arr = raw[:total].reshape(param.dimensions, order="F").astype(float)
            if arr.ndim == 3 and arr.shape[0] == 3 and arr.shape[1] == 4:
                # (3_coords, 4_corners, N_plates) — standard C3D layout
                return [arr[:, :, i].T for i in range(arr.shape[2])]
            if arr.ndim == 2 and arr.shape[0] == 3 and arr.shape[1] == 4:
                # Single plate: (3, 4)
                return [arr.T]
            return []
        except Exception as ex:
            logger.warning("FORCE_PLATFORM:CORNERS extraction failed: {}".format(ex))
            return []

    def _extract_events(self):
        """Read C3D EVENT:* parameters and return a sorted list of TrialEvent.

        Returns [] if the parameters are absent or malformed — never raises.
        """
        try:
            used_param = self.reader.get("EVENT:USED")
            if used_param is None:
                return []
            n = int(used_param.int16_value)
            if n <= 0:
                return []

            times_param = self.reader.get("EVENT:TIMES")
            if times_param is None:
                return []

            # C3D spec: EVENT:TIMES is [2 x n] where row [1] holds seconds.
            # Some exporters store it flat; handle both.
            times_raw = np.asarray(times_param.float_value)
            if times_raw.ndim == 2:
                times = times_raw[1].flatten()
            else:
                times = times_raw.flatten()

            def _str_list(param):
                if param is None:
                    return []
                try:
                    v = param.string_value
                    if isinstance(v, str):
                        return [v.strip()]
                    if isinstance(v, bytes):
                        return [v.decode("utf-8", errors="replace").strip()]
                    arr = np.asarray(v)
                    return [
                        s.strip() if isinstance(s, str)
                        else s.decode("utf-8", errors="replace").strip()
                        for s in arr.flatten()
                    ]
                except Exception:
                    return []

            labels = _str_list(self.reader.get("EVENT:LABELS"))
            contexts = _str_list(self.reader.get("EVENT:CONTEXTS"))

            events = []
            for i in range(min(n, len(times))):
                events.append(TrialEvent(
                    time_s=float(times[i]),
                    label=labels[i] if i < len(labels) else "",
                    context=contexts[i] if i < len(contexts) else "",
                ))
            events.sort(key=lambda e: e.time_s)

            if events:
                logger.info("C3D events ({}): {}".format(len(events), events))
            return events

        except Exception as ex:
            logger.warning("C3D event extraction skipped: {}".format(ex))
            return []

    def __del__(self):
        # Use __dict__ directly to avoid triggering __getattr__ when _fh or
        # data were never set (e.g. init failed before self.data was assigned).
        fh = self.__dict__.get("_fh", None)
        if fh is not None and not fh.closed:
            fh.close()

    def __getattr__(self, key):
        if key in self.data.keys():
            return self.data[key]
        elif key == "metadata":
            return {
                "point_fs": self.data["point_fs"],
                "analog_fs": self.data["analog_fs"],
                "point_number": self.data["point_number"],
                "channel_number": self.data["channel_number"],
                "frame_number": self.data["frame_number"],
                "point_labels": self.data["point_labels"],
                "channel_labels": self.data["channel_labels"],
                "time": self.data["time"],
            }

    def __getitem__(self, idx):
        return self.data[idx]

    def __setitem__(self, idx, value):
        self.data[idx] = value

    def __delitem__(self, key):
        return

    def __missing__(self, key):
        return
