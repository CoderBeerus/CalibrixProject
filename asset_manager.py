# asset_manager.py
# CALIBRIX — Upgrades 4 & 8
#   Upgrade 4: Sensor Asset Management (ISO 9001 §7.1.5)
#   Upgrade 8: Calibration History Database (ISO 10012)
#
# Uses Python's built-in sqlite3 — zero external dependencies.
# Database file: <report_output_directory>/calibrix_history.db

import sqlite3
import os
import time
from datetime import datetime, date
from dataclasses import dataclass, field
from typing import List, Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SensorAsset:
    """Represents a calibrated instrument (ISO 9001 §7.1.5)."""
    serial_number:      str   = ""
    equipment_tag:      str   = ""          # P&ID tag, e.g. TT-101
    manufacturer:       str   = ""
    model:              str   = ""
    nominal_r0:         float = 100.0       # Ω — not all RTDs are 100Ω
    calibration_interval_months: int = 12   # For due-date calculation
    notes:              str   = ""


@dataclass
class CalibrationRecord:
    """One completed calibration session stored in the database."""
    record_id:        int   = 0             # Auto-assigned by DB
    serial_number:    str   = ""
    cal_date:         str   = ""            # ISO format YYYY-MM-DD
    certificate_no:   str   = ""            # Auto-generated
    mode:             str   = ""
    setpoints:        str   = ""            # JSON-serialised list
    verdict:          str   = "N/A"
    mbe:              float = 0.0
    rmse:             float = 0.0
    mae:              float = 0.0
    std_dev:          float = 0.0
    expanded_uncertainty: float = 0.0
    pdf_path:         str   = ""
    operator_name:    str   = ""
    tolerance_class:  str   = ""
    ambient_temp:     float = 0.0           # Upgrade 7
    humidity:         float = 0.0           # Upgrade 7
    pressure:         float = 0.0           # Upgrade 7


# ---------------------------------------------------------------------------
# Asset Manager — SQLite back-end
# ---------------------------------------------------------------------------

