# dual_rtd_mock.py — CALIBRIX v2 Dual RTD Mock Source
#
# Simulates two correlated MAX31865 channels:
#   Channel 0 = REFERENCE  : smooth exponential convergence, low noise
#   Channel 1 = DUT        : follows Reference + configurable offset/lag/noise/drift
#
# DualSample is the single shared record for paired measurements.
# Fault injection supported for testing.
# Real Hardware mode NEVER uses this module.

import math
import random
import time
from dataclasses import dataclass, field
from typing import Optional

RTD_NOMINAL = 100.0
_A =  3.90830e-3
_B = -5.77500e-7
_C = -4.18300e-12


def _cvd_temp_to_resistance(temp: float, r0: float = RTD_NOMINAL) -> float:
    t = float(temp)
    if t >= 0:
        return r0 * (1 + _A * t + _B * t**2)
    return r0 * (1 + _A * t + _B * t**2 + _C * (t - 100) * t**3)


@dataclass
class DualSample:
    """Single paired measurement record — Reference + DUT."""
    timestamp:          float = 0.0
    sample_index:       int   = 0
    setpoint:           float = 0.0

    ref_temperature:    float = 0.0
    ref_resistance:     float = 0.0
    ref_valid:          bool  = True
    ref_fault:          bool  = False
    ref_fault_msg:      str   = ""
    ref_stable:         bool  = False

    dut_temperature:    float = 0.0
    dut_resistance:     float = 0.0
    dut_valid:          bool  = True
    dut_fault:          bool  = False
    dut_fault_msg:      str   = ""
    dut_stable:         bool  = False

    delta_temperature:  float = 0.0   # DUT - Reference (signed)
    calibration_state:  str   = "IDLE"


# ── Fault severity ────────────────────────────────────────────────────
FAULT_NONE     = 0
FAULT_WARNING  = 1
FAULT_CRITICAL = 2


