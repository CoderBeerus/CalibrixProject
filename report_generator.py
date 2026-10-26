# report_generator.py — CALIBRIX v2
# ═══════════════════════════════════════════════════════════════════════
# Phase 3: Full Calibration Decision Report System
#
# Engineering corrections:
#   §1  Decision value column: |error| + U vs tolerance
#   §2  Per-point uncertainty budgets displayed
#   §3  Range validity flags in report
#   §5  As-Found / As-Left comparison with improvement indicator
#   §6  Correction table in PDF + CSV (correction = −MBE)
#   §7  Hysteresis table when bidirectional setpoints used
#   §8  Full uncertainty budget table (u1–u5, u_c, U)
#   §9  Certificate number displayed consistently
#   §10 Data consistency: report reads from single metrics payload
# ═══════════════════════════════════════════════════════════════════════
import csv
import time
import os
from xml.sax.saxutils import escape as _esc   # §BugFix-3: Escape dynamic text for ReportLab

from reportlab.lib.pagesizes import letter
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image, Table,
    TableStyle, PageBreak, HRFlowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None


# ═══════════════════════════════════════════════════════════════════════
# CSV EXPORTS
# ═══════════════════════════════════════════════════════════════════════

def safe_para(text, style, escape=True):
    """
    §BugFix: Hardened sanitization using xml.sax.saxutils.escape.
    Always escapes dynamic content by default to prevent paraparser crashes.
    Use escape=False only for trust-verified static templates with internal tags.
    """
    from xml.sax.saxutils import escape as _esc
    import math

    # 🔥 HARD TYPE GUARD
    if text is None:
        text = "N/A"

    # Prevent recursion objects
    if isinstance(text, (dict, list, tuple)):
        text = str(text)

    # Convert numeric/numpy types safely before str conversion
    try:
        import numpy as np
        if isinstance(text, (np.generic, float, int)):
            if isinstance(text, (float, np.floating)):
                if math.isnan(text) or math.isinf(text):
                    text = "N/A"
                else:
                    text = f"{text:.4f}"
            else:
                text = str(text)
    except Exception:
        pass

    text = str(text)

    # §BugFix: Use escape properly to handle XML metacharacters
    if escape:
        text = _esc(text)

    return Paragraph(text, style)

import math

def safe_cell(x):
    """
    §Fix: Sanitizes data for cells (Table or CSV). 
    Does NOT use XML escape here as it is rendered as literal text in Tables/CSV.
    Ensures no stripping or incorrect entity encoding.
    """
    import math
    if x is None:
        return "N/A"

    try:
        import numpy as np
        if isinstance(x, (np.generic, float, int)):
            if isinstance(x, (float, np.floating)):
                if math.isnan(x) or math.isinf(x):
                    return "N/A"
                return f"{x:.4f}"
            return str(x)
    except Exception:
        pass

    return str(x)


def generate_csv_report(filename, data_log):
    """Export raw data points as CSV."""
    header = ["Time (s)", "Measured Temp (°C)", "Reference Temp (°C)",
              "Resistance (Ω)", "Relative Error (%)"]
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        data = data_log.get_data()
        writer.writerows(zip(data["time"], data["measured"],
                             data["standard"], data["resistance"], data["error"]))
    print(f"CSV report: {filename}")


def generate_correction_table_csv(filename, per_point_results):
    """
    §6: Standalone correction table CSV — importable by SCADA/DCS/PLC.
    correction = standard − measured_mean = −MBE  (strict consistency)
    """
    header = ["Setpoint (°C)", "Direction",
              "As-Found MBE (°C)", "Correction (°C)",
              "As-Left MBE (°C)", "As-Found U (°C)", "As-Left U (°C)",
              "Decision |MBE|+U (°C)", "Tolerance (°C)", "Verdict"]
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for r in per_point_results:
            writer.writerow([
                safe_cell(r.get("setpoint")),
                safe_cell(r.get("direction")),
                safe_cell(r.get("as_found", r.get("mbe"))),
                safe_cell(r.get("correction")),
                safe_cell(r.get("as_left")),
                safe_cell(r.get("as_found_U")),
                safe_cell(r.get("as_left_U")),
                safe_cell(r.get("as_left_decision")),
                safe_cell(r.get("tolerance")),
                safe_cell(r.get("verdict")),
            ])
    print(f"Correction table CSV: {filename}")


