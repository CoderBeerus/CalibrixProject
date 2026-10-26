# uncertainty_calculator.py
# CALIBRIX — Upgrade 1: GUM-Compliant Measurement Uncertainty Budget
# Standard: JCGM 100:2008 (Guide to the Expression of Uncertainty in Measurement)
#
# Five uncertainty components:
#   u1 — ADC Resolution          (Type B, rectangular)
#   u2 — Repeatability           (Type A, from std-dev of measurements)
#   u3 — Reference Standard      (Type B, from reference certificate)
#   u4 — Self-heating            (Type B, rectangular)
#   u5 — CVD Model Residual      (Type B, fixed, rectangular)
#
# Combined:  u_c = sqrt(u1² + u2² + u3² + u4² + u5²)
# Expanded:  U   = k × u_c      (k=2 gives 95% confidence level)

import math


# Physical constants / defaults for the MAX31865 + PT100 system
_R_REF_DEFAULT    = 430.0    # Reference resistor (Ω) — overridden by settings
_ADC_BITS         = 15       # MAX31865 15-bit ADC
_PT100_SENSITIVITY = 0.385   # Ω/°C at 0°C (IEC 60751, approx for R0=100Ω)
_RTD_CURRENT_A    = 0.25e-3  # Excitation current in A (typical MAX31865: ~250 µA)
_THERMAL_RES      = 0.15     # RTD thermal resistance (°C/mW), typical for PT100 in still air
_CVD_RESIDUAL     = 0.05     # Fixed ±0.05°C model residual (polynomial approximation error)
_COVERAGE_FACTOR  = 2        # k=2 → 95% confidence interval


class UncertaintyCalculator:
    """
    Computes a full GUM-compliant uncertainty budget for a single calibration point.

    Usage
    -----
    uc = UncertaintyCalculator(r_ref=430.0, u_ref_expanded=0.03, k_ref=2)
    budget = uc.compute(std_dev_measured=0.04, n_samples=60)
    print(budget['expanded_uncertainty_str'])
    """

    def __init__(self, r_ref=_R_REF_DEFAULT, u_ref_expanded=0.05, k_ref=2,
                 nominal_r0=100.0):
        """
        Parameters
        ----------
        r_ref : float
            Reference resistor value on the MAX31865 board (Ω).
        u_ref_expanded : float
            Expanded uncertainty of the reference standard (°C), from its certificate.
        k_ref : int
            Coverage factor from the reference certificate (usually 2).
        nominal_r0 : float
            Nominal resistance of the RTD at 0°C (Ω). Default 100.0 for PT100.
        """
        self.r_ref           = r_ref
        self.u_ref_expanded  = u_ref_expanded
        self.k_ref           = k_ref
        self.nominal_r0      = nominal_r0   # §IF-4: configurable for PT1000 etc.

    # ------------------------------------------------------------------
    # Individual uncertainty components
    # ------------------------------------------------------------------

    def u1_adc_resolution(self):
        """
        Type B — ADC quantisation / resolution.
        Resolution in Ω: one LSB = R_ref / 2^ADC_BITS
        Convert to °C using RTD sensitivity dR/dT ≈ 0.385 Ω/°C at 0°C.
        Rectangular distribution → divisor √3.
        """
        lsb_ohm = self.r_ref / (2 ** _ADC_BITS)
        lsb_deg = lsb_ohm / _PT100_SENSITIVITY
        u = lsb_deg / math.sqrt(3)
        return float(u)

    @staticmethod
    def u2_repeatability(std_dev_measured, n_samples):
        """
        Type A — repeatability from repeated measurements.
        u_rep = σ / √n   (standard error of the mean)

        Parameters
        ----------
        std_dev_measured : float
            Sample standard deviation of the measured series (°C).
        n_samples : int
            Number of samples in the series.
        """
        if n_samples <= 0:
            return 0.0
        return float(std_dev_measured / math.sqrt(max(n_samples, 1)))

    def u3_reference_standard(self):
        """
        Type B — uncertainty of the reference standard.
        u_ref = U_ref / k_ref
        (From the reference instrument's own calibration certificate.)
        """
        return float(self.u_ref_expanded / self.k_ref)

    def u4_self_heating(self, nominal_r0=None):
        """
        Type B — self-heating of the RTD element due to excitation current.
        Self-heating error ≈ I² × R_RTD × θ   (where θ = thermal resistance °C/mW)
        R_RTD = nominal_r0 (Ω) at 0°C.
        Rectangular distribution → divisor √3.
        """
        i        = _RTD_CURRENT_A            # A
        r_rtd    = nominal_r0 if nominal_r0 is not None else self.nominal_r0
        power_mw = (i ** 2) * r_rtd * 1000  # mW
        heat_deg = power_mw * _THERMAL_RES   # °C
        u        = heat_deg / math.sqrt(3)
        return float(u)

    @staticmethod
    def u5_cvd_residual():
        """
        Type B — residual error of the Callendar-Van Dusen polynomial.
        Fixed ±0.05°C contribution (rectangular) as documented in IEC 60751.
        """
        return float(_CVD_RESIDUAL / math.sqrt(3))

    # ------------------------------------------------------------------
    # Combined & Expanded Uncertainty
    # ------------------------------------------------------------------

    def compute(self, std_dev_measured, n_samples):
        """
        Compute the complete GUM uncertainty budget.

        Parameters
        ----------
        std_dev_measured : float
            Sample standard deviation of measured temperatures (°C).
        n_samples : int
            Number of data samples collected.

        Returns
        -------
        dict
            Full budget with individual components, u_c, U, and report string.
        """
        u1 = self.u1_adc_resolution()
        u2 = self.u2_repeatability(std_dev_measured, n_samples)
        u3 = self.u3_reference_standard()
        u4 = self.u4_self_heating()
        u5 = self.u5_cvd_residual()

        u_c = math.sqrt(u1**2 + u2**2 + u3**2 + u4**2 + u5**2)
        U   = _COVERAGE_FACTOR * u_c

        return {
            "u1_adc_resolution":    u1,
            "u2_repeatability":     u2,
            "u3_reference":         u3,
            "u4_self_heating":      u4,
            "u5_cvd_residual":      u5,
            "combined_u_c":         u_c,
            "coverage_factor_k":    _COVERAGE_FACTOR,
            "expanded_uncertainty": U,
            # Ready-to-print string for PDF certificate
            "expanded_uncertainty_str": f"U = ±{U:.4f}°C (k={_COVERAGE_FACTOR}, 95% confidence level)",
            # Individual component strings for budget table in PDF
            "components_table": [
                ["Component", "Symbol", "Type", "Value (deg C)"],
                ["ADC Resolution",       "u1", "B", f"{u1:.5f}"],
                ["Repeatability",        "u2", "A", f"{u2:.5f}"],
                ["Reference Standard",   "u3", "B", f"{u3:.5f}"],
                ["Self-Heating",         "u4", "B", f"{u4:.5f}"],
                ["CVD Model Residual",   "u5", "B", f"{u5:.5f}"],
                ["Combined Std. Unc.",   "u_c", "-", f"{u_c:.5f}"],
                [f"Expanded Unc. (k={_COVERAGE_FACTOR})", "U", "-", f"{U:.5f}"],
            ],
        }
