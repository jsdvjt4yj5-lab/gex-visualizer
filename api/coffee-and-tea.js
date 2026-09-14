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

2. Options pricing: when chain_data_available is false, use Black-Scholes
with an assumed short-dated IV (document the assumption, e.g. ~13%) and
actual days-to-expiry. Set pricing_source to "black_scholes_estimate".
When chain_data_available is true, note pricing should ideally reflect
real chain data, but since this endpoint doesn't have live bid/ask wired
in yet, still use "black_scholes_estimate" and be honest about it - never
claim "live_chain" unless real chain pricing was actually provided in the
input.

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

4. Position sizing: default risk budget is 5% of portfolio_size_usd = max
loss per structure. contracts = floor(risk_budget / max_loss_per_contract).
State this assumption in the guidance text.

5. 50%-profit-target exits:
- Credit structures: target = close at 50% of credit collected decayed
  (banked profit = credit / 2 per contract)
- Debit structures: target = close at 1.5x debit paid (banked profit =
  debit / 2 per contract)
- Never propose a calendar in this workflow (single-expiry chain data only)

6. STOP-LOSS RULE (finalized): exit at 50% of the structure's max loss
(price-based, mirroring the profit-target rule), PLUS a structural-
invalidation trigger (confirmed break of the level the trade depended on,
e.g. "volume-confirmed close below 755"). The structural trigger takes
precedence and can fire independently, even before the 50%-loss price
level is reached - whichever condition hits first governs the exit.

7. Probability of profit: use the lognormal/Black-Scholes-implied
distribution at expiry (same IV/DTE as pricing) to compute probability
spot finishes within the structure's profit zone - a full breakeven-range
calculation, not a delta shortcut. Rank ALL strategies highest to lowest
POP in pop_ranking, with a one-line "why" citing single-sided vs two-sided
structure and breakeven distance.

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

10. REALIZED-VS-IMPLIED VOL CHECK (finalized): if the input includes
realized_vol_10d_pct/realized_vol_20d_pct (may be absent - if so, state
"insufficient data" in volatility_check and use the placeholder IV alone),
compare against iv_used_pct. Classify verdict as "rich" (IV notably above
realized - favors credit/premium-selling structures), "cheap" (IV notably
below realized - favors long strangles/debit verticals), or "fair"
(roughly in line). State strategy_tilt explaining which structures this
favors and why.

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
POC, VAH/VAL, MA30, wall sizes at matching strikes, and whether the gamma
regime itself changed. Set has_prior_snapshot accordingly - if no prior
snapshot was provided, set it false and leave changes empty rather than
fabricating a comparison.

MANAGEMENT RULES:
- GEX walls are "pay attention" lines, not hard floors/ceilings - they
  describe dealer hedging mechanics, not certainty.
- A gap through a level (vs. a slow grind to it) suggests hedging was
  overwhelmed rather than respected - treat the pin thesis as broken
  pending confirmation, not held through on hope of mean reversion.

DISCLAIMERS: All figures illustrative unless pricing_source is
"live_chain". This is not financial advice. GEX walls are dealer-hedging
mechanics, not guarantees. Every strategy MUST be defined-risk - a
response with any naked leg or missing max_loss_per_contract_usd will be
rejected downstream.`;

const OUTPUT_SCHEMA_NOTE = `Keep every text field short - hard limits, not suggestions:
- "summary", "why_it_matters", "session_summary", "base_case", "est_path", "strategy_tilt": 25 words max each
- "significance", "detail", "guidance", "structural_trigger", "entry_trigger", "condition", "why", "delta_note", "tension_or_alignment_note": 15 words max each
These are real limits because this output has many nested sections and exactly
3 full strategies - verbosity in any one field risks the whole response being
cut off before it completes, which fails the entire session. A shorter,
complete response is always better than a longer one that gets cut off. Return
ONLY a single JSON object with this exact top-level shape - no
prose, no markdown fences, no commentary outside the JSON:

