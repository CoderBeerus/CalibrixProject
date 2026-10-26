# dual_ui.py — CALIBRIX v2 Dual RTD Dashboard
#
# Full Dual RTD Comparison instrumentation panel.
# Integrates: DualWorker, DualCalSequencer, DualStabilizationEngine,
#             DualUncertaintyCalculator, dual_report_generator.
#
# Mode selector: FULL CALIBRATION | DEMONSTRATION
# Demo mode is DEFAULT for mock; uses fewer samples and shorter dwell.
# Real hardware mode defaults to Full Calibration.
#
# Key layout:
#   Left panel (tabs): Dual Config, Asset/Identity, History, Point Results
#   Right panel:       Cards | ΔT | Live Metrics & Stability | State | Graphs
#
# Does NOT break Single RTD workflow.

import os
import sys
import time
import math
import json
import collections
import threading

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
    QComboBox, QSpinBox, QGroupBox, QFormLayout, QDoubleSpinBox,
    QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView,
    QScrollArea, QCheckBox, QFrame, QMessageBox, QApplication, QSizePolicy, QGridLayout
)
from PyQt5.QtCore import QThread, pyqtSignal, pyqtSlot, Qt, QTimer
from PyQt5.QtGui import QFont, QColor

import pyqtgraph as pg
from pyqtgraph import mkPen, mkBrush

from dual_rtd_mock import (
    DualRTDMockSource, DualSample,
    DEMO_PASS_OFFSET, DEMO_FAIL_OFFSET, DEMO_NOISE, DEMO_LAG,
    DEMO_SETPOINTS, DEMO_SAMPLES_PT, DEMO_STAB_DWELL,
    DEMO_STAB_SLOPE, DEMO_STAB_SIGMA, DEMO_STAB_PROX,
)
from dual_acquisition_worker import DualWorker
from dual_stabilization import DualStabilizationEngine
from dual_sequencer import DualCalSequencer, DualCalState
from dual_report_generator import generate_dual_pdf, generate_dual_csv, generate_dual_correction_csv
from asset_manager import AssetManager, SensorAsset, CalibrationRecord
from metrics import ISO_LIMITS, compute_metrics

# ── LED style helpers ────────────────────────────────────────────────
LED_GREEN  = "background:#2e7d32;border-radius:7px;min-width:14px;min-height:14px;"
LED_YELLOW = "background:#f9a825;border-radius:7px;min-width:14px;min-height:14px;"
LED_RED    = "background:#c62828;border-radius:7px;min-width:14px;min-height:14px;"
LED_GREY   = "background:#424242;border-radius:7px;min-width:14px;min-height:14px;"
LED_BLUE   = "background:#1565c0;border-radius:7px;min-width:14px;min-height:14px;"

# ── Colours ────────────────────────────────────────────────────────
C_REF    = "#00cc66"
C_DUT    = "#ff7043"
C_DELTA  = "#ffaa00"
C_TOL    = "#44cc44"
C_ZERO   = "#888888"
MAX_PTS  = 2000

# ── Full calibration defaults (DO NOT change for demo) ───────────────
FULL_SETPOINTS   = [0.0, 50.0, 100.0]
FULL_SAMPLES_PT  = 30
FULL_STAB_DWELL  = 30.0
FULL_STAB_SLOPE  = 0.01
FULL_STAB_SIGMA  = 0.05
FULL_STAB_PROX   = 0.5


