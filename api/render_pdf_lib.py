"""
Render layer: takes a validated schema.ReasoningOutput (the reasoning
layer's response) plus the session metadata, and produces the same styled
PDF format used throughout the chat sessions — dynamically, from the data,
not hardcoded per session like the original prototype script was.

This is what was missing from the webapp output — the reasoning layer was
producing correct structured data, but nothing was turning it into the
formatted document. This closes that gap.
"""
from __future__ import annotations
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                 TableStyle, HRFlowable, PageBreak)

from schema import ReasoningOutput

# ---------- shared styles ----------

ss = getSampleStyleSheet()
TITLE = ParagraphStyle('T', parent=ss['Title'], fontSize=19, spaceAfter=4,
                        textColor=colors.HexColor('#14342B'))
SUB = ParagraphStyle('S', parent=ss['Normal'], fontSize=9.5,
                      textColor=colors.HexColor('#666666'), spaceAfter=14)
H1 = ParagraphStyle('H1', parent=ss['Heading1'], fontSize=13.5, spaceBefore=16,
                     spaceAfter=7, textColor=colors.HexColor('#1E7A5F'))
H2 = ParagraphStyle('H2', parent=ss['Heading2'], fontSize=11, spaceBefore=10,
                     spaceAfter=5, textColor=colors.HexColor('#14342B'))
BODY = ParagraphStyle('B', parent=ss['Normal'], fontSize=10, leading=14.5, spaceAfter=7)
BULLET = ParagraphStyle('BU', parent=BODY, leftIndent=16, bulletIndent=5, spaceAfter=4)
CELL = ParagraphStyle('C', parent=ss['Normal'], fontSize=8.5, leading=11.5)
CELLB = ParagraphStyle('CB', parent=CELL, fontName='Helvetica-Bold')
NOTE = ParagraphStyle('N', parent=ss['Normal'], fontSize=8.5, leading=11.5,
                       textColor=colors.HexColor('#777777'))

GREEN = colors.HexColor('#1E7A5F')
ROWBG = colors.HexColor('#F4F8F6')
GRID = colors.HexColor('#CCCCCC')


def _table(data, col_widths, font_size=8.5):
    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), GREEN),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, ROWBG]),
        ('GRID', (0, 0), (-1, -1), 0.4, GRID),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('FONTSIZE', (0, 0), (-1, -1), font_size),
        ('LEFTPADDING', (0, 0), (-1, -1), 4), ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    return t


def _p(text, style=CELL):
    return Paragraph(str(text), style)


def _fmt_usd(x, allow_none="Uncapped"):
    if x is None:
        return allow_none
    return f"${x:,.0f}"


def _legs_str(legs) -> str:
    parts = []
    for leg in legs:
        parts.append(f"{leg.action.title()} {leg.type} ${leg.strike:g}")
    return " / ".join(parts)


# ---------- section builders ----------

def build_header(S, session_date, expiration, portfolio_size, spot):
    S.append(Paragraph("Protocol Coffee and Tea &mdash; SPY", TITLE))
    S.append(Paragraph(
        f"{session_date} &nbsp;&middot;&nbsp; exp {expiration} &nbsp;&middot;&nbsp; "
        f"portfolio ${portfolio_size:,.0f} &nbsp;&middot;&nbsp; spot ${spot:g}", SUB))
    S.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#DDDDDD'), spaceAfter=10))


def build_market_structure(S, ms):
    S.append(Paragraph("1. Market Structure Read", H1))
    S.append(Paragraph(ms.summary, BODY))
    if ms.key_levels:
        rows = [[_p("Level", CELLB), _p("Type", CELLB), _p("GEX Size", CELLB), _p("Significance", CELLB)]]
        for lvl in ms.key_levels:
            gex = f"${lvl.gex_usd_m:.1f}M" if lvl.gex_usd_m is not None else "&mdash;"
            rows.append([_p(f"{lvl.strike:g}"), _p(lvl.type), _p(gex), _p(lvl.significance)])
        S.append(_table(rows, [0.85*inch, 1.3*inch, 0.9*inch, 3.2*inch]))
    S.append(Spacer(1, 4))


