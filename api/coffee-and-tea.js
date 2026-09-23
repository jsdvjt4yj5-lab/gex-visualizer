// Route: POST /api/coffee-and-tea
// Body: ReasoningInput shape (see coffee-and-tea-json-schema.md):
//   { session_date, expiration, gex_data, flow_data, chain_data_available,
//     prior_snapshot, portfolio_size_usd, macro_events_this_window }
//
// The full workflow spec is embedded directly below rather than loaded
// from a separate file (Vercel functions don't have a persistent
// filesystem to read a bundled doc from reliably across deploys). This
// combines protocol-coffee-and-tea-spec.md with the three items that
// spec's Section 9 lists as "open" but were resolved in a later
// conversation: stop-loss rule, realized-vs-implied vol check, and the
// liquidity/bid-ask check thresholds.
//
// Pricing/sizing/PoP math moved server-side (see lib/options-pricing.js)
// rather than being computed by the model by hand - a real session
// (Sept 17/18) showed the model working through Black-Scholes d1/d2 by
// hand, making an arithmetic slip mid-calculation, and self-correcting
// visibly in its response text before ever emitting JSON. That's both a
// reliability risk (self-correction isn't guaranteed every time) and a
// major source of generated tokens contributing to this endpoint's 504
// timeouts. The model still chooses which strikes/structure to propose
// (a genuine judgment call based on the GEX read) - it no longer prices
// them.
import { computeStrategyEconomics, computeImpliedMove } from '../lib/options-pricing.js';

// ---- Options expiration calendar facts (no API call - pure date math) ----
// Kept local to this file rather than a shared lib, matching this repo's
// existing pattern of small self-contained date helpers per endpoint
// (e.g. the NYSE holiday calculator duplicated client-side in index.html).
function thirdFriday(year, monthIndex) {
  // monthIndex is 0-based (0=Jan) to match JS Date conventions.
  const first = new Date(Date.UTC(year, monthIndex, 1));
  const firstFridayOffset = (5 - first.getUTCDay() + 7) % 7; // Sun=0..Sat=6, Friday=5
  const d = new Date(Date.UTC(year, monthIndex, 1 + firstFridayOffset + 14));
  return d.toISOString().slice(0, 10);
}

function nextMonthlyOpex(fromDateStr) {
  let [y, m] = fromDateStr.split('-').map(Number);
  m -= 1; // to 0-based
  for (let i = 0; i < 4; i++) {
    const candidate = thirdFriday(y, m);
    if (candidate >= fromDateStr) return candidate;
    m += 1;
    if (m > 11) { m = 0; y += 1; }
  }
  return null;
}

function nextTripleWitching(fromDateStr) {
  const quarterMonths = [2, 5, 8, 11]; // 0-based: Mar, Jun, Sep, Dec
  const [fromYear, fromMonth] = fromDateStr.split('-').map(Number);
  let y = fromYear;
  for (let i = 0; i < 6; i++) {
    for (const m of quarterMonths) {
      if (y === fromYear && m < fromMonth - 1) continue;
      const candidate = thirdFriday(y, m);
      if (candidate >= fromDateStr) return candidate;
    }
    y += 1;
  }
  return null;
}

function daysBetween(fromDateStr, toDateStr) {
  const a = new Date(fromDateStr + 'T00:00:00Z');
  const b = new Date(toDateStr + 'T00:00:00Z');
  return Math.round((b - a) / 86400000);
}

// SPY's quarterly ex-dividend dates. Unlike op-ex/triple witching, this
// is NOT a fixed calendar rule - it's whatever State Street's board
// declares each quarter, and while it's usually close to that quarter's
// third Friday, it isn't always exactly that day: the June 2026 ex-date
// (2026-06-18) fell on the Thursday before that quarter's third-Friday
// triple witching (2026-06-19), confirmed via State Street/broker
// dividend histories. So this is a maintained list of CONFIRMED dates,
// not a formula - State Street typically only announces each quarter's
// date a few weeks ahead, so add the next one here once it's confirmed
// (check a broker's SPY dividend history or ssga.com).
const SPY_DIVIDEND_EX_DATES = [
  '2025-12-19',
  '2026-03-20',
  '2026-06-18',
  '2026-09-18',
  // 2026-12 not yet announced as of this writing (Sept 2026) - add once confirmed
];

