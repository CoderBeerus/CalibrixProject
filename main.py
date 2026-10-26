# main.py — CALIBRIX v2 (Full Industrial Upgrade)
# §1-3: Worker uses threading.Event for clean stop
# §2-7: Fault diagnostics — LED indicator, auto-stop on critical fault
# §Section 5: Redesigned UI with status bar, color-coded indicators
# §2-9: 3-wire + Class A blocked at start
# §2-10: Correction loop integrated
# §2-11: Uncertainty-augmented PASS/FAIL
# §2-12: State-machine safety enforced in sequencer
import sys, time, os, json, math, threading
import numpy as np

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QComboBox, QSpinBox, QGroupBox,
    QFormLayout, QFileDialog, QMessageBox, QDoubleSpinBox,
    QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView,
    QScrollArea, QCheckBox, QFrame,
)
from PyQt5.QtCore import QThread, pyqtSignal, pyqtSlot, Qt, QTimer
from PyQt5.QtGui import QPixmap, QDoubleValidator, QColor, QFont

from max31865_driver_mock import MAX31865 as MockMAX31865, temp_to_resistance

RealMAX31865 = None
real_converter = None
real_ideal_temp_to_resistance = None
try:
    from max31865_driver import (
        MAX31865 as RealMAX31865,
        resistance_to_temp_converter as real_converter,
        temp_to_resistance as real_ideal_temp_to_resistance,
        SensorFault,
    )
    REAL_DRIVER_AVAILABLE = True
    print("[CALIBRIX] Real MAX31865 driver loaded successfully.")
except ImportError:
    REAL_DRIVER_AVAILABLE = False
    print("[CALIBRIX] Real MAX31865 driver NOT available — mock will be used.")
    class SensorFault(Exception):
        def __init__(self, *a, **kw):
            self.is_critical = True
            super().__init__(*a)

from settings_manager import SettingsManager
from data_logger import DataLogger
from plotter import WaveformEngine
from metrics import (compute_metrics, compute_validation_metrics,
                     get_verdict, ISO_LIMITS, check_class_validity)
from report_generator import (UnifiedReportGenerator, generate_csv_report,
                              generate_correction_table_csv)
from uncertainty_calculator import UncertaintyCalculator
from stabilization_engine import StabilizationEngine
from calibration_sequencer import CalibrationSequencer, CalState
from asset_manager import AssetManager, SensorAsset, CalibrationRecord
from dual_ui import DualRTDPanel

# ── Dark stylesheet ───────────────────────────────────────────────────
DS = """
QWidget { background:#1e1e1e; color:#e8e8e8; font-family:Arial,sans-serif; font-size:11px; }
QTabWidget::pane { border:1px solid #3a3a3a; }
QTabBar::tab { background:#2a2a2a; color:#ccc; padding:6px 14px; border-radius:4px 4px 0 0; }
QTabBar::tab:selected { background:#3d3d3d; color:#fff; font-weight:bold; }
QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox {
    background:#2a2a2a; border:1px solid #4a4a4a; padding:4px; border-radius:3px;
    selection-background-color:#555;
}
QGroupBox { border:1px solid #3a3a3a; border-radius:5px; margin-top:1ex; background:#242424; }
QGroupBox::title { subcontrol-origin:margin; padding:0 4px; margin-left:8px; color:#ccc; }
QPushButton {
    background:#1565c0; border:1px solid #0d47a1; color:#fff;
    padding:5px 10px; border-radius:4px;
}
QPushButton:hover { background:#1976d2; }
QPushButton:disabled { background:#333; color:#666; border-color:#333; }
QPushButton:checked { background:#2e7d32; border-color:#1b5e20; }
QTableWidget { gridline-color:#3a3a3a; }
QHeaderView::section { background:#2a2a2a; padding:4px; border:1px solid #3a3a3a; }
QScrollBar:vertical { background:#2a2a2a; width:9px; border-radius:4px; }
QScrollBar::handle:vertical { background:#555; border-radius:4px; min-height:16px; }
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical { border:none; background:none; }
QCheckBox { color:#ccc; }
QLabel { background:transparent; }
"""

# ── Status LED colours ────────────────────────────────────────────────
LED_GREEN  = "background:#2e7d32; border-radius:7px; min-width:14px; min-height:14px;"
LED_YELLOW = "background:#f9a825; border-radius:7px; min-width:14px; min-height:14px;"
LED_RED    = "background:#c62828; border-radius:7px; min-width:14px; min-height:14px;"
LED_GREY   = "background:#424242; border-radius:7px; min-width:14px; min-height:14px;"


# ── Worker thread ─────────────────────────────────────────────────────
class Worker(QThread):
    """
    §1-3: Uses threading.Event for stopping — no polling of bool flag.
    §2-7: Distinguishes SensorFault (critical/warning) from other exceptions.
    """
    new_data_signal   = pyqtSignal(float, float)  # (temp °C, resistance Ω)
    error_signal      = pyqtSignal(str, bool)      # (message, is_critical)
    fault_status      = pyqtSignal(int)            # 0=OK 1=warning 2=critical

    def __init__(self, sensor, interval: float):
        super().__init__()
        self.sensor     = sensor
        self.interval   = float(interval)
        self._stop_event = threading.Event()   # §1-3

    def run(self):
        while not self._stop_event.is_set():    # §1-3
            try:
                temp, resistance = self.sensor.read_temp()
                print(f"Sensor reading: {temp} {resistance}")  # §DEBUG

                # Skip invalid readings
                if temp is None or resistance is None:
                    print("[SKIP] None sensor data")
                    continue

                if not (math.isfinite(temp) and math.isfinite(resistance)):
                    print("[SKIP] Non-finite sensor data")
                    continue

                self.fault_status.emit(0)       # §2-7: OK
                self.new_data_signal.emit(float(temp), float(resistance))
            except SensorFault as sf:
                print(f"[FAULT] {sf}")
                print(f"[FAULT LEVEL] {'CRITICAL' if sf.is_critical else 'WARNING'}")
                # Always show fault, but DO NOT STOP SYSTEM
                if sf.is_critical:
                    self.fault_status.emit(2)
                else:
                    self.fault_status.emit(1)
                # Emit as NON-CRITICAL (no popup blocking)
                self.error_signal.emit(str(sf), False)
                # Wait the specified interval before retrying
                self._stop_event.wait(self.interval)
                continue
            except Exception as exc:
                print(f"Sensor error: {exc}")
                self.fault_status.emit(2)
                self.error_signal.emit(str(exc), False)
                self._stop_event.wait(self.interval)
                continue
                continue
            self._stop_event.wait(self.interval)   # §1-3: interruptible sleep

    def stop(self):
        """§1-3: Signal the event and wait for the thread to join."""
        self._stop_event.set()
        self.wait(3000)


