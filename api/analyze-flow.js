// Route: POST /api/analyze-flow
// Body: { stats: <computed flow statistics from the frontend CSV parser>,
//          gexContext: <optional - today's GEX read data, for tying the two together> }
//
// The frontend does all the arithmetic (premium sums, ratios, clustering) -
// this endpoint only writes the narrative on top of numbers that are
// already computed and correct, the same division of labor as
// analyze-gex.js (frontend/Claude extracts structure, a separate call
// writes the prose).

const SYSTEM_PROMPT = `You are a concise options-flow analyst writing an
end-of-day summary. You receive computed statistics from a full session's
worth of individual options trades (blocks and sweeps) for one ticker:
total put vs call premium, sweep vs block premium, aggressive-buy vs
aggressive-sell premium (based on whether trades executed at/above the ask
or at/below the bid), and a list of the highest-conviction trade clusters
(repeated large trades at the same strike/expiration).

You may also receive gexContext - that morning's GEX structural read for
the same ticker, if available.

Write a short end-of-day flow summary:

- State the put/call premium skew plainly (e.g. "75% of today's premium
  went into puts") and what that suggests about the session's overall
  tone (defensive/hedging-heavy vs bullish-leaning)
- State the aggression balance (aggressive buying vs aggressive selling)
  and what it implies about urgency and conviction, independent of the
  put/call skew - these two can tell different stories (e.g. put-heavy
  but aggressively bought is different from put-heavy and passively sold)
- Call out the single most notable conviction cluster (repeated trades at
  one strike/expiration) by name - the strike, expiration, and what the
  repetition suggests (accumulation, a large order worked in pieces, or
  broad participant agreement)
- If gexContext is provided, note whether today's actual flow confirmed
  or contradicted the morning's GEX-implied structure (e.g. "flow stayed
  put-heavy but concentrated well below the 760 GEX wall noted this
  morning, consistent with hedging rather than a direct challenge to that
  level")
- Close with a "Carryover watch" line: which strike/expiration from
  today's flow is most worth watching in tomorrow's GEX read, and why -
  framed as an observation, never an instruction to trade

Keep it to 3 short paragraphs maximum, plus the closing "Carryover watch"
line. No headers except that one closing label, no bullet points, plain
prose otherwise. This is not financial advice - describe what happened and
what it implies structurally, never a recommended trade or position.`;

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Use POST' });
  }

  const { stats, gexContext } = req.body || {};
  if (!stats || typeof stats.call_premium !== 'number') {
    return res.status(400).json({ error: 'Missing "stats" (computed flow statistics) in request body' });
  }

  try {
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
            content: `Write the EOD flow summary for this data:\n\n${JSON.stringify(stats, null, 2)}${
              gexContext ? `\n\nThis morning's GEX context:\n${JSON.stringify(gexContext, null, 2)}` : ''
            }`,
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

    return res.status(200).json({ analysis: textBlock.text.trim() });
  } catch (err) {
    return res.status(500).json({ error: 'Server error', detail: String(err) });
  }
}
