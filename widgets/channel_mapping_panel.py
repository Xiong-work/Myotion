"""
widgets/channel_mapping_panel.py — reusable channel enable/rename/MVC-file
mapping table for batch EMG workflows (Batch Import's "map once" step and
the post-hoc "Edit Mapping" action for already-loaded participants).

Structurally similar to EMGAddWindow's per-participant channel table in
main.py, but built fresh rather than shared with it -- that wizard's table
is tightly wired into its own self.emg/self.mvcfiles instance state and
self.tr() translation strings; duplicating the table-building logic here is
safer than threading a new "batch mode" through a working, unrelated dialog.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem, QComboBox, QCompleter, QHeaderView,
)

from modules.pyMotion.core.muscleName import muscleName
from modules.pyMotion.core.muscle_guess import _guess_muscle_from_channel


class ChannelMappingPanel(QWidget):
    """channels: list[str] of raw channel names shared across the cohort.
    mvc_files: list[str] of MVC file basenames available to assign from
        (batch mode assumes one shared MVC-file-naming convention across
        the cohort, unlike the single-add wizard's per-participant file list).
    workspace: used for chan_to_joint / mvc_file_to_channel fuzzy-match
        history, same infrastructure the single-add wizard already builds up.
    initial: optional (enabled: iterable[str], muscle: dict, mvc_file: dict)
        to pre-fill from -- either an existing BatchConfig.channel_mapping or
        an already-loaded participant's live emg state. When omitted, all
        channels start enabled and muscle/MVC assignments are auto-guessed.
    """

    def __init__(self, channels, mvc_files, workspace, initial=None, parent=None):
        super().__init__(parent)
        self.channels = list(channels)
        self.mvc_files = list(mvc_files)
        self.workspace = workspace
        self.enabled = set(initial[0]) if initial else set(self.channels)
        self.muscle = dict(initial[1]) if initial else {}
        self.mvc_map = dict(initial[2]) if initial else {}

        if not (initial and initial[1]):
            self._auto_guess_muscles()
        if not (initial and initial[2]):
            self._auto_guess_mvc()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(len(self.channels), 3, self)
        self.table.setHorizontalHeaderLabels(["Channel", "Muscle", "MVC file"])
        for i, chan in enumerate(self.channels):
            item = QTableWidgetItem(chan)
            item.setFlags(
                (item.flags() & ~Qt.ItemFlag.ItemIsEditable) | Qt.ItemFlag.ItemIsUserCheckable
            )
            item.setCheckState(
                Qt.CheckState.Checked if chan in self.enabled else Qt.CheckState.Unchecked
            )
            self.table.setItem(i, 0, item)
            self.table.setCellWidget(i, 1, self._muscle_combo(chan))
            self.table.setCellWidget(i, 2, self._mvc_combo(chan))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)

    # ------------------------------------------------------------------
    # Auto-guessing (mirrors EMGAddWindow.applyFuzzMatchOnJoint / OnMVC)
    # ------------------------------------------------------------------

    def _auto_guess_muscles(self):
        for c in self.channels:
            guess = _guess_muscle_from_channel(c)
            if guess is None and self.workspace is not None:
                candidates = self.workspace.matchChanToJoint(c, muscleName.short, lower_bound=50)
                if candidates:
                    guess = candidates[0][0]
            if guess is not None:
                self.muscle[c] = guess

    def _auto_guess_mvc(self):
        if not self.mvc_files or self.workspace is None:
            return
        if len(self.mvc_files) == 1:
            for c in self.channels:
                self.mvc_map[c] = self.mvc_files[0]
            return
        for c in self.channels:
            candidates = self.workspace.matchChanToMVCFile(c, self.mvc_files, lower_bound=50)
            if candidates:
                self.mvc_map[c] = candidates[0][0]

    # ------------------------------------------------------------------
    # Table cell widgets
    # ------------------------------------------------------------------

    def _muscle_combo(self, chan):
        combo = QComboBox()
        combo.setEditable(True)
        for j in muscleName.short:
            combo.addItem(muscleName.getConcatName(j))
        completer = QCompleter([muscleName.getConcatName(j) for j in muscleName.short], combo)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        combo.setCompleter(completer)
        if chan in self.muscle:
            combo.setCurrentText(muscleName.getConcatName(self.muscle[chan]))
        else:
            combo.setCurrentIndex(-1)
        combo.currentTextChanged.connect(lambda text, c=chan: self._on_muscle_changed(c, text))
        return combo

    def _mvc_combo(self, chan):
        combo = QComboBox()
        combo.addItem("(none)")
        for f in self.mvc_files:
            combo.addItem(f)
        if chan in self.mvc_map and self.mvc_map[chan] in self.mvc_files:
            combo.setCurrentText(self.mvc_map[chan])
        else:
            combo.setCurrentIndex(0)
        combo.currentTextChanged.connect(lambda text, c=chan: self._on_mvc_changed(c, text))
        return combo

    def _on_muscle_changed(self, chan, text):
        for j in muscleName.short:
            if muscleName.getConcatName(j) == text:
                self.muscle[chan] = j
                return
        if text.strip():
            self.muscle[chan] = text.strip()
        elif chan in self.muscle:
            del self.muscle[chan]

    def _on_mvc_changed(self, chan, text):
        if text and text != "(none)":
            self.mvc_map[chan] = text
        elif chan in self.mvc_map:
            del self.mvc_map[chan]

    # ------------------------------------------------------------------
    # Result
    # ------------------------------------------------------------------

    def get_mapping(self):
        """Read current table state.

        Returns (enabled: set[str], muscle: dict, mvc_file: dict, errors: list[str]).
        errors is non-empty when an enabled channel has no muscle assigned,
        or two enabled channels share the same muscle assignment -- caller
        decides whether to block on these (same duplicate-joint-name check
        EMGAddWindow.sanity() already applies for the single-add wizard).
        """
        enabled = set()
        for i, chan in enumerate(self.channels):
            item = self.table.item(i, 0)
            if item.checkState() == Qt.CheckState.Checked:
                enabled.add(chan)

        errors = []
        used = {}
        for chan in enabled:
            muscle = self.muscle.get(chan)
            if not muscle:
                errors.append("Channel '{}' is enabled but has no muscle assigned.".format(chan))
                continue
            if muscle in used:
                errors.append(
                    "Muscle '{}' assigned to both '{}' and '{}'.".format(muscle, used[muscle], chan)
                )
                continue
            used[muscle] = chan

        muscle_out = {c: m for c, m in self.muscle.items() if c in enabled}
        mvc_out = {c: f for c, f in self.mvc_map.items() if c in enabled}
        return enabled, muscle_out, mvc_out, errors
