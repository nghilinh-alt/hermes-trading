"""
tools/run_ict_backtest.py -- run the Phase 2 backtest engine against the
CSVs fetched by fetch_ict_backtest_data.py and print metrics per asset.

Run once locally: python tools/run_ict_backtest.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

from hermes_trading.ict.backtest import compute_metrics, run_backtest_single_asset
from hermes_trading.ict.util import Candle

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "ict-backtest"
ASSETS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "TAO/USDT"]
EQUITY0 = 1000.0


def load_csv(symbol: str) -> list[Candle]:
    path = DATA_DIR / f"{symbol.replace('/', '_')}.csv"
    candles = []
    with path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            candles.append(Candle(
                timestamp=int(float(row["timestamp"])),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            ))
    return candles


def main() -> None:
    for symbol in ASSETS:
        path = DATA_DIR / f"{symbol.replace('/', '_')}.csv"
        if not path.exists():
            print(f"{symbol}: no data file, skipping", file=sys.stderr)
            continue
        candles = load_csv(symbol)
        span_days = (candles[-1].timestamp - candles[0].timestamp) / 86_400_000
        print(f"\n=== {symbol} ({len(candles)} 15m candles, {span_days:.0f} days) ===")

        result = run_backtest_single_asset(candles, symbol, EQUITY0)
        metrics = compute_metrics(result.trades, EQUITY0, result.final_equity)

        print(f"  considered setups: {result.considered_setups}")
        print(f"  qualified setups:  {result.qualified_setups}")
        print(f"  trades:            {metrics['trades']}")
        if metrics["trades"]:
            print(f"  win rate:          {metrics['win_rate']:.1%}")
            print(f"  expectancy (R):    {metrics['expectancy_r']:.2f}")
            print(f"  profit factor:     {metrics['profit_factor']:.2f}")
            print(f"  max drawdown:      {metrics['max_drawdown_pct']:.1%}")
            print(f"  avg hold (bars):   {metrics['avg_hold_bars']:.1f}")
            print(f"  total PnL ($):     {metrics['total_pnl_usd']:.2f}")
            trades_per_month = metrics["trades"] / (span_days / 30.0)
            print(f"  trades/month:      {trades_per_month:.2f}")
            for t in result.trades:
                print(f"    {t.close_reason:12s} entry={t.entry_price:.2f} exit={t.exit_price:.2f} "
                      f"pnl=${t.pnl_usd:.2f} R={t.r_multiple:.2f} hold={t.hold_bars}h grade={t.grade.value}")
        else:
            print("  (no trades)")


if __name__ == "__main__":
    main()