function nextSpyDividendExDate(fromDateStr) {
  const upcoming = SPY_DIVIDEND_EX_DATES.filter((d) => d >= fromDateStr).sort();
  return upcoming[0] || null;
}

// Best-effort calendar note for one date (session_date or expiration) -
// op-ex/triple-witching are pure date math and available for any ticker;
// the SPY dividend check only applies when the session's ticker is SPY
// (or unspecified, which defaults to SPY elsewhere in this file), since
// SPY_DIVIDEND_EX_DATES is SPY-specific. LOOKAHEAD_DAYS controls how far
// ahead any of these are flagged as "coming up" (currently 14).
const LOOKAHEAD_DAYS = 14;
function opexContextForDate(dateStr, ticker) {
  if (!dateStr) return null;
  const nextOpex = nextMonthlyOpex(dateStr);
  const nextWitching = nextTripleWitching(dateStr);
  if (!nextOpex || !nextWitching) return null;
  const isSpy = !ticker || ticker.toUpperCase() === 'SPY';
  const nextDivExDate = isSpy ? nextSpyDividendExDate(dateStr) : null;
  return {
    date: dateStr,
    monthly_opex: {
      date: nextOpex,
      is_today: nextOpex === dateStr,
      days_until: daysBetween(dateStr, nextOpex),
      within_lookahead: daysBetween(dateStr, nextOpex) <= LOOKAHEAD_DAYS,
    },
    triple_witching: {
      date: nextWitching,
      is_today: nextWitching === dateStr,
      days_until: daysBetween(dateStr, nextWitching),
      within_lookahead: daysBetween(dateStr, nextWitching) <= LOOKAHEAD_DAYS,
    },
    spy_dividend_ex_date: nextDivExDate ? {
      date: nextDivExDate,
      is_today: nextDivExDate === dateStr,
      days_until: daysBetween(dateStr, nextDivExDate),
      within_lookahead: daysBetween(dateStr, nextDivExDate) <= LOOKAHEAD_DAYS,
    } : null,
  };
}

