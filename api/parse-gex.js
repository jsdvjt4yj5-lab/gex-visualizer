// Deploy target: Vercel serverless function (or adapt for Cloudflare Workers).
// Route: POST /api/parse-gex
// Body: { image: "<base64 string, no data: prefix>", media_type: "image/jpeg", read_type: "daily" | "weekly" }
//
// Keeps the Anthropic API key server-side only. Never call the API directly
// from the browser — that would expose the key to anyone viewing page source.

const buildSystemPrompt = () => {
  const today = new Date().toISOString().slice(0, 10); // YYYY-MM-DD, server clock

  return `You extract structured data from screenshots of the
Bullflow.io GEX (gamma exposure) table. The table has strikes as rows and
expiration dates as columns. Cells are colored green (positive GEX) or red
(negative GEX), sometimes with brighter shades for larger magnitudes, and "-"
for no data at that strike/expiry.

TODAY'S ACTUAL DATE IS ${today}. Trust this over any date assumption from
your training - your training data has a cutoff and does NOT reliably know
the current year. Every expiration date in these screenshots is a near-term
date (within about two months of today), never a date from a past year.

CRITICAL: Your entire response must be ONLY the JSON object below. No
explanation, no commentary, no notes about image quality, no text before or
after the JSON - not even a single sentence. This applies even if the
screenshot is cropped, blurry, or missing information. If the ticker or spot
price truly are not visible anywhere in the image, use "UNKNOWN" for ticker
and 0 for spot_price - never explain why in prose. If very few strikes are
readable, return just those strikes - a short but valid JSON response is
always correct; a longer prose explanation is always wrong, with no
exceptions.

Return ONLY valid JSON matching this exact shape:

{
  "ticker": string,
  "spot_price": number,
  "expiration": string (the column you extracted, formatted YYYY-MM-DD. The
    screenshot usually shows only MM/DD with no year - when that happens,
    use the year that makes this date fall on or shortly after ${today}, the
    real current date given above. Never use a year before ${today.slice(0, 4)}),
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
};

// Best-effort write of the full parsed snapshot to Cloudflare D1, so
// screenshot-sourced snapshots land in the same gex_snapshots table as
// Tiger-pull snapshots (get-tiger-gex.py) - D1 is the single source of
// truth for weekly/monthly reads (see get-snapshot-history.js), so every
// snapshot source needs to write here. Mirrors the Python version's
// schema (ticker, session_date, gex_data_json - composite primary key on
// ticker+session_date) and its "never block the real response" contract:
// any failure here is caught and reported back as {"stored": false, ...},
// never thrown.
async function saveSnapshotToD1(ticker, sessionDate, gexDataJson) {
  const accountId = (process.env.CLOUDFLARE_ACCOUNT_ID || '').trim();
  const databaseId = (process.env.CLOUDFLARE_D1_DATABASE_ID || '').trim();
  const apiToken = (process.env.CLOUDFLARE_API_TOKEN || '').trim();

  if (!accountId || !databaseId || !apiToken) {
    return { stored: false, reason: 'missing_cloudflare_env_vars' };
  }

  try {
    const d1Res = await fetch(
      `https://api.cloudflare.com/client/v4/accounts/${accountId}/d1/database/${databaseId}/query`,
      {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${apiToken}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          sql: 'INSERT OR REPLACE INTO gex_snapshots (ticker, session_date, gex_data_json) VALUES (?, ?, ?)',
          params: [ticker, sessionDate, gexDataJson],
        }),
      }
    );

    if (!d1Res.ok) {
      const detail = await d1Res.text();
      return { stored: false, reason: 'http_error', detail };
    }

    const result = await d1Res.json();
    if (result.success) {
      return { stored: true };
    }
    return { stored: false, reason: 'd1_query_failed', detail: result.errors };
  } catch (err) {
    return { stored: false, reason: 'request_error', detail: String(err) };
  }
}

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
        system: buildSystemPrompt(),
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
      // Fallback: the model occasionally wraps the JSON in explanatory
      // prose despite instructions not to. Try extracting the substring
      // between the first { and the last } before giving up entirely.
      const firstBrace = cleaned.indexOf('{');
      const lastBrace = cleaned.lastIndexOf('}');
      if (firstBrace !== -1 && lastBrace > firstBrace) {
        try {
          parsed = JSON.parse(cleaned.slice(firstBrace, lastBrace + 1));
        } catch (e2) {
          return res.status(502).json({ error: 'Model did not return valid JSON', raw: cleaned });
        }
      } else {
        return res.status(502).json({ error: 'Model did not return valid JSON', raw: cleaned });
      }
    }

    parsed.captured_at = new Date().toISOString();
    parsed.read_type = read_type;

    // Safety net: even with the date given in the prompt, correct the
    // expiration's year deterministically if the model still got it wrong
    // (e.g. defaulting to a stale year from training). Expirations in these
    // screenshots are always near-term, so if the parsed date is more than
    // ~30 days in the past, or more than ~2 years out, snap it to the
    // nearest occurrence of that month/day on or after today.
    if (typeof parsed.expiration === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(parsed.expiration)) {
      const now = new Date();
      const parsedDate = new Date(parsed.expiration + 'T00:00:00Z');
      const daysDiff = (parsedDate - now) / (1000 * 60 * 60 * 24);

      if (daysDiff < -30 || daysDiff > 730) {
        const [, mm, dd] = parsed.expiration.split('-');
        let candidateYear = now.getUTCFullYear();
        let candidate = new Date(`${candidateYear}-${mm}-${dd}T00:00:00Z`);
        if ((candidate - now) / (1000 * 60 * 60 * 24) < -30) {
          candidateYear += 1;
          candidate = new Date(`${candidateYear}-${mm}-${dd}T00:00:00Z`);
        }
        parsed.expiration = `${candidateYear}-${mm}-${dd}`;
      }
    }

    if (!Array.isArray(parsed.levels) || parsed.levels.length === 0) {
      return res.status(422).json({
        error: 'No readable strike data found in this screenshot — try a clearer, less cropped image',
      });
    }

    // Sanity check: spot_price should fall reasonably close to the strike
    // range shown (a GEX table is always screenshotted near-the-money).
    // If it's wildly off, that's almost always a single-digit misread by
    // the vision model (e.g. "764.48" read as "564.48") - correct it using
    // the median strike rather than silently passing bad data downstream
    // and hoping the analysis step notices and self-corrects.
    if (typeof parsed.spot_price === 'number' && parsed.spot_price > 0) {
      const strikes = parsed.levels.map((l) => l.strike).sort((a, b) => a - b);
      const medianStrike = strikes[Math.floor(strikes.length / 2)];
      const relativeDiff = Math.abs(parsed.spot_price - medianStrike) / medianStrike;

      if (relativeDiff > 0.15) {
        parsed.spot_price_raw = parsed.spot_price; // preserve what was read, for debugging
        parsed.spot_price = medianStrike;
        parsed.spot_price_corrected = true;
      }
    }

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

    // Persist to D1 - serialize before appending storage-status fields,
    // so what's stored is the clean snapshot, not the status describing it.
    if (parsed.ticker && parsed.ticker !== 'UNKNOWN') {
      const sessionDate = new Date().toISOString().slice(0, 10);
      const gexDataJson = JSON.stringify(parsed);
      const storageResult = await saveSnapshotToD1(parsed.ticker, sessionDate, gexDataJson);
      parsed.stored = storageResult.stored;
      if (!storageResult.stored) {
        parsed.storage_detail = storageResult;
      }
    } else {
      parsed.stored = false;
      parsed.storage_detail = { stored: false, reason: 'ticker_unknown' };
    }

    return res.status(200).json(parsed);
  } catch (err) {
    return res.status(500).json({ error: 'Server error', detail: String(err) });
  }
}
