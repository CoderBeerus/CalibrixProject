# dual_report_generator.py — CALIBRIX v2
#
# PDF and CSV report for Dual RTD Comparison Calibration.
# Reuses report_generator.py infrastructure (safe_para, safe_cell, _styles, etc.)

import csv
import os
import time
from xml.sax.saxutils import escape as _esc

from reportlab.lib.pagesizes import letter
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image, Table,
    TableStyle, PageBreak, HRFlowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT

from report_generator import safe_para, safe_cell, _styles, _stable, _vtable, _graph, _sig


# ── CSV export ────────────────────────────────────────────────────────

def generate_dual_csv(filename: str, dual_log: list):
    """
    Export raw paired dual samples as CSV.
    dual_log: list of DualSample objects.
    """
    header = [
        "Sample", "Unix Timestamp", "Elapsed Time (s)", "Setpoint (°C)",
        "Ref Temp (°C)", "Ref Resistance (Ω)",
        "DUT Temp (°C)", "DUT Resistance (Ω)",
        "ΔT = DUT - Ref (°C)",
        "Ref Valid", "DUT Valid", "State"
    ]
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        start_t = dual_log[0].timestamp if dual_log else 0.0
        for s in dual_log:
            writer.writerow([
                s.sample_index,
                f"{s.timestamp:.3f}",
                f"{s.timestamp - start_t:.3f}",
                f"{s.setpoint:.3f}",
                f"{s.ref_temperature:.4f}",
                f"{s.ref_resistance:.4f}",
                f"{s.dut_temperature:.4f}",
                f"{s.dut_resistance:.4f}",
                f"{s.delta_temperature:.4f}" if hasattr(s, 'delta_temperature') else "N/A",
                "OK" if s.ref_valid else "FAULT",
                "OK" if s.dut_valid else "FAULT",
                s.calibration_state,
            ])
    print(f"Dual RTD CSV: {filename}")


def generate_dual_correction_csv(filename: str, per_point: list):
    """Export per-point correction table for Dual RTD."""
    header = [
        "Setpoint (°C)", "Direction",
        "As-Found ΔT (°C)", "As-Found U (°C)", "As-Found Verdict",
        "Correction (°C)", "As-Left ΔT (°C)", "As-Left U (°C)",
        "As-Left Verdict", "Tolerance (°C)", "Final Verdict"
    ]
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for r in per_point:
            writer.writerow([
                safe_cell(r.get("setpoint")),
                safe_cell(r.get("direction")),
                safe_cell(r.get("as_found_delta")),
                safe_cell(r.get("as_found_U")),
                safe_cell(r.get("as_found_verdict")),
                safe_cell(r.get("correction")),
                safe_cell(r.get("as_left_delta")),
                safe_cell(r.get("as_left_U")),
                safe_cell(r.get("as_left_verdict")),
                safe_cell(r.get("tolerance")),
                safe_cell(r.get("verdict")),
            ])
    print(f"Dual RTD correction CSV: {filename}")


# ── PDF report ────────────────────────────────────────────────────────

