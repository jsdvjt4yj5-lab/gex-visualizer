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


class ImpliedMove(BaseModel):
    expected_range_usd: dict[str, float]  # {"low": ..., "high": ...}
    one_sigma_move_usd: float
    one_sigma_move_pct: float
    iv_used_pct: float
    session_date: str
    expiration: str
    method: str


class VolatilityCheck(BaseModel):
    realized_vol_10d_pct: float
    realized_vol_20d_pct: float
    iv_used_pct: float
    iv_source: Literal["tiger_underlying_iv", "user_assumed", "placeholder", "live_chain"]
    verdict: Literal["rich", "cheap", "fair"]
    strategy_tilt: str
    # Optional + defaulted to None so a session run before this field
    # existed (or any response missing it) still validates fine - this
    # never becomes a hard requirement that could break re-rendering an
    # older stored session.
    implied_move: Optional[ImpliedMove] = None


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
    # Optional (v2 PDF layout) - the range the base case expects to hold,
    # e.g. [774, 780]. Defaulted so older stored sessions still validate.
    base_case_levels: list[float] = Field(default_factory=list)
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


class SnapshotChange(BaseModel):
    metric: str
    prior: Optional[float] = None
    current: Optional[float] = None
    delta_note: str


class DayOverDayComparison(BaseModel):
    has_prior_snapshot: bool
    changes: list[SnapshotChange] = Field(default_factory=list)


class AvoidItem(BaseModel):
    structure: str
    reason: str


class SizingRegime(BaseModel):
    regime: Literal["calm", "high_vol", "unknown"]
    realized_vol_20d_pct: Optional[float] = None
    threshold_pct: float
    risk_budget_pct: float
    reason: str


class ReasoningOutput(BaseModel):
    market_structure: MarketStructure
    macro_context: MacroContext
    volatility_check: VolatilityCheck
    eod_flow_context: Optional[EodFlowContext] = None
    trade_thesis: TradeThesis
    strategies: list[Strategy]
    day_over_day_comparison: Optional[DayOverDayComparison] = None
    # Optional (v2 PDF layout) - structures that don't fit today's gamma
    # shape, with why. Defaulted so older stored sessions still validate.
    avoid: list[AvoidItem] = Field(default_factory=list)
    # Server-computed in coffee-and-tea.js (regimeRiskBudget). Optional so
    # sessions stored before regime sizing existed still render.
    sizing_regime: Optional[SizingRegime] = None

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
                                 TableStyle, HRFlowable, PageBreak, Image, KeepTogether)
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
# White bold for header rows - CELLB's default black text was hard to read
# on the green header background (TEXTCOLOR in TableStyle doesn't reach
# into Paragraph cells).
CELLH = ParagraphStyle('CH', parent=CELLB, textColor=colors.white)
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
#
# v2 layout (Sept 2026): mirrors the hand-built chat session PDF rather
# than the earlier "narrative up top, tables in appendices" structure.
# Changes vs v1:
#   - Data-quality table first (IV source, RV, flow, chain, day-over-day
#     staleness, Tiger data_quality warnings) so every caveat is visible
#     before any number is read.
#   - Day-over-day moved up from Appendix C into the main flow.
#   - Sections with no data render an explicit placeholder instead of
#     being silently skipped (EOD flow, RV, liquidity).
#   - Macro events as a table with ET and SGT times.
#   - Thesis as a scenario table.
#   - One strategy table ranked by POP (highest first), with strikes,
#     expiry, entry trigger, price, size, risk, gain and breakeven.
#   - Separate exits table: 50% target / 50%-max-loss stop / structural
#     invalidation (which takes precedence).
#   - Optional "Avoid" section.
# Everything below is display-only: nothing here recomputes pricing,
# sizing or POP - those still come from lib/options-pricing.js via
# coffee-and-tea.js. Breakeven is the one derived number, computed from
# the already-priced net premium and the legs.

