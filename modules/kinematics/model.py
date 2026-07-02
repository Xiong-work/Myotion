# Nominal playback granularity (frames/sec) when a participant has EMG but no
# usable kinematics clock. Arbitrary but reasonable: fine enough for smooth
# scrubbing, far coarser than raw EMG sample rates (avoids a multi-kHz timer).
_EMG_ONLY_PLAYBACK_FPS = 100.0


class Model:
    """
    Model class for kinematics module.
    model defines a kinematic data set and provides methods to manipulate it. and current frame of the model
    """

    def __init__(self, data) -> None:
        self.profile = data  # workspace.profile — owns crop_interval and extra_events
        self.kinematic = data.kinematic
        self.emg = data.emg
        # Reference to profile.extra_events so the controller can append without
        # going through main.py — workspace.saveWorkSpace() will serialize it.
        self.extra_events = getattr(data, "extra_events", [])
        # Combined, time-sorted view: C3D events + user-created events.
        self.events = sorted(
            list(getattr(data.kinematic, "events", [])) + list(self.extra_events),
            key=lambda e: e.time_s,
        )
        # Force plate data from analog channels (empty list if none in file)
        self.force_plates = getattr(data.kinematic, "force_plates", [])
        # True once kinematic has real marker data with a usable frame clock.
        # EMG-only participants (Kinematics Inspection's "no kinematics" flow)
        # fall back to the EMG's own sample rate/duration as the master clock
        # below, and the 3D pane shows its "no model" placeholder instead.
        self.has_kinematics = bool(
            self.kinematic.isValid() and getattr(self.kinematic, "point_fs", 0) > 0
        )

    def kinematic_frame(self, frame):
        return self.kinematic[frame]

    def kinematic_frames(self):
        if self.has_kinematics:
            return self.kinematic.length
        return max(1, int(round(self._emg_duration_s() * _EMG_ONLY_PLAYBACK_FPS)))

    def kinematic_frame_rate(self):
        if self.has_kinematics:
            return self.kinematic.point_fs
        return _EMG_ONLY_PLAYBACK_FPS

    def total_time(self):
        if self.has_kinematics:
            return self.kinematic.length / self.kinematic.point_fs
        return self._emg_duration_s()

    def _emg_duration_s(self):
        tst = self.emg.emgTST
        return tst.time if tst is not None else 0.0
