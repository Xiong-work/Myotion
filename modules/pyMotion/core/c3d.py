import c3d
import numpy as np
from .logger import *
from .timeSeriesTable import *


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

    def __del__(self):
        fh = getattr(self, "_fh", None)
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
