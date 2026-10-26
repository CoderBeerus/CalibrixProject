# data_logger.py — CALIBRIX v2
# §1-3: Uses threading.Event for clean shutdown
# §1-4: NaN preserved for 0°C setpoint; never silently converted to 0
import time
import threading
import math


class DataLogger:
    """Thread-safe timestamped data store for one calibration session."""

    def __init__(self):
        self._lock       = threading.Lock()
        self.start_time  = None
        self.start_time_raw = None
        self.reset()

    def start(self):
        self.reset()
        self.start_time_raw = self.start_time = time.time()

    def stop(self):
        """Signal that no more points should be accepted."""
        with self._lock:
            self.start_time = None

    def add_point(self, measured_temp: float, standard_temp: float, resistance: float):
        """§1-4: error_percent = NaN when standard_temp == 0 (not 0)."""
        with self._lock:
            if self.start_time is None:
                return
            elapsed = time.time() - self.start_time

            if math.isnan(measured_temp) or math.isnan(resistance) or math.isinf(measured_temp):
                return  # §1-4: skip corrupt readings entirely
            if abs(measured_temp) > 1000 or abs(resistance) > 10000:
                return
            if elapsed < 0 or elapsed > 1000000:
                return

            if standard_temp != 0:
                error_pct = abs(measured_temp - standard_temp) / abs(standard_temp) * 100.0
            else:
                error_pct = float("nan")   # §1-4: defined behaviour at 0°C setpoint

            self.timestamps.append(elapsed)
            self.measured_values.append(float(measured_temp))
            self.standard_values.append(float(standard_temp))
            self.errors.append(error_pct)
            self.resistance_values.append(float(resistance))

    def get_data(self) -> dict:
        with self._lock:
            return {
                "time":       list(self.timestamps),
                "measured":   list(self.measured_values),
                "standard":   list(self.standard_values),
                "error":      list(self.errors),
                "resistance": list(self.resistance_values),
            }

    def reset(self):
        with self._lock:
            self.timestamps        = []
            self.measured_values   = []
            self.standard_values   = []
            self.errors            = []
            self.resistance_values = []
