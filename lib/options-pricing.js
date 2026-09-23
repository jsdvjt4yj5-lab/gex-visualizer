// Server-side options math for Protocol Coffee and Tea. Moved out of the
// model's job (which was doing this by hand, visibly, in its response
// text - see the Sept 17/18 session where it worked through d1/d2 by
// hand, made an arithmetic slip mid-calculation, and self-corrected) and
// into deterministic code instead. This removes a large source of
// generated tokens (the model's scratch work) and a real correctness
// risk (hand arithmetic isn't guaranteed to self-correct every time).
//
// Scope: given a strategy's legs (chosen by the model, based on its read
// of the GEX structure - that judgment call still belongs to the model)
// plus market context (spot, IV, days to expiry, portfolio size), this
// computes pricing, sizing, 50%-target/stop-loss price triggers, and
// probability of profit. The model no longer produces any of these
// numbers itself.

const round2 = (x) => Math.round(x * 100) / 100;

// Standard normal CDF via the Abramowitz-Stegun approximation (max error
// ~7.5e-8) - accurate enough for this use case and avoids pulling in a
// stats dependency for one function.
function normalCDF(x) {
  const t = 1 / (1 + 0.2316419 * Math.abs(x));
  const d = 0.3989423 * Math.exp((-x * x) / 2);
  let p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))));
  if (x > 0) p = 1 - p;
  return p;
}

// Black-Scholes price for a single European option leg. r assumed 0
// (short-dated, r's effect is negligible at this DTE and it keeps the
// model simple and auditable).
function bsPrice(type, S, K, T, sigma) {
  if (T <= 0 || sigma <= 0) {
    return type === 'C' ? Math.max(S - K, 0) : Math.max(K - S, 0);
  }
  const sqrtT = Math.sqrt(T);
  const d1 = (Math.log(S / K) + (sigma * sigma * T) / 2) / (sigma * sqrtT);
  const d2 = d1 - sigma * sqrtT;
  return type === 'C'
    ? S * normalCDF(d1) - K * normalCDF(d2)
    : K * normalCDF(-d2) - S * normalCDF(-d1);
}

// Years to expiry. 0DTE is special-cased: Coffee and Tea runs roughly
// 1hr after the 9:30am ET open, so ~5.5 of the session's 6.5 trading
// hours remain. Multi-day DTE approximates trading days from calendar
// days (5/7 ratio) - not holiday-aware, but reasonable for the short
// windows this workflow deals with.
function yearsToExpiry(sessionDate, expiration) {
  const TRADING_HOURS_PER_DAY = 6.5;
  const TRADING_DAYS_PER_YEAR = 252;
  if (sessionDate === expiration) {
    const hoursRemaining = 5.5;
    return hoursRemaining / TRADING_HOURS_PER_DAY / TRADING_DAYS_PER_YEAR;
  }
  const msPerDay = 24 * 60 * 60 * 1000;
  const calendarDays =
    (new Date(`${expiration}T00:00:00Z`) - new Date(`${sessionDate}T00:00:00Z`)) / msPerDay;
  const tradingDays = Math.max(calendarDays * (5 / 7), 1);
  return tradingDays / TRADING_DAYS_PER_YEAR;
}

// Per-share payoff at expiry for a given final price ST, across all legs.
// This is exactly piecewise-linear in ST with kinks at each strike - a
// true mathematical property of option payoffs, not an approximation.
function payoffAt(ST, pricedLegs) {
  let pnl = 0;
  for (const leg of pricedLegs) {
    const intrinsic = leg.type === 'C' ? Math.max(ST - leg.strike, 0) : Math.max(leg.strike - ST, 0);
    const mult = leg.action === 'buy' ? 1 : -1;
    pnl += mult * (intrinsic - leg.price);
  }
  return pnl;
}

