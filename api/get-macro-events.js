// Route: POST /api/get-macro-events
// Body: { session_date: "2026-09-15", expiration: "2026-09-18" }
// Returns: { macro_events: [ { event, date, time_et, detail }, ... ] }
//
// Auto-fetches the macro calendar for a Coffee and Tea session's holding
// window, replacing the old manual "type events into a textarea" step.
// Uses Anthropic's web_search tool (same idea as parse-gex.js/analyze-gex.js
// use the Messages API for structured extraction, just with search
// enabled) to find real, current events rather than relying on training
// data, which goes stale immediately for anything calendar-based.
//
// Same event categories as the standing macro-events-compilation workflow
// and section 9 of the Coffee and Tea spec: FOMC, CPI, jobs data, PPI,
// PMI, Treasury yield events, BoJ/yen. Window is [session_date, expiration]
// inclusive - matches what coffee-and-tea.js's SPEC section 9 checks
// macro_events_this_window against.

const SYSTEM_PROMPT = `You find upcoming macro/economic calendar events for
US equities and rates markets, within a specific date window.

Search for events specifically scheduled between the given start and end
dates (inclusive). Categories to check, in order of importance:
- FOMC meetings/decisions, Fed speakers with market-moving significance
- CPI, PPI releases
- Jobs data (nonfarm payrolls, jobless claims, ADP)
- PMI releases (ISM manufacturing/services, S&P Global PMI)
- US Treasury auctions or yield-relevant events (10Y/30Y auctions, refunding announcements)
- BoJ policy decisions, yen-relevant events (USD/JPY intervention risk, BoJ speakers)

Routine weekly releases count - e.g. initial jobless claims (Thursdays
8:30 ET), housing starts, durable goods, consumer sentiment - as do
scheduled Fed speeches by voters or the Chair/Vice Chair. A normal week
almost always has at least one event; return [] only if you searched and
genuinely found none.

Only include events with a real, confirmed date within the window - do not
guess or extrapolate a recurring event's date without verifying it via
search. If nothing verifiable falls in the window, return an empty array
rather than fabricating an event.

Verify the current Fed Chair and other officeholders via search rather
than assuming - these change and your training data may be stale.

Return ONLY a JSON array, no prose, no markdown fences, no commentary
outside the array. Each item:
{"event": string, "date": "YYYY-MM-DD", "time_et": "HH:MM" | null, "detail": string | null}

If there are no qualifying events in the window, return exactly: []`;

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Use POST' });
  }

  const { session_date, expiration } = req.body || {};
  if (!session_date || !expiration) {
    return res.status(400).json({ error: 'Missing session_date or expiration in request body' });
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
        model: 'claude-haiku-4-5-20251001', // interim cost cut - was claude-sonnet-4-6. This whole file gets replaced once the Finnhub/FRED-based rebuild ships (see conversation), so this is a stopgap, not a permanent choice.
        max_tokens: 2000,
        system: SYSTEM_PROMPT,
        messages: [
          {
            role: 'user',
            content: `Today is ${session_date}. Find US macro calendar events between ${session_date} and ${expiration} (inclusive). Search a weekly economic calendar for this specific week (e.g. "economic calendar week of <Monday's date>") rather than each event type separately.`,
          },
        ],
        // max_uses caps searches per call - the ~192K-token spike traced
        // earlier came from 12 compounding searches in one request. A few-day
        // window rarely needs more than 3-4.
        tools: [{ type: 'web_search_20250305', name: 'web_search', max_uses: 5 }],
      }),
    });

    if (!anthropicRes.ok) {
      const errText = await anthropicRes.text();
      return res.status(502).json({ error: 'Anthropic API error', detail: errText });
    }

    const result = await anthropicRes.json();

    // With web_search enabled, content interleaves tool_use/tool_result
    // blocks with text blocks (the model may write text between searches,
    // e.g. before deciding to search again) - concatenate every text
    // block in order, not just the first, to get the model's complete
    // final answer rather than a truncated fragment of it.
    const textContent = result.content
      .filter((b) => b.type === 'text')
      .map((b) => b.text)
      .join('');

    if (!textContent) {
      return res.status(502).json({ error: 'No text content in model response' });
    }

    if (result.stop_reason === 'max_tokens') {
      return res.status(502).json({
        error: 'Response was cut off before completing (hit the token limit).',
        raw: textContent,
      });
    }

    let cleaned = textContent.replace(/```json|```/g, '').trim();
    let parsed;
    try {
      parsed = JSON.parse(cleaned);
    } catch (e) {
      // Fallback: extract the substring between the first [ and the
      // matching last ] - mirrors the object-extraction fallback used
      // elsewhere in this app, adapted for a top-level array response.
      const firstBracket = cleaned.indexOf('[');
      const lastBracket = cleaned.lastIndexOf(']');
      if (firstBracket !== -1 && lastBracket > firstBracket) {
        try {
          parsed = JSON.parse(cleaned.slice(firstBracket, lastBracket + 1));
        } catch (e2) {
          return res.status(502).json({ error: 'Model did not return valid JSON', raw: cleaned });
        }
      } else {
        return res.status(502).json({ error: 'Model did not return valid JSON', raw: cleaned });
      }
    }

    if (!Array.isArray(parsed)) {
      return res.status(502).json({ error: 'Expected a JSON array of macro events', raw: parsed });
    }

    return res.status(200).json({ macro_events: parsed });
  } catch (err) {
    return res.status(500).json({ error: 'Server error', detail: String(err) });
  }
}
