"""Tests for hermes_trading.ict.backtest -- spec S:5, S:11."""
from __future__ import annotations

import pytest

from hermes_trading.ict.backtest import (
    ClosedTrade,
    _bucket_start,
    _invalidated,
    _manage_position,
    _search_fill,
    compute_metrics,
    resample,
    run_backtest_single_asset,
)
from hermes_trading.ict.types import (
    Direction,
    Grade,
    LiquidityKind,
    LiquidityPool,
    LiquiditySource,
    StructureBreak,
    BreakKind,
    Sweep,
    Swing,
    SwingKind,
)
from hermes_trading.ict.util import Candle
from tests.ict.helpers import make_candles

MIN_MS = 60_000
HOUR_MS = 60 * MIN_MS
DAY_MS = 24 * HOUR_MS


def _c15(ts, o, h, l, c, v=10.0) -> Candle:
    return Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=v)


# ── resample ─────────────────────────────────────────────────────────────────


def test_resample_aggregates_a_closed_hour_bucket():
    candles = [
        _c15(0, 100, 102, 99, 101, 10),
        _c15(15 * MIN_MS, 101, 103, 100, 102, 20),
        _c15(30 * MIN_MS, 102, 104, 101, 103, 30),
        _c15(45 * MIN_MS, 103, 105, 100, 104, 40),
        _c15(60 * MIN_MS, 104, 106, 103, 105, 50),  # proves bucket 1 closed; itself not closed
    ]
    result = resample(candles, "1h")
    assert len(result) == 1
    bar = result[0]
    assert bar.timestamp == 0
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (100, 105, 99, 104, 100)


def test_resample_no_bars_when_none_fully_closed():
    candles = [_c15(0, 100, 101, 99, 100), _c15(15 * MIN_MS, 100, 101, 99, 100)]
    assert resample(candles, "1h") == []


def test_resample_15m_passthrough():
    candles = [_c15(0, 100, 101, 99, 100)]
    assert resample(candles, "15m") == candles


def test_bucket_start_weekly_aligns_to_monday():
    # 1970-01-01 (epoch 0) was a Thursday; the Monday of that week is 1969-12-29.
    assert _bucket_start(0, "1w") == -3 * DAY_MS
    # 1970-01-05 was a Monday; any timestamp within that week should map to its own start.
    monday_ts = 4 * DAY_MS
    assert _bucket_start(monday_ts, "1w") == monday_ts
    assert _bucket_start(monday_ts + 12 * HOUR_MS, "1w") == monday_ts
    assert _bucket_start(monday_ts + 6 * DAY_MS + 23 * HOUR_MS, "1w") == monday_ts


def test_resample_no_lookahead():
    """resample(prefix) always equals the full result filtered to buckets closed within that prefix."""
    candles = []
    ts = 0
    price = 100.0
    for i in range(120):  # ~30 hours of 15m bars
        price += (i % 5) - 2
        candles.append(_c15(ts, price, price + 2, price - 2, price + 0.5, 10 + i))
        ts += 15 * MIN_MS

    full = resample(candles, "1h")
    for k in range(4, len(candles) + 1, 3):
        prefix_result = resample(candles[:k], "1h")
        last_ts = candles[k - 1].timestamp
        expected = [b for b in full if b.timestamp + HOUR_MS <= last_ts]
        assert prefix_result == expected, f"diverged at prefix length {k}"


# ── _invalidated ────────────────────────────────────────────────────────────


def _bullish_sweep_and_mss():
    pool = LiquidityPool(price=100.0, kind=LiquidityKind.SELLSIDE, source=LiquiditySource.SWING, index=1)
    sweep = Sweep(index=2, pool=pool, penetration=3.0, direction=Direction.BULLISH)  # wick extreme = 97
    mss = StructureBreak(index=5, kind=BreakKind.MSS, direction=Direction.BULLISH,
                          broken_swing=Swing(0, 106.0, SwingKind.HIGH, 1), close=108)
    return sweep, mss


def test_invalidated_when_close_beyond_sweep_extreme():
    sweep, mss = _bullish_sweep_and_mss()
    candle = Candle(timestamp=0, open=98, high=99, low=95, close=96, volume=1)  # closes below 97
    assert _invalidated(Direction.BULLISH, candle, sweep, mss, atr_value=8.0) is True


