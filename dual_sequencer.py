# dual_sequencer.py — CALIBRIX v2
#
# Dual RTD calibration state machine.
# Primary measurement: Delta_T = DUT - Reference
# Decision rule: |mean(Delta_T)| + U_dual <= tolerance  → PASS
# Correction: correction = Reference - DUT = -mean(Delta_T)
# As-Found preserved; As-Left computed after correction.
#
# Reuses CalState enum from calibration_sequencer where applicable.
# Extended with CHANNEL_CHECK, FAULT, ABORTED states.

from enum import Enum, auto
from dataclasses import dataclass, field
from typing import List, Optional
import math
import time

from metrics import ISO_LIMITS, ISO_RANGES
from dual_uncertainty import DualUncertaintyCalculator


class DualCalState(Enum):
    IDLE          = auto()
    CHANNEL_CHECK = auto()
    HEATING       = auto()
    STABILIZING   = auto()
    STABLE        = auto()
    RECORDING     = auto()
    POINT_COMPLETE = auto()
    COMPLETE      = auto()
    ABORTED       = auto()
    FAULT         = auto()


# Valid forward transitions
_DUAL_TRANSITIONS = {
    DualCalState.IDLE:          {DualCalState.CHANNEL_CHECK},
    DualCalState.CHANNEL_CHECK: {DualCalState.HEATING, DualCalState.FAULT},
    DualCalState.HEATING:       {DualCalState.STABILIZING, DualCalState.FAULT},
    DualCalState.STABILIZING:   {DualCalState.STABLE, DualCalState.FAULT},
    DualCalState.STABLE:        {DualCalState.RECORDING, DualCalState.STABILIZING},
    DualCalState.RECORDING:     {DualCalState.POINT_COMPLETE, DualCalState.STABILIZING, DualCalState.FAULT},
    DualCalState.POINT_COMPLETE:{DualCalState.HEATING, DualCalState.COMPLETE},
    DualCalState.COMPLETE:      set(),
    DualCalState.ABORTED:       set(),
    DualCalState.FAULT:         {DualCalState.ABORTED},
}


@dataclass
class DualPointResult:
    """
    Complete calibration record for one setpoint in Dual RTD mode.
    Primary comparison is Delta_T = DUT - Reference.
    """
    setpoint:               float = 0.0
    point_index:            int   = 0
    direction:              str   = "ascending"
    n_samples:              int   = 0

    # Raw paired samples
    ref_samples:            List[float] = field(default_factory=list)
    dut_samples:            List[float] = field(default_factory=list)
    delta_samples:          List[float] = field(default_factory=list)   # DUT - REF
    ref_resistance_samples: List[float] = field(default_factory=list)
    dut_resistance_samples: List[float] = field(default_factory=list)

    # Range validity
    range_valid:            bool  = True
    range_message:          str   = ""

    # IEC tolerance at this setpoint
    tolerance_limit:        float = 0.0

    # ── As-Found (raw Delta_T) ────────────────────────────────────
    mean_ref:               float = 0.0
    mean_dut:               float = 0.0
    mean_delta:             float = 0.0      # mean(DUT - Reference)
    std_delta:              float = 0.0      # σ of Delta_T
    mae_delta:              float = 0.0
    rmse_delta:             float = 0.0
    max_pos_error:          float = 0.0
    max_neg_error:          float = 0.0
    max_abs_error:          float = 0.0

    as_found_delta:         float = 0.0      # = mean_delta (alias)
    as_found_uncertainty:   dict  = field(default_factory=dict)
    as_found_expanded_U:    float = 0.0
    as_found_decision:      float = 0.0      # |mean_delta| + U
    as_found_verdict:       str   = "N/A"

    # ── Correction ────────────────────────────────────────────────
    correction_offset:      float = 0.0      # = -(mean_delta) = Reference - DUT

    # ── As-Left (after correction) ────────────────────────────────
    as_left_delta:          float = 0.0
    as_left_std:            float = 0.0
    as_left_uncertainty:    dict  = field(default_factory=dict)
    as_left_expanded_U:     float = 0.0
    as_left_decision:       float = 0.0
    as_left_verdict:        str   = "N/A"

    # ── Final verdict ─────────────────────────────────────────────
    verdict:                str   = "N/A"
    improvement:            bool  = False


