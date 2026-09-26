// Route: GET /api/get-snapshot-history?action=history&ticker=SPY&days=35  (default action)
//        GET /api/get-snapshot-history?action=grade&ticker=SPY
//        GET /api/get-snapshot-history?action=weekly-summary&ticker=SPY
//        GET /api/get-snapshot-history?action=implied-move-summary&ticker=SPY
//        GET /api/get-snapshot-history?action=tickers  (no ticker needed)
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
// implied-move-summary: read-only calibration check of the 1-sigma
//   implied move each Coffee and Tea session stores in
//   volatility_check.implied_move. For every resolved session, checks
//   whether the expiration close landed inside the range, and how big the
//   actual move was in units of the implied 1-sigma. Computed on demand
//   from ct_sessions + Yahoo closes - writes nothing, no new table.

// Formats any Date/timestamp as YYYY-MM-DD in US Eastern time. Used both
// for "today" (called with no argument) and for labeling historical
// price bars by their correct ET trading date, rather than whatever date
// UTC happens to assign them.
function easternDateStr(date) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York',
    year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(date || new Date());
  const get = (type) => parts.find((p) => p.type === type).value;
  return `${get('year')}-${get('month')}-${get('day')}`;
}

// Returns today's date (YYYY-MM-DD) in US Eastern time - NOT server-local
// time and NOT raw UTC. This app runs on the US market's own calendar, so
// "today" needs to mean the exchange's today regardless of where the
// request originates. Using plain `new Date().toISOString()` (UTC) here
// would disagree with the actual ET trading date for roughly 8pm-midnight
// ET every day, since UTC's calendar rolls over 4-5 hours before ET's
// does - risking grading a strategy as resolved before its expiration has
// actually happened, or comparing against the wrong day's snapshot.
function todayEasternDateStr() {
  return easternDateStr(new Date());
}

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
  const cutoffDate = easternDateStr(cutoff);

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
  const quote = result.indicators?.quote?.[0] || {};
  const closes = quote.close || [];
  const highs = quote.high || [];
  const lows = quote.low || [];
  return timestamps
    .map((ts, i) => ({
      date: easternDateStr(new Date(ts * 1000)),
      close: closes[i],
      // high/low feed the grader's entry-trigger check; null-safe.
      high: typeof highs[i] === 'number' ? highs[i] : null,
      low: typeof lows[i] === 'number' ? lows[i] : null,
    }))
    .filter((d) => typeof d.close === 'number' && d.date >= fromDateStr && d.date <= toDateStr);
}

// ---- Grader (v2) ----
// v1 priced the entry at the session day's CLOSE, so for same-day (0DTE)
// sessions entry and expiry used the same price and the grade measured
// leftover time value (a near-automatic win for credits, loss for debits),
// not the market move. v2:
//   - entry price = the premium Coffee and Tea actually quoted
//     (pricing.amount_per_contract_usd, priced at priced_at_underlying at
//     session time) - the same number the session's POP was computed from
//   - entry check: a trigger-priced strategy only counts as entered if a
//     day's high/low range reached its entry_level (session day through
//     the day before expiry, or expiry day itself for 0DTE). Coarse: the
//     day's range includes trading before the session ran, so this can
//     count an entry that happened before the trigger was even published.
//   - exits in the session's own rule order: invalidation level (closing
//     basis), 50% target, 50% stop - checked on daily closes after entry;
//     otherwise the expiry close. Intraday target/stop touches that
//     reversed by the close are NOT seen (daily bars only).
//   - estimated P&L per trade, and structure type from the legs.
// Time-to-expiry follows lib/options-pricing.js's convention (trading
// days = calendar days x 5/7, floor 1, / 252) so re-pricing stays
// consistent with how the session priced the trade.

function tradingYearsToExpiry(fromDate, expiration) {
  if (fromDate >= expiration) return 0;
  const calendarDays = (new Date(`${expiration}T00:00:00Z`) - new Date(`${fromDate}T00:00:00Z`)) / 86400000;
  return Math.max(calendarDays * (5 / 7), 1) / 252;
}