def test_invalidated_when_mss_fully_retraced_by_a_meaningful_margin():
    sweep, mss = _bullish_sweep_and_mss()
    # broken_swing.price=106, buffer=0.25*8=2 -> invalidation threshold is 104. Closing at 103 clears it decisively.
    candle = Candle(timestamp=0, open=107, high=108, low=102, close=103, volume=1)
    assert _invalidated(Direction.BULLISH, candle, sweep, mss, atr_value=8.0) is True


def test_not_invalidated_by_a_graze_just_below_broken_swing_price():
    """
    Regression: a first-attempt retracement bar that dips into the entry
    zone and closes just below (not decisively through) the broken swing
    level is normal retracement-in-progress, not a failed reversal --
    requires clearing the level by mss_retrace_buffer_mult x ATR, not
    merely closing under the exact line.
    """
    sweep, mss = _bullish_sweep_and_mss()
    # close=105 is below 106 but within the 2.0-point buffer (threshold 104) -- a graze, not a real retrace.
    candle = Candle(timestamp=0, open=107, high=108, low=104, close=105, volume=1)
    assert _invalidated(Direction.BULLISH, candle, sweep, mss, atr_value=8.0) is False


def test_not_invalidated_by_a_wick_that_closes_back_above_the_level():
    sweep, mss = _bullish_sweep_and_mss()
    # Wicks down to grab a zone at 105 but closes back at 106.2 -- a normal retracement-and-hold, not invalidation.
    candle = Candle(timestamp=0, open=106.5, high=106.8, low=104.5, close=106.2, volume=1)
    assert _invalidated(Direction.BULLISH, candle, sweep, mss, atr_value=8.0) is False


def test_broken_swing_check_disabled_without_atr():
    """atr_value=None (insufficient history) disables the retrace-margin check entirely."""
    sweep, mss = _bullish_sweep_and_mss()
    candle = Candle(timestamp=0, open=107, high=108, low=99, close=100, volume=1)  # well below 106, no ATR available
    assert _invalidated(Direction.BULLISH, candle, sweep, mss, atr_value=None) is False


# ── _search_fill ──────────────────────────────────────────────────────────────


def test_search_fill_finds_touch_before_ttl():
    sweep, mss = _bullish_sweep_and_mss()
    exec_full = make_candles([(100, 101, 99, 100)] * 6 + [  # indices 0-5
        (106, 107, 106, 106.5),   # 6: no touch
        (106, 106.8, 104.5, 106.2),  # 7: low=104.5 touches entry=105, close stays above 106 -- fills, not invalidated
    ])
    fill_index = _search_fill(exec_full, 6, entry_price=105.0, direction=Direction.BULLISH, sweep=sweep, mss=mss, ttl_bars=20)
    assert fill_index == 7


def test_search_fill_none_when_invalidated_first():
    sweep, mss = _bullish_sweep_and_mss()
    exec_full = make_candles([(100, 101, 99, 100)] * 6 + [
        (106, 107, 95, 96),        # 6: closes below sweep extreme (97) -- invalidated before ever touching 105
        (95, 106, 94, 105.5),      # 7: would have touched 105, too late
    ])
    assert _search_fill(exec_full, 6, entry_price=105.0, direction=Direction.BULLISH, sweep=sweep, mss=mss, ttl_bars=20) is None


def test_search_fill_none_on_ttl_timeout():
    sweep, mss = _bullish_sweep_and_mss()
    exec_full = make_candles([(100, 101, 99, 100)] * 6 + [(106, 107, 106, 106.5)] * 5)  # never touches 105
    assert _search_fill(exec_full, 6, entry_price=105.0, direction=Direction.BULLISH, sweep=sweep, mss=mss, ttl_bars=3) is None


# ── _manage_position ────────────────────────────────────────────────────────────


