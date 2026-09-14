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

from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.quote.quote_client import QuoteClient

EASTERN = ZoneInfo("America/New_York")

DEFAULT_RISK_FREE_RATE = 0.043
DEFAULT_DIVIDEND_YIELD = 0.012

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


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            query = parse_qs(urlparse(self.path).query)
            symbol = query.get("symbol", ["SPY"])[0].upper()
            read_type = query.get("read_type", ["daily"])[0]

            risk_free_rate = get_config_float("TIGER_RISK_FREE_RATE", DEFAULT_RISK_FREE_RATE)
            dividend_yield = get_config_float("TIGER_DIVIDEND_YIELD", DEFAULT_DIVIDEND_YIELD)

            spot = get_spot_price(symbol)
            quote_client = get_quote_client()

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

            for expiry in expiries_to_fetch:
                t_years = years_to_expiry(expiry, now_et)
                chain = quote_client.get_option_chain(symbol, expiry)

                day_had_data = False
                for _, row in chain.iterrows():
                    strike = row["strike"]
                    oi = row["open_interest"]
                    iv = row["implied_vol"]
                    if strike not in by_strike:
                        by_strike[strike] = 0.0
                    if oi is None or iv is None:
                        continue
                    try:
                        oi = float(oi)
                        iv = float(iv)
                    except (TypeError, ValueError):
                        continue
                    gamma = bs_gamma(spot, strike, t_years, iv, risk_free_rate, dividend_yield)
                    if gamma is None:
                        continue
                    contract_gex = gamma * oi * 100 * (spot ** 2) * 0.01
                    if abs(contract_gex) >= 1:  # ignore dust-level contributions when checking "had data"
                        day_had_data = True
                    if row["put_call"] == "CALL":
                        by_strike[strike] += contract_gex
                    elif row["put_call"] == "PUT":
                        by_strike[strike] -= contract_gex

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
                "gamma_inputs": {
                    "risk_free_rate": risk_free_rate,
                    "dividend_yield": dividend_yield,
                },
            }
            if expiries_empty:
                response_body["expirations_skipped_empty"] = expiries_empty
            if spot_price_corrected:
                response_body["spot_price_raw"] = spot_price_raw
                response_body["spot_price_corrected"] = True

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