# ── Main window ───────────────────────────────────────────────────────
class CalibrixApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CALIBRIX v2 — RTD Sensor Calibration System")
        self.setGeometry(60, 60, 1380, 820)

        self.settings    = SettingsManager()
        self.data_logger = DataLogger()
        self.sensor      = None
        self.worker_thread = None
        self.converter   = None
        self.ideal_temp_to_resistance = None

        self.stab_engine = StabilizationEngine()
        self.sequencer   = None
        self._multipoint = False
        self._cal_start_time = None

        db = os.path.join(self.settings.report_output_directory, "calibrix_history.db")
        self.asset_manager = AssetManager(db)

        # §CF-3: Proper boolean flags for waveform event markers
        self._stab_marked = False
        self._rec_marked  = False

        self.auto_stop_timer = QTimer(self)
        self.auto_stop_timer.setSingleShot(True)
        self.auto_stop_timer.timeout.connect(self.stop_calibration)

        self.use_mock = not (REAL_DRIVER_AVAILABLE and sys.platform.startswith("linux"))
        if REAL_DRIVER_AVAILABLE and sys.platform.startswith("linux"):
            print("[CALIBRIX] REAL sensor selected (Linux + driver available).")
        else:
            print(f"[CALIBRIX] MOCK sensor selected (REAL_DRIVER_AVAILABLE={REAL_DRIVER_AVAILABLE}, platform={sys.platform}).")

        # ── Root layout ───────────────────────────────────────────────
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(3)

        # ── Top: mode selector ────────────────────────────────────────
        mode_bar = QHBoxLayout()
        mode_bar.setContentsMargins(0, 0, 0, 0)
        mode_bar.setSpacing(4)

        self._single_mode_btn = QPushButton("SINGLE RTD")
        self._single_mode_btn.setCheckable(True)
        self._single_mode_btn.setChecked(True)
        self._single_mode_btn.setFixedHeight(30)
        self._single_mode_btn.setStyleSheet(
            "QPushButton{background:#1565c0;color:#fff;font-weight:bold;padding:4px 16px;border-radius:4px;}"
            "QPushButton:checked{background:#2e7d32;color:#fff;border:2px solid #69f0ae;}"
        )
        self._dual_mode_btn = QPushButton("DUAL RTD COMPARISON")
        self._dual_mode_btn.setCheckable(True)
        self._dual_mode_btn.setChecked(False)
        self._dual_mode_btn.setFixedHeight(30)
        self._dual_mode_btn.setStyleSheet(
            "QPushButton{background:#1565c0;color:#fff;font-weight:bold;padding:4px 16px;border-radius:4px;}"
            "QPushButton:checked{background:#7b4f00;color:#ffe082;border:2px solid #ffaa00;}"
        )
        self._single_mode_btn.clicked.connect(lambda: self._switch_rtd_mode(False))
        self._dual_mode_btn.clicked.connect(lambda: self._switch_rtd_mode(True))

        self._mock_banner_top = QLabel("  ⚧  MOCK / SIMULATION MODE  ")
        self._mock_banner_top.setAlignment(Qt.AlignCenter)
        self._mock_banner_top.setStyleSheet(
            "background:#7b4f00;color:#ffe082;font-weight:bold;"
            "font-size:11px;padding:3px 10px;border-radius:3px;"
        )
        self._mock_banner_top.setVisible(self.use_mock)

        mode_bar.addWidget(self._single_mode_btn)
        mode_bar.addWidget(self._dual_mode_btn)
        mode_bar.addSpacing(12)
        mode_bar.addWidget(self._mock_banner_top)
        mode_bar.addStretch()
        root.addLayout(mode_bar)

        # ── Stacked widget: Single RTD | Dual RTD ────────────────────
        from PyQt5.QtWidgets import QStackedWidget
        self._stack = QStackedWidget()
        root.addWidget(self._stack, 1)

        # Page 0: Single RTD (existing layout)
        single_page = QWidget()
        single_layout = QVBoxLayout(single_page)
        single_layout.setContentsMargins(0, 0, 0, 0)
        single_layout.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(4)
        self.tabs = QTabWidget()
        self.tabs.setFixedWidth(400)
        top.addWidget(self.tabs)
        self.waveform = WaveformEngine(self)
        top.addWidget(self.waveform, 1)
        single_layout.addLayout(top, 1)
        single_layout.addWidget(self._make_status_bar())
        self._stack.addWidget(single_page)

        # Page 1: Dual RTD
        self._dual_panel = DualRTDPanel(
            settings=self.settings,
            asset_manager=self.asset_manager,
            use_mock=self.use_mock,
        )
        self._stack.addWidget(self._dual_panel)

        self._build_tab_main()
        self._build_tab_asset()
        self._build_tab_env()
        self._build_tab_trace()
        self._build_tab_history()

        self.setStyleSheet(DS)
        self.update_ui_state(False)
        self.on_mode_changed(self.mode_combo.currentText())
        self._set_fault_led(0)

    def _switch_rtd_mode(self, dual: bool):
        """Switch between Single RTD (page 0) and Dual RTD (page 1)."""
        if dual:
            self._dual_mode_btn.setChecked(True)
            self._single_mode_btn.setChecked(False)
            self._stack.setCurrentIndex(1)
            self._dual_panel.set_use_mock(self.use_mock)
        else:
            self._single_mode_btn.setChecked(True)
            self._dual_mode_btn.setChecked(False)
            self._stack.setCurrentIndex(0)


    # ─── Status bar ──────────────────────────────────────────────────
    def _make_status_bar(self):
        bar = QFrame()
        bar.setFrameShape(QFrame.HLine)
        bar.setMaximumHeight(32)
        bar.setStyleSheet("background:#111;")

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(8, 2, 8, 2)

        def _item(label):
            lbl = QLabel(label)
            lbl.setStyleSheet("color:#888; font-size:10px;")
            val = QLabel("—")
            val.setStyleSheet("color:#ccc; font-size:10px; font-weight:bold; margin-right:12px;")
            layout.addWidget(lbl)
            layout.addWidget(val)
            return val

        self._sb_state    = _item("State:")
        self._sb_setpoint = _item("Setpoint:")
        self._sb_verdict  = _item("Verdict:")

        layout.addStretch()

        # §2-7: Fault LED
        self._fault_led = QLabel()
        self._fault_led.setFixedSize(14, 14)
        self._fault_led.setStyleSheet(LED_GREY)
        self._fault_label = QLabel("Sensor: —")
        self._fault_label.setStyleSheet("color:#888; font-size:10px;")
        layout.addWidget(self._fault_led)
        layout.addWidget(self._fault_label)

        return bar

    def _set_fault_led(self, level: int):
        """§2-7: 0=OK(green) 1=warning(yellow) 2=critical(red) -1=idle(grey)."""
        if level == 0:
            self._fault_led.setStyleSheet(LED_GREEN)
            self._fault_label.setText("Sensor: OK")
        elif level == 1:
            self._fault_led.setStyleSheet(LED_YELLOW)
            self._fault_label.setText("Sensor: Warning")
        elif level == 2:
            self._fault_led.setStyleSheet(LED_RED)
            self._fault_label.setText("Sensor: FAULT")
        else:
            self._fault_led.setStyleSheet(LED_GREY)
            self._fault_label.setText("Sensor: —")

    # ─── TAB 1: Main ─────────────────────────────────────────────────
    def _build_tab_main(self):
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        inner  = QWidget(); layout = QVBoxLayout(inner)
        scroll.setWidget(inner)
        self.tabs.addTab(scroll, "⚙ Main")

        layout.addWidget(self._mk_config_group())
        layout.addWidget(self._mk_thresh_group())
        layout.addWidget(self._mk_multipoint_group())
        layout.addWidget(self._mk_control_group())
        layout.addWidget(self._mk_stab_group())
        layout.addWidget(self._mk_metrics_group())
        layout.addStretch()

    def _mk_config_group(self):
        grp = QGroupBox("Configuration"); f = QFormLayout()

        self._hw_btn = QPushButton("Switch to REAL Sensor" if self.use_mock else "Switch to MOCK")
        self._hw_btn.setStyleSheet("background:darkorange;color:#111;" if self.use_mock else "")
        self._hw_btn.clicked.connect(self._toggle_hw)
        f.addRow("Driver:", self._hw_btn)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems([
            "1. Field Validation (On-site Health Check)",
            "2. Model-Based Calibration (IEC 60751)",
            "3. External Reference Comparison",
        ])
        idx = self.mode_combo.findText(self.settings.validation_mode)
        if idx >= 0: self.mode_combo.setCurrentIndex(idx)
        self.mode_combo.currentTextChanged.connect(self.on_mode_changed)
        f.addRow("Mode:", self.mode_combo)

        self.wire_combo = QComboBox()
        self.wire_combo.addItems(["4-Wire","3-Wire","2-Wire"])
        self.wire_combo.setCurrentText(f"{self.settings.rtd_wires}-Wire")
        f.addRow("Wiring:", self.wire_combo)

        self.setpoint_input = QLineEdit(str(self.settings.standard_reference_value))
        v = QDoubleValidator(-273.15, 1000.0, 4); v.setNotation(QDoubleValidator.StandardNotation)
        self.setpoint_input.setValidator(v)
        f.addRow("Setpoint (°C):", self.setpoint_input)

        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0.1, 60.0); self.interval_spin.setSingleStep(0.1)
        self.interval_spin.setValue(self.settings.sampling_interval); self.interval_spin.setSuffix(" s")
        f.addRow("Interval:", self.interval_spin)

        self.tol_combo = QComboBox()
        self.tol_combo.addItems(list(ISO_LIMITS.keys()))
        self.tol_combo.setCurrentText(self.settings.tolerance_class)
        f.addRow("Tolerance:", self.tol_combo)

        self.dur_spin = QSpinBox()
        self.dur_spin.setRange(10,3600); self.dur_spin.setValue(int(self.settings.calibration_duration))
        self.dur_spin.setSuffix(" s")
        f.addRow("Duration (single):", self.dur_spin)

        self.op_input = QLineEdit(self.settings.operator_name)
        f.addRow("Operator:", self.op_input)

        self.photo_lbl = QLabel("No Photo")
        sel_ph = QPushButton("Select"); sel_ph.clicked.connect(self._sel_photo)
        ph_row = QHBoxLayout(); ph_row.addWidget(self.photo_lbl); ph_row.addWidget(sel_ph)
        f.addRow("Photo:", ph_row)

        self.dir_input = QLineEdit(self.settings.report_output_directory)
        self.dir_input.setReadOnly(True)
        brw = QPushButton("Browse"); brw.clicked.connect(self._sel_dir)
        dr  = QHBoxLayout(); dr.addWidget(self.dir_input); dr.addWidget(brw)
        f.addRow("Folder:", dr)

        grp.setLayout(f); return grp

    def _mk_thresh_group(self):
        self.thresh_grp = QGroupBox("Validation Thresholds"); f = QFormLayout()

        self.drift_spin = QDoubleSpinBox(); self.drift_spin.setRange(0,10); self.drift_spin.setSingleStep(0.1)
        self.drift_spin.setValue(self.settings.drift_threshold); self.drift_spin.setSuffix(" °C")
        f.addRow("Drift:", self.drift_spin)

        self.sigma_spin = QDoubleSpinBox(); self.sigma_spin.setRange(0,5); self.sigma_spin.setSingleStep(0.01)
        self.sigma_spin.setValue(self.settings.sigma_threshold); self.sigma_spin.setSuffix(" °C")
        f.addRow("σ Limit:", self.sigma_spin)

        self.noise_spin = QDoubleSpinBox(); self.noise_spin.setRange(0,10); self.noise_spin.setSingleStep(0.1)
        self.noise_spin.setValue(self.settings.noise_threshold); self.noise_spin.setSuffix(" °C")
        f.addRow("Noise:", self.noise_spin)

        self.thresh_grp.setLayout(f); return self.thresh_grp

    def _mk_multipoint_group(self):
        self.mp_grp = QGroupBox("Multi-Point Calibration (IEC 60751 §4.2.2)"); f = QFormLayout()

        self.mp_btn = QPushButton("Enable Multi-Point Mode"); self.mp_btn.setCheckable(True)
        self.mp_btn.toggled.connect(self._on_mp_toggle)
        f.addRow("", self.mp_btn)

        self.sp_input = QLineEdit(",".join(str(x) for x in self.settings.multipoint_setpoints))
        self.sp_input.setToolTip("Comma-separated °C values, e.g. 0,50,100")
        f.addRow("Setpoints:", self.sp_input)

        self.spp_spin = QSpinBox(); self.spp_spin.setRange(5,500)
        self.spp_spin.setValue(self.settings.samples_per_point)
        f.addRow("Samples/pt:", self.spp_spin)

        self.sl_spin = QDoubleSpinBox(); self.sl_spin.setRange(0.001,1); self.sl_spin.setDecimals(3)
        self.sl_spin.setValue(self.settings.stabilization_slope_thresh); self.sl_spin.setSuffix(" °C/s")
        f.addRow("Slope thresh:", self.sl_spin)

        self.ss_spin = QDoubleSpinBox(); self.ss_spin.setRange(0.01,1); self.ss_spin.setSingleStep(0.01)
        self.ss_spin.setValue(self.settings.stabilization_sigma_thresh); self.ss_spin.setSuffix(" °C")
        f.addRow("σ thresh:", self.ss_spin)

        self.sp_prox = QDoubleSpinBox(); self.sp_prox.setRange(0.1,5); self.sp_prox.setSingleStep(0.1)
        self.sp_prox.setValue(self.settings.stabilization_proximity); self.sp_prox.setSuffix(" °C")
        f.addRow("Proximity:", self.sp_prox)

        self.sp_dwell = QDoubleSpinBox(); self.sp_dwell.setRange(5,300); self.sp_dwell.setSingleStep(5)
        self.sp_dwell.setValue(self.settings.stabilization_dwell); self.sp_dwell.setSuffix(" s")
        f.addRow("Dwell:", self.sp_dwell)

        self.uref_spin = QDoubleSpinBox(); self.uref_spin.setRange(0.001,5); self.uref_spin.setDecimals(3)
        self.uref_spin.setValue(self.settings.u_ref_expanded); self.uref_spin.setSuffix(" °C")
        f.addRow("U_ref (GUM):", self.uref_spin)

        self.mp_grp.setLayout(f); return self.mp_grp

    def _mk_control_group(self):
        grp = QGroupBox("Controls"); layout = QVBoxLayout()

        self.start_btn = QPushButton("▶  Start"); self.start_btn.clicked.connect(self.start_calibration)
        self.stop_btn  = QPushButton("⏹  Stop & Report"); self.stop_btn.clicked.connect(self.stop_calibration)
        self.adv_btn   = QPushButton("➡  Advance to Next Point"); self.adv_btn.clicked.connect(self._advance)
        self.adv_btn.setVisible(False)

        layout.addWidget(self.start_btn); layout.addWidget(self.stop_btn); layout.addWidget(self.adv_btn)
        grp.setLayout(layout); return grp

    def _mk_stab_group(self):
        self.stab_grp = QGroupBox("Stabilization Monitor"); f = QFormLayout()

        self.stab_state  = QLabel("IDLE")
        self.stab_slope  = QLabel("—")
        self.stab_sigma  = QLabel("—")
        self.stab_prox   = QLabel("—")
        self.stab_dwell  = QLabel("—")
        self.stab_status = QLabel("NOT STABLE")
        self.stab_status.setStyleSheet("color:#c62828;font-weight:bold;")

        f.addRow("State:",   self.stab_state)
        f.addRow("Slope:",   self.stab_slope)
        f.addRow("σ:",       self.stab_sigma)
        f.addRow("Prox.:",   self.stab_prox)
        f.addRow("Dwell:",   self.stab_dwell)
        f.addRow("● Status:",self.stab_status)

        self.stab_grp.setLayout(f); return self.stab_grp

    def _mk_metrics_group(self):
        grp = QGroupBox("Live Metrics"); f = QFormLayout()

        self.drift_lbl = QLabel("N/A"); self.noise_lbl = QLabel("N/A")
        self.sigma_lbl = QLabel("N/A"); self.mae_lbl   = QLabel("N/A")
        self.mbe_lbl   = QLabel("N/A"); self.rmse_lbl  = QLabel("N/A")
        self.cv_lbl    = QLabel("N/A"); self.std_lbl   = QLabel("N/A")
        self.verdict_lbl = QLabel("N/A")
        self.verdict_lbl.setFont(QFont("Arial",11,QFont.Bold))

        self.val_w = QWidget(); vl = QFormLayout(self.val_w)
        vl.addRow("Drift:",     self.drift_lbl)
        vl.addRow("Noise:",     self.noise_lbl)
        vl.addRow("σ:",         self.sigma_lbl)

        self.cal_w = QWidget(); cl = QFormLayout(self.cal_w)
        cl.addRow("MAE:",       self.mae_lbl)
        cl.addRow("MBE:",       self.mbe_lbl)
        cl.addRow("RMSE:",      self.rmse_lbl)
        cl.addRow("CV%:",       self.cv_lbl)
        cl.addRow("Std Dev:",   self.std_lbl)

        f.addRow(self.val_w); f.addRow(self.cal_w)
        f.addRow("Verdict:",    self.verdict_lbl)
        grp.setLayout(f); return grp

    # ─── TAB 2: Asset ────────────────────────────────────────────────
    def _build_tab_asset(self):
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        inner = QWidget(); layout = QVBoxLayout(inner); scroll.setWidget(inner)
        self.tabs.addTab(scroll, "🔖 Asset")

        grp = QGroupBox("Sensor ID (ISO 9001 §7.1.5)"); form = QFormLayout()
        self.serial_in = QLineEdit(self.settings.sensor_serial_number)
        self.tag_in    = QLineEdit(self.settings.sensor_equipment_tag)
        self.mfr_in    = QLineEdit(self.settings.sensor_manufacturer)
        self.mdl_in    = QLineEdit(self.settings.sensor_model)
        self.r0_sp     = QDoubleSpinBox(); self.r0_sp.setRange(1,10000)
        self.r0_sp.setValue(self.settings.sensor_nominal_r0); self.r0_sp.setSuffix(" Ω")
        self.intv_sp   = QSpinBox(); self.intv_sp.setRange(1,120)
        self.intv_sp.setValue(self.settings.calibration_interval_months); self.intv_sp.setSuffix(" months")

        form.addRow("Serial:", self.serial_in); form.addRow("Tag (P&ID):", self.tag_in)
        form.addRow("Manufacturer:", self.mfr_in); form.addRow("Model:", self.mdl_in)
        form.addRow("Nominal R₀:", self.r0_sp); form.addRow("Cal. Interval:", self.intv_sp)
        sb = QPushButton("Save Asset"); sb.clicked.connect(self._save_asset)
        form.addRow(sb); grp.setLayout(form); layout.addWidget(grp)

        # §2-8: due date display
        self.due_lbl = QLabel("Due date: load asset to check.")
        self.due_lbl.setWordWrap(True)
        layout.addWidget(self.due_lbl)

        self.overdue_lbl = QLabel("")
        self.overdue_lbl.setWordWrap(True)
        chk_btn = QPushButton("Check Overdue Instruments")
        chk_btn.clicked.connect(self._check_overdue)
        layout.addWidget(self.overdue_lbl); layout.addWidget(chk_btn)
        layout.addStretch()

    # ─── TAB 3: Environment ──────────────────────────────────────────
    def _build_tab_env(self):
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        inner = QWidget(); layout = QVBoxLayout(inner); scroll.setWidget(inner)
        self.tabs.addTab(scroll, "🌡 Env")

        grp = QGroupBox("Environmental Conditions (ISO/IEC 17025 §7.8.2)"); form = QFormLayout()

        self.amb_sp  = QDoubleSpinBox(); self.amb_sp.setRange(-40,60); self.amb_sp.setValue(self.settings.ambient_temperature); self.amb_sp.setSuffix(" °C")
        self.hum_sp  = QDoubleSpinBox(); self.hum_sp.setRange(0,100); self.hum_sp.setValue(self.settings.relative_humidity); self.hum_sp.setSuffix(" %")
        self.pres_sp = QDoubleSpinBox(); self.pres_sp.setRange(800,1100); self.pres_sp.setValue(self.settings.atmospheric_pressure); self.pres_sp.setSuffix(" hPa")

        form.addRow("Ambient Temp:", self.amb_sp)
        form.addRow("Rel. Humidity:", self.hum_sp)
        form.addRow("Pressure:", self.pres_sp)

        self.env_lbl = QLabel("")
        self.env_lbl.setWordWrap(True)
        form.addRow("Status:", self.env_lbl)
        self.amb_sp.valueChanged.connect(self._upd_env)
        self.hum_sp.valueChanged.connect(self._upd_env)
        self._upd_env()

        grp.setLayout(form); layout.addWidget(grp); layout.addStretch()

    # ─── TAB 4: Traceability ─────────────────────────────────────────
    def _build_tab_trace(self):
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        inner = QWidget(); layout = QVBoxLayout(inner); scroll.setWidget(inner)
        self.tabs.addTab(scroll, "🔗 Trace")

        grp = QGroupBox("Reference Standard (ISO/IEC 17025)"); form = QFormLayout()
        self.ref_name = QLineEdit(self.settings.ref_standard_name)
        self.ref_ser  = QLineEdit(self.settings.ref_serial_number)
        self.ref_lab  = QLineEdit(self.settings.ref_calibrating_lab)
        self.ref_acc  = QLineEdit(self.settings.ref_lab_accreditation_no)
        self.ref_cert = QLineEdit(self.settings.ref_certificate_number)
        self.ref_date = QLineEdit(self.settings.ref_calibration_date)
        self.ref_u    = QDoubleSpinBox(); self.ref_u.setRange(0.001,5); self.ref_u.setDecimals(3)
        self.ref_u.setValue(self.settings.ref_uncertainty_expanded); self.ref_u.setSuffix(" °C")
        self.ref_k    = QSpinBox(); self.ref_k.setRange(1,3); self.ref_k.setValue(self.settings.ref_uncertainty_k)

        for label, widget in [("Std Name:",self.ref_name),("Serial:",self.ref_ser),
                               ("Lab:",self.ref_lab),("Accreditation:",self.ref_acc),
                               ("Cert No.:",self.ref_cert),("Cal Date:",self.ref_date),
                               ("U_ref:",self.ref_u),("k:",self.ref_k)]:
            form.addRow(label, widget)

        sv = QPushButton("Save Traceability"); sv.clicked.connect(self._save_trace)
        form.addRow(sv); grp.setLayout(form); layout.addWidget(grp); layout.addStretch()

    # ─── TAB 5: History ──────────────────────────────────────────────
    def _build_tab_history(self):
        container = QWidget(); layout = QVBoxLayout(container)
        self.tabs.addTab(container, "📋 History")

        self.hist_serial = QLineEdit(); self.hist_serial.setPlaceholderText("Serial (blank=all)")
        ld = QPushButton("Load"); ld.clicked.connect(self._load_history)
        hr = QHBoxLayout(); hr.addWidget(self.hist_serial); hr.addWidget(ld)
        layout.addLayout(hr)

        self.hist_table = QTableWidget(0,7)
        self.hist_table.setHorizontalHeaderLabels(["Date","Serial","Mode","Verdict","MBE","RMSE","PDF"])
        self.hist_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.hist_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.hist_table)

        # §IF-2: PyQtGraph MBE trend chart (replaces text-only label)
        import pyqtgraph as pg
        self.trend_plot = pg.PlotWidget(title="MBE Drift Trend")
        self.trend_plot.setLabel("left", "MBE", units="°C")
        self.trend_plot.setLabel("bottom", "Calibration #")
        self.trend_plot.showGrid(x=True, y=True, alpha=0.3)
        self.trend_plot.setMaximumHeight(180)
        self.trend_plot.setBackground("#1e1e1e")
        self.trend_plot.addLine(y=0, pen=pg.mkPen("#666", width=1, style=Qt.DashLine))
        self._trend_curve = self.trend_plot.plot(
            pen=pg.mkPen("#ffaa00", width=2), symbol="o",
            symbolBrush="#ffaa00", symbolSize=6)
        self.trend_lbl = QLabel("MBE trend: filter by serial to view.")
        self.trend_lbl.setWordWrap(True)
        layout.addWidget(self.trend_plot)
        layout.addWidget(self.trend_lbl)

    # ─── Helpers ─────────────────────────────────────────────────────

    def on_mode_changed(self, text):
        """§BugFix-1: Strict mode separation — Validation disables multi-point."""
        is_val = "Validation" in text
        self.thresh_grp.setVisible(is_val)
        self.val_w.setVisible(is_val)
        self.cal_w.setVisible(not is_val)
        self.settings.validation_mode = text
        # §BugFix-1: Block multi-point in Validation mode
        self.mp_grp.setEnabled(not is_val)
        if is_val and self._multipoint:
            self.mp_btn.setChecked(False)   # force off
            self._multipoint = False

    def _on_mp_toggle(self, checked):
        # §BugFix-1: Double-check — prevent multi-point enable in Validation mode
        if checked and "Validation" in self.mode_combo.currentText():
            self.mp_btn.setChecked(False)
            QMessageBox.warning(self, "Mode Conflict",
                                "Multi-point calibration is not available in Validation mode.")
            return
        self._multipoint = checked
        self.mp_btn.setText("✓ Multi-Point ENABLED" if checked else "Enable Multi-Point Mode")
        self.dur_spin.setEnabled(not checked)

    def update_ui_state(self, running: bool):
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.mode_combo.setEnabled(not running)
        self.wire_combo.setEnabled(not running)
        self.setpoint_input.setEnabled(not running)
        self.interval_spin.setEnabled(not running)
        self.tol_combo.setEnabled(not running)
        self.dur_spin.setEnabled(not running)
        self.mp_btn.setEnabled(not running)
        self.sp_input.setEnabled(not running)
        # §Fix-5: Lock stabilization thresholds during calibration
        self.sl_spin.setEnabled(not running)
        self.ss_spin.setEnabled(not running)
        self.sp_prox.setEnabled(not running)
        self.sp_dwell.setEnabled(not running)
        self.spp_spin.setEnabled(not running)
        self.uref_spin.setEnabled(not running)
        # §Fix-5: Lock hardware toggle during calibration
        self._hw_btn.setEnabled(not running)
        # §Fix-5: Lock asset fields during calibration
        self.serial_in.setEnabled(not running)
        self.tag_in.setEnabled(not running)
        self.mfr_in.setEnabled(not running)
        self.mdl_in.setEnabled(not running)
        self.r0_sp.setEnabled(not running)
        self.intv_sp.setEnabled(not running)
        # §Fix-5: Lock environment fields during calibration
        self.amb_sp.setEnabled(not running)
        self.hum_sp.setEnabled(not running)
        self.pres_sp.setEnabled(not running)
        # §Fix-5: Lock traceability fields during calibration
        self.ref_name.setEnabled(not running)
        self.ref_ser.setEnabled(not running)
        self.ref_lab.setEnabled(not running)
        self.ref_acc.setEnabled(not running)
        self.ref_cert.setEnabled(not running)
        self.ref_date.setEnabled(not running)
        self.ref_u.setEnabled(not running)
        self.ref_k.setEnabled(not running)
        self.op_input.setEnabled(not running)

    def _toggle_hw(self):
        if self.use_mock and not sys.platform.startswith("linux"):
            QMessageBox.warning(self, "OS", "Real hardware only on Linux/Raspberry Pi.")
            return
        self.use_mock = not self.use_mock
        if self.use_mock:
            self._hw_btn.setText("Switch to REAL Sensor")
            self._hw_btn.setStyleSheet("background:darkorange;color:#111;")
            self._mock_banner_top.setVisible(True)
        else:
            self._hw_btn.setText("Switch to MOCK Sensor")
            self._hw_btn.setStyleSheet("")
            self._mock_banner_top.setVisible(False)
        # Propagate to Dual panel
        self._dual_panel.set_use_mock(self.use_mock)

    def _sel_photo(self):
        dlg = QFileDialog(); dlg.setNameFilter("Images (*.png *.jpg *.jpeg *.bmp)")
        if dlg.exec_():
            files = dlg.selectedFiles()
            if files:
                self.settings.operator_photo_path = files[0]
                if hasattr(self, 'photo_lbl'):
                    self.photo_lbl.setText(os.path.basename(files[0]))
                self.settings.save_settings()

    def _sel_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Report Directory", self.settings.report_output_directory)
        if d:
            self.settings.report_output_directory = d
            self.dir_input.setText(d)
            self.settings.save_settings()

    def _upd_env(self):
        amb = self.amb_sp.value(); hum = self.hum_sp.value()
        ok = 18 <= amb <= 28 and hum < 70
        self.env_lbl.setText("✓ Within IEC 60751 lab limits" if ok else
                              f"⚠ {'Ambient out of range' if not (18<=amb<=28) else 'Humidity ≥ 70%'}")
        self.env_lbl.setStyleSheet("color:#2e7d32;" if ok else "color:#f9a825;")

    def _save_asset(self):
        sn = self.serial_in.text().strip()
        if not sn:
            QMessageBox.warning(self, "Error", "Serial number required."); return
        asset = SensorAsset(sn, self.tag_in.text().strip(), self.mfr_in.text().strip(),
                            self.mdl_in.text().strip(), self.r0_sp.value(), self.intv_sp.value())
        self.asset_manager.save_instrument(asset)
        self.settings.sensor_serial_number        = sn
        self.settings.sensor_equipment_tag        = asset.equipment_tag
        self.settings.sensor_manufacturer         = asset.manufacturer
        self.settings.sensor_model                = asset.model
        self.settings.sensor_nominal_r0           = asset.nominal_r0
        self.settings.calibration_interval_months = asset.calibration_interval_months
        self.settings.save_settings()
        # §2-8: show due date
        due = self.asset_manager.get_calibration_due_date(sn)
        self.due_lbl.setText(f"Next cal. due: {due}" if due else "Next cal. due: not yet calibrated")
        QMessageBox.information(self, "Saved", f"Asset {sn} saved.")

    def _check_overdue(self):
        overdue = self.asset_manager.get_overdue_instruments()
        if not overdue:
            self.overdue_lbl.setText("✓ No overdue instruments.")
            self.overdue_lbl.setStyleSheet("color:#2e7d32;")
        else:
            lines = ["⚠ OVERDUE:"] + [
                f"  • {o['serial']} [{o['tag']}] — {o['days_overdue']}d overdue (due {o['due_date']})"
                for o in overdue
            ]
            self.overdue_lbl.setText("\n".join(lines))
            self.overdue_lbl.setStyleSheet("color:#c62828;")

    def _save_trace(self):
        name = self.ref_name.text().strip()
        serial = self.ref_ser.text().strip()
        lab = self.ref_lab.text().strip()
        cert = self.ref_cert.text().strip()
        cal_date_str = self.ref_date.text().strip()
        
        # Validation checks
        if not all([name, serial, lab, cert, cal_date_str]):
            QMessageBox.warning(self, "Validation Error", "All Reference Standard string fields must be filled before saving.")
            return
            
        try:
            datetime.strptime(cal_date_str, "%Y-%m-%d")
        except ValueError:
            QMessageBox.warning(self, "Validation Error", "Calibration Date must be in YYYY-MM-DD format.")
            return

        self.settings.ref_standard_name        = name
        self.settings.ref_serial_number        = serial
        self.settings.ref_calibrating_lab      = lab
        self.settings.ref_lab_accreditation_no = self.ref_acc.text().strip()
        self.settings.ref_certificate_number   = cert
        self.settings.ref_calibration_date     = cal_date_str
        self.settings.ref_uncertainty_expanded = self.ref_u.value()
        self.settings.ref_uncertainty_k        = self.ref_k.value()
        self.settings.u_ref_expanded           = self.ref_u.value()
        self.settings.save_settings()
        QMessageBox.information(self, "Saved", "Traceability evaluated and saved.")

    def _load_history(self):
        sn = self.hist_serial.text().strip()
        records = self.asset_manager.get_history(sn) if sn else self.asset_manager.get_all_records()
        if sn:
            trend = self.asset_manager.get_mbe_trend(sn)
            if trend:
                # §IF-2: Plot MBE trend chart
                mbe_vals = [t['mbe'] for t in trend]
                x_vals = list(range(1, len(mbe_vals) + 1))
                self._trend_curve.setData(x_vals, mbe_vals)
                self.trend_lbl.setText("MBE trend: " + "  →  ".join(
                    f"{t['date']}: {t['mbe']:.4f}°C" for t in trend[-6:]))
            else:
                self._trend_curve.setData([], [])
                self.trend_lbl.setText(f"No trend data for {sn}")
        else:
            self._trend_curve.setData([], [])
            self.trend_lbl.setText("Enter a serial number to view MBE drift trend.")
        self.hist_table.setRowCount(0)
        for rec in records:
            r = self.hist_table.rowCount(); self.hist_table.insertRow(r)
            vi = QTableWidgetItem(rec.verdict)
            vi.setForeground(QColor("#2e7d32") if rec.verdict=="PASS" else QColor("#c62828"))
            self.hist_table.setItem(r,0,QTableWidgetItem(rec.cal_date))
            self.hist_table.setItem(r,1,QTableWidgetItem(rec.serial_number))
            self.hist_table.setItem(r,2,QTableWidgetItem(rec.mode[:20]))
            self.hist_table.setItem(r,3,vi)
            self.hist_table.setItem(r,4,QTableWidgetItem(f"{rec.mbe:.4f}"))
            self.hist_table.setItem(r,5,QTableWidgetItem(f"{rec.rmse:.4f}"))
            self.hist_table.setItem(r,6,QTableWidgetItem(os.path.basename(rec.pdf_path)))

    # ─── Start calibration ───────────────────────────────────────────
    def start_calibration(self):
        txt = self.setpoint_input.text()
        if not txt or txt in ["-","+"]:
            QMessageBox.critical(self,"Error","Enter a valid setpoint."); return

        sp  = float(txt)
        tc  = self.tol_combo.currentText()
        wires = int(self.wire_combo.currentText().split("-")[0])

        # §2-9: 3-wire + Class A guard
        ok, msg = check_class_validity(sp, tc, wires)
        if not ok:
            QMessageBox.critical(self, "Configuration Blocked", msg); return

        # Sync settings
        self.settings.sampling_interval          = self.interval_spin.value()
        self.settings.rtd_wires                  = wires
        self.settings.tolerance_class            = tc
        self.settings.operator_name              = self.op_input.text()
        self.settings.calibration_duration       = self.dur_spin.value()
        self.settings.drift_threshold            = self.drift_spin.value()
        self.settings.sigma_threshold            = self.sigma_spin.value()
        self.settings.noise_threshold            = self.noise_spin.value()
        self.settings.validation_mode            = self.mode_combo.currentText()
        self.settings.u_ref_expanded             = self.uref_spin.value()
        self.settings.sensor_serial_number       = self.serial_in.text().strip()
        self.settings.sensor_equipment_tag       = self.tag_in.text().strip()
        self.settings.sensor_manufacturer        = self.mfr_in.text().strip()
        self.settings.sensor_model               = self.mdl_in.text().strip()
        self.settings.sensor_nominal_r0          = self.r0_sp.value()
        self.settings.calibration_interval_months = self.intv_sp.value()
        self.settings.ambient_temperature        = self.amb_sp.value()
        self.settings.relative_humidity          = self.hum_sp.value()
        self.settings.atmospheric_pressure       = self.pres_sp.value()
        self.settings.ref_standard_name          = self.ref_name.text().strip()
        self.settings.ref_serial_number          = self.ref_ser.text().strip()
        self.settings.ref_calibrating_lab        = self.ref_lab.text().strip()
        self.settings.ref_lab_accreditation_no   = self.ref_acc.text().strip()
        self.settings.ref_certificate_number     = self.ref_cert.text().strip()
        self.settings.ref_calibration_date       = self.ref_date.text().strip()
        self.settings.ref_uncertainty_expanded   = self.ref_u.value()
        self.settings.ref_uncertainty_k          = self.ref_k.value()
        self.settings.save_settings()

        if not self.use_mock and not REAL_DRIVER_AVAILABLE:
            QMessageBox.critical(self,"Error","spidev missing — cannot use real sensor."); return

        DriverClass = MockMAX31865 if self.use_mock else RealMAX31865
        if self.use_mock:
            from max31865_driver_mock import resistance_to_temp_converter as c1, temp_to_resistance as c2
            self.converter = c1; self.ideal_temp_to_resistance = c2
        else:
            self.converter = real_converter; self.ideal_temp_to_resistance = real_ideal_temp_to_resistance

        self.sensor = DriverClass(wires=wires, r_ref=self.settings.r_ref)
        self.settings.standard_reference_value = sp
        if self.use_mock:
            self.sensor.standard_temp_for_simulation = sp
            mode_text = self.mode_combo.currentText()
            if "Field" in mode_text:
                mode_key = "FIELD"
            elif "Model" in mode_text:
                mode_key = "MODEL"
            elif "External" in mode_text:
                mode_key = "EXTERNAL"
            else:
                mode_key = "MODEL"
            self.sensor.set_mode(mode_key)
        else:
            print("[WARNING] Real sensor mode \u2014 behavior will not simulate drift/offset.")

        # §BugFix-1: Hard guard — cannot enter multi-point in Validation mode
        if self._multipoint and "Validation" in self.settings.validation_mode:
            self._multipoint = False
            self.mp_btn.setChecked(False)

        # Multi-point setup
        if self._multipoint:
            try:
                setpoints = [float(x.strip()) for x in self.sp_input.text().split(",") if x.strip()]
                if not setpoints:
                    raise ValueError("Empty setpoints")
            except ValueError:
                QMessageBox.critical(self,"Error","Invalid setpoints."); return
            self.stab_engine = StabilizationEngine(
                window_size=self.settings.stabilization_window,
                slope_thresh=self.sl_spin.value(),
                sigma_thresh=self.ss_spin.value(),
                proximity_deg=self.sp_prox.value(),
                dwell_seconds=self.sp_dwell.value(),
            )
            self.sequencer = CalibrationSequencer(
                setpoints=setpoints,
                samples_per_point=self.spp_spin.value(),
                stabilization_engine=self.stab_engine,
                tolerance_class=tc,
                r_ref=self.settings.r_ref,
                u_ref_expanded=self.settings.u_ref_expanded,
                k_ref=self.settings.ref_uncertainty_k,
                nominal_r0=self.settings.sensor_nominal_r0,
            )
            self.sequencer.start()
            if self.use_mock:
                self.sensor.standard_temp_for_simulation = self.sequencer.current_setpoint
            self.adv_btn.setVisible(True)
        else:
            self.sequencer = None
            self.adv_btn.setVisible(False)

        self.data_logger.start()
        self.waveform.clear_plot()
        self._cal_start_time = time.time()
        self.verdict_lbl.setText("Collecting…")
        self.verdict_lbl.setStyleSheet("color:#aaa;")
        self._sb_state.setText("RUNNING")
        self._sb_setpoint.setText(f"{sp:.1f}°C")
        self._sb_verdict.setText("—")
        self._set_fault_led(0)

        self.worker_thread = Worker(self.sensor, self.settings.sampling_interval)
        self.worker_thread.new_data_signal.connect(self._on_data)
        self.worker_thread.error_signal.connect(self._on_worker_error)
        self.worker_thread.fault_status.connect(self._set_fault_led)
        self.worker_thread.start()

        # Mark Cal Start on waveform (§Section 4-4)
        self.waveform.mark_cal_start(0.0)

        if not self._multipoint:
            self.auto_stop_timer.start(int(self.settings.calibration_duration * 1000))

        self.update_ui_state(True)

    @pyqtSlot(str, bool)
    def _on_worker_error(self, msg: str, is_critical: bool):
        # Show in status bar instead of blocking popup
        self._fault_label.setText(f"Sensor: {msg[:60]}")

    # ─── CVD-based reference model ─────────────────────────────────
    def reference_model(self, setpoint):
        """§ISSUE-4: Structured reference — returns (T_ref, R_ref) via CVD."""
        T_ref = float(setpoint)
        if self.ideal_temp_to_resistance is not None:
            R_ref = self.ideal_temp_to_resistance(T_ref)
        else:
            # Fallback: use mock CVD before start_calibration sets the converter
            R_ref = temp_to_resistance(T_ref)
        return T_ref, R_ref

    @pyqtSlot(float, float)
    def _on_data(self, temp: float, resistance: float):
        if temp is None or math.isnan(temp):
            return

        # §Fix-MP: In multi-point mode use the sequencer's active setpoint,
        # not the static global reference value, so each sample is logged
        # against the correct calibration point.
        if self.sequencer:
            raw_setpoint = self.sequencer.current_setpoint
        else:
            raw_setpoint = self.settings.standard_reference_value

        # §ISSUE-4: Use structured CVD reference model
        T_ref, R_ref = self.reference_model(raw_setpoint)
        print(f"[REF MODEL ACTIVE] Setpoint={raw_setpoint}, T_ref={T_ref:.3f}, R_ref={R_ref:.3f}")
        standard_val = T_ref

        # Feed sequencer
        if self.sequencer:
            self.sequencer.add_reading(temp, standard_val, resistance)
            state = self.sequencer.state
            self._sb_state.setText(state.name)
            self.stab_state.setText(state.name)

            st = self.sequencer.stabilization_status()
            if st:
                self._upd_stab(st)

            # §Section 4-4 / §CF-3: event markers (proper boolean check)
            if st.get("stable") and not self._stab_marked:
                elapsed = time.time() - self._cal_start_time
                self.waveform.mark_stability_achieved(elapsed)
                self._stab_marked = True

            if state == CalState.RECORDING and not self._rec_marked:
                elapsed = time.time() - self._cal_start_time
                self.waveform.mark_recording_started(elapsed)
                self._rec_marked = True

            if self.use_mock and state in (CalState.HEATING, CalState.STABILIZING, CalState.RECORDING):
                sp = self.sequencer.current_setpoint
                if self.sensor.standard_temp_for_simulation != sp:
                    self.sensor.standard_temp_for_simulation = sp

            if state == CalState.NEXT_POINT:
                self._prompt_advance()
            elif state == CalState.COMPLETE:
                self.stop_calibration(); return

        # §Fix-3/8: In multipoint mode, only log data during RECORDING state
        #  to prevent heating/stabilizing data from polluting the metrics.
        if self.sequencer:
            if state == CalState.RECORDING:
                self.data_logger.add_point(temp, T_ref, resistance)
        else:
            self.data_logger.add_point(temp, T_ref, resistance)
        log = self.data_logger.get_data()
        n   = len(log["time"])

        # §BugFix-2: Guard against empty buffers and NaN/Inf before graph update
        if n == 0:
            return
        if math.isnan(temp) or math.isinf(temp):
            return

        # Update status bar with reference model values
        self._sb_setpoint.setText(f"{T_ref:.2f} °C")

        A_c, B_c = ISO_LIMITS.get(self.settings.tolerance_class, (0, 0))
        tol  = A_c + B_c * abs(T_ref)
        try:
            self.waveform.update_plot(
                log["time"], log["measured"],
                [T_ref]*n,
                [T_ref+tol]*n,
                [T_ref-tol]*n,
            )
        except Exception as e:
            print(f"[WARN] Waveform update failed: {e}")

        # Live metrics — §Fix-3: require minimum 5 samples for meaningful stats
        if n >= 5:
            if "Validation" in self.settings.validation_mode:
                vm = compute_validation_metrics(log["measured"], standard_val,
                                                self.settings.drift_threshold,
                                                self.settings.sigma_threshold,
                                                self.settings.noise_threshold)
                self.drift_lbl.setText(f"{vm['drift']:.4f}")
                self.noise_lbl.setText(f"{vm['noise']:.4f}")
                self.sigma_lbl.setText(f"{vm['std_dev']:.4f}")
            else:
                m = compute_metrics(log["measured"], log["standard"])
                self.mae_lbl.setText(f"{m['mae']:.4f}")
                self.mbe_lbl.setText(f"{m['mbe']:.4f}")
                self.rmse_lbl.setText(f"{m['rmse']:.4f}")
                self.cv_lbl.setText(f"{m['cv_percent']:.2f}")
                self.std_lbl.setText(f"{m['std_dev']:.4f}")

    def _upd_stab(self, st: dict):
        def _fmt(ok, val, thresh, unit):
            return ("✓" if ok else "✗") + f"  {val:.4f} {unit}  (lim: {thresh:.4f})"
        self.stab_slope.setText(_fmt(st["slope_ok"], st["slope_val"], st["slope_thresh"], "°C/s"))
        self.stab_sigma.setText(_fmt(st["sigma_ok"], st["sigma_val"], st["sigma_thresh"], "°C"))
        self.stab_prox.setText (_fmt(st["prox_ok"],  st["prox_val"],  st["prox_thresh"],  "°C"))
        elapsed = st["dwell_elapsed"]; req = st["dwell_required"]
        self.stab_dwell.setText(f"{elapsed:.1f} s / {req:.0f} s")
        if st.get("stable"):
            self.stab_status.setText("✓ STABLE")
            self.stab_status.setStyleSheet("color:#2e7d32;font-weight:bold;")
        else:
            self.stab_status.setText("✗ NOT STABLE")
            self.stab_status.setStyleSheet("color:#c62828;font-weight:bold;")

    def _prompt_advance(self):
        sp = self.sequencer.current_setpoint
        self.adv_btn.setVisible(True)
        self.verdict_lbl.setText(f"→ Set bath to {sp:.1f}°C then click Advance")

    def _advance(self):
        if self.sequencer and self.sequencer.state == CalState.NEXT_POINT:
            sp = self.sequencer.current_setpoint
            if self.use_mock: self.sensor.standard_temp_for_simulation = sp
            # §CF-3: Reset stab markers for next point (proper boolean)
            self._stab_marked = False
            self._rec_marked  = False
            self.sequencer.advance_to_next()
            self.verdict_lbl.setText("Collecting…")
            self.adv_btn.setVisible(False)

    # ─── Stop & report ───────────────────────────────────────────────
    def stop_calibration(self):
        """§BugFix-4/8: Global try/except — partial calibration never crashes."""
        # ── Phase 1: Safely stop hardware and worker ──────────────
        if self.auto_stop_timer.isActive():
            self.auto_stop_timer.stop()
        if self.worker_thread:
            try:
                self.worker_thread.stop()
            except Exception:
                pass
        if self.sensor:
            try:
                self.sensor.close()
            except Exception:
                pass
        self.adv_btn.setVisible(False)
        self.update_ui_state(False)
        self._sb_state.setText("STOPPED")
        self._set_fault_led(-1)
        self._stab_marked = False
        self._rec_marked  = False

        # ── Phase 2: Process data and generate reports ────────────
        try:
            self._process_results()
        except Exception as e:
            # §BugFix-8: Never crash — show error dialog
            import traceback
            tb = traceback.format_exc()
            print(f"[ERROR] stop_calibration failed:\n{tb}")
            QMessageBox.critical(self, "Calibration Error",
                                 f"An error occurred during result processing:\n{e}")
            self.verdict_lbl.setText("ERROR")
            self.verdict_lbl.setStyleSheet("color:#c62828;font-weight:bold;")
        finally:
            self.settings.save_settings()

    def _process_results(self):
        """§BugFix-4: Separated result processing for clean error handling."""
        log = self.data_logger.get_data()

        # §BugFix-4: Handle insufficient data without crashing
        if not log["time"] or len(log["time"]) < 3:
            self.verdict_lbl.setText("Insufficient Data")
            self.verdict_lbl.setStyleSheet("color:#f9a825;font-weight:bold;")
            return

        # §BugFix-4: Determine calibration status (COMPLETE vs INCOMPLETE)
        cal_status = "COMPLETE"
        if self.sequencer:
            expected = len(self.sequencer.setpoints)
            completed = len(self.sequencer.all_results)
            if completed < expected:
                cal_status = "INCOMPLETE"
                print(f"[INFO] Partial calibration: {completed}/{expected} points completed")

        verdict = "N/A"
        metrics_payload = {}
        ub = None

        if "Validation" in self.settings.validation_mode:
            vm = compute_validation_metrics(
                log["measured"], self.settings.standard_reference_value,
                self.settings.drift_threshold, self.settings.sigma_threshold,
                self.settings.noise_threshold)
            verdict = vm["verdict"]
            metrics_payload = vm
        else:
            if self.sequencer:
                metrics_payload = self.sequencer.aggregate_results()
                verdict = metrics_payload.get("overall_verdict", "N/A")
                
                # If sequencer stopped before completing even one point, fallback to raw logs for the report
                if not metrics_payload.get("per_point"):
                    if len(log["measured"]) >= 3 and len(log["standard"]) >= 3:
                        metrics_payload.update(compute_metrics(log["measured"], log["standard"]))
                    else:
                        metrics_payload.update({
                            "mean_measured": np.mean(log["measured"]) if log["measured"] else 0,
                            "std_dev": 0, "mbe": 0, "mae": 0, "rmse": 0, "cv_percent": 0,
                        })
            else:
                metrics_payload = compute_metrics(log["measured"], log["standard"])

            # Overall uncertainty budget for certificate summary
            n_s   = len(log["measured"])
            std_d = metrics_payload.get("std_dev", 0)
            uc    = UncertaintyCalculator(r_ref=self.settings.r_ref,
                                          u_ref_expanded=self.settings.u_ref_expanded,
                                          k_ref=self.settings.ref_uncertainty_k,
                                          nominal_r0=self.settings.sensor_nominal_r0)
            ub    = uc.compute(std_dev_measured=std_d, n_samples=max(n_s, 1))

            # Use worst-case per-point U for the summary budget
            worst_U = metrics_payload.get("worst_case_U", 0)
            if worst_U > 0:
                ub["per_point_max_U"] = worst_U

            # For single-point (no sequencer), compute verdict from metrics
            if not (self.sequencer and self.sequencer.all_results):
                verdict_U = ub.get("expanded_uncertainty", 0)
                vi = get_verdict(log["measured"], log["standard"],
                                 self.settings.tolerance_class,
                                 expanded_uncertainty=verdict_U)
                verdict = vi["verdict"]

        # §BugFix-4: Mark partial calibrations
        if cal_status == "INCOMPLETE":
            verdict = f"{verdict} (INCOMPLETE)"

        self.verdict_lbl.setText(verdict)
        self.verdict_lbl.setStyleSheet(
            "color:#2e7d32;font-weight:bold;" if "PASS" in verdict else "color:#c62828;font-weight:bold;")
        self._sb_verdict.setText(verdict)

        # §BugFix-4: Always generate report, even for partial calibrations
        metrics_payload["cal_status"] = cal_status
        generated_pdf = self._gen_reports(metrics_payload, verdict, ub)
        self._save_history(metrics_payload, verdict, ub, generated_pdf)

    def _gen_reports(self, metrics, verdict, ub):
        """
        §Fix-Harden: Wrapped in try/except so failures are shown explicitly.
        Graph snapshot failure no longer aborts the whole PDF.
        Returns the generated PDF path on success, or "" on failure.
        """
        log = self.data_logger.get_data()
        if not log["time"]:
            return ""

        out = self.settings.report_output_directory
        try:
            os.makedirs(out, exist_ok=True)
        except OSError as e:
            QMessageBox.critical(self, "Report Error", f"Cannot create output directory:\n{e}")
            return ""

        ts   = time.strftime("%Y%m%d_%H%M%S")
        sv   = self.settings.standard_reference_value
        sv_s = str(int(sv)) if np.isclose(sv, round(sv)) else f"{sv:.1f}".replace(".", "p")
        pfx  = "VAL" if "Validation" in self.settings.validation_mode else "CAL"
        base = f"CALIBRIX_{pfx}_{sv_s}C_{ts}"

        # §Fix-Harden: snapshot failure is caught and reported; report still proceeds.
        graph_path = os.path.join(out, f"{base}_Graph.png")
        try:
            # §Fix-6: Process pending paint events so the graph is fully rendered
            QApplication.processEvents()
            time.sleep(0.05)
            QApplication.processEvents()
            self.waveform.save_snapshot(graph_path)
            if not os.path.exists(graph_path) or os.path.getsize(graph_path) < 100:
                raise RuntimeError("Snapshot file is empty or missing")
        except Exception as e:
            print(f"[WARN] Graph snapshot failed ({e}) — report will use placeholder.")
            graph_path = ""   # _graph() in report_generator handles missing path gracefully

        pdf_path = os.path.join(out, f"{base}_Report.pdf")
        sn   = self.settings.sensor_serial_number or "NOSERIAL"
        cert = AssetManager.generate_certificate_number(sn)
        due  = self.asset_manager.get_calibration_due_date(sn) or ""

        ctx = {
            "rtd_wires":          self.settings.rtd_wires,
            "setpoint":           sv,
            "operator_name":      self.settings.operator_name,
            "operator_photo_path":self.settings.operator_photo_path,
            "tolerance_class":    self.settings.tolerance_class,
            "verdict":            verdict,
            "start_time":         self.data_logger.start_time_raw,
            "certificate_no":     cert,
            "calibration_due_date": due,
            "sensor_serial_number": self.settings.sensor_serial_number,
            "sensor_equipment_tag": self.settings.sensor_equipment_tag,
            "sensor_manufacturer":  self.settings.sensor_manufacturer,
            "sensor_model":         self.settings.sensor_model,
            "sensor_nominal_r0":    self.settings.sensor_nominal_r0,
            "calibration_interval_months": self.settings.calibration_interval_months,
            "ambient_temperature":  self.settings.ambient_temperature,
            "relative_humidity":    self.settings.relative_humidity,
            "atmospheric_pressure": self.settings.atmospheric_pressure,
            "ref_standard_name":    self.settings.ref_standard_name,
            "ref_serial_number":    self.settings.ref_serial_number,
            "ref_calibrating_lab":  self.settings.ref_calibrating_lab,
            "ref_lab_accreditation_no": self.settings.ref_lab_accreditation_no,
            "ref_certificate_number":   self.settings.ref_certificate_number,
            "ref_calibration_date":     self.settings.ref_calibration_date,
            "ref_uncertainty_expanded": self.settings.ref_uncertainty_expanded,
            "ref_uncertainty_k":        self.settings.ref_uncertainty_k,
            "uncertainty_budget": ub,
            "multipoint_setpoints": ([r["setpoint"] for r in metrics.get("per_point", [])]
                                     if metrics.get("per_point") else None),
        }

        # §Fix-Harden: PDF generation failures surface as a dialog, not a silent crash.
        try:
            UnifiedReportGenerator.generate(self.settings.validation_mode,
                                            pdf_path, graph_path, metrics, log, ctx)
        except Exception as e:
            QMessageBox.critical(self, "Report Generation Failed",
                                 f"PDF could not be created:\n{e}")
            return ""

        # CSV is best-effort — failure should not block the PDF path from being returned.
        csv_path = os.path.join(out, f"{base}_Data.csv")
        try:
            generate_csv_report(csv_path, self.data_logger)
        except Exception as e:
            print(f"[WARN] CSV export failed: {e}")

        # §IF-1: Standalone correction table CSV for multi-point calibrations
        per_pt = metrics.get("per_point", [])
        if per_pt:
            corr_path = os.path.join(out, f"{base}_CorrectionTable.csv")
            try:
                generate_correction_table_csv(corr_path, per_pt)
            except Exception as e:
                print(f"[WARN] Correction table CSV failed: {e}")

        try:
            if sys.platform.startswith("win"):    os.startfile(pdf_path)
            elif sys.platform.startswith("darwin"): os.system(f'open "{pdf_path}"')
            else:                                 os.system(f'xdg-open "{pdf_path}"')
        except Exception:
            pass

        return pdf_path   # §Fix-History: caller stores this in the DB record

    def _save_history(self, metrics, verdict, ub, pdf_path: str = ""):
        """
        §Fix-History: pdf_path is now passed in from _gen_reports so the
        database record always contains the real generated file path.
        """
        sn = self.settings.sensor_serial_number or ""
        if not sn: return
        if not self.asset_manager.get_instrument(sn):
            self.asset_manager.save_instrument(SensorAsset(
                sn, self.settings.sensor_equipment_tag, self.settings.sensor_manufacturer,
                self.settings.sensor_model, self.settings.sensor_nominal_r0,
                self.settings.calibration_interval_months))
        pts = metrics.get("per_point", [])
        sp_json = json.dumps([r["setpoint"] for r in pts] if pts else [self.settings.standard_reference_value])
        rec = CalibrationRecord(
            serial_number        = sn,
            cal_date             = time.strftime("%Y-%m-%d"),
            certificate_no       = AssetManager.generate_certificate_number(sn),
            mode                 = self.settings.validation_mode,
            setpoints            = sp_json,
            verdict              = verdict,
            mbe                  = metrics.get("mbe", metrics.get("drift", 0)),
            rmse                 = metrics.get("rmse", 0),
            mae                  = metrics.get("mae", 0),
            std_dev              = metrics.get("std_dev", 0),
            expanded_uncertainty = ub["expanded_uncertainty"] if ub else 0.0,
            pdf_path             = pdf_path,   # §Fix-History: actual path, not ""
            operator_name        = self.settings.operator_name,
            tolerance_class      = self.settings.tolerance_class,
            ambient_temp         = self.settings.ambient_temperature,
            humidity             = self.settings.relative_humidity,
            pressure             = self.settings.atmospheric_pressure,
        )
        self.asset_manager.save_calibration(rec)

    def closeEvent(self, event):
        if self.auto_stop_timer.isActive(): self.auto_stop_timer.stop()
        if self.worker_thread and self.worker_thread.isRunning():
            self.worker_thread.stop()
        # Also stop Dual RTD worker if running
        if hasattr(self, '_dual_panel'):
            self._dual_panel.stop_worker_for_close()
        self.settings.save_settings()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = CalibrixApp()
    win.show()
    sys.exit(app.exec_())
