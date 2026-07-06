import asyncio
import os
from .timeSeriesTable import *
from .c3d import *
from .mat import *
from .xml import *
from enum import Enum
from .logger import *
import re
from enum import IntEnum
from copy import deepcopy
import numpy as np
import scipy.signal as _sig

# Compiled patterns for analog channels that are never EMG:
# force-plate forces/torques and IMU accelerometer/gyro/mag data.
# Applied automatically when loading C3D files so these channels are not
# presented to the user as selectable EMG signals.
_NON_EMG_PATTERNS = [
    # Force plate with explicit prefix + optional plate-number suffix:
    #   "Force.Fx", "Force.Fx1", "Torque.Mz", "Moment.Mx2", etc.
    # "Moment" is an alternative spelling used by some systems (e.g. Vicon).
    re.compile(r"^(Force|Torque|Moment)\.[FM][xyz]\d*$", re.IGNORECASE),
    # Bare force/moment component: "Fx", "Fy1", "Fz2", "Mx", "My1", "Mz2", etc.
    # Exactly F or M + x/y/z + optional digits. Common muscle abbreviations are
    # safe: RF, FDL, MG, MF, BF all differ because their 2nd char is not x/y/z.
    re.compile(r"^[FM][xyz]\d*$", re.IGNORECASE),
    # Same, but plate-number BEFORE the axis letter: "F1X", "F2Y", "M3Z", etc.
    # -- a naming convention some force-plate systems use instead of "Fx1".
    re.compile(r"^[FM]\d*[xyz]$", re.IGNORECASE),
    # Pre-computed center-of-pressure channels: "COP1X", "COP2Y", "COPX", etc.
    re.compile(r"^COP\d*[xyz]$", re.IGNORECASE),
    # IMU sensor channels: "Sensor 1.ACCX1", "Sensor 3.GYROZ3", "Sensor 2.MAGY2", etc.
    # Requires "Sensor N." prefix -- "Sensor N.EMG" and "Sensor_EMG1" are kept.
    re.compile(r"^Sensor\s*\d+\.(ACC|GYRO|MAG)[XYZ]\d*$", re.IGNORECASE),
]


def _is_non_emg_channel(label: str) -> bool:
    """Return True if label matches a known non-EMG analog channel pattern."""
    return any(p.match(label) for p in _NON_EMG_PATTERNS)


class emgConfigEnum(IntEnum):
    DC_OFFSET = 0
    FILTER = 1
    FULL_W_RECT = 2
    NORMALIZATION = 3
    ACTIVATION = 4
    SUMMARY = 5
    MAX = 6


class emgConfigInfo:
    classical_steps = [
        emgConfigEnum.DC_OFFSET,
        emgConfigEnum.FILTER,
        emgConfigEnum.FULL_W_RECT,
        emgConfigEnum.FILTER,
        emgConfigEnum.NORMALIZATION,
        emgConfigEnum.SUMMARY,
    ]
    nameMap = {
        emgConfigEnum.DC_OFFSET: "Remove DC Offset",
        emgConfigEnum.FILTER: "Filter",
        emgConfigEnum.FULL_W_RECT: "Full-Wave Rectification",
        emgConfigEnum.NORMALIZATION: "Normalization",
        emgConfigEnum.ACTIVATION: "Activation",
        emgConfigEnum.SUMMARY: "Summary",
    }


class emgDCOffset:
    id = emgConfigEnum.DC_OFFSET

    def __init__(self):
        self.enable = True

    def toXML(self):
        e = xmlElement("emgDCOffset")
        e.addNode("enable", xmlString(self.enable))
        return e

    @staticmethod
    def fromXML(xml):
        root = xml.find("emgDCOffset")
        if root == None:
            return None

        obj = emgDCOffset()
        e = root.find("enable")
        if e and e.text:
            obj.enable = xmlStringParse(e.text, bool)
        else:
            obj.enable = False
        return obj


class emgRectification:
    id = emgConfigEnum.FULL_W_RECT

    def __init__(self):
        self.enable = True

    def toXML(self):
        e = xmlElement("emgRectification")
        e.addNode("enable", xmlString(self.enable))
        return e

    @staticmethod
    def fromXML(xml):
        root = xml.find("emgRectification")
        if root == None:
            return None

        obj = emgRectification()
        e = root.find("enable")
        if e and e.text:
            obj.enable = xmlStringParse(e.text, bool)
        else:
            obj.enable = False
        return obj


