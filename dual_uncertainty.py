# dual_uncertainty.py — CALIBRIX v2
#
# Dual RTD uncertainty path for Dual RTD Comparison Calibration.
# Primary measurement is Delta_T = DUT - Reference.
#
# Components:
#   u_ref_adc   — Reference channel ADC resolution (Type B, rectangular)
#   u_dut_adc   — DUT channel ADC resolution (Type B, rectangular)
#   u_ref_heat  — Reference channel self-heating (Type B, rectangular)
#   u_dut_heat  — DUT channel self-heating (Type B, rectangular)
#   u_ref_std   — Reference standard uncertainty from certificate (Type B)
#   u_rep       — Repeatability of Delta_T (Type A, σ/√n)
#
# NOT included: CVD model residual (not applicable as physical-reference term
#               in direct comparison calibration).
#
# Combined:  u_c = sqrt(u_ref_adc² + u_dut_adc² + u_ref_heat² +
#                        u_dut_heat² + u_ref_std² + u_rep²)
# Expanded:  U = k × u_c  (k=2, 95% confidence)
#
# Assumptions are explicit and not fabricated.

import math

_ADC_BITS           = 15       # MAX31865 15-bit ADC
_PT100_SENSITIVITY  = 0.385    # Ω/°C at 0°C (IEC 60751, approx for R0=100Ω)
_RTD_CURRENT_A      = 0.25e-3  # Excitation current (A), typical MAX31865
_THERMAL_RES        = 0.15     # °C/mW thermal resistance (PT100 in still air)
_COVERAGE_FACTOR    = 2        # k=2 → 95% confidence


class DualUncertaintyCalculator:
    """
    GUM-compliant uncertainty budget for Dual RTD comparison calibration.

    The measurement of interest is Delta_T = T_DUT - T_Reference.
    Reference channel uncertainty comes from the reference standard certificate.
    DUT channel uncertainty comes from the measurement system only.

    Parameters
    ----------
    r_ref : float
        Reference resistor on MAX31865 (Ω). Used for ADC resolution.
    u_ref_expanded : float
        Expanded uncertainty of the Reference RTD (from its calibration certificate), °C.
        Default 0.05 °C — must be provided; not fabricated.
    k_ref : int
        Coverage factor of the reference certificate (default 2).
    nominal_r0 : float
        Nominal RTD resistance at 0°C (Ω). Default 100.0 for PT100.
    """

    def __init__(self,
                 r_ref: float = 430.0,
                 u_ref_expanded: float = 0.05,
                 k_ref: int = 2,
                 nominal_r0: float = 100.0):
        self.r_ref          = float(r_ref)
        self.u_ref_expanded = float(u_ref_expanded)
        self.k_ref          = int(k_ref)
        self.nominal_r0     = float(nominal_r0)

    def _u_adc(self) -> float:
        """ADC resolution for one MAX31865 channel (Type B, rectangular)."""
        lsb_ohm = self.r_ref / (2 ** _ADC_BITS)
        lsb_deg = lsb_ohm / _PT100_SENSITIVITY
        return lsb_deg / math.sqrt(3)

    def _u_self_heating(self) -> float:
        """Self-heating for one channel (Type B, rectangular)."""
        power_mw = (_RTD_CURRENT_A ** 2) * self.nominal_r0 * 1000
        heat_deg = power_mw * _THERMAL_RES
        return heat_deg / math.sqrt(3)

    def _u_ref_standard(self) -> float:
        """Type B — uncertainty of the Reference RTD from its certificate."""
        return self.u_ref_expanded / self.k_ref

    def compute(self, std_delta: float, n_samples: int) -> dict:
        """
        Compute full dual uncertainty budget.

        Parameters
        ----------
        std_delta : float
            Sample standard deviation of Delta_T series (°C).
        n_samples : int
            Number of paired samples collected.

        Returns
        -------
        dict with all components, u_c, U, and formatted strings.
        """
        u_ref_adc  = self._u_adc()
        u_dut_adc  = self._u_adc()
        u_ref_heat = self._u_self_heating()
        u_dut_heat = self._u_self_heating()
        u_ref_std  = self._u_ref_standard()
        u_rep      = float(std_delta) / math.sqrt(max(n_samples, 1))

        u_c = math.sqrt(u_ref_adc**2 + u_dut_adc**2 +
                        u_ref_heat**2 + u_dut_heat**2 +
                        u_ref_std**2  + u_rep**2)
        U   = _COVERAGE_FACTOR * u_c

        return {
            "u_ref_adc":            u_ref_adc,
            "u_dut_adc":            u_dut_adc,
            "u_ref_self_heating":   u_ref_heat,
            "u_dut_self_heating":   u_dut_heat,
            "u_ref_standard":       u_ref_std,
            "u_repeatability":      u_rep,
            "combined_u_c":         u_c,
            "coverage_factor_k":    _COVERAGE_FACTOR,
            "expanded_uncertainty": U,
            "expanded_uncertainty_str": (
                f"U = ±{U:.4f}°C (k={_COVERAGE_FACTOR}, 95% confidence level)\n"
                f"[Ref cert U_ref={self.u_ref_expanded}°C, k={self.k_ref}; "
                f"ADC resolution; self-heating; repeatability of ΔT]"
            ),
            "components_table": [
                ["Component",                 "Symbol",       "Type", "Value (°C)"],
                ["Ref ADC Resolution",         "u_ref_adc",   "B",    f"{u_ref_adc:.5f}"],
                ["DUT ADC Resolution",         "u_dut_adc",   "B",    f"{u_dut_adc:.5f}"],
                ["Ref Self-Heating",           "u_ref_heat",  "B",    f"{u_ref_heat:.5f}"],
                ["DUT Self-Heating",           "u_dut_heat",  "B",    f"{u_dut_heat:.5f}"],
                ["Reference Standard Cert.",   "u_ref_std",   "B",    f"{u_ref_std:.5f}"],
                ["Repeatability of ΔT",        "u_rep",       "A",    f"{u_rep:.5f}"],
                ["Combined Std. Unc.",         "u_c",         "-",    f"{u_c:.5f}"],
                [f"Expanded Unc. (k={_COVERAGE_FACTOR})", "U", "-",  f"{U:.5f}"],
            ],
        }
