"""
tools/audit_historical_pnl.py — Bybit historical PnL audit.

Pulls ALL closed positions from Bybit's closed-pnl endpoint in 7-day
paginated chunks and buckets them by calendar week + asset. Designed
to diagnose the -$18k cumRealisedPnl on the account.

Usage (on VPS):
    cd /opt/trading/hermes_trading
    set -a && source .env && set +a
    python -m tools.audit_historical_pnl [--assets BTC ETH SOL TAO] [--weeks 52]

Output: summary table to stdout + optional CSV to audit_pnl_<date>.csv
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt


def _get_exchange() -> ccxt.Exchange:
    api_key = os.getenv("BYBIT_API_KEY", "")
    secret  = os.getenv("BYBIT_API_SECRET", "")
    if not api_key or not secret:
        key_path = os.getenv("BYBIT_RSA_PRIVATE_KEY_PATH", "")
        if key_path and Path(key_path).exists():
            secret = Path(key_path).read_text()
        else:
            sys.exit("BYBIT_API_KEY and BYBIT_API_SECRET (or RSA key) must be set")
    return ccxt.bybit({
        "apiKey": api_key,
        "secret": secret,
        "enableRateLimit": True,
        "options": {"defaultType": "linear"},
    })


def _fetch_closed_pnl_window(exchange, symbol_clean: str, start_ms: int, end_ms: int) -> list[dict]:
    """Fetch all closed-pnl records within a time window via cursor pagination."""
    records = []
    cursor  = None
    while True:
        params = {
            "category":   "linear",
            "symbol":     symbol_clean,
            "startTime":  str(start_ms),
            "endTime":    str(end_ms),
            "limit":      100,
        }
        if cursor:
            params["cursor"] = cursor
        try:
            result = exchange.private_get_v5_position_closed_pnl(params)
        except Exception as e:
            print(f"  Warning: API error for {symbol_clean} [{start_ms}..{end_ms}]: {e}", file=sys.stderr)
            break
        items  = result.get("result", {}).get("list", [])
        cursor = result.get("result", {}).get("nextPageCursor") or None
        records.extend(items)
        if not cursor or not items:
            break
        time.sleep(0.2)   # rate-limit courtesy
    return records


def _symbol_clean(asset: str) -> str:
    """BTC/USDT -> BTCUSDT"""
    return asset.replace("/", "").replace(":USDT", "")


def main():
    parser = argparse.ArgumentParser(description="Bybit historical PnL audit")
    parser.add_argument("--assets", nargs="+", default=["BTC/USDT", "ETH/USDT", "SOL/USDT", "TAO/USDT"])
    parser.add_argument("--weeks",  type=int, default=52, help="How many weeks back to pull (default 52)")
    parser.add_argument("--csv",    action="store_true", help="Also write CSV output file")
    args = parser.parse_args()

    exchange = _get_exchange()
    now      = datetime.now(timezone.utc)

    # Collect all records across assets and time windows
    all_records: list[dict] = []

    for asset in args.assets:
        sym = _symbol_clean(asset)
        print(f"\nFetching {asset} ({sym})...")
        week_end = now
        for w in range(args.weeks):
            week_start = week_end - timedelta(days=7)
            start_ms   = int(week_start.timestamp() * 1000)
            end_ms     = int(week_end.timestamp() * 1000)
            recs = _fetch_closed_pnl_window(exchange, sym, start_ms, end_ms)
            for r in recs:
                r["_asset"] = asset
            all_records.extend(recs)
            if recs:
                print(f"  Week {w+1}: {week_start.date()} → {week_end.date()}: {len(recs)} trades")
            week_end = week_start
            time.sleep(0.1)

    if not all_records:
        print("\nNo records found.")
        return

    # Parse and bucket by week
    print(f"\n{'='*70}")
    print(f"TOTAL RECORDS: {len(all_records)}")
    print(f"{'='*70}")

    # Group by calendar week
    by_week: dict[str, dict] = {}
    for r in all_records:
        created_ms = int(r.get("createdTime", 0) or 0)
        if not created_ms:
            continue
        dt   = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
        week = dt.strftime("%Y-W%W")
        asset = r.get("_asset", "?")
        key  = f"{week}|{asset}"
        if key not in by_week:
            by_week[key] = {"week": week, "asset": asset, "count": 0, "pnl": 0.0, "wins": 0, "losses": 0}
        pnl = float(r.get("closedPnl", 0) or 0)
        by_week[key]["count"]  += 1
        by_week[key]["pnl"]    += pnl
        if pnl > 0:
            by_week[key]["wins"]   += 1
        else:
            by_week[key]["losses"] += 1

    # Sort and print
    rows = sorted(by_week.values(), key=lambda x: x["week"])
    print(f"\n{'Week':<12} {'Asset':<12} {'Trades':>8} {'Wins':>6} {'Losses':>7} {'PnL (USDT)':>14}")
    print("-" * 65)
    total_pnl = 0.0
    for row in rows:
        pnl_str = f"{row['pnl']:+.2f}"
        print(f"{row['week']:<12} {row['asset']:<12} {row['count']:>8} {row['wins']:>6} {row['losses']:>7} {pnl_str:>14}")
        total_pnl += row["pnl"]

    print("-" * 65)
    print(f"{'TOTAL':>12} {'':<12} {len(all_records):>8} {'':>6} {'':>7} {total_pnl:+14.2f}")
    print(f"\nNote: cumRealisedPnl on Bybit may differ (includes fees, funding, liquidations).")

    if args.csv:
        fname = f"audit_pnl_{now.strftime('%Y%m%d')}.csv"
        with open(fname, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["week", "asset", "count", "wins", "losses", "pnl"])
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nCSV written: {fname}")


if __name__ == "__main__":
    main()
