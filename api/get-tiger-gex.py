# Vercel Python serverless function.
# Route: GET /api/get-tiger-gex?symbol=SPY&read_type=daily
#
# Add-on data source, alongside the existing screenshot-based parse-gex.js.
# Produces the EXACT SAME JSON shape parse-gex.js does, so every
# downstream feature (MA confluence, volume profile, weekly/monthly
# comparisons, PDF export) works identically regardless of which source
# a given snapshot came from.
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
from datetime import date, datetime, timezone

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
            today_str = date.today().isoformat()
            future = expirations[expirations["date"] >= today_str]
            nearest_expiry = future.iloc[0]["date"]

            chain = quote_client.get_option_chain(symbol, nearest_expiry)

            by_strike = {}
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
                if row["put_call"] == "CALL":
                    by_strike[strike] += contract_gex
                elif row["put_call"] == "PUT":
                    by_strike[strike] -= contract_gex

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

            response_body = {
                "ticker": symbol,
                "spot_price": round(spot, 2),
                "expiration": nearest_expiry,
                "levels": levels,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "read_type": read_type,
                "source": "tiger",  # distinguishes this from screenshot-sourced data
            }

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