def generate_dual_pdf(filename: str, graph_path: str,
                      metrics: dict, ctx: dict):
    """
    Generate professional Dual RTD Comparison Calibration Report.

    ctx keys (required):
        ref_serial, ref_manufacturer, ref_model, ref_r0,
        ref_cert_no, ref_cal_date, ref_u_expanded, ref_u_k,
        dut_serial, dut_manufacturer, dut_model, dut_r0, dut_tag,
        rtd_wires, tolerance_class, verdict,
        operator_name, certificate_no, calibration_due_date,
        ambient_temperature, relative_humidity, atmospheric_pressure,
        is_demonstration, mode_label
    """
    doc   = SimpleDocTemplate(filename, pagesize=letter,
                              leftMargin=0.75*inch, rightMargin=0.75*inch,
                              topMargin=0.75*inch, bottomMargin=0.75*inch)
    s     = _styles()
    story = []

    # Title
    mode_lbl = ctx.get("mode_label", "Dual RTD Comparison Calibration")
    is_demo  = ctx.get("is_demonstration", False)
    subtitle = ("DEMONSTRATION MODE — ACCELERATED ACQUISITION" if is_demo
                else "Automated Dual-RTD Reference Comparison Workflow")

    story += [
        safe_para("CALIBRIX — Dual RTD Comparison Calibration Report", s["Title2"]),
        safe_para(_esc(subtitle), s["SubTitle2"], escape=False),
        Spacer(1, 0.1*inch),
    ]

    # Certificate header
    cert = ctx.get("certificate_no", f"DUAL-{time.strftime('%Y%m%d%H%M%S')}")
    left = [
        safe_para(f"<b>Certificate:</b> {_esc(str(cert))}", s["MetaL"], escape=False),
        safe_para(f"<b>Date:</b> {time.strftime('%Y-%m-%d %H:%M:%S')}", s["MetaL"], escape=False),
        safe_para(f"<b>Mode:</b> {_esc(mode_lbl)}", s["MetaL"], escape=False),
    ]
    due = ctx.get("calibration_due_date", "")
    if due:
        left.append(safe_para(f"<b>Next Cal. Due:</b> {_esc(str(due))}", s["MetaL"], escape=False))
    if is_demo:
        left.append(safe_para("<b>⚠ DEMONSTRATION MODE — ACCELERATED ACQUISITION</b>",
                              s["MetaL"], escape=False))
    right = [
        safe_para("<b>Operator</b>", s["MetaRH"], escape=False),
        safe_para(f"<b>Name:</b> {_esc(str(ctx.get('operator_name','')))}",
                  s["MetaR"], escape=False),
    ]
    t = Table([[left, right]], colWidths=[4.0*inch, 3.0*inch])
    t.setStyle(TableStyle([
        ("ALIGN", (0,0),(0,0),"LEFT"), ("ALIGN",(1,0),(1,0),"RIGHT"),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("LINEBELOW",(0,0),(-1,0),0.4,colors.HexColor("#cccccc")),
    ]))
    story += [t, Spacer(1, 0.12*inch)]

    # ── Reference RTD Identity ────────────────────────────────────
    story.append(safe_para("Reference RTD Identity (Traceable Reference Standard)", s["Sec"]))
    _stable(story, [
        ["Field", "Value"],
        ["Role",            "REFERENCE (measurement reference)"],
        ["Serial Number",   ctx.get("ref_serial", "-") or "-"],
        ["Manufacturer",    ctx.get("ref_manufacturer", "-") or "-"],
        ["Model",           ctx.get("ref_model", "-") or "-"],
        ["Nominal R0",      f"{ctx.get('ref_r0', 100):.1f} Ohm @ 0°C"],
        ["Certificate No.", ctx.get("ref_cert_no", "-") or "-"],
        ["Calibration Date",ctx.get("ref_cal_date", "-") or "-"],
        ["U_ref",           f"±{ctx.get('ref_u_expanded',0.05):.4f}°C (k={ctx.get('ref_u_k',2)})"],
    ], [2.5*inch, 3.5*inch])
    story.append(Spacer(1, 0.08*inch))

    # ── DUT RTD Identity ──────────────────────────────────────────
    story.append(safe_para("DUT RTD Identity (Device Under Test)", s["Sec"]))
    _stable(story, [
        ["Field", "Value"],
        ["Role",            "DUT (Device Under Test)"],
        ["Serial Number",   ctx.get("dut_serial", "-") or "-"],
        ["Equipment Tag",   ctx.get("dut_tag", "-") or "-"],
        ["Manufacturer",    ctx.get("dut_manufacturer", "-") or "-"],
        ["Model",           ctx.get("dut_model", "-") or "-"],
        ["Nominal R0",      f"{ctx.get('dut_r0', 100):.1f} Ohm @ 0°C"],
    ], [2.5*inch, 3.5*inch])
    story.append(Spacer(1, 0.08*inch))

    # ── Method & Wiring ───────────────────────────────────────────
    story.append(safe_para("Calibration Method & Configuration", s["Sec"]))
    _stable(story, [
        ["Field", "Value"],
        ["Method",          "Automated Dual-RTD Comparison (DUT vs Reference)"],
        ["Primary Metric",  "ΔT = T_DUT − T_Reference"],
        ["Wiring",          f"{ctx.get('rtd_wires',4)}-Wire (both channels)"],
        ["Tolerance Class", ctx.get("tolerance_class", "Class A")],
        ["Compliance",      "IEC 60751:2022 tolerance limits"],
        ["Decision Rule",   "|mean(ΔT)| + U(k=2) ≤ tolerance → PASS"],
        ["Note",            "Software comparison — not an accredited laboratory calibration"],
    ], [2.5*inch, 3.5*inch])
    story.append(Spacer(1, 0.08*inch))

    # ── Per-Point Decision Table ──────────────────────────────────
    per_pt = metrics.get("per_point", [])
    if per_pt:
        story.append(safe_para("Per-Point Calibration Results — As-Found & As-Left", s["Sec"]))
        hdr = ["Point", "Setpoint", "As-Found ΔT", "U(k=2)", "Tolerance", "As-Found", "Correction", "As-Left ΔT", "As-Left"]
        rows = [hdr]
        for r in per_pt:
            rows.append([
                str(r.get("point_index", 0) + 1),
                safe_cell(r.get("setpoint")),
                safe_cell(r.get("as_found_delta")),
                safe_cell(r.get("as_left_U", r.get("as_found_U"))),
                safe_cell(r.get("tolerance")),
                safe_cell(r.get("as_found_verdict")),
                safe_cell(r.get("correction")),
                safe_cell(r.get("as_left_delta")),
                safe_cell(r.get("as_left_verdict")),
            ])
        _vtable(story, rows,
                [0.45*inch, 0.75*inch, 0.9*inch, 0.7*inch, 0.8*inch, 0.8*inch, 0.8*inch, 0.8*inch, 0.8*inch],
                vc=5, fs="FAIL")
        story.append(Spacer(1, 0.08*inch))

    # ── Uncertainty Budget ────────────────────────────────────────
    ub = metrics.get("uncertainty_budget") or (per_pt[0].get("uncertainty_budget") if per_pt else None)
    if ub:
        story.append(safe_para("Measurement Uncertainty Budget — Dual RTD", s["Sec"]))
        comp = ub.get("components_table", [])
        if comp:
            _stable(story, comp, [3.0*inch,1.0*inch,0.6*inch,1.4*inch])
        u_str = ub.get("expanded_uncertainty_str","")
        if u_str:
            story.append(safe_para(_esc(u_str.split("\n")[0]), s["UBold"], escape=False))
        story.append(safe_para(
            "Note: CVD polynomial residual is not included as a physical-reference "
            "uncertainty term in direct comparison calibration.",
            s["NL"]))
        story.append(Spacer(1, 0.08*inch))

    # ── Decision Rule ─────────────────────────────────────────────
    story.append(safe_para("Decision Rule", s["Sub"]))
    story.append(safe_para(
        "PASS iff |mean(ΔT)| + U(k=2) ≤ tolerance, "
        "where ΔT = T_DUT − T_Reference, "
        "and tolerance = A + B·|T_setpoint| per IEC 60751. "
        "Verdict based on As-Left (corrected) ΔT.",
        s["NL"]))
    story.append(Spacer(1, 0.08*inch))

    # ── Overall Verdict ───────────────────────────────────────────
    verdict = metrics.get("overall_verdict", ctx.get("verdict","N/A"))
    cal_status = metrics.get("cal_status","COMPLETE")
    story.append(safe_para("Calibration Verdict", s["Sec"]))
    if cal_status == "INCOMPLETE":
        story.append(safe_para("⚠ CALIBRATION INCOMPLETE — not all points measured",
                               s["Blocked"]))
    if is_demo:
        story.append(safe_para("⚠ DEMONSTRATION MODE — results not for regulatory use",
                               s["Blocked"]))
    story.append(safe_para(f"VERDICT: {verdict}",
                           s["VPass"] if "PASS" in str(verdict) else s["VFail"]))
    story.append(Spacer(1, 0.1*inch))

    # ── Graph ─────────────────────────────────────────────────────
    if graph_path and os.path.exists(graph_path):
        story.append(safe_para("Performance Graph — Temperature & Differential Error vs Time", s["Sec"]))
        _graph(story, graph_path, s)

    # ── Raw data summary ──────────────────────────────────────────
    if per_pt:
        story += [Spacer(1,0.15*inch),
                  safe_para("Per-Point Statistics Summary", s["Sub"])]
        sum_hdr = ["Setpoint","Mean Ref","Mean DUT","Mean ΔT","σ(ΔT)","Max|ΔT|","Samples"]
        sum_rows = [sum_hdr]
        for r in per_pt:
            sum_rows.append([
                safe_cell(r.get("setpoint")),
                safe_cell(r.get("mean_ref")),
                safe_cell(r.get("mean_dut")),
                safe_cell(r.get("mean_delta")),
                safe_cell(r.get("std_delta")),
                safe_cell(r.get("max_abs_error")),
                safe_cell(r.get("n_samples")),
            ])
        _stable(story, sum_rows,
                [0.85*inch,0.9*inch,0.9*inch,0.9*inch,0.75*inch,0.75*inch,0.65*inch])

    _sig(story, s)
    doc.build(story)
    print(f"Dual RTD PDF: {filename}")