def build_macro_context(S, mc):
    S.append(Paragraph("1a. Macro Context", H1))
    S.append(Paragraph(f"<b>{mc.key_catalyst}</b>", BODY))
    S.append(Paragraph(mc.why_it_matters, BODY))
    for g in mc.per_strategy_guidance:
        S.append(Paragraph(f"<b>{g.strategy_type}:</b> {g.guidance}", BULLET, bulletText='\u2022'))
    S.append(Spacer(1, 4))


def build_vol_check(S, vc):
    S.append(Paragraph("1b. Volatility Check", H1))
    S.append(Paragraph(
        f"<b>10-day RV: {vc.realized_vol_10d_pct:g}% &nbsp;&middot;&nbsp; "
        f"20-day RV: {vc.realized_vol_20d_pct:g}% &nbsp;&middot;&nbsp; "
        f"IV used: {vc.iv_used_pct:g}% ({vc.iv_source.replace('_', ' ')})</b>", BODY))
    S.append(Paragraph(f"<b>Verdict: {vc.verdict.upper()}.</b> {vc.strategy_tilt}", BODY))
    S.append(Spacer(1, 4))


def build_flow_context(S, fc):
    if fc is None:
        return
    S.append(Paragraph("1c. EOD Flow Context", H1))
    S.append(Paragraph(fc.session_summary, BODY))
    if fc.wall_cross_references:
        rows = [[_p("Strike", CELLB), _p("GEX Confirms?", CELLB), _p("Detail", CELLB)]]
        for w in fc.wall_cross_references:
            rows.append([_p(f"{w.strike:g}"), _p("Yes" if w.gex_confirms else "No"), _p(w.detail)])
        S.append(_table(rows, [0.7*inch, 1.1*inch, 4.4*inch]))
        S.append(Spacer(1, 6))
    for sp in fc.standout_prints:
        S.append(Paragraph(f"<b>Standout ({sp.strike:g}):</b> {sp.detail}", BODY))
    S.append(Paragraph(fc.tension_or_alignment_note, BODY))
    S.append(Spacer(1, 4))


def build_thesis(S, thesis):
    S.append(Paragraph("2. Trade Thesis", H1))
    S.append(Paragraph(f"<b>Base case:</b> {thesis.base_case}", BULLET, bulletText='\u2022'))
    ub = thesis.upside_break
    S.append(Paragraph(
        f"<b>Upside break:</b> {ub.condition} &rarr; {', '.join(str(t) for t in ub.target_levels)}",
        BULLET, bulletText='\u2022'))
    db = thesis.downside_break
    S.append(Paragraph(
        f"<b>Downside break:</b> {db.condition} &rarr; {', '.join(str(t) for t in db.target_levels)}",
        BULLET, bulletText='\u2022'))
    S.append(Spacer(1, 4))


def build_strategy_comparison(S, strategies):
    S.append(PageBreak())
    S.append(Paragraph("Appendix A: Strategy Comparison", H1))
    rows = [[_p("Strategy", CELLB), _p("Legs", CELLB), _p("Pricing", CELLB), _p("Entry Trigger", CELLB)]]
    for s in strategies:
        price = s.pricing
        price_str = (f"{price.credit_or_debit} ${price.amount_per_contract_usd:g}/ct &mdash; "
                     f"max loss ${price.max_loss_per_contract_usd:g}")
        rows.append([_p(s.name), _p(_legs_str(s.legs)), _p(price_str), _p(s.entry_trigger)])
    S.append(_table(rows, [1.1*inch, 2.0*inch, 1.6*inch, 1.5*inch]))
    S.append(Spacer(1, 4))


def build_sizing(S, strategies):
    S.append(Paragraph("Appendix B: Position Sizing, Profit Targets &amp; Stop-Losses", H1))
    rows = [[_p("Strategy", CELLB), _p("Contracts", CELLB), _p("Max Loss", CELLB),
             _p("Max Profit", CELLB), _p("50% Target", CELLB), _p("Stop-Loss", CELLB)]]
    for s in strategies:
        sizing = s.sizing
        pt = s.profit_target_50pct
        sl = s.stop_loss
        rows.append([
            _p(s.name),
            _p(sizing.contracts),
            _p(_fmt_usd(sizing.total_max_loss_usd)),
            _p(_fmt_usd(sizing.total_max_profit_usd)),
            _p(_fmt_usd(pt.total_profit_usd)),
            _p(_fmt_usd(sl.total_loss_at_stop_usd, allow_none="Monitor manually")),
        ])
    S.append(_table(rows, [1.1*inch, 0.65*inch, 0.85*inch, 0.85*inch, 0.85*inch, 1.1*inch]))
    S.append(Spacer(1, 6))
    for s in strategies:
        S.append(Paragraph(
            f"<b>{s.name} stop-loss trigger:</b> {s.stop_loss.structural_trigger}", NOTE))
    S.append(Spacer(1, 4))