// Probability of profit via the lognormal distribution implied by (S, T,
// sigma) - the same distribution Black-Scholes itself assumes, so this
// is consistent with the pricing above rather than a separate estimate.
// Works generically for any leg combination: finds where the (piecewise-
// linear) payoff crosses zero, then sums risk-neutral probability mass
// over the resulting profit intervals using the standard N(d2) identity.
function computePoP(strikes, pricedLegs, S, T, sigma) {
  const farProbe = strikes[strikes.length - 1] * 3 + 1000;
  const points = [0, ...strikes, farProbe];
  const payoffs = points.map((p) => payoffAt(p, pricedLegs));

  // CDF of the lognormal distribution: P(S_T < K). K=0 -> 0, effectively
  // K=Infinity -> 1 (handled by the caller passing a sentinel).
  function cdf(K) {
    if (K <= 0) return 0;
    if (!isFinite(K)) return 1;
    const d2 = (Math.log(S / K) - (sigma * sigma * T) / 2) / (sigma * Math.sqrt(T));
    return 1 - normalCDF(d2); // P(S_T < K) = N(-d2)
  }

  // Build the full sorted list of interval boundaries: original points
  // plus any exact zero-crossing found by linear interpolation within
  // each segment (payoff is genuinely linear within each segment,
  // including the final segment out to farProbe, so interpolation finds
  // the EXACT crossing regardless of how far farProbe is).
  const boundaries = [0];
  for (let i = 0; i < points.length - 1; i++) {
    const [x0, x1] = [points[i], points[i + 1]];
    const [y0, y1] = [payoffs[i], payoffs[i + 1]];
    if (y0 * y1 < 0) {
      const t = y0 / (y0 - y1);
      boundaries.push(x0 + t * (x1 - x0));
    }
    boundaries.push(x1);
  }
  const sortedBoundaries = [...new Set(boundaries)].sort((a, b) => a - b);

  let popFraction = 0;
  for (let i = 0; i < sortedBoundaries.length - 1; i++) {
    const lower = sortedBoundaries[i];
    const upper = sortedBoundaries[i + 1];
    const mid = (lower + upper) / 2;
    const midPayoff = payoffAt(mid, pricedLegs);
    if (midPayoff > 0) {
      const upperK = upper === farProbe ? Infinity : upper;
      popFraction += cdf(upperK) - cdf(lower);
    }
  }
  // The region beyond farProbe (true infinity) carries whatever sign the
  // last segment had - already captured above since farProbe's segment
  // uses upperK = Infinity when its midpoint is profitable.
  return Math.max(0, Math.min(100, round2(popFraction * 100)));
}