// Structure type from the legs, so "Bear Call Spread 775/780" and "Bear
// Call Spread" group together. Vocabulary matches Coffee and Tea's names.
function classifyStructure(legs) {
  const L = legs || [];
  const expiries = new Set(L.map((l) => l.expiry).filter(Boolean));
  if (expiries.size > 1) return 'Calendar / diagonal';
  const calls = L.filter((l) => l.type === 'C');
  const puts = L.filter((l) => l.type === 'P');
  const vertical = (pair) => {
    const [x, y] = [...pair].sort((m, n) => m.strike - n.strike);
    if (!x || !y || x.action === y.action) return null;
    const soldLower = x.action === 'sell';
    if (x.type === 'C') return soldLower ? 'Bear call spread' : 'Long call vertical';
    return soldLower ? 'Long put vertical' : 'Bull put spread';
  };
  if (L.length === 1) return L[0].action === 'buy' ? (L[0].type === 'C' ? 'Long call' : 'Long put') : 'Short single leg';
  if (L.length === 2 && (calls.length === 2 || puts.length === 2)) return vertical(L) || 'Other';
  if (L.length === 2 && calls.length === 1 && puts.length === 1) {
    const same = calls[0].strike === puts[0].strike;
    if (calls[0].action === 'buy' && puts[0].action === 'buy') return same ? 'Long straddle' : 'Long strangle';
    if (calls[0].action === 'sell' && puts[0].action === 'sell') return same ? 'Short straddle' : 'Short strangle';
    return 'Other';
  }
  if (L.length === 3 && (calls.length === 3 || puts.length === 3)) return 'Butterfly';
  if (L.length === 4 && calls.length === 2 && puts.length === 2) {
    const shortC = calls.find((l) => l.action === 'sell');
    const shortP = puts.find((l) => l.action === 'sell');
    if (shortC && shortP) return shortC.strike === shortP.strike ? 'Iron butterfly' : 'Iron condor';
    return 'Long iron condor';
  }
  return 'Other';
}