class DualRTDMockSource:
    """
    Mock Dual RTD source — two correlated but independent channels.

    Reference channel:
        τ_ref     — thermal time constant (s), default 5s
        σ_ref     — measurement noise std-dev (°C), default 0.01

    DUT channel:
        follows Reference with:
        dut_offset   — fixed bias (°C), default +0.12 → produces PASS with ±0.50 tol
        dut_noise    — noise std-dev (°C), default 0.03
        dut_tau_lag  — additional thermal lag (s), default 1.0
        dut_drift    — slow upward drift (°C/s), default 0.0

    Fault injection:
        inject_ref_fault(severity, message)
        inject_dut_fault(severity, message)
        clear_faults()
    """

    TAU_REF      = 5.0
    SIGMA_REF    = 0.01
    TAU_DUT_LAG  = 1.0
    SIGMA_DUT    = 0.03

    def __init__(self,
                 r0_ref: float = 100.0,
                 r0_dut: float = 100.0,
                 initial_temp: float = 25.0,
                 dut_offset: float = 0.12,
                 dut_noise: float = 0.03,
                 dut_lag: float = 1.0,
                 dut_drift: float = 0.0):

        self.r0_ref      = r0_ref
        self.r0_dut      = r0_dut
        self.dut_offset  = dut_offset
        self.dut_noise   = dut_noise
        self.dut_lag     = max(0.0, float(dut_lag))
        self.dut_drift   = float(dut_drift)

        # Thermal state
        self._ref_temp   = float(initial_temp)
        self._dut_temp   = float(initial_temp)
        self._last_tick  = time.time()
        self._start_time = time.time()
        self._setpoint   = float(initial_temp)

        # Fault injection state
        self._ref_fault_sev = FAULT_NONE
        self._ref_fault_msg = ""
        self._dut_fault_sev = FAULT_NONE
        self._dut_fault_msg = ""

        self._sample_index = 0

    @property
    def setpoint(self) -> float:
        return self._setpoint

    @setpoint.setter
    def setpoint(self, value: float):
        self._setpoint = float(value)

    def inject_ref_fault(self, severity: int, message: str = "Simulated reference fault"):
        self._ref_fault_sev = severity
        self._ref_fault_msg = message

    def inject_dut_fault(self, severity: int, message: str = "Simulated DUT fault"):
        self._dut_fault_sev = severity
        self._dut_fault_msg = message

    def clear_faults(self):
        self._ref_fault_sev = FAULT_NONE
        self._ref_fault_msg = ""
        self._dut_fault_sev = FAULT_NONE
        self._dut_fault_msg = ""

    def read_pair(self, calibration_state: str = "IDLE") -> DualSample:
        """
        Read one paired DualSample. Both channels share a timestamp and sample index.
        Raises RuntimeError for CRITICAL faults (mimicking real driver).
        """
        now = time.time()
        dt  = max(0.001, now - self._last_tick)
        self._last_tick = now
        elapsed = now - self._start_time
        self._sample_index += 1

        # ── Reference channel ─────────────────────────────────────
        ref_valid = True
        ref_fault = False
        ref_fault_msg = ""

        if self._ref_fault_sev == FAULT_CRITICAL:
            raise RuntimeError(f"REFERENCE CRITICAL FAULT: {self._ref_fault_msg}")
        if self._ref_fault_sev == FAULT_WARNING:
            ref_fault = True
            ref_fault_msg = self._ref_fault_msg

        # Exponential thermal response toward setpoint
        tau_ref = self.TAU_REF
        self._ref_temp += (self._setpoint - self._ref_temp) * (1 - math.exp(-dt / tau_ref))
        ref_noise = random.gauss(0.0, self.SIGMA_REF)
        ref_meas  = self._ref_temp + ref_noise
        ref_res   = _cvd_temp_to_resistance(ref_meas, self.r0_ref)

        # ── DUT channel ───────────────────────────────────────────
        dut_valid = True
        dut_fault = False
        dut_fault_msg = ""

        if self._dut_fault_sev == FAULT_CRITICAL:
            raise RuntimeError(f"DUT CRITICAL FAULT: {self._dut_fault_msg}")
        if self._dut_fault_sev == FAULT_WARNING:
            dut_fault = True
            dut_fault_msg = self._dut_fault_msg

        # DUT follows reference with additional lag
        tau_dut = self.TAU_REF + self.dut_lag
        self._dut_temp += (self._setpoint - self._dut_temp) * (1 - math.exp(-dt / tau_dut))
        dut_noise = random.gauss(0.0, self.dut_noise)
        dut_drift = self.dut_drift * elapsed
        dut_meas  = self._dut_temp + self.dut_offset + dut_noise + dut_drift
        dut_res   = _cvd_temp_to_resistance(dut_meas, self.r0_dut)

        delta = dut_meas - ref_meas

        return DualSample(
            timestamp          = now,
            sample_index       = self._sample_index,
            setpoint           = self._setpoint,
            ref_temperature    = ref_meas,
            ref_resistance     = ref_res,
            ref_valid          = ref_valid,
            ref_fault          = ref_fault,
            ref_fault_msg      = ref_fault_msg,
            ref_stable         = False,   # filled by caller / stabilization engine
            dut_temperature    = dut_meas,
            dut_resistance     = dut_res,
            dut_valid          = dut_valid,
            dut_fault          = dut_fault,
            dut_fault_msg      = dut_fault_msg,
            dut_stable         = False,
            delta_temperature  = delta,
            calibration_state  = calibration_state,
        )

    def close(self):
        pass


# ── Demonstration mode preset ─────────────────────────────────────────

DEMO_PASS_OFFSET = 0.12   # °C — PASS with ±0.50 tolerance
DEMO_FAIL_OFFSET = 0.65   # °C — FAIL with ±0.50 tolerance
DEMO_NOISE       = 0.025  # °C
DEMO_LAG         = 0.8    # s

# ── Demo default setpoints ────────────────────────────────────────────
DEMO_SETPOINTS    = [25.0, 50.0, 75.0]
DEMO_SAMPLES_PT   = 10         # samples/point (fast demo)
DEMO_STAB_DWELL   = 8.0        # seconds (non-zero, but short)
DEMO_STAB_SLOPE   = 0.05       # °C/s (relaxed for fast bath)
DEMO_STAB_SIGMA   = 0.08       # °C
DEMO_STAB_PROX    = 2.0        # °C proximity to setpoint

# Full calibration defaults are untouched — only demo mode reads these.
