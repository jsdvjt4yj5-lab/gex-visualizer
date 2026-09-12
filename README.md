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
