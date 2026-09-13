// Route: POST /api/analyze-flow-monthly
// Body: { snapshots: [ <computed flow stats objects, same shape as
//          analyze-flow.js and analyze-flow-weekly.js accept>, ... ] }
//
// Same idea as analyze-flow-weekly.js but widened to a full trading month.

const SYSTEM_PROMPT = `You are a concise options-flow analyst. You receive
an array of daily EOD flow statistics for the SAME ticker, ordered oldest
to newest, spanning roughly a month. Each entry has: captured_at,
trade_count, call_premium, put_premium, put_call_ratio_pct,
sweep_premium, block_premium, aggressive_buy_premium,
aggressive_sell_premium, and top_clusters.

Produce a short written MONTHLY flow read - a regime summary, not a
day-by-day recap. Specifically:

- Characterize the month's overall tone: has flow been consistently
  put-heavy, consistently call-heavy, or has the skew shifted over the
  month? Give an approximate sense of the swing (e.g. "skew ranged from
  40% to 78% puts over the period" rather than every day's number).
- Characterize the aggression regime over the month - has urgency (sweep
  activity, aggressive buying) been elevated throughout, or did it spike
  around specific periods and calm down otherwise?
- Identify any strike/expiration that recurs as a top cluster across MANY
  days spanning weeks, not just a day or two - this is the strongest
  possible flow signal this data can produce, since it implies sustained
  conviction surviving multiple expiration cycles
- If fewer than 5 usable snapshots are provided, say so plainly instead of
  fabricating a month-long trend from too little data

Keep it to 3 short paragraphs maximum. Plain prose, no bullet points, no
headers. This is not financial advice - describe trend and change only,
never phrase anything as a directive.`;

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
    const compact = snapshots.map((s) => ({
      captured_at: s.captured_at,
      trade_count: s.trade_count,
      put_call_ratio_pct: s.put_call_ratio_pct,
      sweep_premium: s.sweep_premium,
      block_premium: s.block_premium,
      aggressive_buy_premium: s.aggressive_buy_premium,
      aggressive_sell_premium: s.aggressive_sell_premium,
      top_clusters: (s.top_clusters || []).slice(0, 5),
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
            content: `Give the monthly flow read for these ${compact.length} daily snapshots of ${snapshots[0].ticker}:\n\n${JSON.stringify(compact, null, 2)}`,
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
      put_call_trend: snapshots.map((s) => ({ captured_at: s.captured_at, put_call_ratio_pct: s.put_call_ratio_pct })),
    });
  } catch (err) {
    return res.status(500).json({ error: 'Server error', detail: String(err) });
  }
}
