# calibration_sequencer.py — CALIBRIX v2
# ═══════════════════════════════════════════════════════════════════════
# Phase 3: Full Calibration Decision System
#
# Engineering corrections implemented:
#   §1  Per-point decision: |MBE| + U ≤ tolerance  (GUM + IEC 60751)
#   §2  Per-point uncertainty budget (u1–u5, u_c, U)
#   §3  Range validity enforcement per point
#   §5  As-Found / As-Left with full recomputation of metrics + uncertainty
#   §6  Correction = −MBE (strict consistency)
#   §7  Hysteresis support (bidirectional setpoints, e.g. 0→50→100→50→0)
#   §10 Data consistency: PointResult is the single source of truth
#
# State machine: IDLE → HEATING → STABILIZING → STABLE → RECORDING
#                                                         → NEXT_POINT → …
#                                                         → COMPLETE
# ═══════════════════════════════════════════════════════════════════════
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import List, Optional, Dict
import math

from uncertainty_calculator import UncertaintyCalculator
from metrics import ISO_LIMITS, ISO_RANGES


# ── State machine ─────────────────────────────────────────────────────
class CalState(Enum):
    IDLE        = auto()
    HEATING     = auto()
    STABILIZING = auto()
    STABLE      = auto()
    RECORDING   = auto()
    NEXT_POINT  = auto()
    COMPLETE    = auto()


# Valid forward transitions — no skipping allowed (§2-12)
_VALID_TRANSITIONS = {
    CalState.IDLE:        {CalState.HEATING},
    CalState.HEATING:     {CalState.STABILIZING},
    CalState.STABILIZING: {CalState.STABLE},
    CalState.STABLE:      {CalState.RECORDING, CalState.STABILIZING},
    CalState.RECORDING:   {CalState.NEXT_POINT, CalState.COMPLETE, CalState.STABILIZING},
    CalState.NEXT_POINT:  {CalState.HEATING},
    CalState.COMPLETE:    set(),
}


# ── Per-point result (single source of truth) ─────────────────────────
@dataclass
class PointResult:
    """
    Complete calibration record for one setpoint.
    Contains As-Found, correction, As-Left, uncertainty budgets,
    decision values and verdicts.  No 'N/A' allowed in final output.
    """
    setpoint:            float
    point_index:         int   = 0
    direction:           str   = "ascending"     # §7: "ascending" or "descending"

    measured_samples:    List[float] = field(default_factory=list)
    standard_samples:    List[float] = field(default_factory=list)
    resistance_samples:  List[float] = field(default_factory=list)

    # Range validity (§3)
    range_valid:         bool  = True
    range_message:       str   = ""

    # IEC 60751 tolerance at this setpoint
    tolerance_limit:     float = 0.0             # A + B·|T|

    # ── As-Found (raw measurements) ────────────────────────────────
    as_found_mean:       float = 0.0
    as_found_mbe:        float = 0.0             # mean(measured − standard)
    as_found_mae:        float = 0.0
    as_found_rmse:       float = 0.0
    as_found_std:        float = 0.0             # Bessel-corrected σ
    as_found_uncertainty:  dict  = field(default_factory=dict)   # full GUM budget
    as_found_expanded_U:   float = 0.0           # U = k × u_c
    as_found_decision_value: float = 0.0         # |MBE| + U
    as_found_verdict:      str   = "N/A"

    # ── Correction ─────────────────────────────────────────────────
    correction_offset:   float = 0.0             # = −MBE  (= standard − measured_mean)

    # ── As-Left (after correction applied) ─────────────────────────
    as_left_mean:        float = 0.0
    as_left_mbe:         float = 0.0
    as_left_mae:         float = 0.0
    as_left_rmse:        float = 0.0
    as_left_std:         float = 0.0
    as_left_uncertainty:   dict  = field(default_factory=dict)
    as_left_expanded_U:    float = 0.0
    as_left_decision_value: float = 0.0
    as_left_verdict:       str   = "N/A"

    # ── Final verdict ──────────────────────────────────────────────
    verdict:             str   = "N/A"           # authoritative = As-Left verdict
    limit_value:         float = 0.0             # = tolerance_limit (alias)

    # ── Improvement flag ───────────────────────────────────────────
    improvement:         bool  = False           # True if As-Left better than As-Found


