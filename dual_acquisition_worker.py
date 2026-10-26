# dual_acquisition_worker.py — CALIBRIX v2
#
# Single coordinated acquisition worker for Dual RTD mode.
# Reads Reference + DUT in one paired cycle.
# Emits dual_data_signal(DualSample) — both channels share one timestamp/index.
# Uses threading.Event for clean stop (same pattern as single-RTD Worker).
# Real hardware NEVER silently falls back to mock.

import threading
import math
from PyQt5.QtCore import QThread, pyqtSignal

from dual_rtd_mock import DualSample, FAULT_CRITICAL, FAULT_WARNING


class DualWorker(QThread):
    """
    Coordinated Dual RTD acquisition thread.

    Signals
    -------
    dual_data_signal(DualSample) — emitted each acquisition cycle
    error_signal(str, bool)      — (message, is_critical)
    ref_fault_status(int)        — 0=OK, 1=warning, 2=critical
    dut_fault_status(int)        — 0=OK, 1=warning, 2=critical
    """
    dual_data_signal = pyqtSignal(object)    # DualSample
    error_signal     = pyqtSignal(str, bool) # (msg, is_critical)
    ref_fault_status = pyqtSignal(int)       # 0/1/2
    dut_fault_status = pyqtSignal(int)       # 0/1/2

    def __init__(self, source, interval: float):
        """
        Parameters
        ----------
        source  : DualRTDMockSource | DualRTDRealSource
            The paired sensor source. Must have read_pair() method.
        interval : float
            Acquisition interval in seconds.
        """
        super().__init__()
        self.source       = source
        self.interval     = float(interval)
        self._stop_event  = threading.Event()
        self._cal_state   = "IDLE"
        self._paused      = False

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def set_calibration_state(self, state: str):
        """Update the state label injected into each DualSample."""
        self._cal_state = state

    def run(self):
        while not self._stop_event.is_set():
            if self._paused:
                self._stop_event.wait(self.interval)
                continue

            try:
                sample = self.source.read_pair(calibration_state=self._cal_state)

                # ── Per-channel validity / NaN guard ──────────────
                if not sample.ref_valid or not math.isfinite(sample.ref_temperature):
                    sample.ref_valid = False
                    self.ref_fault_status.emit(2)
                elif sample.ref_fault:
                    self.ref_fault_status.emit(1)
                else:
                    self.ref_fault_status.emit(0)

                if not sample.dut_valid or not math.isfinite(sample.dut_temperature):
                    sample.dut_valid = False
                    self.dut_fault_status.emit(2)
                elif sample.dut_fault:
                    self.dut_fault_status.emit(1)
                else:
                    self.dut_fault_status.emit(0)

                # ── Recompute delta from validated values ──────────
                if sample.ref_valid and sample.dut_valid:
                    sample.delta_temperature = sample.dut_temperature - sample.ref_temperature
                else:
                    sample.delta_temperature = float("nan")

                self.dual_data_signal.emit(sample)

            except RuntimeError as e:
                # Critical hardware fault — report but continue loop
                msg = str(e)
                is_critical = "CRITICAL" in msg.upper() or "FAULT" in msg.upper()
                self.error_signal.emit(msg, is_critical)
                if "REFERENCE" in msg.upper():
                    self.ref_fault_status.emit(2)
                if "DUT" in msg.upper():
                    self.dut_fault_status.emit(2)
                # Do not stop — caller decides to abort
            except Exception as e:
                self.error_signal.emit(str(e), False)

            self._stop_event.wait(self.interval)

    def stop(self):
        """Signal stop and wait up to 3 s for thread to exit."""
        self._stop_event.set()
        self.wait(3000)