# ═══════════════════════════════════════════════════════════════════════
# STYLE SHEET
# ═══════════════════════════════════════════════════════════════════════

def _styles():
    s = getSampleStyleSheet()
    def add(name, **kw):
        if name not in s:
            s.add(ParagraphStyle(name=name, **kw))
    add("Title2",    parent=s["h1"],     alignment=TA_CENTER, fontSize=22,
        spaceAfter=0.06*inch, textColor=colors.HexColor("#002244"), fontName="Helvetica-Bold")
    add("SubTitle2", parent=s["Normal"], alignment=TA_CENTER, fontSize=12,
        spaceAfter=0.25*inch, textColor=colors.HexColor("#336699"))
    add("Sec",       parent=s["h2"],     alignment=TA_LEFT,   fontSize=13,
        spaceBefore=12, spaceAfter=4, textColor=colors.HexColor("#111111"), fontName="Helvetica-Bold")
    add("Sub",       parent=s["h3"],     alignment=TA_LEFT,   fontSize=10,
        spaceBefore=8,  spaceAfter=3, textColor=colors.HexColor("#444444"))
    add("MetaL",     parent=s["Normal"], alignment=TA_LEFT,   fontSize=9, spaceAfter=1)
    add("MetaRH",    parent=s["Normal"], alignment=TA_RIGHT,  fontSize=10, spaceAfter=2,
        fontName="Helvetica-Bold")
    add("MetaR",     parent=s["Normal"], alignment=TA_RIGHT,  fontSize=9, spaceAfter=1)
    add("NC",        parent=s["Normal"], alignment=TA_CENTER)
    add("NL",        parent=s["Normal"], alignment=TA_LEFT)
    add("VPass",     parent=s["h2"],     alignment=TA_CENTER,
        textColor=colors.HexColor("#1a7a1a"), spaceBefore=8, spaceAfter=8, fontName="Helvetica-Bold")
    add("VFail",     parent=s["h2"],     alignment=TA_CENTER,
        textColor=colors.HexColor("#cc0000"), spaceBefore=8, spaceAfter=8, fontName="Helvetica-Bold")
    add("Sig",       parent=s["Normal"], fontSize=10, alignment=TA_CENTER,
        spaceBefore=16, spaceAfter=4, fontName="Helvetica-Bold")
    add("Foot",      parent=s["Normal"], fontSize=7,  alignment=TA_CENTER)
    add("UBold",     parent=s["Normal"], fontSize=10, alignment=TA_CENTER,
        textColor=colors.HexColor("#1a7a1a"), fontName="Helvetica-Bold", spaceBefore=5, spaceAfter=5)
    add("CVD",       parent=s["Normal"], fontSize=7.5, alignment=TA_LEFT,
        fontName="Courier", spaceAfter=2)
    add("Blocked",   parent=s["Normal"], fontSize=9, alignment=TA_LEFT,
        textColor=colors.HexColor("#cc6600"), fontName="Helvetica-Bold")
    return s


# ═══════════════════════════════════════════════════════════════════════
# REPORT DISPATCHER
# ═══════════════════════════════════════════════════════════════════════

class UnifiedReportGenerator:
    @staticmethod
    def generate(mode, filename, graph_path, metrics, log_data, ctx):
        if "Validation" in mode:
            _gen_validation(filename, graph_path, metrics, log_data, ctx)
        elif "Model" in mode or "Calibration" in mode:
            _gen_calibration(filename, graph_path, metrics, log_data, ctx, False)
        elif "Comparison" in mode or "External" in mode:
            _gen_calibration(filename, graph_path, metrics, log_data, ctx, True)
        else:
            _gen_calibration(filename, graph_path, metrics, log_data, ctx, False)