class emgNormTypeEnum(IntEnum):
    MVC = 0        # normalize by MVC trial max (existing behaviour)
    TRIAL_MAX = 1  # normalize by max of valid cropped envelope segment


class emgNormalization:
    id = emgConfigEnum.NORMALIZATION

    def __init__(self):
        self.enable = False  # disabled by default; user must opt in and supply MVC files
        self.norm_type = emgNormTypeEnum.MVC

    def toXML(self):
        e = xmlElement("emgNormalization")
        e.addNode("enable", xmlString(self.enable))
        e.addNode("norm_type", xmlString(int(self.norm_type)))
        return e

    @staticmethod
    def fromXML(xml):
        root = xml.find("emgNormalization")
        if root == None:
            return None

        obj = emgNormalization()
        e = root.find("enable")
        if e and e.text:
            obj.enable = xmlStringParse(e.text, bool)
        else:
            obj.enable = False
        e = root.find("norm_type")
        if e and e.text:
            try:
                obj.norm_type = emgNormTypeEnum(xmlStringParse(e.text, int))
            except Exception:
                obj.norm_type = emgNormTypeEnum.MVC
        else:
            obj.norm_type = emgNormTypeEnum.MVC
        return obj


class emgSummary:
    id = emgConfigEnum.SUMMARY

    def __init__(self):
        self.max = 0
        self.min = 0
        self.iemg = 0  # integrated EMG: sum(|x|) * (1/fs) over the analysis segment

    def toXML(self):
        e = xmlElement("emgSummary")
        # we don't save temp calculation to config file
        return e

    @staticmethod
    def fromXML(xml):
        root = xml.find("emgSummary")
        if root == None:
            return None
        return emgSummary()


class emgFilterEnum(IntEnum):
    BAND_PASS = 0
    LOW_PASS = 1
    MAX = 2


class emgFilter:
    id = emgConfigEnum.FILTER

    def __init__(self):
        self.enable = True
        self.type = emgFilterEnum.BAND_PASS
        self.cutoff_l = 0
        self.cutoff_h = 0
        self.order = int(2)

        self.nameMap = {
            emgFilterEnum.LOW_PASS: "low pass filter",
            emgFilterEnum.BAND_PASS: "band pass filter",
            emgFilterEnum.MAX: "N/A",
        }

    def setType(self, t: int):
        if t >= emgFilter.MAX or t < 0:
            logger.error("invalid filter type!")
            return -1

        self.type = t

    def setCutOff_L(self, freq: float):
        self.cutoff_l = freq

    def setCutOff_H(self, freq: float):
        self.cutoff_h = freq

    def setOrder(self, index: int):
        self.order = index

    def toXML(self):
        e = xmlElement("emgFilter")
        e.addNode("type", xmlString(int(self.type)))
        e.addNode("order", xmlString(self.order))
        e.addNode("cutoff_l", xmlString(self.cutoff_l))
        e.addNode("cutoff_h", xmlString(self.cutoff_h))
        return e

    @staticmethod
    def fromXML(xml):
        root = xml.find("emgFilter")
        if root == None:
            return None

        obj = emgFilter()
        e = root.find("type")
        if e and e.text:
            obj.type = xmlStringParse(e.text, int)
        else:
            obj.type = emgFilterEnum.BAND_PASS
        e = root.find("order")
        if e and e.text:
            obj.order = xmlStringParse(e.text, int)
        else:
            obj.order = 2
        e = root.find("cutoff_l")
        if e and e.text:
            obj.cutoff_l = xmlStringParse(e.text, float)
        else:
            obj.cutoff_l = 0
        e = root.find("cutoff_h")
        if e and e.text:
            obj.cutoff_h = xmlStringParse(e.text, float)
        else:
            obj.cutoff_h = 0
        return obj


class emgActivation:
    id = emgConfigEnum.ACTIVATION

    def __init__(self):
        self.threhold = 0
        self.n_above = 5
        self.n_below = 5

    def setThreHold(self, t: float):
        self.threhold = t

    def set_L(self, l):
        self.n_below = l

    def set_H(self, h):
        self.n_above = h

    def toXML(self):
        e = xmlElement("emgActivation")
        e.addNode("threhold", xmlString(self.threhold))
        e.addNode("n_above", xmlString(self.n_above))
        e.addNode("n_below", xmlString(self.n_below))
        return e

    @staticmethod
    def fromXML(xml):
        root = xml.find("emgActivation")
        if root == None:
            return None

        obj = emgActivation()
        e = root.find("threhold")
        if e and e.text:
            obj.threhold = xmlStringParse(e.text, float)
        else:
            obj.threhold = 0
        e = root.find("n_above")
        if e and e.text:
            obj.n_above = xmlStringParse(e.text, int)
        else:
            obj.n_above = 5
        e = root.find("n_below")
        if e and e.text:
            obj.n_below = xmlStringParse(e.text, int)
        else:
            obj.n_below = 5
        return obj