class AssetManager:
    """
    Manages sensor asset registration and calibration history.

    Parameters
    ----------
    db_path : str
        Full path to the SQLite database file.
    """

    def __init__(self, db_path: str = "calibrix_history.db"):
        self.db_path = db_path
        self._ensure_db()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _ensure_db(self):
        """Create tables if they don't exist yet."""
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS instruments (
                    serial_number           TEXT PRIMARY KEY,
                    equipment_tag           TEXT,
                    manufacturer            TEXT,
                    model                   TEXT,
                    nominal_r0              REAL DEFAULT 100.0,
                    cal_interval_months     INTEGER DEFAULT 12,
                    notes                   TEXT
                );

                CREATE TABLE IF NOT EXISTS calibrations (
                    record_id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    serial_number           TEXT,
                    cal_date                TEXT,
                    certificate_no          TEXT,
                    mode                    TEXT,
                    setpoints               TEXT,
                    verdict                 TEXT,
                    mbe                     REAL,
                    rmse                    REAL,
                    mae                     REAL,
                    std_dev                 REAL,
                    expanded_uncertainty    REAL,
                    pdf_path                TEXT,
                    operator_name           TEXT,
                    tolerance_class         TEXT,
                    ambient_temp            REAL DEFAULT 0,
                    humidity                REAL DEFAULT 0,
                    pressure                REAL DEFAULT 0,
                    FOREIGN KEY (serial_number) REFERENCES instruments(serial_number)
                );
            """)

    def _connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.db_path) if os.path.dirname(self.db_path) else ".", exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Instrument CRUD
    # ------------------------------------------------------------------

    def save_instrument(self, asset: SensorAsset):
        """Insert or update a sensor asset record."""
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO instruments
                    (serial_number, equipment_tag, manufacturer, model,
                     nominal_r0, cal_interval_months, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(serial_number) DO UPDATE SET
                    equipment_tag        = excluded.equipment_tag,
                    manufacturer         = excluded.manufacturer,
                    model                = excluded.model,
                    nominal_r0           = excluded.nominal_r0,
                    cal_interval_months  = excluded.cal_interval_months,
                    notes                = excluded.notes
            """, (asset.serial_number, asset.equipment_tag, asset.manufacturer,
                  asset.model, asset.nominal_r0, asset.calibration_interval_months,
                  asset.notes))

    def get_instrument(self, serial_number: str) -> Optional[SensorAsset]:
        """Fetch a sensor asset by serial number."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM instruments WHERE serial_number = ?", (serial_number,)
            ).fetchone()
        if row is None:
            return None
        return SensorAsset(
            serial_number   = row["serial_number"],
            equipment_tag   = row["equipment_tag"],
            manufacturer    = row["manufacturer"],
            model           = row["model"],
            nominal_r0      = row["nominal_r0"],
            calibration_interval_months = row["cal_interval_months"],
            notes           = row["notes"],
        )

    def list_instruments(self) -> List[SensorAsset]:
        """Return all registered instruments."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM instruments ORDER BY serial_number").fetchall()
        return [
            SensorAsset(
                serial_number   = r["serial_number"],
                equipment_tag   = r["equipment_tag"],
                manufacturer    = r["manufacturer"],
                model           = r["model"],
                nominal_r0      = r["nominal_r0"],
                calibration_interval_months = r["cal_interval_months"],
                notes           = r["notes"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Calibration Record CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def generate_certificate_number(serial_number: str) -> str:
        """
        §9: Auto-generate a unique certificate number.
        Format: CAL-YYYYMMDD-HHMMSS-<SERIAL>
        Includes timestamp to guarantee uniqueness for multiple
        calibrations of the same instrument on the same day.
        """
        date_str = time.strftime("%Y%m%d-%H%M%S")
        # Sanitise serial: keep alphanumeric and hyphens only
        safe_serial = "".join(c for c in serial_number if c.isalnum() or c == "-")[:10]
        return f"CAL-{date_str}-{safe_serial}"

    def save_calibration(self, record: CalibrationRecord) -> int:
        """Insert a new calibration record and return its auto-assigned ID."""
        with self._connect() as conn:
            cursor = conn.execute("""
                INSERT INTO calibrations
                    (serial_number, cal_date, certificate_no, mode, setpoints,
                     verdict, mbe, rmse, mae, std_dev, expanded_uncertainty,
                     pdf_path, operator_name, tolerance_class,
                     ambient_temp, humidity, pressure)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (record.serial_number, record.cal_date, record.certificate_no,
                  record.mode, record.setpoints, record.verdict,
                  record.mbe, record.rmse, record.mae, record.std_dev,
                  record.expanded_uncertainty, record.pdf_path,
                  record.operator_name, record.tolerance_class,
                  record.ambient_temp, record.humidity, record.pressure))
            return cursor.lastrowid

    def get_history(self, serial_number: str) -> List[CalibrationRecord]:
        """Fetch all calibration records for a given instrument, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM calibrations WHERE serial_number = ? ORDER BY cal_date DESC",
                (serial_number,)
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_all_records(self) -> List[CalibrationRecord]:
        """Fetch every calibration record (for full history view)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM calibrations ORDER BY cal_date DESC"
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_mbe_trend(self, serial_number: str) -> List[dict]:
        """
        Return MBE trend data for drift tracking (Upgrade 8 trend chart).
        Returns list of {date, mbe} dicts ordered oldest → newest.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT cal_date, mbe FROM calibrations "
                "WHERE serial_number = ? ORDER BY cal_date ASC",
                (serial_number,)
            ).fetchall()
        return [{"date": r["cal_date"], "mbe": r["mbe"]} for r in rows]

    # ------------------------------------------------------------------
    # Due-date / overdue alerts (Upgrade 8)
    # ------------------------------------------------------------------

    def get_overdue_instruments(self) -> List[dict]:
        """
        Return instruments whose last calibration exceeds their interval.
        Returns list of {serial, tag, last_cal_date, due_date, days_overdue}.
        """
        overdue = []
        instruments = self.list_instruments()
        today = date.today()

        for inst in instruments:
            history = self.get_history(inst.serial_number)
            if not history:
                # Never calibrated → immediately overdue
                overdue.append({
                    "serial":       inst.serial_number,
                    "tag":          inst.equipment_tag,
                    "last_cal_date": "Never",
                    "due_date":     "Overdue",
                    "days_overdue": 999,
                })
                continue

            last_cal_str = history[0].cal_date
            try:
                last_cal = datetime.strptime(last_cal_str, "%Y-%m-%d").date()
                # Approximate: months → days (30.44 days/month)
                interval_days = int(inst.calibration_interval_months * 30.44)
                from datetime import timedelta
                due           = last_cal + timedelta(days=interval_days)
                delta         = (today - due).days
                if delta > 0:
                    overdue.append({
                        "serial":        inst.serial_number,
                        "tag":           inst.equipment_tag,
                        "last_cal_date": last_cal_str,
                        "due_date":      due.isoformat(),
                        "days_overdue":  delta,
                    })
            except ValueError:
                pass

        return overdue

    def get_calibration_due_date(self, serial_number: str) -> Optional[str]:
        """
        Compute next calibration due date for a single instrument.
        Returns ISO date string or None if never calibrated.
        """
        instrument = self.get_instrument(serial_number)
        if instrument is None:
            return None
        history = self.get_history(serial_number)
        if not history:
            return None
        last_cal_str = history[0].cal_date
        try:
            from datetime import timedelta
            last_cal = datetime.strptime(last_cal_str, "%Y-%m-%d").date()
            interval_days = int(instrument.calibration_interval_months * 30.44)
            due = last_cal + timedelta(days=interval_days)
            return due.isoformat()
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_record(row) -> CalibrationRecord:
        return CalibrationRecord(
            record_id             = row["record_id"],
            serial_number         = row["serial_number"],
            cal_date              = row["cal_date"],
            certificate_no        = row["certificate_no"],
            mode                  = row["mode"],
            setpoints             = row["setpoints"],
            verdict               = row["verdict"],
            mbe                   = row["mbe"],
            rmse                  = row["rmse"],
            mae                   = row["mae"],
            std_dev               = row["std_dev"],
            expanded_uncertainty  = row["expanded_uncertainty"],
            pdf_path              = row["pdf_path"],
            operator_name         = row["operator_name"],
            tolerance_class       = row["tolerance_class"],
            ambient_temp          = row["ambient_temp"],
            humidity              = row["humidity"],
            pressure              = row["pressure"],
        )
