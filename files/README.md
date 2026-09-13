# GEX Visualizer

Manual screenshot in, structured chart out — no automated contact with
Bullflow at any point.

## Why it's built this way

Bullflow's Terms of Service prohibit automated/scripted extraction of data
from the site. This tool never talks to Bullflow at all — it only processes
an image file you manually saved to your own device. The only automation
happens after the data has already left the site through your own eyes and
hands (the screenshot), which is the same thing as pasting an image into a
chat.

Keep it this way: don't add a step where this app fetches the screenshot
itself (e.g. a headless browser auto-capturing the page). That would put you
back in scraping territory.

## Pipeline

1. **You** take a screenshot of the Bullflow GEX table (manual, always).
2. **Frontend** (`frontend/index.html`) — plain HTML/JS, upload button, sends
   the image as base64 to your own backend endpoint.
3. **Backend** (`server/api/parse-gex.js`) — a serverless function that holds
   your Anthropic API key and calls Claude's vision API to turn the
   screenshot into structured JSON (see `schema.json`).
4. **Frontend renders the chart** — Chart.js horizontal diverging bar chart,
   green/red by sign, amber marker at the gamma flip strike.
5. **Backend** (`server/api/analyze-gex.js`) — takes that structured JSON and
   generates a short written read (flip zone, wall structure, daily vs
   weekly framing), same style Claude gives when you paste a screenshot
   directly into chat.
6. **Download PDF** — client-side only, using jsPDF. Captures the live chart
   canvas as an image and combines it with the written analysis into a
   one-page PDF, named `TICKER_GEX_EXPIRATION.pdf`. No server round-trip for
   the PDF itself — it's assembled entirely in the browser from data already
   on the page.
7. Each parse (chart + analysis) also saves to browser `localStorage` as a
   lightweight history you can click back through and re-export as a PDF
   later.
8. **Weekly read** (`server/api/analyze-weekly.js`) — once you have 2+
   stored snapshots for the same ticker and expiration, a "Generate weekly
   read" button becomes active. It sends all matching snapshots (oldest to
   newest, capped at the most recent 7) to Claude, which compares them
   directly: whether the flip zone has drifted, which walls have grown or
   shrunk, and what that implies heading into the week's expiration. Also
   renders a delta chart (change in GEX per strike from first to latest
   snapshot) and has its own PDF export.
9. **Moving average confluence** (`server/api/get-ma.js`) — after parsing,
   the app fetches the ticker's 30-day and 200-day simple moving averages
   and passes them into the analysis prompt. When a major GEX wall sits
   close to one of these MAs, the written read calls it out as a place
   where technical and dealer-hedging levels may be reinforcing each
   other. This mirrors the price data path used by Howard's separate
   MA200 scanner project: it calls Yahoo Finance's public chart endpoint
   directly (the same unofficial endpoint the `yfinance` Python library
   wraps) rather than a documented, guaranteed API - if Yahoo changes or
   blocks that endpoint, this call will start failing and would need a
   real provider (Alpha Vantage, Twelve Data, etc.) swapped in as a
   fallback.
10. **Volume profile** (`server/api/get-volume-profile.js`) — pulls recent
    intraday 30-minute bars (last 5 days by default) from the same Yahoo
    endpoint and buckets volume by price to compute the Point of Control
    (the price level with the most traded volume) and Value Area (the
    tightest range containing ~70% of volume - standard value-area
    construction). Rendered as its own chart under the GEX chart, and fed
    into the analysis prompt alongside the moving averages - the model
    checks whether POC or value-area edges coincide with major GEX walls,
    since POC + a GEX wall together is a stronger confluence signal than
    either alone (where the market actually traded most AND where dealers
    are hedging most). Included in the PDF export as a second chart image
    when present.
11. **Monthly GEX read** (`server/api/analyze-monthly.js`) — once you have
    5+ saved GEX snapshots for a ticker, a "Generate monthly read" button
    activates alongside the weekly one. Unlike the weekly read, this is
    NOT locked to one expiration column (a month naturally spans several
    weekly/monthly expiration cycles) - it pulls the most recent ~22
    snapshots for the ticker regardless of expiration, condenses each day
    to its flip zone and top few walls (to keep the payload reasonable at
    that many snapshots), and asks for a regime-level read: has the month
    been consistently pinned or volatile, has the flip zone trended in a
    direction, and does any strike persist as a top wall across many days
    despite periodic expiration resets. Renders a spot-vs-flip-zone line
    chart across the period and has its own PDF export.
