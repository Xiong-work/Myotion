from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QGroupBox, QScrollArea, QVBoxLayout, QHBoxLayout,
    QLabel, QCheckBox, QComboBox, QSpinBox, QFrame,
)

from modules.pyMotion.core.emg import emgConfigEnum, emgFilterEnum, emgNormTypeEnum

_CARD_STYLE = (
    "QGroupBox {"
    "  font-weight: bold;"
    "  color: #c8c8c8;"
    "  border: 1px solid #444;"
    "  border-radius: 6px;"
    "  margin-top: 8px;"
    "  padding: 4px;"
    "}"
    "QGroupBox::title {"
    "  subcontrol-origin: margin;"
    "  left: 8px;"
    "  padding: 0 4px;"
    "}"
)

_CARD_ACTIVE_STYLE = (
    "QGroupBox {"
    "  font-weight: bold;"
    "  color: #e8e0ff;"
    "  border: 2px solid #bd93f9;"
    "  border-radius: 6px;"
    "  margin-top: 8px;"
    "  padding: 4px;"
    "  background-color: #2a1e3a;"
    "}"
    "QGroupBox::title {"
    "  subcontrol-origin: margin;"
    "  left: 8px;"
    "  padding: 0 4px;"
    "}"
)

_LBL = "color: #c8c8c8; font-size: 9pt;"
_SPIN = "background-color: #333b46; color: #f4f4f4; border-radius:4px; padding:2px;"
_COMBO = "background-color: #333b46; color: #f4f4f4; border-radius:4px;"

_STEP_NAMES = {
    emgConfigEnum.DC_OFFSET: "Remove DC Offset",
    emgConfigEnum.FILTER: "Filter",
    emgConfigEnum.FULL_W_RECT: "Full-Wave Rectification",
    emgConfigEnum.NORMALIZATION: "Normalization",
    emgConfigEnum.ACTIVATION: "Activation Detection",
    emgConfigEnum.SUMMARY: "Summary Statistics",
}


