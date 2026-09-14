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

14. **Live Tiger GEX pull (experimental add-on)**
    (`server/api/get-tiger-gex.py`) — a second, non-screenshot data source
    sitting alongside `parse-gex.js`. A "Pull from Tiger (experimental)"
    button, next to a ticker input, fetches the nearest expiration's
    option chain directly from Tiger Brokers' API and computes GEX from
    Tiger's pre-computed Greeks and open interest - no screenshot needed.
    Produces the exact same JSON shape `parse-gex.js` does, so every
    downstream feature (MA confluence, volume profile, weekly/monthly
    comparisons, strategy context, PDF export) works identically
    regardless of which source a snapshot came from. Requires a
    `TIGER_OPENAPI_CONFIG` environment variable (same setup as the
    separate `tiger-gex-heatmap` project) and a `requirements.txt` at the
    repo root listing `tigeropen`.

    **UNVALIDATED as of first build** - Tiger's computed numbers have not
    yet been compared against Bullflow's for the same ticker/moment
    (every test happened outside market hours, when gamma/open interest
    come back flat). Treat this as experimental until that side-by-side
    comparison happens on a real trading day. Plan: once validated, this
    could replace the screenshot workflow entirely for GEX (flow analysis
    stays screenshot/CSV-based regardless, per the separate Tiger project's
    README on why that side can't be fully replicated).

### Known limitation (Tiger pull)

Gamma and open interest come back as flat zero outside US market hours -
same Tiger data-feed behavior documented in the `tiger-gex-heatmap`
project. Test this button during active trading hours.

15. **Protocol Coffee and Tea** (`server/api/coffee-and-tea.js`) — a full
    trading-session reasoning layer, imported from a workflow spec
    developed in a separate conversation. Takes whatever GEX data is
    currently loaded (screenshot or Tiger pull, either source works since
    both produce the same shape) plus the most recent EOD flow analysis,
    the prior day's saved snapshot (pulled from `gex_history` in
    localStorage - stateless by design, no server-side database), a
    portfolio size, an assumed IV, and manually-entered macro events, and
    produces a complete structured session writeup: market structure read,
    macro context, a realized-vs-implied volatility check (uses the newly
    added `realized_vol_10d_pct`/`realized_vol_20d_pct` fields in
    `get-ma.js`), EOD flow cross-referencing, trade thesis with break
    scenarios, 3-5 sized defined-risk options strategies (POP-ranked, each
    with entry trigger, 50%-profit-target exit, and a stop-loss combining
    a 50%-max-loss price trigger with a structural-invalidation
    condition), and a day-over-day comparison once a second session has
    been run for the same ticker.

    **Validation, not just prompting:** the endpoint rejects (with a clear
    error rather than silently passing through) any response missing a
    strategy's legs or max-loss figure - the programmatic enforcement of
    "no naked strikes, ever" from the original spec, mirrored from that
    conversation's Python schema validators into plain JS checks here.

    **PDF rendering** (`server/api/render-coffee-and-tea-pdf.py`) — the
    styled render layer imported from the other chat's project
    (originally split across `schema.py` and `render_pdf_lib.py`, later
    merged into this single file - Vercel's Hobby plan caps deployments
    at 12 serverless functions, and every standalone `.py` file in `api/`
    counts toward that limit regardless of whether it's a real endpoint,
    so the two library files got folded into the one handler file rather
    than kept separate). Produces the properly designed layout (green
    color scheme, Appendix A/B/C/D structure, styled tables) rather than
    a plain approximation. Validates the reasoning layer's output through
    the real pydantic `ReasoningOutput` schema before rendering - a
    second, stricter check on top of the JS validation already in
    `coffee-and-tea.js`.
    - **Includes the actual gamma exposure chart image**, not just tables
      and narrative text — the frontend captures the already-rendered GEX
      chart canvas as a PNG and sends it alongside the reasoning output;
      the backend embeds it via reportlab's `Image` flowable, scaled to
      fit the page while preserving its aspect ratio. Requires `Pillow`
      (added to `requirements.txt`) to read the image's native dimensions.
    - **One-click flow**: clicking "Run Protocol Coffee and Tea" now
      automatically generates and downloads the PDF immediately after the
      reasoning call completes — no separate "Download session PDF" click
      needed. That button still exists for re-downloading the same
      session afterward without re-running (and re-paying for) the AI call.

    **Scope notes / what's deliberately NOT built:**
    - **Liquidity/bid-ask check** always shows `awaiting_live_chain` -
      needs real bid/ask data this endpoint doesn't have wired in yet
    - **Macro events are entered manually** (one per line, pipe-separated)
      rather than pulled from a live calendar API - no such integration
      exists in this project yet
    - **Pricing is always `black_scholes_estimate`**, even when GEX data
      came from the Tiger pull - real chain bid/ask pricing isn't wired
      into this endpoint, only used for the GEX computation itself

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
