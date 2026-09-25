// Route: POST /api/get-macro-events
// Body: { session_date: "2026-09-23", expiration: "2026-09-30" }
//   (treated as the start/end of the range to cover, inclusive - the
//   frontend sends today -> end of the trading month)
// Returns: {
//   macro_events: [ { event, date, time_et, detail, importance, source }, ... ],
//   sources: { fred, fomc, treasury }  - per-source ok/count/error
// }
//
// HYBRID CALENDAR (replaces the old "ask the model to web-search the whole
// month" approach, which came back sparse/empty and then got cached blank):
//
//   1. FRED (St. Louis Fed API v1, fred/releases/dates) - official scheduled
//      release dates for US economic data (jobs, CPI, PCE, GDP, claims,
//      durables...), published months ahead. Deterministic, free. Needs
//      FRED_API_KEY in Vercel env vars. FRED gives dates, not times - the
//      time shown is each release's usual clock time (see FRED_RELEASES).
//   2. FOMC - the Fed's own published meeting calendar, hardcoded below
//      (FOMC_MEETINGS). Update once a year when the Fed publishes the next
//      year's dates (usually mid-year).
//   3. Treasury auctions - Fiscal Data API (no key), notes/bonds only. Only
//      auctions Treasury has already ANNOUNCED appear (~1 week ahead).
//
// AI web search REMOVED (previously source 4, a capped Haiku search for Fed
// speeches, geopolitics and foreign central banks). This file now makes no
// model call at all - every event comes from FRED, the FOMC table, or
// Treasury's Fiscal Data API. Trade-off: non-data events (Fed speakers,
// BoJ/ECB decisions, political events) no longer appear automatically.
//
// Sources run in parallel; any one failing never blanks the others. The
// response reports each source's status so the UI/PDF can say what's missing.

// ---------- 1. FRED ----------

