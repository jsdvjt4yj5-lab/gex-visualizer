// Route: GET /api/get-snapshot-history?ticker=SPY&days=35
// Returns: { ticker, snapshots: [ <full GEX snapshot JSON, same shape
//            parse-gex.js/get-tiger-gex.py return>, ... ] }, oldest first
//
// Reads back what's been written to the gex_snapshots D1 table (schema:
// ticker TEXT, session_date TEXT, gex_data_json TEXT, PRIMARY KEY
// (ticker, session_date)). D1 is the single source of truth for
// weekly/monthly reads as of this endpoint - parse-gex.js and
// get-tiger-gex.py both write every snapshot here now, so this replaces
// the old browser-localStorage-only history for that feature.
//
// "days" bounds how far back to look (default 35, comfortably covers the
// ~22-snapshot monthly read plus some slack) - capped at 90 to keep the
// D1 query and response payload bounded.

export default async function handler(req, res) {
  if (req.method !== 'GET') {
    return res.status(405).json({ error: 'Use GET' });
  }

  const { ticker, days } = req.query;
  if (!ticker) {
    return res.status(400).json({ error: 'Missing "ticker" query param' });
  }

  const lookbackDays = Math.min(Math.max(parseInt(days, 10) || 35, 1), 90);

  const accountId = (process.env.CLOUDFLARE_ACCOUNT_ID || '').trim();
  const databaseId = (process.env.CLOUDFLARE_D1_DATABASE_ID || '').trim();
  const apiToken = (process.env.CLOUDFLARE_API_TOKEN || '').trim();

  if (!accountId || !databaseId || !apiToken) {
    return res.status(500).json({ error: 'Cloudflare D1 env vars are not fully configured' });
  }

  try {
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - lookbackDays);
    const cutoffDate = cutoff.toISOString().slice(0, 10);

    const d1Res = await fetch(
      `https://api.cloudflare.com/client/v4/accounts/${accountId}/d1/database/${databaseId}/query`,
      {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${apiToken}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          sql: 'SELECT session_date, gex_data_json FROM gex_snapshots WHERE ticker = ? AND session_date >= ? ORDER BY session_date ASC',
          params: [ticker.toUpperCase(), cutoffDate],
        }),
      }
    );

    if (!d1Res.ok) {
      const errText = await d1Res.text();
      return res.status(502).json({ error: 'D1 query failed', detail: errText });
    }

    const d1Result = await d1Res.json();
    if (!d1Result.success) {
      return res.status(502).json({ error: 'D1 query returned an error', detail: d1Result.errors });
    }

    const rows = d1Result.result?.[0]?.results || [];
    const snapshots = rows
      .map((row) => {
        try {
          return JSON.parse(row.gex_data_json);
        } catch (e) {
          return null; // skip a corrupted row rather than failing the whole request
        }
      })
      .filter(Boolean);

    return res.status(200).json({ ticker: ticker.toUpperCase(), snapshots });
  } catch (err) {
    return res.status(500).json({ error: 'Server error', detail: String(err) });
  }
}
