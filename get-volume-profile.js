// Route: GET /api/get-volume-profile?symbol=SPY&days=5
//
// Builds a volume-at-price profile from intraday bars (same Yahoo Finance
// chart endpoint used by get-ma.js). Buckets traded volume into price
// levels to find:
//   - Point of Control (POC): the price level with the most volume
//   - Value Area: the tightest price range containing ~70% of total volume
//     (the standard value-area definition used in volume profile analysis)
//
// This is a genuine volume-at-price profile, not just a volume trend line -
// it answers "where did the most trading actually happen," which is a
// different (and complementary) question to what GEX answers ("where are
// dealers hedging").

export default async function handler(req, res) {
  if (req.method !== 'GET') {
    return res.status(405).json({ error: 'Use GET' });
  }

  const { symbol, days = '5' } = req.query;
  if (!symbol) {
    return res.status(400).json({ error: 'Missing "symbol" query param' });
  }

  const lookbackDays = Math.min(Math.max(parseInt(days, 10) || 5, 1), 10);

  try {
    const period2 = Math.floor(Date.now() / 1000);
    const period1 = period2 - lookbackDays * 24 * 60 * 60;

    // 30-minute bars: fine enough for a useful profile, coarse enough to
    // stay within Yahoo's intraday history limits for up to ~10 days back.
    const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}?period1=${period1}&period2=${period2}&interval=30m`;

    const yahooRes = await fetch(url, {
      headers: {
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
  } catch (err) {
    return res.status(500).json({ error: 'Server error', detail: String(err) });
  }
}