from datetime import datetime as _dt
try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _ET = _ZoneInfo("America/New_York")
    _SGT = _ZoneInfo("Asia/Singapore")
except Exception:  # tz database unavailable - SGT column just shows a dash
    _ET = _SGT = None


def _h(num, title):
    return Paragraph(f"{num}. {title}" if num else title, H1)


def _placeholder(S, text):
    S.append(Paragraph(f"<i>Placeholder: {text}</i>", BODY))


def _et_to_sgt(date_str, time_et):
    """'2026-09-24', '08:30' (ET) -> 'Thu 8:30 PM SGT'. Best-effort."""
    if not (_ET and date_str and time_et):
        return "&mdash;"
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p", "%Y-%m-%d %I:%M%p"):
        try:
            et = _dt.strptime(f"{date_str} {time_et.strip()}", fmt).replace(tzinfo=_ET)
            sg = et.astimezone(_SGT)
            return sg.strftime("%a %-I:%M %p SGT")
        except ValueError:
            continue
    return "&mdash;"


def _macro_source_tag(ev):
    """Small grey tag showing where a macro event came from - official
    schedules (FRED/FOMC/Treasury) vs. AI web search - so search-sourced
    items can be weighed accordingly."""
    label = {"fred": "official schedule", "fomc": "Fed calendar",
             "treasury": "Treasury", "search": "web search"}.get(ev.get("source"))
    return f"<br/><font color='#888888' size='7'>{label}</font>" if label else ""


def _fmt_et(date_str, time_et):
    try:
        d = _dt.strptime(date_str, "%Y-%m-%d").strftime("%a %d %b")
    except Exception:
        d = date_str or "&mdash;"
    return f"{d}, {time_et} ET" if time_et else f"{d} (time TBC)"


def _breakevens(strategy):
    """Expiry breakeven(s) from legs + net premium, for the standard
    verticals and iron condors/butterflies this workflow allows. Returns a
    display string, or an em dash for anything else (e.g. a strangle, where
    it's still computable but less standard - kept conservative)."""
    legs = strategy.legs
    prem = strategy.pricing.amount_per_contract_usd / 100.0
    credit = strategy.pricing.credit_or_debit == "credit"
    calls = [l for l in legs if l.type == "C"]
    puts = [l for l in legs if l.type == "P"]
    try:
        if len(legs) == 2 and (len(calls) == 2 or len(puts) == 2):
            short = next(l for l in legs if l.action == "sell")
            long_ = next(l for l in legs if l.action == "buy")
            is_call = legs[0].type == "C"
            if credit:
                be = short.strike + prem if is_call else short.strike - prem
            else:
                be = long_.strike + prem if is_call else long_.strike - prem
            return f"{be:.2f}"
        if len(legs) == 4 and len(calls) == 2 and len(puts) == 2 and credit:
            sc = next(l for l in calls if l.action == "sell")
            sp = next(l for l in puts if l.action == "sell")
            return f"{sp.strike - prem:.2f} / {sc.strike + prem:.2f}"
    except StopIteration:
        pass
    return "&mdash;"


def _legs_short(legs):
    return " / ".join(f"{l.action.title()} {l.strike:g}{l.type}" for l in legs)


def _ranked(strategies):
    # Highest POP first; strategies without a POP (not solvable) go last.
    return sorted(strategies, key=lambda s: (s.pop_pct is None, -(s.pop_pct or 0)))


def build_header(S, session_date, expiration, portfolio_size, spot):
    S.append(Paragraph("Protocol Coffee and Tea &mdash; SPY", TITLE))
    S.append(Paragraph(
        f"{session_date} &nbsp;&middot;&nbsp; exp {expiration} &nbsp;&middot;&nbsp; "
        f"portfolio ${portfolio_size:,.0f} &nbsp;&middot;&nbsp; spot ${spot:g}", SUB))
    S.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#DDDDDD'), spaceAfter=6))