// Which FRED releases matter for an SPY options session, matched by release
// NAME (more robust than hardcoding IDs). First match wins, so the more
// specific patterns come first. Times are the release's standard ET time.
const FRED_RELEASES = [
  { re: /^Employment Situation$/i, label: 'Jobs report (nonfarm payrolls, unemployment)', time: '08:30', importance: 'high' },
  { re: /^Consumer Price Index$/i, label: 'CPI', time: '08:30', importance: 'high' },
  { re: /^Producer Price Index/i, label: 'PPI', time: '08:30', importance: 'high' },
  { re: /^Personal Income and Outlays$/i, label: 'PCE inflation / personal income & spending', time: '08:30', importance: 'high' },
  { re: /^Gross Domestic Product$/i, label: 'GDP', time: '08:30', importance: 'high' },
  { re: /Advance Monthly Sales for Retail/i, label: 'Retail sales', time: '08:30', importance: 'high' },
  { re: /Unemployment Insurance Weekly Claims/i, label: 'Initial jobless claims', time: '08:30', importance: 'medium' },
  { re: /Durable Goods/i, label: 'Durable goods orders', time: '08:30', importance: 'medium' },
  { re: /^New Residential Construction$/i, label: 'Housing starts & building permits', time: '08:30', importance: 'medium' },
  { re: /Employment Cost Index/i, label: 'Employment Cost Index', time: '08:30', importance: 'medium' },
  { re: /ADP National Employment/i, label: 'ADP employment', time: '08:15', importance: 'medium' },
  { re: /Job Openings and Labor Turnover/i, label: 'JOLTS job openings', time: '10:00', importance: 'medium' },
  { re: /Surveys of Consumers/i, label: 'UMich consumer sentiment', time: '10:00', importance: 'medium' },
  { re: /Industrial Production and Capacity Utilization/i, label: 'Industrial production', time: '09:15', importance: 'medium' },
  { re: /International Trade in Goods and Services/i, label: 'Trade balance', time: '08:30', importance: 'low' },
  { re: /Advance Economic Indicators/i, label: 'Advance economic indicators (goods trade, inventories)', time: '08:30', importance: 'low' },
  { re: /Import and Export Price/i, label: 'Import/export prices', time: '08:30', importance: 'low' },
  { re: /Productivity and Costs/i, label: 'Productivity & unit labor costs', time: '08:30', importance: 'low' },
  { re: /^New Residential Sales$/i, label: 'New home sales', time: '10:00', importance: 'low' },
  { re: /Existing Home Sales/i, label: 'Existing home sales', time: '10:00', importance: 'low' },
  { re: /Construction Spending/i, label: 'Construction spending', time: '10:00', importance: 'low' },
  { re: /Manufacturers' Shipments, Inventories/i, label: 'Factory orders', time: '10:00', importance: 'low' },
];

function todayEastern() {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(new Date());
}

async function fetchFred(start, end) {
  const key = (process.env.FRED_API_KEY || '').trim();
  if (!key) return { ok: false, events: [], error: 'FRED_API_KEY not set in Vercel env vars' };

  // realtime_start bounds which release dates come back. Clamped to today
  // (ET) so it's never in the future; results are filtered to [start, end]
  // below regardless. include_release_dates_with_no_data=true is what makes
  // FRED return SCHEDULED future dates, not just ones already published.
  const today = todayEastern();
  const realtimeStart = start < today ? start : today;
  const collected = [];
  try {
    for (let offset = 0, page = 0; page < 5; page++, offset += 1000) {
      const qs = new URLSearchParams({
        api_key: key,
        file_type: 'json',
        realtime_start: realtimeStart,
        realtime_end: end,
        include_release_dates_with_no_data: 'true',
        order_by: 'release_date',
        sort_order: 'asc',
        limit: '1000',
        offset: String(offset),
      });
      const r = await fetch(`https://api.stlouisfed.org/fred/releases/dates?${qs}`);
      if (!r.ok) {
        const body = await r.text();
        return { ok: false, events: [], error: `FRED HTTP ${r.status}: ${body.slice(0, 200)}` };
      }
      const j = await r.json();
      const rows = j.release_dates || [];
      collected.push(...rows);
      if (rows.length < 1000) break;
    }
  } catch (err) {
    return { ok: false, events: [], error: `FRED request failed: ${String(err)}` };
  }

  const events = [];
  const unmatchedNames = new Set();
  for (const row of collected) {
    if (!row.date || row.date < start || row.date > end) continue;
    const match = FRED_RELEASES.find((m) => m.re.test(row.release_name || ''));
    if (!match) { unmatchedNames.add(row.release_name); continue; }
    events.push({
      event: match.label,
      date: row.date,
      time_et: match.time,
      detail: `Official schedule (FRED: ${row.release_name}); time is the usual release time.`,
      importance: match.importance,
      source: 'fred',
    });
  }
  return { ok: true, events, raw_count: collected.length, ignored_release_count: unmatchedNames.size };
}

// ---------- 2. FOMC ----------

// Source: federalreserve.gov/monetarypolicy/fomccalendars.htm (checked
// 2026-09-23). Each entry is the DECISION day (second day of the meeting).
// sep: true = Summary of Economic Projections / dot plot at that meeting.
// Add the next year's dates here once the Fed publishes them.
const FOMC_MEETINGS = [
  { decision: '2026-01-28', sep: false }, { decision: '2026-03-18', sep: true },
  { decision: '2026-04-29', sep: false }, { decision: '2026-06-17', sep: true },
  { decision: '2026-07-29', sep: false }, { decision: '2026-09-16', sep: true },
  { decision: '2026-10-28', sep: false }, { decision: '2026-12-09', sep: true },
  { decision: '2027-01-27', sep: false }, { decision: '2027-03-17', sep: true },
  { decision: '2027-04-28', sep: false }, { decision: '2027-06-09', sep: true },
  { decision: '2027-07-28', sep: false }, { decision: '2027-09-15', sep: true },
  { decision: '2027-10-27', sep: false }, { decision: '2027-12-08', sep: true },
];

function addDays(dateStr, n) {
  const d = new Date(dateStr + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

function fomcEvents(start, end) {
  const events = [];
  for (const m of FOMC_MEETINGS) {
    if (m.decision >= start && m.decision <= end) {
      events.push({
        event: `FOMC rate decision${m.sep ? ' + projections (dot plot)' : ''}`,
        date: m.decision,
        time_et: '14:00',
        detail: 'Statement 2:00 PM ET, Chair press conference 2:30 PM ET.',
        importance: 'high',
        source: 'fomc',
      });
    }
    // Minutes come out three weeks after the decision (Wednesday, 2:00 PM ET).
    const minutes = addDays(m.decision, 21);
    if (minutes >= start && minutes <= end) {
      events.push({
        event: 'FOMC minutes',
        date: minutes,
        time_et: '14:00',
        detail: `Minutes of the ${m.decision} meeting (three weeks after the decision).`,
        importance: 'medium',
        source: 'fomc',
      });
    }
  }
  const lastKnown = FOMC_MEETINGS[FOMC_MEETINGS.length - 1].decision;
  return { ok: true, events, stale_warning: end > lastKnown ? `FOMC list ends ${lastKnown} - add next year's dates` : null };
}

// ---------- 3. Treasury auctions ----------

async function fetchTreasuryAuctions(start, end) {
  const qs = new URLSearchParams({
    fields: 'security_type,security_term,auction_date,announcemt_date,offering_amt,reopening',
    filter: `auction_date:gte:${start},auction_date:lte:${end},security_type:in:(Note,Bond)`,
    sort: 'auction_date',
    'page[size]': '100',
  });
  try {
    const r = await fetch(`https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query?${qs}`);
    if (!r.ok) return { ok: false, events: [], error: `Treasury HTTP ${r.status}` };
    const j = await r.json();
    const events = (j.data || []).map((a) => {
      const amt = Number(a.offering_amt);
      const amtStr = Number.isFinite(amt) && amt > 0 ? ` - $${(amt / 1e9).toFixed(0)}B` : '';
      return {
        event: `${a.security_term} ${a.security_type} auction${a.reopening === 'Yes' ? ' (reopening)' : ''}`,
        date: a.auction_date,
        time_et: '13:00',
        detail: `Announced ${a.announcemt_date}${amtStr}. Results ~1:00 PM ET; a weak auction can move yields.`,
        importance: /^(10|20|30)-Year/.test(a.security_term || '') ? 'medium' : 'low',
        source: 'treasury',
      };
    });
    return { ok: true, events };
  } catch (err) {
    return { ok: false, events: [], error: `Treasury request failed: ${String(err)}` };
  }
}

// ---------- handler ----------

const IMPORTANCE_RANK = { high: 0, medium: 1, low: 2 };

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Use POST' });
  }
  const { session_date, expiration } = req.body || {};
  const isDate = (s) => typeof s === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(s);
  if (!isDate(session_date) || !isDate(expiration) || expiration < session_date) {
    return res.status(400).json({ error: 'Need session_date and expiration as YYYY-MM-DD, expiration >= session_date' });
  }
  const start = session_date;
  const end = expiration;

  const [fred, treasury] = await Promise.all([
    fetchFred(start, end),
    fetchTreasuryAuctions(start, end),
  ]);
  const fomc = fomcEvents(start, end);

  const all = [...fred.events, ...fomc.events, ...treasury.events];
  const seen = new Set();
  const macro_events = all
    .filter((e) => {
      const k = `${e.date}|${e.event.toLowerCase()}`;
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    })
    .sort((a, b) =>
      a.date.localeCompare(b.date) ||
      (a.time_et || '99:99').localeCompare(b.time_et || '99:99') ||
      IMPORTANCE_RANK[a.importance] - IMPORTANCE_RANK[b.importance]);

  const status = (s) => ({ ok: s.ok, count: s.events.length, ...(s.error ? { error: s.error } : {}) });
  return res.status(200).json({
    macro_events,
    range: { start, end },
    sources: {
      fred: status(fred),
      fomc: { ...status(fomc), ...(fomc.stale_warning ? { warning: fomc.stale_warning } : {}) },
      treasury: status(treasury),
    },
  });
}