const SPEC = `PROTOCOL COFFEE AND TEA - full workflow specification

PURPOSE: A recurring session workflow that turns a GEX (gamma exposure)
read into a structured trade writeup: market structure, thesis, sized
defined-risk options strategies, and management rules.

CORE CALCULATION LOGIC:

1. Structure read: identify the gamma regime (uniform_positive if every
strike carries positive net GEX, flip_zone_present if a sign change
exists). Identify the nearest major walls above and below spot (largest
3-4 by $ GEX size in each direction). Note confluence: a GEX wall aligning
within ~2 points of POC, VAH/VAL, or a moving average is a stronger level
than an isolated wall - weight these accordingly in market_structure and
key_levels.

2. Strike selection: the input includes iv_used_pct and iv_source directly -
these are provided values, already determined server-side (a real
Tiger-derived underlying IV when available, an assumed fallback only
otherwise). Copy them verbatim into volatility_check - do not assume,
recompute, or second-guess them. You do NOT need to price legs, compute
credit/debit amounts, position sizing, or probability of profit - all of
that is computed server-side from the strikes you choose. Your job here
is choosing which strikes form each structure, based on the GEX read.

3. Strategy generation: map each market view to ONE of these defined-risk
structures - NEVER a naked short strike:
- Iron Condor (range holds, high confidence): short call+long call wing /
  short put+long put wing, symmetric around spot
- Iron Butterfly (tighter pin): short straddle ATM + long wings further out
- Bull Put Spread (downside holds): short put near support + long put further below
- Bear Call Spread (ceiling holds): short call near resistance + long call further above
- Long Call Vertical (expect upside breakout): long call near resistance + short call further above
- Long Put Vertical (expect downside breakout): long put near support + short put further below
- Long Strangle (violent break either direction): long call + long put, both OTM
Generate exactly 3 of the most relevant structures given the actual market
structure read, not more - this keeps the total response length reliable.

4. Profit-target narrative: for each strategy's profit_target_est_path,
briefly describe the path to the 50% target in plain terms (e.g. "Theta
decay with spot holding the range through mid-session") - the actual
target price and dollar amounts are computed server-side from your
chosen legs, you're only providing the qualitative description.
- Never propose a calendar in this workflow (single-expiry chain data only)

5. STOP-LOSS narrative: for each strategy's stop_loss_structural_trigger,
describe the structural invalidation condition (confirmed break of the
level the trade depended on, e.g. "volume-confirmed close below 755").
The actual 50%-max-loss price trigger and dollar amounts are computed
server-side - you're only providing this structural description. The
structural trigger takes precedence over the price-based stop and can
fire independently, even before the 50%-loss level is reached.

8. Entry triggers:
- Neutral/range strategies: near the center of the expected range
- Single-sided credit spreads: on a pullback/pushback toward the short strike
- Directional/breakout strategies: wait for volume-confirmed price action
  through the level - never anticipate a break before it happens
- Volatility plays: catalyst-driven, enter before the known event

9. Macro catalyst tie-in: if any event in macro_events_this_window falls
inside the expiration window, flag it explicitly in trade_thesis, note it
can break the pin thesis independent of GEX positioning, and add an
elevated-risk-window caution to range/credit strategy guidance in
macro_context.per_strategy_guidance.

9a. Options expiration catalyst tie-in: the input's
options_expiration_context gives session_date and expiration each three
sub-objects - monthly_opex, triple_witching, and (SPY sessions only)
spy_dividend_ex_date - each with date, is_today, days_until, and
within_lookahead (true when days_until is 14 or fewer). This is purely
calendar/schedule data (date math for op-ex/witching, a maintained list
of confirmed dates for the SPY dividend), never GEX or chain data.
Whenever any of the three has within_lookahead true for session_date or
expiration, treat it the same as a macro_events_this_window catalyst:
flag it explicitly in trade_thesis, naming which one(s) (monthly op-ex,
the larger quarterly triple witching, and/or the SPY dividend ex-date)
and how many days out (today, or "in N days"), and add an
elevated-risk-window caution to range/credit strategy guidance in
macro_context.per_strategy_guidance. The mechanism differs by type - say
so accordingly: op-ex/triple witching can drive pinning toward a
max-pain-like level into the close and/or a volatility pickup right
after as dealer hedges roll off; a dividend ex-date causes a mechanical,
known-in-advance gap down in the underlying by roughly the dividend
amount at the open (dealers holding short calls typically hedge this,
so it's a smaller, more predictable effect than an op-ex unwind, but
still worth noting for strikes very close to spot). If none of the three
has within_lookahead true, do not mention any of this at all.

10. REALIZED-VS-IMPLIED VOL CHECK (finalized): use the iv_used_pct/
iv_source provided directly in the input (see step 2) - do not compute or
assume a separate IV number for this check. If the input includes
realized_vol_10d_pct/realized_vol_20d_pct (may be absent - if so, state
"insufficient data" in volatility_check and skip the comparison),
compare realized vol against iv_used_pct. Classify verdict as "rich" (IV
notably above realized - favors credit/premium-selling structures),
"cheap" (IV notably below realized - favors long strangles/debit
verticals), or "fair" (roughly in line). State strategy_tilt explaining
which structures this favors and why.

11. EOD FLOW CONTEXT (when flow_data is provided): summarize the session's
skew and aggression tone (session_summary). Cross-reference each major GEX
wall against flow_data.top_conviction_trades and note in
wall_cross_references whether flow reinforces or contradicts that wall
(gex_confirms: true/false). List standout_prints (the highest-premium or
highest-score individual clusters). End with tension_or_alignment_note - a
one-line verdict on whether flow and GEX structure agree or are in tension.

12. LIQUIDITY CHECK (finalized, placeholder until live chain flows in):
every strategy's liquidity_check.status defaults to "awaiting_live_chain"
with detail null, since this endpoint doesn't have live bid/ask data wired
in. Do not fabricate a green/yellow/red status without real spread/OI data.

13. Day-over-day comparison: if prior_snapshot is provided, diff spot,
POC, VAH/VAL, MA30, and whether the gamma regime itself changed - always
include these directly in changes, regardless of how much they moved.
For wall-level GEX sizes at matching strikes: only include a wall if its
$ GEX size changed by at least $50M in magnitude, and cap this to the 5
walls with the largest magnitude change (largest first) - do not
generate an entry for every wall regardless of size; most barely move
and just add length without changing what's actionable. Set
has_prior_snapshot accordingly - if no prior snapshot was provided, set
it false and leave changes empty rather than fabricating a comparison.

14. Base-case range and avoid list (feed the PDF's thesis and "Avoid"
sections): set trade_thesis.base_case_levels to the [low, high] range the
base case expects to hold (two numbers). Fill "avoid" with 0-2 defined-
risk structures that look tempting but fit today's gamma shape badly -
e.g. selling premium into a negative-gamma pocket where hedging amplifies
moves - each with a one-line reason. Empty array if nothing stands out.

15. Sizing regime: the input's sizing_regime (regime, risk_budget_pct,
reason) is already decided server-side and already applied to every
strategy's size - do not change or restate sizes. Reflect it in the
prose: in "high_vol" or "unknown", state lower confidence in the GEX read
in market_structure.summary or strategy_tilt; in "calm", you may state
the read is in the regime where it has historically been most reliable.
Never imply walls will contain price in any regime.

MANAGEMENT RULES:
- GEX walls are "pay attention" lines, not hard floors/ceilings - they
  describe dealer hedging mechanics, not certainty.
- A gap through a level (vs. a slow grind to it) suggests hedging was
  overwhelmed rather than respected - treat the pin thesis as broken
  pending confirmation, not held through on hope of mean reversion.

DISCLAIMERS: All figures illustrative unless pricing_source is
"live_chain". This is not financial advice. GEX walls are dealer-hedging
mechanics, not guarantees. Every strategy MUST be defined-risk - a
response with any naked/uncovered leg will be rejected downstream.`;

