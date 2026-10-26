# max31865_driver.py — CALIBRIX v2
# §2-7: Full MAX31865 fault register diagnostics
import spidev
import time
import math

# ── Fault bit masks (MAX31865 datasheet Table 7) ─────────────────────
FAULT_HIGH_THRESHOLD  = 0b10000000   # RTD > high threshold
FAULT_LOW_THRESHOLD   = 0b01000000   # RTD < low threshold
FAULT_REFIN_HIGH      = 0b00100000   # REFIN- > 0.85 × V_BIAS (open circuit)
FAULT_REFIN_LOW       = 0b00010000   # REFIN- < 0.85 × V_BIAS (short to VCC)
FAULT_RTD_LOW         = 0b00001000   # RTD < 0.85 × V_BIAS (short to GND)
FAULT_OVERVOLTAGE     = 0b00000100   # Over/under voltage

FAULT_DESCRIPTIONS = {
    FAULT_HIGH_THRESHOLD: "RTD above high threshold",
    FAULT_LOW_THRESHOLD:  "RTD below low threshold",
    FAULT_REFIN_HIGH:     "Open circuit detected (REFIN- high)",
    FAULT_REFIN_LOW:      "Short to VCC detected (REFIN- low)",
    FAULT_RTD_LOW:        "Short to GND detected",
    FAULT_OVERVOLTAGE:    "Over/under voltage on input",
}

# Faults that require immediate stop
CRITICAL_FAULTS = {FAULT_REFIN_HIGH, FAULT_REFIN_LOW, FAULT_RTD_LOW, FAULT_OVERVOLTAGE}


class SensorFault(Exception):
    """Raised when the MAX31865 reports a hardware fault."""
    def __init__(self, fault_byte: int, descriptions: list[str], is_critical: bool):
        self.fault_byte   = fault_byte
        self.descriptions = descriptions
        self.is_critical  = is_critical
        msg = ("CRITICAL SENSOR FAULT" if is_critical else "Sensor Warning") + ": " + "; ".join(descriptions)
        super().__init__(msg)


def resistance_to_temp_converter(resistance, rtd_nominal=100.0,
                                  a=3.90830e-3, b=-5.77500e-7, c=-4.18300e-12):
    """Inverse CVD: resistance → temperature (dual-branch, §Fix-6: R^5 term present)."""
    r_ratio = resistance / rtd_nominal
    if r_ratio >= 1.0:
        delta = a**2 - 4 * b * (1 - r_ratio)
        if delta < 0:
            return float("nan")
        return (-a + math.sqrt(delta)) / (2 * b)
    else:
        r    = resistance
        # §Fix-6: Full 5th-degree polynomial including R^5 term
        temp = (-242.02 + 2.2228 * r + 2.5859e-3 * r**2
                - 4.8260e-6 * r**3 - 2.8183e-8 * r**4 + 1.5243e-10 * r**5)
        if temp > 0:
            temp = (-a + math.sqrt(a**2 - 4 * b * (1 - r_ratio))) / (2 * b)
        return temp


def temp_to_resistance(temp, rtd_nominal=100.0,
                        a=3.90830e-3, b=-5.77500e-7, c=-4.18300e-12):
    """Forward CVD: temperature → ideal resistance."""
    t = float(temp)
    if t >= 0:
        return rtd_nominal * (1 + a * t + b * t**2)
    else:
        return rtd_nominal * (1 + a * t + b * t**2 + c * (t - 100) * t**3)


class MAX31865:
    """Driver for the MAX31865 RTD-to-Digital Converter with full fault diagnostics."""

    def __init__(self, bus=0, device=0, wires=4, r_ref=430.0):
        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.max_speed_hz = 500000
        self.spi.mode = 0b01
        self.r_ref = r_ref
        self.RTD_NOMINAL = 100.0
        self.A = 3.90830e-3
        self.B = -5.77500e-7
        self.C = -4.18300e-12
        self.configure(wires)

    def configure(self, wires):
        # Bit 7: V_BIAS ON | Bit 6: Auto-convert ON | Bit 1: Fault clear
        # NOTE: Bit 5 (one-shot) must be OFF when auto-convert is ON
        config = 0b11000010   # V_BIAS=1, AUTO=1, ONE_SHOT=0, FAULT_CLR=1
        if wires == 3:
            config |= 0b00010000  # Bit 4: 3-wire compensation
        self._config_base = config & 0b11111100  # Store without fault-clear for later use
        self.spi.xfer2([0x80, config])
        time.sleep(0.1)

    def read_fault_register(self) -> int:
        """Read the 8-bit fault status register (address 0x07)."""
        result = self.spi.xfer2([0x07, 0x00])
        return result[1]

    def clear_faults(self):
        """Send fault clear command while keeping auto-conversion active."""
        self.spi.xfer2([0x80, self._config_base | 0b00000010])  # Set fault-clear bit
        time.sleep(0.05)

    def check_faults(self):
        """
        §2-7: Read and decode fault register.
        Raises SensorFault if any fault bit is set.
        Returns normally if no faults.
        """
        fault_byte = self.read_fault_register()
        if fault_byte == 0:
            return

        descriptions = []
        is_critical  = False
        for mask, desc in FAULT_DESCRIPTIONS.items():
            if fault_byte & mask:
                descriptions.append(desc)
                if mask in CRITICAL_FAULTS:
                    is_critical = True

        if descriptions:
            raise SensorFault(fault_byte, descriptions, is_critical)

    def read_raw_adc(self):
        rtd_data = self.spi.xfer2([0x01, 0x00, 0x00])
        adc_val  = (rtd_data[1] << 8) | rtd_data[2]
        fault    = adc_val & 0x0001
        adc_val >>= 1
        if fault:
            self.check_faults()   # §2-7: decode and raise properly
        return adc_val

    def temp_to_resistance(self, temp):
        return temp_to_resistance(temp, self.RTD_NOMINAL, self.A, self.B, self.C)

    def read_temp(self):
        """Read temperature. Raises SensorFault on hardware fault."""
        self.check_faults()
        raw_val    = self.read_raw_adc()
        resistance = (raw_val / 32768.0) * self.r_ref
        temp       = resistance_to_temp_converter(resistance)
        print(f"[DRIVER READ] temp={temp:.4f}, resistance={resistance:.4f}, raw={raw_val}")
        return temp, resistance

    def close(self):
        self.spi.close()
