"""Tests for hermes_trading.ict.liquidity -- spec S:3.3, S:3.4, S:3.5."""
from __future__ import annotations

from hermes_trading.ict.liquidity import (
    detect_sweep,
    equal_highs,
    equal_lows,
    liquidity_pools,
    prior_period_high_low,
    sr_zones,
)
from hermes_trading.ict.structure import find_swings
from hermes_trading.ict.types import (
    Direction,
    LiquidityKind,
    LiquidityPool,
    LiquiditySource,
    ZoneKind,
)
from tests.ict.helpers import make_candles


def _resistance_fixture(near_gap: float, far_gap: float = 20.0) -> list:
    """Three swing highs (n=1): two clustered `near_gap` apart, one `far_gap` away. No swing lows."""
    a, b = 110.0, 110.0 + near_gap
    c = b + far_gap
    return make_candles([
        (100, 101, 99, 100),  # 0 filler
        (100, 101, 99, 100),  # 1 filler
        (100, a, 99, 105),    # 2 swing high a
        (100, 101, 99, 100),  # 3 filler
        (100, 101, 99, 100),  # 4 filler
        (100, b, 99, 105),    # 5 swing high b (near a)
        (100, 101, 99, 100),  # 6 filler
        (100, 101, 99, 100),  # 7 filler
        (100, c, 99, 105),    # 8 swing high c (far from a/b)
        (100, 101, 99, 100),  # 9 filler
        (100, 101, 99, 100),  # 10 filler
    ])


# ── sr_zones ─────────────────────────────────────────────────────────────────


def test_sr_zones_clusters_close_pivots_and_excludes_lone_ones():
    candles = _resistance_fixture(near_gap=0.05)
    swings = find_swings(candles, n=1)
    zones = sr_zones(candles, swings, atr_period=2)

    resistances = [z for z in zones if z.kind == ZoneKind.RESISTANCE]
    assert len(resistances) == 1
    z = resistances[0]
    assert z.touches == 2
    assert z.member_indices == (2, 5)
    assert z.price_low == 110.0
    assert 110.04 < z.price_high < 110.06

    assert [z for z in zones if z.kind == ZoneKind.SUPPORT] == []


def test_sr_zones_respects_min_touches():
    candles = _resistance_fixture(near_gap=0.05)
    swings = find_swings(candles, n=1)
    zones = sr_zones(candles, swings, atr_period=2, min_touches=3)
    assert zones == []  # the 2-touch cluster no longer qualifies


# ── equal_highs / equal_lows ─────────────────────────────────────────────────


def test_equal_highs_flags_close_pair_as_buyside_liquidity():
    candles = _resistance_fixture(near_gap=0.05)
    swings = find_swings(candles, n=1)
    pools = equal_highs(swings, candles, atr_period=2)
    assert len(pools) == 1
    p = pools[0]
    assert p.kind == LiquidityKind.BUYSIDE
    assert p.source == LiquiditySource.EQUAL_HIGHS
    assert p.member_indices == (2, 5)


def test_equal_lows_flags_close_pair_as_sellside_liquidity():
    candles = make_candles([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 90.0, 95),     # swing low a
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 90.05, 95),    # swing low b (near a)
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 60.0, 95),     # swing low c (far away)
        (100, 101, 99, 100),
        (100, 101, 99, 100),
    ])
    swings = find_swings(candles, n=1)
    pools = equal_lows(swings, candles, atr_period=2)
    assert len(pools) == 1
    p = pools[0]
    assert p.kind == LiquidityKind.SELLSIDE
    assert p.source == LiquiditySource.EQUAL_LOWS
    assert p.member_indices == (2, 5)


# ── prior_period_high_low ─────────────────────────────────────────────────────


def test_prior_period_high_low_uses_last_closed_bar():
    daily = make_candles([(100, 110, 90, 105), (105, 108, 95, 100)])
    high, low = prior_period_high_low(daily)
    assert (high, low) == (108, 95)


def test_prior_period_high_low_raises_on_empty_series():
    import pytest

    with pytest.raises(ValueError):
        prior_period_high_low([])


# ── liquidity_pools integration ───────────────────────────────────────────────


def test_liquidity_pools_includes_pdh_pdl_when_daily_series_given():
    candles = make_candles([(100, 101, 99, 100)] * 5)
    swings: list = []
    daily = make_candles([(100, 200, 50, 150)])
    pools = liquidity_pools(candles, swings, daily_candles=daily)
    sources = {p.source for p in pools}
    assert LiquiditySource.PDH in sources and LiquiditySource.PDL in sources
    pdh = next(p for p in pools if p.source == LiquiditySource.PDH)
    pdl = next(p for p in pools if p.source == LiquiditySource.PDL)
    assert pdh.price == 200 and pdl.price == 50


# ── detect_sweep ───────────────────────────────────────────────────────────────


def _pool(price: float, index: int, kind=LiquidityKind.SELLSIDE) -> LiquidityPool:
    return LiquidityPool(price=price, kind=kind, source=LiquiditySource.SWING, index=index)


def test_detect_sweep_same_candle_close_back():
    candles = make_candles([
        (100, 101, 99, 100),   # 0
        (100, 101, 99, 100),   # 1
        (100, 101, 95, 96),    # 2 pool established here (low=95)
        (100, 101, 99, 100),   # 3
        (100, 101, 99, 100),   # 4
        (100, 101, 90, 100),   # 5 wick to 90 (below 95), closes back at 100 (above 95)
    ])
    pool = _pool(95.0, index=2)
    sweeps = detect_sweep(candles, [pool], atr_period=2)
    assert len(sweeps) == 1
    sw = sweeps[0]
    assert sw.index == 5
    assert sw.direction == Direction.BULLISH
    assert sw.penetration == 5.0


def test_detect_sweep_no_close_back_within_max_bars_is_not_a_sweep():
    candles = make_candles([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 95, 96),   # pool
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 90, 92),   # wick below, closes at 92 -- still below 95, no reversal
    ])
    pool = _pool(95.0, index=2)
    assert detect_sweep(candles, [pool], atr_period=2, max_bars=1) == []


def test_detect_sweep_close_back_on_a_later_bar_needs_max_bars_2():
    candles = make_candles([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 95, 96),   # pool
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 90, 92),   # wick below, closes below (bar 5)
        (92, 98, 96, 97),     # stays above 95 (no fresh wick), closes back above at 97 (bar 6)
    ])
    pool = _pool(95.0, index=2)
    assert detect_sweep(candles, [pool], atr_period=2, max_bars=1) == []
    sweeps = detect_sweep(candles, [pool], atr_period=2, max_bars=2)
    assert len(sweeps) == 1
    assert sweeps[0].index == 6


def test_detect_sweep_bearish_buyside():
    candles = make_candles([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 105, 99, 100),   # pool at 105 (high)
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 110, 99, 100),   # wick above 105, closes back below at 100
    ])
    pool = _pool(105.0, index=2, kind=LiquidityKind.BUYSIDE)
    sweeps = detect_sweep(candles, [pool], atr_period=2)
    assert len(sweeps) == 1
    assert sweeps[0].direction == Direction.BEARISH
    assert sweeps[0].index == 5
