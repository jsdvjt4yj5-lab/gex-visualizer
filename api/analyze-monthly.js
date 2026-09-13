// Route: POST /api/analyze-monthly
// Body: { snapshots: [ <parsed GEX JSON objects, same shape as parse-gex.js
//          output>, ... ] }
//
// Same idea as analyze-weekly.js but widened to a full trading month
// (~20-22 sessions) and framed as a regime summary rather than a
// day-by-day comparison. To keep the payload reasonable at this many
// snapshots, each day is condensed to its flip zone and top few walls
// before being sent - the model reasons over trend, not raw strike
// tables it would otherwise have to re-derive 20+ times.

const SYSTEM_PROMPT = `You are a concise options-flow analyst. You receive
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

  const { snapshots } = req.body || {};
  if (!Array.isArray(snapshots) || snapshots.length < 5) {
    return res.status(400).json({ error: 'Need at least 5 snapshots for a monthly read' });
  }

  const tickers = new Set(snapshots.map((s) => s.ticker));
  if (tickers.size > 1) {
    return res.status(400).json({
      error: `Snapshots span multiple tickers (${[...tickers].join(', ')}) - filter to one ticker before comparing`,
    });
  }

  try {
    // Condense each day to keep the payload reasonable at 20+ snapshots.
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
        system: SYSTEM_PROMPT,
        messages: [
          {
            role: 'user',
            content: `Give the monthly read for these ${condensed.length} daily snapshots of ${snapshots[0].ticker}:\n\n${JSON.stringify(condensed, null, 2)}`,
          },
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

    return res.status(200).json({
      ticker: snapshots[0].ticker,
      snapshot_count: snapshots.length,
      date_range: [snapshots[0].captured_at, snapshots[snapshots.length - 1].captured_at],
      analysis: textBlock.text.trim(),
      flip_zone_trend: condensed.map((c) => ({ captured_at: c.captured_at, flip_zone: c.flip_zone, spot_price: c.spot_price })),
    });
  } catch (err) {
    return res.status(500).json({ error: 'Server error', detail: String(err) });
  }
}
