// Route: GET /api/get-ma?symbol=SPY
//
// Mirrors the data path used by Howard's yfinance-based MA200 scanner:
// pulls daily closes from Yahoo Finance's public chart endpoint (the same
// unofficial endpoint yfinance wraps under the hood) and computes simple
// moving averages server-side. No API key required, but this is an
// unofficial endpoint, not a documented Yahoo API contract - if Yahoo
// changes or blocks it, this call will start failing and need a real
// provider swapped in (Alpha Vantage, Twelve Data, etc.) as a fallback.

export default async function handler(req, res) {
  if (req.method !== 'GET') {
    return res.status(405).json({ error: 'Use GET' });
  }

  const { symbol } = req.query;
  if (!symbol) {
    return res.status(400).json({ error: 'Missing "symbol" query param' });
  }

  try {
    // ~280 calendar days back covers 200+ trading days comfortably.
    const period2 = Math.floor(Date.now() / 1000);
    const period1 = period2 - 280 * 24 * 60 * 60;

    const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}?period1=${period1}&period2=${period2}&interval=1d`;

    const yahooRes = await fetch(url, {
      headers: {
        // Yahoo's endpoint returns 403 without a browser-like UA, same
        // fragility noted in the MA200 scanner's Wikipedia fetch.
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
      },
    });

    if (!yahooRes.ok) {
      return res.status(502).json({ error: 'Yahoo Finance request failed', status: yahooRes.status });
    }

    const data = await yahooRes.json();
    const result = data?.chart?.result?.[0];
    if (!result) {
      const err = data?.chart?.error?.description || 'No data returned for this symbol';
      return res.status(404).json({ error: err });
    }

    const closes = result.indicators?.quote?.[0]?.close || [];
    const validCloses = closes.filter((c) => typeof c === 'number');

    if (validCloses.length < 30) {
      return res.status(422).json({ error: 'Not enough price history to compute moving averages' });
    }

    const sma = (arr, period) => {
      if (arr.length < period) return null;
      const slice = arr.slice(-period);
      return +(slice.reduce((sum, v) => sum + v, 0) / period).toFixed(2);
    };

    // Annualized realized volatility from daily log returns - standard
    // close-to-close historical vol calculation. Used by Protocol Coffee
    // and Tea's IV-rich/cheap/fair check.
    const realizedVol = (arr, period) => {
      if (arr.length < period + 1) return null;
      const slice = arr.slice(-(period + 1));
      const logReturns = [];
      for (let i = 1; i < slice.length; i++) {
        logReturns.push(Math.log(slice[i] / slice[i - 1]));
      }
      const mean = logReturns.reduce((s, v) => s + v, 0) / logReturns.length;
      const variance = logReturns.reduce((s, v) => s + (v - mean) ** 2, 0) / (logReturns.length - 1);
      const dailyStdDev = Math.sqrt(variance);
      const annualized = dailyStdDev * Math.sqrt(252) * 100; // as a percentage
      return +annualized.toFixed(2);
    };

    const ma30 = sma(validCloses, 30);
    const ma200 = sma(validCloses, 200);
    const realizedVol10d = realizedVol(validCloses, 10);
    const realizedVol20d = realizedVol(validCloses, 20);
    const lastPrice = +validCloses[validCloses.length - 1].toFixed(2);

    return res.status(200).json({
      symbol: symbol.toUpperCase(),
      price: lastPrice,
      ma30,
      ma200,
      ma200_available: validCloses.length >= 200,
      realized_vol_10d_pct: realizedVol10d,
      realized_vol_20d_pct: realizedVol20d,
    });
  } catch (err) {
    return res.status(500).json({ error: 'Server error', detail: String(err) });
  }
}