# ═══════════════════════════════════════════════════════════════════════
# VALIDATION REPORT
# ═══════════════════════════════════════════════════════════════════════

def _gen_validation(filename, graph_path, metrics, log_data, ctx):
    doc   = SimpleDocTemplate(filename, pagesize=letter)
    s     = _styles()
    story = []
    story += [safe_para("RTD Field Validation Report", s["Title2"]),
               safe_para("On-Site Sensor Health Check", s["SubTitle2"]),
               Spacer(1, 0.1*inch)]
    _meta(story, s, ctx, "Field Validation")
    _asset(story, s, ctx)
    _traceability(story, s, ctx)
    _env(story, s, ctx)

    story.append(safe_para("Validation Metrics", s["Sec"]))
    th = metrics.get("thresholds", {})
    td, ts, tn = th.get("drift","N/A"), th.get("sigma","N/A"), th.get("noise","N/A")
    # §MN-4: Format threshold values cleanly
    td_s = f"{td:.4f}" if isinstance(td, (int, float)) else str(td)
    ts_s = f"{ts:.4f}" if isinstance(ts, (int, float)) else str(ts)
    tn_s = f"{tn:.4f}" if isinstance(tn, (int, float)) else str(tn)
    rows = [
       ["Metric", "Value", "Limit", "Status"],

       ["Mean Temperature",
        safe_cell(metrics.get("mean")) + " deg C",
        "-", "-"],

       ["Expected Process Value",
        safe_cell(ctx.get("setpoint")) + " deg C",
        "-", "-"],

       ["Drift (Mean - Expected)",
        safe_cell(metrics.get("drift")) + " deg C",
        "+/-" + safe_cell(td) + " deg C",
        "PASS" if isinstance(td,(int,float)) and abs(metrics.get("drift",0) or 0)<=td else "FAIL"],

       ["Stability (std dev)",
        safe_cell(metrics.get("std_dev")) + " deg C",
        "<=" + safe_cell(ts) + " deg C",
        "PASS" if isinstance(ts,(int,float)) and (metrics.get("std_dev") or 0)<=ts else "FAIL"],

       ["Noise (P2P)",
        safe_cell(metrics.get("noise")) + " deg C",
        "<=" + safe_cell(tn) + " deg C",
        "PASS" if isinstance(tn,(int,float)) and (metrics.get("noise") or 0)<=tn else "FAIL"],
    ] 
    _vtable(story, rows, [2.2*inch,1.4*inch,1.3*inch,0.9*inch])

    ub = ctx.get("uncertainty_budget")
    if ub:
        u_str = ub.get("expanded_uncertainty_str","")
        if u_str:
            story.append(Spacer(1,0.1*inch))
            story.append(safe_para(u_str, s["UBold"]))

    verdict = metrics.get("verdict","N/A")
    story += [Spacer(1,0.1*inch), safe_para("Verdict", s["Sec"]),
               safe_para(f"VERDICT: {verdict}", s["VPass"] if verdict=="PASS" else s["VFail"]),
               Spacer(1,0.1*inch)]
    _graph(story, graph_path, s)
    _sig(story, s)
    doc.build(story)
    print(f"Validation PDF: {filename}")


# ═══════════════════════════════════════════════════════════════════════
# CALIBRATION / COMPARISON REPORT
# ═══════════════════════════════════════════════════════════════════════

