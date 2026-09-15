"""
Pydantic models mirroring coffee-and-tea-json-schema.md.
Used to validate the reasoning layer's output before it ever reaches the
render layer (dashboard + PDF) — catches a malformed or incomplete
response from the model before it becomes a bad trade suggestion.
"""
from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator


# ---------- Input models ----------

class GexWall(BaseModel):
    strike: float
    gex_usd_m: float
    side: Literal["above", "below"]


class GexData(BaseModel):
    spot: float
    poc: Optional[float] = None
    vah: Optional[float] = None
    val: Optional[float] = None
    ma30: Optional[float] = None
    ma200: Optional[float] = None
    walls: list[GexWall]
    gamma_regime: Literal["uniform_positive", "flip_zone_present"]


class ConvictionTrade(BaseModel):
    strike: float
    type: Literal["C", "P"]
    expiry: str
    score: float
    trade_count: int
    premium_usd_m: float


class FlowData(BaseModel):
    total_trades: int
    total_premium_usd_m: float
    put_pct: float
    call_pct: float
    aggressive_buy_usd_m: float
    aggressive_sell_usd_m: float
    sweep_usd_m: float
    block_usd_m: float
    top_conviction_trades: list[ConvictionTrade] = Field(default_factory=list)


class MacroEvent(BaseModel):
    event: str
    date: str
    time_et: Optional[str] = None
    detail: Optional[str] = None


class ReasoningInput(BaseModel):
    session_date: str
    expiration: str
    gex_data: GexData
    flow_data: Optional[FlowData] = None
    chain_data_available: bool = False
    prior_snapshot: Optional[GexData] = None
    portfolio_size_usd: float
    macro_events_this_window: list[MacroEvent] = Field(default_factory=list)


# ---------- Output models ----------

class KeyLevel(BaseModel):
    strike: float
    type: str = "level"  # default rather than required - this is a
    # cosmetic label, not a risk-defining field, so a missing value
    # shouldn't fail the whole PDF the way a missing max_loss would
    gex_usd_m: Optional[float] = None
    significance: str


class MarketStructure(BaseModel):
    summary: str
    key_levels: list[KeyLevel]


class PerStrategyGuidance(BaseModel):
    strategy_type: str
    guidance: str


class MacroContext(BaseModel):
    key_catalyst: str
    why_it_matters: str
    per_strategy_guidance: list[PerStrategyGuidance] = Field(default_factory=list)


class VolatilityCheck(BaseModel):
    realized_vol_10d_pct: float
    realized_vol_20d_pct: float
    iv_used_pct: float
    iv_source: Literal["placeholder", "live_chain"]
    verdict: Literal["rich", "cheap", "fair"]
    strategy_tilt: str


class WallCrossReference(BaseModel):
    strike: float
    gex_confirms: bool
    detail: str


class StandoutPrint(BaseModel):
    strike: float
    detail: str


class EodFlowContext(BaseModel):
    session_summary: str
    wall_cross_references: list[WallCrossReference] = Field(default_factory=list)
    standout_prints: list[StandoutPrint] = Field(default_factory=list)
    tension_or_alignment_note: str


class BreakScenario(BaseModel):
    condition: str
    target_levels: list[float]


class TradeThesis(BaseModel):
    base_case: str
    upside_break: BreakScenario
    downside_break: BreakScenario


class StrategyLeg(BaseModel):
    action: Literal["buy", "sell"]
    type: Literal["C", "P"]
    strike: float
    expiry: str


class Pricing(BaseModel):
    credit_or_debit: Literal["credit", "debit"]
    amount_per_contract_usd: float
    max_loss_per_contract_usd: float
    max_profit_per_contract_usd: Optional[float] = None  # may be "uncapped" for strangles
    pricing_source: Literal["black_scholes_estimate", "live_chain"]


class Sizing(BaseModel):
    risk_budget_pct: float
    contracts: int
    total_max_loss_usd: float
    total_max_profit_usd: Optional[float] = None


