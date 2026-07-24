"""
tools/fetch_ict_live_data_bybit.py -- maintains a rolling per-asset 15m
OHLCV cache sourced from Bybit, for the live TRADING worker (as opposed
to tools/fetch_ict_live_data.py, which sources from Binance for the
alert-only scanner).

Why a separate Bybit-sourced cache rather than reusing the Binance one:
once real entries/stops/targets are computed from candles and then
enforced by Bybit's own native SL/TP on Bybit's own price feed, deciding
off a *different* exchange's candles is a real (if usually small)
cross-exchange basis-risk gap the backtest never modeled. This cache
keeps the live trading worker's decisions and the exchange's own fills
on the same price feed. The existing Binance-sourced cache and the
alert-only scanner that reads it are left completely untouched as a
rollback path.

Same shape as fetch_ict_live_data.py otherwise: append-only/ever-growing
(never truncated, so bar indices stay stable across the life of the
process), seeds SEED_DAYS on first run, small incremental fetch (with a
small overlap to catch a possibly-still-forming last candle) thereafter.
Public market data only, no API key needed for OHLCV.

Importable (fetch_or_update_cache) for the live worker loop, and runnable
standalone to seed/refresh caches ahead of time:
  python tools/fetch_ict_live_data_bybit.py
"""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

import ccxt

from hermes_trading.ict.util import Candle

ASSETS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "TAO/USDT",
          "BNB/USDT", "LINK/USDT", "SUI/USDT", "NEAR/USDT"]
TIMEFRAME = "15m"
SEED_DAYS = 730  # 2 years -- fast to seed, extend later if needed
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "ict-live-bybit"
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


def _to_perp_symbol(symbol: str) -> str:
    base, quote = symbol.split("/")
    return f"{base}/{quote}:{quote}"


def _fetch_range(exchange: ccxt.Exchange, symbol: str, since_ms: int) -> list[Candle]:
    all_rows: list[list[float]] = []
    limit = 1000
    since = since_ms
    perp_symbol = _to_perp_symbol(symbol)
    while True:
        rows = exchange.fetch_ohlcv(perp_symbol, timeframe=TIMEFRAME, since=since, limit=limit)
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
    exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "linear"}})
    exchange.load_markets()
    for symbol in ASSETS:
        print(f"Updating {symbol} Bybit live cache...", file=sys.stderr)
        candles = fetch_or_update_cache(exchange, symbol)
        span_days = (candles[-1].timestamp - candles[0].timestamp) / (24 * 60 * 60 * 1000) if candles else 0
        print(f"  {symbol}: {len(candles)} candles, {span_days:.1f} days -> {_cache_path(symbol)}", file=sys.stderr)


if __name__ == "__main__":
    main()
