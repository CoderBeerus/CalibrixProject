# settings_manager.py
# CALIBRIX — Settings upgraded to include:
#   Upgrade 4  : Sensor asset fields (serial, tag, manufacturer, model, R0, interval)
#   Upgrade 7  : Environmental conditions (ambient temp, humidity, pressure)
#   Upgrade 9  : Traceability chain / reference standard documentation
import json
import os


class SettingsManager:
    """Manages all user-configurable settings for CALIBRIX."""

    def __init__(self):
        # --- Core sensor / measurement settings ---
        self.rtd_wires               = 4       # 2, 3, or 4
        self.sampling_interval       = 1.0     # seconds
        self.standard_reference_value = 100.0  # °C
        self.r_ref                   = 430.0   # MAX31865 reference resistor (Ω)
        self.tolerance_class         = "Class A"

        # --- Validation thresholds ---
        self.drift_threshold  = 0.5   # °C
        self.sigma_threshold  = 0.1   # °C
        self.noise_threshold  = 0.3   # °C

        # --- Operator ---
        self.operator_name       = "Default Operator"
        self.operator_photo_path = ""

        # --- Session ---
        self.calibration_duration = 60    # seconds
        self.validation_mode = "Field Validation (On-site Health Check)"

        # --- Output ---
        self.report_output_directory = "CALIBRIX_Reports"

        # --- Multi-point calibration (Upgrade 2) ---
        self.multipoint_setpoints      = [0.0, 50.0, 100.0]  # °C
        self.samples_per_point         = 30
        self.stabilization_window      = 20    # rolling-window size
        self.stabilization_slope_thresh = 0.01  # °C/s
        self.stabilization_sigma_thresh = 0.05  # °C
        self.stabilization_proximity    = 0.5   # °C
        self.stabilization_dwell        = 30.0  # seconds

        # --- Upgrade 4: Sensor Asset Management ---
        self.sensor_serial_number         = ""
        self.sensor_equipment_tag         = ""   # P&ID tag e.g. TT-101
        self.sensor_manufacturer          = ""
        self.sensor_model                 = ""
        self.sensor_nominal_r0            = 100.0  # Ω
        self.calibration_interval_months  = 12

        # --- Upgrade 7: Environmental Conditions ---
        self.ambient_temperature = 23.0   # °C  (IEC 60751 lab: 18–28°C)
        self.relative_humidity   = 50.0   # %   (IEC 60751 lab: <70%)
        self.atmospheric_pressure = 1013.25  # hPa

        # --- Upgrade 9: Traceability Chain ---
        self.ref_standard_name          = ""
        self.ref_serial_number          = ""
        self.ref_calibrating_lab        = ""
        self.ref_lab_accreditation_no   = ""
        self.ref_certificate_number     = ""
        self.ref_calibration_date       = ""
        self.ref_uncertainty_expanded   = 0.05   # °C  U_ref (feeds uncertainty budget u3)
        self.ref_uncertainty_k          = 2      # Coverage factor of reference certificate

        # --- GUM Uncertainty (Upgrade 1) ---
        self.u_ref_expanded = 0.05  # Alias kept in sync with ref_uncertainty_expanded

        # --- Dual RTD Settings ---
        self.dual_ref_serial          = "REF-001"
        self.dual_ref_manufacturer    = ""
        self.dual_ref_model           = ""
        self.dual_ref_r0              = 100.0
        self.dual_ref_cert_no         = ""
        self.dual_ref_cal_date        = ""
        self.dual_dut_serial          = "DUT-001"
        self.dual_dut_tag             = ""
        self.dual_dut_manufacturer    = ""
        self.dual_dut_model           = ""
        self.dual_dut_r0              = 100.0
        self.dual_ref_spi_bus         = 0
        self.dual_ref_spi_device      = 0    # CE0 → /dev/spidev0.0
        self.dual_dut_spi_bus         = 0
        self.dual_dut_spi_device      = 1    # CE1 → /dev/spidev0.1
        self.dual_tolerance_class     = "Class A"
        self.dual_dut_offset          = 0.12   # Mock DUT bias (°C)
        self.dual_dut_noise           = 0.025  # Mock DUT noise σ (°C)

        # --- Internal ---
        self._current_settings_filepath = os.path.join(os.getcwd(), "calibrix_settings.json")
        self.load_settings()

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _get_settings_filepath_for_dir(self, directory: str) -> str:
        return os.path.join(directory, "calibrix_settings.json")

    def load_settings(self):
        """Load settings from JSON file; silently fall back to defaults."""
        if os.path.exists(self._current_settings_filepath):
            path = self._current_settings_filepath
        else:
            path = self._get_settings_filepath_for_dir(os.getcwd())

        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    s = json.load(f)
                self._apply_dict(s)
                print(f"Settings loaded from {path}")
            except json.JSONDecodeError:
                print(f"JSON decode error in {path} — using defaults.")
                self._reset_output_dir()
            except Exception as e:
                print(f"Unexpected error loading settings ({e}) — using defaults.")
                self._reset_output_dir()
        else:
            print(f"Settings file not found at {path} — using defaults.")
            self._reset_output_dir()
            os.makedirs(self.report_output_directory, exist_ok=True)
            self.save_settings()

    def _apply_dict(self, s: dict):
        """Apply a loaded settings dict, falling back to current default for missing keys."""
        g = lambda key, default: s.get(key, default)

        self.rtd_wires                  = g("rtd_wires", self.rtd_wires)
        self.sampling_interval          = g("sampling_interval", self.sampling_interval)
        self.standard_reference_value   = g("standard_reference_value", self.standard_reference_value)
        self.r_ref                      = g("r_ref", self.r_ref)
        self.tolerance_class            = g("tolerance_class", self.tolerance_class)

        self.drift_threshold            = g("drift_threshold", self.drift_threshold)
        self.sigma_threshold            = g("sigma_threshold", self.sigma_threshold)
        self.noise_threshold            = g("noise_threshold", self.noise_threshold)

        self.operator_name              = g("operator_name", self.operator_name)
        self.operator_photo_path        = g("operator_photo_path", self.operator_photo_path)
        self.calibration_duration       = g("calibration_duration", self.calibration_duration)
        self.validation_mode            = g("validation_mode", self.validation_mode)

        self.report_output_directory    = g("report_output_directory", self.report_output_directory)

        # Multi-point
        self.multipoint_setpoints       = g("multipoint_setpoints", self.multipoint_setpoints)
        self.samples_per_point          = g("samples_per_point", self.samples_per_point)
        self.stabilization_window       = g("stabilization_window", self.stabilization_window)
        self.stabilization_slope_thresh = g("stabilization_slope_thresh", self.stabilization_slope_thresh)
        self.stabilization_sigma_thresh = g("stabilization_sigma_thresh", self.stabilization_sigma_thresh)
        self.stabilization_proximity    = g("stabilization_proximity", self.stabilization_proximity)
        self.stabilization_dwell        = g("stabilization_dwell", self.stabilization_dwell)

        # Asset management (Upgrade 4)
        self.sensor_serial_number        = g("sensor_serial_number", self.sensor_serial_number)
        self.sensor_equipment_tag        = g("sensor_equipment_tag", self.sensor_equipment_tag)
        self.sensor_manufacturer         = g("sensor_manufacturer", self.sensor_manufacturer)
        self.sensor_model                = g("sensor_model", self.sensor_model)
        self.sensor_nominal_r0           = g("sensor_nominal_r0", self.sensor_nominal_r0)
        self.calibration_interval_months = g("calibration_interval_months", self.calibration_interval_months)

        # Environmental (Upgrade 7)
        self.ambient_temperature         = g("ambient_temperature", self.ambient_temperature)
        self.relative_humidity           = g("relative_humidity", self.relative_humidity)
        self.atmospheric_pressure        = g("atmospheric_pressure", self.atmospheric_pressure)

        # Traceability (Upgrade 9)
        self.ref_standard_name           = g("ref_standard_name", self.ref_standard_name)
        self.ref_serial_number           = g("ref_serial_number", self.ref_serial_number)
        self.ref_calibrating_lab         = g("ref_calibrating_lab", self.ref_calibrating_lab)
        self.ref_lab_accreditation_no    = g("ref_lab_accreditation_no", self.ref_lab_accreditation_no)
        self.ref_certificate_number      = g("ref_certificate_number", self.ref_certificate_number)
        self.ref_calibration_date        = g("ref_calibration_date", self.ref_calibration_date)
        self.ref_uncertainty_expanded    = g("ref_uncertainty_expanded", self.ref_uncertainty_expanded)
        self.ref_uncertainty_k           = g("ref_uncertainty_k", self.ref_uncertainty_k)
        self.u_ref_expanded              = self.ref_uncertainty_expanded

        # Dual RTD
        self.dual_ref_serial         = g("dual_ref_serial",         self.dual_ref_serial)
        self.dual_ref_manufacturer   = g("dual_ref_manufacturer",   self.dual_ref_manufacturer)
        self.dual_ref_model          = g("dual_ref_model",          self.dual_ref_model)
        self.dual_ref_r0             = g("dual_ref_r0",             self.dual_ref_r0)
        self.dual_ref_cert_no        = g("dual_ref_cert_no",        self.dual_ref_cert_no)
        self.dual_ref_cal_date       = g("dual_ref_cal_date",       self.dual_ref_cal_date)
        self.dual_dut_serial         = g("dual_dut_serial",         self.dual_dut_serial)
        self.dual_dut_tag            = g("dual_dut_tag",            self.dual_dut_tag)
        self.dual_dut_manufacturer   = g("dual_dut_manufacturer",   self.dual_dut_manufacturer)
        self.dual_dut_model          = g("dual_dut_model",          self.dual_dut_model)
        self.dual_dut_r0             = g("dual_dut_r0",             self.dual_dut_r0)
        self.dual_ref_spi_bus        = g("dual_ref_spi_bus",        self.dual_ref_spi_bus)
        self.dual_ref_spi_device     = g("dual_ref_spi_device",     self.dual_ref_spi_device)
        self.dual_dut_spi_bus        = g("dual_dut_spi_bus",        self.dual_dut_spi_bus)
        self.dual_dut_spi_device     = g("dual_dut_spi_device",     self.dual_dut_spi_device)
        self.dual_tolerance_class    = g("dual_tolerance_class",    self.dual_tolerance_class)
        self.dual_dut_offset         = g("dual_dut_offset",         self.dual_dut_offset)
        self.dual_dut_noise          = g("dual_dut_noise",          self.dual_dut_noise)

        self._current_settings_filepath = self._get_settings_filepath_for_dir(self.report_output_directory)

    def _reset_output_dir(self):
        self.report_output_directory    = "CALIBRIX_Reports"
        self._current_settings_filepath = self._get_settings_filepath_for_dir(self.report_output_directory)

    def save_settings(self):
        """Persist current settings to JSON."""
        self.u_ref_expanded = self.ref_uncertainty_expanded  # keep in sync

        s = {
            "rtd_wires":                   self.rtd_wires,
            "sampling_interval":           self.sampling_interval,
            "standard_reference_value":    self.standard_reference_value,
            "r_ref":                       self.r_ref,
            "tolerance_class":             self.tolerance_class,
            "drift_threshold":             self.drift_threshold,
            "sigma_threshold":             self.sigma_threshold,
            "noise_threshold":             self.noise_threshold,
            "operator_name":               self.operator_name,
            "operator_photo_path":         self.operator_photo_path,
            "calibration_duration":        self.calibration_duration,
            "validation_mode":             self.validation_mode,
            "report_output_directory":     self.report_output_directory,
            # Multi-point
            "multipoint_setpoints":        self.multipoint_setpoints,
            "samples_per_point":           self.samples_per_point,
            "stabilization_window":        self.stabilization_window,
            "stabilization_slope_thresh":  self.stabilization_slope_thresh,
            "stabilization_sigma_thresh":  self.stabilization_sigma_thresh,
            "stabilization_proximity":     self.stabilization_proximity,
            "stabilization_dwell":         self.stabilization_dwell,
            # Asset (Upgrade 4)
            "sensor_serial_number":         self.sensor_serial_number,
            "sensor_equipment_tag":         self.sensor_equipment_tag,
            "sensor_manufacturer":          self.sensor_manufacturer,
            "sensor_model":                 self.sensor_model,
            "sensor_nominal_r0":            self.sensor_nominal_r0,
            "calibration_interval_months":  self.calibration_interval_months,
            # Environmental (Upgrade 7)
            "ambient_temperature":          self.ambient_temperature,
            "relative_humidity":            self.relative_humidity,
            "atmospheric_pressure":         self.atmospheric_pressure,
            # Traceability (Upgrade 9)
            "ref_standard_name":            self.ref_standard_name,
            "ref_serial_number":            self.ref_serial_number,
            "ref_calibrating_lab":          self.ref_calibrating_lab,
            "ref_lab_accreditation_no":     self.ref_lab_accreditation_no,
            "ref_certificate_number":       self.ref_certificate_number,
            "ref_calibration_date":         self.ref_calibration_date,
            "ref_uncertainty_expanded":     self.ref_uncertainty_expanded,
            "ref_uncertainty_k":            self.ref_uncertainty_k,
            # Dual RTD
            "dual_ref_serial":              self.dual_ref_serial,
            "dual_ref_manufacturer":        self.dual_ref_manufacturer,
            "dual_ref_model":               self.dual_ref_model,
            "dual_ref_r0":                  self.dual_ref_r0,
            "dual_ref_cert_no":             self.dual_ref_cert_no,
            "dual_ref_cal_date":            self.dual_ref_cal_date,
            "dual_dut_serial":              self.dual_dut_serial,
            "dual_dut_tag":                 self.dual_dut_tag,
            "dual_dut_manufacturer":        self.dual_dut_manufacturer,
            "dual_dut_model":               self.dual_dut_model,
            "dual_dut_r0":                  self.dual_dut_r0,
            "dual_ref_spi_bus":             self.dual_ref_spi_bus,
            "dual_ref_spi_device":          self.dual_ref_spi_device,
            "dual_dut_spi_bus":             self.dual_dut_spi_bus,
            "dual_dut_spi_device":          self.dual_dut_spi_device,
            "dual_tolerance_class":         self.dual_tolerance_class,
            "dual_dut_offset":              self.dual_dut_offset,
            "dual_dut_noise":               self.dual_dut_noise,
        }

        out_dir = self.report_output_directory
        os.makedirs(out_dir, exist_ok=True)
        path = self._get_settings_filepath_for_dir(out_dir)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=4)
            print(f"Settings saved to {path}")
        except Exception as e:
            print(f"Error saving settings: {e}")

    def load_standards_from_csv(self, file_path):
        """Placeholder for future CSV import of standard values."""
        print(f"CSV import not yet implemented: {file_path}")
