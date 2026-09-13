// Route: POST /api/analyze-gex
// Body: { symbol, spot_price, expirations, strikes, matrix, generated_at }
//   (the exact JSON shape returned by /api/gex-heatmap)
//
// Takes the computed heatmap data and writes a narrative read, same
// division of labor as the Bullflow project: the heatmap endpoint already
// computed correct numbers, this endpoint only writes prose on top.

const SYSTEM_PROMPT = `You are a concise options-flow analyst. You receive
a GEX (gamma exposure) heatmap: net GEX per strike across several upcoming
expirations for one ticker, computed from live broker data (open interest
and gamma from the option chain).

Produce a short written read:

- Start with a one-line "Summary" label followed by a single sentence
  capturing the overall gamma regime and the most notable wall, so
  someone can get the gist without reading the rest.
- Then: identify the overall gamma regime for the nearest expiration:
  uniformly positive (pinned/range-compressing), uniformly negative
  (volatile/trending), or mixed with a flip zone - name the flip strike if
  one exists
- Call out the single largest-magnitude wall across ALL expirations shown,
  naming its strike and expiration - a wall that shows up strongly in a
  LATER expiration (not just the nearest one) is worth flagging as
  something to watch as that date approaches
- If a strike appears as a significant wall across MULTIPLE expirations
  (not just one), note that as a persistent structural level
- Close with a "Levels to watch" line: the single most relevant strike,
  what would confirm the current read holding, and what would invalidate
  it - framed as structural observations, never instructions to trade

Keep the "Summary" line to one sentence, then 3 short paragraphs maximum,
plus the closing "Levels to watch"
line. No headers except that one closing label, no bullet points, plain
prose otherwise. This is not financial advice - describe structure and
implications only, never a recommended trade, strike, or spread.`;

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Use POST' });
  }

  const { symbol, spot_price, expirations, strikes, matrix, generated_at } = req.body || {};
  if (!symbol || !strikes || !matrix) {
    return res.status(400).json({ error: 'Missing heatmap data in request body' });
  }

  try {
    const payload = { symbol, spot_price, expirations, strikes, matrix, generated_at };

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
            content: `Write the read for this GEX heatmap data:\n\n${JSON.stringify(payload, null, 2)}`,
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
