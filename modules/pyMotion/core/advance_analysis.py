from .timeSeriesTable import *
from .synergy import MusclesyneRgies


class advance_analysis:
    """Holds one muscle-synergy decomposition (MusclesyneRgies) per trial.

    result.M is the muscle weighting matrix, result.P is the timing
    (activation) pattern -- see synergy.py for the full field list.
    """

    def __init__(self):
        self.data = {}  # trial_name -> MusclesyneRgies

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value: MusclesyneRgies):
        self.data[key] = value

    def __delitem__(self, key):
        if key in self.data:
            del self.data[key]

    def __contains__(self, key):
        return key in self.data

    def __missing__(self, key):
        return None
