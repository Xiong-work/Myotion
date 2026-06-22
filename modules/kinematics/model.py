class Model:
    """
    Model class for kinematics module.
    model defines a kinematic data set and provides methods to manipulate it. and current frame of the model
    """

    def __init__(self, data) -> None:
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

    def kinematic_frame(self, frame):
        return self.kinematic[frame]

    def kinematic_frames(self):
        return self.kinematic.length

    def kinematic_frame_rate(self):
        return self.kinematic.data.fs

    def total_time(self):
        return self.kinematic.length / self.kinematic.data.fs
