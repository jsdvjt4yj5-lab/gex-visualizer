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
    type: str
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
