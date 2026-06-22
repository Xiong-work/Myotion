import numpy as np


class TrialEvent:
    """A single labeled event on a trial timeline (e.g. heel strike, toe off)."""

    __slots__ = ("time_s", "label", "context")

    def __init__(self, time_s: float, label: str = "", context: str = ""):
        self.time_s = float(time_s)
        self.label = label.strip()
        self.context = context.strip()

    def __repr__(self):
        return "TrialEvent({:.3f}s, {!r}, {!r})".format(
            self.time_s, self.label, self.context
        )

    def __eq__(self, other):
        return (
            isinstance(other, TrialEvent)
            and self.time_s == other.time_s
            and self.label == other.label
            and self.context == other.context
        )


class ForcePlateGroup:
    """Grouped force plate analog channels for one physical plate.

    Populated in Phase 3.  Defined here so downstream code can type-check
    against it without importing from the kinematics module.
    """

    __slots__ = ("plate_id", "fs", "Fx", "Fy", "Fz", "Mx", "My", "Mz", "corners")

    def __init__(
        self,
        plate_id: int,
        fs: float,
        Fx: np.ndarray,
        Fy: np.ndarray,
        Fz: np.ndarray,
        Mx: np.ndarray,
        My: np.ndarray,
        Mz: np.ndarray,
        corners=None,
    ):
        self.plate_id = plate_id
        self.fs = fs
        self.Fx = Fx
        self.Fy = Fy
        self.Fz = Fz
        self.Mx = Mx
        self.My = My
        self.Mz = Mz
        # ndarray shape [4, 3] — 4 corner positions in C3D lab frame (mm), or None
        self.corners = corners

    def __repr__(self):
        has_geo = self.corners is not None
        return "ForcePlateGroup(plate_id={}, fs={}, n={}, corners={})".format(
            self.plate_id, self.fs, len(self.Fx), has_geo
        )