def _gen_calibration(filename, graph_path, metrics, log_data, ctx, is_ext):
    doc   = SimpleDocTemplate(filename, pagesize=letter)
    s     = _styles()
    story = []
    title    = ("External Reference Comparison" if is_ext
                else "RTD Model-Based Calibration Report (IEC 60751)")
    subtitle = ("Comparison vs Portable Reference" if is_ext
                else "Callendar-Van Dusen Method — IEC 60751:2022")
    story += [safe_para(title, s["Title2"]),
               safe_para(subtitle, s["SubTitle2"]),
               Spacer(1, 0.1*inch)]
    _meta(story, s, ctx, "External" if is_ext else "Model-Based Calibration (IEC 60751)")
    _asset(story, s, ctx)
    _traceability(story, s, ctx)
    _env(story, s, ctx)

    # ── Sensor details ────────────────────────────────────────────
    story.append(safe_para("Sensor Details", s["Sec"]))
    _stable(story, [
        ["Detail","Value"],
        ["Sensor Type","PT100 RTD"],
        ["Wiring",f"{ctx.get('rtd_wires',4)}-Wire"],
        ["Nominal R0",f"{ctx.get('sensor_nominal_r0',100):.1f} Ohm @ 0 deg C"],
        ["Tolerance Class", ctx.get("tolerance_class","Class A")],
        ["Compliance Standard","IEC 60751:2022"],
    ], [2.5*inch,3.5*inch])
    story.append(Spacer(1,0.1*inch))

    # ── CVD polynomial reference ──────────────────────────────────
    story.append(safe_para("CVD Model Reference (IEC 60751)", s["Sec"]))
    story += [
        safe_para("Forward (T->R, T>=0C):  R = R0*(1 + A*T + B*T^2)", s["CVD"]),
        safe_para("Forward (T->R, T<0C):  R = R0*(1 + A*T + B*T^2 + C*(T-100)*T^3)", s["CVD"]),
        safe_para("Inverse (R->T, R>=R0):  T = (-A + sqrt(A^2 - 4B(1-R/R0))) / (2B)", s["CVD"]),
        safe_para("Inverse (R->T, R<R0):  T = -242.02 + 2.2228R + 2.5859e-3*R^2"
                  " - 4.8260e-6*R^3 - 2.8183e-8*R^4 + 1.5243e-10*R^5", s["CVD"]),
        safe_para("Constants: A=3.90830e-3, B=-5.77500e-7, C=-4.18300e-12, R0=100 Ohm", s["CVD"]),
        Spacer(1, 0.08*inch),
    ]

    per_pt = metrics.get("per_point", [])

    # ══════════════════════════════════════════════════════════════
    # §1: PER-POINT DECISION TABLE
    # ══════════════════════════════════════════════════════════════
    if per_pt:
        story.append(safe_para("Per-Point Calibration Decision (GUM + IEC 60751)", s["Sec"]))
        hdr = ["Setpoint","Dir","MBE","U(k=2)","|MBE|+U","Tolerance","Verdict"]
        rows = [hdr]
        for r in per_pt:
            dec  = r.get("as_left_decision", 0)
            tol  = r.get("tolerance", 0)
            verd = r.get("verdict", "N/A")
            rows.append([
                         safe_cell(r.get("setpoint")),
                         safe_cell(r.get("direction", "")[0:3].upper() if r.get("direction") else "-"),
                         safe_cell(r.get("as_left_mbe", r.get("mbe"))),
                         safe_cell(r.get("as_left_U", r.get("uncertainty_U", 0))),
                         safe_cell(dec),
                         safe_cell(tol),
                         safe_cell(verd),
            ])
        _vtable(story, rows,
                [0.8*inch, 0.5*inch, 0.9*inch, 0.9*inch, 0.9*inch, 0.9*inch, 0.7*inch],
                vc=6)

        # Range validity flags (§3)
        blocked = [r for r in per_pt if not r.get("range_valid", True)]
        for b in blocked:
            story.append(safe_para(
                f"⚠ {b.get('range_message', 'Range invalid')} — verdict BLOCKED",
                s["Blocked"], escape=True))

        story.append(Spacer(1, 0.1*inch))

        # ══════════════════════════════════════════════════════════
        # §5: AS-FOUND / AS-LEFT COMPARISON
        # ══════════════════════════════════════════════════════════
        story.append(safe_para("As-Found / As-Left Validation (§5)", s["Sec"]))
        afl_hdr = ["Setpoint", "As-Found MBE", "As-Found |MBE|+U",
                    "Correction", "As-Left MBE", "As-Left |MBE|+U", "Improved?"]
        afl_rows = [afl_hdr]
        for r in per_pt:
            imp = "YES" if r.get("improvement", False) else "NO"
            afl_rows.append([
                safe_cell(r.get("setpoint")),
                safe_cell(r.get("as_found", r.get("mbe"))),
                safe_cell(r.get("as_found_decision")),
                safe_cell(r.get("correction")),
                safe_cell(r.get("as_left")),
                safe_cell(r.get("as_left_decision")),
                imp,
            ])
        _stable(story, afl_rows,
                [0.8*inch, 1.1*inch, 1.1*inch, 0.8*inch, 1.0*inch, 1.1*inch, 0.7*inch])
        story.append(Spacer(1, 0.1*inch))

        # ══════════════════════════════════════════════════════════
        # §6: CORRECTION TABLE
        # ══════════════════════════════════════════════════════════
        story.append(safe_para("Correction Table (correction = \u2212MBE)", s["Sec"]))
        ct_hdr = ["Setpoint", "Measured Mean", "Reference", "Correction"]
        ct_rows = [ct_hdr]
        for r in per_pt:
            ct_rows.append([
                safe_cell(r.get("setpoint")),
                safe_cell(r.get("mean")),
                safe_cell(r.get("setpoint")),
                safe_cell(r.get("correction")),
            ])
        _stable(story, ct_rows, [1.5*inch, 1.5*inch, 1.5*inch, 1.5*inch])
        story.append(Spacer(1, 0.1*inch))

    # ══════════════════════════════════════════════════════════════
    # §7: HYSTERESIS TABLE
    # ══════════════════════════════════════════════════════════════
    hyst = metrics.get("hysteresis", [])
    if hyst:
        story.append(safe_para("Hysteresis Analysis (§7)", s["Sec"]))
        h_hdr = ["Temperature", "Ascending MBE", "Descending MBE", "Hysteresis"]
        h_rows = [h_hdr]
        for h in hyst:
            h_rows.append([
                safe_cell(h.get("temperature")),
                safe_cell(h.get("ascending_mbe")),
                safe_cell(h.get("descending_mbe")),
                safe_cell(h.get("hysteresis")),
            ])
        _stable(story, h_rows, [1.5*inch, 1.5*inch, 1.5*inch, 1.5*inch])
        max_h = metrics.get("max_hysteresis", 0)
        story.append(safe_para(f"Maximum hysteresis: {safe_cell(max_h)} deg C", s["NL"]))
        story.append(Spacer(1, 0.1*inch))

    # ── Overall metrics ───────────────────────────────────────────
    ub    = ctx.get("uncertainty_budget")
    u_str = ub.get("expanded_uncertainty_str","") if ub else ""
    
    if not per_pt:
        story.append(safe_para("Overall Calibration Metrics", s["Sec"]))
        rows = [
            ["Metric","Value"],
            ["Mean Measured Value",    f"{metrics.get('mean_measured',0):.4f} deg C"],
            ["Mean Bias Error (MBE)",  f"{metrics.get('mbe',0):.4f} deg C"],
            ["RMSE",                   f"{metrics.get('rmse',0):.4f} deg C"],
            ["MAE",                    f"{metrics.get('mae',0):.4f} deg C"],
            ["Standard Deviation (s)", f"{metrics.get('std_dev',0):.4f} deg C"],
            ["CV%",                    f"{metrics.get('cv_percent',0):.4f} %"],
            ["Expanded Uncertainty",   u_str if u_str else "-"],
        ]
        _stable(story, rows, [2.8*inch, 3.2*inch])
        story.append(Spacer(1,0.1*inch))

    # ══════════════════════════════════════════════════════════════
    # §8: UNCERTAINTY BUDGET TABLE (full: u1–u5, u_c, U)
    # ══════════════════════════════════════════════════════════════
    if ub:
        story.append(safe_para("Measurement Uncertainty Budget (GUM / JCGM 100:2008)", s["Sec"]))
        comp = ub.get("components_table",[])
        if comp:
            _stable(story, comp, [2.5*inch,0.8*inch,0.7*inch,1.4*inch])
        if u_str:
            story.append(safe_para(u_str, s["UBold"]))
        story.append(Spacer(1,0.1*inch))

    # Per-point uncertainty budgets (§2)
    if per_pt:
        pp_with_ub = [r for r in per_pt if r.get("uncertainty_budget")]
        if pp_with_ub:
            story.append(safe_para("Per-Point Uncertainty Summary", s["Sub"]))
            ub_hdr = ["Setpoint", "u1 (ADC)", "u2 (Rep.)", "u3 (Ref.)",
                       "u4 (Heat)", "u5 (CVD)", "u_c", "U(k=2)"]
            ub_rows = [ub_hdr]
            for r in pp_with_ub:
                b = r["uncertainty_budget"]
                ub_rows.append([
                    safe_cell(r.get("setpoint")),
                    safe_cell(b.get("u1_adc_resolution")),
                    safe_cell(b.get("u2_repeatability")),
                    safe_cell(b.get("u3_reference")),
                    safe_cell(b.get("u4_self_heating")),
                    safe_cell(b.get("u5_cvd_residual")),
                    safe_cell(b.get("combined_u_c")),
                    safe_cell(b.get("expanded_uncertainty")),
                ])
            _stable(story, ub_rows,
                    [0.7*inch, 0.8*inch, 0.8*inch, 0.8*inch,
                     0.8*inch, 0.7*inch, 0.7*inch, 0.8*inch])
            story.append(Spacer(1, 0.1*inch))

    # ── Decision rule explanation ─────────────────────────────────
    story.append(safe_para("Decision Rule", s["Sub"]))
    story.append(safe_para(
        "Per IEC 60751 + GUM/JCGM 100: PASS iff |MBE| + U(k=2) "
        "\u2264 tolerance,  where tolerance = A + B\u00b7|T|. "
        "Verdict is based on As-Left (corrected) measurements.", s["NL"]))
    story.append(Spacer(1, 0.08*inch))

    # ── Verdict ───────────────────────────────────────────────────
    verdict = metrics.get("overall_verdict", ctx.get("verdict","N/A"))
    cal_status = metrics.get("cal_status", "COMPLETE")
    story += [safe_para("Calibration Verdict", s["Sec"])]
    # §BugFix-4: Flag incomplete calibrations
    if cal_status == "INCOMPLETE":
        story.append(safe_para("⚠ CALIBRATION INCOMPLETE — not all points were measured",
                               s["Blocked"]))
    story += [safe_para(f"VERDICT: {verdict}",
                          s["VPass"] if "PASS" in str(verdict) else s["VFail"]),
               Spacer(1,0.1*inch)]

    story.append(PageBreak())
    story.append(safe_para("Performance Graph", s["Sec"]))
    _graph(story, graph_path, s)

    # Last 20 data points
    story += [Spacer(1,0.15*inch), safe_para("Tabular Data (Last 20 Points)", s["Sub"])]
    td = log_data["time"][-20:]
    md = log_data["measured"][-20:]
    sd = log_data["standard"][-20:]
    hd = [["Time (s)","Measured (°C)","Reference (°C)","Dev (°C)"]]
    for i in range(len(td)):
        hd.append([
           safe_cell(td[i]),
           safe_cell(md[i]),
           safe_cell(sd[i]),
           safe_cell(md[i] - sd[i]),
        ])
    _stable(story, hd, [1.5*inch]*4)

    _sig(story, s)
    doc.build(story)
    print(f"Calibration PDF: {filename}")


