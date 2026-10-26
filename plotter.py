# plotter.py — CALIBRIX v2 Waveform Engine (PyQtGraph)
# §Section 4: Complete replacement of matplotlib with PyQtGraph
#   • Multi-channel: Measured, Standard, Error (secondary axis)
#   • Rolling 1000-point deque buffer — incremental setData() only
#   • Event markers: Cal Start / Stability Achieved / Recording Started
#   • Phase regions: Heating / Stabilizing / Recording
#   • Hover tooltip: Time, M_i, S_i, Error
#   • Toolbar: toggle channels, reset zoom, export snapshot
#   • Tolerance band: semi-transparent fill
#   • No full canvas redraws — pure incremental updates

import collections
import math
import time
import os

import pyqtgraph as pg
import pyqtgraph.exporters
from pyqtgraph import mkPen, mkBrush, InfiniteLine, LinearRegionItem
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QCheckBox, QFileDialog
)
from PyQt5.QtCore import Qt, QPointF
from PyQt5.QtGui import QColor

# Consistent dark theme
pg.setConfigOption("background", "#1e1e1e")
pg.setConfigOption("foreground", "#e0e0e0")
pg.setConfigOption("antialias",  True)

MAX_POINTS = 1000   # Rolling buffer size

# ─── Colour palette ───────────────────────────────────────────────────
C_MEASURED  = "#ff4444"
C_STANDARD  = "#00cc66"
C_ERROR     = "#ffaa00"
C_UPPER_TOL = "#44cc44"
C_LOWER_TOL = "#44cc44"
C_TOL_FILL  = (68, 204, 68, 30)     # RGBA — semi-transparent green
C_HEAT_REG  = (255, 140,  0,  25)   # orange tint
C_STAB_REG  = (255, 255,  0,  25)   # yellow tint
C_REC_REG   = ( 0,  200, 80,  35)   # green tint


