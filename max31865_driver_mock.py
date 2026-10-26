# max31865_driver_mock.py — CALIBRIX v2
# First-order thermal lag + Gaussian noise simulation
# Mock fault injection support for testing
import random
import math
import time

RTD_NOMINAL = 100.0
A =  3.90830e-3
B = -5.77500e-7
C = -4.18300e-12


def temp_to_resistance(temp: float) -> float:
    t = float(temp)
    if t >= 0:
        return RTD_NOMINAL * (1 + A * t + B * t**2)
    else:
        return RTD_NOMINAL * (1 + A * t + B * t**2 + C * (t - 100) * t**3)


def resistance_to_temp_converter(resistance: float) -> float:
    r_ratio = float(resistance) / RTD_NOMINAL
    if r_ratio >= 1.0:
        delta = A**2 - 4 * B * (1 - r_ratio)
        if delta < 0:
            return float("nan")
        return (-A + math.sqrt(delta)) / (2 * B)
    else:
        r    = resistance
        temp = (-242.02 + 2.2228 * r + 2.5859e-3 * r**2
                - 4.8260e-6 * r**3 - 2.8183e-8 * r**4 + 1.5243e-10 * r**5)
        if temp > 0:
            temp = (-A + math.sqrt(A**2 - 4 * B * (1 - r_ratio))) / (2 * B)
        return temp


# Fault severity constants (mirrors real driver)
FAULT_NONE    = 0
FAULT_WARNING = 1
FAULT_CRITICAL = 2


class MAX31865:
    """
    Mock MAX31865 driver with mode-aware simulation profiles.
    Supports three calibration modes with distinct behaviour:
      • Field Validation  — noisy + slow drift (σ=0.1°C, +0.01°C/s)
      • Lab Calibration    — smooth exponential convergence (τ=3s, σ=0.02°C)
      • External Reference — stable + constant 0.3°C bias offset
    """
    TAU     = 3.0    # thermal time constant (s) — fast convergence for cal bath model
    NOISE   = 0.02   # default Gaussian noise std-dev (°C)

    # Mode-specific parameters
    FIELD_NOISE   = 0.10   # σ for Field Validation
    FIELD_DRIFT   = 0.01   # °C per second drift
    EXT_OFFSET    = 0.30   # constant UUT-vs-reference bias (°C)
    EXT_NOISE     = 0.03   # σ for External Reference

    def __init__(self, bus=0, device=0, wires=4, r_ref=430.0):
        print("--- Using MOCK MAX31865 (thermal-lag model active) ---")
        self.standard_temp_for_simulation = 100.0
        self._current_temp = 25.0
        self._last_tick    = time.time()
        self._start_time   = time.time()
        self._mode         = ""
        # Fault injection: set to FAULT_WARNING or FAULT_CRITICAL to test
        self._injected_fault = FAULT_NONE
        self._fault_message  = ""

    def set_mode(self, mode: str):
        """Set simulation mode — called by main.py at calibration start."""
        self._mode = mode
        self._start_time = time.time()
        print(f"[MOCK] Simulation mode set: {mode}")

    def configure(self, _wires):
        pass

    def inject_fault(self, severity: int, message: str = "Simulated fault"):
        """Call from tests/UI to simulate a hardware fault."""
        self._injected_fault = severity
        self._fault_message  = message

    def clear_faults(self):
        self._injected_fault = FAULT_NONE
        self._fault_message  = ""

    def read_temp(self):
        """Simulate reading — behaviour depends on active mode."""
        if self._injected_fault == FAULT_CRITICAL:
            raise RuntimeError(f"CRITICAL SENSOR FAULT: {self._fault_message}")
        if self._injected_fault == FAULT_WARNING:
            print(f"[MOCK WARNING] {self._fault_message}")

        now = time.time()
        dt  = max(0.001, now - self._last_tick)
        self._last_tick = now

        target = self.standard_temp_for_simulation
        self._current_temp += (target - self._current_temp) * (1 - math.exp(-dt / self.TAU))

        if self._mode == "FIELD":
            # ── Field Validation: noisy sensor + slow upward drift ────
            elapsed = now - self._start_time
            noise = random.gauss(0.0, self.FIELD_NOISE)
            drift = self.FIELD_DRIFT * elapsed
            noisy = self._current_temp + noise + drift

        elif self._mode == "EXTERNAL":
            # ── External Reference: stable + constant bias offset ─────
            noise = random.gauss(0.0, self.EXT_NOISE)
            noisy = self._current_temp + self.EXT_OFFSET + noise

        else:
            # ── Model-Based Calibration (default): smooth convergence ─
            noisy = self._current_temp + random.gauss(0.0, self.NOISE)

        return noisy, temp_to_resistance(noisy)

    def close(self):
        pass

