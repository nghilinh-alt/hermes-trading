"""
tools/split_sample_check.py -- split-sample stability check (spec S:11's
validation guardrail, informal version).

Not a true walk-forward re-optimization (parameters were already tuned
against the full 2-year window in the calibration pass, so this can't
cleanly separate "in-sample" from "out-of-sample" for the calibration
choice itself). What it DOES check: whether the calibrated engine's
performance is roughly consistent across the two halves of the window, or
concentrated in one regime -- a strategy that only "works" in one specific
6-12 month stretch is a red flag regardless of calibration cleanliness.

Run once locally: python tools/split_sample_check.py
"""
from __future__ import annotations

import csv
from pathlib import Path

from hermes_trading.ict.backtest import compute_metrics, run_backtest_single_asset
from hermes_trading.ict.util import Candle
from tools.run_ict_backtest import CALIBRATED_PARAMS

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
                timestamp=int(float(row["timestamp"])), open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]), volume=float(row["volume"]),
            ))
    return candles


def fmt_metrics(trades, equity0, label):
    m = compute_metrics(trades, equity0, equity0 + sum(t.pnl_usd for t in trades))
    if m["trades"] == 0:
        return f"    {label}: 0 trades"
    return (f"    {label}: trades={m['trades']:>3} win_rate={m['win_rate']:.0%} "
            f"expectancy_R={m['expectancy_r']:+.2f} PF={m['profit_factor']:.2f} "
            f"max_dd={m['max_drawdown_pct']:.0%} PnL=${m['total_pnl_usd']:+.2f}")


def main() -> None:
    print("=== Split-sample stability check (calibrated params, first year vs second year) ===\n")
    for symbol in ASSETS:
        candles = load_csv(symbol)
        t0 = candles[0].timestamp
        t_last = candles[-1].timestamp
        midpoint = t0 + (t_last - t0) // 2

        result = run_backtest_single_asset(candles, symbol, EQUITY0, **CALIBRATED_PARAMS)
        year1 = [t for t in result.trades if t.entry_timestamp < midpoint]
        year2 = [t for t in result.trades if t.entry_timestamp >= midpoint]

        print(f"=== {symbol} ===")
        print(fmt_metrics(result.trades, EQUITY0, "full window "))
        print(fmt_metrics(year1, EQUITY0, "first half  "))
        print(fmt_metrics(year2, EQUITY0, "second half "))
        print()


if __name__ == "__main__":
    main()