class ProfitTarget(BaseModel):
    trigger_price_usd: float
    total_profit_usd: float
    est_path: str


class StopLoss(BaseModel):
    price_trigger_usd: Optional[float] = None  # null if "monitor manually"
    total_loss_at_stop_usd: Optional[float] = None
    structural_trigger: str


class LiquidityCheck(BaseModel):
    status: Literal["awaiting_live_chain", "green", "yellow", "red"]
    detail: Optional[str] = None


class Strategy(BaseModel):
    name: str
    view: str
    legs: list[StrategyLeg]
    pricing: Pricing
    sizing: Sizing
    profit_target_50pct: ProfitTarget
    stop_loss: StopLoss
    pop_pct: Optional[float] = None  # null for calendars (not solvable)
    entry_trigger: str
    liquidity_check: LiquidityCheck

    @model_validator(mode="after")
    def defined_risk_only(self):
        """
        Enforces the "no naked strikes" rule programmatically rather than
        trusting the model to remember it. A structure with no legs, or a
        missing max-loss figure, is rejected before it ever reaches render.
        """
        if not self.legs:
            raise ValueError(f"{self.name}: no legs defined — refusing to render a strategy with no structure")
        if self.pricing.max_loss_per_contract_usd is None:
            raise ValueError(f"{self.name}: max_loss_per_contract_usd is required — undefined risk is not allowed")
        return self


class PopRankEntry(BaseModel):
    rank: int
    strategy_name: str
    pop_pct: float
    why: str


class SnapshotChange(BaseModel):
    metric: str
    prior: Optional[float] = None
    current: Optional[float] = None
    delta_note: str


class DayOverDayComparison(BaseModel):
    has_prior_snapshot: bool
    changes: list[SnapshotChange] = Field(default_factory=list)


class ReasoningOutput(BaseModel):
    market_structure: MarketStructure
    macro_context: MacroContext
    volatility_check: VolatilityCheck
    eod_flow_context: Optional[EodFlowContext] = None
    trade_thesis: TradeThesis
    strategies: list[Strategy]
    pop_ranking: list[PopRankEntry]
    day_over_day_comparison: Optional[DayOverDayComparison] = None

    @model_validator(mode="after")
    def at_least_one_strategy(self):
        if not self.strategies:
            raise ValueError("reasoning output must include at least one strategy")
        return self


# Explicit rebuild as a safety net for forward-reference resolution -
# these classes use `from __future__ import annotations` (all type hints
# become strings), and pydantic needs the module properly registered to
# resolve them. Vercel's import mechanism should handle this correctly on
# its own, but calling rebuild explicitly here removes any dependency on
# exactly how the entrypoint gets loaded.
ReasoningOutput.model_rebuild()

# ---------- merged from render_pdf_lib.py ----------

"""
Render layer: takes a validated schema.ReasoningOutput (the reasoning
layer's response) plus the session metadata, and produces the same styled
PDF format used throughout the chat sessions — dynamically, from the data,
not hardcoded per session like the original prototype script was.

This is what was missing from the webapp output — the reasoning layer was
producing correct structured data, but nothing was turning it into the
formatted document. This closes that gap.
"""
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                 TableStyle, HRFlowable, PageBreak, Image)
import base64
from io import BytesIO


# ---------- gamma chart image (added: was text/tables only before) ----------

