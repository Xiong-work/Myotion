import scipy.io
import numpy as np
from .logger import *
from .timeSeriesTable import *

"""
mat data class

movement_data:   input raw data from mat file
format:
{
 "type":
 "name""
 ...
 "data":
}
"""


class matdata:
    def __init__(self, movement_data):
        self.metadata_keys = [
            "type",
            "name",
            "time_units",
            "begin_time",
            "frequency",
            "count",
            "units",
            "data",
        ]

        self.keys = self.metadata_keys
        self.keys.append("data")

        self.dict = {}
        for key in self.keys:
            self.dict[key] = movement_data[key]

        self.metadata = {}
        for key in self.metadata_keys:
            self.metadata[key] = movement_data[key]

    def keys(self):
        return self.keys

    def __getattr__(self, key):
        if key in self.keys:
            return self.dict[key]
        elif key == "metadata":
            return self.metadata
        elif key == "keys":
            return self.keys


class matFile:
    def __init__(self, file):
        self.file = file
        try:
            self.reader = scipy.io.loadmat(file, squeeze_me=True)
        except Exception as e:
            logger.error(f"Failed to open MAT file: {file}. Error: {str(e)}")
            raise ValueError(f"Failed to open MAT file: {file}. {str(e)}")

        # Find the first non-dunder data key dynamically (e.g. 'TABLE', 'DATA', etc.)
        data_keys = [k for k in sorted(self.reader.keys()) if not k.startswith("__")]
        if not data_keys:
            raise ValueError(
                "Invalid MAT file format: no data tables found. "
                "Available keys: {}".format(list(self.reader.keys()))
            )
        self.raw = self.reader[data_keys[0]]

        # Parse optional info metadata — degrade gracefully if fields are missing or renamed
        try:
            info = self.raw["info"].tolist()
        except Exception:
            info = None

        self.metadata = {
            "create_version": self._safe_field(info, "created_with_version"),
            "export_version": self._safe_field(info, "exported_with_version"),
            "last_name": self._safe_field(info, "last_name"),
            "first_name": self._safe_field(info, "first name", "first_name", "firstname"),
            "gender": self._safe_field(info, "sex", "gender"),
            "date": self._safe_field(info, "measurement_date", "date"),
            "record_name": self._safe_field(info, "record_name", "name"),
            "channel_number": 0,
            "labels": [],
        }

        # ['type', 'name', 'time_begin', 'time_end', sources]
        movements = self.raw["movements"].tolist()
        if "sources" not in movements.dtype.names:
            logger.error("sources is not found in movements")
            raise ValueError(
                "Invalid MAT file format: 'sources' field not found in movements. "
                "Available fields: {}".format(list(movements.dtype.names))
            )
        # [ 'sources', 'signals' ]
        sources = movements["sources"].tolist()

        if "signals" not in sources.dtype.names:
            logger.error("signals is not found in sources")
            raise ValueError(
                "Invalid MAT file format: 'signals' field not found in sources. "
                "Available fields: {}".format(list(sources.dtype.names))
            )
        signals = sources["signals"].tolist()

        # matdata type — skip individual channels that have incomplete fields
        movement_datas = []
        for key in signals.dtype.fields:
            signal_x = signals[key].tolist()
            movement_data = {}
            try:
                for sub_key in signal_x.dtype.fields:
                    movement_data[sub_key] = np.squeeze(signal_x[sub_key]).tolist()
                movement_datas.append(matdata(movement_data))
            except Exception as e:
                logger.warning("Skipping signal '{}': {}".format(key, e))
        if len(movement_datas) == 0:
            logger.error("movement data not extracted from mat")
            raise ValueError("Invalid MAT file format: no valid channels could be extracted")

        self.movements = {
            "type": movements["type"].tolist(),
            "name": movements["name"].tolist(),
            "time_begin": movements["time_begin"].tolist(),
            "time_end": movements["time_end"].tolist(),
            "source": sources,
            "channels": movement_datas,
        }

        self.metadata["labels"] = [m.name for m in self.movements["channels"]]
        self.metadata["channel_number"] = len(self.movements["channels"])

        logger.info("extracted mat labels {}".format(self.metadata["labels"]))

    @staticmethod
    def _safe_field(struct, *keys, default=""):
        """Try multiple field names on a tolist()-ed MATLAB struct; return first found or default."""
        if struct is None:
            return default
        for k in keys:
            try:
                val = struct[k]
                return val.tolist() if hasattr(val, "tolist") else val
            except Exception:
                continue
        return default

    def __getattr__(self, key):
        if key == "metadata":
            return self.metadata
        elif key in self.metadata.keys():
            return self.metadata[key]
        elif key in self.movements.keys():
            return self.movements[key]

    def __getitem__(self, idx):
        return self.data[idx]

    def __setitem__(self, idx, value):
        self.data[idx] = value

    def __delitem__(self, key):
        return

    def __missing__(self, key):
        return

    def convertToTST(self):
        if self.channel_number == 0:
            return None

        label = [c.name for c in self.channels]
        data = [c.data for c in self.channels]
        fs = self.channels[0].frequency
        return timeSeriesTable(fs, label, data)
