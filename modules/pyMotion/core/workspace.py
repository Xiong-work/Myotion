from .emg import *
from .report import *
from .freq_analysis import *
from .person import *
from .timeSeriesTable import *
from .fuzzMatch import *
from .logger import *
import os
import threading
import numpy as np
from ctypes import c_int, addressof


PROJ_EXT = ".myo"
"""
Workspace maintained a set of people with data
"""


class workspace:
    # data profile of each participant
    class profile:
        def __init__(self, emg, kin):
            self.emg = emg
            self.report = None
            self.kinematic = kin
            self.loading = False
            self.extra_events = []  # user-created TrialEvents, persisted to workspace XML
            self.crop_interval = None  # (t_start_s, t_end_s) or None; shared by EMG and kinematics

        def isLoading(self):
            return self.loading

        def isEMGReady(self):
            if self.loading:
                return False
            return self.emg.isProcessDone()

        def isReportReady(self):
            if self.loading:
                return False
            return self.report != None

        def getDataStatus(self):
            return self.isLoading(), self.emg.isEMGReady() and self.isReportReady()

        def toXML(self):
            root = xmlElement("profile")
            root.addSubTree(self.emg.toXML())
            if self.report != None:
                # add report location to xml
                root.addNode("report", xmlString(self.report.getPath()))
            if self.extra_events:
                events_node = xmlElement("events")
                for ev in self.extra_events:
                    e = xmlElement("event")
                    e.addNode("time", xmlString(ev.time_s))
                    e.addNode("label", xmlString(ev.label))
                    e.addNode("context", xmlString(ev.context))
                    events_node.addSubTree(e)
                root.addSubTree(events_node)
            if self.crop_interval is not None:
                ci = xmlElement("crop_interval")
                ci.addNode("start", xmlString(float(self.crop_interval[0])))
                ci.addNode("end", xmlString(float(self.crop_interval[1])))
                root.addSubTree(ci)
            return root

        @staticmethod
        def fromXML(xml):
            root = xml.find("profile")
            if root == None:
                logger.error("loadWorkSpace: no profile found")
                return None

            profile_obj = workspace.profile(None, None)
            # load emg
            emg_obj = emg.fromXML(root)
            report_obj = None

            # if report exists, use report data
            # otherwise, load emg data from file
            report_path = root.find("report")
            if report_path != None:
                # load report data to emg
                report_obj = report(None, None)
                report_obj.fpath = xmlStringParse(report_path.text)

            profile_obj.loading = True
            profile_obj.emg = emg_obj
            profile_obj.kin = None
            profile_obj.report = report_obj
            # Restore user-created events from XML
            profile_obj.extra_events = []
            events_node = root.find("events")
            if events_node is not None:
                for ev_el in events_node:
                    try:
                        t_el = ev_el.find("time")
                        l_el = ev_el.find("label")
                        c_el = ev_el.find("context")
                        t = float(t_el.text) if t_el is not None and t_el.text else 0.0
                        l = l_el.text.strip() if l_el is not None and l_el.text else ""
                        c = c_el.text.strip() if c_el is not None and c_el.text else ""
                        profile_obj.extra_events.append(TrialEvent(t, l, c))
                    except Exception:
                        pass
            # Restore crop interval from XML
            profile_obj.crop_interval = None
            ci_node = root.find("crop_interval")
            if ci_node is not None:
                try:
                    s_el = ci_node.find("start")
                    e_el = ci_node.find("end")
                    if (s_el is not None and e_el is not None
                            and s_el.text and e_el.text):
                        profile_obj.crop_interval = (float(s_el.text), float(e_el.text))
                except Exception:
                    pass
            return profile_obj

    class reportEMGConfig:
        def __init__(self):
            self.csv = True
            self.c3d = False
            self.mat = False

        def outputMVC(self, st):
            self.mvc = st

        def outputCSV(self, st):
            self.csv = st

        def outputC3D(self, st):
            self.c3d = st

        def outputMAT(self, st):
            self.mat = st

    def __init__(self, path, name):
        self.name = name
        self.fpath = path
        # list of participants in workspace
        self.participants = []
        self.filtered_participants = []

        # data of participants, hash:profile
        self.profileList = {}

        # list of saved emg config
        self.saved_emgconfig = {}

        # fuzzy match for matching strings
        self.fuzzs = {}
        # name match for mvc_file_name -> channel
        self.fuzzs["mvc_file_to_channel"] = fuzzMatch()
        # name match for channel name -> joint
        self.fuzzs["chan_to_joint"] = fuzzMatch()

        # report - EMG config
        self.reportemgconfig = self.reportEMGConfig()

        # loading thread
        self.emgloaderthread = None
        self.emgloaderstop = False

    def __del__(self):
        # stop emg loader thread
        if self.emgloaderthread:
            self.emgloaderstop = True
            self.emgloaderthread.join()

    def clear(self):
        self.participants.clear()
        self.profileList.clear()
        self.kinematicList.clear()
        self.saved_emgconfig.clear()

    # check if person exist
    def hasParticipant(self, person):
        return person in self.participants

    def getParticipants(self):
        return self.participants

    def getParticipantWithName(self, name):
        for s in self.participants:
            if name == s.name:
                return s
        return None

    def getparticipantStringList(self):
        return [p.name for p in self.participants]

    def findParticipant(self, name):
        for s in self.participants:
            if name == s.name:
                return s
        return None

    def getFilteredParticipants(self, regex):
        to_be_ret = []
        if len(regex) == 0:
            return self.participants

        for p in self.participants:
            if re.search(regex, p.name) is not None:
                to_be_ret.append(p)

        return to_be_ret

    # use person as key to access profile
    def __getitem__(self, person):
        if not self.hasParticipant(person):
            return self.__missing__(person)
        # return profile
        return self.profileList[person.name]

    def __delitem__(self, key):
        return

    def __missing__(self, key):
        return

    def addParticipant(self, person, emg, kin):
        if self.hasParticipant(person):
            return -1

        self.participants.append(person)
        self.profileList[person.name] = self.profile(emg, kin)
        return 0

    def profileStatusList(self):
        return [self.profileList[id].getDataStatus() for p, id in self.participants]

    def saveEMGConfigure(self, person, cfgname):
        if not self.hasParticipant(person):
            return -1
        self.saved_emgconfig[cfgname] = (
            self.profileList[person.name].emg.getProcessConfig().copy()
        )
        return 0

    def getEMGConfigures(self):
        return self.saved_emgconfig

    def hasEMGConfigFile(self, name):
        return False

    def genReport(self, person):
        if not self.hasParticipant(person):
            return
        profile = self.profileList[person.name]
        profile.report = report(person, profile.emg,
                                crop_interval=profile.crop_interval)
        profile.emg.setProcessDone()

    def saveReport(self, person, path):
        if not self.hasParticipant(person):
            return
        profile = self.profileList[person.name]
        if profile.report is None:
            return

        # each participant gets their own sub-folder under the workspace directory
        participant_dir = os.path.join(path, person.name)
        os.makedirs(participant_dir, exist_ok=True)

        # save "rpt" report
        report_name = os.path.join(participant_dir, person.name + ".rpt")
        profile.report.writeXML(report_name)

        # save processed EMG as csv
        if self.reportemgconfig.csv:
            emg = profile.emg
            tst = emg.emgTST
            fs = tst.fs

            # channels that were selected for analysis (in original order)
            available = set(tst.data.keys())
            enabled_chans = [
                c for c in emg.Channels
                if c in emg.enabledChannels and c in available
            ]

            # analysis segment bounds
            ci = profile.crop_interval
            if ci is not None:
                seg_start, seg_end = ci[0], ci[1]
            else:
                seg_start, seg_end = 0.0, tst.time

            header = (
                "# Sample frequency: {} Hz\n"
                "# Analysis segment: {:.3f} s - {:.3f} s\n"
                "# Channels: {}\n"
            ).format(fs, seg_start, seg_end, ", ".join(enabled_chans))

            # Build DataFrame with enabled channels only, then crop to the analysis
            # segment.  Filtering ran on the full signal (in processWithConfigure),
            # so slicing here avoids filter transients at the cut point.
            full_df = tst.toPandasFrame()[enabled_chans]
            n_total = len(full_df)
            i_start = max(0, min(n_total, int(round(seg_start * fs))))
            i_end   = max(0, min(n_total, int(round(seg_end   * fs))))
            emgdf = full_df.iloc[i_start:i_end].reset_index(drop=True).copy()
            # Time column preserves absolute trial timestamps
            n_out = len(emgdf)
            time_arr = np.linspace(seg_start, seg_end, n_out) if n_out > 0 else np.array([])
            emgdf.insert(0, "Time (s)", time_arr)

            csv_name = os.path.join(
                participant_dir, person.name + "_emg_processed.csv"
            )
            with open(csv_name, "w", encoding="utf-8", newline="") as f:
                f.write(header)
                emgdf.to_csv(f, index=False)

            # Per-channel summary metrics — one row per channel, all 13 TD metrics.
            # Written at report-save time so the Stats module can load them instantly
            # without reprocessing the full signal.
            import scipy.stats as _ss
            import pandas as _pd
            _summary_rows = []
            for _col in enabled_chans:
                _arr = emgdf[_col].to_numpy(dtype=float)
                if len(_arr) == 0:
                    continue
                _zc = int(np.sum(np.diff(np.sign(_arr)) != 0))
                _n = len(_arr)
                _summary_rows.append({
                    'Participant': person.name,
                    'Channel':    _col,
                    'min':        round(float(_arr.min()),                            6),
                    'max':        round(float(_arr.max()),                            6),
                    'mean':       round(float(_arr.mean()),                           6),
                    'median':     round(float(np.median(_arr)),                       6),
                    'std':        round(float(_arr.std(ddof=1)) if _n > 1 else 0.0,  6),
                    'var':        round(float(_arr.var(ddof=1)) if _n > 1 else 0.0,  6),
                    'ptp':        round(float(np.ptp(_arr)),                          6),
                    'zc':         _zc,
                    'auc':        round(float(np.trapz(np.abs(_arr), dx=1.0 / fs)),  6),
                    'rms':        round(float(np.sqrt(np.mean(_arr ** 2))),           6),
                    'mav':        round(float(np.mean(np.abs(_arr))),                 6),
                    'skewness':   round(float(_ss.skew(_arr)),                        6),
                    'kurtosis':   round(float(_ss.kurtosis(_arr)),                    6),
                })
            if _summary_rows:
                _shdr = (
                    "# Participant: {}\n"
                    "# Sample frequency: {} Hz\n"
                    "# Analysis segment: {:.3f} s - {:.3f} s\n"
                ).format(person.name, fs, seg_start, seg_end)
                _summary_name = os.path.join(
                    participant_dir, person.name + "_summary.csv"
                )
                with open(_summary_name, "w", encoding="utf-8", newline="") as f:
                    f.write(_shdr)
                    _pd.DataFrame(_summary_rows).to_csv(f, index=False)

            # MVC csv — only channels that have actual MVC data loaded
            if emg.emgMVCTST and len(emg.emgMVCTST.data) > 0:
                mvc_tst = emg.emgMVCTST
                mvc_chans = [
                    c for c in enabled_chans
                    if mvc_tst.hasChannel(c) and len(mvc_tst[c]) > 0
                ]
                if mvc_chans:
                    mvc_fs = mvc_tst.fs
                    mvc_header = (
                        "# Sample frequency: {} Hz\n"
                        "# MVC trial (full recording, no crop applied)\n"
                        "# Channels: {}\n"
                    ).format(mvc_fs, ", ".join(mvc_chans))

                    # Build DataFrame directly from selected channels only.
                    # mvc_tst.toPandasFrame() can fail when some channels have data
                    # and others are empty (emgMVCTST.n stays 0 after initialisation
                    # so getLinspace() returns an empty array regardless of data length).
                    import pandas as _pd
                    mvcdf = _pd.DataFrame(
                        {c: _pd.Series(mvc_tst[c]) for c in mvc_chans}
                    )
                    n_mvc = len(mvcdf)
                    mvc_time = (
                        np.linspace(0.0, n_mvc / mvc_fs, n_mvc)
                        if n_mvc > 0 else np.array([])
                    )
                    mvcdf.insert(0, "Time (s)", mvc_time)

                    mvccsv_name = os.path.join(
                        participant_dir, person.name + "_mvc_processed.csv"
                    )
                    with open(mvccsv_name, "w", encoding="utf-8", newline="") as f:
                        f.write(mvc_header)
                        mvcdf.to_csv(f, index=False)

    def saveWorkSpace(self, path):
        filename = os.path.normpath(path + "/" + self.name + PROJ_EXT)
        root = xmlElement("workspace")
        root.addNode("directory", xmlString(self.fpath))
        # save pariticpant list, emg, and filepath

        for person in self.participants:
            p = xmlElement("participant")
            p.addSubTree(person.toXML())
            p.addSubTree(self.profileList[person.name].toXML())
            root.addSubTree(p)

        # save fuzz match
        mvc_file_to_channel = xmlElement("mvc_file_to_channel")
        mvc_file_to_channel.addSubTree(self.fuzzs["mvc_file_to_channel"].toXML())
        root.addSubTree(mvc_file_to_channel)
        chan_to_joint = xmlElement("chan_to_joint")
        chan_to_joint.addSubTree(self.fuzzs["chan_to_joint"].toXML())
        root.addSubTree(chan_to_joint)

        # saved emg configuration
        p = xmlElement("savedEMGConfig")
        for name, cfg in self.saved_emgconfig.items():
            e = xmlElement("config")
            e.addNode("name", xmlString(name))
            e.addSubTree(cfg.toXML())
            p.addSubTree(e)
        root.addSubTree(p)

        writer = xmlWriter(filename, root)
        writer.write()

    @staticmethod
    def loadWorkSpace(path, file, doneCallback, errorCallback):
        logger.info("loadWorkSpace: loading from {}/{}...".format(path, file))
        proj_name = file[: -len(PROJ_EXT)]
        workspace_obj = workspace(None, proj_name)

        # parse myo file
        xml = xmlReader(path + "/" + file).get()
        if xml == None:
            logger.error("loadWorkSpace: file is empty")
            return -1
        root = xml.find("workspace")
        if root == None:
            logger.error("loadWorkSpace: no workspace found")
            return -1

        # project direction
        e = root.find("directory")
        if e is None:
            logger.error("loadWorkSpace: no directory found")
            return -1
        workspace_obj.fpath = xmlStringParse(e.text)
        logger.info("loadWorkSpace: project path {}".format(workspace_obj.fpath))
        # participants
        pending_load = {}
        for el in root.iter("participant"):
            person_obj, profile_obj = person.fromXML(el), workspace.profile.fromXML(el)
            if person_obj is None or profile_obj is None:
                logger.error("loadWorkSpace: failed to parse participant, continue")
                continue

            logger.info("loadWorkSpace: loading participant {}".format(person_obj.name))
            workspace_obj.participants.append(person_obj)
            workspace_obj.profileList[person_obj.name] = profile_obj
            pending_load[person_obj.name] = profile_obj

        # fuzz match
        e = root.find("mvc_file_to_channel")
        if e:
            workspace_obj.fuzzs["mvc_file_to_channel"] = fuzzMatch.fromXML(e)

        e = root.find("chan_to_joint")
        if e:
            workspace_obj.fuzzs["chan_to_joint"] = fuzzMatch.fromXML(e)

        # saved emg configuration
        e = root.find("savedEMGConfig")
        if e:
            for el in e:
                n = el.find("name")
                if n == None or n.text == None:
                    continue
                workspace_obj.saved_emgconfig[
                    xmlStringParse(n.text)
                ] = emgConfigure.fromXML(el)

        # spawn a loading thread to load emg
        if len(pending_load):
            workspace_obj.emgloaderthread = threading.Thread(
                target=workspace_obj.emgAsyncLoader, args=(pending_load, doneCallback, errorCallback)
            )
            workspace_obj.emgloaderthread.start()
        return workspace_obj

    def emgAsyncLoader(self, pending_load, doneCallback, errorCallback):
        for name, profile in pending_load.items():
            try:
                emg_file = profile.emg.emgFile
                original_exists = (
                    emg_file is not None
                    and len(str(emg_file).strip()) > 0
                    and os.path.isfile(str(emg_file))
                )

                if original_exists:
                    # Always load from the original source file when it is available.
                    # This preserves the true rawTST so the pipeline sees unprocessed
                    # signal when the user clicks Signal Processing again.
                    # The .rpt report is kept for export only — not as the signal source.
                    logger.info(
                        "emg async loader: loading profile {} from original file {}".format(
                            name, emg_file
                        )
                    )
                    asyncio.run(profile.emg.async_load())
                    if profile.report is not None:
                        # Flag that this participant was previously processed
                        profile.emg.isprocessdone = True
                elif profile.report is not None:
                    # Original file is missing — fall back to report data
                    logger.info(
                        "emg async loader: original file missing, loading {} from report {}".format(
                            name, profile.report.fpath
                        )
                    )
                    tst = profile.report.async_load()
                    profile.emg.load_from_report(tst)
                else:
                    logger.info(
                        "emg async loader: loading profile {} from {}".format(
                            name, emg_file
                        )
                    )
                    asyncio.run(profile.emg.async_load())
                # load kinematics data
                profile.kinematic = kinematic(profile.emg.emgFile)
                logger.info("emg async loader: done")
                profile.loading = False
                doneCallback()
            except Exception as e:
                logger.error(
                    f"emg async loader: failed to load {name} with {profile.emg.emgFile}, error: {str(e)}"
                )
                profile.loading = False
                errorCallback(
                    f"Cannot load participant {name} from {profile.emg.emgFile}: {str(e)}"
                )
                continue

            if self.emgloaderstop:
                logger.info("emg async loader: stopping")
                return
        self.emgloaderthread = None

    def addChanToMVCFileMap(self, channel, mvc_file_name):
        self.fuzzs["mvc_file_to_channel"].addPair(channel, mvc_file_name)

    def matchChanToMVCFile(self, channel, mvc_file_names, lower_bound=0):
        return self.fuzzs["mvc_file_to_channel"].match(
            channel, mvc_file_names, lower_bound
        )

    def addChanToJointMap(self, channel, joint):
        self.fuzzs["chan_to_joint"].addPair(channel, joint)

    def matchChanToJoint(self, channel, joints, lower_bound=0):
        return self.fuzzs["chan_to_joint"].match(channel, joints, lower_bound)
