# Vercel Python serverless function.
# Route: GET /api/get-tiger-gex?symbol=SPY&read_type=daily
#
# Add-on data source, alongside the existing screenshot-based parse-gex.js.
# Produces the EXACT SAME JSON shape parse-gex.js does, so every
# downstream feature (MA confluence, volume profile, weekly/monthly
# comparisons, PDF export) works identically regardless of which source
# a given snapshot came from.
#
# AGGREGATES across every remaining trading day THIS WEEK (today through
# this week's Friday) rather than using a single expiration - net GEX per
# strike is summed across all of them. An empty/thin same-day (0DTE)
# chain just contributes zero rather than needing special-case handling:
# whichever days in the week actually have usable data drive the result,
# and `expirations_included` / `expirations_skipped_empty` in the
# response show exactly which ones did.
#
# GAMMA IS COMPUTED LOCALLY, NOT READ FROM TIGER.
# Tiger's own docs (docs.itigerup.com/docs/quote-option) explicitly
# deprecate the delta/gamma/theta/vega/rho fields returned by
# get_option_chain: they say those values update only once daily and
# are "not timely enough for intraday use." In practice this means
# `gamma` comes back as None on every row, every expiry, regardless of
# whether the market is open - this was confirmed against the SDK docs
# after a run during regular trading hours returned an all-empty
# result. (Earlier comments in this file blamed that on "market
# closed" / a same-day-chain quirk - that diagnosis was wrong; this is
# the real cause.)
#
# Tiger's own recommendation is to compute Greeks locally from live
# market inputs (spot, strike, implied vol, time-to-expiry, risk-free
# rate) using an option pricing model - they ship a QuantLib-based
# American-option calculator for this. We use a plain Black-Scholes-
# Merton gamma formula instead (dividend-yield-adjusted) rather than
# QuantLib, because:
#   - `implied_vol` and `open_interest` are NOT deprecated and are
#     still live, current fields on the chain response - only the
#     Greeks are gone. So we have everything BS-gamma needs.
#   - Gamma is materially insensitive to American vs. European early-
#     exercise features for a liquid, low-dividend underlying like
#     SPY - the standard simplification most retail/prosumer GEX
#     tools (almost certainly including Bullflow) rely on.
#   - QuantLib is a large compiled dependency, awkward to build
#     reliably in a Vercel Python function, and its finite-difference
#     solver isn't built for being run per-contract across an entire
#     chain on every request. Plain BS-gamma is closed-form and runs
#     in microseconds per contract with zero extra dependencies.
#
# UNVALIDATED as of setup: Tiger-sourced computed numbers have not yet
# been compared against Bullflow's for the same ticker/moment. Treat
# this as an experimental second source until that comparison happens
# on a real trading day.
#
# SETUP: add TIGER_OPENAPI_CONFIG as an environment variable in this
# Vercel project (same value used in the separate tiger-gex-heatmap
# project - the full contents of your tiger_openapi_config.properties
# file). Also add a requirements.txt at the repo root listing:
# tigeropen
#
# Optional env vars for the gamma calc (both have sane defaults, so
# only set these if you want to override them):
#   TIGER_RISK_FREE_RATE  - annualized risk-free rate as a decimal,
#                            e.g. 0.043 for 4.3%. Defaults to 0.043.
#                            (Roughly tracks short-term Treasury
#                            yields; doesn't need to be exact - gamma
#                            is not very sensitive to this input.)
#   TIGER_DIVIDEND_YIELD   - underlying's annualized dividend yield as
#                            a decimal, e.g. 0.012 for 1.2%. Defaults
#                            to 0.012 (roughly SPY's trailing yield).

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import math
import os
import tempfile
from datetime import date, datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.quote.quote_client import QuoteClient
from tigeropen.common.consts import Market

EASTERN = ZoneInfo("America/New_York")

DEFAULT_RISK_FREE_RATE = 0.043
DEFAULT_DIVIDEND_YIELD = 0.012

# Only fetch/process strikes within this % of spot in either direction -
# cuts payload size and parse time substantially, since the far-OTM tails
# of a full SPY chain carry negligible GEX contribution anyway. Override
# via TIGER_STRIKE_BAND_PCT (e.g. "0.10" for +-10%) if a wider or
# narrower window is ever needed.
DEFAULT_STRIKE_BAND_PCT = 0.08

# Last-resort fallback if even get_option_analysis fails (e.g. rate limit,
# transient error) - a rough, deliberately unremarkable SPY-ish vol level
# so the calc still runs rather than returning nothing.
HARD_FALLBACK_IV = 0.15

# Floor time-to-expiry at 60 seconds so a same-day (0DTE) contract
# right at/after market close doesn't divide by (near) zero. Gamma
# genuinely spikes as expiry approaches - that's real, not a bug - but
# it must stay finite.
MIN_T_SECONDS = 60


def get_spot_price(ticker):
    import urllib.request
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read())
    result = data["chart"]["result"][0]
    closes = result["indicators"]["quote"][0]["close"]
    valid = [c for c in closes if c is not None]
    return valid[-1]


def get_quote_client():
    config_text = os.environ.get("TIGER_OPENAPI_CONFIG", "")
    if not config_text:
        raise RuntimeError("TIGER_OPENAPI_CONFIG environment variable is not set")
    tmp_dir = tempfile.mkdtemp()
    config_path = os.path.join(tmp_dir, "tiger_openapi_config.properties")
    with open(config_path, "w") as f:
        f.write(config_text)
    client_config = TigerOpenClientConfig(props_path=tmp_dir + os.sep)
    return QuoteClient(client_config)


