// Route: POST /api/analyze-flow-period
// Body: { period: "weekly" | "monthly", snapshots: [ <computed flow stats
//          objects, same shape as analyze-flow.js accepts>, ... ] }
//
// Consolidates the former analyze-flow-weekly.js and
// analyze-flow-monthly.js into one endpoint (part of freeing up two
// Vercel function slots, alongside the equivalent GEX merge). Behavior is
// otherwise unchanged: weekly compares 2+ days directly; monthly widens
// to ~20+ days and gives a regime summary instead of a day-by-day
// comparison.

const WEEKLY_PROMPT = `You are a concise options-flow analyst. You receive
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

const MONTHLY_PROMPT = `You are a concise options-flow analyst. You receive
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
      top_clusters: period === 'monthly' ? (s.top_clusters || []).slice(0, 5) : s.top_clusters,
    }));

    const userContent = period === 'weekly'
      ? `Compare these ${compact.length} daily flow snapshots for ${snapshots[0].ticker}:\n\n${JSON.stringify(compact, null, 2)}`
      : `Give the monthly flow read for these ${compact.length} daily snapshots of ${snapshots[0].ticker}:\n\n${JSON.stringify(compact, null, 2)}`;

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
