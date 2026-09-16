// Route: GET /api/get-price-data?type=ma&symbol=SPY
//        GET /api/get-price-data?type=volume-profile&symbol=SPY&days=5
//
// Merges the former get-ma.js and get-volume-profile.js into one endpoint,
// the same consolidation already done for analyze-gex-period.js and
// analyze-flow-period.js (Vercel's Hobby plan caps deployments at 12
// serverless functions). Behavior is otherwise unchanged from the two
// original files - both pull from the same Yahoo Finance chart endpoint
// and do local computation, no AI calls, so they merge cleanly by "type".
//
// Both are unofficial-endpoint dependent (the same public chart API
// yfinance wraps under the hood, not a documented Yahoo contract) - if
// Yahoo changes or blocks it, both types here start failing together and
// would need a real provider (Alpha Vantage, Twelve Data, etc.) swapped in.

async function fetchYahooChart(symbol, period1, period2, interval) {
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}?period1=${period1}&period2=${period2}&interval=${interval}`;
  const yahooRes = await fetch(url, {
    headers: {
      // Yahoo's endpoint returns 403 without a browser-like UA, same
      // fragility noted in the MA200 scanner's Wikipedia fetch.
      'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    },
  });
  if (!yahooRes.ok) {
    return { error: { status: 502, body: { error: 'Yahoo Finance request failed', status: yahooRes.status } } };
  }
  const data = await yahooRes.json();
  const result = data?.chart?.result?.[0];
  if (!result) {
    const err = data?.chart?.error?.description || 'No data returned for this symbol';
    return { error: { status: 404, body: { error: err } } };
  }
  return { result };
}

async function handleMa(req, res, symbol) {
  // Needs 200+ TRADING days, not calendar days. The US market trades
  // ~252 days out of 365 (~69%), so a naive 280-calendar-day window
  // averages only ~193 trading days - under the 200 needed, especially
  // in stretches with clustered holidays (Thanksgiving/Christmas/New
  // Year's). 380 calendar days comfortably clears 200 trading days even
  // in a holiday-heavy window, with real margin to spare.
  const period2 = Math.floor(Date.now() / 1000);
  const period1 = period2 - 380 * 24 * 60 * 60;

  const { result, error } = await fetchYahooChart(symbol, period1, period2, '1d');
  if (error) return res.status(error.status).json(error.body);

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
}

async function handleVolumeProfile(req, res, symbol) {
  const { days = '5' } = req.query;
  const lookbackDays = Math.min(Math.max(parseInt(days, 10) || 5, 1), 10);

  const period2 = Math.floor(Date.now() / 1000);
  const period1 = period2 - lookbackDays * 24 * 60 * 60;

  // 30-minute bars: fine enough for a useful profile, coarse enough to
  // stay within Yahoo's intraday history limits for up to ~10 days back.
  const { result, error } = await fetchYahooChart(symbol, period1, period2, '30m');
  if (error) return res.status(error.status).json(error.body);

  const quote = result.indicators?.quote?.[0] || {};
  const closes = quote.close || [];
  const volumes = quote.volume || [];

  const bars = [];
  for (let i = 0; i < closes.length; i++) {
    if (typeof closes[i] === 'number' && typeof volumes[i] === 'number' && volumes[i] > 0) {
      bars.push({ price: closes[i], volume: volumes[i] });
    }
  }

  if (bars.length < 10) {
    return res.status(422).json({ error: 'Not enough intraday data to build a volume profile' });
  }

  const prices = bars.map((b) => b.price);
  const minPrice = Math.min(...prices);
  const maxPrice = Math.max(...prices);
  const range = maxPrice - minPrice || 1;

  // ~30 buckets across the observed range keeps resolution reasonable
  // regardless of the ticker's price level.
  const bucketCount = 30;
  const bucketSize = range / bucketCount;

  const buckets = Array.from({ length: bucketCount }, (_, i) => ({
    priceLow: +(minPrice + i * bucketSize).toFixed(2),
    priceHigh: +(minPrice + (i + 1) * bucketSize).toFixed(2),
    volume: 0,
  }));

  bars.forEach(({ price, volume }) => {
    let idx = Math.floor((price - minPrice) / bucketSize);
    if (idx >= bucketCount) idx = bucketCount - 1;
    if (idx < 0) idx = 0;
    buckets[idx].volume += volume;
  });

  const totalVolume = buckets.reduce((sum, b) => sum + b.volume, 0);

  // Point of Control: the single bucket with the most volume.
  const pocBucket = buckets.reduce((max, b) => (b.volume > max.volume ? b : max), buckets[0]);
  const poc = +((pocBucket.priceLow + pocBucket.priceHigh) / 2).toFixed(2);

  // Value area: expand outward from the POC bucket, adding whichever
  // neighboring bucket (above or below) has more volume, until ~70% of
  // total volume is included. Standard value-area construction.
  const pocIndex = buckets.indexOf(pocBucket);
  let included = new Set([pocIndex]);
  let includedVolume = pocBucket.volume;
  let lowPtr = pocIndex - 1;
  let highPtr = pocIndex + 1;

  while (includedVolume < totalVolume * 0.7 && (lowPtr >= 0 || highPtr < bucketCount)) {
    const lowVol = lowPtr >= 0 ? buckets[lowPtr].volume : -1;
    const highVol = highPtr < bucketCount ? buckets[highPtr].volume : -1;

    if (highVol >= lowVol && highPtr < bucketCount) {
      included.add(highPtr);
      includedVolume += buckets[highPtr].volume;
      highPtr++;
    } else if (lowPtr >= 0) {
      included.add(lowPtr);
      includedVolume += buckets[lowPtr].volume;
      lowPtr--;
    } else {
      break;
    }
  }

  const includedIndices = [...included].sort((a, b) => a - b);
  const valueAreaLow = +buckets[includedIndices[0]].priceLow.toFixed(2);
  const valueAreaHigh = +buckets[includedIndices[includedIndices.length - 1]].priceHigh.toFixed(2);

  return res.status(200).json({
    symbol: symbol.toUpperCase(),
    lookback_days: lookbackDays,
    poc,
    value_area_low: valueAreaLow,
    value_area_high: valueAreaHigh,
    buckets: buckets.map((b) => ({
      price_low: b.priceLow,
      price_high: b.priceHigh,
      volume: b.volume,
    })),
  });
}

export default async function handler(req, res) {
  if (req.method !== 'GET') {
    return res.status(405).json({ error: 'Use GET' });
  }

  const { type, symbol } = req.query;
  if (!symbol) {
    return res.status(400).json({ error: 'Missing "symbol" query param' });
  }
  if (type !== 'ma' && type !== 'volume-profile') {
    return res.status(400).json({ error: 'type must be "ma" or "volume-profile"' });
  }

  try {
    if (type === 'ma') {
      return await handleMa(req, res, symbol);
    }
    return await handleVolumeProfile(req, res, symbol);
  } catch (err) {
    return res.status(500).json({ error: 'Server error', detail: String(err) });
  }
}