12. **EOD Flow Analysis** (`server/api/analyze-flow.js`) — a second,
    separate upload path (CSV, not a screenshot) for Bullflow's flow
    export ("Collections"). All arithmetic (put/call premium split,
    sweep/block totals, aggression balance, strike/expiration clustering)
    is computed deterministically in the browser - Claude only writes the
    narrative on top of numbers that are already correct. Opens with a
    one-line "Summary", then covers skew, aggression, and the top
    conviction cluster, optionally ties into the same day's GEX read if
    one was already loaded, and closes with a "Carryover watch" line.
    Renders a premium-by-strike chart (calls green, puts red) and has its
    own PDF export.
13. **Flow history + weekly/monthly flow reads**
    (`server/api/analyze-flow-weekly.js`,
    `server/api/analyze-flow-monthly.js`) — every flow analysis auto-saves
    to a separate history (same pattern as GEX). Once 2+ days exist for a
    ticker, "Generate weekly flow read" compares them: whether put/call
    skew and aggression are trending, and whether any strike/expiration
    cluster repeats across multiple days (a much stronger signal than a
    single day's print). At 5+ days, "Generate monthly flow read" gives
    the same kind of regime summary as the monthly GEX read, plus a
    put%-over-time line chart. Both have their own PDF exports.

### Known limitation

The EOD flow chart (premium by strike) sums all expirations at a given
strike into one bar - e.g. a $760 put expiring this Friday and a $760 put
expiring in December both add to the same bar. The written analysis does
distinguish expirations correctly when naming conviction clusters; only
the chart visualization conflates them. Worth knowing if a strike's bar
looks large but is actually spread across several unrelated expirations.

### How the weekly comparison finds matching snapshots

The ticker filter dropdown (above the history list) scopes everything to
one ticker at a time. Within that ticker, it automatically picks whichever
expiration column appears most often across your saved snapshots — so for
this to produce a clean comparison, **screenshot the same expiration
column consistently** (e.g. always the nearest weekly Friday expiration
for SPY) rather than switching which column you capture day to day.

## Setup

### 1. Deploy the backend

The function in `server/api/parse-gex.js` is written for Vercel's
file-based API routes, but the logic is plain `fetch` — porting to
Cloudflare Workers or Netlify Functions is a small rewrite (just change the
handler signature, the fetch call itself is identical).

```
vercel deploy
```

Set the environment variable in your Vercel project settings:

```
ANTHROPIC_API_KEY=sk-ant-...
```

**Never put this key in the frontend code or commit it to git.** It must
only live server-side as an environment variable.

### 2. Point the frontend at your backend

In `frontend/index.html`, update:

```js
const API_ENDPOINT = '/api/parse-gex';
```

If frontend and backend are deployed together on Vercel (recommended —
just put both folders in one repo), the relative path works as-is.

### 3. Host the frontend

Any static host works: Vercel (same project), GitHub Pages, Netlify.

## Extending it

- **Cross-device history**: right now history lives in browser
  `localStorage`, so weekly comparisons only see snapshots uploaded from
  the same browser/device. Swap in a small database (Vercel KV, Supabase)
  if you want snapshots from your phone and laptop to combine into one
  weekly read.
- **Multi-expiration support**: right now the parser is told to pick the
  largest-magnitude column per screenshot. If you want to compare multiple
  expirations side by side in a single read, extend the schema to an array
  of expiration blocks and adjust both prompts accordingly.
- **Auto-detecting a stale weekly series**: the weekly endpoint currently
  just picks whichever expiration shows up most often in your history. If
  you roll from one week's Friday expiration to the next, old snapshots
  from the prior week will still get included until they age out past the
  7-snapshot cap — worth adding an explicit "start new week" action if
  that mixing becomes a problem.