# ── Calibration Sequencer ─────────────────────────────────────────────
class CalibrationSequencer:
    """
    Multi-point calibration state machine with full decision logic.

    At each point:
      1. Record N samples
      2. Compute As-Found stats + uncertainty + decision
      3. Apply correction (offset = −MBE)
      4. Recompute As-Left stats + uncertainty + decision
      5. Determine PASS/FAIL/BLOCKED verdict

    Decision rule (GUM + IEC 60751):
      decision_value = |MBE| + U
      PASS  iff  decision_value ≤ tolerance  AND  range_valid
    """

    def __init__(self, setpoints=None, samples_per_point=30,
                 stabilization_engine=None, tolerance_class="Class A",
                 r_ref=430.0, u_ref_expanded=0.05, k_ref=2,
                 nominal_r0=100.0):
        self.setpoints         = setpoints if setpoints else [0.0, 50.0, 100.0]
        self.samples_per_point = samples_per_point
        self.stab_engine       = stabilization_engine
        self.tolerance_class   = tolerance_class

        # §2: Per-point uncertainty calculator
        self._uc = UncertaintyCalculator(
            r_ref=r_ref,
            u_ref_expanded=u_ref_expanded,
            k_ref=k_ref,
            nominal_r0=nominal_r0,
        )

        self._point_idx = 0
        self._state     = CalState.IDLE
        self.all_results: List[PointResult] = []
        self._current:   Optional[PointResult] = None

        # §7: Compute direction for each setpoint (hysteresis support)
        self._directions = self._compute_directions()

    # ─── Direction detection for hysteresis ────────────────────────
    def _compute_directions(self) -> List[str]:
        """
        §7: Determine ascending/descending direction for each setpoint.
        For [0,50,100,50,0] → ['ascending','ascending','ascending','descending','descending']
        """
        dirs = []
        for i in range(len(self.setpoints)):
            if i == 0:
                if len(self.setpoints) > 1:
                    dirs.append("ascending" if self.setpoints[1] >= self.setpoints[0]
                                else "descending")
                else:
                    dirs.append("ascending")
            else:
                dirs.append("ascending" if self.setpoints[i] >= self.setpoints[i - 1]
                            else "descending")
        return dirs

    # ─── Properties ───────────────────────────────────────────────
    @property
    def state(self):
        return self._state

    @property
    def current_setpoint(self):
        if self._point_idx < len(self.setpoints):
            return self.setpoints[self._point_idx]
        return self.setpoints[-1]

    @property
    def current_point_result(self):
        return self._current

    # ─── State transition (enforced, no skipping) ─────────────────
    def _transition(self, new_state: CalState) -> bool:
        if new_state not in _VALID_TRANSITIONS.get(self._state, set()):
            print(f"[SEQUENCER] Invalid transition {self._state} → {new_state} — ignored")
            return False
        self._state = new_state
        return True

    # ─── Start / advance ──────────────────────────────────────────
    def start(self):
        self._point_idx  = 0
        self.all_results = []
        self._state      = CalState.IDLE
        self._enter_point()

    def advance_to_next(self):
        if self._state == CalState.NEXT_POINT:
            self._enter_point()

    def _enter_point(self):
        sp  = self.current_setpoint
        idx = self._point_idx
        d   = self._directions[idx] if idx < len(self._directions) else "ascending"
        self._current = PointResult(setpoint=sp, point_index=idx, direction=d)
        if self.stab_engine:
            self.stab_engine.reset(setpoint=sp)
        self._transition(CalState.HEATING)

    # ─── Add reading (called every sample tick) ───────────────────
    def add_reading(self, measured_temp: float, standard_temp: float, resistance: float):
        if math.isnan(measured_temp):
            return

        if self._state == CalState.HEATING:
            if self.stab_engine:
                self.stab_engine.add_sample(measured_temp)
                st = self.stab_engine.status()
                if st.get("prox_ok"):
                    self._transition(CalState.STABILIZING)
            else:
                self._transition(CalState.STABILIZING)

        elif self._state == CalState.STABILIZING:
            if self.stab_engine:
                self.stab_engine.add_sample(measured_temp)
                if self.stab_engine.is_stable:
                    self._transition(CalState.STABLE)
            else:
                self._transition(CalState.STABLE)

        elif self._state == CalState.STABLE:
            # Transition to RECORDING ONLY if engine confirms stability on this tick
            if self.stab_engine:
                self.stab_engine.add_sample(measured_temp)
                if not self.stab_engine.is_stable:
                    self._state = CalState.STABILIZING
                    return
            self._transition(CalState.RECORDING)

        elif self._state == CalState.RECORDING:
            # Block transient phase completely: Discard buffer if stability is lost
            if self.stab_engine:
                self.stab_engine.add_sample(measured_temp)
                if not self.stab_engine.is_stable:
                    self._current.measured_samples.clear()
                    self._current.standard_samples.clear()
                    self._current.resistance_samples.clear()
                    self._state = CalState.STABILIZING
                    return

            self._current.measured_samples.append(measured_temp)
            self._current.standard_samples.append(standard_temp)
            self._current.resistance_samples.append(resistance)
            if len(self._current.measured_samples) >= self.samples_per_point:
                self._finalise_point()

    # ─── Stabilization status ─────────────────────────────────────
    def stabilization_status(self):
        if self.stab_engine and self._state in (CalState.HEATING, CalState.STABILIZING):
            return self.stab_engine.status()
        return {}

    # ══════════════════════════════════════════════════════════════
    # CORE DECISION ENGINE — _finalise_point()
    # ══════════════════════════════════════════════════════════════
    def _finalise_point(self):
        """
        Complete decision pipeline for one calibration point:
          1. Compute As-Found statistics
          2. Check range validity (§3)
          3. Compute As-Found uncertainty budget (§2)
          4. Compute As-Found decision value = |MBE| + U (§1)
          5. Determine As-Found verdict
          6. Compute correction = −MBE (§6)
          7. Apply correction → As-Left samples
          8. Recompute As-Left statistics (§5)
          9. Recompute As-Left uncertainty (§5)
         10. Compute As-Left decision value (§5)
         11. Determine As-Left verdict (§5)
         12. Set final verdict = As-Left verdict
        """
        m = self._current.measured_samples
        s = self._current.standard_samples
        n = len(m)
        if n == 0:
            return

        setpoint = self._current.setpoint

        # ── Step 0: Tolerance limit ───────────────────────────────
        A, B = ISO_LIMITS.get(self.tolerance_class, (0.30, 0.005))
        tolerance = A + B * abs(setpoint)
        self._current.tolerance_limit = tolerance
        self._current.limit_value     = tolerance

        # ── Step 2: Range validity check (§3) ─────────────────────
        if self.tolerance_class in ISO_RANGES:
            lo, hi = ISO_RANGES[self.tolerance_class]
            if not (lo <= setpoint <= hi):
                self._current.range_valid   = False
                self._current.range_message = (
                    f"{self.tolerance_class} not valid at {setpoint:.1f}°C "
                    f"(valid range: {lo:.0f} to {hi:.0f}°C)")

        # ── Step 1: As-Found statistics ───────────────────────────
        af = self._compute_stats(m, s, n)
        self._current.as_found_mean = af["mean"]
        self._current.as_found_mbe  = af["mbe"]
        self._current.as_found_mae  = af["mae"]
        self._current.as_found_rmse = af["rmse"]
        self._current.as_found_std  = af["std"]

        # ── Step 3: As-Found uncertainty budget (§2) ──────────────
        af_ub = self._uc.compute(std_dev_measured=af["std"], n_samples=n)
        self._current.as_found_uncertainty  = af_ub
        self._current.as_found_expanded_U   = af_ub["expanded_uncertainty"]

        # ── Step 4: As-Found decision value (§1) ──────────────────
        af_decision = abs(af["mbe"]) + af_ub["expanded_uncertainty"]
        self._current.as_found_decision_value = af_decision

        # ── Step 5: As-Found verdict ──────────────────────────────
        if not self._current.range_valid:
            self._current.as_found_verdict = "BLOCKED"
        else:
            self._current.as_found_verdict = (
                "PASS" if af_decision <= tolerance else "FAIL")

        # ── Step 6: Correction = −MBE (§6) ────────────────────────
        #   correction = standard − measured_mean = −MBE
        offset = -af["mbe"]
        self._current.correction_offset = offset

        # ── Step 7: Apply correction → As-Left ────────────────────
        m_corrected = [v + offset for v in m]

        # ── Step 8: Recompute As-Left statistics (§5) ─────────────
        al = self._compute_stats(m_corrected, s, n)
        self._current.as_left_mean = al["mean"]
        self._current.as_left_mbe  = al["mbe"]
        self._current.as_left_mae  = al["mae"]
        self._current.as_left_rmse = al["rmse"]
        self._current.as_left_std  = al["std"]

        # ── Step 9: Recompute As-Left uncertainty (§5) ────────────
        al_ub = self._uc.compute(std_dev_measured=al["std"], n_samples=n)
        self._current.as_left_uncertainty  = al_ub
        self._current.as_left_expanded_U   = al_ub["expanded_uncertainty"]

        # ── Step 10: As-Left decision value (§5) ──────────────────
        al_decision = abs(al["mbe"]) + al_ub["expanded_uncertainty"]
        self._current.as_left_decision_value = al_decision

        # ── Step 11: As-Left verdict (§5) ─────────────────────────
        if not self._current.range_valid:
            self._current.as_left_verdict = "BLOCKED"
        else:
            self._current.as_left_verdict = (
                "PASS" if al_decision <= tolerance else "FAIL")

        # ── Step 12: Final verdict + improvement flag ─────────────
        self._current.verdict = self._current.as_left_verdict
        self._current.improvement = (
            self._current.as_left_decision_value <
            self._current.as_found_decision_value)

        # ── Append and advance ────────────────────────────────────
        self.all_results.append(self._current)
        self._point_idx += 1

        if self._point_idx >= len(self.setpoints):
            self._transition(CalState.COMPLETE)
        else:
            self._transition(CalState.NEXT_POINT)

    # ─── Statistics helper ────────────────────────────────────────
    @staticmethod
    def _compute_stats(measured, standard, n):
        """Compute mean, MBE, MAE, RMSE, std_dev from matched arrays."""
        mean_m = sum(measured) / n
        mbe    = sum(a - b for a, b in zip(measured, standard)) / n
        mae    = sum(abs(a - b) for a, b in zip(measured, standard)) / n
        rmse   = math.sqrt(sum((a - b)**2 for a, b in zip(measured, standard)) / n)
        ddof   = n - 1 if n > 1 else 1
        std    = math.sqrt(sum((a - mean_m)**2 for a in measured) / ddof)
        return {"mean": mean_m, "mbe": mbe, "mae": mae, "rmse": rmse, "std": std}

    # ══════════════════════════════════════════════════════════════
    # AGGREGATE RESULTS (§7 hysteresis, §10 consistency)
    # ══════════════════════════════════════════════════════════════
    def aggregate_results(self) -> dict:
        """
        Aggregate all per-point results into a single payload for
        report generation and database storage.
        Includes hysteresis analysis (§7) and overall verdict.
        """
        if not self.all_results:
            return {}

        all_m = []
        all_s = []
        per_point = []

        for r in self.all_results:
            all_m.extend(r.measured_samples)
            all_s.extend(r.standard_samples)
            per_point.append({
                # Identity
                "setpoint":        r.setpoint,
                "direction":       r.direction,
                "point_index":     r.point_index,
                "range_valid":     r.range_valid,
                "range_message":   r.range_message,
                "tolerance":       r.tolerance_limit,

                # As-Found
                "mean":            r.as_found_mean,
                "mbe":             r.as_found_mbe,
                "mae":             r.as_found_mae,
                "rmse":            r.as_found_rmse,
                "std_dev":         r.as_found_std,
                "as_found_U":      r.as_found_expanded_U,
                "as_found_decision": r.as_found_decision_value,
                "as_found_verdict":  r.as_found_verdict,

                # Correction
                "correction":      r.correction_offset,

                # As-Left
                "as_left_mean":    r.as_left_mean,
                "as_left_mbe":     r.as_left_mbe,
                "as_left_mae":     r.as_left_mae,
                "as_left_std":     r.as_left_std,
                "as_left_U":       r.as_left_expanded_U,
                "as_left_decision": r.as_left_decision_value,
                "as_left_verdict":  r.as_left_verdict,

                # Final
                "verdict":         r.verdict,
                "improvement":     r.improvement,

                # Full uncertainty budget for this point
                "uncertainty_budget": r.as_found_uncertainty,

                # Backward-compat aliases
                "as_found":        r.as_found_mbe,
                "as_left":         r.as_left_mbe,
                "uncertainty_U":   r.as_found_expanded_U,
                "uncertainty_str": r.as_found_uncertainty.get(
                    "expanded_uncertainty_str", ""),
            })

        # ── Overall statistics ────────────────────────────────────
        n = len(all_m)
        if n == 0:
            return {"per_point": per_point, "overall_verdict": "N/A"}

        mean_m  = sum(all_m) / n
        mbe     = sum(a - b for a, b in zip(all_m, all_s)) / n
        mae     = sum(abs(a - b) for a, b in zip(all_m, all_s)) / n
        rmse    = math.sqrt(sum((a - b)**2 for a, b in zip(all_m, all_s)) / n)
        ddof    = n - 1 if n > 1 else 1
        std_dev = math.sqrt(sum((a - mean_m)**2 for a in all_m) / ddof)
        cv_pct  = (std_dev / abs(mean_m) * 100) if mean_m != 0 else 0.0

        # ── Overall verdict ───────────────────────────────────────
        all_v = [r.verdict for r in self.all_results]
        if "BLOCKED" in all_v:
            overall_verdict = "BLOCKED"
        elif "FAIL" in all_v:
            overall_verdict = "FAIL"
        elif all(v == "PASS" for v in all_v):
            overall_verdict = "PASS"
        else:
            overall_verdict = "FAIL"

        # ── Worst-case uncertainty (conservative) ─────────────────
        max_U = max((r.as_left_expanded_U for r in self.all_results), default=0.0)

        # ── §7: Hysteresis analysis ───────────────────────────────
        hysteresis_table = self._compute_hysteresis()

        return {
            "mean_measured":    mean_m,
            "std_dev":          std_dev,
            "mbe":              mbe,
            "mae":              mae,
            "rmse":             rmse,
            "cv_percent":       cv_pct,
            "per_point":        per_point,
            "all_measured":     all_m,
            "all_standard":     all_s,
            "overall_verdict":  overall_verdict,
            "worst_case_U":     max_U,
            "hysteresis":       hysteresis_table,
            "max_hysteresis":   (max(h["hysteresis"] for h in hysteresis_table)
                                 if hysteresis_table else 0.0),
        }

    def _compute_hysteresis(self) -> List[dict]:
        """
        §7: Hysteresis = |MBE_ascending − MBE_descending| at each
        shared temperature.  Only populated when bidirectional setpoints
        are used (e.g. 0→50→100→50→0).
        """
        asc_errors  = {}
        desc_errors = {}
        for r in self.all_results:
            bucket = asc_errors if r.direction == "ascending" else desc_errors
            bucket[r.setpoint] = r.as_found_mbe

        table = []
        for temp in sorted(set(asc_errors.keys()) & set(desc_errors.keys())):
            h = abs(asc_errors[temp] - desc_errors[temp])
            table.append({
                "temperature":    temp,
                "ascending_mbe":  asc_errors[temp],
                "descending_mbe": desc_errors[temp],
                "hysteresis":     h,
            })
        return table