def get_config_float(env_name, default):
    raw = os.environ.get(env_name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def save_snapshot_to_d1(ticker, session_date, gex_data_json):
    """Best-effort write of the full daily GEX snapshot to Cloudflare D1,
    via D1's HTTP query API (no extra dependency - reuses urllib like
    get_spot_price() above). Storage is a nice-to-have persistence layer
    on top of the live read, not something the read itself depends on, so
    any failure here - missing env vars, a network error, a bad response -
    is swallowed and reported back as {"stored": False, ...} rather than
    raising, and never blocks or alters the actual GEX response.
    """
    import urllib.request
    import urllib.error

    account_id = (os.environ.get("CLOUDFLARE_ACCOUNT_ID") or "").strip()
    database_id = (os.environ.get("CLOUDFLARE_D1_DATABASE_ID") or "").strip()
    api_token = (os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()

    if not all([account_id, database_id, api_token]):
        return {"stored": False, "reason": "missing_cloudflare_env_vars"}

    # The account_id/database_id get built directly into the request URL,
    # which Python's http.client encodes as pure ASCII - a stray non-ASCII
    # character (a smart/curly hyphen instead of a regular one, a
    # non-breaking space, etc. - easy to pick up via copy-paste) throws a
    # cryptic position-based UnicodeEncodeError deep inside urlopen().
    # Check each value explicitly up front so a bad one is named clearly
    # instead of surfacing as an opaque "ordinal not in range(128)" error.
    for var_name, value in [
        ("CLOUDFLARE_ACCOUNT_ID", account_id),
        ("CLOUDFLARE_D1_DATABASE_ID", database_id),
        ("CLOUDFLARE_API_TOKEN", api_token),
    ]:
        try:
            value.encode("ascii")
        except UnicodeEncodeError:
            return {
                "stored": False,
                "reason": "non_ascii_env_var",
                "detail": f"{var_name} contains a non-ASCII character (often a smart/curly "
                          f"dash or invisible character from copy-paste) - re-copy it from the "
                          f"source and re-paste into Vercel, avoiding rich-text sources.",
            }

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query"
    payload = json.dumps({
        "sql": "INSERT OR REPLACE INTO gex_snapshots (ticker, session_date, gex_data_json) VALUES (?, ?, ?)",
        "params": [ticker, session_date, gex_data_json],
    }).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read().decode())
        if result.get("success"):
            return {"stored": True}
        return {"stored": False, "reason": "d1_query_failed", "detail": result.get("errors")}
    except urllib.error.HTTPError as e:
        return {"stored": False, "reason": "http_error", "detail": f"{e.code}: {e.read().decode(errors='replace')}"}
    except Exception as e:
        return {"stored": False, "reason": "request_error", "detail": str(e)}


def fetch_prior_snapshot_from_d1(ticker, before_date):
    """Best-effort read of the most recent prior snapshot for this ticker,
    used only for the day-over-day sanity check below. Returns None on
    any failure (missing env vars, network error, no prior row) - this
    check is a nice-to-have, never something that should block or fail
    the actual GEX response.
    """
    import urllib.request
    import urllib.error

    account_id = (os.environ.get("CLOUDFLARE_ACCOUNT_ID") or "").strip()
    database_id = (os.environ.get("CLOUDFLARE_D1_DATABASE_ID") or "").strip()
    api_token = (os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()
    if not all([account_id, database_id, api_token]):
        return None

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query"
    payload = json.dumps({
        "sql": "SELECT gex_data_json FROM gex_snapshots WHERE ticker = ? AND session_date < ? ORDER BY session_date DESC LIMIT 1",
        "params": [ticker, before_date],
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read().decode())
        rows = result.get("result", [{}])[0].get("results", [])
        if not rows:
            return None
        return json.loads(rows[0]["gex_data_json"])
    except Exception:
        return None


def fetch_latest_snapshot_from_d1(ticker):
    """Best-effort read of the single most recent GEX snapshot for this
    ticker, regardless of date - used to pull notable GEX levels into
    Chart Analysis as optional reference context (see handle_chart_
    analysis below). Unlike fetch_prior_snapshot_from_d1, this isn't
    bounded to "before some date" - it just wants whatever the latest
    stored read is. Returns None on any failure (missing env vars,
    network error, nothing stored yet) since this is a nice-to-have -
    the price-action read must never block or fail on it.
    """
    import urllib.request
    import urllib.error

    account_id = (os.environ.get("CLOUDFLARE_ACCOUNT_ID") or "").strip()
    database_id = (os.environ.get("CLOUDFLARE_D1_DATABASE_ID") or "").strip()
    api_token = (os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()
    if not all([account_id, database_id, api_token]):
        return None

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query"
    payload = json.dumps({
        "sql": "SELECT session_date, gex_data_json FROM gex_snapshots WHERE ticker = ? ORDER BY session_date DESC LIMIT 1",
        "params": [ticker],
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read().decode())
        rows = result.get("result", [{}])[0].get("results", [])
        if not rows:
            return None
        snapshot = json.loads(rows[0]["gex_data_json"])
        snapshot["session_date"] = rows[0]["session_date"]
        return snapshot
    except Exception:
        return None


def third_friday(year, month):
    """The date of the third Friday of a given month - the standard
    monthly equity/index options expiration date."""
    first = date(year, month, 1)
    first_friday_offset = (4 - first.weekday()) % 7  # Mon=0..Sun=6, Friday=4
    return first + timedelta(days=first_friday_offset + 14)


def next_monthly_opex(from_date):
    """The next monthly options expiration (third Friday) on or after
    from_date."""
    y, m = from_date.year, from_date.month
    for _ in range(4):
        candidate = third_friday(y, m)
        if candidate >= from_date:
            return candidate
        m += 1
        if m > 12:
            m = 1
            y += 1
    return None


def next_triple_witching(from_date):
    """The next quarterly triple-witching date (third Friday of March,
    June, September, or December) on or after from_date - the date stock
    index futures, stock index options, and equity options all expire
    together, historically associated with elevated volume/volatility."""
    quarter_months = [3, 6, 9, 12]
    y = from_date.year
    for _ in range(6):
        for m in quarter_months:
            if y == from_date.year and m < from_date.month:
                continue
            candidate = third_friday(y, m)
            if candidate >= from_date:
                return candidate
        y += 1
    return None


# SPY's quarterly ex-dividend dates. Unlike op-ex/triple witching, this is
# NOT a fixed calendar rule - it's whatever State Street's board declares
# each quarter, and while it's usually close to that quarter's third
# Friday, it isn't always exactly that day: the June 2026 ex-date
# (2026-06-18) fell on the Thursday before that quarter's third-Friday
# triple witching (2026-06-19), confirmed via State Street/broker
# dividend histories. So this is a maintained list of CONFIRMED dates,
# not a formula - State Street typically only announces each quarter's
# date a few weeks ahead, so add the next one here once it's confirmed
# (check a broker's SPY dividend history or ssga.com). Kept in sync with
# the identical list in api/coffee-and-tea.js.
SPY_DIVIDEND_EX_DATES = [
    date(2025, 12, 19),
    date(2026, 3, 20),
    date(2026, 6, 18),
    date(2026, 9, 18),
    # 2026-12 not yet announced as of this writing (Sept 2026) - add once confirmed
]


def next_spy_dividend_ex_date(from_date):
    upcoming = sorted(d for d in SPY_DIVIDEND_EX_DATES if d >= from_date)
    return upcoming[0] if upcoming else None


def opex_context_note(today_date, symbol):
    """Best-effort, purely calendar-computed (no API call) note on where
    today sits relative to the next monthly options expiration, next
    quarterly triple witching, and (SPY only) next confirmed SPY dividend
    ex-date - used to give Chart Analysis awareness of expiration/
    dividend-driven price behavior without any GEX/options-chain data at
    all, so it works even when the D1 GEX reference (see fetch_latest_
    snapshot_from_d1) is unavailable.
    """
    next_opex = next_monthly_opex(today_date)
    next_witching = next_triple_witching(today_date)
    if not next_opex or not next_witching:
        return None
    is_today_opex = next_opex == today_date
    is_today_witching = next_witching == today_date
    opex_part = (
        "IS a monthly options expiration Friday" if is_today_opex
        else f"is not an expiration Friday (next monthly op-ex: {next_opex.isoformat()})"
    )
    witching_part = (
        "also IS quarterly triple witching (stock index futures, index options, and equity options all expire together)"
        if is_today_witching
        else f"next quarterly triple witching: {next_witching.isoformat()}"
    )
    note = f"Today ({today_date.isoformat()}) {opex_part}; {witching_part}."

    if symbol.upper() == "SPY":
        next_div = next_spy_dividend_ex_date(today_date)
        if next_div:
            is_today_div = next_div == today_date
            div_part = (
                "Today IS also SPY's confirmed dividend ex-date."
                if is_today_div
                else f"Next confirmed SPY dividend ex-date: {next_div.isoformat()}."
            )
            note += f" {div_part}"

    return note


def years_to_expiry(expiry_date_str, now_et):
    """Time to expiry in years, measured to 4:00pm ET on the expiry
    date, floored at MIN_T_SECONDS so it never hits zero/negative."""
    expiry_date = date.fromisoformat(expiry_date_str)
    market_close_et = datetime(
        expiry_date.year, expiry_date.month, expiry_date.day,
        16, 0, 0, tzinfo=EASTERN,
    )
    seconds_remaining = (market_close_et - now_et).total_seconds()
    seconds_remaining = max(seconds_remaining, MIN_T_SECONDS)
    return seconds_remaining / (365.0 * 24 * 3600)


def bs_gamma(spot, strike, t_years, sigma, risk_free_rate, dividend_yield):
    """Black-Scholes-Merton gamma (dividend-yield-adjusted). Same
    formula for calls and puts - gamma doesn't depend on option
    direction in this model. Returns None if inputs are unusable."""
    if spot <= 0 or strike <= 0 or t_years <= 0 or sigma <= 0:
        return None
    sqrt_t = math.sqrt(t_years)
    d1 = (
        math.log(spot / strike)
        + (risk_free_rate - dividend_yield + 0.5 * sigma * sigma) * t_years
    ) / (sigma * sqrt_t)
    pdf_d1 = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    return math.exp(-dividend_yield * t_years) * pdf_d1 / (spot * sigma * sqrt_t)


def get_underlying_iv(quote_client, symbol):
    """Tiger's per-contract implied_vol field is coming back as 0.0 for
    every row on the chain endpoint (confirmed in testing - not documented
    by Tiger as a known gap). get_option_analysis still returns a real,
    live 30-day aggregate IV for the underlying itself, so we use that as
    a flat volatility input across the whole chain instead. This loses
    strike-to-strike vol skew (real markets price OTM puts richer than
    OTM calls) but is far better than the alternative of no usable vol
    input at all. Falls back to HARD_FALLBACK_IV only if this call itself
    fails (e.g. transient error/rate limit) - not expected in normal use.
    Returns (iv_value, source_string) so callers can report which path was used.
    """
    try:
        results = quote_client.get_option_analysis(symbols=[symbol], market=Market.US)
        for item in results:
            if getattr(item, "symbol", None) == symbol:
                iv = getattr(item, "implied_vol_30_days", None)
                if iv is not None and not math.isnan(iv) and iv > 0:
                    return iv, "option_analysis_30d"
    except Exception:
        pass
    return HARD_FALLBACK_IV, "hard_fallback"


# ---- Price-data mode: MA / volume-profile from Tiger's own bars, as a
# real (documented, authenticated) alternative to the unofficial Yahoo
# Finance endpoint get-price-data.js currently uses. Response shapes below
# deliberately match get-price-data.js's fields exactly, so the frontend
# can call either interchangeably (Tiger first, Yahoo as a free fallback).

def fetch_tiger_bars(quote_client, symbol, period, limit):
    """Returns a list of {time_ms, close, volume} dicts, oldest first.
    Tiger's exact returned column names aren't 100% confirmed from
    documentation alone (docs show Rust/CLI examples, not a literal
    Python DataFrame column list) - this tries the common variants and
    raises a clear, diagnosable error naming the ACTUAL columns Tiger
    returned if none match, rather than failing silently or guessing wrong
    in a way that's hard to debug from outside.
    """
    df = quote_client.get_bars([symbol], period=period, limit=limit)
    if df is None or len(df) == 0:
        raise RuntimeError(f"Tiger returned no bars for {symbol} (period={period}, limit={limit})")

    cols = {str(c).lower(): c for c in df.columns}
    time_col = cols.get("time") or cols.get("timestamp")
    close_col = cols.get("close")
    volume_col = cols.get("volume")
    if not time_col or not close_col:
        raise RuntimeError(
            f"Unexpected columns in Tiger bars response: {list(df.columns)} "
            f"- expected at least 'time'/'timestamp' and 'close'"
        )

    bars = []
    for _, row in df.iterrows():
        t_raw = row[time_col]
        if t_raw is None or (isinstance(t_raw, float) and math.isnan(t_raw)):
            continue
        close_raw = row[close_col]
        if close_raw is None or (isinstance(close_raw, float) and math.isnan(close_raw)):
            continue
        volume_raw = row[volume_col] if volume_col is not None else None
        bars.append({
            "time_ms": int(t_raw),
            "close": float(close_raw),
            "volume": float(volume_raw) if volume_raw is not None and not (isinstance(volume_raw, float) and math.isnan(volume_raw)) else None,
        })
    bars.sort(key=lambda b: b["time_ms"])
    return bars


def sma(closes, period):
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 2)


def realized_vol_pct(closes, period):
    """Annualized realized volatility from daily log returns - same
    close-to-close calculation get-price-data.js already uses, ported
    directly so results are consistent regardless of which source
    (Tiger or Yahoo) actually served the underlying price data.
    """
    if len(closes) < period + 1:
        return None
    window = closes[-(period + 1):]
    log_returns = [math.log(window[i] / window[i - 1]) for i in range(1, len(window))]
    mean = sum(log_returns) / len(log_returns)
    variance = sum((v - mean) ** 2 for v in log_returns) / (len(log_returns) - 1)
    daily_std = math.sqrt(variance)
    return round(daily_std * math.sqrt(252) * 100, 2)


def handle_price_data_ma(quote_client, symbol):
    # limit=210: 200 needed + a small buffer, same reasoning as the
    # holiday-aware exact walk-back already used for the Yahoo version -
    # Tiger's own daily bars are real trading days by construction, so no
    # calendar-day padding guess is needed here at all.
    bars = fetch_tiger_bars(quote_client, symbol, period="day", limit=210)
    closes = [b["close"] for b in bars]
    if len(closes) < 30:
        return {"error": "Not enough price history to compute moving averages"}, 422

    return {
        "symbol": symbol,
        "price": round(closes[-1], 2),
        "ma30": sma(closes, 30),
        "ma200": sma(closes, 200),
        "ma200_available": len(closes) >= 200,
        "realized_vol_10d_pct": realized_vol_pct(closes, 10),
        "realized_vol_20d_pct": realized_vol_pct(closes, 20),
        "source": "tiger",
    }, 200


def handle_price_data_volume_profile(quote_client, symbol, days):
    # 30-min bars, ~13 per regular trading day - limit=300 comfortably
    # covers the max 10-day lookback with real margin, then filtered to
    # the actual requested window by timestamp below.
    bars = fetch_tiger_bars(quote_client, symbol, period="30min", limit=300)
    cutoff_ms = int((datetime.now(timezone.utc).timestamp() - days * 24 * 3600) * 1000)
    bars = [b for b in bars if b["time_ms"] >= cutoff_ms and b["volume"]]
    if len(bars) < 10:
        return {"error": "Not enough intraday data to build a volume profile"}, 422

    prices = [b["close"] for b in bars]
    min_price, max_price = min(prices), max(prices)
    price_range = (max_price - min_price) or 1
    bucket_count = 30
    bucket_size = price_range / bucket_count

    buckets = [{
        "price_low": round(min_price + i * bucket_size, 2),
        "price_high": round(min_price + (i + 1) * bucket_size, 2),
        "volume": 0.0,
    } for i in range(bucket_count)]

    for b in bars:
        idx = min(max(int((b["close"] - min_price) / bucket_size), 0), bucket_count - 1)
        buckets[idx]["volume"] += b["volume"]

    total_volume = sum(bk["volume"] for bk in buckets)
    poc_idx = max(range(bucket_count), key=lambda i: buckets[i]["volume"])
    poc = round((buckets[poc_idx]["price_low"] + buckets[poc_idx]["price_high"]) / 2, 2)

    # Value area: expand outward from POC, adding whichever neighboring
    # bucket has more volume, until ~70% of total volume is included -
    # same standard construction get-volume-profile.js already uses.
    included = {poc_idx}
    included_volume = buckets[poc_idx]["volume"]
    low_ptr, high_ptr = poc_idx - 1, poc_idx + 1
    while included_volume < total_volume * 0.7 and (low_ptr >= 0 or high_ptr < bucket_count):
        low_vol = buckets[low_ptr]["volume"] if low_ptr >= 0 else -1
        high_vol = buckets[high_ptr]["volume"] if high_ptr < bucket_count else -1
        if high_vol >= low_vol and high_ptr < bucket_count:
            included.add(high_ptr)
            included_volume += buckets[high_ptr]["volume"]
            high_ptr += 1
        elif low_ptr >= 0:
            included.add(low_ptr)
            included_volume += buckets[low_ptr]["volume"]
            low_ptr -= 1
        else:
            break

    included_sorted = sorted(included)
    return {
        "symbol": symbol,
        "lookback_days": days,
        "poc": poc,
        "value_area_low": round(buckets[included_sorted[0]]["price_low"], 2),
        "value_area_high": round(buckets[included_sorted[-1]]["price_high"], 2),
        "buckets": buckets,
        "source": "tiger",
    }, 200


CHART_ANALYSIS_SYSTEM_PROMPT = """You are a concise technical analyst. You
receive DAILY and WEEKLY arrays of closing prices and volumes for a
single ticker, each ordered oldest to newest, and - only on some calls,
see below - a MONTHLY array too. There is no options/gamma data at all:
this is a pure price-action read, deliberately separate from any
GEX-based analysis. You also receive each provided timeframe's own
simple moving averages: 30-day and 200-day for daily, 10-week and
40-week for weekly, 12-month for monthly. Any of these may be marked
unavailable if there isn't enough history yet for that particular bar
count - work with whatever is available rather than fabricating a
missing MA.

The MONTHLY timeframe is only sent during the last week of each
calendar month, since a monthly bar barely moves before it closes and
re-analyzing it daily would just repeat the prior read. On calls where
it's missing, you'll be told explicitly that it was left out by design -
in that case write only the Weekly and Daily sections (skip the Monthly
header and paragraph entirely) and have the Multi-timeframe context
paragraph speak only to whether Weekly and Daily agree or conflict.
Never apologize for or explain the missing monthly data, and never
fabricate a monthly read from the daily/weekly bars alone - just proceed
with what was actually provided.

For EACH timeframe you were actually given data for, cover:
- The overall trend over the period shown (uptrend, downtrend, range-bound,
  or a recent shift from one to another - name roughly when a shift
  happened if one did)
- Approximate support and resistance levels implied by where price has
  repeatedly reversed or stalled in that timeframe's data
- Any notable pattern in the closes (e.g. higher highs/higher lows,
  a clear range, a breakout or breakdown from a prior range)
- How current price sits relative to recent levels (near a prior high/low,
  mid-range, etc.)
- Where price sits relative to that timeframe's moving average(s)
  (above/below/crossing), and whether an MA is close to (roughly within
  1% of) a support/resistance level you already identified from price
  action alone - call out that confluence explicitly if it's genuinely
  there. Only mention confluence that's real - don't force a connection
  between unrelated levels. If an MA isn't available, just work with
  what is and say so briefly rather than fabricating a read.
- If that timeframe's data shows a sideways range (not a clean trend),
  assess whether it reads more like **accumulation** or **distribution**
  using volume behavior within the range: accumulation shows volume
  spiking on down-bars inside the range (selling being absorbed) with
  higher lows forming as the range matures, often with a false breakdown
  below range support that gets reclaimed; distribution shows volume
  spiking on up-bars inside the range (supply being sold into strength)
  with lower highs forming, often with a false breakout above range
  resistance that fails. Only call this out when the range and volume
  pattern genuinely support one reading over the other - if the range is
  too short, the volume signal is mixed, or price is simply trending,
  say the range doesn't yet show a clear accumulation/distribution
  signature rather than forcing a call.

You may also receive an optional list of notable GEX (gamma exposure)
levels from the most recent stored options-based snapshot for this
ticker - strikes with their net GEX in millions and whether each sits in
a gamma flip zone, plus the session date that snapshot was captured on.
This is dealer options-positioning data, not price action, and Chart
Analysis stays a pure price-action read - never summarize the GEX data
on its own or explain dealer hedging mechanics. Its only use here is
confluence: if a support/resistance level you already identified from
price action alone (in the Daily section, since a single-expiration GEX
snapshot is a near-term read and rarely still relevant at weekly/monthly
horizons) sits close to (roughly within 0.5-1%) a notable GEX strike,
call that out explicitly as reinforced by GEX positioning at that
strike - a price-based level and dealer positioning agreeing is a
stronger signal than either alone. If the snapshot's session date is
more than a few days old, mention briefly that the GEX reference may be
stale. If no GEX levels are provided, or none genuinely line up with a
level you already found, don't mention GEX at all - never invent or
force a GEX connection to a level found from price alone.

You will also always receive an options expiration context line stating
where today sits relative to the next monthly options expiration (third
Friday of the month) and next quarterly triple witching (third Friday of
March/June/September/December, when stock index futures, index options,
and equity options all expire together) - purely a calendar fact, not
GEX or dealer-positioning data. On SPY sessions, this line also includes
the next confirmed SPY dividend ex-date (a maintained list of announced
dates, not a formula - so it may occasionally be absent if the next
quarter's date hasn't been announced yet). Mention any of these only
when genuinely close enough to matter for the Daily section: today
itself is one of these dates, or one falls within roughly the next 3-5
trading days from the most recent daily bar. In that case, one sentence
is enough per item - name which one it is and the relevant mechanism:
op-ex/triple witching can drive pinning into the close beforehand and/or
a volatility pickup right after as dealer hedges roll off; a dividend
ex-date causes a small, mechanical gap down in the underlying by roughly
the dividend amount at the open (dealers typically hedge this, so it's
more predictable and smaller in magnitude than an op-ex unwind). If the
next occurrence of any of these is weeks away, don't mention it at all -
this is a brief situational note, never its own section or a mandatory
part of every response.

On SPY sessions only, you may also receive a sector context line: the 11
S&P GICS sectors' percent change across several windows (1-day, 5-day,
1-month, 3-month, 1-year), ranked strongest to weakest by today's move.
This is a breadth/rotation check, not price action of the ticker itself.
The 1-day/5-day figures are for the Daily section only - use them only
when they genuinely add something to what the price/volume data already
shows: e.g. today's move is broad-based (most sectors moving the same
direction) versus narrow (concentrated in one or two, with others flat
or diverging), or a notably strong/weak sector stands out at the top or
bottom of the ranking in a way that's relevant context for SPY's own
move. The 1-month/3-month/1-year figures belong with the Weekly or
Monthly section instead (whichever's timeframe they're closer to) - use
them only for genuine multi-week/multi-month rotation context, e.g. a
sector that's been leading or lagging for months, not for interpreting
a single day's move. A brief mention is enough wherever it's used - name
the standout sector(s) and what the spread tells you, don't list all 11,
and don't force sector commentary into every section just because the
data exists. If the sectors don't add a meaningfully different picture
from the price action alone at that timeframe, or this data wasn't
provided (any ticker other than SPY), don't mention sectors at all.

Structure the response as: a one-line bolded "Summary:" with a single
sentence capturing the whole picture, then bolded section headers in
this order - "Monthly:" (only when monthly data was provided), then
"Weekly:", then "Daily:" (broadest context first, narrowest last) - each
followed by 1-3 short paragraphs covering the points above for that
timeframe. End with one final bolded header, "Multi-timeframe context:",
with a short paragraph on whether the provided timeframes agree or
conflict (e.g. a bullish monthly/weekly backdrop with a daily pullback
reads very differently from daily strength fighting a weekly downtrend)
- only draw out real tension or alignment; if the timeframes simply
agree, say so briefly rather than manufacturing a conflict that isn't
there.

Use **double asterisks** for section headers and for the occasional 2-4
word label you want emphasized inside a paragraph (a level name, a trend
call) - do not overuse this beyond headers and a few labels. Plain prose
paragraphs under each header, no bullet points.

This is not financial advice. Describe structure and price history only -
never phrase anything as a directive to buy, sell, or take a specific
action, and never name a specific trade, strike, or options strategy."""


def fetch_sector_performance_alphavantage():
    """Best-effort S&P sector performance via Alpha Vantage's SECTOR
    endpoint - one API call returns all 11 GICS sectors across several
    windows at once (1 Day, 5 Day, 1 Month, 3 Month, 1 Year used here),
    instead of fetching each sector ETF individually via Tiger. Runs on
    its own key/quota (ALPHAVANTAGE_API_KEY env var; free tier is 25
    requests/day, 5/min as of this writing) - entirely separate from and
    doesn't touch the Tiger kline quota at all.

    Returns (data, debug): data is None on any failure (missing key,
    network error, rate-limited, unexpected response shape) since this
    is a nice-to-have Chart Analysis should never block or fail on - but
    debug always describes what actually happened (which of those it
    was, an HTTP status, an Alpha Vantage error/rate-limit message
    verbatim, or the keys an unexpected response shape actually had), so
    a failure is diagnosable via the API response's sector_context_debug
    field instead of silently vanishing the way it used to.
    """
    api_key = (os.environ.get("ALPHAVANTAGE_API_KEY") or "").strip()
    if not api_key:
        return None, {"status": "no_api_key"}

    import urllib.request
    import urllib.error

    url = f"https://www.alphavantage.co/query?function=SECTOR&apikey={api_key}"
    req = urllib.request.Request(url, headers={"User-Agent": "gex-visualizer"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        return None, {
            "status": "http_error",
            "code": e.code,
            "detail": e.read().decode(errors="replace")[:300],
        }
    except urllib.error.URLError as e:
        return None, {"status": "network_error", "detail": str(e.reason)}
    except Exception as e:
        return None, {"status": "request_failed", "detail": str(e)}

    try:
        data = json.loads(raw)
    except Exception:
        return None, {"status": "invalid_json", "raw_preview": raw[:300]}

    # Alpha Vantage returns HTTP 200 even for a bad key, a rate-limit, or
    # an unrecognized function - it just swaps the "Rank ..." sections
    # for an "Information", "Note", or "Error Message" field instead.
    # Surface that verbatim rather than falling through to the generic
    # "unexpected shape" case below, since the message itself usually
    # says exactly what's wrong (e.g. an invalid key, or the free-tier
    # 25/day cap being hit).
    for error_field in ("Error Message", "Note", "Information"):
        if error_field in data:
            return None, {"status": "api_error_response", "field": error_field, "detail": str(data[error_field])[:300]}

    # Alpha Vantage labels each window "Rank <letter>: <window> Performance"
    # (e.g. "Rank B: 1 Day Performance") - matched by substring rather than
    # the exact rank letter, since the letter isn't guaranteed stable and
    # substring matching is just as simple and more robust to it shifting.
    def find_window(substring):
        key = next((k for k in data.keys() if substring in k), None)
        return data.get(key) if key else None

    windows = {
        "one_day_pct": find_window("1 Day"),
        "five_day_pct": find_window("5 Day"),
        "one_month_pct": find_window("1 Month"),
        "three_month_pct": find_window("3 Month"),
        "one_year_pct": find_window("1 Year"),
    }
    if not any(windows.values()):
        return None, {"status": "unexpected_shape", "keys_found": list(data.keys())[:10]}

    def parse_pct(s):
        try:
            return float(s.strip().rstrip("%"))
        except (TypeError, ValueError, AttributeError):
            return None

    all_sectors = sorted({sector for w in windows.values() if w for sector in w.keys()})
    result = [
        {
            "sector": sector,
            **{
                field: parse_pct(w.get(sector)) if w else None
                for field, w in windows.items()
            },
        }
        for sector in all_sectors
    ]
    return result, {"status": "ok", "sector_count": len(result)}


def _condense_bars(bars, n):
    """Trims to the most recent n bars and reshapes to {date, close, volume}
    dicts for the model payload - shared by all three timeframes below."""
    trimmed = bars[-n:]
    return [
        {
            "date": datetime.fromtimestamp(b["time_ms"] / 1000, tz=timezone.utc)
                .astimezone(EASTERN).date().isoformat(),
            "close": round(b["close"], 2),
            "volume": b["volume"],
        }
        for b in trimmed
    ]


def handle_chart_analysis(quote_client, symbol):
    # Three Tiger kline calls per click now (day/week/month) instead of
    # one - each period is a separate get_bars() call, and they're
    # believed to share the same "kline" quota bucket Tiger reported as
    # remain: 20 (reset cadence still unconfirmed - see handover notes).
    # So a single "Generate chart analysis" click now costs 3 quota
    # units, not 1 - except during most of the month, where it's 2 (see
    # is_last_week_of_month below).
    daily_bars = fetch_tiger_bars(quote_client, symbol, period="day", limit=210)
    if len(daily_bars) < 30:
        return {"error": "Not enough daily bars to write a chart analysis"}, 422

    # Weekly: 110 bars (~2 years) comfortably covers a 40-week MA with a
    # buffer.
    weekly_bars = fetch_tiger_bars(quote_client, symbol, period="week", limit=110)

    # Monthly context barely moves within a month - a monthly bar doesn't
    # even close until month-end, so re-running it every day burns a
    # Tiger kline call (and prompt space) for a read that's almost
    # identical to yesterday's. Only fetch it during the last 7 calendar
    # days of the month, when the current monthly bar is close to final
    # and actually worth a fresh look; every other day Chart Analysis
    # covers Daily + Weekly only.
    today_et = datetime.now(timezone.utc).astimezone(EASTERN).date()
    next_month_first = (
        date(today_et.year + 1, 1, 1) if today_et.month == 12
        else date(today_et.year, today_et.month + 1, 1)
    )
    days_in_month = (next_month_first - date(today_et.year, today_et.month, 1)).days
    is_last_week_of_month = today_et.day > days_in_month - 7

    # Monthly: 40 bars (~3+ years) comfortably covers a 12-month MA.
    monthly_bars = (
        fetch_tiger_bars(quote_client, symbol, period="month", limit=40)
        if is_last_week_of_month else []
    )

    daily_closes = [b["close"] for b in daily_bars]
    weekly_closes = [b["close"] for b in weekly_bars]
    monthly_closes = [b["close"] for b in monthly_bars]

    daily_ma30 = sma(daily_closes, 30)
    daily_ma200 = sma(daily_closes, 200)
    daily_ma200_available = len(daily_closes) >= 200

    weekly_ma10 = sma(weekly_closes, 10)
    weekly_ma40 = sma(weekly_closes, 40)
    weekly_ma40_available = len(weekly_closes) >= 40

    monthly_ma12 = sma(monthly_closes, 12)
    monthly_ma12_available = len(monthly_closes) >= 12

    # Trim the price-action payload sent to the model per timeframe -
    # enough bars for a trend/support-resistance read without bloating
    # the prompt with the full MA lookback window on each one.
    daily_condensed = _condense_bars(daily_bars, 90)
    weekly_condensed = _condense_bars(weekly_bars, 52)
    monthly_condensed = _condense_bars(monthly_bars, 24)

    timeframes_meta = {
        "daily": {
            "bars_used": len(daily_condensed),
            "date_range": [daily_condensed[0]["date"], daily_condensed[-1]["date"]],
            "bars": daily_condensed,
            "ma30": daily_ma30,
            "ma200": daily_ma200,
            "ma200_available": daily_ma200_available,
        },
        "weekly": {
            "bars_used": len(weekly_condensed),
            "date_range": [weekly_condensed[0]["date"], weekly_condensed[-1]["date"]],
            "bars": weekly_condensed,
            "ma10": weekly_ma10,
            "ma40": weekly_ma40,
            "ma40_available": weekly_ma40_available,
        } if weekly_condensed else None,
        "monthly": {
            "bars_used": len(monthly_condensed),
            "date_range": [monthly_condensed[0]["date"], monthly_condensed[-1]["date"]],
            "bars": monthly_condensed,
            "ma12": monthly_ma12,
            "ma12_available": monthly_ma12_available,
        } if monthly_condensed else None,
    }

    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_api_key:
        return {"error": "ANTHROPIC_API_KEY is not configured"}, 500

    import urllib.request
    import urllib.error

    daily_ma_note = f"30-day MA: {daily_ma30}"
    daily_ma_note += f", 200-day MA: {daily_ma200}" if daily_ma200_available else ", 200-day MA: not enough history yet"

    weekly_ma_note = f"10-week MA: {weekly_ma10}"
    weekly_ma_note += f", 40-week MA: {weekly_ma40}" if weekly_ma40_available else ", 40-week MA: not enough history yet"

    monthly_ma_note = (
        f"12-month MA: {monthly_ma12}" if monthly_ma12_available
        else "12-month MA: not enough history yet"
    )

    # Optional GEX confluence context - pulls the most recent stored GEX
    # snapshot for this ticker (if any) and surfaces its most notable
    # strikes to the model as reference only. This stays a nice-to-have:
    # a missing/failed D1 read here must never block the price-action
    # read, and the prompt tells the model to only use this for
    # confluence, not as its own analysis.
    gex_snapshot = fetch_latest_snapshot_from_d1(symbol)
    notable_gex_levels = []
    gex_note = None
    if gex_snapshot and gex_snapshot.get("levels"):
        ranked = sorted(
            gex_snapshot["levels"],
            key=lambda l: abs(l.get("net_gex_millions", 0)),
            reverse=True,
        )
        notable_gex_levels = sorted(ranked[:10], key=lambda l: l["strike"])
        gex_note = (
            f"Session {gex_snapshot.get('session_date', 'unknown date')}, "
            f"spot at capture {gex_snapshot.get('spot_price', 'n/a')}: "
            + ", ".join(
                f"{l['strike']} ({l['net_gex_millions']:+.1f}M"
                + (", flip zone" if l.get("is_flip_zone") else "")
                + ")"
                for l in notable_gex_levels
            )
        )
        # Separately, hand the daily chart a tighter subset for drawing
        # reference lines - only strikes that actually fall within the
        # visible daily price range, so the chart doesn't get stretched
        # to fit a strike from a stale/far-off snapshot.
        daily_closes_in_view = [b["close"] for b in daily_condensed]
        lo, hi = min(daily_closes_in_view), max(daily_closes_in_view)
        pad = (hi - lo) * 0.15 if hi > lo else hi * 0.02
        timeframes_meta["daily"]["gex_levels"] = [
            l for l in notable_gex_levels if lo - pad <= l["strike"] <= hi + pad
        ]

    user_content = (
        f"Write the multi-timeframe technical read for {symbol}.\n\n"
        f"DAILY bars ({len(daily_condensed)}, {daily_ma_note}):\n{json.dumps(daily_condensed)}\n\n"
        f"WEEKLY bars ({len(weekly_condensed)}, {weekly_ma_note}):\n{json.dumps(weekly_condensed)}"
    )
    if is_last_week_of_month and monthly_condensed:
        user_content += (
            f"\n\nMONTHLY bars ({len(monthly_condensed)}, {monthly_ma_note}):\n{json.dumps(monthly_condensed)}"
        )
    else:
        user_content += (
            "\n\nMONTHLY: not included this call - by design, the monthly timeframe "
            "is only refreshed during the last 7 calendar days of each month, since a "
            "monthly bar barely moves before it closes. Write Weekly and Daily sections "
            "only (skip the Monthly section and header entirely), and have the "
            "Multi-timeframe context paragraph speak only to whether Weekly and Daily "
            "agree or conflict."
        )
    if gex_note:
        user_content += f"\n\nOptional GEX reference (dealer positioning, not price action):\n{gex_note}"

    opex_note = opex_context_note(today_et, symbol)
    if opex_note:
        user_content += f"\n\nOptions expiration context (calendar fact): {opex_note}"

    # SPY only - broad-market sector breadth isn't meaningful context for
    # an arbitrary single ticker the way it is for the index itself.
    sector_data, sector_debug = (
        fetch_sector_performance_alphavantage() if symbol.upper() == "SPY" else (None, None)
    )
    sector_note = None
    if sector_data:
        ranked = sorted(
            sector_data,
            key=lambda s: s["one_day_pct"] if s["one_day_pct"] is not None else -999,
            reverse=True,
        )

        def fmt_sector(s):
            windows = [
                (s["one_day_pct"], "1d"),
                (s["five_day_pct"], "5d"),
                (s["one_month_pct"], "1mo"),
                (s["three_month_pct"], "3mo"),
                (s["one_year_pct"], "1yr"),
            ]
            pieces = [f"{v:+.1f}%/{label}" for v, label in windows if v is not None]
            return f"{s['sector']} (" + ", ".join(pieces) + ")"

        sector_note = "; ".join(fmt_sector(s) for s in ranked)
    if sector_note:
        user_content += (
            f"\n\nOptional sector context (SPY only - the 11 S&P GICS sectors' "
            f"performance across several windows, ranked strongest to weakest "
            f"by today's move): {sector_note}"
        )

    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 1800,
        "system": CHART_ANALYSIS_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_content}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": anthropic_api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": "Anthropic API error", "detail": e.read().decode(errors="replace")}, 502
    except Exception as e:
        return {"error": "Anthropic request failed", "detail": str(e)}, 502

    text_block = next((b for b in result.get("content", []) if b.get("type") == "text"), None)
    if not text_block:
        return {"error": "No text content in model response"}, 502

    return {
        "symbol": symbol,
        "analysis": text_block["text"].strip(),
        "timeframes": timeframes_meta,
        "monthly_included": is_last_week_of_month,
        "sector_context": sector_data or None,
        "sector_context_debug": sector_debug,
        "gex_reference": {
            "session_date": gex_snapshot.get("session_date"),
            "levels": notable_gex_levels,
        } if gex_snapshot else None,
        # Kept at top level too (daily values) for any caller still
        # reading the pre-multi-timeframe response shape.
        "bars_used": timeframes_meta["daily"]["bars_used"],
        "date_range": timeframes_meta["daily"]["date_range"],
        "ma30": daily_ma30,
        "ma200": daily_ma200,
        "ma200_available": daily_ma200_available,
        "source": "tiger",
    }, 200


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            query = parse_qs(urlparse(self.path).query)

            # Diagnostic mode: check whether this Tiger account has
            # historical K-line access and what quota remains, without
            # running any of the actual GEX fetch/compute logic below.
            # Usage: /api/get-tiger-gex?mode=kline_quota
            if query.get("mode", [None])[0] == "kline_quota":
                try:
                    quote_client = get_quote_client()
                    quota_result = quote_client.get_kline_quota(with_details=True)
                    # quota_result is typically a list of dicts (one per
                    # kline type: 'kline', 'future_kline', 'option_kline')
                    # - pass it through as-is rather than guessing its
                    # exact shape, so whatever Tiger actually returns is
                    # visible.
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "kline_quota": quota_result,
                        "note": "If this account has no historical-quote "
                                "permission tier, Tiger may return an "
                                "empty result or an error here rather "
                                "than a quota breakdown - that itself is "
                                "the answer.",
                    }, default=str).encode())
                except Exception as e:
                    self.send_response(200)  # 200, not 500 - this IS the diagnostic answer
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "kline_quota": None,
                        "error": str(e),
                        "note": "The kline_quota call itself failed - this "
                                "usually means the account doesn't have "
                                "historical K-line/bar access at all, "
                                "rather than a transient network issue. "
                                "Check Tiger Trade app > Profile > My "
                                "Service > API Permissions for a "
                                "historical-quote tier.",
                    }).encode())
                return

            # Price-data mode: MA or volume-profile from Tiger's own bars,
            # as an alternative to Yahoo Finance for the same features.
            # Usage: /api/get-tiger-gex?mode=price_data&symbol=SPY&type=ma
            #        /api/get-tiger-gex?mode=price_data&symbol=SPY&type=volume-profile&days=5
            if query.get("mode", [None])[0] == "price_data":
                price_symbol = query.get("symbol", ["SPY"])[0].upper()
                data_type = query.get("type", ["ma"])[0]
                try:
                    price_quote_client = get_quote_client()
                    if data_type == "ma":
                        body, status = handle_price_data_ma(price_quote_client, price_symbol)
                    elif data_type == "volume-profile":
                        days = min(max(int(query.get("days", ["5"])[0]), 1), 10)
                        body, status = handle_price_data_volume_profile(price_quote_client, price_symbol, days)
                    else:
                        body, status = {"error": 'type must be "ma" or "volume-profile"'}, 400
                except Exception as e:
                    body, status = {"error": "Tiger price-data fetch failed", "detail": str(e)}, 502
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body, default=str).encode())
                return

            # Chart Analysis mode: a pure price-action technical read from
            # Tiger's daily bars, deliberately separate from GEX reasoning.
            # Usage: /api/get-tiger-gex?mode=chart_analysis&symbol=SPY
            if query.get("mode", [None])[0] == "chart_analysis":
                chart_symbol = query.get("symbol", ["SPY"])[0].upper()
                try:
                    chart_quote_client = get_quote_client()
                    body, status = handle_chart_analysis(chart_quote_client, chart_symbol)
                except Exception as e:
                    body, status = {"error": "Chart analysis failed", "detail": str(e)}, 502
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body, default=str).encode())
                return

            symbol = query.get("symbol", ["SPY"])[0].upper()
            read_type = query.get("read_type", ["daily"])[0]

            risk_free_rate = get_config_float("TIGER_RISK_FREE_RATE", DEFAULT_RISK_FREE_RATE)
            dividend_yield = get_config_float("TIGER_DIVIDEND_YIELD", DEFAULT_DIVIDEND_YIELD)
            strike_band_pct = get_config_float("TIGER_STRIKE_BAND_PCT", DEFAULT_STRIKE_BAND_PCT)

            spot = get_spot_price(symbol)
            strike_low = spot * (1 - strike_band_pct)
            strike_high = spot * (1 + strike_band_pct)
            quote_client = get_quote_client()
            underlying_iv, underlying_iv_source = get_underlying_iv(quote_client, symbol)

            now_et = datetime.now(timezone.utc).astimezone(EASTERN)
            today = now_et.date()
            today_str = today.isoformat()

            expirations = quote_client.get_option_expirations(symbols=[symbol])
            future = expirations[expirations["date"] >= today_str]

            if future.empty:
                self.send_response(422)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "No upcoming expirations found for this symbol"}).encode())
                return

            # Aggregate across every remaining trading day THIS WEEK
            # (today through this week's Friday), not just one expiration.
            week_start = today - timedelta(days=today.weekday())  # this Monday
            week_end = week_start + timedelta(days=6)              # this Sunday
            this_week = future[
                (future["date"] >= week_start.isoformat()) &
                (future["date"] <= week_end.isoformat())
            ]

            if not this_week.empty:
                expiries_to_fetch = this_week["date"].tolist()
            else:
                # Nothing left this week (e.g. running after Friday's
                # close) - fall back to the single nearest upcoming one.
                expiries_to_fetch = [future["date"].tolist()[0]]

            by_strike = {}
            expiries_with_data = []
            expiries_empty = []
            diagnostics = {}  # per-expiry row counts, to make future debugging faster

            for expiry in expiries_to_fetch:
                t_years = years_to_expiry(expiry, now_et)
                chain = quote_client.get_option_chain(symbol, expiry)

                # Narrow to strikes within the configured band around spot
                # before doing anything else with this chain - the far
                # tails add parse/gamma-calc time and payload size for
                # negligible GEX contribution. Non-numeric/missing strikes
                # are left in the "keep" set here; they still hit the
                # existing per-row missing/conversion-error handling below
                # rather than being silently dropped by the filter itself.
                if not chain.empty:
                    strike_numeric = pd.to_numeric(chain["strike"], errors="coerce")
                    in_band = strike_numeric.isna() | (
                        (strike_numeric >= strike_low) & (strike_numeric <= strike_high)
                    )
                    chain = chain[in_band]

                total_rows = len(chain)
                skipped_missing = 0        # raw_strike/oi was None (iv is no longer a skip reason - see fallback below)
                skipped_nan = 0            # converted to float but was NaN
                skipped_conversion_error = 0  # couldn't convert to float at all
                skipped_gamma_failed = 0   # converted fine, but bs_gamma rejected the inputs
                used_rows = 0
                used_fallback_iv_rows = 0  # rows where per-contract iv was unusable and underlying_iv was substituted
                sample_rows = []  # a few raw (pre-conversion) values, for debugging
                seen_contracts = set()   # (strike, put_call) pairs already processed this expiry
                duplicate_rows = 0       # same contract appearing more than once - data-quality red flag

                day_had_data = False
                for _, row in chain.iterrows():
                    raw_strike = row["strike"]
                    oi = row["open_interest"]
                    iv = row["implied_vol"]

                    if len(sample_rows) < 3:
                        sample_rows.append({
                            "strike": repr(raw_strike),
                            "open_interest": repr(oi),
                            "implied_vol": repr(iv),
                            "put_call": repr(row.get("put_call")),
                        })

                    if raw_strike is None or oi is None:
                        skipped_missing += 1
                        continue
                    try:
                        strike = float(raw_strike)
                        oi = float(oi)
                        # iv may legitimately be None/missing on some rows -
                        # that's fine now, it just means we fall back below.
                        iv = float(iv) if iv is not None else 0.0
                    except (TypeError, ValueError):
                        skipped_conversion_error += 1
                        continue
                    # pandas represents missing numeric cells as NaN, not
                    # None - the "is None" check above does NOT catch this,
                    # so it needs an explicit isnan check or NaN silently
                    # poisons the gamma calc (gamma/contract_gex become NaN,
                    # and "abs(NaN) >= 1" is always False, so the row never
                    # counts as data without ever raising an error).
                    if math.isnan(strike) or math.isnan(oi) or math.isnan(iv):
                        skipped_nan += 1
                        continue

                    # Tiger's per-contract implied_vol is coming back as 0.0
                    # for every row in practice (confirmed in testing) - use
                    # the underlying's aggregate IV as a flat fallback
                    # whenever a contract's own IV isn't usable, rather than
                    # dropping the contract entirely. Tracked per-row (not
                    # incremented directly here) so the eventual count only
                    # includes rows that actually end up in used_rows below -
                    # otherwise the two counters track different populations
                    # (this one would include dust/duplicate rows the other
                    # excludes) and a rate computed from them can exceed 100%.
                    used_fallback_this_row = False
                    if iv <= 0:
                        iv = underlying_iv
                        used_fallback_this_row = True

                    contract_key = (strike, row["put_call"])
                    if contract_key in seen_contracts:
                        duplicate_rows += 1
                        continue  # don't double-count gamma exposure for a contract already processed
                    seen_contracts.add(contract_key)

                    if strike not in by_strike:
                        by_strike[strike] = 0.0
                    gamma = bs_gamma(spot, strike, t_years, iv, risk_free_rate, dividend_yield)
                    if gamma is None or math.isnan(gamma):
                        skipped_gamma_failed += 1
                        continue
                    contract_gex = gamma * oi * 100 * (spot ** 2) * 0.01
                    if abs(contract_gex) >= 1:  # ignore dust-level contributions when checking "had data"
                        day_had_data = True
                        used_rows += 1
                        if used_fallback_this_row:
                            used_fallback_iv_rows += 1
                    # SIGN CONVENTION (explicit, written contract - audited Sept 2026):
                    # this is "Model A" per Chilingarian (2026, SSRN 7131778) - the
                    # SqueezeMetrics open-interest proxy. Calls contribute +gamma,
                    # puts contribute -gamma, which assumes dealers are net long the
                    # calls and net short the puts customers hold. Black-Scholes
                    # gamma itself is identical and positive for a call and put at
                    # the same strike (put-call parity) - the sign here is NOT a
                    # property of the option Greek, it is this stated inventory
                    # assumption. This is a proxy for dealer positioning inferred
                    # from open interest, not a direct observation of the dealer
                    # book - direct-data research (Amaya et al. 2025) finds this
                    # assumption is frequently wrong for SPX specifically. Model A
                    # is used here deliberately and consistently; if this ever
                    # changes, every downstream regime read inverts silently.
                    if row["put_call"] == "CALL":
                        by_strike[strike] += contract_gex
                    elif row["put_call"] == "PUT":
                        by_strike[strike] -= contract_gex

                diagnostics[expiry] = {
                    "t_years": t_years,
                    "total_rows": total_rows,
                    "used_rows": used_rows,
                    "used_fallback_iv_rows": used_fallback_iv_rows,
                    "skipped_missing": skipped_missing,
                    "skipped_nan": skipped_nan,
                    "skipped_conversion_error": skipped_conversion_error,
                    "skipped_gamma_failed": skipped_gamma_failed,
                    "duplicate_rows": duplicate_rows,
                    "sample_rows": sample_rows,
                }

                if day_had_data:
                    expiries_with_data.append(expiry)
                else:
                    expiries_empty.append(expiry)

            if not expiries_with_data:
                # Every expiration this week came back with no usable
                # open_interest/implied_vol data - genuinely nothing
                # available right now, not something the gamma fix can
                # help with (e.g. a very thin/illiquid chain).
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "ticker": symbol,
                    "spot_price": round(spot, 2),
                    "expiration": expiries_to_fetch[-1] if expiries_to_fetch else None,
                    "levels": [],
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "read_type": read_type,
                    "source": "tiger",
                    "note": f"Checked {len(expiries_to_fetch)} expirations this week ({', '.join(expiries_to_fetch)}), all returned no usable open_interest/implied_vol data.",
                    "underlying_iv_used_as_fallback": underlying_iv,
                    "underlying_iv_source": underlying_iv_source,
                    "diagnostics": diagnostics,
                }).encode())
                return

            # Match parse-gex.js's output shape exactly: levels sorted
            # descending by strike, values in millions, flip zone flagged.
            sorted_strikes = sorted(by_strike.keys(), reverse=True)
            levels = []
            for strike in sorted_strikes:
                net_gex_millions = round(by_strike[strike] / 1_000_000, 2)
                if abs(net_gex_millions) < 0.01:
                    continue  # matches parse-gex.js's "omit no-data rows" behavior
                levels.append({"strike": strike, "net_gex_millions": net_gex_millions})

            for i in range(len(levels) - 1):
                a, b = levels[i], levels[i + 1]
                if (a["net_gex_millions"] >= 0) != (b["net_gex_millions"] >= 0):
                    a["is_flip_zone"] = True
                    b["is_flip_zone"] = True
                    break

            # Sanity check: spot should sit reasonably close to the strike
            # range fetched (this is Tiger's own chain, so a mismatch here
            # usually means Yahoo returned a stale/wrong-ticker price
            # rather than a parsing error, but the safeguard is worth
            # having regardless).
            spot_price_final = round(spot, 2)
            spot_price_corrected = False
            spot_price_raw = None
            if levels:
                strike_values = sorted(l["strike"] for l in levels)
                median_strike = strike_values[len(strike_values) // 2]
                relative_diff = abs(spot_price_final - median_strike) / median_strike
                if relative_diff > 0.15:
                    spot_price_raw = spot_price_final
                    spot_price_final = median_strike
                    spot_price_corrected = True

            # ---- DATA QUALITY CHECKS ----
            # Aggregate, human-readable pass/warn checks on top of the raw
            # per-expiry diagnostics above - the goal is that a bad fetch
            # is visibly flagged in the response itself, not something you
            # have to notice by manually reading diagnostics.
            data_quality = {}

            # 1. Market hours - Tiger's gamma/OI are documented to come
            # back flat outside active trading hours (see README known
            # limitation). A fetch outside 9:30-16:00 ET on a weekday is
            # not necessarily bad, but its numbers should be trusted less.
            is_weekday = now_et.weekday() < 5
            market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
            in_market_hours = is_weekday and market_open <= now_et <= market_close
            data_quality["market_hours"] = {
                "status": "pass" if in_market_hours else "warn",
                "detail": None if in_market_hours else
                    f"Fetched at {now_et.strftime('%H:%M %Z')} on a "
                    f"{'weekday' if is_weekday else 'weekend'}, outside 9:30-16:00 ET - "
                    f"gamma/OI are known to come back flat outside market hours.",
            }

            # 2. IV fallback rate - Tiger's per-contract implied_vol is a
            # known-broken field (documented above); this makes that
            # visible as a top-level check instead of only in gamma_inputs.
            total_used = sum(d["used_rows"] for d in diagnostics.values())
            total_fallback = sum(d["used_fallback_iv_rows"] for d in diagnostics.values())
            fallback_rate_pct = round(100 * total_fallback / total_used, 1) if total_used else 0.0
            data_quality["iv_fallback_rate"] = {
                "status": "warn" if fallback_rate_pct >= 95 else "pass",
                "fallback_rate_pct": fallback_rate_pct,
                "detail": "Nearly all rows used the flat underlying-level IV fallback - "
                          "strike-to-strike vol skew is not reflected in this gamma calc."
                          if fallback_rate_pct >= 95 else None,
            }

            # 3. Coverage - flags a chain that technically returned rows
            # but barely any usable data made it through (thin/illiquid
            # chain, or a fetch that silently mostly failed).
            MIN_EXPECTED_USED_ROWS = 20
            data_quality["coverage"] = {
                "status": "warn" if total_used < MIN_EXPECTED_USED_ROWS else "pass",
                "total_used_rows": total_used,
                "detail": f"Only {total_used} usable contract rows across all fetched "
                          f"expiries - unusually thin, treat this snapshot cautiously."
                          if total_used < MIN_EXPECTED_USED_ROWS else None,
            }

            # 4. Duplicate contracts - the same (strike, put/call) appearing
            # more than once within an expiry is a red flag for a corrupted
            # or double-counted fetch. Rows are already de-duplicated
            # before being summed (see the per-row loop above); this just
            # surfaces that it happened.
            total_duplicates = sum(d["duplicate_rows"] for d in diagnostics.values())
            data_quality["duplicate_rows"] = {
                "status": "warn" if total_duplicates > 0 else "pass",
                "count": total_duplicates,
                "detail": f"{total_duplicates} duplicate (strike, put/call) row(s) found and "
                          f"skipped after the first occurrence - Tiger's chain data may be "
                          f"inconsistent this fetch." if total_duplicates > 0 else None,
            }

            # 5. Day-over-day sanity - compare against the most recent
            # prior D1 snapshot for this ticker. A huge, implausible jump
            # in spot or total |GEX| is more often bad data than a real
            # market move, and is worth flagging even though it's best-
            # effort (skipped entirely if no prior snapshot or D1 is
            # unreachable, rather than blocking the response).
            # Uses `today` (already computed from now_et above, i.e. the
            # US Eastern trading date) rather than date.today() (server
            # local time / UTC on Vercel) - the two disagree for roughly
            # 8pm-midnight ET each day, when UTC has already rolled to the
            # next calendar date but the ET trading day hasn't. Using the
            # wrong one here would compare against - or later overwrite -
            # the wrong day's snapshot.
            prior_snapshot = fetch_prior_snapshot_from_d1(symbol, today.isoformat())
            if prior_snapshot and prior_snapshot.get("levels"):
                prior_spot = prior_snapshot.get("spot_price")
                prior_total_gex = sum(abs(l["net_gex_millions"]) for l in prior_snapshot["levels"])
                current_total_gex = sum(abs(l["net_gex_millions"]) for l in levels)
                spot_change_pct = (abs(spot_price_final - prior_spot) / prior_spot * 100) if prior_spot else None
                gex_ratio = (current_total_gex / prior_total_gex) if prior_total_gex > 0 else None
                spot_jump_flag = spot_change_pct is not None and spot_change_pct > 8
                gex_jump_flag = gex_ratio is not None and (gex_ratio > 5 or gex_ratio < 0.2)
                data_quality["day_over_day"] = {
                    "status": "warn" if (spot_jump_flag or gex_jump_flag) else "pass",
                    "prior_session_date": prior_snapshot.get("captured_at", "")[:10] or None,
                    "spot_change_pct": round(spot_change_pct, 2) if spot_change_pct is not None else None,
                    "total_abs_gex_ratio_vs_prior": round(gex_ratio, 2) if gex_ratio is not None else None,
                    "detail": "Spot or total GEX magnitude moved implausibly versus the last "
                              "stored snapshot - could be a real move, but worth a manual check "
                              "before trusting this read." if (spot_jump_flag or gex_jump_flag) else None,
                }
            else:
                data_quality["day_over_day"] = {"status": "unavailable", "detail": "No prior snapshot found for comparison."}

            data_quality["overall"] = "warn" if any(
                v.get("status") == "warn" for v in data_quality.values() if isinstance(v, dict)
            ) else "pass"

            response_body = {
                "ticker": symbol,
                "spot_price": spot_price_final,
                "expiration": expiries_with_data[-1],  # latest date actually contributing data, for display purposes
                "expirations_included": expiries_with_data,  # full transparency on what got aggregated
                "levels": levels,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "read_type": read_type,
                "source": "tiger",  # distinguishes this from screenshot-sourced data
                "gamma_method": "black_scholes_local",  # gamma is computed locally, not read from Tiger (see header comment)
                "dealer_positioning_convention": "model_a_squeezemetrics_proxy",  # inferred from OI, not observed - see sign-convention comment above
                "strike_band_pct": strike_band_pct,
                "strike_range_fetched": [round(strike_low, 2), round(strike_high, 2)],
                "gamma_inputs": {
                    "risk_free_rate": risk_free_rate,
                    "dividend_yield": dividend_yield,
                    "underlying_iv_used_as_fallback": underlying_iv,
                    "underlying_iv_source": underlying_iv_source,  # "option_analysis_30d" (normal) or "hard_fallback" (Tiger call itself failed)
                    "note": "Per-contract implied_vol from Tiger's chain endpoint is currently coming back as 0.0 for every row (confirmed in testing, undocumented by Tiger) - this flat underlying-level IV is substituted whenever that happens. This loses strike-to-strike vol skew.",
                },
                "diagnostics": diagnostics,
                "data_quality": data_quality,
            }
            if expiries_empty:
                response_body["expirations_skipped_empty"] = expiries_empty
            if spot_price_corrected:
                response_body["spot_price_raw"] = spot_price_raw
                response_body["spot_price_corrected"] = True

            # Persist the full snapshot (all strikes, not summarized) to
            # D1, keyed by today's US Eastern trading date (NOT
            # date.today(), which is server-local/UTC and disagrees with
            # ET for part of every evening - see the day-over-day comment
            # above for why that distinction matters here) - serialize
            # before adding the storage-status fields below, so what gets
            # stored is the clean snapshot itself, not the storage result
            # describing it.
            session_date = today.isoformat()
            gex_data_json = json.dumps(response_body)
            storage_result = save_snapshot_to_d1(symbol, session_date, gex_data_json)
            response_body["stored"] = storage_result["stored"]
            if not storage_result["stored"]:
                response_body["storage_detail"] = storage_result

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(response_body).encode())

        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