def build_gamma_chart(S, image_b64):
    """
    Embeds the GEX bar chart (captured client-side from the same Chart.js
    canvas the app already renders) into the PDF. Was missing entirely
    before - the styled PDF had tables and narrative text but no visual.
    Scales to fit the page width while preserving the chart's actual
    aspect ratio (which varies with how many strikes are shown).
    """
    if not image_b64:
        return
    try:
        # Strip a data: URL prefix if the frontend sent the full data URI
        # rather than just the base64 payload.
        if ',' in image_b64 and image_b64.strip().startswith('data:'):
            image_b64 = image_b64.split(',', 1)[1]
        image_bytes = base64.b64decode(image_b64)
        img_buffer = BytesIO(image_bytes)

        # Determine the chart's native size to preserve aspect ratio -
        # reportlab's Image needs explicit width/height, it won't infer one
        # from the other.
        from PIL import Image as PILImage
        pil_img = PILImage.open(BytesIO(image_bytes))
        native_w, native_h = pil_img.size

        max_width = 6.3 * inch
        scale = max_width / native_w
        display_w = max_width
        display_h = native_h * scale

        # Cap height too, in case of an unusually tall chart (many strikes) -
        # scale down further if needed rather than letting it overrun the page.
        max_height = 7.5 * inch
        if display_h > max_height:
            scale2 = max_height / display_h
            display_h = max_height
            display_w = display_w * scale2

        S.append(Paragraph("Gamma Exposure Chart", H1))
        S.append(Image(img_buffer, width=display_w, height=display_h))
        S.append(Spacer(1, 8))
    except Exception:
        # If image embedding fails for any reason, skip it rather than
        # breaking PDF generation entirely - the rest of the report still
        # has real value without the chart image.
        pass


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
        parts.append(f"{leg.action.title()} {leg.type} ${leg.strike:g} ({leg.expiry})")
    return " / ".join(parts)


def _strategy_expiry_str(legs) -> str:
    """Distinct expiries across a strategy's legs, in leg order. Usually a
    single date; diagonals/calendars (Protocol Cake) can carry two."""
    expiries = []
    for leg in legs:
        if leg.expiry not in expiries:
            expiries.append(leg.expiry)
    return " / ".join(expiries)


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


def build_standalone_flow_analysis(S, text):
    """
    The separate, full-prose EOD Flow Analysis (from the app's standalone
    Flow Analyst feature, generated from the raw Bullflow Collections CSV)
    - distinct from build_flow_context above, which is Coffee and Tea's
    own condensed cross-reference derived from the same underlying
    flow_data. Both are useful: this one is the fuller independent read,
    the other integrates it directly against the GEX walls. Only appears
    if a standalone flow analysis was actually generated in this session.
    """
    if not text:
        return
    S.append(Paragraph("1d. EOD Flow Analysis (full)", H1))
    for paragraph in text.split("\n\n"):
        if paragraph.strip():
            S.append(Paragraph(paragraph.strip(), BODY))
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
    rows = [[_p("Strategy", CELLB), _p("Expiry", CELLB), _p("Contracts", CELLB), _p("Max Loss", CELLB),
             _p("Max Profit", CELLB), _p("50% Target", CELLB), _p("Stop-Loss", CELLB)]]
    for s in strategies:
        sizing = s.sizing
        pt = s.profit_target_50pct
        sl = s.stop_loss
        rows.append([
            _p(s.name),
            _p(_strategy_expiry_str(s.legs)),
            _p(sizing.contracts),
            _p(_fmt_usd(sizing.total_max_loss_usd)),
            _p(_fmt_usd(sizing.total_max_profit_usd)),
            _p(_fmt_usd(pt.total_profit_usd)),
            _p(_fmt_usd(sl.total_loss_at_stop_usd, allow_none="Monitor manually")),
        ])
    S.append(_table(rows, [0.95*inch, 0.75*inch, 0.55*inch, 0.8*inch, 0.8*inch, 0.75*inch, 1.0*inch]))
    S.append(Spacer(1, 6))
    for s in strategies:
        S.append(Paragraph(
            f"<b>{s.name} stop-loss trigger:</b> {s.stop_loss.structural_trigger}", NOTE))
    S.append(Spacer(1, 4))


