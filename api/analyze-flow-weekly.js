// Route: POST /api/analyze-flow-weekly
// Body: { snapshots: [ <computed flow stats objects from the frontend CSV
//          parser, same shape as sent to analyze-flow.js>, ... ] }
//
// Takes 2+ daily flow snapshots for the same ticker (oldest to newest) and
// produces a comparison read: whether put/call skew, aggression, and
// conviction clusters are building, fading, or reversing across the week.
// Mirrors analyze-weekly.js (the GEX weekly comparison), same division of
// labor - the frontend already computed each day's stats correctly, this
// endpoint only compares them and writes the narrative.

const SYSTEM_PROMPT = `You are a concise options-flow analyst. You receive
an array of daily EOD flow statistics for the SAME ticker, ordered oldest
to newest. Each entry has: trade_count, call_premium, put_premium,
put_call_ratio_pct, sweep_premium, block_premium, aggressive_buy_premium,
aggressive_sell_premium, and top_clusters (repeated trades at specific
strike/expiration combinations).

Produce a short written weekly flow read that compares the days against
each other - not a description of any single day. Specifically:

- State whether the put/call skew has been building, fading, or reversing
  across the days provided (e.g. "put skew climbed from 58% to 75% over
  three sessions")
- State whether aggression (aggressive buy vs sell premium) has been
  trending more urgent or more passive across the period
- Check whether any strike/expiration cluster appears across MULTIPLE
  days' top_clusters - a cluster that repeats day after day is a much
  stronger signal than one that appears once and vanishes. Name it
  specifically if one exists.
- If fewer than 2 usable snapshots are provided, say so plainly instead of
  fabricating a trend
- Close with a "Carryover watch" line: which strike/expiration shows the
  most persistent, multi-day conviction and is therefore most worth
  checking against the next GEX read - framed as an observation, never an
  instruction to trade

Keep it to 3 short paragraphs maximum, plus the closing "Carryover watch"
line. No headers except that one closing label, no bullet points
otherwise. This is not financial advice - describe trend and change only,
never phrase anything as a directive.`;

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
      trade_count: s.trade_count,
      call_premium: s.call_premium,
      put_premium: s.put_premium,
      put_call_ratio_pct: s.put_call_ratio_pct,
      sweep_premium: s.sweep_premium,
      block_premium: s.block_premium,
      aggressive_buy_premium: s.aggressive_buy_premium,
      aggressive_sell_premium: s.aggressive_sell_premium,
      top_clusters: s.top_clusters,
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
            content: `Compare these ${compact.length} daily flow snapshots for ${snapshots[0].ticker}:\n\n${JSON.stringify(compact, null, 2)}`,
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
      put_call_trend: snapshots.map((s) => ({
        captured_at: s.captured_at,
        put_call_ratio_pct: s.put_call_ratio_pct,
      })),
    });
  } catch (err) {
    return res.status(500).json({ error: 'Server error', detail: String(err) });
  }
}