def build_data_quality(S, output, ctx, data_quality):
    """Every caveat in one place, before any number. Built only from what
    the session actually had - never guesses at a status."""
    vc = output.volatility_check
    rows = [[_p("Item", CELLH), _p("Status", CELLH), _p("Impact", CELLH)]]

    iv_label = {
        "live_chain": "Live chain IV",
        "tiger_underlying_iv": "Tiger underlying 30-day IV (flat, no skew)",
        "user_assumed": "User-assumed IV",
        "placeholder": "Hardcoded placeholder IV",
    }.get(vc.iv_source, vc.iv_source)
    iv_impact = ("Pricing reflects the live chain." if vc.iv_source == "live_chain" else
                 "All premiums, breakevens and POPs are Black-Scholes estimates at this IV. "
                 "Re-price off the live chain before entry.")
    rows.append([_p("Implied vol"), _p(f"<b>{vc.iv_used_pct:g}%</b> &mdash; {iv_label}"), _p(iv_impact)])

    if ctx is not None:
        rv_missing = ctx.get("realized_vol_20d_pct") is None
        rows.append([_p("Realized vol (10d/20d)"),
                     _p("Not supplied" if rv_missing else
                        f"{ctx.get('realized_vol_10d_pct')}% / {ctx.get('realized_vol_20d_pct')}%"),
                     _p("RV-vs-IV comparison could not run; sizing defaults to the reduced budget."
                        if rv_missing else "Used for the vol check below.")])
        rows.append([_p("EOD flow"),
                     _p("Supplied" if ctx.get("flow_data") else "Not supplied"),
                     _p("Used in EOD Flow Context." if ctx.get("flow_data") else
                        "EOD Flow Context is a placeholder; fresh vs. stale OI can't be separated.")])
        rows.append([_p("Chain source"),
                     _p("Tiger pull" if ctx.get("chain_data_available") else "Screenshot / manual"),
                     _p("&mdash;" if ctx.get("chain_data_available") else
                        "Walls are only as accurate as the parsed screenshot.")])

    sr = output.sizing_regime
    if sr is not None:
        label = {"calm": "Calm", "high_vol": "<b>High vol</b>", "unknown": "<b>Unknown</b>"}[sr.regime]
        rows.append([_p("Sizing regime"), _p(f"{label} &mdash; {sr.risk_budget_pct:g}% risk per strategy"),
                     _p(sr.reason)])

    live_liq = any(s.liquidity_check.status != "awaiting_live_chain" for s in output.strategies)
    rows.append([_p("Bid/ask"), _p("Checked" if live_liq else "Not available"),
                 _p("See Liquidity section." if live_liq else "Liquidity check is a placeholder.")])

    stale = _prior_staleness_days(ctx)
    if stale is not None and stale > 3:
        rows.append([_p("Prior snapshot"), _p(f"{stale} days old"),
                     _p("Day-over-day is a direction-of-change read, not like-for-like.")])

    if isinstance(data_quality, dict):
        for key, check in data_quality.items():
            if isinstance(check, dict) and check.get("status") == "warn":
                rows.append([_p(f"Tiger: {key.replace('_', ' ')}"), _p("<b>WARN</b>"),
                             _p(check.get("detail") or "&mdash;")])

    S.append(Paragraph("Data quality notes", H1))
    S.append(_table(rows, [1.35*inch, 2.0*inch, 3.35*inch]))
    S.append(Spacer(1, 4))


def _prior_staleness_days(ctx):
    if not ctx:
        return None
    prior = ctx.get("prior_snapshot") or {}
    cap = prior.get("captured_at")
    sd = ctx.get("session_date")
    if not (cap and sd):
        return None
    try:
        cap_date = _dt.fromisoformat(cap.replace("Z", "+00:00"))
        if _ET and cap_date.tzinfo:
            cap_date = cap_date.astimezone(_ET)
        return (_dt.strptime(sd, "%Y-%m-%d").date() - cap_date.date()).days
    except Exception:
        return None