def build_pop_ranking(S, pop_ranking, strategies):
    S.append(PageBreak())
    S.append(Paragraph("Appendix C: Probability-of-Profit Ranking", H1))
    expiry_by_name = {s.name: _strategy_expiry_str(s.legs) for s in strategies}
    rows = [[_p("Rank", CELLB), _p("Strategy", CELLB), _p("Expiry", CELLB), _p("POP", CELLB), _p("Why", CELLB)]]
    for entry in sorted(pop_ranking, key=lambda e: e.rank):
        rows.append([
            _p(entry.rank),
            _p(entry.strategy_name),
            _p(expiry_by_name.get(entry.strategy_name, "&mdash;")),
            _p(f"{entry.pop_pct:g}%"),
            _p(entry.why),
        ])
    S.append(_table(rows, [0.4*inch, 1.15*inch, 0.7*inch, 0.5*inch, 3.15*inch]))
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
                  portfolio_size: float, spot: float, out_path: str,
                  gex_chart_image: str = None):
    doc = SimpleDocTemplate(
        out_path, pagesize=letter,
        leftMargin=0.9*inch, rightMargin=0.9*inch,
        topMargin=0.85*inch, bottomMargin=0.8*inch,
    )
    S = []
    build_header(S, session_date, expiration, portfolio_size, spot)
    build_gamma_chart(S, gex_chart_image)
    build_market_structure(S, output.market_structure)
    build_macro_context(S, output.macro_context)
    build_vol_check(S, output.volatility_check)
    build_flow_context(S, output.eod_flow_context)
    build_thesis(S, output.trade_thesis)
    build_strategy_comparison(S, output.strategies)
    build_sizing(S, output.strategies)
    build_pop_ranking(S, output.pop_ranking, output.strategies)
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

# ---------- merged from render-coffee-and-tea-pdf.py (handler) ----------

# Vercel Python serverless function.
# Route: POST /api/render-coffee-and-tea-pdf
# Body: { "output": <ReasoningOutput JSON from /api/coffee-and-tea>,
#          "session_date": "2026-09-15", "expiration": "2026-09-18",
#          "portfolio_size": 30000, "spot": 764.48 }
# Returns: raw PDF bytes (Content-Type: application/pdf), not JSON.
#
# This is the styled render layer from the other chat's project, wired in
# as-is (render_pdf_lib.py + schema.py are unmodified copies) rather than
# reimplemented in jsPDF, to preserve the actual designed layout (colors,
# Appendix A/B/C/D structure, table styling) instead of the simpler
# approximation the client-side jsPDF version produced.
#
# Validates the output through the real pydantic ReasoningOutput schema
# before rendering - a second, more rigorous validation layer on top of
# the lighter JS checks already done in coffee-and-tea.js. A response
# that failed this validation would mean something is wrong with the
# reasoning layer's output shape, not just a rendering hiccup.

from http.server import BaseHTTPRequestHandler
import json
import os
import tempfile

from pydantic import ValidationError


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body_raw = self.rfile.read(content_length)
            body = json.loads(body_raw)

            output_data = body.get('output')
            session_date = body.get('session_date')
            expiration = body.get('expiration')
            portfolio_size = body.get('portfolio_size')
            spot = body.get('spot')
            gex_chart_image = body.get('gex_chart_image')  # optional - PDF renders without a chart if absent

            if not all([output_data, session_date, expiration, portfolio_size, spot]):
                self._send_json_error(400, 'Missing one of: output, session_date, expiration, portfolio_size, spot')
                return

            try:
                output = ReasoningOutput.model_validate(output_data)
            except ValidationError as e:
                self._send_json_error(422, f'Output failed schema validation: {e}')
                return

            tmp_dir = tempfile.mkdtemp()
            out_path = os.path.join(tmp_dir, 'session.pdf')

            generate_pdf(
                output,
                session_date=session_date,
                expiration=expiration,
                portfolio_size=float(portfolio_size),
                spot=float(spot),
                out_path=out_path,
                gex_chart_image=gex_chart_image,
            )

            with open(out_path, 'rb') as f:
                pdf_bytes = f.read()

            self.send_response(200)
            self.send_header('Content-Type', 'application/pdf')
            self.send_header('Content-Disposition', f'attachment; filename="coffeeandteaprotocol_{session_date}.pdf"')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(pdf_bytes)

        except Exception as e:
            self._send_json_error(500, f'Server error: {e}')

    def _send_json_error(self, status, message):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'error': message}).encode())
