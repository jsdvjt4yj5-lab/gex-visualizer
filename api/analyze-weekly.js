// Route: POST /api/analyze-weekly
// Body: { snapshots: [ <parsed GEX JSON objects, same shape as parse-gex.js output>, ... ] }
//
// Takes 2+ daily snapshots for the same ticker (ideally same expiration) and
// produces a comparison read: how walls have grown/shrunk, whether the flip
// zone has drifted, and what that implies heading toward the week's
// expiration. Snapshots should be sorted oldest -> newest by the caller.

const SYSTEM_PROMPT = `You are a concise options-flow analyst. You receive an
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

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Use POST' });
  }

  const { snapshots } = req.body || {};
  if (!Array.isArray(snapshots) || snapshots.length < 2) {
    return res.status(400).json({ error: 'Need at least 2 snapshots to compare' });
  }

  const tickers = new Set(snapshots.map((s) => s.ticker));
  if (tickers.size > 1) {
    return res.status(400).json({
      error: `Snapshots span multiple tickers (${[...tickers].join(', ')}) - filter to one ticker before comparing`,
    });
  }

  try {
    const compact = snapshots.map((s) => ({
      captured_at: s.captured_at,
      expiration: s.expiration,
      spot_price: s.spot_price,
      levels: s.levels,
    }));

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
            content: `Compare these ${compact.length} snapshots for ${snapshots[0].ticker}:\n\n${JSON.stringify(compact, null, 2)}`,
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

    // Also return a simple per-strike delta table (first vs last snapshot)
    // so the frontend can chart it without another AI call.
    const first = snapshots[0].levels;
    const last = snapshots[snapshots.length - 1].levels;
    const deltas = last.map((lvl) => {
      const prior = first.find((f) => f.strike === lvl.strike);
      return {
        strike: lvl.strike,
        first_value: prior ? prior.net_gex_millions : null,
        last_value: lvl.net_gex_millions,
        change: prior ? +(lvl.net_gex_millions - prior.net_gex_millions).toFixed(2) : null,
      };
    });

    return res.status(200).json({
      ticker: snapshots[0].ticker,
      snapshot_count: snapshots.length,
      date_range: [snapshots[0].captured_at, snapshots[snapshots.length - 1].captured_at],
      analysis: textBlock.text.trim(),
      deltas,
    });
  } catch (err) {
    return res.status(500).json({ error: 'Server error', detail: String(err) });
  }
}
