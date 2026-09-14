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
# strike is summed across all of them. This also means an empty same-day
# (0DTE) chain - a real Tiger API data quirk seen in testing - just
# contributes zero rather than needing special-case handling: whichever
# days in the week actually have data drive the result, and
# `expirations_included` / `expirations_skipped_empty` in the response
# show exactly which ones did.
#
# UNVALIDATED as of setup: Tiger's computed numbers have not yet been
# compared against Bullflow's for the same ticker/moment. Treat this as
# an experimental second source until that comparison happens on a real
# trading day.
#
# SETUP: add TIGER_OPENAPI_CONFIG as an environment variable in this
# Vercel project (same value used in the separate tiger-gex-heatmap
# project - the full contents of your tiger_openapi_config.properties
# file). Also add a requirements.txt at the repo root listing: tigeropen

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import os
import tempfile
from datetime import date, datetime, timezone, timedelta

from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.quote.quote_client import QuoteClient


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


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            query = parse_qs(urlparse(self.path).query)
            symbol = query.get("symbol", ["SPY"])[0].upper()
            read_type = query.get("read_type", ["daily"])[0]

            spot = get_spot_price(symbol)
            quote_client = get_quote_client()

            expirations = quote_client.get_option_expirations(symbols=[symbol])
            today = date.today()
            today_str = today.isoformat()
            future = expirations[expirations["date"] >= today_str]

            if future.empty:
                self.send_response(422)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "No upcoming expirations found for this symbol"}).encode())
                return

            # Aggregate across every remaining trading day THIS WEEK
            # (today through this week's Friday), not just one expiration.
            # This also naturally solves the earlier bug where a same-day
            # 0DTE chain came back completely empty: that day just
            # contributes zero to the sum instead of needing a guess about
            # which single expiration to fall back to.
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
                chain = quote_client.get_option_chain(symbol, expiry)

                day_had_data = False
                for _, row in chain.iterrows():
                    strike = row["strike"]
                    oi = row["open_interest"]
                    gamma = row["gamma"]
                    if strike not in by_strike:
                        by_strike[strike] = 0.0
                    if oi is None or gamma is None:
                        continue
                    try:
                        oi = float(oi)
                        gamma = float(gamma)
                    except (TypeError, ValueError):
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
                # Every expiration this week came back empty - genuinely
                # no data available right now (market closed), not
                # something aggregation can fix.
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
                    "note": f"Checked {len(expiries_to_fetch)} expirations this week ({', '.join(expiries_to_fetch)}), all returned empty/zero data - likely market closed.",
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
