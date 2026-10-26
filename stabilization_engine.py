# stabilization_engine.py — CALIBRIX v2 (Bug-fixed & hardened)
# Fixes: §1-1 slope_thresh naming, §1-2 div-by-zero / NaN guards
import collections
import time
import math


class StabilizationEngine:
    """
    Rolling-window four-condition thermal stabilization checker.

    Conditions (all must be True simultaneously for dwell timer to run):
      1. |slope| < slope_thresh   — °C/s
      2. sigma   < sigma_thresh   — °C
      3. |mean − setpoint| < proximity_deg — °C
      4. All three held for >= dwell_seconds → STABLE
    """

    def __init__(self,
                 window_size:   int   = 20,
                 slope_thresh:  float = 0.01,
                 sigma_thresh:  float = 0.05,
                 proximity_deg: float = 0.5,
                 dwell_seconds: float = 30.0):

        self.window_size   = max(2, int(window_size))
        self.slope_thresh  = float(slope_thresh)   # §1-1: only self.slope_thresh used
        self.sigma_thresh  = float(sigma_thresh)
        self.proximity_deg = float(proximity_deg)
        self.dwell_seconds = float(dwell_seconds)

        self._temps = collections.deque(maxlen=self.window_size)
        self._times = collections.deque(maxlen=self.window_size)

        self._dwell_start = None
        self._stable      = False
        self.setpoint     = 0.0

    def reset(self, setpoint: float = 0.0):
        self._temps.clear()
        self._times.clear()
        self._dwell_start = None
        self._stable      = False
        self.setpoint     = float(setpoint)

    def add_sample(self, temperature: float, timestamp=None):
        if math.isnan(temperature):
            return
        if timestamp is None:
            timestamp = time.time()
        self._temps.append(float(temperature))
        self._times.append(float(timestamp))
        self._update_stable_state()

    @property
    def is_stable(self):
        return self._stable

    def status(self):
        """§1-2: Always returns safe numeric dict — never raises, never NaN."""
        n = len(self._temps)
        empty = {
            "stable": False,
            "slope_ok": False,  "slope_val": 0.0,     "slope_thresh":   self.slope_thresh,
            "sigma_ok": False,  "sigma_val": 0.0,     "sigma_thresh":   self.sigma_thresh,
            "prox_ok":  False,  "prox_val":  0.0,     "prox_thresh":    self.proximity_deg,
            "dwell_ok": False,  "dwell_elapsed": 0.0, "dwell_required": self.dwell_seconds,
        }
        if n < 2:
            return empty

        try:
            slope_val, sigma_val, prox_val = self._compute_conditions()
        except Exception:
            return empty

        # §1-2: sanitise — replace any NaN/inf with safe defaults
        slope_val = float(slope_val) if math.isfinite(slope_val) else 0.0
        sigma_val = float(sigma_val) if math.isfinite(sigma_val) else 0.0
        prox_val  = float(prox_val)  if math.isfinite(prox_val)  else float("inf")

        slope_ok  = abs(slope_val) < self.slope_thresh   # §1-1
        sigma_ok  = sigma_val      < self.sigma_thresh
        prox_ok   = abs(prox_val)  < self.proximity_deg
        all_three = slope_ok and sigma_ok and prox_ok

        now = time.time()
        dwell_elapsed = 0.0
        if all_three and self._dwell_start is not None:
            dwell_elapsed = max(0.0, now - self._dwell_start)
        dwell_ok = dwell_elapsed >= self.dwell_seconds

        return {
            "stable":        self._stable,
            "slope_ok":      slope_ok,  "slope_val":     abs(slope_val), "slope_thresh":   self.slope_thresh,
            "sigma_ok":      sigma_ok,  "sigma_val":     sigma_val,      "sigma_thresh":   self.sigma_thresh,
            "prox_ok":       prox_ok,   "prox_val":      abs(prox_val),  "prox_thresh":    self.proximity_deg,
            "dwell_ok":      dwell_ok,  "dwell_elapsed": dwell_elapsed,  "dwell_required": self.dwell_seconds,
        }

    def _compute_conditions(self):
        temps  = list(self._temps)
        times  = list(self._times)
        n      = len(temps)
        t_mean = sum(times) / n
        T_mean = sum(temps) / n

        num = sum((times[i] - t_mean) * (temps[i] - T_mean) for i in range(n))
        den = sum((times[i] - t_mean) ** 2 for i in range(n))
        slope = (num / den) if den > 1e-12 else 0.0  # §1-2 div-by-zero

        sigma = 0.0
        if n > 1:
            sigma = math.sqrt(max(0.0, sum((t - T_mean)**2 for t in temps) / (n - 1)))

        return slope, sigma, (T_mean - self.setpoint)

    def _update_stable_state(self):
        if len(self._temps) < 2:
            return
        try:
            slope_val, sigma_val, prox_val = self._compute_conditions()
        except Exception:
            self._stable = False
            return

        slope_ok  = math.isfinite(slope_val) and abs(slope_val) < self.slope_thresh  # §1-1
        sigma_ok  = math.isfinite(sigma_val) and sigma_val      < self.sigma_thresh
        prox_ok   = math.isfinite(prox_val)  and abs(prox_val)  < self.proximity_deg
        all_three = slope_ok and sigma_ok and prox_ok

        now = time.time()
        if all_three:
            if self._dwell_start is None:
                self._dwell_start = now
            self._stable = (now - self._dwell_start) >= self.dwell_seconds
        else:
            self._dwell_start = None
            self._stable      = False
