"""
tools/fetch_ict_live_data.py -- maintains a rolling per-asset 15m OHLCV
cache for the live scanner. Seeds a fresh cache with SEED_DAYS of history
if none exists, otherwise does a small incremental fetch (only candles
since the last cached bar, with a small overlap to avoid gaps from a
still-forming candle at last update). Public market data only, no API key.

The cache is append-only / ever-growing by design (never truncated) --
this keeps bar indices stable across scan_asset() calls over the life of
the process, which matters for alert dedup (see scanner.py). At 15m
resolution this is small even over years (~35k rows/year, a few MB), so
there's no practical need to prune it.

Importable (fetch_or_update_cache) for the scanner loop, and runnable
standalone to seed/refresh caches ahead of time:
  python tools/fetch_ict_live_data.py
"""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

import ccxt

from hermes_trading.ict.util import Candle

ASSETS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "TAO/USDT"]
TIMEFRAME = "15m"
SEED_DAYS = 730  # 2 years -- fast to seed, extend later via tools/fetch_ict_backtest_data.py's pattern if desired
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "ict-live"
_TF_MS = 15 * 60 * 1000
_OVERLAP_BARS = 4  # re-fetch the last hour on each incremental update in case the newest bar was still forming


def _cache_path(symbol: str) -> Path:
    return OUT_DIR / f"{symbol.replace('/', '_')}.csv"


def _read_cache(symbol: str) -> list[Candle]:
    path = _cache_path(symbol)
    if not path.exists():
        return []
    candles = []
    with path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            candles.append(Candle(
                timestamp=int(float(row["timestamp"])), open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]), volume=float(row["volume"]),
            ))
    return candles


def _write_cache(symbol: str, candles: list[Candle]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(symbol)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for c in candles:
            writer.writerow([c.timestamp, c.open, c.high, c.low, c.close, c.volume])


def _fetch_range(exchange: ccxt.Exchange, symbol: str, since_ms: int) -> list[Candle]:
    all_rows: list[list[float]] = []
    limit = 1000
    since = since_ms
    while True:
        rows = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, since=since, limit=limit)
        if not rows:
            break
        all_rows.extend(rows)
        last_ts = rows[-1][0]
        next_since = last_ts + _TF_MS
        if len(rows) < limit:
            break
        since = next_since
        time.sleep(exchange.rateLimit / 1000)
    return [Candle(timestamp=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5]) for r in all_rows]


def fetch_or_update_cache(exchange: ccxt.Exchange, symbol: str) -> list[Candle]:
    """Seed if missing, else incrementally update. Returns the full up-to-date candle list."""
    existing = _read_cache(symbol)
    if not existing:
        since_ms = exchange.milliseconds() - SEED_DAYS * 24 * 60 * 60 * 1000
        fresh = _fetch_range(exchange, symbol, since_ms)
        _write_cache(symbol, fresh)
        return fresh

    resume_index = max(0, len(existing) - _OVERLAP_BARS)
    since_ms = existing[resume_index].timestamp
    new_rows = _fetch_range(exchange, symbol, since_ms)

    merged = {c.timestamp: c for c in existing[:resume_index]}
    for c in new_rows:
        merged[c.timestamp] = c
    combined = [merged[k] for k in sorted(merged)]
    _write_cache(symbol, combined)
    return combined


def main() -> None:
    exchange = ccxt.binance({"enableRateLimit": True})
    exchange.load_markets()
    for symbol in ASSETS:
        print(f"Updating {symbol} live cache...", file=sys.stderr)
        candles = fetch_or_update_cache(exchange, symbol)
        span_days = (candles[-1].timestamp - candles[0].timestamp) / (24 * 60 * 60 * 1000) if candles else 0
        print(f"  {symbol}: {len(candles)} candles, {span_days:.1f} days -> {_cache_path(symbol)}", file=sys.stderr)


if __name__ == "__main__":
    main()