const OUTPUT_SCHEMA_NOTE = `Keep every text field short - hard limits, not suggestions:
- "summary", "why_it_matters", "session_summary", "base_case", "profit_target_est_path", "strategy_tilt": 25 words max each
- "significance", "detail", "guidance", "stop_loss_structural_trigger", "entry_trigger", "condition", "delta_note", "tension_or_alignment_note", "reason": 15 words max each
- "structure": 8 words max
These are real limits because this output has many nested sections and exactly
3 full strategies - verbosity in any one field risks the whole response being
cut off before it completes, which fails the entire session. A shorter,
complete response is always better than a longer one that gets cut off. Return
ONLY a single JSON object with this exact top-level shape - no
prose, no markdown fences, no commentary outside the JSON:

{
  "market_structure": { "summary": string, "key_levels": [{"strike": number, "type": "wall"|"flip_zone"|"support"|"resistance"|"confluence", "gex_usd_m": number|null, "significance": string}] },
  "macro_context": { "key_catalyst": string, "why_it_matters": string, "per_strategy_guidance": [{"strategy_type": string, "guidance": string}] },
  "volatility_check": { "realized_vol_10d_pct": number, "realized_vol_20d_pct": number, "iv_used_pct": number, "iv_source": "tiger_underlying_iv"|"user_assumed"|"placeholder"|"live_chain", "verdict": "rich"|"cheap"|"fair", "strategy_tilt": string },
  "eod_flow_context": { "session_summary": string, "wall_cross_references": [{"strike": number, "gex_confirms": boolean, "detail": string}], "standout_prints": [{"strike": number, "detail": string}], "tension_or_alignment_note": string } | null,
  "trade_thesis": { "base_case": string, "base_case_levels": [number], "upside_break": {"condition": string, "target_levels": [number]}, "downside_break": {"condition": string, "target_levels": [number]} },
  "strategies": [{
    "name": string, "view": string,
    "legs": [{"action": "buy"|"sell", "type": "C"|"P", "strike": number, "expiry": string}],
    "profit_target_est_path": string,
    "stop_loss_structural_trigger": string,
    "entry_trigger": string,
    "liquidity_check": {"status": "awaiting_live_chain"|"green"|"yellow"|"red", "detail": string|null}
  }],
  "day_over_day_comparison": {"has_prior_snapshot": boolean, "changes": [{"metric": string, "prior": number|null, "current": number|null, "delta_note": string}]} | null,
  "avoid": [{"structure": string, "reason": string}]
}`;