def build_pop_ranking(S, pop_ranking):
    S.append(PageBreak())
    S.append(Paragraph("Appendix C: Probability-of-Profit Ranking", H1))
    rows = [[_p("Rank", CELLB), _p("Strategy", CELLB), _p("POP", CELLB), _p("Why", CELLB)]]
    for entry in sorted(pop_ranking, key=lambda e: e.rank):
        rows.append([_p(entry.rank), _p(entry.strategy_name), _p(f"{entry.pop_pct:g}%"), _p(entry.why)])
    S.append(_table(rows, [0.45*inch, 1.5*inch, 0.6*inch, 3.65*inch]))
    S.append(Spacer(1, 4))


def build_day_over_day(S, dod):
    if dod is None or not dod.has_prior_snapshot:
        return
    S.append(Paragraph("Appendix D: Day-over-Day Comparison", H1))
    if dod.changes:
        rows = [[_p("Metric", CELLB), _p("Prior", CELLB), _p("Current", CELLB), _p("Note", CELLB)]]
        for c in dod.changes:
            rows.append([_p(c.metric), _p(c.prior if c.prior is not None else "&mdash;"),
                         _p(c.current if c.current is not None else "&mdash;"), _p(c.delta_note)])
        S.append(_table(rows, [1.3*inch, 0.8*inch, 0.8*inch, 3.3*inch]))
    S.append(Spacer(1, 4))


def build_footer(S):
    S.append(Spacer(1, 14))
    S.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#DDDDDD'), spaceAfter=8))
    S.append(Paragraph(
        "Session notes generated by Protocol Coffee and Tea. Greeks, credits and sizing figures "
        "are illustrative estimates unless pricing_source is \"live_chain\". Educational material "
        "only &mdash; not financial advice, and not a recommendation to enter any position. "
        "Verify all pricing against your broker before trading.", NOTE))


# ---------- entry point ----------

def generate_pdf(output: ReasoningOutput, session_date: str, expiration: str,
                  portfolio_size: float, spot: float, out_path: str):
    doc = SimpleDocTemplate(
        out_path, pagesize=letter,
        leftMargin=0.9*inch, rightMargin=0.9*inch,
        topMargin=0.85*inch, bottomMargin=0.8*inch,
    )
    S = []
    build_header(S, session_date, expiration, portfolio_size, spot)
    build_market_structure(S, output.market_structure)
    build_macro_context(S, output.macro_context)
    build_vol_check(S, output.volatility_check)
    build_flow_context(S, output.eod_flow_context)
    build_thesis(S, output.trade_thesis)
    build_strategy_comparison(S, output.strategies)
    build_sizing(S, output.strategies)
    build_pop_ranking(S, output.pop_ranking)
    build_day_over_day(S, output.day_over_day_comparison)
    build_footer(S)
    doc.build(S)
    return out_path


if __name__ == "__main__":
    # Manual test — load a saved reasoning_outputs row and render it.
    import sqlite3
    conn = sqlite3.connect("coffee_and_tea.db")
    row = conn.execute(
        "SELECT session_date, output_json FROM reasoning_outputs ORDER BY session_date DESC LIMIT 1"
    ).fetchone()
    if row:
        session_date, output_json = row
        output = ReasoningOutput.model_validate_json(output_json)
        path = generate_pdf(
            output, session_date=session_date, expiration="2026-09-18",
            portfolio_size=30000, spot=output.market_structure.key_levels[0].strike,
            out_path=f"SPY_Coffee_and_Tea_{session_date}.pdf",
        )
        print(f"Wrote {path}")
    else:
        print("No saved reasoning output found — run orchestrator.py first.")