class DualCalSequencer:
    """
    Multi-point Dual RTD calibration state machine.

    Workflow per point:
        1. Record N paired samples
        2. Compute As-Found: mean(Delta_T), σ, |mean|+U
        3. Determine As-Found verdict
        4. Compute correction = -mean(Delta_T)
        5. Compute As-Left: apply correction, recompute
        6. Final verdict = As-Left verdict
    """

    def __init__(self,
                 setpoints=None,
                 samples_per_point: int = 30,
                 dual_stab_engine=None,
                 tolerance_class: str = "Class A",
                 u_ref_expanded: float = 0.05,
                 k_ref: int = 2,
                 r_ref: float = 430.0,
                 nominal_r0: float = 100.0):

        self.setpoints         = setpoints if setpoints else [0.0, 50.0, 100.0]
        self.samples_per_point = int(samples_per_point)
        self.stab_engine       = dual_stab_engine   # DualStabilizationEngine
        self.tolerance_class   = tolerance_class

        self._uc = DualUncertaintyCalculator(
            r_ref=r_ref,
            u_ref_expanded=u_ref_expanded,
            k_ref=k_ref,
            nominal_r0=nominal_r0,
        )

        self._point_idx   = 0
        self._state       = DualCalState.IDLE
        self.all_results: List[DualPointResult] = []
        self._current:    Optional[DualPointResult] = None
        self._directions  = self._compute_directions()

        # State timestamp for dwell tracking
        self._state_time  = time.time()

    def _compute_directions(self) -> List[str]:
        dirs = []
        for i in range(len(self.setpoints)):
            if i == 0:
                dirs.append("ascending" if len(self.setpoints) < 2 or
                             self.setpoints[1] >= self.setpoints[0] else "descending")
            else:
                dirs.append("ascending" if self.setpoints[i] >= self.setpoints[i - 1]
                            else "descending")
        return dirs

    @property
    def state(self) -> DualCalState:
        return self._state

    @property
    def current_setpoint(self) -> float:
        if self._point_idx < len(self.setpoints):
            return self.setpoints[self._point_idx]
        return self.setpoints[-1]

    @property
    def current_point_result(self) -> Optional[DualPointResult]:
        return self._current

    def _transition(self, new_state: DualCalState) -> bool:
        if new_state not in _DUAL_TRANSITIONS.get(self._state, set()):
            print(f"[DUAL SEQ] Invalid transition {self._state.name} -> {new_state.name} -- ignored")
            return False
        self._state = new_state
        self._state_time = time.time()
        return True

    def start(self):
        self._point_idx  = 0
        self.all_results = []
        self._state      = DualCalState.IDLE
        self._enter_channel_check()

    def abort(self):
        self._state = DualCalState.ABORTED

    def flag_fault(self):
        if self._state not in (DualCalState.COMPLETE, DualCalState.ABORTED):
            self._state = DualCalState.FAULT

    def advance_to_next(self):
        if self._state == DualCalState.POINT_COMPLETE:
            self._point_idx += 1
            if self._point_idx >= len(self.setpoints):
                self._state = DualCalState.COMPLETE
            else:
                self._enter_point()

    def _enter_channel_check(self):
        # Direct state set (no transition validation needed from IDLE->IDLE)
        self._state = DualCalState.CHANNEL_CHECK
        self._state_time = time.time()

    def complete_channel_check(self, success: bool = True):
        """Called after initial channel self-check. Enters first point."""
        if self._state == DualCalState.CHANNEL_CHECK:
            if success:
                self._enter_point()
            else:
                self._state = DualCalState.FAULT

    def _enter_point(self):
        sp  = self.current_setpoint
        idx = self._point_idx
        d   = self._directions[idx] if idx < len(self._directions) else "ascending"
        self._current = DualPointResult(setpoint=sp, point_index=idx, direction=d)
        if self.stab_engine:
            self.stab_engine.reset(setpoint=sp)
        # Force state directly (CHANNEL_CHECK → HEATING is not in table)
        self._state = DualCalState.HEATING
        self._state_time = time.time()

    def add_reading(self, ref_temp: float, dut_temp: float,
                    ref_res: float = 0.0, dut_res: float = 0.0,
                    ref_valid: bool = True, dut_valid: bool = True):
        """
        Feed one paired reading into the state machine.
        Invalid/NaN readings are skipped for recording but still feed the
        stabilization engine if valid.
        """
        import math as _math
        if _math.isnan(ref_temp) or _math.isnan(dut_temp):
            return
        if not ref_valid or not dut_valid:
            # Feed stab engine with whatever is valid
            if self.stab_engine:
                r_val = ref_temp if ref_valid and not _math.isnan(ref_temp) else None
                d_val = dut_temp if dut_valid and not _math.isnan(dut_temp) else None
                if r_val is not None or d_val is not None:
                    self.stab_engine.add_sample(r_val, d_val, None)
            return

        delta = dut_temp - ref_temp

        if self._state == DualCalState.HEATING:
            if self.stab_engine:
                self.stab_engine.add_sample(ref_temp, dut_temp)
                st = self.stab_engine.dual_status()
                if st.get("ref_prox_ok") and st.get("dut_prox_ok"):
                    self._transition(DualCalState.STABILIZING)
            else:
                self._transition(DualCalState.STABILIZING)

        elif self._state == DualCalState.STABILIZING:
            if self.stab_engine:
                self.stab_engine.add_sample(ref_temp, dut_temp)
                if self.stab_engine.is_stable:
                    self._transition(DualCalState.STABLE)
            else:
                self._transition(DualCalState.STABLE)

        elif self._state == DualCalState.STABLE:
            if self.stab_engine:
                self.stab_engine.add_sample(ref_temp, dut_temp)
                if not self.stab_engine.is_stable:
                    self._state = DualCalState.STABILIZING
                    return
            self._transition(DualCalState.RECORDING)

        elif self._state == DualCalState.RECORDING:
            if self.stab_engine:
                self.stab_engine.add_sample(ref_temp, dut_temp)
                if not self.stab_engine.is_stable:
                    # Lost stability — discard buffer and re-stabilize
                    self._current.ref_samples.clear()
                    self._current.dut_samples.clear()
                    self._current.delta_samples.clear()
                    self._current.ref_resistance_samples.clear()
                    self._current.dut_resistance_samples.clear()
                    self._state = DualCalState.STABILIZING
                    return

            self._current.ref_samples.append(ref_temp)
            self._current.dut_samples.append(dut_temp)
            self._current.delta_samples.append(delta)
            self._current.ref_resistance_samples.append(ref_res)
            self._current.dut_resistance_samples.append(dut_res)

            if len(self._current.ref_samples) >= self.samples_per_point:
                self._finalise_point()

    def stabilization_status(self) -> dict:
        if self.stab_engine and self._state in (DualCalState.HEATING, DualCalState.STABILIZING,
                                                 DualCalState.STABLE, DualCalState.RECORDING):
            return self.stab_engine.dual_status()
        return {}

    # ── Point finalisation ────────────────────────────────────────────

    def _finalise_point(self):
        r = self._current
        n = len(r.ref_samples)
        if n == 0:
            return

        # Tolerance
        A, B = ISO_LIMITS.get(self.tolerance_class, (0.30, 0.005))
        tol  = A + B * abs(r.setpoint)
        r.tolerance_limit = tol

        # Range validity
        if self.tolerance_class in ISO_RANGES:
            lo, hi = ISO_RANGES[self.tolerance_class]
            if not (lo <= r.setpoint <= hi):
                r.range_valid   = False
                r.range_message = (f"{self.tolerance_class} not valid at {r.setpoint:.1f}°C "
                                   f"(valid range: {lo:.0f} to {hi:.0f}°C)")

        # ── As-Found statistics (Delta_T based) ───────────────────
        deltas  = r.delta_samples
        refs    = r.ref_samples
        duts    = r.dut_samples

        mean_delta = sum(deltas) / n
        mean_ref   = sum(refs)   / n
        mean_dut   = sum(duts)   / n

        ddof = n - 1 if n > 1 else 1
        std_delta = math.sqrt(sum((d - mean_delta)**2 for d in deltas) / ddof)
        mae_delta = sum(abs(d) for d in deltas) / n
        mse_delta = sum(d**2 for d in deltas) / n
        rmse_delta = math.sqrt(mse_delta)
        max_pos = max(deltas)
        max_neg = min(deltas)
        max_abs = max(abs(d) for d in deltas)

        r.mean_ref     = mean_ref
        r.mean_dut     = mean_dut
        r.mean_delta   = mean_delta
        r.std_delta    = std_delta
        r.mae_delta    = mae_delta
        r.rmse_delta   = rmse_delta
        r.max_pos_error = max_pos
        r.max_neg_error = max_neg
        r.max_abs_error = max_abs
        r.as_found_delta = mean_delta
        r.n_samples    = n

        # ── As-Found uncertainty ──────────────────────────────────
        af_ub = self._uc.compute(std_delta=std_delta, n_samples=n)
        r.as_found_uncertainty = af_ub
        r.as_found_expanded_U  = af_ub["expanded_uncertainty"]

        # ── As-Found decision: |mean(Delta_T)| + U ───────────────
        af_dec = abs(mean_delta) + r.as_found_expanded_U
        r.as_found_decision = af_dec
        if not r.range_valid:
            r.as_found_verdict = "BLOCKED"
        else:
            r.as_found_verdict = "PASS" if af_dec <= tol else "FAIL"

        # ── Correction = -(mean_delta) = Reference - DUT ─────────
        correction = -mean_delta
        r.correction_offset = correction

        # ── As-Left: apply correction to delta samples ────────────
        al_deltas = [d + correction for d in deltas]
        al_mean   = sum(al_deltas) / n
        al_std    = math.sqrt(sum((d - al_mean)**2 for d in al_deltas) / ddof)

        al_ub  = self._uc.compute(std_delta=al_std, n_samples=n)
        al_dec = abs(al_mean) + al_ub["expanded_uncertainty"]

        r.as_left_delta       = al_mean
        r.as_left_std         = al_std
        r.as_left_uncertainty = al_ub
        r.as_left_expanded_U  = al_ub["expanded_uncertainty"]
        r.as_left_decision    = al_dec

        if not r.range_valid:
            r.as_left_verdict = "BLOCKED"
        else:
            r.as_left_verdict = "PASS" if al_dec <= tol else "FAIL"

        r.verdict     = r.as_left_verdict
        r.improvement = (al_dec < af_dec)

        self.all_results.append(r)

        if self._point_idx + 1 >= len(self.setpoints):
            self._state = DualCalState.COMPLETE
        else:
            self._state = DualCalState.POINT_COMPLETE

    # ── Aggregate results ─────────────────────────────────────────────

    def aggregate_results(self) -> dict:
        if not self.all_results:
            return {}

        all_deltas = []
        per_point  = []

        for r in self.all_results:
            all_deltas.extend(r.delta_samples)
            per_point.append({
                "setpoint":           r.setpoint,
                "direction":          r.direction,
                "point_index":        r.point_index,
                "n_samples":          r.n_samples,
                "range_valid":        r.range_valid,
                "range_message":      r.range_message,
                "tolerance":          r.tolerance_limit,
                # Core stats
                "mean_ref":           r.mean_ref,
                "mean_dut":           r.mean_dut,
                "mean_delta":         r.mean_delta,
                "std_delta":          r.std_delta,
                "mae_delta":          r.mae_delta,
                "rmse_delta":         r.rmse_delta,
                "max_pos_error":      r.max_pos_error,
                "max_neg_error":      r.max_neg_error,
                "max_abs_error":      r.max_abs_error,
                # As-Found
                "as_found_delta":     r.as_found_delta,
                "as_found_U":         r.as_found_expanded_U,
                "as_found_decision":  r.as_found_decision,
                "as_found_verdict":   r.as_found_verdict,
                # Correction
                "correction":         r.correction_offset,
                # As-Left
                "as_left_delta":      r.as_left_delta,
                "as_left_U":          r.as_left_expanded_U,
                "as_left_decision":   r.as_left_decision,
                "as_left_verdict":    r.as_left_verdict,
                # Final
                "verdict":            r.verdict,
                "improvement":        r.improvement,
                "uncertainty_budget": r.as_found_uncertainty,
                # Backward-compat aliases
                "mbe":                r.mean_delta,
                "as_found":           r.as_found_delta,
                "as_left":            r.as_left_delta,
            })

        # Overall
        n = len(all_deltas)
        if n == 0:
            return {"per_point": per_point, "overall_verdict": "N/A"}

        overall_mean  = sum(all_deltas) / n
        ddof          = n - 1 if n > 1 else 1
        overall_std   = math.sqrt(sum((d - overall_mean)**2 for d in all_deltas) / ddof)
        overall_mae   = sum(abs(d) for d in all_deltas) / n
        overall_rmse  = math.sqrt(sum(d**2 for d in all_deltas) / n)
        overall_max_abs = max((abs(d) for d in all_deltas), default=0.0)
        max_U         = max((r.as_left_expanded_U for r in self.all_results), default=0.0)

        as_left_verdicts = [r.as_left_verdict for r in self.all_results]
        as_found_verdicts = [r.as_found_verdict for r in self.all_results]

        if "BLOCKED" in as_left_verdicts or "BLOCKED" in as_found_verdicts:
            overall_verdict = "BLOCKED"
        elif "FAIL" in as_left_verdicts:
            overall_verdict = "FAIL"
        elif all(v == "PASS" for v in as_found_verdicts):
            overall_verdict = "PASS AS-FOUND"
        elif all(v == "PASS" for v in as_left_verdicts):
            overall_verdict = "PASS AFTER CORRECTION"
        else:
            overall_verdict = "FAIL"

        asc_points = {r.setpoint: r.as_found_delta for r in self.all_results if r.direction == "ascending"}
        desc_points = {r.setpoint: r.as_found_delta for r in self.all_results if r.direction == "descending"}
        max_hysteresis = 0.0
        for sp in asc_points:
            if sp in desc_points:
                h = abs(asc_points[sp] - desc_points[sp])
                if h > max_hysteresis:
                    max_hysteresis = h

        return {
            "mean_delta":       overall_mean,
            "std_delta":        overall_std,
            "mae_delta":        overall_mae,
            "rmse_delta":       overall_rmse,
            "max_abs_error":    overall_max_abs,
            "mbe":              overall_mean,
            "mae":              overall_mae,
            "rmse":             overall_rmse,
            "std_dev":          overall_std,
            "max_hysteresis":   max_hysteresis,
            "per_point":        per_point,
            "overall_verdict":  overall_verdict,
            "worst_case_U":     max_U,
        }
