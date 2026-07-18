"""
tools/run_ict_backtest.py -- run the Phase 2 backtest engine against the
CSVs fetched by fetch_ict_backtest_data.py and print metrics per asset.

Run once locally: python tools/run_ict_backtest.py [--calibrated]
  (no flag)     -- spec's own locked S:3/S:9 defaults, unmodified
  --calibrated  -- session 18's loosened parameter set (see CALIBRATED_PARAMS
                    docstring below), tuned toward >=2 trades/month/asset
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

# Calibration pass, session 18 (2026-07-18): spec's own untuned defaults produced
# 0 trades over 2yr/4 assets (see memory.md). Loosened parameters below, in the
# order they were found to matter (funnel-traced against real BTC/ETH/SOL/TAO
# history), to hit the ~2 trades/month/asset floor Linh asked for:
#   - kill_zones: removed entirely -- crypto trades 24/7 and this alone cut ~80%
#     of otherwise-valid setups (spec S:8 explicitly flags session windows as
#     "per-asset overrideable").
#   - swing_n_weekly 3->2: spec's compute_bias (S:4) has NO branch that permits
#     Weekly=RANGE, so a strict weekly trend read is a hard requirement; with
#     swing_n_weekly=3 the weekly trend read RANGE 60-74% of the time (a slow,
#     coarse signal on a choppy weekly timeframe), which alone accounted for
#     most of the missing trade volume.
#   - disp_atr_mult 1.5->0.75: the single biggest remaining lever. Only ~12% of
#     real MSS events had a displacement candle at the spec default, so most
#     MSS events left no valid FVG/OB behind at all (entry_zone was the
#     dominant Stage-1 gate failure). NOTE: at 0.75x this is HALF the spec
#     default -- a real quality tradeoff, not just noise reduction; a 0.75x-ATR
#     candle is a fairly ordinary move, not a decisive one.
#   - min_rr 2.0->0.8: after fixing the above, RR became the dominant gate.
#     0.8 is well below spec's own floor (2.0, "prefer >=3.0") -- the biggest
#     single quality concession in this set; a sub-1.0 RR needs a very high
#     win rate just to break even.
#   - min_target_atr_mult 2.0->1.5, b_threshold 11->9, max_bars_after_mss
#     10->20, state_ttl_bars 20->40: secondary loosening, smaller individual
#     impact but each removed a few more borderline rejections.
CALIBRATED_PARAMS = dict(
    kill_zones=((0, 24),),
    swing_n_weekly=2,
    disp_atr_mult=0.75,
    min_rr=0.8,
    min_target_atr_mult=1.5,
    b_threshold=9,
    max_bars_after_mss=20,
    state_ttl_bars=40,
)


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
    calibrated = "--calibrated" in sys.argv
    params = CALIBRATED_PARAMS if calibrated else {}
    print(f"=== mode: {'CALIBRATED (loosened)' if calibrated else 'SPEC DEFAULTS (locked)'} ===")
    if calibrated:
        print(f"    params: {params}")

    totals = {"trades": 0, "wins": 0, "pnl": 0.0}
    for symbol in ASSETS:
        path = DATA_DIR / f"{symbol.replace('/', '_')}.csv"
        if not path.exists():
            print(f"{symbol}: no data file, skipping", file=sys.stderr)
            continue
        candles = load_csv(symbol)
        span_days = (candles[-1].timestamp - candles[0].timestamp) / 86_400_000
        print(f"\n=== {symbol} ({len(candles)} 15m candles, {span_days:.0f} days) ===")

        result = run_backtest_single_asset(candles, symbol, EQUITY0, **params)
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
            totals["trades"] += metrics["trades"]
            totals["wins"] += sum(1 for t in result.trades if t.pnl_usd > 0)
            totals["pnl"] += metrics["total_pnl_usd"]
        else:
            print("  (no trades)")

    print(f"\n=== TOTAL across {len(ASSETS)} assets ===")
    print(f"  trades: {totals['trades']}  wins: {totals['wins']}  "
          f"win rate: {(totals['wins'] / totals['trades']) if totals['trades'] else 0:.1%}  "
          f"total PnL: ${totals['pnl']:.2f}")


if __name__ == "__main__":
    main()
