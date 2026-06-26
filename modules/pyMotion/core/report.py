from .emg import *
from .freq_analysis import *
from .person import *
from .workspace import *
from .kinematic import *
from .advance_analysis import *
from .statistic import *
from .xml import *


class report:
    def __init__(self, person, emg, crop_interval=None):
        self.root = None
        self.fpath = None

        if person is None or emg is None:
            return

        self.root = xmlElement("report")
        self.root.addSubTree(person.toXML())
        # get a copy of emgTST, filter out disabled signal
        filteredTST = emg.emgTST.copy()
        filteredTST.setname("EMG")

        all_chan = filteredTST.channels.copy()
        for c in all_chan:
            if c not in emg.enabledChannels:
                filteredTST.removeChannel(c)

        # emg Time Series Data
        # MVC is saved in splited file
        self.root.addSubTree(filteredTST.toXML())
        # emg process configuration
        self.root.addSubTree(emg.processCFG.toXML())

        # Per-channel time-domain summary: max, min, iemg over the analysis segment.
        # Replaces the old emgStatistic block which computed dozens of metrics on
        # the full (uncropped) signal — many of which were removed from the pipeline.
        self.root.addSubTree(
            report._build_time_summary(filteredTST, crop_interval)
        )

        # Frequency-domain metrics are NOT included here.
        # They depend on the original raw signal and a user-chosen analysis window,
        # both of which are unavailable at report-load time (rawTST may be stale).
        # Use the dedicated Freq Analysis module + "Export Freq Results" button instead.

    @staticmethod
    def _build_time_summary(tst, crop_interval):
        """Per-channel max, min, IEMG over the analysis segment of the processed signal."""
        root = xmlElement("timeSummary")
        fs = tst.fs
        for chan in tst.channels:
            arr = np.array(tst[chan], dtype=float)
            if crop_interval is not None:
                i_s = max(0, int(crop_interval[0] * fs))
                i_e = max(0, min(len(arr), int(crop_interval[1] * fs)))
                arr = arr[i_s:i_e]
            if len(arr) > 0:
                max_v  = float(arr.max())
                min_v  = float(arr.min())
                iemg_v = float(np.trapz(np.abs(arr), dx=1.0 / fs))
            else:
                max_v = min_v = iemg_v = 0.0
            chan_el = xmlElement("channel")
            chan_el.addNode("name", xmlString(chan))
            chan_el.addNode("max",  xmlString(round(max_v,  6)))
            chan_el.addNode("min",  xmlString(round(min_v,  6)))
            chan_el.addNode("iemg", xmlString(round(iemg_v, 6)))
            root.addSubTree(chan_el)
        return root

    @staticmethod
    def _build_freq_stats(emg, enabled_channels, crop_interval):
        """Per-channel MNF and MDF from the frequency-safe signal path."""
        root = xmlElement("freqAnalysis")
        fs = emg.getfs()
        for chan in enabled_channels:
            arr = emg.getFreqSafeSegment(chan, crop_interval)
            if len(arr) == 0:
                continue
            tst = timeSeriesTable(fs, [chan], [arr])
            freq_lin, v_lin = tst.fft(chan)
            total = float(np.sum(v_lin))
            if total > 0:
                mnf = float(np.dot(freq_lin, v_lin) / total)
                cum = np.cumsum(v_lin)
                idx = min(len(freq_lin) - 1,
                          int(np.searchsorted(cum, cum[-1] / 2, side="right")))
                mdf = float(freq_lin[idx])
            else:
                mnf = mdf = 0.0
            chan_el = xmlElement("channel")
            chan_el.addNode("name", xmlString(chan))
            chan_el.addNode("mnf",  xmlString(round(mnf, 4)))
            chan_el.addNode("mdf",  xmlString(round(mdf, 4)))
            root.addSubTree(chan_el)
        return root

    def toXML(self):
        return self.root

    def getPath(self):
        return self.fpath

    # load report, return tst
    def async_load(self):
        if self.fpath is None:
            return
        xml = xmlReader(self.fpath).get()
        if xml == None:
            logger.error("report: file is empty")
            return -1
        root = xml.find("report")
        if root == None:
            logger.error("report: no report found")
            return -1
        self.root = xml
        # this assumes only one tst is included
        return timeSeriesTable.fromXML(root)

    def writeXML(self, file):
        self.fpath = file
        xmlWriter(file, self.root).write()