def build_market_structure(S, ms, spot):
    S.append(_h(1, "Market Structure Read"))
    S.append(Paragraph(ms.summary, BODY))
    if ms.key_levels:
        rows = [[_p("Level", CELLH), _p("Type", CELLH), _p("GEX Size", CELLH), _p("Read", CELLH)]]
        levels = sorted(ms.key_levels, key=lambda l: -l.strike)
        spot_row = None
        for lvl in levels:
            if spot_row is None and lvl.strike < spot:
                rows.append([_p(f"<b>{spot:g}</b>"), _p("<b>spot</b>"), _p("&mdash;"), _p("Current price")])
                spot_row = len(rows) - 1
            gex = (("&minus;" if lvl.gex_usd_m < 0 else "") + f"${abs(lvl.gex_usd_m):.1f}M"
                   if lvl.gex_usd_m is not None else "&mdash;")
            rows.append([_p(f"{lvl.strike:g}"), _p(lvl.type.replace('_', ' ')), _p(gex), _p(lvl.significance)])
        t = _table(rows, [0.75*inch, 1.05*inch, 0.95*inch, 3.95*inch])
        if spot_row is not None:
            t.setStyle(TableStyle([('BACKGROUND', (0, spot_row), (-1, spot_row), colors.HexColor('#FFF4D6'))]))
        S.append(t)
    S.append(Paragraph(
        "Walls are zones where dealer hedging tends to slow price, not levels that contain it.", NOTE))
    S.append(Spacer(1, 4))


def build_day_over_day(S, dod, ctx):
    S.append(_h(2, "Day-over-Day Comparison"))
    if dod is None or not dod.has_prior_snapshot:
        _placeholder(S, "no prior snapshot available for comparison.")
        return
    stale = _prior_staleness_days(ctx)
    if stale is not None and stale > 3:
        S.append(Paragraph(
            f"Prior snapshot is <b>{stale} days old</b>; walls may sit on expiries that have since "
            f"rolled off. Treat this as direction of change, not a like-for-like diff.", BODY))
    if dod.changes:
        # Spot/regime and the price anchors (POC/VA/MAs) always show - the
        # spec asks the model to include them regardless of size, and v1's
        # $50M wall filter was silently dropping them. Walls keep v1's
        # rule: >= $50M change, largest 5.
        HEADLINE_METRICS = {"spot", "gamma_regime", "gamma regime", "poc", "vah", "val", "ma30", "ma200"}
        WALL_CHANGE_THRESHOLD_USD_M = 50
        MAX_WALL_ROWS = 5
        headline = [c for c in dod.changes if c.metric.lower() in HEADLINE_METRICS]
        wall_changes = [c for c in dod.changes if c.metric.lower() not in HEADLINE_METRICS]

        def _magnitude(c):
            if c.prior is None or c.current is None:
                return 0
            return abs(c.current - c.prior)

        shown = sorted([c for c in wall_changes if _magnitude(c) >= WALL_CHANGE_THRESHOLD_USD_M],
                       key=_magnitude, reverse=True)[:MAX_WALL_ROWS]
        omitted = len(wall_changes) - len(shown)
        rows = [[_p("Metric", CELLH), _p("Prior", CELLH), _p("Current", CELLH), _p("Change", CELLH)]]
        for c in headline + shown:
            rows.append([_p(c.metric), _p(c.prior if c.prior is not None else "&mdash;"),
                         _p(c.current if c.current is not None else "&mdash;"), _p(c.delta_note)])
        S.append(_table(rows, [1.4*inch, 1.0*inch, 1.0*inch, 3.3*inch]))
        if omitted > 0:
            S.append(Paragraph(f"{omitted} smaller wall change(s) omitted.", NOTE))
    S.append(Spacer(1, 4))