// Full economics for one strategy: pricing, sizing, PoP, and 50%-target/
// stop-loss price triggers. legs: [{action:'buy'|'sell', type:'C'|'P',
// strike, expiry}]. Returns null fields gracefully (e.g. uncapped max
// profit) rather than guessing.
export function computeStrategyEconomics({
  legs,
  spot,
  ivUsedPct,
  sessionDate,
  expiration,
  portfolioSizeUsd,
  riskBudgetPct = 5,
}) {
  const sigma = Math.max(ivUsedPct, 0.01) / 100;
  const T = yearsToExpiry(sessionDate, expiration);
  const pricedLegs = legs.map((leg) => ({
    ...leg,
    price: bsPrice(leg.type, spot, leg.strike, T, sigma),
  }));

  let netPremiumPerShare = 0;
  for (const leg of pricedLegs) {
    netPremiumPerShare += (leg.action === 'sell' ? 1 : -1) * leg.price;
  }
  const creditOrDebit = netPremiumPerShare >= 0 ? 'credit' : 'debit';
  const amountPerContractUsd = round2(Math.abs(netPremiumPerShare) * 100);

  const strikes = [...new Set(legs.map((l) => l.strike))].sort((a, b) => a - b);
  const farProbe = strikes[strikes.length - 1] * 3 + 1000;
  const candidates = [0, ...strikes, farProbe];
  const payoffs = candidates.map((c) => payoffAt(c, pricedLegs));

  const minPayoff = Math.min(...payoffs);
  const maxPayoffFinite = Math.max(...payoffs.slice(0, -1));
  // If the far probe is still climbing past the highest strike's payoff,
  // profit is genuinely uncapped (e.g. a long call left uncovered on
  // that side) - report null rather than a made-up ceiling.
  const stillRisingAtFarProbe = payoffs[payoffs.length - 1] > maxPayoffFinite + 0.001;

  const maxLossPerContractUsd = round2(Math.max(0, -minPayoff) * 100);
  const maxProfitPerContractUsd = stillRisingAtFarProbe ? null : round2(Math.max(0, maxPayoffFinite) * 100);

  const popPct = computePoP(strikes, pricedLegs, spot, T, sigma);

  const safeMaxLoss = Math.max(maxLossPerContractUsd, 0.01);
  const contracts = Math.max(0, Math.floor(((portfolioSizeUsd * riskBudgetPct) / 100) / safeMaxLoss));
  const totalMaxLossUsd = round2(contracts * maxLossPerContractUsd);
  const totalMaxProfitUsd = maxProfitPerContractUsd === null ? null : round2(contracts * maxProfitPerContractUsd);

  // 50%-target and stop-loss, per spec: credit structures target 50% of
  // credit decayed; debit structures target 1.5x debit paid. Stop-loss
  // exits at 50% of max loss, price-based (the structural trigger stays
  // a model-written text field - that's a judgment call, not arithmetic).
  const profitBankedPerContractUsd =
    creditOrDebit === 'credit' ? round2(amountPerContractUsd / 2) : round2(amountPerContractUsd / 2);
  const stopLossBankedPerContractUsd = round2(maxLossPerContractUsd / 2);

  const profitTargetPriceTriggerUsd =
    creditOrDebit === 'credit'
      ? round2((amountPerContractUsd - profitBankedPerContractUsd) / 100)
      : round2((amountPerContractUsd + profitBankedPerContractUsd) / 100);
  const stopLossPriceTriggerUsd =
    creditOrDebit === 'credit'
      ? round2((amountPerContractUsd + stopLossBankedPerContractUsd) / 100)
      : round2(Math.max(0, amountPerContractUsd - stopLossBankedPerContractUsd) / 100);

  return {
    pricing: {
      credit_or_debit: creditOrDebit,
      amount_per_contract_usd: amountPerContractUsd,
      max_loss_per_contract_usd: maxLossPerContractUsd,
      max_profit_per_contract_usd: maxProfitPerContractUsd,
      pricing_source: 'black_scholes_estimate',
    },
    sizing: {
      risk_budget_pct: riskBudgetPct,
      contracts,
      total_max_loss_usd: totalMaxLossUsd,
      total_max_profit_usd: totalMaxProfitUsd,
    },
    profit_target_50pct: {
      trigger_price_usd: profitTargetPriceTriggerUsd,
      total_profit_usd: round2(contracts * profitBankedPerContractUsd),
    },
    stop_loss: {
      price_trigger_usd: stopLossPriceTriggerUsd,
      total_loss_at_stop_usd: round2(contracts * stopLossBankedPerContractUsd),
    },
    pop_pct: popPct,
  };
}

// Underlying (SPY) price levels at which a strategy's option-price exits
// would trigger - so the 50% profit target and the price-based stop can be
// drawn on a stock chart (TradingView export, PDF), not just quoted as a
// spread price.
//
// Assumption, stated plainly: levels are solved at the SAME time-to-expiry
// used for pricing (i.e. "if the move happens soon after entry"), with IV
// held constant. Time decay moves them:
//   - credit spreads: decay does part of the work, so later on the 50%
//     target needs LESS of a move than the line shows (it can even be hit
//     with no move at all);
//   - debit spreads: decay works against you, so later on the target needs
//     MORE of a move than the line shows.
// Returns the nearest crossing on each side of spot (null if none within
// +/-8%). Verticals normally cross on one side only; condors/flies can
// cross on both, or on neither (target reachable only through decay).
function positionValue(S, legs, T, sigma, creditOrDebit) {
  let net = 0;
  for (const leg of legs) {
    net += (leg.action === 'buy' ? 1 : -1) * bsPrice(leg.type, S, leg.strike, T, sigma);
  }
  // credit: what it costs to buy the position back; debit: what it sells for
  return creditOrDebit === 'credit' ? -net : net;
}