# ═══════════════════════════════════════════════════════════════════════
# SECTION HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _meta(story, s, ctx, mode):
    cert  = ctx.get("certificate_no", f"CAL-{time.strftime('%Y%m%d%H%M%S')}")
    due   = ctx.get("calibration_due_date","")
    left  = [
        safe_para(f"<b>Certificate:</b> {_esc(str(cert))}",           s["MetaL"], escape=False),
        safe_para(f"<b>Date:</b> {time.strftime('%Y-%m-%d %H:%M:%S')}", s["MetaL"], escape=False),
        safe_para(f"<b>Mode:</b> {_esc(str(mode))}",                  s["MetaL"], escape=False),
    ]
    if due:
        left.append(safe_para(f"<b>Next Cal. Due:</b> {_esc(str(due))}", s["MetaL"], escape=False))
    right = [
        safe_para("<b>Operator</b>",                       s["MetaRH"], escape=False),
        safe_para(f"<b>Name:</b> {_esc(str(ctx.get('operator_name','')))}", s["MetaR"], escape=False),
    ]
    photo = ctx.get("operator_photo_path","")
    if photo and os.path.exists(photo) and PILImage:
        try:
            pil = PILImage.open(photo)
            w, h = pil.size
            mx = 0.9*inch
            r  = min(mx/w, mx/h)
            img = Image(photo, width=w*r, height=h*r)
            img.hAlign = "RIGHT"
            right += [Spacer(1,0.03*inch), img]
        except Exception:
            pass
    t = Table([[left, right]], colWidths=[4.0*inch, 3.0*inch])
    t.setStyle(TableStyle([
        ("ALIGN",(0,0),(0,0),"LEFT"), ("ALIGN",(1,0),(1,0),"RIGHT"),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("LINEBELOW",(0,0),(-1,0),0.4,colors.HexColor("#cccccc")),
    ]))
    story += [t, Spacer(1,0.12*inch)]