class emgConfigure:
    def __init__(self):
        # default config for each step
        self.stepConfig = []
        for i, s in enumerate(emgConfigInfo.classical_steps):
            config = self.initConfig(s)

            # Step 2 (index 1): band-pass for EMG signal conditioning
            if s == emgConfigEnum.FILTER and i == 1:
                config.type = emgFilterEnum.BAND_PASS
                config.cutoff_l = 50
                config.cutoff_h = 450
                config.order = 2

            # Step 4 (index 3): low-pass linear envelope
            if s == emgConfigEnum.FILTER and i == 3:
                config.type = emgFilterEnum.LOW_PASS
                config.cutoff_l = 6
                config.order = 2

            self.stepConfig.append(config)


    # use step id as key to access config file
    def __getitem__(self, id):
        return self.stepConfig[id]

    def copy(self):
        t = emgConfigure()
        t.stepConfig = deepcopy(self.stepConfig)
        return t

    """
    # add new step
    def addStep(self, idx, pos):
        if idx >= emgConfigure.MAX or idx < 0:
            logger.error("invalid emg steps!")
            return -1

        self.step.insert(pos, idx)

    # remove step
    def removeStep(self, pos):
        self.step.remove(pos)
    """

    def getTypeInfo(self, idx):
        type_id = self.stepConfig[idx].id
        return type_id, emgConfigInfo.nameMap[type_id]

    def getStepStringList(self):
        result = []
        for s in self.stepConfig:
            if s.id == emgConfigEnum.FILTER:
                if hasattr(s, "type") and int(s.type) == int(emgFilterEnum.LOW_PASS):
                    result.append("Low-pass Filter")
                else:
                    result.append("Band-pass Filter")
            else:
                result.append(emgConfigInfo.nameMap[s.id])
        return result

    def size(self):
        return len(self.stepConfig)

    # create a config for one step
    def initConfig(self, type):
        if type == emgConfigEnum.FILTER:
            return emgFilter()
        elif type == emgConfigEnum.ACTIVATION:
            return emgActivation()
        elif type == emgConfigEnum.DC_OFFSET:
            return emgDCOffset()
        elif type == emgConfigEnum.FULL_W_RECT:
            return emgRectification()
        elif type == emgConfigEnum.NORMALIZATION:
            return emgNormalization()
        elif type == emgConfigEnum.SUMMARY:
            return emgSummary()
        else:
            return None

    """
    <emgConfigure>
        <remove_dc_offset/>
        <filter> 
           <type> </type>
           ...
        </filter>
    </emgConfigure>
    """

    def toXML(self):
        # top tree
        e = xmlElement("emgConfigure")
        for s in self.stepConfig:
            e.addSubTree(s.toXML())
        return e

    @staticmethod
    def fromXML(xml):
        root = xml.find("emgConfigure")
        if root == None:
            return None

        obj = emgConfigure()
        obj.stepConfig = []
        for el in root:
            cfg = emgFilter.fromXML(el)
            if cfg:
                obj.stepConfig.append(cfg)
                continue
            cfg = emgActivation.fromXML(el)
            if cfg:
                obj.stepConfig.append(cfg)
                continue
            cfg = emgDCOffset.fromXML(el)
            if cfg:
                obj.stepConfig.append(cfg)
                continue
            cfg = emgRectification.fromXML(el)
            if cfg:
                obj.stepConfig.append(cfg)
                continue
            cfg = emgNormalization.fromXML(el)
            if cfg:
                obj.stepConfig.append(cfg)
                continue
            cfg = emgSummary.fromXML(el)
            if cfg:
                obj.stepConfig.append(cfg)
                continue

        if len(obj.stepConfig) == 0:
            return emgConfigure()
        return obj