// Guard before pricing: computeStrategyEconomics assumes non-empty legs,
// so this has to run before the pricing/PoP merge step, not after.
function assertStrategiesHaveLegs(strategies) {
  if (!Array.isArray(strategies) || strategies.length === 0) {
    throw new Error('Reasoning output has no strategies - rejected');
  }
  for (const s of strategies) {
    if (!Array.isArray(s.legs) || s.legs.length === 0) {
      throw new Error(`Strategy "${s.name}" has no legs defined - refusing to price a strategy with no structure`);
    }
  }
}

// Lightweight structural validation mirroring the Python schema's
// defined_risk_only and at_least_one_strategy validators - reject a
// malformed response before it reaches the frontend rather than let a
// bad structure through silently. Runs AFTER the pricing merge below, so
// max_loss_per_contract_usd is always a real number by this point -
// this remains as a final defense-in-depth check, not the primary guard.
function validateOutput(output) {
  if (!Array.isArray(output.strategies) || output.strategies.length === 0) {
    throw new Error('Reasoning output has no strategies - rejected');
  }
  for (const s of output.strategies) {
    if (!Array.isArray(s.legs) || s.legs.length === 0) {
      throw new Error(`Strategy "${s.name}" has no legs defined - refusing to render a strategy with no structure`);
    }
    if (s.pricing?.max_loss_per_contract_usd === null || s.pricing?.max_loss_per_contract_usd === undefined) {
      throw new Error(`Strategy "${s.name}" is missing max_loss_per_contract_usd - undefined risk is not allowed`);
    }
  }
  return true;
}

// Best-effort write of the full session output to Cloudflare D1 (ct_sessions
// table) - this is the storage layer strategy-level backtesting reads
// from later. Never blocks or fails the actual response; a storage
// failure just means this session won't be gradeable, not that the
// session itself failed.
async function saveSessionToD1(ticker, sessionDate, expiration, outputJson) {
  const accountId = (process.env.CLOUDFLARE_ACCOUNT_ID || '').trim();
  const databaseId = (process.env.CLOUDFLARE_D1_DATABASE_ID || '').trim();
  const apiToken = (process.env.CLOUDFLARE_API_TOKEN || '').trim();

  if (!accountId || !databaseId || !apiToken) {
    return { stored: false, reason: 'missing_cloudflare_env_vars' };
  }

  try {
    const d1Res = await fetch(
      `https://api.cloudflare.com/client/v4/accounts/${accountId}/d1/database/${databaseId}/query`,
      {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${apiToken}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          sql: 'INSERT OR REPLACE INTO ct_sessions (ticker, session_date, expiration, output_json) VALUES (?, ?, ?, ?)',
          params: [ticker, sessionDate, expiration, outputJson],
        }),
      }
    );
    if (!d1Res.ok) {
      const detail = await d1Res.text();
      return { stored: false, reason: 'http_error', detail };
    }
    const result = await d1Res.json();
    if (result.success) return { stored: true };
    return { stored: false, reason: 'd1_query_failed', detail: result.errors };
  } catch (err) {
    return { stored: false, reason: 'request_error', detail: String(err) };
  }
}

