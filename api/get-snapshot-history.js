// Route: GET /api/get-snapshot-history?action=history&ticker=SPY&days=35  (default action)
//        GET /api/get-snapshot-history?action=grade&ticker=SPY
//        GET /api/get-snapshot-history?action=weekly-summary&ticker=SPY
//
// Three actions in one file rather than three separate Vercel functions -
// Hobby plan caps deployments at 12 serverless functions (same reason
// get-ma.js/get-volume-profile.js were merged into get-price-data.js), so
// strategy-level backtesting had to fold into an existing route instead
// of adding new ones.
//
// history: reads gex_snapshots (unchanged from the original version of
//   this file) - the single source of truth for weekly/monthly GEX reads.
// grade: finds Coffee and Tea sessions (ct_sessions) whose expiration has
//   passed, walks actual daily closes from session date to expiry,
//   re-prices each recommended structure with Black-Scholes (using that
//   session's own stated IV assumption, so grading is against the
//   session's own logic, not a new assumption), and classifies each
//   strategy as a win (hit profit target, or finished profitable at
//   expiry) or loss (hit stop-loss, or finished unprofitable at expiry) -
//   whichever happens first chronologically. Writes results to ct_grades.
//   This grades win/loss, not exact fill-based P&L - there's no historical
//   live bid/ask to check against, only the same Black-Scholes estimate
//   pricing already used everywhere else in this app.
// weekly-summary: aggregates ct_grades from the last 7 days - overall win
//   rate, win rate by strategy name, and predicted-POP-vs-actual-win-rate
//   calibration (the number that actually says whether the GEX thesis's
//   confidence levels are trustworthy).

function getCloudflareCreds() {
  return {
    accountId: (process.env.CLOUDFLARE_ACCOUNT_ID || '').trim(),
    databaseId: (process.env.CLOUDFLARE_D1_DATABASE_ID || '').trim(),
    apiToken: (process.env.CLOUDFLARE_API_TOKEN || '').trim(),
  };
}

async function runD1Query(sql, params) {
  const { accountId, databaseId, apiToken } = getCloudflareCreds();
  if (!accountId || !databaseId || !apiToken) {
    throw new Error('Cloudflare D1 env vars are not fully configured');
  }
  const d1Res = await fetch(
    `https://api.cloudflare.com/client/v4/accounts/${accountId}/d1/database/${databaseId}/query`,
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${apiToken}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ sql, params }),
    }
  );
  if (!d1Res.ok) {
    throw new Error(`D1 HTTP error: ${await d1Res.text()}`);
  }
  const result = await d1Res.json();
  if (!result.success) {
    throw new Error(`D1 query error: ${JSON.stringify(result.errors)}`);
  }
  return result.result?.[0]?.results || [];
}

// ---- history (unchanged behavior) ----

async function handleHistory(req, res, ticker) {
  const { days } = req.query;
  const lookbackDays = Math.min(Math.max(parseInt(days, 10) || 35, 1), 90);

  const cutoff = new Date();
  cutoff.setDate(cutoff.getDate() - lookbackDays);
  const cutoffDate = cutoff.toISOString().slice(0, 10);

  const rows = await runD1Query(
    'SELECT session_date, gex_data_json FROM gex_snapshots WHERE ticker = ? AND session_date >= ? ORDER BY session_date ASC',
    [ticker, cutoffDate]
  );

  const snapshots = rows
    .map((row) => {
      try {
        return JSON.parse(row.gex_data_json);
      } catch (e) {
        return null;
      }
    })
    .filter(Boolean);

  return res.status(200).json({ ticker, snapshots });
}

// ---- Black-Scholes pricer, for grading ----

function erf(x) {
  // Abramowitz-Stegun approximation, ~1e-7 max error - plenty for grading.
  const sign = x < 0 ? -1 : 1;
  x = Math.abs(x);
  const a1 = 0.254829592, a2 = -0.284496736, a3 = 1.421413741,
        a4 = -1.453152027, a5 = 1.061405429, p = 0.3275911;
  const t = 1 / (1 + p * x);
  const y = 1 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * Math.exp(-x * x);
  return sign * y;
}
function normCdf(x) {
  return 0.5 * (1 + erf(x / Math.SQRT2));
}

// European option price via Black-Scholes. T in years; T=0 uses intrinsic
// value directly (avoids div-by-zero and matches expiry-day settlement).
function bsPrice(type, spot, strike, T, sigma, r, q) {
  if (T <= 0 || sigma <= 0) {
    return type === 'C' ? Math.max(spot - strike, 0) : Math.max(strike - spot, 0);
  }
  const sqrtT = Math.sqrt(T);
  const d1 = (Math.log(spot / strike) + (r - q + (sigma * sigma) / 2) * T) / (sigma * sqrtT);
  const d2 = d1 - sigma * sqrtT;
  if (type === 'C') {
    return spot * Math.exp(-q * T) * normCdf(d1) - strike * Math.exp(-r * T) * normCdf(d2);
  }
  return strike * Math.exp(-r * T) * normCdf(-d2) - spot * Math.exp(-q * T) * normCdf(-d1);
}

