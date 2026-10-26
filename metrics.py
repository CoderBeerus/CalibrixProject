# metrics.py — CALIBRIX v2
# §1-5: All metric keys lowercase throughout
# §2-11: get_verdict() uses abs(error) + U <= tolerance (uncertainty-augmented)
import math
import numpy as np

# IEC 60751:2022 tolerance limits: Δt = A + B·|t|
ISO_LIMITS = {
    "Class AA": (0.10, 0.0017),
    "Class A":  (0.15, 0.0020),
    "Class B":  (0.30, 0.0050),
    "Class C":  (0.60, 0.0100),
}

# Valid temperature ranges per class (IEC 60751:2022 Table 4)
ISO_RANGES = {
    "Class AA": (-50.0,  250.0),
    "Class A":  (-100.0, 450.0),
    "Class B":  (-196.0, 600.0),
    "Class C":  (-196.0, 600.0),
}


def compute_metrics(measured, standard):
    """
    §1-5: All returned keys are lowercase.
    §1-4: NaN values in inputs are excluded via nanmean/nanstd.
    """
    m = np.asarray(measured, dtype=float)
    s = np.asarray(standard, dtype=float)

    empty = {k: 0.0 for k in [
        "mean_measured", "mean_standard", "std_dev", "cv_percent",
        "mae", "mean_rel_error_percent", "mbe", "rmse",
        "per_point_abs_errors", "per_point_rel_errors_percent",
    ]}
    if m.size == 0:
        return empty

    if m.shape != s.shape:
        raise ValueError("measured and standard must have the same length")

    # §1-4: ignore NaN entries in both arrays
    valid = np.isfinite(m) & np.isfinite(s)
    m_v, s_v = m[valid], s[valid]
    n = m_v.size
    if n == 0:
        return empty

    mean_m  = float(np.mean(m_v))
    mean_s  = float(np.mean(s_v))
    ddof    = 1 if n > 1 else 0
    std_dev = float(np.std(m_v, ddof=ddof))
    cv      = (std_dev / abs(mean_m) * 100) if mean_m != 0 else float("nan")

    abs_err = np.abs(m_v - s_v)
    with np.errstate(divide="ignore", invalid="ignore"):
        rel_err = np.where(s_v != 0, abs_err / np.abs(s_v) * 100, np.nan)

    mae  = float(np.mean(abs_err)) if abs_err.size > 0 else 0.0
    mbe  = float(np.mean(m_v - s_v)) if m_v.size > 0 else 0.0
    rmse = float(np.sqrt(np.mean((m_v - s_v)**2))) if m_v.size > 0 else 0.0
    
    if np.all(np.isnan(rel_err)):
        mre = 0.0
    else:
        mre = float(np.nanmean(rel_err))

    return {
        "mean_measured":            mean_m,
        "mean_standard":            mean_s,
        "std_dev":                  std_dev,
        "cv_percent":               float(cv) if math.isfinite(cv) else 0.0,
        "mae":                      mae,
        "mean_rel_error_percent":   mre,
        "mbe":                      mbe,
        "rmse":                     rmse,
        "per_point_abs_errors":     abs_err.tolist(),
        "per_point_rel_errors_percent": [float(x) if np.isfinite(x) else 0.0 for x in rel_err],
    }


def compute_validation_metrics(measured, expected_setpoint,
                                drift_thresh, sigma_thresh, noise_thresh):
    """§1-5: lowercase keys only."""
    m = np.asarray(measured, dtype=float)
    m = m[np.isfinite(m)]  # §1-4

    if m.size == 0:
        return {"mean": 0.0, "std_dev": 0.0, "drift": 0.0,
                "noise": 0.0, "verdict": "N/A",
                "thresholds": {"drift": drift_thresh, "sigma": sigma_thresh, "noise": noise_thresh}}

    mean_val = float(np.mean(m))
    ddof     = 1 if m.size > 1 else 0
    std_dev  = float(np.std(m, ddof=ddof))
    drift    = mean_val - expected_setpoint
    noise    = float(np.max(m) - np.min(m))

    passed  = (abs(drift) <= drift_thresh and std_dev <= sigma_thresh and noise <= noise_thresh)
    return {
        "mean":    mean_val,
        "std_dev": std_dev,
        "drift":   float(drift),
        "noise":   noise,
        "verdict": "PASS" if passed else "FAIL",
        "thresholds": {"drift": drift_thresh, "sigma": sigma_thresh, "noise": noise_thresh},
    }


def check_class_validity(setpoint_temp, tolerance_class, rtd_wires):
    """
    §2-9: 3-wire + Class A/AA → block.
    Upgrade 5: out-of-range setpoint → block.
    Returns (ok: bool, message: str).
    """
    if rtd_wires == 3 and tolerance_class in ("Class A", "Class AA"):
        return (False,
                f"BLOCKED: 3-wire configuration does not support {tolerance_class}.\n"
                "Switch to 4-wire to proceed.")
    if tolerance_class in ISO_RANGES:
        lo, hi = ISO_RANGES[tolerance_class]
        if not (lo <= setpoint_temp <= hi):
            return (False,
                    f"BLOCKED: {tolerance_class} is not valid at {setpoint_temp:.1f}°C.\n"
                    f"Valid range: {lo:.0f}°C to {hi:.0f}°C.")
    return (True, "")


def get_verdict(measured, standard, tolerance_class="Class A", expanded_uncertainty=0.0):
    """
    §2-11: Per-point PASS/FAIL uses:
        abs(error) + expanded_uncertainty <= A + B*|T_ref|
    expanded_uncertainty defaults to 0 for backward compatibility.
    """
    m = np.asarray(measured, dtype=float)
    s = np.asarray(standard, dtype=float)

    empty = {"verdict": "N/A", "num_failures": 0, "failed_indices": [], "limit_values": []}
    if m.size == 0:
        return empty
    if not np.all(np.isfinite(m)) or not np.all(np.isfinite(s)):
        return {"verdict": "BLOCKED", "num_failures": m.size, "failed_indices": list(range(m.size)), "limit_values": []}
    if tolerance_class not in ISO_LIMITS:
        raise ValueError(f"Unknown tolerance class: {tolerance_class}")

    A, B    = ISO_LIMITS[tolerance_class]
    abs_err = np.abs(m - s)
    limits  = A + B * np.abs(s)

    # §2-11: conservative augmented criterion
    failures = (abs_err + float(expanded_uncertainty)) > limits
    failed   = np.nonzero(failures)[0].tolist()

    return {
        "verdict":        "PASS" if not failures.any() else "FAIL",
        "num_failures":   int(failures.sum()),
        "failed_indices": failed,
        "limit_values":   limits.tolist(),
    }