// Extract the last complete, parseable JSON object from raw model text.
// This matters because the model sometimes writes out its reasoning -
// occasionally including a full DRAFT JSON object - before settling on a
// final, corrected JSON object at the end. Naive first-brace-to-last-brace
// slicing then grabs everything from the draft's opening brace through the
// final object's closing brace, including all the reasoning prose in
// between, which is never valid JSON on its own. Scanning backward from
// the end of the text and brace-balancing instead reliably isolates just
// the LAST complete object - the one the model actually intends as its
// answer - regardless of how much draft/reasoning text precedes it.
function extractLastJSONObject(text) {
  let searchEnd = text.length - 1;
  while (searchEnd >= 0) {
    const end = text.lastIndexOf('}', searchEnd);
    if (end === -1) return null;
    let depth = 0;
    let start = -1;
    for (let i = end; i >= 0; i--) {
      if (text[i] === '}') depth++;
      else if (text[i] === '{') {
        depth--;
        if (depth === 0) { start = i; break; }
      }
    }
    if (start !== -1) {
      try {
        return JSON.parse(text.slice(start, end + 1));
      } catch (e) {
        // This closing brace didn't belong to a valid top-level object -
        // keep searching further back in the text.
      }
    }
    searchEnd = end - 1;
  }
  return null;
}

const round2 = (x) => Math.round(x * 100) / 100;
const ASSUMED_FALLBACK_IV_PCT = 13;
// Regime-dependent risk budget (Sugar findings: the GEX signal is most
// reliable in calm regimes, least in high-vol ones - Maurer 2026). Computed
// here in code, not left to the model, so the regime actually changes
// position size rather than just the prose. Tiers:
//   20d realized vol > 20%  -> 2.5% per strategy (halved)
//   20d realized vol <= 20% -> 5%   per strategy (full)
//   realized vol missing    -> 2.5% per strategy (conservative default)
// Threshold is deliberately a single, pre-specified number - not tuned to
// results - so it can be tested honestly once graded sessions accumulate.
const RISK_BUDGET_FULL_PCT = 5;
const RISK_BUDGET_REDUCED_PCT = 2.5;
const HIGH_VOL_RV20_THRESHOLD_PCT = 20;