def test_manage_position_stop_hit_before_partial():
    exec_full = make_candles([
        (105, 106, 104, 105),  # 0: fill bar
        (105, 105.5, 99, 100),  # 1: drops straight to stop (96) -- low=99 doesn't hit 96 yet
        (100, 100.5, 95, 96),   # 2: low=95 <= stop(96) -- stopped out
    ])
    trade = _manage_position(exec_full, [], fill_index=0, asset="TEST", direction=Direction.BULLISH,
                              grade=Grade.A_PLUS, entry=105.0, initial_stop=96.0, target=130.0, qty=10.0, fee_pct=0.0)
    assert trade.close_reason == "stop"
    assert trade.exit_index == 2
    assert trade.exit_price == 96.0
    assert trade.pnl_usd == pytest.approx(10.0 * (96.0 - 105.0))


def test_manage_position_partial_then_target():
    entry, stop = 100.0, 96.0  # risk_per_unit = 4, partial level (2R) = 108
    exec_full = make_candles([
        (100, 101, 99, 100),   # 0: fill bar
        (101, 109, 100, 108),  # 1: hits partial level 108 -- 50% off, stop -> breakeven (100)
        (108, 121, 107, 120),  # 2: hits target (120)
    ])
    trade = _manage_position(exec_full, [], fill_index=0, asset="TEST", direction=Direction.BULLISH,
                              grade=Grade.A_PLUS, entry=entry, initial_stop=stop, target=120.0, qty=10.0, fee_pct=0.0)
    assert trade.close_reason == "target"
    assert trade.exit_index == 2
    # 5 units @ +8 (partial) + 5 units @ +20 (target) = 40 + 100 = 140
    assert trade.pnl_usd == pytest.approx(140.0)
    assert trade.r_multiple == pytest.approx(140.0 / (10.0 * 4.0))


def test_manage_position_trails_stop_after_partial():
    entry, stop = 100.0, 96.0
    higher_low = Swing(index=2, price=103.0, kind=SwingKind.LOW, confirmed_index=2)
    exec_full = make_candles([
        (100, 101, 99, 100),    # 0: fill
        (101, 109, 100, 108),   # 1: partial at 108, stop -> BE (100)
        (108, 110, 103, 109),   # 2: a higher low forms here (103), confirmed same bar for this test
        (109, 110, 101, 102),   # 3: drops to 101 -- ABOVE the trailed stop of 103? no: 101 < 103 -> trail-stopped at 103
    ])
    trade = _manage_position(exec_full, [higher_low], fill_index=0, asset="TEST", direction=Direction.BULLISH,
                              grade=Grade.A_PLUS, entry=entry, initial_stop=stop, target=200.0, qty=10.0, fee_pct=0.0)
    assert trade.close_reason == "trail_stop"
    assert trade.exit_price == 103.0
    assert trade.exit_index == 3


# ── _manage_position (bearish mirror) ────────────────────────────────────────────


def test_manage_position_bearish_stop_hit_before_partial():
    exec_full = make_candles([
        (105, 106, 104, 105),   # 0: fill bar (short)
        (105, 109, 104.5, 108),  # 1: rises toward stop (114) but doesn't reach it -- high=109
        (108, 115, 107, 114),   # 2: high=115 >= stop(114) -- stopped out
    ])
    trade = _manage_position(exec_full, [], fill_index=0, asset="TEST", direction=Direction.BEARISH,
                              grade=Grade.A_PLUS, entry=105.0, initial_stop=114.0, target=80.0, qty=10.0, fee_pct=0.0)
    assert trade.close_reason == "stop"
    assert trade.exit_index == 2
    assert trade.exit_price == 114.0
    assert trade.pnl_usd == pytest.approx(10.0 * (105.0 - 114.0))


def test_manage_position_bearish_partial_then_target():
    entry, stop = 100.0, 104.0  # risk_per_unit = 4, partial level (2R) = 92
    exec_full = make_candles([
        (100, 101, 99, 100),   # 0: fill bar (short)
        (99, 100, 91, 92),     # 1: hits partial level 92 -- 50% off, stop -> breakeven (100)
        (92, 93, 79, 80),      # 2: hits target (80)
    ])
    trade = _manage_position(exec_full, [], fill_index=0, asset="TEST", direction=Direction.BEARISH,
                              grade=Grade.A_PLUS, entry=entry, initial_stop=stop, target=80.0, qty=10.0, fee_pct=0.0)
    assert trade.close_reason == "target"
    assert trade.exit_index == 2
    # 5 units @ +8 (partial: 100->92) + 5 units @ +20 (target: 100->80) = 40 + 100 = 140
    assert trade.pnl_usd == pytest.approx(140.0)
    assert trade.r_multiple == pytest.approx(140.0 / (10.0 * 4.0))