{
  "market_structure": { "summary": string, "key_levels": [{"strike": number, "type": "wall"|"flip_zone"|"support"|"resistance"|"confluence", "gex_usd_m": number|null, "significance": string}] },
  "macro_context": { "key_catalyst": string, "why_it_matters": string, "per_strategy_guidance": [{"strategy_type": string, "guidance": string}] },
  "volatility_check": { "realized_vol_10d_pct": number, "realized_vol_20d_pct": number, "iv_used_pct": number, "iv_source": "placeholder"|"live_chain", "verdict": "rich"|"cheap"|"fair", "strategy_tilt": string },
  "eod_flow_context": { "session_summary": string, "wall_cross_references": [{"strike": number, "gex_confirms": boolean, "detail": string}], "standout_prints": [{"strike": number, "detail": string}], "tension_or_alignment_note": string } | null,
  "trade_thesis": { "base_case": string, "upside_break": {"condition": string, "target_levels": [number]}, "downside_break": {"condition": string, "target_levels": [number]} },
  "strategies": [{
    "name": string, "view": string,
    "legs": [{"action": "buy"|"sell", "type": "C"|"P", "strike": number, "expiry": string}],
    "pricing": {"credit_or_debit": "credit"|"debit", "amount_per_contract_usd": number, "max_loss_per_contract_usd": number, "max_profit_per_contract_usd": number|null, "pricing_source": "black_scholes_estimate"|"live_chain"},
    "sizing": {"risk_budget_pct": number, "contracts": number, "total_max_loss_usd": number, "total_max_profit_usd": number|null},
    "profit_target_50pct": {"trigger_price_usd": number, "total_profit_usd": number, "est_path": string},
    "stop_loss": {"price_trigger_usd": number|null, "total_loss_at_stop_usd": number|null, "structural_trigger": string},
    "pop_pct": number|null,
    "entry_trigger": string,
    "liquidity_check": {"status": "awaiting_live_chain"|"green"|"yellow"|"red", "detail": string|null}
  }],
  "pop_ranking": [{"rank": number, "strategy_name": string, "pop_pct": number, "why": string}],
  "day_over_day_comparison": {"has_prior_snapshot": boolean, "changes": [{"metric": string, "prior": number|null, "current": number|null, "delta_note": string}]} | null
}`;

// Lightweight structural validation mirroring the Python schema's
// defined_risk_only and at_least_one_strategy validators - reject a
// malformed response before it reaches the frontend rather than let a
// bad structure through silently.
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

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Use POST' });
  }

  const input = req.body || {};
  if (!input.gex_data || !input.portfolio_size_usd) {
    return res.status(400).json({ error: 'Missing gex_data or portfolio_size_usd in request body' });
  }

  try {
    const systemPrompt = `You are running Protocol Coffee and Tea, a defined-risk options trading session workflow. Follow this specification exactly.\n\n${SPEC}\n\n${OUTPUT_SCHEMA_NOTE}`;

    const anthropicRes = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': process.env.ANTHROPIC_API_KEY,
        'anthropic-version': '2023-06-01',
      },
      body: JSON.stringify({
        model: 'claude-sonnet-4-6',
        max_tokens: 32000,
        system: systemPrompt,
        messages: [
          { role: 'user', content: JSON.stringify(input, null, 2) },
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
      const firstBrace = cleaned.indexOf('{');
      const lastBrace = cleaned.lastIndexOf('}');
      if (firstBrace !== -1 && lastBrace > firstBrace) {
        try {
          parsed = JSON.parse(cleaned.slice(firstBrace, lastBrace + 1));
        } catch (e2) {
          return res.status(502).json({ error: 'Model did not return valid JSON', raw: cleaned });
        }
      } else {
        return res.status(502).json({ error: 'Model did not return valid JSON', raw: cleaned });
      }
    }

    try {
      validateOutput(parsed);
    } catch (validationErr) {
      return res.status(502).json({ error: `Response failed validation: ${validationErr.message}`, raw: parsed });
    }

    return res.status(200).json(parsed);
  } catch (err) {
    return res.status(500).json({ error: 'Server error', detail: String(err) });
  }
}
