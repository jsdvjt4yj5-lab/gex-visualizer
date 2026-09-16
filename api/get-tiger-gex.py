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


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            query = parse_qs(urlparse(self.path).query)
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
                    # dropping the contract entirely.
                    if iv <= 0:
                        iv = underlying_iv
                        used_fallback_iv_rows += 1

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