def build_flow_context(S, fc):
    S.append(_h(3, "EOD Flow Context"))
    if fc is None:
        _placeholder(S, "prior-day EOD flow not supplied for this session.")
        return
    S.append(Paragraph(fc.session_summary, BODY))
    if fc.wall_cross_references:
        rows = [[_p("Strike", CELLH), _p("Flow vs GEX", CELLH), _p("Detail", CELLH)]]
        for w in fc.wall_cross_references:
            rows.append([_p(f"{w.strike:g}"), _p("Reinforces" if w.gex_confirms else "Contradicts"), _p(w.detail)])
        S.append(_table(rows, [0.75*inch, 1.1*inch, 4.85*inch]))
        S.append(Spacer(1, 6))
    for sp in fc.standout_prints:
        S.append(Paragraph(f"<b>Standout ({sp.strike:g}):</b> {sp.detail}", BODY))
    S.append(Paragraph(f"<b>Verdict:</b> {fc.tension_or_alignment_note}", BODY))
    S.append(Spacer(1, 4))


def build_macro_context(S, mc, ctx, session_date, expiration):
    S.append(_h(4, f"Macro Context (holding window: {session_date} to {expiration})"))
    S.append(Paragraph(f"<b>{mc.key_catalyst}</b> &mdash; {mc.why_it_matters}", BODY))
    events = (ctx or {}).get("macro_events_this_window") or []
    if events:
        rows = [[_p("When (ET)", CELLH), _p("SGT", CELLH), _p("Event", CELLH), _p("Detail", CELLH)]]
        for ev in sorted(events, key=lambda e: (e.get("date") or "", e.get("time_et") or "99:99")):
            rows.append([_p(_fmt_et(ev.get("date"), ev.get("time_et"))),
                         _p(_et_to_sgt(ev.get("date"), ev.get("time_et"))),
                         _p((ev.get("event") or "&mdash;") + _macro_source_tag(ev)),
                         _p(ev.get("detail") or "&mdash;")])
        S.append(_table(rows, [1.45*inch, 1.15*inch, 1.9*inch, 2.2*inch]))
        S.append(Spacer(1, 6))
    elif ctx is not None:
        S.append(Paragraph("No scheduled macro events found in the holding window.", NOTE))
    for g in mc.per_strategy_guidance:
        S.append(Paragraph(f"<b>{g.strategy_type}:</b> {g.guidance}", BULLET, bulletText='\u2022'))
    S.append(Spacer(1, 4))


def build_vol_check(S, vc, ctx):
    S.append(_h(5, "Realized vs. Implied Vol"))
    rv_missing = ctx is not None and ctx.get("realized_vol_20d_pct") is None
    if rv_missing:
        _placeholder(S, f"realized vol not supplied; IV used {vc.iv_used_pct:g}% "
                        f"({vc.iv_source.replace('_', ' ')}). Verdict below is not data-backed.")
    else:
        S.append(Paragraph(
            f"<b>10d RV {vc.realized_vol_10d_pct:g}% &nbsp;&middot;&nbsp; 20d RV {vc.realized_vol_20d_pct:g}% "
            f"&nbsp;&middot;&nbsp; IV {vc.iv_used_pct:g}% ({vc.iv_source.replace('_', ' ')})</b>", BODY))
    if vc.implied_move:
        im = vc.implied_move
        S.append(Paragraph(
            f"Implied 1&sigma; range to expiry: ${im.expected_range_usd['low']:.2f}&ndash;"
            f"${im.expected_range_usd['high']:.2f} (&plusmn;{im.one_sigma_move_pct:g}%)", BODY))
    S.append(Paragraph(f"<b>Verdict: {vc.verdict.upper()}.</b> {vc.strategy_tilt}", BODY))
    S.append(Spacer(1, 4))


