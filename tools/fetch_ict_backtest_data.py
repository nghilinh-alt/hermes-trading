"""
tools/fetch_ict_backtest_data.py -- one-off fetch of 15m OHLCV history for the
ICT Phase 2 backtest. Public market data only (no API key needed). Saves CSVs
to data/ict-backtest/<asset>.csv (timestamp,open,high,low,close,volume).

Run once locally: python tools/fetch_ict_backtest_data.py
"""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

import ccxt

ASSETS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "TAO/USDT"]
TIMEFRAME = "15m"
DAYS_BACK = 730
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "ict-backtest"


def fetch_asset(exchange: ccxt.Exchange, symbol: str, days_back: int) -> list[list[float]]:
    tf_ms = 15 * 60 * 1000
    since = exchange.milliseconds() - days_back * 24 * 60 * 60 * 1000
    all_rows: list[list[float]] = []
    limit = 1000
    while True:
        rows = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, since=since, limit=limit)
        if not rows:
            break
        all_rows.extend(rows)
        last_ts = rows[-1][0]
        next_since = last_ts + tf_ms
        if next_since <= since or len(rows) < limit:
            if len(rows) < limit:
                break
        since = next_since
        time.sleep(exchange.rateLimit / 1000)
        if len(all_rows) > (days_back + 5) * 96:
            break
    # de-dup + sort (paginated fetches can overlap at the boundary)
    seen = {}
    for row in all_rows:
        seen[row[0]] = row
    return [seen[k] for k in sorted(seen)]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    exchange = ccxt.binance({"enableRateLimit": True})
    exchange.load_markets()

    for symbol in ASSETS:
        print(f"Fetching {symbol} ({DAYS_BACK}d, {TIMEFRAME})...", file=sys.stderr)
        rows = fetch_asset(exchange, symbol, DAYS_BACK)
        if not rows:
            print(f"  no data for {symbol}, skipping", file=sys.stderr)
            continue
        out_path = OUT_DIR / f"{symbol.replace('/', '_')}.csv"
        with out_path.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
            writer.writerows(rows)
        span_days = (rows[-1][0] - rows[0][0]) / (24 * 60 * 60 * 1000)
        print(f"  {symbol}: {len(rows)} candles, {span_days:.1f} days -> {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