function nearestCrossings(fn, spot) {
  const step = spot * 0.0005;
  const maxSteps = Math.round(0.08 / 0.0005);
  const f0 = fn(spot);
  const found = { below: null, above: null };
  for (const dir of [-1, 1]) {
    let prevS = spot;
    let prevF = f0;
    for (let i = 1; i <= maxSteps; i++) {
      const S = spot + dir * i * step;
      const f = fn(S);
      if ((prevF <= 0 && f >= 0) || (prevF >= 0 && f <= 0)) {
        let lo = prevS, hi = S, flo = prevF;
        for (let k = 0; k < 50; k++) {
          const mid = (lo + hi) / 2;
          const fm = fn(mid);
          if ((flo <= 0 && fm <= 0) || (flo >= 0 && fm >= 0)) { lo = mid; flo = fm; } else { hi = mid; }
        }
        found[dir < 0 ? 'below' : 'above'] = round2((lo + hi) / 2);
        break;
      }
      prevS = S;
      prevF = f;
    }
  }
  return found;
}

export function computeExitUnderlyingLevels({
  legs, spot, ivUsedPct, sessionDate, expiration, creditOrDebit, profitTriggerUsd, stopTriggerUsd,
}) {
  const sigma = Math.max(ivUsedPct, 0.01) / 100;
  const T = yearsToExpiry(sessionDate, expiration);
  const solve = (target) => {
    if (typeof target !== 'number' || !Number.isFinite(target)) return [];
    const c = nearestCrossings((S) => positionValue(S, legs, T, sigma, creditOrDebit) - target, spot);
    return [c.below, c.above].filter((x) => x !== null);
  };
  return {
    profit_target: solve(profitTriggerUsd),
    stop: solve(stopTriggerUsd),
    assumption: 'solved at entry time-to-expiry, constant IV',
  };
}

// Implied move for the underlying by expiration: the market's 1-standard-
// deviation expected range, via the standard IV-formula method (spot *
// IV * sqrt(T)). Deliberately reuses yearsToExpiry() rather than a
// separate calendar-day calculation - this keeps the T used for the
// implied move identical to the T used to price every leg above, so the
// two numbers a session shows (strategy pricing and implied move) are
// never derived from two different time-to-expiry assumptions.
//
// This is the lognormal 1-sigma range under the SAME (S, sigma, T)
// Black-Scholes already assumes elsewhere in this file - not a separate,
// more-accurate straddle-priced figure (that would need real ATM chain
// prices, which this workflow doesn't fetch). Treat it as a consistency
// check against the GEX wall structure, not a market-calibrated quote.
export function computeImpliedMove({ spot, ivUsedPct, sessionDate, expiration }) {
  const sigma = Math.max(ivUsedPct, 0.01) / 100;
  const T = yearsToExpiry(sessionDate, expiration);
  const oneSigmaUsd = round2(spot * sigma * Math.sqrt(T));
  const oneSigmaPct = round2((oneSigmaUsd / spot) * 100);

  return {
    expected_range_usd: {
      low: round2(spot - oneSigmaUsd),
      high: round2(spot + oneSigmaUsd),
    },
    one_sigma_move_usd: oneSigmaUsd,
    one_sigma_move_pct: oneSigmaPct,
    iv_used_pct: ivUsedPct,
    session_date: sessionDate,
    expiration,
    method: 'iv_formula_1sigma',
  };
}