def build_thesis(S, thesis):
    outer, S = S, []  # collected, then appended as one KeepTogether block
    S.append(_h(6, "Thesis"))
    lv = lambda xs: ", ".join(f"{x:g}" for x in xs) if xs else "&mdash;"
    rows = [[_p("Scenario", CELLH), _p("Condition / expected behavior", CELLH), _p("Levels", CELLH)],
            [_p("<b>Base case</b>"), _p(thesis.base_case),
             _p(f"{thesis.base_case_levels[0]:g}&ndash;{thesis.base_case_levels[-1]:g} range"
                if len(thesis.base_case_levels) >= 2 else lv(thesis.base_case_levels))],
            [_p("<b>Upside break</b>"), _p(thesis.upside_break.condition), _p(lv(thesis.upside_break.target_levels))],
            [_p("<b>Downside break</b>"), _p(thesis.downside_break.condition), _p(lv(thesis.downside_break.target_levels))]]
    S.append(_table(rows, [1.15*inch, 4.2*inch, 1.35*inch]))
    S.append(Spacer(1, 4))
    outer.append(KeepTogether(S))


def build_strategies(S, strategies, portfolio_size):
    outer, S = S, []  # collected, then appended as one KeepTogether block
    S.append(_h(7, "Strategies (ranked by POP, highest first)"))
    ranked = _ranked(strategies)
    total_risk = sum(s.sizing.total_max_loss_usd for s in ranked)
    budget = ranked[0].sizing.risk_budget_pct if ranked else 0
    S.append(Paragraph(
        f"Risk budget: {budget:g}% of portfolio (${portfolio_size * budget / 100:,.0f}) per strategy. "
        f"Combined max risk if every strategy were entered: <b>${total_risk:,.0f} "
        f"({100 * total_risk / portfolio_size:.1f}%)</b>. Trigger-based strategies may not all fire.", BODY))
    rows = [[_p(h, CELLH) for h in ("#", "Strategy", "Strikes / expiry", "Entry trigger", "Price",
                                     "Qty", "Max risk", "Max gain", "B/E", "POP")]]
    for i, s in enumerate(ranked, 1):
        pr = s.pricing
        rows.append([
            _p(i), _p(s.name),
            _p(f"{_legs_short(s.legs)}<br/>exp <b>{_strategy_expiry_str(s.legs)}</b>"),
            _p(s.entry_trigger),
            _p(f"${pr.amount_per_contract_usd / 100:.2f} {pr.credit_or_debit}"),
            _p(s.sizing.contracts),
            _p(_fmt_usd(s.sizing.total_max_loss_usd)),
            _p(_fmt_usd(s.sizing.total_max_profit_usd)),
            _p(_breakevens(s)),
            _p(f"{s.pop_pct:.0f}%" if s.pop_pct is not None else "&mdash;"),
        ])
    S.append(_table(rows, [0.25*inch, 0.85*inch, 1.2*inch, 1.3*inch, 0.65*inch,
                           0.35*inch, 0.6*inch, 0.6*inch, 0.5*inch, 0.4*inch], font_size=8))
    if any(s.pricing.pricing_source != "live_chain" for s in ranked):
        S.append(Paragraph("Priced at spot at session time (Black-Scholes estimate); a trigger-level "
                           "entry will price differently.", NOTE))
    S.append(Spacer(1, 4))
    outer.append(KeepTogether(S))


def build_exits(S, strategies):
    outer, S = S, []  # collected, then appended as one KeepTogether block
    S.append(_h(8, "Exits"))
    rows = [[_p("#", CELLH), _p("Profit target (50% rule)", CELLH), _p("Price stop (50% of max loss)", CELLH),
             _p("Structural invalidation (takes precedence)", CELLH)]]
    for i, s in enumerate(_ranked(strategies), 1):
        pt, sl = s.profit_target_50pct, s.stop_loss
        verb_tp = "Buy back" if s.pricing.credit_or_debit == "credit" else "Sell"
        stop = (f"{verb_tp} at ~${sl.price_trigger_usd:.2f} (&minus;{_fmt_usd(sl.total_loss_at_stop_usd)})"
                if sl.price_trigger_usd is not None else "Monitor manually")
        rows.append([_p(i),
                     _p(f"{verb_tp} at ~${pt.trigger_price_usd:.2f} (+{_fmt_usd(pt.total_profit_usd)})"
                        f"<br/><font color='#777777'>{pt.est_path}</font>"),
                     _p(stop), _p(sl.structural_trigger)])
    S.append(_table(rows, [0.25*inch, 2.25*inch, 1.8*inch, 2.4*inch]))
    S.append(Spacer(1, 4))
    outer.append(KeepTogether(S))