def _asset(story, s, ctx):
    serial = ctx.get("sensor_serial_number","")
    tag    = ctx.get("sensor_equipment_tag","")
    if not serial and not tag:
        return
    story.append(safe_para("Instrument Identification (ISO 9001 §7.1.5)", s["Sec"]))
    _stable(story, [
        ["Field","Value"],
        ["Serial Number",      serial or "-"],
        ["Equipment Tag",      tag    or "-"],
        ["Manufacturer",       ctx.get("sensor_manufacturer","") or "-"],
        ["Model",              ctx.get("sensor_model","")        or "-"],
        ["Nominal R0",         f"{ctx.get('sensor_nominal_r0',100):.1f} Ohm"],
        ["Cal. Interval",      f"{ctx.get('calibration_interval_months',12)} months"],
    ], [2.5*inch, 3.5*inch])
    story.append(Spacer(1,0.08*inch))


def _traceability(story, s, ctx):
    name = ctx.get("ref_standard_name","")
    lab  = ctx.get("ref_calibrating_lab","")
    cert = ctx.get("ref_certificate_number","")
    story.append(safe_para("Traceability Chain (ISO/IEC 17025)", s["Sec"]))
    u_r = ctx.get("ref_uncertainty_expanded","")
    k_r = ctx.get("ref_uncertainty_k",2)
    _stable(story, [
        ["Field","Value"],
        ["Reference Type",           "Model-Based (IEC 60751)"],
        ["Traceability Status",      "NOT TRACEABLE (Prototype System)"],
        ["Reference Standard",      name  or "-"],
        ["Serial Number",           ctx.get("ref_serial_number","") or "-"],
        ["Calibrating Lab",         lab   or "-"],
        ["Lab Accreditation No.",   ctx.get("ref_lab_accreditation_no","") or "-"],
        ["Certificate No.",         cert  or "-"],
        ["Cal. Date",               ctx.get("ref_calibration_date","") or "-"],
        ["U_ref",                   f"+/-{u_r} deg C (k={k_r})" if u_r else "-"],
    ], [2.5*inch, 3.5*inch])
    story.append(Spacer(1,0.08*inch))


