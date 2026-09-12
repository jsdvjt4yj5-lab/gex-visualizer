// Route: POST /api/analyze-gex
// Body: { data: <the parsed GEX JSON from parse-gex.js> }
//
// Takes structured GEX data and produces a short written read - the same
// style of analysis as a human analyst would give: where the flip zone is,
// what the wall structure implies, daily vs weekly framing.

const SYSTEM_PROMPT = `You are a concise options-flow analyst. You receive
structured GEX (gamma exposure) data for a single ticker and expiration, and
optionally the ticker's 30-day and 200-day simple moving averages. You
produce a short written read, matching this style:

- Identify the gamma flip zone (where sign changes near spot price)
- Describe the wall structure above and below spot (magnitude and which
  strikes dominate)
- Explain the practical implication: positive gamma above tends to dampen
  moves / cap upside; negative gamma below tends to accelerate moves if
  breached
- If moving averages are provided, check whether any major GEX wall (a
  strike with unusually large magnitude) sits close to the MA30 or MA200
  (within roughly 1% is worth calling out). When a wall and an MA
  coincide, note that the two forms of support/resistance may be
  reinforcing each other - a technical level and a dealer-hedging level at
  the same price. Only mention this if there's a real coincidence; don't
  force a connection that isn't there.
- End with a one-line takeaway in plain language
- If read_type is "weekly", frame the takeaway around the broader
  positioning picture rather than day-to-day noise; if "daily", focus on
  the nearest actionable levels

Keep it to 3 short paragraphs maximum. No headers, no bullet points, plain
prose. This is not financial advice and you should not phrase anything as a
directive ("buy", "sell") - describe structure and implications only.`;

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Use POST' });
  }

  const { data, movingAverages } = req.body || {};
  if (!data || !data.levels) {
    return res.status(400).json({ error: 'Missing "data" (parsed GEX JSON) in request body' });
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
        max_tokens: 600,
        system: SYSTEM_PROMPT,
        messages: [
          {
            role: 'user',
            content: `Write the read for this data:\n\n${JSON.stringify(data, null, 2)}${
              movingAverages ? `\n\nMoving averages:\n${JSON.stringify(movingAverages, null, 2)}` : ''
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
