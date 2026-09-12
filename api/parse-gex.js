// Deploy target: Vercel serverless function (or adapt for Cloudflare Workers).
// Route: POST /api/parse-gex
// Body: { image: "<base64 string, no data: prefix>", media_type: "image/jpeg", read_type: "daily" | "weekly" }
//
// Keeps the Anthropic API key server-side only. Never call the API directly
// from the browser — that would expose the key to anyone viewing page source.

const SYSTEM_PROMPT = `You extract structured data from screenshots of the
Bullflow.io GEX (gamma exposure) table. The table has strikes as rows and
expiration dates as columns. Cells are colored green (positive GEX) or red
(negative GEX), sometimes with brighter shades for larger magnitudes, and "-"
for no data at that strike/expiry.

Return ONLY valid JSON matching this exact shape, nothing else — no markdown
fences, no commentary:

{
  "ticker": string,
  "spot_price": number,
  "expiration": string (the column you extracted, formatted YYYY-MM-DD, infer
    the year from context if only month/day is shown),
  "levels": [
    { "strike": number, "net_gex_millions": number }
  ]
}

Rules:
- Pick the expiration column with the largest-magnitude values unless the
  user specifies otherwise — this is almost always the nearest monthly
  expiration and the one worth visualizing.
- Convert all values to millions as a signed float. "+$73.6M" -> 73.6.
  "-$1.3M" -> -1.3. "+$237.8K" -> 0.2378. "-" (no data) -> omit that row
  entirely, do not emit 0.
- List strikes in descending order, exactly as shown top to bottom.
- If any number is genuinely unreadable, omit that row rather than guessing.
- Do not include a "is_flip_zone" field — the frontend computes that.`;

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Use POST' });
  }

  const { image, media_type = 'image/jpeg', read_type = 'daily' } = req.body || {};

  if (!image) {
    return res.status(400).json({ error: 'Missing "image" (base64 string) in request body' });
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
        max_tokens: 2000,
        system: SYSTEM_PROMPT,
        messages: [
          {
            role: 'user',
            content: [
              {
                type: 'image',
                source: { type: 'base64', media_type, data: image },
              },
              {
                type: 'text',
                text: 'Extract the GEX table from this screenshot into the JSON shape described in the system prompt.',
              },
            ],
          },
        ],
      }),
    });

    if (!anthropicRes.ok) {
      const errText = await anthropicRes.text();
      return res.status(502).json({ error: 'Anthropic API error', detail: errText });
    }

    const data = await anthropicRes.json();
    const textBlock = data.content.find((b) => b.type === 'text');
    if (!textBlock) {
      return res.status(502).json({ error: 'No text content in model response' });
    }

    // Strip accidental markdown fences before parsing, just in case.
    const cleaned = textBlock.text.replace(/```json|```/g, '').trim();
    let parsed;
    try {
      parsed = JSON.parse(cleaned);
    } catch (e) {
      return res.status(502).json({ error: 'Model did not return valid JSON', raw: cleaned });
    }

    parsed.captured_at = new Date().toISOString();
    parsed.read_type = read_type;

    // Flag the flip zone: the strike closest to spot where sign changes.
    const sorted = [...parsed.levels].sort((a, b) => b.strike - a.strike);
    for (let i = 0; i < sorted.length - 1; i++) {
      if (Math.sign(sorted[i].net_gex_millions) !== Math.sign(sorted[i + 1].net_gex_millions)) {
        sorted[i].is_flip_zone = true;
        sorted[i + 1].is_flip_zone = true;
        break;
      }
    }
    parsed.levels = sorted;

    return res.status(200).json(parsed);
  } catch (err) {
    return res.status(500).json({ error: 'Server error', detail: String(err) });
  }
}