function gradeStrategy(strategy, sessionDate, expiration, days, ivUsedPct) {
  const legs = strategy.legs || [];
  if (legs.length === 0) return null;
  const sigma = (ivUsedPct || 13) / 100;
  const contracts = strategy.sizing?.contracts || 1;
  const pr = strategy.pricing || {};
  const base = { structure_type: classifyStructure(legs), credit_or_debit: pr.credit_or_debit || null };

  const window = days.filter((d) => d.date >= sessionDate && d.date <= expiration);
  if (window.length === 0) return null;

  // ---- Entry
  const entryLevel = typeof strategy.entry_level === 'number' ? strategy.entry_level : null;
  const triggerPriced = pr.priced_at === 'entry_trigger' && entryLevel !== null;
  let entryIdx = 0;
  let entryValue;
  let entryBasis;
  let entrySpot = typeof pr.priced_at_underlying === 'number' ? pr.priced_at_underlying : null;
  const storedValue = typeof pr.amount_per_contract_usd === 'number'
    ? (pr.credit_or_debit === 'credit' ? -1 : 1) * pr.amount_per_contract_usd / 100
    : null;

  if (triggerPriced) {
    // Entry days: session day through the day before expiry; for a 0DTE
    // session the only candidate is the session (= expiry) day itself.
    const isCandidate = (d) => sessionDate === expiration || d.date < expiration;
    entryIdx = window.findIndex((d) => isCandidate(d) && d.high !== null && d.low !== null
      && d.low <= entryLevel && entryLevel <= d.high);
    if (entryIdx === -1) {
      const noRange = window.filter(isCandidate).every((d) => d.high === null || d.low === null);
      return {
        ...base,
        outcome: 'not_entered',
        resolution_reason: noRange ? 'no_high_low_data' : 'trigger_not_reached',
        resolution_date: window[window.length - 1].date,
        entered: 0, entry_spot: null, entry_basis: 'entry_trigger', pnl_usd: 0,
      };
    }
    entrySpot = entryLevel;
    const entryDay = window[entryIdx].date;
    if (entryDay === sessionDate && storedValue !== null) {
      entryValue = storedValue;
      entryBasis = 'entry_trigger_session_price';
    } else {
      // Trigger first reached on a later day - re-price at the trigger
      // level with that day's time remaining (start of day: one trading
      // day more than at its close).
      const T = tradingYearsToExpiry(entryDay, expiration) + 1 / 252;
      entryValue = positionValue(legs, entryLevel, T, sigma, RISK_FREE_RATE, DIVIDEND_YIELD);
      entryBasis = 'entry_trigger_repriced_later_day';
    }
  } else if (storedValue !== null) {
    entryValue = storedValue;
    entryBasis = entrySpot !== null ? 'session_spot_price' : 'session_price';
  } else {
    // Very old session with no stored price - v1 fallback, flagged.
    entrySpot = window[0].close;
    entryValue = positionValue(legs, entrySpot, tradingYearsToExpiry(sessionDate, expiration), sigma, RISK_FREE_RATE, DIVIDEND_YIELD);
    entryBasis = 'approx_session_close';
  }

  const profitTargetTotal = strategy.profit_target_50pct?.total_profit_usd ?? null;
  const stopLossTotal = strategy.stop_loss?.total_loss_at_stop_usd ?? null;
  const inv = typeof strategy.invalidation_level === 'number' ? strategy.invalidation_level : null;
  // Which side of the invalidation level the trade lives on, judged from
  // the entry spot (a bull put spread entered at 770 with inv 765 is
  // invalidated by a close BELOW 765).
  const invBelow = inv !== null && entrySpot !== null && inv !== entrySpot ? inv < entrySpot : null;
  const entered = {
    ...base, entered: 1, entry_basis: entryBasis,
    entry_spot: entrySpot !== null ? Math.round(entrySpot * 100) / 100 : null,
  };
  const pnlAt = (close, T) => (positionValue(legs, close, T, sigma, RISK_FREE_RATE, DIVIDEND_YIELD) - entryValue) * 100 * contracts;

  // ---- Path: closes from the entry day up to (not including) expiry day,
  // in the session's own exit-rule order.
  for (let i = entryIdx; i < window.length; i++) {
    const day = window[i];
    if (day.date >= expiration || typeof day.close !== 'number') continue;
    const pnl = pnlAt(day.close, tradingYearsToExpiry(day.date, expiration));
    if (invBelow !== null && (invBelow ? day.close < inv : day.close > inv)) {
      return { ...entered, outcome: pnl > 0 ? 'win' : 'loss', resolution_reason: 'invalidation', resolution_date: day.date, pnl_usd: Math.round(pnl) };
    }
    if (profitTargetTotal !== null && pnl >= profitTargetTotal) {
      return { ...entered, outcome: 'win', resolution_reason: 'profit_target', resolution_date: day.date, pnl_usd: Math.round(profitTargetTotal) };
    }
    if (stopLossTotal !== null && pnl <= -stopLossTotal) {
      return { ...entered, outcome: 'loss', resolution_reason: 'stop_loss', resolution_date: day.date, pnl_usd: -Math.round(stopLossTotal) };
    }
  }

  // ---- Expiry: settle at intrinsic on the last close on/before expiry.
  const finalRow = [...window].reverse().find((d) => typeof d.close === 'number');
  if (!finalRow) return null;
  const finalPnl = pnlAt(finalRow.close, 0);
  return {
    ...entered,
    outcome: finalPnl > 0 ? 'win' : 'loss',
    resolution_reason: 'expiry',
    resolution_date: finalRow.date,
    pnl_usd: Math.round(finalPnl),
  };
}

// Adds v2 columns to ct_grades if missing. Idempotent: a "duplicate
// column" error just means it already ran.
const GRADE_V2_COLUMNS = [
  ['structure_type', 'TEXT'], ['credit_or_debit', 'TEXT'], ['entered', 'INTEGER'],
  ['entry_spot', 'REAL'], ['entry_basis', 'TEXT'], ['pnl_usd', 'REAL'], ['grader_version', 'INTEGER'],
];
async function ensureGradeV2Columns() {
  // One PRAGMA read normally; ALTERs only run the first time.
  let existing = null;
  try {
    const cols = await runD1Query('PRAGMA table_info(ct_grades)', []);
    existing = new Set(cols.map((c) => c.name));
  } catch (e) {
    existing = null; // fall through to try-every-ALTER
  }
  for (const [name, type] of GRADE_V2_COLUMNS) {
    if (existing && existing.has(name)) continue;
    try {
      await runD1Query(`ALTER TABLE ct_grades ADD COLUMN ${name} ${type}`, []);
    } catch (e) {
      if (!/duplicate column/i.test(e.message)) throw e;
    }
  }
}