def test_manage_position_bearish_trails_stop_after_partial():
    entry, stop = 100.0, 104.0
    lower_high = Swing(index=2, price=97.0, kind=SwingKind.HIGH, confirmed_index=2)
    exec_full = make_candles([
        (100, 101, 99, 100),   # 0: fill (short)
        (99, 100, 91, 92),     # 1: partial at 92, stop -> BE (100)
        (92, 97, 90, 91),      # 2: a lower high forms here (97)
        (91, 98, 90, 96),      # 3: rises to 98 -- ABOVE the trailed stop of 97 -> trail-stopped
    ])
    trade = _manage_position(exec_full, [lower_high], fill_index=0, asset="TEST", direction=Direction.BEARISH,
                              grade=Grade.A_PLUS, entry=entry, initial_stop=stop, target=20.0, qty=10.0, fee_pct=0.0)
    assert trade.close_reason == "trail_stop"
    assert trade.exit_price == 97.0
    assert trade.exit_index == 3


# ── compute_metrics ────────────────────────────────────────────────────────────


def _trade(pnl, r, hold, reason="target"):
    return ClosedTrade("BTC/USDT", Direction.BULLISH, Grade.A_PLUS, 0, 100, 1, 110, 96, 120, pnl, r, reason, hold)


def test_compute_metrics_hand_computed():
    trades = [_trade(200, 2.0, 10), _trade(-100, -1.0, 5), _trade(300, 3.0, 20)]
    m = compute_metrics(trades, equity0=1000.0, final_equity=1400.0)
    assert m["trades"] == 3
    assert m["win_rate"] == pytest.approx(2 / 3)
    assert m["expectancy_r"] == pytest.approx((2.0 - 1.0 + 3.0) / 3)
    assert m["profit_factor"] == pytest.approx(500.0 / 100.0)
    assert m["avg_hold_bars"] == pytest.approx((10 + 5 + 20) / 3)
    assert m["total_pnl_usd"] == pytest.approx(400.0)


def test_compute_metrics_empty():
    m = compute_metrics([], equity0=1000.0, final_equity=1000.0)
    assert m["trades"] == 0
    assert m["win_rate"] is None


def test_compute_metrics_drawdown():
    # equity: 1000 -> 1200 (peak) -> 1000 (dd 200/1200) -> 1100
    trades = [_trade(200, 2.0, 5), _trade(-200, -2.0, 5), _trade(100, 1.0, 5)]
    m = compute_metrics(trades, equity0=1000.0, final_equity=1100.0)
    assert m["max_drawdown_pct"] == pytest.approx(200 / 1200)


# ── run_backtest_single_asset smoke test ─────────────────────────────────────


def test_run_backtest_single_asset_smoke():
    """Runs a multi-week synthetic 15m series without error; result is internally consistent."""
    import random

    rng = random.Random(11)
    candles = []
    ts = 0
    price = 100.0
    for i in range(96 * 90):  # 90 days of 15m bars
        drift = rng.uniform(-0.3, 0.5)  # slight uptrend bias
        close = price + drift
        high = max(price, close) + abs(rng.uniform(0, 1.0))
        low = min(price, close) - abs(rng.uniform(0, 1.0))
        candles.append(Candle(timestamp=ts, open=price, high=high, low=low, close=close, volume=rng.uniform(50, 200)))
        price = close
        ts += 15 * MIN_MS

    result = run_backtest_single_asset(candles, "TEST/USDT", equity0=1000.0)
    assert result.asset == "TEST/USDT"
    assert result.qualified_setups <= result.considered_setups
    for t in result.trades:
        assert t.exit_index > t.entry_index
        assert t.hold_bars >= 0

    metrics = compute_metrics(result.trades, 1000.0, result.final_equity)
    assert metrics["trades"] == len(result.trades)