def build_liquidity(S, strategies):
    S.append(_h(9, "Liquidity / Bid-Ask Check"))
    ranked = _ranked(strategies)
    if all(s.liquidity_check.status == "awaiting_live_chain" for s in ranked):
        _placeholder(S, "awaiting live chain. Verify relative spread and OI per leg "
                        "(green: &lt;5% and OI &gt;500) and use limit orders at mid.")
        return
    rows = [[_p("#", CELLH), _p("Strategy", CELLH), _p("Status", CELLH), _p("Detail", CELLH)]]
    for i, s in enumerate(ranked, 1):
        rows.append([_p(i), _p(s.name), _p(s.liquidity_check.status.replace('_', ' ').upper()),
                     _p(s.liquidity_check.detail or "&mdash;")])
    S.append(_table(rows, [0.25*inch, 1.6*inch, 1.2*inch, 3.65*inch]))
    S.append(Spacer(1, 4))


def build_avoid(S, avoid):
    if not avoid:
        return
    S.append(_h(10, "Avoid"))
    for a in avoid:
        S.append(Paragraph(f"<b>{a.structure}:</b> {a.reason}", BULLET, bulletText='\u2022'))
    S.append(Spacer(1, 4))


def build_footer(S):
    S.append(Spacer(1, 14))
    S.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#DDDDDD'), spaceAfter=8))
    S.append(Paragraph(
        "Session notes generated by Protocol Coffee and Tea. Credits, sizing and POP are "
        "illustrative estimates unless pricing_source is \"live_chain\". Reasoning-driven, not yet "
        "backtest-validated. Educational material only &mdash; not financial advice. Verify all "
        "pricing against your broker before trading.", NOTE))


# ---------- entry point ----------

def generate_pdf(output: ReasoningOutput, session_date: str, expiration: str,
                  portfolio_size: float, spot: float, out_path: str,
                  input_context: dict | None = None, data_quality: dict | None = None):
    doc = SimpleDocTemplate(
        out_path, pagesize=letter,
        leftMargin=0.9*inch, rightMargin=0.9*inch,
        topMargin=0.85*inch, bottomMargin=0.8*inch,
    )
    S = []
    build_header(S, session_date, expiration, portfolio_size, spot)
    build_data_quality(S, output, input_context, data_quality)
    build_market_structure(S, output.market_structure, spot)
    build_day_over_day(S, output.day_over_day_comparison, input_context)
    build_flow_context(S, output.eod_flow_context)
    build_macro_context(S, output.macro_context, input_context, session_date, expiration)
    build_vol_check(S, output.volatility_check, input_context)
    build_thesis(S, output.trade_thesis)
    build_strategies(S, output.strategies, portfolio_size)
    build_exits(S, output.strategies)
    build_liquidity(S, output.strategies)
    build_avoid(S, output.avoid)
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
            # Optional (v2 layout): the same input object sent to
            # /api/coffee-and-tea, plus the Tiger pull's data_quality block.
            # Display-only (data-quality table, macro event table, day-over-
            # day staleness note, missing-RV flags). Older frontends that
            # don't send them still get a valid PDF; those parts just degrade
            # to what the output alone can show.
            input_context = body.get('input') or None
            data_quality = body.get('data_quality') or None

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
                input_context=input_context,
                data_quality=data_quality,
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