class _ChannelCard(QGroupBox):
    """Compact RTD channel display card (Reference or DUT)."""

    def __init__(self, role: str, color: str, parent=None):
        super().__init__(role, parent)
        self.setStyleSheet(
            f"QGroupBox {{ border:2px solid {color}; border-radius:6px; "
            f"margin-top:1ex; background:#1a1a2e; }}"
            f"QGroupBox::title {{ color:{color}; font-weight:bold; font-size:12px; "
            f"subcontrol-origin:margin; padding:0 6px; }}"
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        lay = QFormLayout(self)
        lay.setContentsMargins(8, 14, 8, 8)
        lay.setSpacing(4)

        def _val(txt, sz=18, bold=True):
            lbl = QLabel(txt)
            font = QFont("Courier", sz)
            font.setBold(bold)
            lbl.setFont(font)
            lbl.setStyleSheet(f"color:{color};background:transparent;")
            return lbl

        def _info(txt="—"):
            lbl = QLabel(txt)
            lbl.setStyleSheet("color:#aaa;font-size:10px;background:transparent;")
            return lbl

        self.temp_lbl = _val("—.—— °C")
        self.res_lbl  = _val("—.—— Ω", sz=12)
        self.stab_lbl = _info("Stability: —")
        self.fault_lbl = _info("● OK")

        self._led = QLabel()
        self._led.setFixedSize(12, 12)
        self._led.setStyleSheet(LED_GREY)

        lay.addRow("Temp:", self.temp_lbl)
        lay.addRow("Resistance:", self.res_lbl)
        lay.addRow("Stability:", self.stab_lbl)
        led_row = QHBoxLayout()
        led_row.addWidget(self._led)
        led_row.addWidget(self.fault_lbl)
        led_row.addStretch()
        lay.addRow("Health:", led_row)

    def update(self, temp: float, res: float, stable: bool,
               fault: bool = False, fault_msg: str = ""):
        if math.isfinite(temp):
            self.temp_lbl.setText(f"{temp:+8.4f} °C")
        else:
            self.temp_lbl.setText("—FAULT—")
        if math.isfinite(res):
            self.res_lbl.setText(f"{res:8.4f} Ω")
        else:
            self.res_lbl.setText("— Ω")

        if fault:
            self._led.setStyleSheet(LED_RED)
            self.fault_lbl.setText(f"FAULT: {fault_msg[:30]}" if fault_msg else "FAULT")
        elif stable:
            self._led.setStyleSheet(LED_GREEN)
            self.fault_lbl.setText("✓ STABLE")
        else:
            self._led.setStyleSheet(LED_YELLOW)
            self.fault_lbl.setText("Stabilizing…")

        self.stab_lbl.setText("✓ STABLE" if stable else "Stabilizing…")


class DualRTDPanel(QWidget):
    """
    Full Dual RTD Comparison dashboard widget.
    Plugs into main.py via QStackedWidget.
    """

    def __init__(self, settings, asset_manager: AssetManager, use_mock: bool = True,
                 parent=None):
        super().__init__(parent)
        self.settings       = settings
        self.asset_manager  = asset_manager
        self.use_mock       = use_mock

        self._worker:   DualWorker   = None
        self._source                 = None
        self._sequencer: DualCalSequencer = None
        self._stab_eng: DualStabilizationEngine = None
        self._is_demo   = True    # Default DEMO for mock
        self._running   = False

        # Rolling buffers
        self._buf_time  = collections.deque(maxlen=MAX_PTS)
        self._buf_ref   = collections.deque(maxlen=MAX_PTS)
        self._buf_dut   = collections.deque(maxlen=MAX_PTS)
        self._buf_delta = collections.deque(maxlen=MAX_PTS)
        self._buf_setpt = collections.deque(maxlen=MAX_PTS)
        self._last_t    = 0.0
        self._start_time = None

        # Dual sample log (all recorded pairs)
        self._dual_log: list = []
        self._stab_marked = False
        self._rec_marked  = False

        self._build_ui()

    # ── UI Construction ───────────────────────────────────────────────

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)

        # ── Left: control tabs ────────────────────────────────────
        self._tabs = QTabWidget()
        self._tabs.setFixedWidth(380)
        root.addWidget(self._tabs)

        self._build_config_tab()
        self._build_asset_tab()
        self._build_point_results_tab()
        self._build_history_tab()

        # ── Right: dashboard ──────────────────────────────────────
        right = QVBoxLayout()
        right.setSpacing(6)

        # Mock banner
        self._mock_banner = QLabel("⚙  MOCK / SIMULATION MODE")
        self._mock_banner.setAlignment(Qt.AlignCenter)
        self._mock_banner.setStyleSheet(
            "background:#7b4f00; color:#ffe082; font-weight:bold; "
            "font-size:12px; padding:4px; border-radius:3px;")
        self._mock_banner.setVisible(self.use_mock)
        right.addWidget(self._mock_banner)

        # Channel cards row
        cards_row = QHBoxLayout()
        self._ref_card = _ChannelCard("⭐  REFERENCE RTD", C_REF)
        self._dut_card = _ChannelCard("🔬  DUT RTD", C_DUT)
        cards_row.addWidget(self._ref_card)
        cards_row.addWidget(self._dut_card)
        right.addLayout(cards_row)

        # ΔT display + PASS/FAIL
        delta_grp = QGroupBox("Differential Error")
        delta_grp.setStyleSheet(
            "QGroupBox{border:2px solid #ffaa00;border-radius:6px;"
            "margin-top:1ex;background:#1a1a1a;}"
            "QGroupBox::title{color:#ffaa00;font-weight:bold;font-size:12px;"
            "subcontrol-origin:margin;padding:0 6px;}")
        delta_lay = QHBoxLayout(delta_grp)
        delta_lay.setContentsMargins(12, 12, 12, 10)

        self._delta_lbl = QLabel("ΔT = — °C")
        font_delta = QFont("Courier", 28)
        font_delta.setBold(True)
        self._delta_lbl.setFont(font_delta)
        self._delta_lbl.setStyleSheet("color:#ffaa00;background:transparent;")
        self._delta_lbl.setAlignment(Qt.AlignCenter)

        verdict_col = QVBoxLayout()
        self._verdict_badge = QLabel("—")
        font_verd = QFont("Arial", 22)
        font_verd.setBold(True)
        self._verdict_badge.setFont(font_verd)
        self._verdict_badge.setAlignment(Qt.AlignCenter)
        self._verdict_badge.setMinimumWidth(100)
        self._verdict_badge.setStyleSheet(
            "background:#333; color:#888; border-radius:8px; padding:6px;")

        self._tol_lbl = QLabel("Tolerance: —")
        self._tol_lbl.setStyleSheet("color:#aaa; font-size:10px;")
        self._tol_lbl.setAlignment(Qt.AlignCenter)
        self._u_lbl = QLabel("U: —")
        self._u_lbl.setStyleSheet("color:#aaa; font-size:10px;")
        self._u_lbl.setAlignment(Qt.AlignCenter)
        verdict_col.addWidget(self._verdict_badge)
        verdict_col.addWidget(self._tol_lbl)
        verdict_col.addWidget(self._u_lbl)

        delta_lay.addWidget(self._delta_lbl, 2)
        delta_lay.addLayout(verdict_col, 1)
        right.addWidget(delta_grp)

        # Info Row: Stabilization + Live Metrics
        info_row = QHBoxLayout()
        
        # Stabilization Info
        stab_grp = QGroupBox("Stabilization Status")
        stab_form = QFormLayout()
        self._stab_ref_lbl   = QLabel("—")
        self._stab_dut_lbl   = QLabel("—")
        self._stab_delta_lbl = QLabel("—")
        self._stab_dwell_lbl = QLabel("—")
        self._stab_status_lbl = QLabel("NOT STABLE")
        self._stab_status_lbl.setStyleSheet("color:#c62828;font-weight:bold;")
        stab_form.addRow("Ref stable:", self._stab_ref_lbl)
        stab_form.addRow("DUT stable:", self._stab_dut_lbl)
        stab_form.addRow("ΔT σ:", self._stab_delta_lbl)
        stab_form.addRow("Dwell:", self._stab_dwell_lbl)
        stab_form.addRow("● Status:", self._stab_status_lbl)
        stab_grp.setLayout(stab_form)
        info_row.addWidget(stab_grp, 1)

        # Live Metrics
        metrics_grp = QGroupBox("Live Point Metrics")
        metrics_lay = QGridLayout()
        
        def mk_lbl(text):
            l = QLabel(text)
            l.setStyleSheet("color:#aaa;")
            return l
            
        def mk_val():
            l = QLabel("—")
            l.setStyleSheet("color:#fff; font-weight:bold;")
            return l
            
        metrics_lay.addWidget(mk_lbl("Ref Mean:"), 0, 0)
        self._m_ref_mean = mk_val()
        metrics_lay.addWidget(self._m_ref_mean, 0, 1)
        
        metrics_lay.addWidget(mk_lbl("DUT Mean:"), 1, 0)
        self._m_dut_mean = mk_val()
        metrics_lay.addWidget(self._m_dut_mean, 1, 1)

        metrics_lay.addWidget(mk_lbl("MBE (ΔT):"), 2, 0)
        self._m_mbe = mk_val()
        metrics_lay.addWidget(self._m_mbe, 2, 1)
        
        metrics_lay.addWidget(mk_lbl("MAE:"), 3, 0)
        self._m_mae = mk_val()
        metrics_lay.addWidget(self._m_mae, 3, 1)
        
        metrics_lay.addWidget(mk_lbl("RMSE:"), 0, 2)
        self._m_rmse = mk_val()
        metrics_lay.addWidget(self._m_rmse, 0, 3)
        
        metrics_lay.addWidget(mk_lbl("Std Dev (σ):"), 1, 2)
        self._m_std = mk_val()
        metrics_lay.addWidget(self._m_std, 1, 3)
        
        metrics_lay.addWidget(mk_lbl("Max +Err:"), 2, 2)
        self._m_max_pos = mk_val()
        metrics_lay.addWidget(self._m_max_pos, 2, 3)
        
        metrics_lay.addWidget(mk_lbl("Max -Err:"), 3, 2)
        self._m_max_neg = mk_val()
        metrics_lay.addWidget(self._m_max_neg, 3, 3)
        
        metrics_grp.setLayout(metrics_lay)
        info_row.addWidget(metrics_grp, 1)
        
        right.addLayout(info_row)

        # State bar
        state_bar = QFrame()
        state_bar.setStyleSheet("background:#111;border-radius:3px;")
        state_bar.setMaximumHeight(30)
        sb_lay = QHBoxLayout(state_bar)
        sb_lay.setContentsMargins(8, 2, 8, 2)

        def _sb(label):
            lbl_l = QLabel(label)
            lbl_l.setStyleSheet("color:#666;font-size:10px;")
            lbl_v = QLabel("—")
            lbl_v.setStyleSheet("color:#ccc;font-size:10px;font-weight:bold;margin-right:12px;")
            sb_lay.addWidget(lbl_l); sb_lay.addWidget(lbl_v)
            return lbl_v

        self._sb_state   = _sb("State:")
        self._sb_point   = _sb("Point:")
        self._sb_sample  = _sb("Sample:")
        self._sb_setpt   = _sb("Setpoint:")
        sb_lay.addStretch()
        right.addWidget(state_bar)

        # Graphs
        self._build_graphs(right)

        root.addLayout(right, 1)

    def _build_graphs(self, parent_layout):
        gw = pg.GraphicsLayoutWidget()
        parent_layout.addWidget(gw, 1)

        # Main temperature graph
        self._plot_main = gw.addPlot(row=0, col=0)
        self._plot_main.setLabel("left", "Temperature", units="°C",
                                 **{"color":"#e0e0e0","font-size":"10pt"})
        self._plot_main.setLabel("bottom", "Time", units="s",
                                 **{"color":"#e0e0e0","font-size":"9pt"})
        self._plot_main.showGrid(x=True, y=True, alpha=0.25)
        self._plot_main.addLegend(offset=(10,10))
        self._plot_main.enableAutoRange(axis='y') # Ensure auto range is enabled
        self._plot_main.enableAutoRange(axis='x') # Ensure auto range is enabled

        self._curve_ref   = self._plot_main.plot(pen=mkPen(C_REF,  width=2),
                                                  name="Reference RTD")
        self._curve_dut   = self._plot_main.plot(pen=mkPen(C_DUT,  width=2),
                                                  name="DUT RTD")
        self._curve_sp    = self._plot_main.plot(
            pen=mkPen("#00bfff", width=1, style=Qt.DashLine), name="Setpoint")

        # Error / ΔT graph
        self._plot_err = gw.addPlot(row=1, col=0)
        self._plot_err.setLabel("left", "ΔT = DUT−Ref", units="°C",
                                **{"color":"#e0e0e0","font-size":"9pt"})
        self._plot_err.setLabel("bottom", "Time", units="s",
                                **{"color":"#e0e0e0","font-size":"9pt"})
        self._plot_err.showGrid(x=True, y=True, alpha=0.2)
        self._plot_err.setMaximumHeight(160)
        self._plot_err.enableAutoRange(axis='y') # Fix bug: let delta graph autorange vertically
        self._plot_err.enableAutoRange(axis='x')
        self._plot_err.setXLink(self._plot_main)

        # Zero line and tolerance lines (dynamic, updated per point)
        self._err_zero = self._plot_err.addLine(
            y=0, pen=mkPen(C_ZERO, width=1, style=Qt.DashLine))
        self._err_tol_pos = self._plot_err.addLine(y=0.5, pen=mkPen(C_TOL, width=1, style=Qt.DotLine))
        self._err_tol_neg = self._plot_err.addLine(y=-0.5, pen=mkPen(C_TOL, width=1, style=Qt.DotLine))

        self._curve_delta = self._plot_err.plot(
            pen=mkPen(C_DELTA, width=2), name="ΔT")

        self._event_lines = []

        gw.ci.layout.setRowStretchFactor(0, 3)
        gw.ci.layout.setRowStretchFactor(1, 1)
        self._graph_widget = gw

    def _build_config_tab(self):
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        inner = QWidget(); lay = QVBoxLayout(inner); scroll.setWidget(inner)
        self._tabs.addTab(scroll, "⚙ Dual Config")

        # Mode selector
        mode_grp = QGroupBox("Calibration Mode")
        mode_lay = QVBoxLayout(mode_grp)

        self._demo_btn = QPushButton("DEMONSTRATION MODE — ACCELERATED ACQUISITION")
        self._demo_btn.setCheckable(True)
        self._demo_btn.setChecked(True)   # Default: DEMO for mock
        self._demo_btn.setStyleSheet(
            "QPushButton{background:#7b4f00;color:#ffe082;font-weight:bold;padding:6px;}"
            "QPushButton:!checked{background:#1565c0;color:#fff;}"
        )
        self._demo_btn.clicked.connect(self._on_mode_toggle)

        self._full_btn = QPushButton("FULL CALIBRATION")
        self._full_btn.setCheckable(True)
        self._full_btn.setChecked(False)
        self._full_btn.setStyleSheet(
            "QPushButton{background:#1565c0;color:#fff;font-weight:bold;padding:6px;}"
            "QPushButton:checked{background:#2e7d32;color:#fff;}"
        )
        self._full_btn.clicked.connect(self._on_full_toggle)

        mode_row = QHBoxLayout()
        mode_row.addWidget(self._demo_btn)
        mode_row.addWidget(self._full_btn)
        mode_lay.addLayout(mode_row)

        self._demo_info = QLabel(
            "Demo: 3 points (25/50/75°C), 10 samples/pt, 8s dwell. "
            "Clearly labeled. Does NOT change full calibration settings.")
        self._demo_info.setWordWrap(True)
        self._demo_info.setStyleSheet("color:#f9a825;font-size:10px;padding:2px;")
        mode_lay.addWidget(self._demo_info)
        lay.addWidget(mode_grp)

        # Mock DUT configuration
        dut_grp = QGroupBox("Mock DUT Configuration (PASS/FAIL demo)")
        dut_form = QFormLayout()

        self._dut_offset_spin = QDoubleSpinBox()
        self._dut_offset_spin.setRange(-5.0, 5.0)
        self._dut_offset_spin.setSingleStep(0.01)
        self._dut_offset_spin.setDecimals(3)
        self._dut_offset_spin.setValue(DEMO_PASS_OFFSET)
        self._dut_offset_spin.setSuffix(" °C")
        dut_form.addRow("DUT Offset (bias):", self._dut_offset_spin)

        self._dut_noise_spin = QDoubleSpinBox()
        self._dut_noise_spin.setRange(0.001, 1.0)
        self._dut_noise_spin.setSingleStep(0.005)
        self._dut_noise_spin.setDecimals(3)
        self._dut_noise_spin.setValue(DEMO_NOISE)
        self._dut_noise_spin.setSuffix(" °C")
        dut_form.addRow("DUT Noise (σ):", self._dut_noise_spin)

        # Quick PASS/FAIL preset buttons
        preset_row = QHBoxLayout()
        pass_btn = QPushButton("▶ PASS preset (+0.12°C)")
        pass_btn.setStyleSheet("background:#1b5e20;color:#fff;")
        pass_btn.clicked.connect(lambda: self._dut_offset_spin.setValue(DEMO_PASS_OFFSET))

        fail_btn = QPushButton("▶ FAIL preset (+0.65°C)")
        fail_btn.setStyleSheet("background:#b71c1c;color:#fff;")
        fail_btn.clicked.connect(lambda: self._dut_offset_spin.setValue(DEMO_FAIL_OFFSET))

        preset_row.addWidget(pass_btn); preset_row.addWidget(fail_btn)
        dut_form.addRow("Quick preset:", preset_row)

        dut_grp.setLayout(dut_form)
        lay.addWidget(dut_grp)

        # Setpoints / samples / tolerance
        cal_grp = QGroupBox("Calibration Settings")
        cal_form = QFormLayout()

        self._sp_input = QLineEdit(",".join(str(x) for x in DEMO_SETPOINTS))
        self._sp_input.setToolTip("Comma-separated °C values")
        cal_form.addRow("Setpoints (°C):", self._sp_input)

        self._spp_spin = QSpinBox()
        self._spp_spin.setRange(3, 500)
        self._spp_spin.setValue(DEMO_SAMPLES_PT)
        cal_form.addRow("Samples/point:", self._spp_spin)

        self._tol_combo = QComboBox()
        self._tol_combo.addItems(list(ISO_LIMITS.keys()))
        self._tol_combo.setCurrentText("Class A")
        cal_form.addRow("Tolerance:", self._tol_combo)

        self._interval_spin = QDoubleSpinBox()
        self._interval_spin.setRange(0.1, 10.0)
        self._interval_spin.setSingleStep(0.1)
        self._interval_spin.setValue(0.5)
        self._interval_spin.setSuffix(" s")
        cal_form.addRow("Interval:", self._interval_spin)

        self._uref_spin = QDoubleSpinBox()
        self._uref_spin.setRange(0.001, 5.0)
        self._uref_spin.setDecimals(3)
        self._uref_spin.setValue(0.05)
        self._uref_spin.setSuffix(" °C")
        cal_form.addRow("U_ref (cert):", self._uref_spin)

        self._op_input = QLineEdit(getattr(self.settings, "operator_name", "Operator"))
        cal_form.addRow("Operator:", self._op_input)

        cal_grp.setLayout(cal_form)
        lay.addWidget(cal_grp)

        # Controls
        ctrl_grp = QGroupBox("Controls")
        ctrl_lay = QVBoxLayout()
        self._start_btn = QPushButton("▶  Start Dual Calibration")
        self._start_btn.clicked.connect(self.start_calibration)
        self._stop_btn  = QPushButton("⏹  Stop & Generate Report")
        self._stop_btn.clicked.connect(self.stop_calibration)
        self._stop_btn.setEnabled(False)
        self._adv_btn   = QPushButton("➡  Advance to Next Point")
        self._adv_btn.clicked.connect(self._advance)
        self._adv_btn.setVisible(False)
        ctrl_lay.addWidget(self._start_btn)
        ctrl_lay.addWidget(self._stop_btn)
        ctrl_lay.addWidget(self._adv_btn)
        ctrl_grp.setLayout(ctrl_lay)
        lay.addWidget(ctrl_grp)
        lay.addStretch()

    def _build_asset_tab(self):
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        inner = QWidget(); lay = QVBoxLayout(inner); scroll.setWidget(inner)
        self._tabs.addTab(scroll, "🔖 Asset")

        ref_grp = QGroupBox("Reference RTD Identity")
        ref_form = QFormLayout()
        self._ref_serial = QLineEdit(getattr(self.settings, "dual_ref_serial", "REF-001"))
        self._ref_mfr    = QLineEdit(getattr(self.settings, "dual_ref_manufacturer", ""))
        self._ref_model  = QLineEdit(getattr(self.settings, "dual_ref_model", ""))
        self._ref_r0     = QDoubleSpinBox()
        self._ref_r0.setRange(1, 10000)
        self._ref_r0.setValue(getattr(self.settings, "dual_ref_r0", 100.0))
        self._ref_r0.setSuffix(" Ω")
        self._ref_cert   = QLineEdit(getattr(self.settings, "dual_ref_cert_no", ""))
        self._ref_caldate = QLineEdit(getattr(self.settings, "dual_ref_cal_date", ""))
        ref_form.addRow("Serial:", self._ref_serial)
        ref_form.addRow("Manufacturer:", self._ref_mfr)
        ref_form.addRow("Model:", self._ref_model)
        ref_form.addRow("Nominal R₀:", self._ref_r0)
        ref_form.addRow("Cert No.:", self._ref_cert)
        ref_form.addRow("Cal Date:", self._ref_caldate)
        ref_grp.setLayout(ref_form)
        lay.addWidget(ref_grp)

        dut_grp = QGroupBox("DUT RTD Identity")
        dut_form = QFormLayout()
        self._dut_serial = QLineEdit(getattr(self.settings, "dual_dut_serial", "DUT-001"))
        self._dut_tag    = QLineEdit(getattr(self.settings, "dual_dut_tag", ""))
        self._dut_mfr    = QLineEdit(getattr(self.settings, "dual_dut_manufacturer", ""))
        self._dut_model  = QLineEdit(getattr(self.settings, "dual_dut_model", ""))
        self._dut_r0     = QDoubleSpinBox()
        self._dut_r0.setRange(1, 10000)
        self._dut_r0.setValue(getattr(self.settings, "dual_dut_r0", 100.0))
        self._dut_r0.setSuffix(" Ω")
        dut_form.addRow("Serial:", self._dut_serial)
        dut_form.addRow("Tag:", self._dut_tag)
        dut_form.addRow("Manufacturer:", self._dut_mfr)
        dut_form.addRow("Model:", self._dut_model)
        dut_form.addRow("Nominal R₀:", self._dut_r0)
        dut_grp.setLayout(dut_form)
        lay.addWidget(dut_grp)

        dir_grp = QGroupBox("Output Directory")
        dir_form = QFormLayout()
        self._dir_input = QLineEdit(getattr(self.settings, "report_output_directory", "CALIBRIX_Reports"))
        dir_form.addRow("Folder:", self._dir_input)
        dir_grp.setLayout(dir_form)
        lay.addWidget(dir_grp)
        lay.addStretch()

    def _build_point_results_tab(self):
        container = QWidget(); lay = QVBoxLayout(container)
        self._tabs.addTab(container, "📊 Point Results")
        
        self._pts_table = QTableWidget(0, 9)
        self._pts_table.setHorizontalHeaderLabels([
            "Pt", "Setpt", "Ref Mean", "DUT Mean", "MBE(ΔT)", "MAE", "U", "Limit", "Verdict"
        ])
        self._pts_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._pts_table.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(self._pts_table)

    def _build_history_tab(self):
        container = QWidget(); lay = QVBoxLayout(container)
        self._tabs.addTab(container, "📋 History")

        self._hist_table = QTableWidget(0, 7)
        self._hist_table.setHorizontalHeaderLabels(
            ["Date","DUT Serial","Verdict","Mean ΔT","Max |ΔT|","U","PDF"])
        self._hist_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._hist_table.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(self._hist_table)

        ld_btn = QPushButton("Refresh History")
        ld_btn.clicked.connect(self._load_history)
        lay.addWidget(ld_btn)

    # ── Mode toggle ───────────────────────────────────────────────────

    def _on_mode_toggle(self, checked):
        if checked:
            self._is_demo = True
            self._full_btn.setChecked(False)
            # Load demo defaults into UI (NOT into full calibration settings)
            self._sp_input.setText(",".join(str(x) for x in DEMO_SETPOINTS))
            self._spp_spin.setValue(DEMO_SAMPLES_PT)
        else:
            self._is_demo = False
            self._demo_btn.setChecked(False)
            self._full_btn.setChecked(True)
            self._sp_input.setText(",".join(str(x) for x in FULL_SETPOINTS))
            self._spp_spin.setValue(FULL_SAMPLES_PT)

    def _on_full_toggle(self, checked):
        if checked:
            self._is_demo = False
            self._demo_btn.setChecked(False)
            self._sp_input.setText(",".join(str(x) for x in FULL_SETPOINTS))
            self._spp_spin.setValue(FULL_SAMPLES_PT)
        else:
            self._is_demo = True
            self._full_btn.setChecked(False)
            self._demo_btn.setChecked(True)
            self._sp_input.setText(",".join(str(x) for x in DEMO_SETPOINTS))
            self._spp_spin.setValue(DEMO_SAMPLES_PT)

    def set_use_mock(self, use_mock: bool):
        """Called by main.py when hardware mode changes."""
        self.use_mock = use_mock
        self._mock_banner.setVisible(use_mock)
        if not use_mock:
            # Default to FULL CALIBRATION for real hardware
            self._is_demo = False
            self._demo_btn.setChecked(False)
            self._full_btn.setChecked(True)
            self._sp_input.setText(",".join(str(x) for x in FULL_SETPOINTS))
            self._spp_spin.setValue(FULL_SAMPLES_PT)

    # ── Start calibration ─────────────────────────────────────────────

    def start_calibration(self):
        if self._running:
            return

        # Parse setpoints
        try:
            setpoints = [float(x.strip()) for x in self._sp_input.text().split(",") if x.strip()]
            if not setpoints:
                raise ValueError("Empty setpoints")
        except ValueError as e:
            QMessageBox.critical(self, "Error", f"Invalid setpoints: {e}")
            return

        samples_pt  = self._spp_spin.value()
        tol_class   = self._tol_combo.currentText()
        interval    = self._interval_spin.value()
        u_ref       = self._uref_spin.value()

        # Require valid reference uncertainty in REAL HARDWARE mode
        if not self.use_mock and u_ref <= 0.0:
            QMessageBox.critical(self, "Invalid Configuration",
                                 "Reference Uncertainty must be > 0 in Real Hardware mode.\n"
                                 "Check the Reference RTD certificate.")
            return

        # Stabilization parameters — demo uses relaxed values
        if self._is_demo:
            slope_t = DEMO_STAB_SLOPE
            sigma_t = DEMO_STAB_SIGMA
            prox_t  = DEMO_STAB_PROX
            dwell_t = DEMO_STAB_DWELL
        else:
            slope_t = FULL_STAB_SLOPE
            sigma_t = FULL_STAB_SIGMA
            prox_t  = FULL_STAB_PROX
            dwell_t = FULL_STAB_DWELL

        # Tolerance for display
        A, B = ISO_LIMITS.get(tol_class, (0.30, 0.005))

        # ── Create source ─────────────────────────────────────────
        if self.use_mock:
            dut_offset = self._dut_offset_spin.value()
            dut_noise  = self._dut_noise_spin.value()
            self._source = DualRTDMockSource(
                dut_offset=dut_offset,
                dut_noise=dut_noise,
                dut_lag=DEMO_LAG,
            )
            self._source.setpoint = setpoints[0]
        else:
            try:
                from dual_rtd_driver import DualRTDRealSource
                self._source = DualRTDRealSource(
                    ref_bus=getattr(self.settings, "dual_ref_spi_bus", 0),
                    ref_device=getattr(self.settings, "dual_ref_spi_device", 0),
                    dut_bus=getattr(self.settings, "dual_dut_spi_bus", 0),
                    dut_device=getattr(self.settings, "dual_dut_spi_device", 1),
                    wires=4,
                    r_ref=getattr(self.settings, "r_ref", 430.0),
                )
                self._source.setpoint = setpoints[0]
            except Exception as e:
                QMessageBox.critical(self, "Hardware Error",
                                     f"Cannot initialize real Dual RTD hardware:\n{e}")
                return

        # ── Create stabilization engine ───────────────────────────
        self._stab_eng = DualStabilizationEngine(
            window_size=20,
            slope_thresh=slope_t,
            sigma_thresh=sigma_t,
            proximity_deg=prox_t,
            dwell_seconds=dwell_t,
            delta_sigma_thresh=sigma_t,
        )

        # ── Create sequencer ──────────────────────────────────────
        self._sequencer = DualCalSequencer(
            setpoints=setpoints,
            samples_per_point=samples_pt,
            dual_stab_engine=self._stab_eng,
            tolerance_class=tol_class,
            u_ref_expanded=u_ref,
            k_ref=2,
            r_ref=getattr(self.settings, "r_ref", 430.0),
            nominal_r0=self._ref_r0.value(),
        )
        self._sequencer.start()

        self._update_tolerance_lines(setpoints[0], A, B)

        # ── Reset buffers / state ─────────────────────────────────
        self._buf_time.clear(); self._buf_ref.clear()
        self._buf_dut.clear(); self._buf_delta.clear()
        self._buf_setpt.clear()
        self._dual_log.clear()
        self._pts_table.setRowCount(0)
        self._last_t   = 0.0
        self._start_time = time.time()
        self._stab_marked = False
        self._rec_marked  = False
        self._clear_event_lines()
        
        # We rely on auto-range now, no hard YRange set.

        # ── Start worker ──────────────────────────────────────────
        self._worker = DualWorker(self._source, interval)
        self._worker.dual_data_signal.connect(self._on_dual_data)
        self._worker.error_signal.connect(self._on_worker_error)
        self._worker.ref_fault_status.connect(lambda v: None)   # handled in _on_dual_data
        self._worker.dut_fault_status.connect(lambda v: None)
        self._worker.start()

        self._running = True
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._adv_btn.setVisible(False)
        self._sb_state.setText("STARTING")
        self._sb_setpt.setText(f"{setpoints[0]:.1f}°C")

        demo_str = "DEMO" if self._is_demo else "FULL"
        print(f"[DUAL] Started — {demo_str}, {len(setpoints)} pts, {samples_pt} spp, "
              f"dwell={dwell_t}s, interval={interval}s, mock={self.use_mock}")

    def _update_tolerance_lines(self, setpoint, A, B):
        tol = A + B * abs(setpoint)
        self._err_tol_pos.setValue( tol)
        self._err_tol_neg.setValue(-tol)
        self._tol_lbl.setText(f"Tolerance: ±{tol:.4f}°C")
    # ── Data handler ──────────────────────────────────────────────────

    @pyqtSlot(object)
    def _on_dual_data(self, sample: DualSample):
        """Receive coordinated pair from worker."""
        if not self._running:
            return
            
        if self._sequencer and self._sequencer.state == DualCalState.POINT_COMPLETE:
            # Drop trailing queued signals after worker is paused
            return

        # Use actual DualSample timestamp for authoritative elapsed time
        if getattr(self, '_start_time', None) is None:
            self._start_time = sample.timestamp
        t = sample.timestamp - self._start_time
        
        pre_state = self._sequencer.state if self._sequencer else None

        # Update source setpoint for mock convergence only during active states
        if self._sequencer and self.use_mock and self._source:
            state = self._sequencer.state
            if state not in (DualCalState.POINT_COMPLETE, DualCalState.COMPLETE):
                self._source.setpoint = self._sequencer.current_setpoint

        if self._sequencer and self._sequencer.state == DualCalState.CHANNEL_CHECK:
            if sample.ref_valid and sample.dut_valid:
                self._sequencer.complete_channel_check(True)
            elif sample.ref_fault or sample.dut_fault:
                self._sequencer.complete_channel_check(False)

        # Feed sequencer
        if self._sequencer:
            # Reject completely invalid or massive values to protect PyqtGraph
            if not (math.isfinite(sample.ref_temperature) and math.isfinite(sample.dut_temperature)):
                return
            if abs(sample.ref_temperature) > 1000 or abs(sample.dut_temperature) > 1000:
                return
            if t < 0 or t > 1000000:
                return

            self._sequencer.add_reading(
                ref_temp  = sample.ref_temperature,
                dut_temp  = sample.dut_temperature,
                ref_res   = sample.ref_resistance,
                dut_res   = sample.dut_resistance,
                ref_valid = sample.ref_valid,
                dut_valid = sample.dut_valid,
            )
            state = self._sequencer.state
            sample.calibration_state = state.name
            self._sb_state.setText(state.name)
            self._sb_point.setText(
                f"{self._sequencer._point_idx + 1}/{len(self._sequencer.setpoints)}"
                if state not in (DualCalState.COMPLETE, DualCalState.POINT_COMPLETE)
                else f"{len(self._sequencer.all_results)}/{len(self._sequencer.setpoints)}")
            
            # Update Live Metrics if we have samples
            if self._sequencer._current:
                n = len(self._sequencer._current.ref_samples)
                self._sb_sample.setText(f"{n}/{self._sequencer.samples_per_point}")
                
                # Update metrics if we have enough points
                if n >= 3:
                    m = compute_metrics(self._sequencer._current.dut_samples, self._sequencer._current.ref_samples)
                    self._m_ref_mean.setText(f"{m['mean_standard']:.4f}")
                    self._m_dut_mean.setText(f"{m['mean_measured']:.4f}")
                    self._m_mbe.setText(f"{m['mbe']:.4f}")
                    self._m_mae.setText(f"{m['mae']:.4f}")
                    self._m_rmse.setText(f"{m['rmse']:.4f}")
                    self._m_std.setText(f"{m['std_dev']:.4f}")
                    
                    errs = m.get('per_point_abs_errors', [])
                    raw_errs = [d - r for d, r in zip(self._sequencer._current.dut_samples, self._sequencer._current.ref_samples)]
                    if raw_errs:
                        self._m_max_pos.setText(f"{max(raw_errs):.4f}")
                        self._m_max_neg.setText(f"{min(raw_errs):.4f}")
                else:
                    self._m_ref_mean.setText("—")
                    self._m_dut_mean.setText("—")
                    self._m_mbe.setText("—")
                    self._m_mae.setText("—")
                    self._m_rmse.setText("—")
                    self._m_std.setText("—")
                    self._m_max_pos.setText("—")
                    self._m_max_neg.setText("—")
                    
            self._sb_setpt.setText(f"{self._sequencer.current_setpoint:.1f}°C")

            # Stabilization status
            st = self._sequencer.stabilization_status()
            if st:
                self._update_stab_display(st)
                # Stability achieved event marker
                if st.get("stable") and not self._stab_marked:
                    self._add_event_line(t, "#ffff00", "Stable")
                    self._stab_marked = True
            # Recording started event marker
            if state == DualCalState.RECORDING and not self._rec_marked:
                self._add_event_line(t, "#00ff80", "Recording")
                self._rec_marked = True

            # Update tolerance display for current setpoint
            tc = self._tol_combo.currentText()
            A, B = ISO_LIMITS.get(tc, (0.30, 0.005))
            self._update_tolerance_lines(self._sequencer.current_setpoint, A, B)

            # Log only during RECORDING
            if state == DualCalState.RECORDING:
                self._dual_log.append(sample)

            if state == DualCalState.POINT_COMPLETE:
                self._prompt_advance()
            elif state in (DualCalState.COMPLETE, DualCalState.FAULT, DualCalState.ABORTED):
                self.stop_calibration()
                return

        else:
            # No sequencer — live monitoring only
            if sample.ref_valid:
                self._dual_log.append(sample)

        # ── Update cards ──────────────────────────────────────────
        st_info = self._sequencer.stabilization_status() if self._sequencer else {}
        ref_stable = st_info.get("ref_stable", False)
        dut_stable = st_info.get("dut_stable", False)

        self._ref_card.update(
            sample.ref_temperature, sample.ref_resistance,
            ref_stable, sample.ref_fault, sample.ref_fault_msg)
        self._dut_card.update(
            sample.dut_temperature, sample.dut_resistance,
            dut_stable, sample.dut_fault, sample.dut_fault_msg)

        # ── Update ΔT display ─────────────────────────────────────
        delta = sample.delta_temperature
        if math.isfinite(delta):
            self._delta_lbl.setText(f"ΔT = {delta:+8.4f} °C")
        else:
            self._delta_lbl.setText("ΔT = —")

        # ── Live PASS/FAIL estimate ───────────────────────────────
        tc = self._tol_combo.currentText()
        A, B = ISO_LIMITS.get(tc, (0.30, 0.005))
        sp = self._sequencer.current_setpoint if self._sequencer else 50.0
        tol = A + B * abs(sp)
        k_val = getattr(self.settings, "ref_uncertainty_k", 2)
        
        fallback_u = self._uref_spin.value()
        if fallback_u <= 0.0 and self.use_mock:
            fallback_u = 0.05  # Assumed value for demo
            
        u_est = fallback_u / k_val   # fallback estimate
        is_assumed = (self._uref_spin.value() <= 0.0 and self.use_mock)
        
        if self._sequencer and self._sequencer._current:
            ds = self._sequencer._current.delta_samples
            n = len(ds)
            if n > 1:
                mean_d = sum(ds) / n
                std_d = math.sqrt(sum((d - mean_d)**2 for d in ds) / (n - 1))
                ub = self._sequencer._uc.compute(std_delta=std_d, n_samples=n)
                u_est = ub["expanded_uncertainty"]
                
        if math.isfinite(delta):
            if abs(delta) + u_est <= tol:
                self._verdict_badge.setText("PROV. PASS")
                self._verdict_badge.setStyleSheet(
                    "background:#1b5e20;color:#69f0ae;border-radius:8px;padding:6px;font-weight:bold;")
            else:
                self._verdict_badge.setText("PROV. FAIL")
                self._verdict_badge.setStyleSheet(
                    "background:#b71c1c;color:#ff8a80;border-radius:8px;padding:6px;font-weight:bold;")
            
            lbl_text = f"U≈ ±{u_est:.4f}°C"
            if is_assumed:
                lbl_text += " (ASSUMED 0.05)"
            self._u_lbl.setText(lbl_text)

        # ── Update graphs ─────────────────────────────────────────
        # Allow the final sample that triggered POINT_COMPLETE to be plotted
        is_paused = (state == DualCalState.POINT_COMPLETE and pre_state == DualCalState.POINT_COMPLETE)
        
        if t > self._last_t and sample.ref_valid and sample.dut_valid and not is_paused:
            self._buf_time.append(t)
            self._buf_ref.append(sample.ref_temperature)
            self._buf_dut.append(sample.dut_temperature)
            self._buf_delta.append(delta if math.isfinite(delta) else 0.0)
            self._buf_setpt.append(sample.setpoint)
            self._last_t = t

            t_arr = list(self._buf_time)
            self._curve_ref.setData(t_arr, list(self._buf_ref))
            self._curve_dut.setData(t_arr, list(self._buf_dut))
            self._curve_sp.setData(t_arr, list(self._buf_setpt))
            self._curve_delta.setData(t_arr, list(self._buf_delta))
            
            # Sliding window X-axis for readability (show last ~180 seconds)
            view_width = 180.0
            min_x = max(0, t - view_width)
            max_x = max(view_width, t + 5)
            self._plot_main.setXRange(min_x, max_x)

    @pyqtSlot(str, bool)
    def _on_worker_error(self, msg: str, is_critical: bool):
        print(f"[DUAL WORKER ERROR] {msg} (critical={is_critical})")
        self._sb_state.setText("ERROR")
        if is_critical:
            self._ref_card.fault_lbl.setText(f"ERROR: {msg[:40]}")

    def _update_stab_display(self, st: dict):
        ref_ok = "✓" if st.get("ref_stable") else "✗"
        dut_ok = "✓" if st.get("dut_stable") else "✗"
        self._stab_ref_lbl.setText(
            f"{ref_ok}  slope={st.get('ref_slope_val',0):.4f}°C/s  σ={st.get('ref_sigma_val',0):.4f}°C")
        self._stab_dut_lbl.setText(
            f"{dut_ok}  slope={st.get('dut_slope_val',0):.4f}°C/s  σ={st.get('dut_sigma_val',0):.4f}°C")
        self._stab_delta_lbl.setText(
            f"σ(ΔT)={st.get('delta_sigma',0):.4f}°C  (lim:{st.get('delta_sigma_thresh',0.05):.4f})")
        elapsed = st.get("ref_dwell_elapsed", 0.0)
        req     = st.get("dwell_required", 30.0)
        self._stab_dwell_lbl.setText(f"{elapsed:.1f}s / {req:.0f}s")
        if st.get("stable"):
            self._stab_status_lbl.setText("✓ STABLE")
            self._stab_status_lbl.setStyleSheet("color:#2e7d32;font-weight:bold;")
        else:
            self._stab_status_lbl.setText("✗ NOT STABLE")
            self._stab_status_lbl.setStyleSheet("color:#c62828;font-weight:bold;")

    def _prompt_advance(self):
        # Do not jump the setpoint display early. Show waiting status.
        self._adv_btn.setVisible(True)
        self._verdict_badge.setText("WAITING")
        self._verdict_badge.setStyleSheet(
            "background:#1565c0;color:#fff;border-radius:8px;padding:6px;font-weight:bold;")
            
        if getattr(self, "_worker", None):
            self._worker.pause()
        
        # Add to point results table if not already added
        if self._sequencer and self._sequencer.all_results:
            if self._pts_table.rowCount() < len(self._sequencer.all_results):
                self._update_point_results_table()

    def _update_point_results_table(self):
        self._pts_table.setRowCount(0)
        for r in self._sequencer.all_results:
            row = self._pts_table.rowCount()
            self._pts_table.insertRow(row)
            
            v_item = QTableWidgetItem(r.verdict)
            v_item.setForeground(QColor("#2e7d32") if r.verdict == "PASS" else QColor("#c62828"))
            
            self._pts_table.setItem(row, 0, QTableWidgetItem(str(r.point_index + 1)))
            self._pts_table.setItem(row, 1, QTableWidgetItem(f"{r.setpoint:.1f}"))
            self._pts_table.setItem(row, 2, QTableWidgetItem(f"{r.mean_ref:.4f}"))
            self._pts_table.setItem(row, 3, QTableWidgetItem(f"{r.mean_dut:.4f}"))
            self._pts_table.setItem(row, 4, QTableWidgetItem(f"{r.mean_delta:+.4f}"))
            self._pts_table.setItem(row, 5, QTableWidgetItem(f"{r.mae_delta:.4f}"))
            self._pts_table.setItem(row, 6, QTableWidgetItem(f"{r.as_left_expanded_U:.4f}"))
            self._pts_table.setItem(row, 7, QTableWidgetItem(f"{r.tolerance_limit:.4f}"))
            self._pts_table.setItem(row, 8, v_item)

    def _advance(self):
        if self._sequencer and self._sequencer.state == DualCalState.POINT_COMPLETE:
            self._sequencer.advance_to_next()
            sp = self._sequencer.current_setpoint
            if self.use_mock and self._source:
                self._source.setpoint = sp
            self._stab_marked = False
            self._rec_marked  = False
            self._adv_btn.setVisible(False)
            self._sb_state.setText("HEATING")
            if getattr(self, "_worker", None):
                self._worker.resume()

    def _clear_event_lines(self):
        for line in self._event_lines:
            self._plot_main.removeItem(line)
        self._event_lines.clear()

    def _add_event_line(self, x: float, color: str, label: str):
        line = pg.InfiniteLine(pos=x, angle=90, pen=pg.mkPen(color, style=Qt.DashLine))
        line.label = pg.InfLineLabel(line, text=label, position=0.95, color=color)
        self._plot_main.addItem(line)
        self._event_lines.append(line)

    # ── Stop & Report ─────────────────────────────────────────────────

    def _save_settings(self):
        self.settings.dual_ref_serial = self._ref_serial.text().strip()
        self.settings.dual_ref_manufacturer = self._ref_mfr.text().strip()
        self.settings.dual_ref_model = self._ref_model.text().strip()
        self.settings.dual_ref_r0 = self._ref_r0.value()
        self.settings.dual_ref_cert_no = self._ref_cert.text().strip()
        self.settings.dual_ref_cal_date = self._ref_caldate.text().strip()
        self.settings.dual_dut_serial = self._dut_serial.text().strip()
        self.settings.dual_dut_tag = self._dut_tag.text().strip()
        self.settings.dual_dut_manufacturer = self._dut_mfr.text().strip()
        self.settings.dual_dut_model = self._dut_model.text().strip()
        self.settings.dual_dut_r0 = self._dut_r0.value()
        self.settings.report_output_directory = self._dir_input.text().strip()
        self.settings.save_settings()

    def stop_worker_for_close(self):
        self._save_settings()
        self._running = False
        if getattr(self, "_worker", None):
            self._worker.stop()
        if getattr(self, "_source", None):
            try:
                self._source.close()
            except Exception:
                pass

    def stop_calibration(self):
        if not self._running:
            return

        self._save_settings()
        print("[DUAL] Stopping calibration...")
        self._running = False
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._adv_btn.setVisible(False)
        self._sb_state.setText("STOPPED")

        if self._worker:
            self._worker.stop()
            self._worker = None

        if self._source:
            try:
                self._source.close()
            except:
                pass

        # Also update point table for the last point if it completed
        if self._sequencer and self._sequencer.all_results:
            self._update_point_results_table()

        try:
            self._process_and_report()
        except Exception as e:
            import traceback
            print(f"[ERROR] Dual report failed:\n{traceback.format_exc()}")
            QMessageBox.critical(self, "Report Error", f"Failed to generate reports:\n{e}")

    def _process_and_report(self):
        if not self._dual_log:
            QMessageBox.warning(self, "No Data", "No data recorded; report not generated.")
            return

        out_dir = self._dir_input.text().strip()
        os.makedirs(out_dir, exist_ok=True)

        sn_dut = self._dut_serial.text().strip() or "DUT_UNKNOWN"
        ts = time.strftime("%Y%m%d_%H%M%S")

        # ── Export RAW CSV ────────────────────────────────────────
        csv_path = os.path.join(out_dir, f"DualCal_Raw_{sn_dut}_{ts}.csv")
        generate_dual_csv(csv_path, self._dual_log)

        # ── Process metrics & PDF ─────────────────────────────────
        if self._sequencer:
            agg = self._sequencer.aggregate_results()
            if not agg:
                print("[INFO] No complete points for PDF.")
                return

            # Save graph snapshot
            graph_path = os.path.join(out_dir, f"DualCal_Graph_{sn_dut}_{ts}.png")
            try:
                import pyqtgraph.exporters
                
                # Fix PDF graph scaling to tightly wrap the actual data
                view_width = 180.0
                max_x = max(self._buf_time) if self._buf_time else view_width
                min_x = max(0, max_x - view_width)
                
                # Filter delta data in this window
                vis_deltas = [d for t, d in zip(self._buf_time, self._buf_delta) if min_x <= t <= max_x]
                if vis_deltas:
                    d_min, d_max = min(vis_deltas), max(vis_deltas)
                    tc = self._tol_combo.currentText()
                    A, B = ISO_LIMITS.get(tc, (0.30, 0.005))
                    sp = self._sequencer.current_setpoint if self._sequencer else 0.0
                    tol = A + B * abs(sp)
                    
                    # Ensure range is at least ± tolerance, plus margin
                    y_min = min(d_min, -tol) * 1.2
                    y_max = max(d_max, tol) * 1.2
                    
                    # Prevent zero-range crash
                    if abs(y_max - y_min) < 1e-6:
                        y_min -= 0.1; y_max += 0.1
                        
                    self._plot_err.setYRange(y_min, y_max)
                    
                    # Also fix main temperature graph Y range
                    vis_refs = [r for t, r in zip(self._buf_time, self._buf_ref) if min_x <= t <= max_x]
                    vis_duts = [r for t, r in zip(self._buf_time, self._buf_dut) if min_x <= t <= max_x]
                    if vis_refs and vis_duts:
                        t_min = min(min(vis_refs), min(vis_duts))
                        t_max = max(max(vis_refs), max(vis_duts))
                        t_min = min(t_min, sp) - 5.0
                        t_max = max(t_max, sp) + 5.0
                        self._plot_main.setYRange(t_min, t_max)

                QApplication.processEvents()
                exporter = pyqtgraph.exporters.ImageExporter(self._graph_widget.scene())
                exporter.export(graph_path)
                
                # Restore auto-range for live UI after snapshot
                self._plot_err.enableAutoRange(axis='y')
                self._plot_main.enableAutoRange(axis='y')
            except Exception as e:
                print(f"[WARN] Dual graph snapshot failed ({e})")
                graph_path = ""

            pdf_path = os.path.join(out_dir, f"DualCal_Report_{sn_dut}_{ts}.pdf")
            try:
                # Mock identity mapping
                ident = {
                    "ref_serial": self._ref_serial.text(),
                    "ref_manufacturer": self._ref_mfr.text(),
                    "ref_model": self._ref_model.text(),
                    "dut_serial": self._dut_serial.text(),
                    "dut_manufacturer": self._dut_mfr.text(),
                    "dut_model": self._dut_model.text(),
                    "dut_tag": self._dut_tag.text(),
                    "is_demonstration": self._is_demo,
                    "tolerance_class": self._tol_combo.currentText(),
                    "rtd_wires": 4, # Both channels 4-wire
                    "ref_r0": self._ref_r0.value(),
                    "dut_r0": self._dut_r0.value(),
                    "ref_cert_no": self._ref_cert.text(),
                    "ref_cal_date": self._ref_caldate.text(),
                    "ref_u_expanded": self._uref_spin.value(),
                    "ref_u_k": 2,
                    "operator_name": self._op_input.text(),
                    "certificate_no": f"DUAL-{ts}",
                }
                generate_dual_pdf(pdf_path, graph_path, agg, ident)

                # Correction CSV
                corr_path = os.path.join(out_dir, f"DualCal_Correction_{sn_dut}_{ts}.csv")
                generate_dual_correction_csv(corr_path, agg.get("per_point", []))

                # History record
                if sn_dut and not self.asset_manager.get_instrument(sn_dut):
                    self.asset_manager.save_instrument(SensorAsset(
                        serial_number=sn_dut,
                        equipment_tag=self._dut_tag.text(),
                        manufacturer=self._dut_mfr.text(),
                        model=self._dut_model.text(),
                        nominal_r0=self._dut_r0.value()
                    ))

                pts = agg.get("per_point", [])
                sp_json = json.dumps([r["setpoint"] for r in pts] if pts else [])
                max_u = max((r.get("as_left_U", 0.0) for r in pts), default=0.0)
                
                rec = CalibrationRecord(
                    serial_number=sn_dut,
                    cal_date=time.strftime("%Y-%m-%d"),
                    certificate_no=AssetManager.generate_certificate_number(sn_dut),
                    mode=f"DUAL {'DEMO' if self._is_demo else 'FULL'}",
                    setpoints=sp_json,
                    verdict=agg.get("overall_verdict", "N/A"),
                    mbe=agg.get("mean_delta", 0.0),
                    rmse=agg.get("rmse_delta", 0.0),
                    mae=agg.get("mae_delta", 0.0),
                    std_dev=agg.get("std_delta", 0.0),
                    expanded_uncertainty=max_u,
                    pdf_path=pdf_path,
                    operator_name=self._op_input.text(),
                    tolerance_class=self._tol_combo.currentText(),
                    ambient_temp=getattr(self.settings, "ambient_temperature", 0.0),
                    humidity=getattr(self.settings, "relative_humidity", 0.0),
                    pressure=getattr(self.settings, "atmospheric_pressure", 0.0)
                )
                self.asset_manager.save_calibration(rec)
                QMessageBox.information(self, "Complete",
                                        f"Calibration stopped.\nReports saved to:\n{out_dir}")
                
                import sys, subprocess
                try:
                    if sys.platform.startswith("win"):
                        os.startfile(pdf_path)
                    elif sys.platform.startswith("darwin"):
                        subprocess.run(["open", pdf_path], check=False)
                    else:
                        subprocess.run(["xdg-open", pdf_path], check=False)
                except Exception as e:
                    print(f"Failed to open PDF: {e}")
                    
            except Exception as e:
                raise e

    def _load_history(self):
        records = self.asset_manager.get_all_records()
        self._hist_table.setRowCount(0)
        # Filter for DUAL records only
        dual_recs = [r for r in records if "DUAL" in r.mode]
        for rec in dual_recs:
            r = self._hist_table.rowCount()
            self._hist_table.insertRow(r)
            vi = QTableWidgetItem(rec.verdict)
            vi.setForeground(QColor("#2e7d32") if rec.verdict == "PASS" else QColor("#c62828"))

            self._hist_table.setItem(r, 0, QTableWidgetItem(rec.cal_date))
            self._hist_table.setItem(r, 1, QTableWidgetItem(rec.serial_number))
            self._hist_table.setItem(r, 2, vi)
            self._hist_table.setItem(r, 3, QTableWidgetItem(f"{rec.mbe:+.4f}"))
            self._hist_table.setItem(r, 4, QTableWidgetItem(f"{rec.rmse:.4f}"))
            self._hist_table.setItem(r, 5, QTableWidgetItem("N/A"))
            self._hist_table.setItem(r, 6, QTableWidgetItem(os.path.basename(rec.pdf_path)))
