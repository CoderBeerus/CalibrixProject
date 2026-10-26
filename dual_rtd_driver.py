# dual_rtd_driver.py — CALIBRIX v2 Real Dual RTD Hardware Source
#
# Wraps two MAX31865 instances for Reference and DUT.
# Each channel uses a separate SPI device (CE0/CE1) or optional GPIO CS.
#
# Target hardware:
#   Raspberry Pi 4
#   SPI bus 0, CE0 → Reference MAX31865
#   SPI bus 0, CE1 → DUT MAX31865
#
# Enable in /boot/config.txt:
#   dtparam=spi=on
#   dtoverlay=spi0-2cs        (to enable CE0 + CE1 simultaneously)
#
# Wiring assumptions:
#   MOSI/MISO/SCLK shared on SPI0 (GPIO 10, 9, 11)
#   CE0 = GPIO 8  → Reference
#   CE1 = GPIO 7  → DUT
#
# This module MUST NOT be imported on non-Linux or when spidev is unavailable.
# main.py / dual_ui.py guard this import behind REAL_DRIVER_AVAILABLE.

from dual_rtd_mock import DualSample   # shared dataclass only
import time

# ── Try importing the real MAX31865 driver ────────────────────────────
try:
    from max31865_driver import MAX31865 as _MAX31865, SensorFault
    _REAL_AVAILABLE = True
except ImportError:
    _REAL_AVAILABLE = False
    class SensorFault(Exception):
        def __init__(self, *a, **kw):
            self.is_critical = True
            super().__init__(*a)


class DualRTDRealSource:
    """
    Real Dual RTD source using two MAX31865 ICs on separate SPI CE lines.

    Parameters
    ----------
    ref_bus, ref_device : int
        SPI bus/device for Reference channel (default 0, 0 → /dev/spidev0.0)
    dut_bus, dut_device : int
        SPI bus/device for DUT channel (default 0, 1 → /dev/spidev0.1)
    wires : int
        RTD wiring (2, 3, or 4). Same for both channels unless overridden.
    r_ref : float
        Reference resistor value (Ω) on MAX31865 board (430 Ω typical).
    """

    def __init__(self,
                 ref_bus: int = 0, ref_device: int = 0,
                 dut_bus: int = 0, dut_device: int = 1,
                 wires: int = 4,
                 r_ref: float = 430.0):

        if not _REAL_AVAILABLE:
            raise ImportError(
                "Real MAX31865 driver (max31865_driver.py + spidev) not available. "
                "Cannot use real hardware Dual RTD mode.")

        self._ref_sensor = _MAX31865(bus=ref_bus, device=ref_device,
                                     wires=wires, r_ref=r_ref)
        self._dut_sensor = _MAX31865(bus=dut_bus, device=dut_device,
                                     wires=wires, r_ref=r_ref)
        self._sample_index = 0
        self._setpoint = 0.0

        print(f"[DUAL REAL] Reference → spidev{ref_bus}.{ref_device}, "
              f"DUT → spidev{dut_bus}.{dut_device}, wires={wires}")

    @property
    def setpoint(self) -> float:
        return self._setpoint

    @setpoint.setter
    def setpoint(self, value: float):
        self._setpoint = float(value)

    def read_pair(self, calibration_state: str = "IDLE") -> DualSample:
        """
        Read one paired DualSample from real hardware.
        Fault is reported per-channel; critical fault raises immediately.
        Non-critical fault sets fault flags in DualSample but continues.
        """
        now = time.time()
        self._sample_index += 1

        # ── Reference channel ─────────────────────────────────────
        ref_temp = ref_res = 0.0
        ref_valid = True
        ref_fault = False
        ref_fault_msg = ""
        try:
            ref_temp, ref_res = self._ref_sensor.read_temp()
            self._ref_sensor.clear_faults()
        except SensorFault as sf:
            ref_fault = True
            ref_fault_msg = str(sf)
            ref_valid = not sf.is_critical
            if sf.is_critical:
                # Propagate — let worker handle
                raise
        except Exception as e:
            ref_fault = True
            ref_fault_msg = str(e)
            ref_valid = False

        # ── DUT channel ───────────────────────────────────────────
        dut_temp = dut_res = 0.0
        dut_valid = True
        dut_fault = False
        dut_fault_msg = ""
        try:
            dut_temp, dut_res = self._dut_sensor.read_temp()
            self._dut_sensor.clear_faults()
        except SensorFault as sf:
            dut_fault = True
            dut_fault_msg = str(sf)
            dut_valid = not sf.is_critical
            if sf.is_critical:
                raise
        except Exception as e:
            dut_fault = True
            dut_fault_msg = str(e)
            dut_valid = False

        import math
        # Guard NaN/Inf
        if ref_valid and (not math.isfinite(ref_temp) or not math.isfinite(ref_res)):
            ref_valid = False
            ref_fault_msg = "Non-finite reference reading"
        if dut_valid and (not math.isfinite(dut_temp) or not math.isfinite(dut_res)):
            dut_valid = False
            dut_fault_msg = "Non-finite DUT reading"

        delta = (dut_temp - ref_temp) if (ref_valid and dut_valid) else float("nan")

        return DualSample(
            timestamp         = now,
            sample_index      = self._sample_index,
            setpoint          = self._setpoint,
            ref_temperature   = ref_temp,
            ref_resistance    = ref_res,
            ref_valid         = ref_valid,
            ref_fault         = ref_fault,
            ref_fault_msg     = ref_fault_msg,
            dut_temperature   = dut_temp,
            dut_resistance    = dut_res,
            dut_valid         = dut_valid,
            dut_fault         = dut_fault,
            dut_fault_msg     = dut_fault_msg,
            delta_temperature = delta,
            calibration_state = calibration_state,
        )

    def clear_faults(self):
        try:
            self._ref_sensor.clear_faults()
        except Exception:
            pass
        try:
            self._dut_sensor.clear_faults()
        except Exception:
            pass

    def close(self):
        try:
            self._ref_sensor.close()
        except Exception:
            pass
        try:
            self._dut_sensor.close()
        except Exception:
            pass
