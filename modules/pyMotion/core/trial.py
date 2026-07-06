import numpy as np


class TrialEvent:
    """A single labeled event on a trial timeline (e.g. heel strike, toe off).

    source: optional free-form provenance tag (e.g. "plate"/"marker" for a
    Gait Analysis event -- see gait_analysis_dialog.py's _apply_gait_events).
    Not part of equality/identity -- two events at the same time/label/
    context are still "the same event" regardless of how they were tagged.
    """

    __slots__ = ("time_s", "label", "context", "source")

    def __init__(self, time_s: float, label: str = "", context: str = "", source: str = ""):
        self.time_s = float(time_s)
        self.label = label.strip()
        self.context = context.strip()
        self.source = source.strip()

    def __repr__(self):
        return "TrialEvent({:.3f}s, {!r}, {!r}, source={!r})".format(
            self.time_s, self.label, self.context, self.source
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

    __slots__ = ("plate_id", "fs", "Fx", "Fy", "Fz", "Mx", "My", "Mz", "corners", "Cx", "Cy")

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
        Cx=None,
        Cy=None,
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
        # Some systems (e.g. AMTI/Bertec-style "COP1X"/"COP1Y" analog channels)
        # export the center of pressure already computed, in lab-frame mm --
        # more accurate than deriving it from Mx/My since it accounts for the
        # plate's true origin offset. None when the C3D only has raw
        # force/moment channels (COP must then be derived, see gait_events.py).
        self.Cx = Cx
        self.Cy = Cy

    def __repr__(self):
        has_geo = self.corners is not None
        return "ForcePlateGroup(plate_id={}, fs={}, n={}, corners={})".format(
            self.plate_id, self.fs, len(self.Fx), has_geo
        )
