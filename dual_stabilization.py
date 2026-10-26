# dual_stabilization.py — CALIBRIX v2
#
# Dual-channel stabilization: wraps two StabilizationEngine instances
# (Reference and DUT) and adds a differential stability check.
#
# STABLE only when ALL of:
#   1. Reference channel is stable (slope, sigma, proximity, dwell)
#   2. DUT channel is stable (slope, sigma, proximity, dwell)
#   3. Delta_T stability: std(Delta_T buffer) < delta_sigma_thresh

import collections
import math
from stabilization_engine import StabilizationEngine


class DualStabilizationEngine:
    """
    Dual-channel stability checker for Dual RTD calibration.

    Parameters are shared between Reference and DUT engines.
    Delta_T stability is checked separately over a rolling buffer.
    """

    def __init__(self,
                 window_size:     int   = 20,
                 slope_thresh:    float = 0.01,
                 sigma_thresh:    float = 0.05,
                 proximity_deg:   float = 0.5,
                 dwell_seconds:   float = 30.0,
                 delta_sigma_thresh: float = 0.05):

        self._ref_engine = StabilizationEngine(
            window_size=window_size,
            slope_thresh=slope_thresh,
            sigma_thresh=sigma_thresh,
            proximity_deg=proximity_deg,
            dwell_seconds=dwell_seconds,
        )
        self._dut_engine = StabilizationEngine(
            window_size=window_size,
            slope_thresh=slope_thresh,
            sigma_thresh=sigma_thresh,
            proximity_deg=proximity_deg,
            dwell_seconds=dwell_seconds,
        )
        self._delta_buf = collections.deque(maxlen=window_size)
        self._delta_sigma_thresh = float(delta_sigma_thresh)

    def reset(self, setpoint: float = 0.0):
        self._ref_engine.reset(setpoint)
        self._dut_engine.reset(setpoint)
        self._delta_buf.clear()

    def add_sample(self, ref_temp: float, dut_temp: float, timestamp=None):
        """Feed one paired measurement into both engines and delta buffer."""
        import math
        if math.isnan(ref_temp) or math.isnan(dut_temp):
            return
        self._ref_engine.add_sample(ref_temp, timestamp)
        self._dut_engine.add_sample(dut_temp, timestamp)
        self._delta_buf.append(dut_temp - ref_temp)

    @property
    def is_stable(self) -> bool:
        return self.dual_status().get("stable", False)

    def dual_status(self) -> dict:
        """
        Returns comprehensive stability dict with per-channel and combined info.
        Always returns a safe numeric dict — never raises.
        """
        ref_st = self._ref_engine.status()
        dut_st = self._dut_engine.status()

        # Delta_T sigma
        n_delta = len(self._delta_buf)
        delta_sigma = 0.0
        if n_delta >= 2:
            vals = list(self._delta_buf)
            mean_d = sum(vals) / n_delta
            var_d  = sum((v - mean_d)**2 for v in vals) / (n_delta - 1)
            delta_sigma = math.sqrt(max(0.0, var_d))

        delta_ok = delta_sigma < self._delta_sigma_thresh

        # Combined stable = all three conditions
        combined = (ref_st.get("stable", False) and
                    dut_st.get("stable", False) and
                    delta_ok)

        return {
            "stable":                combined,
            # Reference channel
            "ref_stable":            ref_st.get("stable", False),
            "ref_slope_ok":          ref_st.get("slope_ok", False),
            "ref_slope_val":         ref_st.get("slope_val", 0.0),
            "ref_sigma_ok":          ref_st.get("sigma_ok", False),
            "ref_sigma_val":         ref_st.get("sigma_val", 0.0),
            "ref_prox_ok":           ref_st.get("prox_ok", False),
            "ref_prox_val":          ref_st.get("prox_val", 0.0),
            "ref_dwell_elapsed":     ref_st.get("dwell_elapsed", 0.0),
            "ref_dwell_required":    ref_st.get("dwell_required", 30.0),
            # DUT channel
            "dut_stable":            dut_st.get("stable", False),
            "dut_slope_ok":          dut_st.get("slope_ok", False),
            "dut_slope_val":         dut_st.get("slope_val", 0.0),
            "dut_sigma_ok":          dut_st.get("sigma_ok", False),
            "dut_sigma_val":         dut_st.get("sigma_val", 0.0),
            "dut_prox_ok":           dut_st.get("prox_ok", False),
            "dut_prox_val":          dut_st.get("prox_val", 0.0),
            "dut_dwell_elapsed":     dut_st.get("dwell_elapsed", 0.0),
            "dut_dwell_required":    dut_st.get("dwell_required", 30.0),
            # Differential
            "delta_sigma":           delta_sigma,
            "delta_sigma_thresh":    self._delta_sigma_thresh,
            "delta_ok":              delta_ok,
            # Shared thresholds
            "slope_thresh":          self._ref_engine.slope_thresh,
            "sigma_thresh":          self._ref_engine.sigma_thresh,
            "prox_thresh":           self._ref_engine.proximity_deg,
            "dwell_required":        self._ref_engine.dwell_seconds,
        }