class WaveformEngine(QWidget):
    """
    Full-featured PyQtGraph waveform engine for CALIBRIX.
    Drop-in replacement for the old MplCanvas — same public API.
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        # Rolling data buffers
        self._time     = collections.deque(maxlen=MAX_POINTS)
        self._measured = collections.deque(maxlen=MAX_POINTS)
        self._standard = collections.deque(maxlen=MAX_POINTS)
        self._error    = collections.deque(maxlen=MAX_POINTS)
        self._upper    = collections.deque(maxlen=MAX_POINTS)
        self._lower    = collections.deque(maxlen=MAX_POINTS)

        # Visibility toggles
        self._show_measured  = True
        self._show_standard  = True
        self._show_error     = True
        self._show_tolerance = True

        # Phase tracking
        self._phase_regions: list[LinearRegionItem] = []
        self._event_lines:   list[InfiniteLine]     = []
        self._heating_start  = None
        self._stab_start     = None
        self._rec_start      = None

        self._last_t = -float('inf')
        self._build_ui()

    def _is_safe(self, val):
        """Sanitize data: ignore NaN, inf, and subnormal garbage (e.g. 1e-303)."""
        try:
            f = float(val)
            # Filter Nan/Inf and extremely small noise (like 1e-303 memory artifacts)
            if not math.isfinite(f): return False
            if 0 < abs(f) < 1e-25: return False 
            return True
        except (ValueError, TypeError):
            return False

    # ──────────────────────────────────────────────────────────────────
    # UI construction
    # ──────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(2)

        # ── Toolbar ───────────────────────────────────────────────────
        tb = QHBoxLayout()
        tb.setContentsMargins(4, 2, 4, 0)

        self._cb_measured  = self._make_toggle("Measured (RTD)",  C_MEASURED,  self._toggle_measured)
        self._cb_standard  = self._make_toggle("Reference",       C_STANDARD,  self._toggle_standard)
        self._cb_error     = self._make_toggle("Error",     C_ERROR,     self._toggle_error)
        self._cb_tolerance = self._make_toggle("Tolerance", C_UPPER_TOL, self._toggle_tolerance)

        reset_btn  = QPushButton("⟳ Reset Zoom")
        reset_btn.setFixedHeight(22)
        reset_btn.clicked.connect(self._reset_zoom)
        export_btn = QPushButton("📷 Snapshot")
        export_btn.setFixedHeight(22)
        export_btn.clicked.connect(self._export_snapshot)

        for w in [self._cb_measured, self._cb_standard, self._cb_error, self._cb_tolerance,
                  reset_btn, export_btn]:
            tb.addWidget(w)
        tb.addStretch()
        root.addLayout(tb)

        # ── Plot layout ───────────────────────────────────────────────
        self._layout_widget = pg.GraphicsLayoutWidget()
        root.addWidget(self._layout_widget, 1)

        # Main plot — temperature
        self._plot = self._layout_widget.addPlot(row=0, col=0)
        self._plot.setLabel("left",   "Temperature", units="°C",
                            **{"color": "#e0e0e0", "font-size": "10pt"})
        self._plot.setLabel("bottom", "Time",        units="s",
                            **{"color": "#e0e0e0", "font-size": "10pt"})
        self._plot.showGrid(x=True, y=True, alpha=0.25)
        self._plot.addLegend(offset=(10, 10))
        self._plot.setMouseEnabled(x=True, y=True)
        # Initialize auto range to track incoming temperature correctly
        self._plot.enableAutoRange(axis='y', enable=True)

        # Error plot (secondary, smaller)
        self._err_plot = self._layout_widget.addPlot(row=1, col=0)
        self._err_plot.setLabel("left",   "Error", units="°C",
                                **{"color": "#e0e0e0", "font-size": "9pt"})
        self._err_plot.setLabel("bottom", "Time",  units="s",
                                **{"color": "#e0e0e0", "font-size": "9pt"})
        self._err_plot.showGrid(x=True, y=True, alpha=0.2)
        self._err_plot.setMaximumHeight(130)
        self._err_plot.enableAutoRange(axis='y', enable=True)

        # Link X axes
        self._err_plot.setXLink(self._plot)
        self._layout_widget.ci.layout.setRowStretchFactor(0, 3)
        self._layout_widget.ci.layout.setRowStretchFactor(1, 1)

        # Curves
        self._curve_measured = self._plot.plot(
            pen=mkPen(C_MEASURED, width=2), name="Measured (RTD)")
        self._curve_standard = self._plot.plot(
            pen=mkPen(C_STANDARD, width=2.5, style=Qt.DashLine), name="Reference (IEC 60751 Model)")
        self._curve_upper    = self._plot.plot(
            pen=mkPen(C_UPPER_TOL, width=1, style=Qt.DotLine))
        self._curve_lower    = self._plot.plot(
            pen=mkPen(C_LOWER_TOL, width=1, style=Qt.DotLine))
        self._curve_error    = self._err_plot.plot(
            pen=mkPen(C_ERROR, width=1.5), name="Error (°C)")

        # Tolerance fill (FillBetweenItem between upper/lower curves)
        self._tol_fill = pg.FillBetweenItem(
            self._curve_upper, self._curve_lower,
            brush=mkBrush(*C_TOL_FILL)
        )
        self._plot.addItem(self._tol_fill)

        # Hover cross-hair + tooltip
        self._vline = self._plot.addLine(x=0, pen=mkPen("#888888", width=1))
        self._hline = self._plot.addLine(y=0, pen=mkPen("#888888", width=1))
        self._tooltip = pg.TextItem(text="", anchor=(0, 1),
                                    color="#ffffff",
                                    fill=pg.mkBrush(0, 0, 0, 160))
        self._tooltip.setFont(pg.QtGui.QFont("Monospace", 8))
        self._plot.addItem(self._tooltip)
        self._plot.scene().sigMouseMoved.connect(self._on_mouse_move)

    # ──────────────────────────────────────────────────────────────────
    # Public API (mirrors old MplCanvas)
    # ──────────────────────────────────────────────────────────────────

    def clear_plot(self):
        """Clear all data and phase markers for a new session."""
        self._time.clear()
        self._measured.clear()
        self._standard.clear()
        self._error.clear()
        self._upper.clear()
        self._lower.clear()
        self._heating_start = self._stab_start = self._rec_start = None
        self._last_t = -float('inf')

        # Reset axis ranges
        self._plot.enableAutoRange(axis='y', enable=True)
        self._err_plot.enableAutoRange(axis='y', enable=True)

        # Remove phase regions and event lines
        for item in self._phase_regions + self._event_lines:
            try:
                self._plot.removeItem(item)
            except Exception:
                pass
        self._phase_regions.clear()
        self._event_lines.clear()

        self._curve_measured.setData([], [])
        self._curve_standard.setData([], [])
        self._curve_upper.setData([], [])
        self._curve_lower.setData([], [])
        self._curve_error.setData([], [])

    def update_plot(self, time_data, measured_data, standard_data, upper_band, lower_band):
        """
        §Section 4-10: Incremental update — setData() only, no redraw.
        Robustly handles data sanitization and empty buffers.
        """
        n = min(len(time_data), len(measured_data))
        if n == 0:
            return

        # If incoming source is shorter than our processed count, we likely reset or restarted
        if hasattr(self, "_prev_n") and n < self._prev_n:
            self.clear_plot()
        self._prev_n = n

        # Filter and append only new, valid data points
        for i in range(n):
            t = time_data[i]
            if t <= self._last_t:
                continue

            m = measured_data[i]
            # Sanity check: ignore NaN, inf, and 1e-303 noise
            if not (self._is_safe(t) and self._is_safe(m)):
                continue

            s = standard_data[i] if i < len(standard_data) else 0.0
            u = upper_band[i]    if i < len(upper_band)    else m
            lo= lower_band[i]    if i < len(lower_band)    else m
            err = m - s

            self._time.append(t)
            self._measured.append(m)
            self._standard.append(s)
            self._upper.append(u)
            self._lower.append(lo)
            self._error.append(err)
            self._last_t = t

        # Prevent plotting if buffer is still empty or no new valid data
        if not self._time:
            return

        t_arr = list(self._time)
        if self._show_measured:
            self._curve_measured.setData(t_arr, list(self._measured))
        if self._show_standard:
            self._curve_standard.setData(t_arr, list(self._standard))
        if self._show_tolerance:
            self._curve_upper.setData(t_arr, list(self._upper))
            self._curve_lower.setData(t_arr, list(self._lower))
        if self._show_error:
            self._curve_error.setData(t_arr, list(self._error))

    # ── Event markers ─────────────────────────────────────────────────

    def mark_cal_start(self, t: float):
        """§Section 4-4: Vertical line at calibration start."""
        line = InfiniteLine(pos=t, angle=90,
                            pen=mkPen("#00ffff", width=1.5, style=Qt.DashLine),
                            label="Start", labelOpts={"color": "#00ffff", "position": 0.92})
        self._plot.addItem(line)
        self._event_lines.append(line)
        self._heating_start = t

    def mark_stability_achieved(self, t: float):
        """§Section 4-4: Vertical line at stability achieved."""
        line = InfiniteLine(pos=t, angle=90,
                            pen=mkPen("#ffff00", width=1.5, style=Qt.DashLine),
                            label="Stable", labelOpts={"color": "#ffff00", "position": 0.85})
        self._plot.addItem(line)
        self._event_lines.append(line)
        self._stab_start = t

    def mark_recording_started(self, t: float):
        """§Section 4-4: Vertical line at recording start."""
        line = InfiniteLine(pos=t, angle=90,
                            pen=mkPen("#00ff80", width=2, style=Qt.DashLine),
                            label="Recording", labelOpts={"color": "#00ff80", "position": 0.78})
        self._plot.addItem(line)
        self._event_lines.append(line)
        self._rec_start = t

    def add_phase_region(self, t_start: float, t_end: float, phase: str):
        """§Section 4-5: Shaded region for Heating / Stabilizing / Recording."""
        if phase == "heating":
            brush = mkBrush(*C_HEAT_REG)
        elif phase == "stabilizing":
            brush = mkBrush(*C_STAB_REG)
        elif phase == "recording":
            brush = mkBrush(*C_REC_REG)
        else:
            brush = mkBrush(128, 128, 128, 20)

        region = LinearRegionItem(values=[t_start, t_end], brush=brush, movable=False)
        region.setZValue(-10)
        self._plot.addItem(region)
        self._phase_regions.append(region)

    # ── Snapshot export ───────────────────────────────────────────────

    def save_snapshot(self, filepath: str):
        """Save current plot as PNG — used by report generator."""
        exporter = pg.exporters.ImageExporter(self._layout_widget.scene())
        exporter.parameters()["width"] = 1200
        exporter.export(filepath)

    # Compatibility shim: old code calls fig.savefig(path)
    class _FigShim:
        def __init__(self, engine):
            self._engine = engine
        def savefig(self, path):
            self._engine.save_snapshot(path)

    @property
    def fig(self):
        return self._FigShim(self)

    # ──────────────────────────────────────────────────────────────────
    # Toolbar callbacks
    # ──────────────────────────────────────────────────────────────────

    def _make_toggle(self, label, color, slot):
        cb = QCheckBox(label)
        cb.setChecked(True)
        cb.setStyleSheet(f"color: {color}; font-weight: bold; font-size: 10px;")
        cb.toggled.connect(slot)
        return cb

    def _toggle_measured(self, checked):
        self._show_measured = checked
        self._curve_measured.setVisible(checked)

    def _toggle_standard(self, checked):
        self._show_standard = checked
        self._curve_standard.setVisible(checked)

    def _toggle_error(self, checked):
        self._show_error = checked
        self._curve_error.setVisible(checked)

    def _toggle_tolerance(self, checked):
        self._show_tolerance = checked
        self._curve_upper.setVisible(checked)
        self._curve_lower.setVisible(checked)
        self._tol_fill.setVisible(checked)

    def _reset_zoom(self):
        self._plot.enableAutoRange()
        self._err_plot.enableAutoRange()

    def _export_snapshot(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Snapshot", "calibrix_snapshot.png", "PNG (*.png)")
        if path:
            self.save_snapshot(path)

    # ──────────────────────────────────────────────────────────────────
    # Hover tooltip (§Section 4-6)
    # ──────────────────────────────────────────────────────────────────

    def _on_mouse_move(self, pos):
        try:
            if not self._plot.sceneBoundingRect().contains(pos):
                self._tooltip.setVisible(False)
                return

            mp    = self._plot.vb.mapSceneToView(pos)
            x_val = mp.x()

            t_list = list(self._time)
            if not t_list:
                return

            # Find nearest point by time
            idx = min(range(len(t_list)), key=lambda i: abs(t_list[i] - x_val))
            t   = t_list[idx]
            m   = list(self._measured)[idx] if self._measured else 0.0
            s   = list(self._standard)[idx] if self._standard else 0.0
            err = m - s

            self._vline.setValue(t)
            self._hline.setValue(m)

            text = (f" t = {t:.2f} s\n"
                    f" RTD = {m:.4f} °C\n"
                    f" Ref = {s:.4f} °C\n"
                    f" Err = {err:.4f} °C")
            self._tooltip.setText(text)
            self._tooltip.setPos(t, m)
            self._tooltip.setVisible(True)
        except Exception:
            pass