class emg:
    # preparsed_c3d: optional already-loaded c3dFile for *file* -- lets a
    # caller that also needs a kinematic() from the same combined C3D (e.g.
    # batch_scan.build_participant()) parse it once instead of twice.
    def __init__(self, file="", preparsed_c3d=None):
        self.emgFile = file  # file path
        self.emgTST = None  # emg data
        self.rawTST = None  # original loaded signal, never modified after setEMGFile
        self.emgMVCTST = None  # emg MVC data
        self.processCFG = None  # emg data process configure
        self.Channels = []  # channels of emg
        self.enabledChannels = set() # enabled channels
        self.mvcFilesMap = {}  # channels:mvc_file_path
        self.chanMap = {}  # old chan name: new chan name
        self.isprocessdone = False

        # filter of channel name, regex
        self.channel_filter = "(emg|EMG)+"

        if file != None and len(file):
            self._setEMGFile(file, c3d_obj=preparsed_c3d)

    async def async_load(self):
        """Asynchronously load EMG and MVC files."""
        if self.emgFile is None:
            return -1

        # Asynchronously load EMG file.
        await self.async_set_emg_file(self.emgFile)

        # Asynchronously load MVC files.
        tasks = [
            self.async_set_mvc_file(chan, mvcfile)
            for chan, mvcfile in self.mvcFilesMap.items()
        ]
        await asyncio.gather(*tasks)

        # Rename channels.
        for old, new in self.chanMap.items():
            self.renameChannel(old, new)

        return 0

    async def async_set_emg_file(self, file):
        """Asynchronously load EMG file."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.setEMGFile, file)

    async def async_set_mvc_file(self, channel, file):
        """Asynchronously load MVC file."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.setMVCFile, channel, file)

    # applying tst to emg, used when
    # loading emg from a report
    def load_from_report(self, tst):
        self.emgTST = tst.copy()
        self.rawTST = self.emgTST.copy()
        self.isprocessdone = True
        self.Channels = tst.labels.copy()
        self.enabledChannels = set(tst.labels)
        # MVC data is not stored in the report — initialise an empty table so
        # re-processing does not crash on None['channel'] subscript
        self.emgMVCTST = timeSeriesTable(tst.fs, tst.labels)

    # use channel as key to access TST
    def __getitem__(self, chan):
        return self.emgTST[chan]

    def getLinspace(self):
        return self.emgTST.getLinspace()

    def isC3D(self, f):
        return isinstance(f, str) and os.path.splitext(f)[1].lower() == ".c3d"

    def isMAT(self, f):
        return isinstance(f, str) and os.path.splitext(f)[1].lower() == ".mat"

    # check if MVC TST has all channels in place
    def isMVCComplete(self):
        for c in self.Channels:
            if c in self.enabledChannels and not self.emgMVCTST.hasChannel(c):
                return False
        return True

    # remove old data
    def clear(self):
        self.emgTST = None
        self.emgMVCTST = None
        self.Channels.clear()
        self.enabledChannels.clear()

    # return all channels
    def getAllChannels(self):
        return self.Channels

    # return channels
    def getChannels(self):
        return [c for c in self.Channels if c in self.enabledChannels]

    def getfs(self):
        return self.emgTST.fs

    def getTST(self):
        return self.emgTST

    # search channels
    def searchChannels(self, filter):
        return self.emgTST.searchChannel(filter)

    # filter for channel name, use regex
    def applyChannelFiler(self, filter):
        self.channel_filter = filter
        self.emgTST.filterChannel(self.channel_filter)
        self.emgMVCTST.filterChannel(self.channel_filter)

    # remove a channel from emg
    def removeChannel(self, channel):
        if channel in self.Channels:
            del self.emgTST[channel]
            del self.emgMVCTST[channel]
            self.Channels.remove(channel)
            self.enabledChannels.remove(channel)

    # remove a list of channels
    def removeChannels(self, channels):
        for c in channels:
            self.removeChannel(c)

    # rename channel from old to new, keep data the same
    def renameChannel(self, old, new):
        if old in self.Channels:
            self.emgTST.renameChannel(old, new)
            self.emgMVCTST.renameChannel(old, new)
            if self.rawTST is not None:
                self.rawTST.renameChannel(old, new)
            # rename channel name
            self.Channels[self.Channels.index(old)] = new
            self.chanMap[old] = new

        if old in self.enabledChannels:
            self.enabledChannels.remove(old)
            self.enabledChannels.add(new)

    # enable channel
    def enableChannel(self, chan):
        if chan not in self.Channels:
            return -1

        self.enabledChannels.add(chan)

    def disableChannel(self, chan):
        if chan not in self.Channels:
            return -1

        self.enabledChannels.remove(chan)

    # set EMG file path
    def setEMGFile(self, f):
        self._setEMGFile(f)

    def _setEMGFile(self, f, c3d_obj=None):
        if f is None or len(str(f).strip()) == 0:
            raise ValueError("EMG file path is empty")
        if not os.path.isfile(f):
            raise ValueError("EMG file not found: {}".format(f))

        self.emgFile = f

        # load file
        try:
            if self.isC3D(f):
                if c3d_obj is None:
                    c3d_obj = c3dFile(f)
                all_labels = c3d_obj.analog.labels

                # Auto-exclude known non-EMG channels (force plates, IMU sensors)
                emg_labels = [l for l in all_labels if not _is_non_emg_channel(l)]
                excluded = [l for l in all_labels if l not in set(emg_labels)]

                if excluded and emg_labels:
                    logger.info(
                        "C3D load '{}': kept {} EMG channel(s) {}, "
                        "excluded {} non-EMG channel(s) {}".format(
                            os.path.basename(f),
                            len(emg_labels), emg_labels,
                            len(excluded), excluded,
                        )
                    )
                elif excluded and not emg_labels:
                    # All channels were non-EMG — raise a clear error
                    raise ValueError(
                        "No EMG channels found in '{}'. "
                        "All {} analog channel(s) were identified as non-EMG "
                        "(force plate / IMU data): {}".format(
                            os.path.basename(f), len(excluded), excluded
                        )
                    )
                else:
                    logger.info(
                        "C3D load '{}': loaded {} channel(s) {}".format(
                            os.path.basename(f), len(emg_labels), emg_labels
                        )
                    )

                self.Channels = emg_labels
                if emg_labels:
                    emg_data = [c3d_obj.analog[l] for l in emg_labels]
                    self.emgTST = timeSeriesTable(c3d_obj.analog.fs, emg_labels, emg_data)
                else:
                    self.emgTST = None

            elif self.isMAT(f):
                mat_obj = matFile(f)
                self.Channels = mat_obj.labels
                # load TST
                self.emgTST = mat_obj.convertToTST()
            else:
                raise ValueError("Unsupported EMG file format: {}".format(f))
        except Exception as e:
            raise ValueError("Failed to load EMG file {}: {}".format(f, str(e)))

        # sanities
        if self.Channels is None or len(self.Channels) == 0:
            raise ValueError("No channels extracted from EMG file: {}".format(f))
        if self.emgTST is None:
            raise ValueError("Failed to convert EMG file to TimeSeriesTable: {}".format(f))

        # preserve original loaded signal for non-destructive analysis helpers
        self.rawTST = self.emgTST.copy()

        # update MVC TST
        self.emgMVCTST = timeSeriesTable(self.emgTST.fs, self.emgTST.labels)

    # set MVC file path
    def setMVCFile(self, channel, f):
        if len(self.Channels) == 0:
            raise ValueError("EMG channels are empty, please load EMG file first")

        if channel not in self.Channels:
            raise ValueError("Channel {} does not exist in EMG channels".format(channel))

        if f is None or len(str(f).strip()) == 0:
            raise ValueError("MVC file path is empty")
        if not os.path.isfile(f):
            raise ValueError("MVC file not found: {}".format(f))

        MVCTST = None
        # open file and load TST
        try:
            if self.isC3D(f):
                c3d_obj = c3dFile(f)
                MVCChannels = c3d_obj.analog.labels
                MVCTST = c3d_obj.analog.convertToTST()

            elif self.isMAT(f):
                mat_obj = matFile(f)
                MVCChannels = mat_obj.labels
                MVCTST = mat_obj.convertToTST()
            else:
                raise ValueError("Unsupported MVC file format: {}".format(f))
        except Exception as e:
            raise ValueError("Cannot open MVC file {}: {}".format(f, str(e)))

        # check if targetted channel exists in f
        if channel not in MVCChannels:
            raise ValueError(
                "Channel {} not found in MVC file {}".format(channel, f)
            )

        self.emgMVCTST[channel] = MVCTST[channel]
        self.mvcFilesMap[channel] = f

    def toXML(self):
        root = xmlElement("emg")
        root.addNode("path", self.emgFile)
        t = xmlElement("mvcPath")
        for chan, f in self.mvcFilesMap.items():
            t.addNode("chan", [xmlString(chan), xmlString(f)])
        root.addSubTree(t)
        root.addNode("enabledChannels", self.enabledChannels)
        # channel name migh have spaces or invalid chars,
        # so addDict is not applicable here
        t = xmlElement("chanMap")
        for old, new in self.chanMap.items():
            t.addNode("chan", [xmlString(old), xmlString(new)])
        root.addSubTree(t)
        return root

    @staticmethod
    def fromXML(xml):
        root = xml.find("emg")
        if root == None:
            return None
        emg_obj = emg()
        e = root.find("path")
        if e == None:
            return None
        emg_obj.emgFile = xmlStringParse(e.text)
        e = root.find("mvcPath")
        if e != None:
            for el in e:
                l = xmlStringParseList(el.text)
                emg_obj.mvcFilesMap[l[0]] = l[1]
        e = root.find("enabledChannels")
        if e != None and e.text != None:
            emg_obj.enabledChannels = set(xmlStringParseList(e.text))
        e = root.find("chanMap")
        if e != None:
            for el in e:
                l = xmlStringParseList(el.text)
                emg_obj.chanMap[l[0]] = l[1]
        return emg_obj

    def isProcessDone(self):
        return self.isprocessdone

    def setProcessDone(self):
        self.isprocessdone = True

    def startProcess(self):
        self.processCFG = emgConfigure()

    # return EMG configure file
    def getProcessConfig(self):
        return self.processCFG

    # assign EMG configure file
    def setProcessConfig(self, cfg):
        self.processCFG = cfg

    def __tryConfigStepImpl(self, tst, chan, step, crop_interval=None):
        if chan not in self.Channels:
            logger.error("Targetted channel not exist")
            raise Exception(logger.errstr)

        if step >= self.processCFG.size():
            logger.error("Selected step out of bound")
            raise Exception(logger.errstr)

        # apply functions
        type, tname = self.processCFG.getTypeInfo(step)
        cfg = self.processCFG[step]
        output = tst[chan]
        try:
            if type == emgConfigEnum.FILTER:
                if cfg.enable:
                    if cfg.type == emgFilterEnum.LOW_PASS:
                        output = tst.lowpass(chan, cfg.cutoff_l, cfg.order)
                    elif cfg.type == emgFilterEnum.BAND_PASS:
                        output = tst.bandpass(
                            chan, cfg.cutoff_l, cfg.cutoff_h, cfg.order
                        )
                    else:
                        output = None
            elif type == emgConfigEnum.FULL_W_RECT:
                if cfg.enable:
                    output = tst.rectification(chan)
            elif type == emgConfigEnum.DC_OFFSET:
                if cfg.enable:
                    output = tst.removeDC(chan)
            elif type == emgConfigEnum.ACTIVATION:
                output = tst.threholdDetection(
                    chan, cfg.threhold, cfg.n_above, cfg.n_below
                )
            elif type == emgConfigEnum.NORMALIZATION:
                if cfg.enable:
                    norm_type = getattr(cfg, 'norm_type', emgNormTypeEnum.MVC)
                    if norm_type == emgNormTypeEnum.TRIAL_MAX:
                        seg = self.getCroppedEnvelopeSegment(chan, crop_interval)
                        max_v = float(np.max(seg)) if len(seg) > 0 else 1.0
                        if max_v <= 0:
                            max_v = 1.0
                    else:
                        max_v = self.emgMVCTST.max(chan)
                    output = tst.normalization(chan, max_v)
            elif type == emgConfigEnum.SUMMARY:
                # Compute stats on the valid analysis segment (cropped if set).
                # Output signal is unchanged — summary never modifies the waveform.
                arr = np.asarray(tst[chan], dtype=float)
                if crop_interval is not None:
                    arr = self._crop_array(arr, tst.fs, crop_interval)
                if len(arr) > 0:
                    cfg.max = float(arr.max())
                    cfg.min = float(arr.min())
                    # IEMG: trapezoidal integration of absolute values over the segment
                    cfg.iemg = float(np.trapz(np.abs(arr), dx=1.0 / tst.fs))
                else:
                    cfg.max = cfg.min = cfg.iemg = 0.0
        except Exception as e:
            output = [0] * tst.size()
            import traceback
            error_msg = traceback.format_exc()
            logger.error(
                "cannot apply config to channel: {}, step: {}, Error Message: {}\n{}".format(
                    chan, tname, str(e), error_msg
                )
            )
        return output

    def tryConfigStep(self, chan, step, crop_interval=None):
        src = self.rawTST if self.rawTST is not None else self.emgTST
        return self.__tryConfigStepImpl(src, chan, step, crop_interval)

    def tryConfigStepTo(self, chan, step, crop_interval=None):
        src = self.rawTST if self.rawTST is not None else self.emgTST
        tst = src.copy()
        for i in range(0, step + 1):
            tst[chan] = self.__tryConfigStepImpl(tst, chan, i, crop_interval)
        return tst[chan]

    # Steps that must not be applied for inspection display (analysis-only).
    _DISPLAY_SKIP = frozenset({
        emgConfigEnum.NORMALIZATION,
        emgConfigEnum.ACTIVATION,
        emgConfigEnum.SUMMARY,
    })

    def is_envelope_configured(self):
        """Return True when the pipeline has rectification followed by an enabled LP filter.

        This combination produces a linear envelope, which is the required input
        for TKE-based onset/offset detection.
        """
        if self.processCFG is None:
            return False
        rect_seen = False
        for i in range(self.processCFG.size()):
            cfg = self.processCFG[i]
            if cfg.id == emgConfigEnum.FULL_W_RECT and getattr(cfg, 'enable', False):
                rect_seen = True
            elif rect_seen and cfg.id == emgConfigEnum.FILTER:
                if (getattr(cfg, 'enable', False) and
                        int(getattr(cfg, 'type', -1)) == int(emgFilterEnum.LOW_PASS)):
                    return True
        return False

    def get_kinematics_display(self, chan):
        """Return the full-trial preprocessed signal suitable for kinematics inspection.

        Replays every enabled pipeline step from processCFG that is NOT
        normalization / activation / summary, starting from rawTST and using
        no crop interval.  This mirrors what the user configured in the Time
        Domain analysis without any analysis-specific scaling or cropping.

        Falls back to the raw signal when processCFG has not been set yet.
        """
        src = self.rawTST if self.rawTST is not None else self.emgTST
        if src is None or chan not in (getattr(src, 'labels', None) or []):
            return np.array([])
        if self.processCFG is None:
            return np.asarray(src[chan], dtype=float)
        tst = src.copy()
        for i in range(self.processCFG.size()):
            step_type, _ = self.processCFG.getTypeInfo(i)
            if step_type in self._DISPLAY_SKIP:
                continue
            tst[chan] = self.__tryConfigStepImpl(tst, chan, i, crop_interval=None)
        return np.asarray(tst[chan], dtype=float)

    # process EMG and MVC using configure file
    def processWithConfigure(self, crop_interval=None):
        for chan in self.Channels:
            if chan not in self.enabledChannels:
                continue

            for step in range(0, self.processCFG.size()):
                self.emgTST[chan] = self.__tryConfigStepImpl(
                    self.emgTST, chan, step, crop_interval
                )
                self.emgMVCTST[chan] = self.__tryConfigStepImpl(
                    self.emgMVCTST, chan, step, crop_interval
                )

    # ------------------------------------------------------------------
    # Non-destructive analysis helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _crop_array(arr, fs, crop_interval):
        """Slice arr to [t_start_s, t_end_s]; returns arr unchanged if crop is None."""
        if crop_interval is None or len(arr) == 0:
            return arr
        t_start, t_end = crop_interval
        n = len(arr)
        i_start = max(0, min(n, int(t_start * fs)))
        i_end = max(0, min(n, int(t_end * fs)))
        if i_end <= i_start:
            return arr
        return arr[i_start:i_end]

    def getFreqSafeFilterStatus(self):
        """Describe what getFreqSafeSegment will actually apply right now,
        given this channel's process config -- the single source of truth
        both getFreqSafeSegment and any UI warning (see main.py's Frequency
        Analysis page) read from, so they can't drift out of sync.

        Returns {"has_config": bool, "dc_offset": bool, "band_pass": bool,
        "cutoff_l": float or None, "cutoff_h": float or None, "order": int
        or None}. "has_config" False means processWithConfigure has never
        been run for this participant at all (frequency analysis would run
        on the fully raw signal); "band_pass" False with "has_config" True
        means a band-pass step exists but is disabled or has invalid/unset
        cutoffs (e.g. left at the default 0 Hz) -- still worth flagging
        since it silently falls back to unfiltered data either way.
        """
        status = {
            "has_config": self.processCFG is not None,
            "dc_offset": False, "band_pass": False,
            "cutoff_l": None, "cutoff_h": None, "order": None,
        }
        if self.processCFG is None or self.rawTST is None:
            return status
        fs = self.rawTST.fs
        for step_idx in range(self.processCFG.size()):
            stype, _ = self.processCFG.getTypeInfo(step_idx)
            cfg = self.processCFG[step_idx]
            if stype == emgConfigEnum.DC_OFFSET and cfg.enable:
                status["dc_offset"] = True
            elif stype == emgConfigEnum.FILTER and cfg.enable and cfg.type == emgFilterEnum.BAND_PASS:
                status["cutoff_l"] = cfg.cutoff_l
                status["cutoff_h"] = cfg.cutoff_h
                status["order"] = cfg.order
                status["band_pass"] = (
                    0 < cfg.cutoff_l < fs / 2
                    and 0 < cfg.cutoff_h < fs / 2
                    and cfg.cutoff_l < cfg.cutoff_h
                )
        return status

    def getFreqSafeSegment(self, channel, crop_interval=None):
        """Return a cropped EMG segment valid for frequency analysis.

        Applies only DC removal and band-pass filtering from processCFG
        (see getFreqSafeFilterStatus -- by step type, not by index), then
        crops to crop_interval. Rectification, LP-envelope, normalization,
        and summary steps are intentionally skipped — they corrupt spectral
        content.

        Falls back to raw cropped signal if processCFG is not yet set, or
        if the band-pass step is disabled/has invalid cutoffs -- see
        getFreqSafeFilterStatus for a way to detect and warn about that
        before trusting the result.

        Returns a numpy ndarray.
        """
        if self.rawTST is None or channel not in self.rawTST.labels:
            return np.array([])
        fs = self.rawTST.fs
        arr = np.array(self.rawTST[channel], dtype=float)
        status = self.getFreqSafeFilterStatus()
        try:
            if status["dc_offset"]:
                arr = arr - arr.mean()
            if status["band_pass"]:
                sos = _sig.butter(status["order"], [status["cutoff_l"], status["cutoff_h"]],
                                  btype='bp', fs=fs, output='sos')
                arr = _sig.sosfiltfilt(sos, arr)
        except Exception as e:
            logger.warning("getFreqSafeSegment for '{}': {}".format(channel, e))
        return self._crop_array(arr, fs, crop_interval)

    def getCroppedEnvelopeSegment(self, channel, crop_interval=None):
        """Return a cropped envelope-processed EMG segment for time-domain analysis.

        Applies DC removal, band-pass, full-wave rectification, and LP envelope
        filter from processCFG (by step type), then crops to crop_interval.
        Normalization and summary steps are skipped — normalization (MVC or
        trial-max) is the caller's responsibility and applies to time-domain
        amplitude workflows only.

        Returns a numpy ndarray.
        """
        if self.rawTST is None or channel not in self.rawTST.labels:
            return np.array([])
        fs = self.rawTST.fs
        arr = np.array(self.rawTST[channel], dtype=float)
        if self.processCFG is not None:
            for step_idx in range(self.processCFG.size()):
                stype, _ = self.processCFG.getTypeInfo(step_idx)
                cfg = self.processCFG[step_idx]
                try:
                    if stype == emgConfigEnum.DC_OFFSET and cfg.enable:
                        arr = arr - arr.mean()
                    elif stype == emgConfigEnum.FILTER and cfg.enable:
                        if (cfg.type == emgFilterEnum.BAND_PASS
                                and 0 < cfg.cutoff_l < fs / 2
                                and 0 < cfg.cutoff_h < fs / 2
                                and cfg.cutoff_l < cfg.cutoff_h):
                            sos = _sig.butter(cfg.order, [cfg.cutoff_l, cfg.cutoff_h],
                                              btype='bp', fs=fs, output='sos')
                            arr = _sig.sosfiltfilt(sos, arr)
                        elif (cfg.type == emgFilterEnum.LOW_PASS
                              and 0 < cfg.cutoff_l < fs / 2):
                            sos = _sig.butter(cfg.order, cfg.cutoff_l,
                                              btype='lp', fs=fs, output='sos')
                            arr = _sig.sosfiltfilt(sos, arr)
                    elif stype == emgConfigEnum.FULL_W_RECT and cfg.enable:
                        arr = np.abs(arr)
                    # NORMALIZATION, SUMMARY → skipped; normalization is caller's responsibility
                except Exception as e:
                    logger.warning("getCroppedEnvelopeSegment step {}: {}".format(step_idx, e))
        return self._crop_array(arr, fs, crop_interval)
