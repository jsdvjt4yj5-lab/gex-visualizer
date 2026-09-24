// Route: POST /api/get-macro-events
// Body: { session_date: "2026-09-15", expiration: "2026-09-18" }
// Returns: { macro_events: [ { event, date, time_et, detail }, ... ], source: "fred", ... }
//
// FRED-only rebuild. Replaces the previous version, which used Claude +
// web search to DISCOVER events (expensive, and unreliable - one call
// spiked to ~192K tokens from compounding searches). This version makes
// no model call at all: it asks FRED (St. Louis Fed, free, no billing)
// for every release scheduled in the window and keeps only the ones on
// the watchlist below.
//
// Response shape is unchanged ({ macro_events: [{event, date, time_et,
// detail}] }), so index.html and coffee-and-tea.js need no changes.
//
// Requires a free FRED API key in the Vercel env var FRED_API_KEY
// (register at fredaccount.stlouisfed.org).
//
// KNOWN COVERAGE GAPS vs. the old web-search version - these are NOT on
// FRED and will no longer appear: ISM PMI, Treasury auctions, BoJ
// decisions. FRED also provides no release TIMES and no consensus
// forecasts - time_et below comes from a fixed table of each release's
// standard publication time, labeled as typical rather than confirmed.

// Matched by release NAME rather than numeric release_id, so a wrong
// guessed ID can't silently drop an event. startsWith + exclude keeps
// look-alike releases (e.g. "Gross Domestic Product by State") out.
const WATCHLIST = [
  { startsWith: 'fomc press release', label: 'FOMC rate decision', time_et: '14:00', exclude: [] },
  { startsWith: 'consumer price index', label: 'CPI', time_et: '08:30', exclude: [] },
  { startsWith: 'producer price index', label: 'PPI', time_et: '08:30', exclude: [] },
  { startsWith: 'employment situation', label: 'Jobs report (nonfarm payrolls)', time_et: '08:30', exclude: ['veterans', 'state'] },
  { startsWith: 'unemployment insurance weekly claims', label: 'Initial jobless claims', time_et: '08:30', exclude: [] },
  { startsWith: 'advance monthly sales for retail', label: 'Retail sales', time_et: '08:30', exclude: [] },
  { startsWith: 'gross domestic product', label: 'GDP', time_et: '08:30', exclude: ['state', 'county', 'metro', 'industry', ' by '] },
  { startsWith: 'personal income and outlays', label: 'PCE / personal income & spending', time_et: '08:30', exclude: ['state', 'county'] },
  { startsWith: 'job openings and labor turnover', label: 'JOLTS', time_et: '10:00', exclude: ['state'] },
];

const FRED_URL = 'https://api.stlouisfed.org/fred/releases/dates';
const PAGE_LIMIT = 1000; // FRED's max per request for this endpoint
const MAX_PAGES = 5;     // a 1-2 week window is normally well under one page

function matchWatchlist(releaseName) {
  const name = (releaseName || '').toLowerCase().trim();
  return WATCHLIST.find(
    (w) => name.startsWith(w.startsWith) && !w.exclude.some((x) => name.includes(x))
  ) || null;
}

async function fetchFredReleaseDates(apiKey, start, end) {
  const rows = [];
  for (let page = 0; page < MAX_PAGES; page++) {
    const params = new URLSearchParams({
      api_key: apiKey,
      file_type: 'json',
      realtime_start: start,
      realtime_end: end,
      include_release_dates_with_no_data: 'true', // needed to see FUTURE scheduled dates
      order_by: 'release_date',
      sort_order: 'asc',
      limit: String(PAGE_LIMIT),
      offset: String(page * PAGE_LIMIT),
    });
    const res = await fetch(`${FRED_URL}?${params}`);
    if (!res.ok) {
      const detail = await res.text();
      const err = new Error(`FRED request failed (${res.status})`);
      err.detail = detail;
      throw err;
    }
    const data = await res.json();
    const batch = data.release_dates || [];
    rows.push(...batch);
    const total = typeof data.count === 'number' ? data.count : rows.length;
    if (batch.length < PAGE_LIMIT || rows.length >= total) break;
  }
  return rows;
}

const isIsoDate = (s) => typeof s === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(s);

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Use POST' });
  }

  const { session_date, expiration } = req.body || {};
  if (!isIsoDate(session_date) || !isIsoDate(expiration)) {
    return res.status(400).json({ error: 'session_date and expiration must be YYYY-MM-DD' });
  }
  if (expiration < session_date) {
    return res.status(400).json({ error: 'expiration is before session_date' });
  }

  const apiKey = (process.env.FRED_API_KEY || '').trim();
  if (!apiKey) {
    return res.status(500).json({
      error: 'FRED_API_KEY is not set - add a free FRED API key to Vercel environment variables',
    });
  }

  try {
    const rows = await fetchFredReleaseDates(apiKey, session_date, expiration);

    const seen = new Set();
    const macroEvents = [];
    for (const row of rows) {
      const date = row.date;
      if (!isIsoDate(date) || date < session_date || date > expiration) continue;
      const w = matchWatchlist(row.release_name);
      if (!w) continue;
      const key = `${w.label}|${date}`;
      if (seen.has(key)) continue; // same event matched by more than one FRED release name
      seen.add(key);
      macroEvents.push({
        event: w.label,
        date,
        time_et: w.time_et,
        detail: `FRED release: ${row.release_name}. Time is the typical release time, not confirmed.`,
      });
    }

    macroEvents.sort((a, b) => (a.date + a.time_et).localeCompare(b.date + b.time_et));

    return res.status(200).json({
      macro_events: macroEvents,
      source: 'fred',
      window: { start: session_date, end: expiration },
      fred_rows_scanned: rows.length,
      coverage_note: 'FRED-only: ISM PMI, Treasury auctions and BoJ are not covered; times are typical, not confirmed.',
    });
  } catch (err) {
    return res.status(502).json({
      error: 'FRED macro calendar fetch failed',
      detail: err.detail || String(err),
    });
  }
}