async function handleGrade(req, res, ticker, { regrade = false } = {}) {
  const today = todayEasternDateStr();
  await ensureGradeV2Columns();
  // Re-grade clears this ticker's grades first - otherwise INSERT OR
  // IGNORE keeps every old (v1) grade forever.
  let cleared = 0;
  if (regrade) {
    const before = await runD1Query('SELECT COUNT(*) AS n FROM ct_grades WHERE ticker = ?', [ticker]);
    cleared = before[0]?.n ?? 0;
    await runD1Query('DELETE FROM ct_grades WHERE ticker = ?', [ticker]);
  }

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

    for (const [i, strategy] of strategies.entries()) {
      const grade = gradeStrategy(strategy, row.session_date, row.expiration, dailyCloses, ivUsedPct);
      if (!grade) continue;

      try {
        // "S1 <name>" keeps two same-named strategies in one session from
        // colliding on the table's key; grouping uses structure_type.
        await runD1Query(
          'INSERT OR IGNORE INTO ct_grades (ticker, session_date, strategy_name, outcome, resolution_reason, resolution_date, predicted_pop, graded_at, structure_type, credit_or_debit, entered, entry_spot, entry_basis, pnl_usd, grader_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
          [ticker, row.session_date, `S${i + 1} ${strategy.name}`, grade.outcome, grade.resolution_reason, grade.resolution_date,
            strategy.pop_pct ?? null, new Date().toISOString(), grade.structure_type, grade.credit_or_debit,
            grade.entered, grade.entry_spot, grade.entry_basis, grade.pnl_usd, 2]
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
    regraded: regrade,
    grades_cleared: cleared,
    sessions_checked: sessions.length,
    sessions_graded: gradedSessions,
    strategies_graded: gradedStrategies,
    errors,
  });
}

async function handleWeeklySummary(req, res, ticker) {
  const windowDays = Math.min(Math.max(parseInt(req.query.days, 10) || 7, 1), 90);
  const cutoff = new Date();
  cutoff.setDate(cutoff.getDate() - windowDays);
  const cutoffDate = easternDateStr(cutoff);

  await ensureGradeV2Columns();
  const rows = await runD1Query(
    'SELECT strategy_name, outcome, resolution_reason, predicted_pop, structure_type, credit_or_debit, entered, entry_basis, pnl_usd, grader_version FROM ct_grades WHERE ticker = ? AND resolution_date >= ? ORDER BY resolution_date ASC',
    [ticker, cutoffDate]
  );

  if (rows.length === 0) {
    return res.status(200).json({
      ticker, window_days: windowDays, graded_count: 0,
      message: `No graded strategies in the last ${windowDays} days - run grading after some sessions have resolved (past their expiration).`,
    });
  }

  const v1Rows = rows.filter((g) => g.grader_version !== 2).length;
  const notEntered = rows.filter((g) => g.outcome === 'not_entered');
  const grades = rows.filter((g) => g.outcome === 'win' || g.outcome === 'loss');
  const wins = grades.filter((g) => g.outcome === 'win');
  const losses = grades.filter((g) => g.outcome === 'loss');
  const pct = (a, b) => (b ? +((a / b) * 100).toFixed(1) : null);
  const avg = (arr) => (arr.length ? +(arr.reduce((s, v) => s + v, 0) / arr.length).toFixed(1) : null);
  const pnls = (arr) => arr.map((g) => g.pnl_usd).filter((v) => typeof v === 'number');
  const sum = (arr) => Math.round(arr.reduce((s, v) => s + v, 0));

  const group = (keyFn) => {
    const m = {};
    grades.forEach((g) => {
      const k = keyFn(g) || 'Unclassified (old grade)';
      (m[k] = m[k] || []).push(g);
    });
    return Object.entries(m)
      .map(([name, gs]) => {
        const w = gs.filter((g) => g.outcome === 'win');
        const l = gs.filter((g) => g.outcome === 'loss');
        return {
          name,
          total: gs.length,
          win_rate_pct: pct(w.length, gs.length),
          total_pnl_usd: sum(pnls(gs)),
          avg_win_usd: avg(pnls(w)),
          avg_loss_usd: avg(pnls(l)),
        };
      })
      .sort((a, b) => b.total - a.total);
  };

  const pops = (arr) => arr.map((g) => g.predicted_pop).filter((v) => typeof v === 'number');

  return res.status(200).json({
    ticker,
    window_days: windowDays,
    graded_count: grades.length,
    not_entered_count: notEntered.length,
    v1_grades_in_window: v1Rows, // >0: old-grader rows mixed in - re-grade to clear them
    win_rate_pct: pct(wins.length, grades.length),
    wins: wins.length,
    losses: losses.length,
    total_pnl_usd: sum(pnls(grades)),
    avg_win_usd: avg(pnls(wins)),
    avg_loss_usd: avg(pnls(losses)),
    by_structure: group((g) => g.structure_type),
    by_credit_debit: group((g) => g.credit_or_debit),
    pop_calibration: {
      avg_predicted_pop_all: avg(pops(grades)),
      avg_predicted_pop_for_wins: avg(pops(wins)),
      avg_predicted_pop_for_losses: avg(pops(losses)),
      note: 'Well-calibrated POP: the average predicted POP across entered trades should roughly match the actual win rate, and wins should carry higher predicted POP than losses. Under ~30 trades, gaps of 10+ points are still noise.',
    },
    grading_notes: "Estimated from daily bars and Black-Scholes at the session's IV, not real fills. Entry is detected from the day's high/low (can count entries from before the session ran); exits use closes only, so intraday target/stop touches that reversed are missed.",
  });
}

// ---- tickers (every ticker with data in D1, for the frontend dropdown) ----
// The ticker filter used to be built only from this browser's
// localStorage, so on another device it came up empty. This lists every
// ticker that has a GEX snapshot or a Coffee and Tea session in D1.
async function handleTickers(req, res) {
  const rows = await runD1Query(
    'SELECT ticker FROM gex_snapshots UNION SELECT ticker FROM ct_sessions', []
  );
  const tickers = [...new Set(rows.map((r) => r.ticker).filter(Boolean))].sort();
  return res.status(200).json({ tickers });
}

// ---- implied-move-summary (IV calibration check) ----
//
// If the IV fed into the implied move is well calibrated, the expiration
// close should land inside the 1-sigma range ~68.3% of the time, and the
// average |actual move| / one-sigma should be ~0.80 (E|Z| for a standard
// normal = sqrt(2/pi)). Ratio persistently above 0.80 -> IV has been too
// low (real moves bigger than priced); below -> IV too high.
//
// Caveat baked into the response: sessions in the same week share the
// same expiration, so observations overlap and aren't independent - the
// effective sample size is smaller than the raw session count.
const EXPECTED_HIT_RATE_PCT = 68.3;
const EXPECTED_ABS_Z = 0.8;

async function handleImpliedMoveSummary(req, res, ticker) {
  const today = todayEasternDateStr();
  const sessions = await runD1Query(
    'SELECT session_date, expiration, output_json FROM ct_sessions WHERE ticker = ? AND expiration < ? ORDER BY session_date ASC',
    [ticker, today]
  );

  const candidates = [];
  let withoutImpliedMove = 0;
  for (const row of sessions) {
    let output;
    try { output = JSON.parse(row.output_json); } catch (e) { continue; }
    const im = output.volatility_check?.implied_move;
    const low = im?.expected_range_usd?.low;
    const high = im?.expected_range_usd?.high;
    if (typeof low !== 'number' || typeof high !== 'number' || high <= low) {
      withoutImpliedMove++;
      continue;
    }
    candidates.push({
      session_date: row.session_date,
      expiration: row.expiration,
      low,
      high,
      spot: (low + high) / 2, // range is symmetric around the spot it was computed from
      one_sigma: (high - low) / 2,
      iv_used_pct: output.volatility_check?.iv_used_pct ?? null,
      iv_source: output.volatility_check?.iv_source ?? 'unknown',
    });
  }

  if (candidates.length === 0) {
    return res.status(200).json({
      ticker,
      sessions_checked: sessions.length,
      sessions_scored: 0,
      message: 'No resolved sessions with a stored implied move yet - only sessions run after the implied-move feature shipped carry one, and they need their expiration to pass.',
    });
  }

  // One Yahoo fetch covering every window, rather than one per session.
  const from = candidates[0].session_date;
  const through = candidates.reduce((m, c) => (c.expiration > m ? c.expiration : m), candidates[0].expiration);
  const closes = await fetchDailyCloses(ticker, from, through);
  if (closes.length === 0) {
    return res.status(502).json({ error: 'No price data returned for the scoring window', from, through });
  }

  const scored = [];
  const unscored = [];
  for (const c of candidates) {
    // Close ON expiration, or the last close before it (holiday/early-close case).
    const onOrBefore = closes.filter((d) => d.date <= c.expiration && d.date >= c.session_date);
    const final = onOrBefore[onOrBefore.length - 1];
    if (!final) { unscored.push({ session_date: c.session_date, reason: 'no close in window' }); continue; }
    const z = (final.close - c.spot) / c.one_sigma;
    scored.push({
      ...c,
      close_date: final.date,
      close: +final.close.toFixed(2),
      inside: final.close >= c.low && final.close <= c.high,
      z: +z.toFixed(2),
    });
  }

  const summarize = (rows) => {
    if (rows.length === 0) return null;
    const hits = rows.filter((r) => r.inside).length;
    const absZ = rows.reduce((s, r) => s + Math.abs(r.z), 0) / rows.length;
    return {
      n: rows.length,
      hit_rate_pct: +((hits / rows.length) * 100).toFixed(1),
      avg_abs_z: +absZ.toFixed(2),
      closes_above_range: rows.filter((r) => r.close > r.high).length,
      closes_below_range: rows.filter((r) => r.close < r.low).length,
    };
  };

  const bySource = {};
  for (const r of scored) (bySource[r.iv_source] ||= []).push(r);
  const bySourceSummary = Object.fromEntries(Object.entries(bySource).map(([k, v]) => [k, summarize(v)]));

  const overall = summarize(scored);
  let verdict = 'insufficient data';
  if (overall && overall.n >= 20) {
    if (overall.avg_abs_z > EXPECTED_ABS_Z * 1.15) verdict = 'IV looks too LOW - realized moves are larger than the implied range priced';
    else if (overall.avg_abs_z < EXPECTED_ABS_Z * 0.85) verdict = 'IV looks too HIGH - realized moves are smaller than the implied range priced';
    else verdict = 'IV looks roughly calibrated';
  }

  return res.status(200).json({
    ticker,
    sessions_checked: sessions.length,
    sessions_without_implied_move: withoutImpliedMove,
    sessions_scored: scored.length,
    expected: { hit_rate_pct: EXPECTED_HIT_RATE_PCT, avg_abs_z: EXPECTED_ABS_Z },
    overall,
    by_iv_source: bySourceSummary,
    verdict,
    caveat: 'Sessions in the same week share an expiration, so observations overlap and are not independent. Treat fewer than ~20 scored sessions as directional only.',
    sessions: scored.map((r) => ({
      session_date: r.session_date, expiration: r.expiration, iv_source: r.iv_source,
      range: [r.low, r.high], close_date: r.close_date, close: r.close, inside: r.inside, z: r.z,
    })),
    unscored,
  });
}

// ---- delete (removes a wrongly-uploaded/wrongly-analysed snapshot) ----

async function handleDelete(req, res, ticker, sessionDate) {
  if (!sessionDate) {
    return res.status(400).json({ error: 'Missing "session_date" query param (YYYY-MM-DD)' });
  }
  await runD1Query(
    'DELETE FROM gex_snapshots WHERE ticker = ? AND session_date = ?',
    [ticker, sessionDate]
  );
  return res.status(200).json({ ticker, session_date: sessionDate, deleted: true });
}

export default async function handler(req, res) {
  if (req.method === 'DELETE') {
    const { ticker, session_date } = req.query;
    if (!ticker) {
      return res.status(400).json({ error: 'Missing "ticker" query param' });
    }
    try {
      return await handleDelete(req, res, ticker.toUpperCase(), session_date);
    } catch (err) {
      return res.status(500).json({ error: 'Server error', detail: String(err) });
    }
  }

  if (req.method !== 'GET') {
    return res.status(405).json({ error: 'Use GET or DELETE' });
  }

  const { ticker, action } = req.query;
  if (action === 'tickers') {
    try {
      return await handleTickers(req, res);
    } catch (err) {
      return res.status(500).json({ error: 'Server error', detail: String(err) });
    }
  }
  if (!ticker) {
    return res.status(400).json({ error: 'Missing "ticker" query param' });
  }
  const tickerUpper = ticker.toUpperCase();
  const resolvedAction = action || 'history';

  try {
    if (resolvedAction === 'history') return await handleHistory(req, res, tickerUpper);
    if (resolvedAction === 'grade') return await handleGrade(req, res, tickerUpper);
    if (resolvedAction === 'regrade') return await handleGrade(req, res, tickerUpper, { regrade: true });
    if (resolvedAction === 'weekly-summary') return await handleWeeklySummary(req, res, tickerUpper);
    if (resolvedAction === 'implied-move-summary') return await handleImpliedMoveSummary(req, res, tickerUpper);
    return res.status(400).json({ error: 'action must be "history", "grade", "regrade", "weekly-summary", or "implied-move-summary"' });
  } catch (err) {
    return res.status(500).json({ error: 'Server error', detail: String(err) });
  }
}