function regimeRiskBudget(rv20) {
  const hasRv = typeof rv20 === 'number' && Number.isFinite(rv20) && rv20 > 0;
  if (!hasRv) {
    return {
      regime: 'unknown',
      realized_vol_20d_pct: null,
      threshold_pct: HIGH_VOL_RV20_THRESHOLD_PCT,
      risk_budget_pct: RISK_BUDGET_REDUCED_PCT,
      reason: 'Realized vol not supplied - sizing defaulted down to the reduced budget.',
    };
  }
  if (rv20 > HIGH_VOL_RV20_THRESHOLD_PCT) {
    return {
      regime: 'high_vol',
      realized_vol_20d_pct: rv20,
      threshold_pct: HIGH_VOL_RV20_THRESHOLD_PCT,
      risk_budget_pct: RISK_BUDGET_REDUCED_PCT,
      reason: `20d RV ${rv20}% is above ${HIGH_VOL_RV20_THRESHOLD_PCT}% - GEX read less reliable, size halved.`,
    };
  }
  return {
    regime: 'calm',
    realized_vol_20d_pct: rv20,
    threshold_pct: HIGH_VOL_RV20_THRESHOLD_PCT,
    risk_budget_pct: RISK_BUDGET_FULL_PCT,
    reason: `20d RV ${rv20}% is at or below ${HIGH_VOL_RV20_THRESHOLD_PCT}% - GEX read at its most reliable, full size.`,
  };
}

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Use POST' });
  }

  const input = req.body || {};
  if (!input.gex_data || !input.portfolio_size_usd) {
    return res.status(400).json({ error: 'Missing gex_data or portfolio_size_usd in request body' });
  }
  const spot = input.gex_data.spot;
  if (typeof spot !== 'number' || spot <= 0) {
    return res.status(400).json({ error: 'Missing or invalid gex_data.spot in request body' });
  }

  // Determine IV server-side, before calling the model, rather than
  // relying on the model to read and correctly apply this rule itself.
  // Three tiers, checked in order:
  // 1. gex_data.gamma_inputs.underlying_iv_used_as_fallback - a real,
  //    Tiger-computed 30-day IV. Stored as a decimal fraction (e.g. 0.13)
  //    by get-tiger-gex.py - see HARD_FALLBACK_IV = 0.15 there - so it's
  //    converted to a percentage here. NOTE: index.html's
  //    buildGexDataFromCurrent() currently does NOT include gamma_inputs
  //    in what it sends, so this tier will not fire via the real UI flow
  //    today - kept as tier 1 in case a future caller does provide it,
  //    rather than removed.
  // 2. input.assumed_iv_pct - a deliberate, user-set value from the
  //    ctAssumedIv field on the page (defaults to 13 there too, but is
  //    editable) - this is what the current UI flow actually sends, and
  //    is a real chosen input, not a guess, so it's labeled accordingly.
  // 3. A hardcoded fallback, only if neither of the above is present.
  const underlyingIvFraction = input.gex_data.gamma_inputs?.underlying_iv_used_as_fallback;
  const hasRealIv = typeof underlyingIvFraction === 'number' && underlyingIvFraction > 0;
  const hasUserAssumedIv = typeof input.assumed_iv_pct === 'number' && input.assumed_iv_pct > 0;

  let ivUsedPct, ivSource;
  if (hasRealIv) {
    ivUsedPct = round2(underlyingIvFraction * 100);
    ivSource = 'tiger_underlying_iv';
  } else if (hasUserAssumedIv) {
    ivUsedPct = round2(input.assumed_iv_pct);
    ivSource = 'user_assumed';
  } else {
    ivUsedPct = ASSUMED_FALLBACK_IV_PCT;
    ivSource = 'placeholder';
  }

  try {
    const systemPrompt = `You are running Protocol Coffee and Tea, a defined-risk options trading session workflow. Follow this specification exactly.\n\n${SPEC}\n\n${OUTPUT_SCHEMA_NOTE}`;
    // Calendar-computed, no chain/GEX data involved - always available
    // even when macro_events_this_window (an external-provider fetch)
    // isn't. Session date and expiration are usually the same day for
    // this single-expiry workflow, but both are checked independently
    // in case they ever differ.
    const inputTicker = input.ticker || 'SPY';
    const optionsExpirationContext = {
      session_date: opexContextForDate(input.session_date, inputTicker),
      expiration: opexContextForDate(input.expiration, inputTicker),
    };
    const sizingRegime = regimeRiskBudget(input.realized_vol_20d_pct);
    const modelInput = {
      ...input,
      sizing_regime: sizingRegime,
      iv_used_pct: ivUsedPct,
      iv_source: ivSource,
      options_expiration_context: optionsExpirationContext,
    };

    const anthropicRes = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': process.env.ANTHROPIC_API_KEY,
        'anthropic-version': '2023-06-01',
      },
      body: JSON.stringify({
        model: 'claude-opus-5-5', // TEMPORARY TRIAL - was 'claude-sonnet-4-6'. Revert after testing.
        max_tokens: 32000,
        // TEMPORARY TRIAL: default effort is "high" - real usage on the
        // first two runs showed ~2,700-3,000 thinking tokens/session.
        // Testing "medium" to see if it meaningfully cuts thinking-token
        // cost while output quality holds up for this bounded, spec-
        // driven task. Remove this whole output_config block on revert.
        output_config: { effort: 'medium' },
        system: systemPrompt,
        messages: [
          { role: 'user', content: JSON.stringify(modelInput, null, 2) },
        ],
      }),
    });

    if (!anthropicRes.ok) {
      const errText = await anthropicRes.text();
      return res.status(502).json({ error: 'Anthropic API error', detail: errText });
    }

    const result = await anthropicRes.json();
    const textBlock = result.content.find((b) => b.type === 'text');
    if (!textBlock) {
      return res.status(502).json({ error: 'No text content in model response' });
    }

    // Check for truncation FIRST - a response cut off by hitting max_tokens
    // produces incomplete JSON that no amount of brace-matching can fix,
    // and the resulting error message should say so plainly rather than
    // the generic "invalid JSON" (which reads like a formatting bug, not
    // a length-limit issue).
    if (result.stop_reason === 'max_tokens') {
      return res.status(502).json({
        error: 'Response was cut off before completing (hit the token limit) - the requested output (multiple strategies + full narrative sections) exceeded max_tokens. Try again, or reduce scope (fewer macro events, shorter portfolio context) if this recurs.',
        raw: textBlock.text,
      });
    }

    let cleaned = textBlock.text.replace(/```json|```/g, '').trim();
    let parsed;
    try {
      parsed = JSON.parse(cleaned);
    } catch (e) {
      parsed = extractLastJSONObject(cleaned);
      if (!parsed) {
        return res.status(502).json({ error: 'Model did not return valid JSON', raw: cleaned });
      }
    }

    // Pricing/sizing/PoP merge: the model chose strikes/structure (a
    // judgment call) - everything numeric about those strikes is computed
    // here, deterministically, rather than trusted from the model's own
    // output. See lib/options-pricing.js for why this moved server-side.
    try {
      assertStrategiesHaveLegs(parsed.strategies);
      parsed.strategies = parsed.strategies.map((s) => {
        const econ = computeStrategyEconomics({
          legs: s.legs,
          spot,
          ivUsedPct,
          sessionDate: input.session_date,
          expiration: input.expiration,
          portfolioSizeUsd: input.portfolio_size_usd,
          riskBudgetPct: sizingRegime.risk_budget_pct,
        });
        return {
          name: s.name,
          view: s.view,
          legs: s.legs,
          pricing: econ.pricing,
          sizing: econ.sizing,
          profit_target_50pct: { ...econ.profit_target_50pct, est_path: s.profit_target_est_path },
          stop_loss: { ...econ.stop_loss, structural_trigger: s.stop_loss_structural_trigger },
          pop_pct: econ.pop_pct,
          entry_trigger: s.entry_trigger,
          liquidity_check: s.liquidity_check,
        };
      });
    } catch (pricingErr) {
      return res.status(502).json({ error: `Pricing failed: ${pricingErr.message}`, raw: parsed });
    }

    // Force the server-determined IV into the response, overriding
    // whatever the model echoed - guarantees consistency with what was
    // actually used to price every strategy above, rather than trusting
    // the model copied the provided value correctly. Also attaches the
    // implied weekly move, computed the same way (see lib/options-
    // pricing.js) - deterministic, added after the model responds, same
    // pattern as everything else in this block.
    if (parsed.volatility_check) {
      parsed.volatility_check.iv_used_pct = ivUsedPct;
      parsed.volatility_check.iv_source = ivSource;
      parsed.volatility_check.implied_move = computeImpliedMove({
        spot,
        ivUsedPct,
        sessionDate: input.session_date,
        expiration: input.expiration,
      });
    }

    // Server-determined, like iv_used_pct above - never taken from the model.
    parsed.sizing_regime = sizingRegime;

    try {
      validateOutput(parsed);
    } catch (validationErr) {
      return res.status(502).json({ error: `Response failed validation: ${validationErr.message}`, raw: parsed });
    }

    // Persist for later backtesting/grading - never blocks the response.
    const ticker = input.ticker || 'SPY';
    if (input.session_date && input.expiration) {
      const storageResult = await saveSessionToD1(ticker, input.session_date, input.expiration, JSON.stringify(parsed));
      parsed._session_stored = storageResult.stored;
      if (!storageResult.stored) {
        parsed._session_storage_detail = storageResult;
      }
    }

    return res.status(200).json(parsed);
  } catch (err) {
    return res.status(500).json({ error: 'Server error', detail: String(err) });
  }
}