def _env(story, s, ctx):
    amb  = ctx.get("ambient_temperature")
    hum  = ctx.get("relative_humidity")
    pres = ctx.get("atmospheric_pressure")
    if amb is None and hum is None:
        return
    story.append(safe_para("Environmental Conditions (ISO/IEC 17025 §7.8.2)", s["Sec"]))
    amb_ok = "OK" if isinstance(amb,(int,float)) and 18<=amb<=28 else "WARN"
    hum_ok = "OK" if isinstance(hum,(int,float)) and hum<70 else "WARN"
    _vtable(story, [
        ["Parameter","Value","IEC Limit","Status"],
        ["Ambient Temp",  f"{amb:.1f} deg C"  if amb  is not None else "-", "18-28 deg C", amb_ok],
        ["Humidity",      f"{hum:.1f}%"   if hum  is not None else "-", "<70%",    hum_ok],
        ["Pressure",      f"{pres:.1f} hPa" if pres is not None else "-", "-",      "-"],
    ], [2.2*inch,1.5*inch,1.5*inch,0.6*inch], vc=3, ps="OK", fs="WARN")
    story.append(Spacer(1,0.08*inch))


# ── Table renderers ───────────────────────────────────────────────────

def _stable(story, data, cw=None):
    str_data = [[safe_cell(cell) for cell in row] for row in data]
    t = Table(str_data, colWidths=cw)
    t.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),  colors.HexColor("#3a3a3a")),
        ("TEXTCOLOR",(0,0),(-1,0),   colors.whitesmoke),
        ("FONTNAME",(0,0),(-1,0),    "Helvetica-Bold"),
        ("ALIGN",(0,0),(0,-1),       "LEFT"),
        ("ALIGN",(1,0),(-1,-1),      "LEFT"),
        ("BACKGROUND",(0,1),(-1,-1), colors.HexColor("#f5f5f5")),
        ("GRID",(0,0),(-1,-1),       0.4, colors.black),
        ("FONTSIZE",(0,0),(-1,-1),   8.5),
        ("BOTTOMPADDING",(0,0),(-1,0), 6),
    ]))
    story.append(t)