class EMGStepCard(QGroupBox):
    configChanged = Signal()
    clicked = Signal()

    def __init__(self, step_index, step_cfg, fs, parent=None):
        if step_cfg.id == emgConfigEnum.FILTER:
            _fname = (
                "Low-pass Filter"
                if (hasattr(step_cfg, "type") and int(step_cfg.type) == int(emgFilterEnum.LOW_PASS))
                else "Band-pass Filter"
            )
            title = "Step {}: {}".format(step_index + 1, _fname)
        else:
            title = "Step {}: {}".format(step_index + 1, _STEP_NAMES.get(step_cfg.id, "Unknown"))
        super().__init__(title, parent)
        self.setStyleSheet(_CARD_STYLE)
        self._step_index = step_index
        self._cfg = step_cfg
        self._fs = fs
        self._building = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 16, 8, 8)
        layout.setSpacing(4)
        self._build_controls(layout, step_cfg)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self.clicked.emit()

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _lbl(text):
        lbl = QLabel(text)
        lbl.setStyleSheet(_LBL)
        return lbl

    def _row(self, *widgets):
        row = QHBoxLayout()
        for w in widgets:
            row.addWidget(w)
        row.addStretch()
        return row

    # ------------------------------------------------------------------ build

    def _build_controls(self, layout, cfg):
        self._building = True
        t = cfg.id

        if t in (emgConfigEnum.DC_OFFSET, emgConfigEnum.FULL_W_RECT):
            self._enable_cb = QCheckBox("Enable")
            self._enable_cb.setStyleSheet(_LBL)
            self._enable_cb.setChecked(cfg.enable)
            self._enable_cb.stateChanged.connect(self._onEnableChanged)
            layout.addLayout(self._row(self._enable_cb))

        elif t == emgConfigEnum.NORMALIZATION:
            self._enable_cb = QCheckBox("Enable")
            self._enable_cb.setStyleSheet(_LBL)
            self._enable_cb.setChecked(cfg.enable)
            self._enable_cb.stateChanged.connect(self._onEnableChanged)
            layout.addLayout(self._row(self._enable_cb))

            self._norm_type_combo = QComboBox()
            self._norm_type_combo.setStyleSheet(_COMBO)
            self._norm_type_combo.addItem("MVC")
            self._norm_type_combo.addItem("Trial Max")
            self._norm_type_combo.setCurrentIndex(
                int(getattr(cfg, 'norm_type', emgNormTypeEnum.MVC))
            )
            self._norm_type_combo.currentIndexChanged.connect(self._onNormTypeChanged)
            layout.addLayout(self._row(self._lbl("Type:"), self._norm_type_combo))

        elif t == emgConfigEnum.FILTER:
            self._enable_cb = QCheckBox("Enable")
            self._enable_cb.setStyleSheet(_LBL)
            self._enable_cb.setChecked(cfg.enable)
            self._enable_cb.stateChanged.connect(self._onEnableChanged)
            layout.addLayout(self._row(self._enable_cb))

            self._type_combo = QComboBox()
            self._type_combo.setStyleSheet(_COMBO)
            self._type_combo.addItem("Band-pass")
            self._type_combo.addItem("Low-pass")
            self._type_combo.setCurrentIndex(int(cfg.type))
            layout.addLayout(self._row(self._lbl("Type:"), self._type_combo))

            self._order_spin = QSpinBox()
            self._order_spin.setStyleSheet(_SPIN)
            self._order_spin.setRange(2, 4)
            self._order_spin.setValue(int(cfg.order))
            layout.addLayout(self._row(self._lbl("Order:"), self._order_spin))

            max_freq = max(1, int(self._fs / 2) - 1) if self._fs else 1000
            self._cutoff_l_spin = QSpinBox()
            self._cutoff_l_spin.setStyleSheet(_SPIN)
            self._cutoff_l_spin.setRange(1, max_freq)
            self._cutoff_l_spin.setValue(max(1, int(cfg.cutoff_l)))
            layout.addLayout(self._row(self._lbl("Low cutoff (Hz):"), self._cutoff_l_spin))

            self._cutoff_h_lbl = self._lbl("High cutoff (Hz):")
            self._cutoff_h_spin = QSpinBox()
            self._cutoff_h_spin.setStyleSheet(_SPIN)
            self._cutoff_h_spin.setRange(1, max_freq)
            self._cutoff_h_spin.setValue(max(1, int(cfg.cutoff_h)))
            self._cutoff_h_row = self._row(self._cutoff_h_lbl, self._cutoff_h_spin)
            layout.addLayout(self._cutoff_h_row)

            self._update_filter_visibility(cfg.type)
            self._type_combo.currentIndexChanged.connect(self._onFilterTypeChanged)
            self._order_spin.valueChanged.connect(self._onFilterParamChanged)
            self._cutoff_l_spin.valueChanged.connect(self._onFilterParamChanged)
            self._cutoff_h_spin.valueChanged.connect(self._onFilterParamChanged)

        elif t == emgConfigEnum.SUMMARY:
            self._stat_labels = {}
            for key in ("max", "min", "med", "rms", "ptp", "zeros"):
                val_lbl = QLabel("—")
                val_lbl.setStyleSheet(_LBL)
                layout.addLayout(self._row(self._lbl("{}:".format(key.upper())), val_lbl))
                self._stat_labels[key] = val_lbl

        self._building = False

    def _update_filter_visibility(self, filter_type):
        is_band = int(filter_type) == int(emgFilterEnum.BAND_PASS)
        self._cutoff_h_spin.setVisible(is_band)
        self._cutoff_h_lbl.setVisible(is_band)

    # ------------------------------------------------------------------ slots

    def _onEnableChanged(self, state):
        if self._building:
            return
        self._cfg.enable = bool(state)
        self.configChanged.emit()

    def _onNormTypeChanged(self, idx):
        if self._building:
            return
        self._cfg.norm_type = emgNormTypeEnum(idx)
        self.configChanged.emit()

    def _onFilterTypeChanged(self, idx):
        if self._building:
            return
        self._cfg.type = emgFilterEnum(idx)
        self._update_filter_visibility(self._cfg.type)
        self.configChanged.emit()

    def _onFilterParamChanged(self):
        if self._building:
            return
        self._cfg.order = self._order_spin.value()
        self._cfg.cutoff_l = self._cutoff_l_spin.value()
        self._cfg.cutoff_h = self._cutoff_h_spin.value()
        self.configChanged.emit()

    # ------------------------------------------------------------------ public

    def setActive(self, active):
        self.setStyleSheet(_CARD_ACTIVE_STYLE if active else _CARD_STYLE)

    def updateSummary(self, cfg):
        """Refresh summary stat labels from an emgSummary config object."""
        if not hasattr(self, "_stat_labels"):
            return
        self._stat_labels["max"].setText("{:.4f}".format(cfg.max))
        self._stat_labels["min"].setText("{:.4f}".format(cfg.min))
        self._stat_labels["med"].setText("{:.4f}".format(cfg.med))
        self._stat_labels["rms"].setText("{:.4f}".format(cfg.rms))
        self._stat_labels["ptp"].setText("{:.4f}".format(cfg.ptp))
        self._stat_labels["zeros"].setText("{:.4f}".format(cfg.zeros))


class EMGPipelinePanel(QWidget):
    configChanged = Signal(int)  # step index that changed
    stepSelected = Signal(int)   # step index clicked by user

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cards = []

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll = scroll

        self._container = QWidget()
        self._vbox = QVBoxLayout(self._container)
        self._vbox.setContentsMargins(4, 4, 4, 4)
        self._vbox.setSpacing(6)
        scroll.setWidget(self._container)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    def load(self, emg_cfg, fs):
        """Rebuild step cards from an emgConfigure and sampling frequency."""
        # Remove all items (widgets + stretch spacers) from the layout
        while self._vbox.count():
            item = self._vbox.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self._cards.clear()

        for i, step_cfg in enumerate(emg_cfg.stepConfig):
            card = EMGStepCard(i, step_cfg, fs, self._container)
            card.configChanged.connect(lambda idx=i: self.configChanged.emit(idx))
            card.clicked.connect(lambda idx=i: self.stepSelected.emit(idx))
            self._vbox.addWidget(card)
            self._cards.append(card)

        self._vbox.addStretch()

    def highlightStep(self, step_index):
        for i, card in enumerate(self._cards):
            card.setActive(i == step_index)

    def scrollToCard(self, step_index):
        if 0 <= step_index < len(self._cards):
            self._scroll.ensureWidgetVisible(self._cards[step_index])

    def updateSummary(self, step_index, cfg):
        if 0 <= step_index < len(self._cards):
            self._cards[step_index].updateSummary(cfg)