// Position value (per contract, $/share) = sum of each leg's current
// price, signed by direction (bought = asset, sold = liability). Self-
// consistent regardless of credit/debit labeling: comparing this value
// at entry vs. later gives P&L directly without needing to reason about
// which sign convention "credit" vs "debit" implies.
function positionValue(legs, spot, T, sigma, r, q) {
  return legs.reduce((sum, leg) => {
    const price = bsPrice(leg.type, spot, leg.strike, T, sigma, r, q);
    return sum + (leg.action === 'buy' ? price : -price);
  }, 0);
}

const RISK_FREE_RATE = 0.043;
const DIVIDEND_YIELD = 0.012;

async function fetchDailyCloses(ticker, fromDateStr, toDateStr) {
  const period1 = Math.floor(new Date(fromDateStr + 'T00:00:00Z').getTime() / 1000) - 86400;
  const period2 = Math.floor(new Date(toDateStr + 'T00:00:00Z').getTime() / 1000) + 86400;
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(ticker)}?period1=${period1}&period2=${period2}&interval=1d`;
  const yahooRes = await fetch(url, {
    headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' },
  });
  if (!yahooRes.ok) return [];
  const data = await yahooRes.json();
  const result = data?.chart?.result?.[0];
  if (!result) return [];
  const timestamps = result.timestamp || [];
  const closes = result.indicators?.quote?.[0]?.close || [];
  return timestamps
    .map((ts, i) => ({
      date: new Date(ts * 1000).toISOString().slice(0, 10),
      close: closes[i],
    }))
    .filter((d) => typeof d.close === 'number' && d.date >= fromDateStr && d.date <= toDateStr);
}

// Walks the daily closes chronologically, re-pricing the structure each
// day, and returns the first outcome to trigger: profit target, stop
// loss, or (if neither fires) the expiry-day result based on final
// intrinsic value vs. entry.
function gradeStrategy(strategy, sessionDate, expiration, dailyCloses, ivUsedPct) {
  const legs = strategy.legs || [];
  if (legs.length === 0) return null;

  const sigma = (ivUsedPct || 13) / 100;
  const expiryDate = new Date(expiration + 'T00:00:00Z');
  const entryDate = new Date(sessionDate + 'T00:00:00Z');
  const T_entry = Math.max((expiryDate - entryDate) / (1000 * 60 * 60 * 24 * 365), 0.0001);

  // Entry spot: approximate from the first available close on/after
  // session_date, since the exact intraday entry spot isn't stored.
  const entryRow = dailyCloses.find((d) => d.date >= sessionDate);
  if (!entryRow) return null;
  const entryValue = positionValue(legs, entryRow.close, T_entry, sigma, RISK_FREE_RATE, DIVIDEND_YIELD);

  const contracts = strategy.sizing?.contracts || 1;
  const profitTargetTotal = strategy.profit_target_50pct?.total_profit_usd ?? null;
  const stopLossTotal = strategy.stop_loss?.total_loss_at_stop_usd ?? null;

  const pathDays = dailyCloses.filter((d) => d.date > sessionDate && d.date <= expiration);

  for (const day of pathDays) {
    const T = Math.max((expiryDate - new Date(day.date + 'T00:00:00Z')) / (1000 * 60 * 60 * 24 * 365), 0);
    const value = positionValue(legs, day.close, T, sigma, RISK_FREE_RATE, DIVIDEND_YIELD);
    const pnlTotal = (value - entryValue) * 100 * contracts; // per-contract $ * 100 shares * contracts

    if (profitTargetTotal !== null && pnlTotal >= profitTargetTotal) {
      return { outcome: 'win', resolution_reason: 'profit_target', resolution_date: day.date };
    }
    if (stopLossTotal !== null && pnlTotal <= -stopLossTotal) {
      return { outcome: 'loss', resolution_reason: 'stop_loss', resolution_date: day.date };
    }
  }

  // Neither hit - resolve at expiry using the final available close.
  const finalRow = [...dailyCloses].reverse().find((d) => d.date <= expiration);
  if (!finalRow) return null;
  const finalValue = positionValue(legs, finalRow.close, 0, sigma, RISK_FREE_RATE, DIVIDEND_YIELD);
  const finalPnl = (finalValue - entryValue) * 100 * contracts;
  return {
    outcome: finalPnl > 0 ? 'win' : 'loss',
    resolution_reason: 'expiry',
    resolution_date: finalRow.date,
  };
}

async function handleGrade(req, res, ticker) {
  const today = new Date().toISOString().slice(0, 10);

  const sessions = await runD1Query(
    'SELECT session_date, expiration, output_json FROM ct_sessions WHERE ticker = ? AND expiration < ? ORDER BY session_date ASC',
    [ticker, today]
  );

  let gradedSessions = 0;
  let gradedStrategies = 0;
  const errors = [];

  for (const row of sessions) {
    let output;
    try {
      output = JSON.parse(row.output_json);
    } catch (e) {
      errors.push({ session_date: row.session_date, error: 'unparseable output_json' });
      continue;
    }
    const strategies = output.strategies || [];
    if (strategies.length === 0) continue;

    let dailyCloses;
    try {
      dailyCloses = await fetchDailyCloses(ticker, row.session_date, row.expiration);
    } catch (e) {
      errors.push({ session_date: row.session_date, error: `price fetch failed: ${e.message}` });
      continue;
    }
    if (dailyCloses.length === 0) {
      errors.push({ session_date: row.session_date, error: 'no price data for this window' });
      continue;
    }

    const ivUsedPct = output.volatility_check?.iv_used_pct;
    let sessionGraded = false;

    for (const strategy of strategies) {
      const grade = gradeStrategy(strategy, row.session_date, row.expiration, dailyCloses, ivUsedPct);
      if (!grade) continue;

      try {
        await runD1Query(
          'INSERT OR IGNORE INTO ct_grades (ticker, session_date, strategy_name, outcome, resolution_reason, resolution_date, predicted_pop, graded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
          [ticker, row.session_date, strategy.name, grade.outcome, grade.resolution_reason, grade.resolution_date, strategy.pop_pct ?? null, new Date().toISOString()]
        );
        gradedStrategies++;
        sessionGraded = true;
      } catch (e) {
        errors.push({ session_date: row.session_date, strategy: strategy.name, error: e.message });
      }
    }
    if (sessionGraded) gradedSessions++;
  }

  return res.status(200).json({
    ticker,
    sessions_checked: sessions.length,
    sessions_graded: gradedSessions,
    strategies_graded: gradedStrategies,
    errors,
  });
}

async function handleWeeklySummary(req, res, ticker) {
  const cutoff = new Date();
  cutoff.setDate(cutoff.getDate() - 7);
  const cutoffDate = cutoff.toISOString().slice(0, 10);

  const grades = await runD1Query(
    'SELECT strategy_name, outcome, resolution_reason, predicted_pop FROM ct_grades WHERE ticker = ? AND resolution_date >= ? ORDER BY resolution_date ASC',
    [ticker, cutoffDate]
  );

  if (grades.length === 0) {
    return res.status(200).json({
      ticker,
      graded_count: 0,
      message: 'No graded strategies in the last 7 days - run action=grade after some sessions have resolved (past their expiration).',
    });
  }

  const wins = grades.filter((g) => g.outcome === 'win');
  const losses = grades.filter((g) => g.outcome === 'loss');
  const winRatePct = +((wins.length / grades.length) * 100).toFixed(1);

  const byName = {};
  grades.forEach((g) => {
    if (!byName[g.strategy_name]) byName[g.strategy_name] = { total: 0, wins: 0 };
    byName[g.strategy_name].total++;
    if (g.outcome === 'win') byName[g.strategy_name].wins++;
  });
  const winRateByStrategy = Object.entries(byName).map(([name, s]) => ({
    strategy_name: name,
    total: s.total,
    win_rate_pct: +((s.wins / s.total) * 100).toFixed(1),
  }));

  // POP calibration: if predicted POP is trustworthy, average predicted
  // POP for winners should roughly track the actual win rate, and should
  // be meaningfully higher than the average predicted POP for losers.
  const avg = (arr) => arr.length ? +(arr.reduce((s, v) => s + v, 0) / arr.length).toFixed(1) : null;
  const winPops = wins.map((g) => g.predicted_pop).filter((v) => v !== null && v !== undefined);
  const lossPops = losses.map((g) => g.predicted_pop).filter((v) => v !== null && v !== undefined);

  return res.status(200).json({
    ticker,
    window_days: 7,
    graded_count: grades.length,
    win_rate_pct: winRatePct,
    wins: wins.length,
    losses: losses.length,
    win_rate_by_strategy: winRateByStrategy,
    pop_calibration: {
      avg_predicted_pop_for_wins: avg(winPops),
      avg_predicted_pop_for_losses: avg(lossPops),
      note: 'If the GEX thesis is well-calibrated, avg predicted POP for wins should track the actual win rate above, and should sit meaningfully higher than avg predicted POP for losses.',
    },
  });
}

export default async function handler(req, res) {
  if (req.method !== 'GET') {
    return res.status(405).json({ error: 'Use GET' });
  }

  const { ticker, action } = req.query;
  if (!ticker) {
    return res.status(400).json({ error: 'Missing "ticker" query param' });
  }
  const tickerUpper = ticker.toUpperCase();
  const resolvedAction = action || 'history';

  try {
    if (resolvedAction === 'history') return await handleHistory(req, res, tickerUpper);
    if (resolvedAction === 'grade') return await handleGrade(req, res, tickerUpper);
    if (resolvedAction === 'weekly-summary') return await handleWeeklySummary(req, res, tickerUpper);
    return res.status(400).json({ error: 'action must be "history", "grade", or "weekly-summary"' });
  } catch (err) {
    return res.status(500).json({ error: 'Server error', detail: String(err) });
  }
}