def _vtable(story, data, cw=None, vc=-1, ps="PASS", fs="FAIL"):
    str_data = [[safe_cell(cell) for cell in row] for row in data]
    t = Table(str_data, colWidths=cw)
    base = [
        ("BACKGROUND",(0,0),(-1,0),  colors.HexColor("#3a3a3a")),
        ("TEXTCOLOR",(0,0),(-1,0),   colors.whitesmoke),
        ("FONTNAME",(0,0),(-1,0),    "Helvetica-Bold"),
        ("ALIGN",(0,0),(-1,-1),      "CENTER"),
        ("ALIGN",(0,0),(0,-1),       "LEFT"),
        ("GRID",(0,0),(-1,-1),       0.4, colors.black),
        ("BACKGROUND",(0,1),(-1,-1), colors.HexColor("#f5f5f5")),
        ("FONTSIZE",(0,0),(-1,-1),   8.5),
        ("BOTTOMPADDING",(0,0),(-1,0), 6),
    ]
    for ri, row in enumerate(str_data[1:], 1):
        cell = row[vc] if vc < len(row) else ""
        if cell == ps:
            base += [("BACKGROUND",(vc,ri),(vc,ri), colors.HexColor("#d4f0d4")),
                     ("TEXTCOLOR",(vc,ri),(vc,ri),  colors.HexColor("#155724"))]
        elif cell == fs:
            base += [("BACKGROUND",(vc,ri),(vc,ri), colors.HexColor("#f8d7da")),
                     ("TEXTCOLOR",(vc,ri),(vc,ri),  colors.HexColor("#721c24"))]
    t.setStyle(TableStyle(base))
    story.append(t)


def _graph(story, path, s):
    if path and os.path.exists(path):
        story.append(Image(path, width=6.3*inch, height=3.8*inch))
    else:
        story.append(safe_para("Graph not available.", s["NL"]))


def _sig(story, s):
    story += [
        Spacer(1, 0.4*inch),
        safe_para("_____________________________", s["Sig"]),
        safe_para("Authorised Technician Signature", s["NC"]),
        Spacer(1, 0.12*inch),
        HRFlowable(width="80%", thickness=0.5, color=colors.grey),
        Spacer(1, 0.04*inch),
        safe_para("<i>Generated by CALIBRIX — RTD Sensor Calibration System v2</i>", s["Foot"], escape=False),
    ]
