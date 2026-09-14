// Route: POST /api/analyze-gex-period
// Body: { period: "weekly" | "monthly", snapshots: [ <parsed GEX JSON
//          objects, same shape as parse-gex.js output>, ... ] }
//
// Consolidates the former analyze-weekly.js and analyze-monthly.js into
// one endpoint (Vercel's Hobby plan caps deployments at 12 serverless
// functions - this merge, plus the equivalent flow merge, frees up two
// slots). Behavior is otherwise unchanged from the two original files:
// weekly compares 2+ recent same-expiration snapshots day-by-day; monthly
// widens to ~20+ snapshots across expirations and gives a regime summary
// instead of a day-by-day comparison.

const WEEKLY_PROMPT = `You are a concise options-flow analyst. You receive an
array of daily GEX (gamma exposure) snapshots for the SAME ticker, ordered
oldest to newest, each with a captured_at timestamp, spot price, and a list
of {strike, net_gex_millions} levels.

Produce a short written weekly read that compares the snapshots against each
other - not a description of any single day. Specifically:

- Identify whether the flip zone (where sign changes near spot) has moved
  over the period, and in which direction
- Call out any strikes where GEX magnitude has clearly grown or shrunk
  across the snapshots (building conviction vs unwinding)
- Note if spot price has moved toward or away from the major walls
- If the snapshots are approaching a Friday weekly expiration, mention that
  a large share of this gamma resets after that date and today's picture
  may not persist past it
- If fewer than 2 usable snapshots are provided, say so plainly instead of
  fabricating a trend
- Close with a short "Levels to watch" line: the level most likely to
  matter into the week's expiration (pick whichever level shows the
  clearest trend across the snapshots), what would CONFIRM the current
  trajectory continuing, and what would INVALIDATE it. Frame as structural
  observations, not instructions - never tell the reader to buy, sell, or
  take a specific action.

Keep it to 3 short paragraphs maximum, plus the closing "Levels to watch"
line. No headers except that one closing label, no bullet points otherwise.
This is not financial advice - describe structure and change only, never
phrase anything as a directive.`;

const MONTHLY_PROMPT = `You are a concise options-flow analyst. You receive
an array of condensed daily GEX (gamma exposure) snapshots for the SAME
ticker, ordered oldest to newest, spanning roughly a month. Each entry has
a captured_at timestamp, spot_price, flip_zone (the approximate strike
where gamma sign changes, if one existed that day), and top_walls (the 3-5
largest-magnitude strikes that day with their values).

Produce a short written MONTHLY read - a regime summary, not a day-by-day
recap. Specifically:

- Characterize the month's overall gamma regime: has it been
  consistently positive/pinned, consistently negative/volatile, or has it
  shifted from one to the other partway through? Name roughly when a
  shift happened if one did.
- Identify whether the flip zone (or the dominant wall, if no flip zone
  existed on most days) has trended toward a particular price level over
  the month, or bounced around without a clear direction
- Note any recurring strike that shows up as a top wall across MANY days
  in the month - a level that persists for weeks is structurally more
  significant than one that appears for a day or two
- Mention that weekly and monthly options expirations reset chunks of
  this gamma periodically, so persistence across a full month despite
  those resets is itself notable when it happens
- If fewer than 5 usable snapshots are provided, say so plainly instead of
  fabricating a month-long trend from too little data

Keep it to 3 short paragraphs maximum. Plain prose, no bullet points, no
headers. This is not financial advice - describe structure and change
only, never phrase anything as a directive.`;

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Use POST' });
  }

  const { period, snapshots } = req.body || {};
  if (period !== 'weekly' && period !== 'monthly') {
    return res.status(400).json({ error: 'period must be "weekly" or "monthly"' });
  }

  const minSnapshots = period === 'weekly' ? 2 : 5;
  if (!Array.isArray(snapshots) || snapshots.length < minSnapshots) {
    return res.status(400).json({ error: `Need at least ${minSnapshots} snapshots for a ${period} read` });
  }

  const tickers = new Set(snapshots.map((s) => s.ticker));
  if (tickers.size > 1) {
    return res.status(400).json({
      error: `Snapshots span multiple tickers (${[...tickers].join(', ')}) - filter to one ticker before comparing`,
    });
  }

  try {
    let userContent;
    let condensedForResponse = null;

    if (period === 'weekly') {
      const compact = snapshots.map((s) => ({
        captured_at: s.captured_at,
        expiration: s.expiration,
        spot_price: s.spot_price,
        levels: s.levels,
      }));
      userContent = `Compare these ${compact.length} snapshots for ${snapshots[0].ticker}:\n\n${JSON.stringify(compact, null, 2)}`;
    } else {
      const condensed = snapshots.map((s) => {
        const flipLevel = (s.levels || []).find((l) => l.is_flip_zone);
        const topWalls = [...(s.levels || [])]
          .sort((a, b) => Math.abs(b.net_gex_millions) - Math.abs(a.net_gex_millions))
          .slice(0, 5)
          .map((l) => ({ strike: l.strike, value: l.net_gex_millions }));
        return {
          captured_at: s.captured_at,
          spot_price: s.spot_price,
          flip_zone: flipLevel ? flipLevel.strike : null,
          top_walls: topWalls,
        };
      });
      condensedForResponse = condensed;
      userContent = `Give the monthly read for these ${condensed.length} daily snapshots of ${snapshots[0].ticker}:\n\n${JSON.stringify(condensed, null, 2)}`;
    }

    const anthropicRes = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': process.env.ANTHROPIC_API_KEY,
        'anthropic-version': '2023-06-01',
      },
      body: JSON.stringify({
        model: 'claude-sonnet-4-6',
        max_tokens: 700,
        system: period === 'weekly' ? WEEKLY_PROMPT : MONTHLY_PROMPT,
        messages: [{ role: 'user', content: userContent }],
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

    const response = {
      ticker: snapshots[0].ticker,
      snapshot_count: snapshots.length,
      date_range: [snapshots[0].captured_at, snapshots[snapshots.length - 1].captured_at],
      analysis: textBlock.text.trim(),
    };

    if (period === 'weekly') {
      // Per-strike delta table (first vs last snapshot) so the frontend
      // can chart it without another AI call.
      const first = snapshots[0].levels;
      const last = snapshots[snapshots.length - 1].levels;
      response.deltas = last.map((lvl) => {
        const prior = first.find((f) => f.strike === lvl.strike);
        return {
          strike: lvl.strike,
          first_value: prior ? prior.net_gex_millions : null,
          last_value: lvl.net_gex_millions,
          change: prior ? +(lvl.net_gex_millions - prior.net_gex_millions).toFixed(2) : null,
        };
      });
    } else {
      response.flip_zone_trend = condensedForResponse.map((c) => ({
        captured_at: c.captured_at,
        flip_zone: c.flip_zone,
        spot_price: c.spot_price,
      }));
    }

    return res.status(200).json(response);
  } catch (err) {
    return res.status(500).json({ error: 'Server error', detail: String(err) });
  }
}
