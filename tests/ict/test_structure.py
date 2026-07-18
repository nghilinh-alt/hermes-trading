"""Tests for hermes_trading.ict.structure -- spec S:3.1, S:3.2."""
from __future__ import annotations

from hermes_trading.ict.structure import (
    alternate_swings,
    detect_bos,
    detect_mss,
    find_swings,
    market_structure,
)
from hermes_trading.ict.types import (
    BreakKind,
    Direction,
    LiquidityKind,
    LiquidityPool,
    LiquiditySource,
    Swing,
    SwingKind,
    Sweep,
    TrendState,
)
from tests.ict.helpers import make_candles


# ── find_swings ──────────────────────────────────────────────────────────────


def test_swing_low_n1():
    candles = make_candles([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 95, 96),   # dip
        (100, 101, 99, 100),
        (100, 101, 99, 100),
    ])
    swings = find_swings(candles, n=1)
    assert swings == [Swing(index=2, price=95, kind=SwingKind.LOW, confirmed_index=3)]


def test_swing_high_n1():
    candles = make_candles([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 105, 99, 104),  # spike
        (100, 101, 99, 100),
        (100, 101, 99, 100),
    ])
    swings = find_swings(candles, n=1)
    assert swings == [Swing(index=2, price=105, kind=SwingKind.HIGH, confirmed_index=3)]


def test_no_swings_in_monotonic_trend():
    """No-signal edge case: strictly increasing highs/lows never produce a local pivot."""
    candles = make_candles([(100 + i, 101 + i, 99 + i, 100 + i) for i in range(8)])
    assert find_swings(candles, n=1) == []


def test_equal_highs_are_not_a_swing():
    """Strict inequality: a flat top does not count as a swing high (tie != pivot)."""
    candles = make_candles([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
    ])
    swings = find_swings(candles, n=1)
    assert [s for s in swings if s.kind == SwingKind.HIGH] == []


def test_swing_low_n2_requires_wider_window():
    candles = make_candles([
        (100, 101, 99, 100),   # 0
        (100, 101, 98, 100),   # 1 shallow dip, should NOT qualify at n=2
        (100, 101, 99, 100),   # 2
        (100, 101, 90, 100),   # 3 deep dip -- the real swing
        (100, 101, 99, 100),   # 4
        (100, 101, 98, 100),   # 5
        (100, 101, 99, 100),   # 6
    ])
    swings = find_swings(candles, n=2)
    assert swings == [Swing(index=3, price=90, kind=SwingKind.LOW, confirmed_index=5)]


def test_find_swings_rejects_n_below_1():
    import pytest

    with pytest.raises(ValueError):
        find_swings(make_candles([(1, 2, 0, 1)] * 3), n=0)


# ── alternate_swings ─────────────────────────────────────────────────────────


def test_alternate_swings_collapses_consecutive_same_kind_to_most_extreme():
    swings = [
        Swing(0, 100, SwingKind.LOW, 1),
        Swing(2, 90, SwingKind.LOW, 3),   # deeper low, same run -- should replace index 0
        Swing(4, 110, SwingKind.HIGH, 5),
        Swing(6, 120, SwingKind.HIGH, 7),  # higher high, same run -- should replace index 4
        Swing(8, 95, SwingKind.LOW, 9),
    ]
    alt = alternate_swings(swings)
    assert [(s.index, s.kind) for s in alt] == [(2, SwingKind.LOW), (6, SwingKind.HIGH), (8, SwingKind.LOW)]
    assert alt[0].price == 90
    assert alt[1].price == 120


# ── market_structure ─────────────────────────────────────────────────────────


def test_market_structure_uptrend():
    swings = [
        Swing(0, 100, SwingKind.LOW, 1),
        Swing(2, 110, SwingKind.HIGH, 3),
        Swing(4, 105, SwingKind.LOW, 5),
        Swing(6, 120, SwingKind.HIGH, 7),
    ]
    assert market_structure(swings) == TrendState.UPTREND


def test_market_structure_downtrend():
    swings = [
        Swing(0, 120, SwingKind.HIGH, 1),
        Swing(2, 100, SwingKind.LOW, 3),
        Swing(4, 110, SwingKind.HIGH, 5),
        Swing(6, 90, SwingKind.LOW, 7),
    ]
    assert market_structure(swings) == TrendState.DOWNTREND


def test_market_structure_range_on_conflicting_hh_hl():
    """Higher high but lower low (or vice versa) is neither trend -- range."""
    swings = [
        Swing(0, 100, SwingKind.LOW, 1),
        Swing(2, 110, SwingKind.HIGH, 3),
        Swing(4, 95, SwingKind.LOW, 5),   # lower low
        Swing(6, 120, SwingKind.HIGH, 7),  # higher high
    ]
    assert market_structure(swings) == TrendState.RANGE


def test_market_structure_range_with_insufficient_swings():
    assert market_structure([Swing(0, 100, SwingKind.LOW, 1)]) == TrendState.RANGE
    assert market_structure([]) == TrendState.RANGE


# ── detect_bos ────────────────────────────────────────────────────────────────


def test_detect_bos_bullish_continuation():
    # Uptrend established (SL 100->105, SH 110->115), then a close above 115 = BOS.
    swings = [
        Swing(0, 100, SwingKind.LOW, 1),
        Swing(2, 110, SwingKind.HIGH, 3),
        Swing(4, 105, SwingKind.LOW, 5),
        Swing(6, 115, SwingKind.HIGH, 7),
    ]
    candles = make_candles([(100, 101, 99, 100)] * 8 + [(115, 118, 114, 117)])  # bar 8 closes above 115
    breaks = detect_bos(candles, swings)
    assert len(breaks) == 1
    b = breaks[0]
    assert b.index == 8
    assert b.kind == BreakKind.BOS
    assert b.direction == Direction.BULLISH
    assert b.broken_swing.price == 115


def test_detect_bos_none_in_range_market():
    swings = [
        Swing(0, 100, SwingKind.LOW, 1),
        Swing(2, 110, SwingKind.HIGH, 3),
        Swing(4, 95, SwingKind.LOW, 5),
        Swing(6, 120, SwingKind.HIGH, 7),
    ]  # conflicting HH/LL -> range, no trend to continue
    candles = make_candles([(100, 130, 90, 100)] * 9)
    assert detect_bos(candles, swings) == []


# ── detect_mss ────────────────────────────────────────────────────────────────


def _ssl_pool(price: float, index: int) -> LiquidityPool:
    return LiquidityPool(price=price, kind=LiquidityKind.SELLSIDE, source=LiquiditySource.SWING, index=index)


def test_detect_mss_requires_prior_sweep():
    # Downtrend (SH 120->110, SL 100->90), then close above the last SH (110) at bar 8.
    swings = [
        Swing(0, 120, SwingKind.HIGH, 1),
        Swing(2, 100, SwingKind.LOW, 3),
        Swing(4, 110, SwingKind.HIGH, 5),
        Swing(6, 90, SwingKind.LOW, 7),
    ]
    candles = make_candles([(100, 101, 99, 100)] * 8 + [(110, 113, 109, 112)])  # bar 8 closes above 110

    # No sweep -> not classified as MSS.
    assert detect_mss(candles, swings, sweeps=[]) == []

    # With a bullish sweep of sell-side liquidity beforehand -> MSS.
    sweep = Sweep(index=7, pool=_ssl_pool(90, 6), penetration=1.0, direction=Direction.BULLISH)
    breaks = detect_mss(candles, swings, sweeps=[sweep])
    assert len(breaks) == 1
    b = breaks[0]
    assert b.index == 8
    assert b.kind == BreakKind.MSS
    assert b.direction == Direction.BULLISH
    assert b.broken_swing.price == 110


def test_detect_mss_wrong_direction_sweep_does_not_qualify():
    swings = [
        Swing(0, 120, SwingKind.HIGH, 1),
        Swing(2, 100, SwingKind.LOW, 3),
        Swing(4, 110, SwingKind.HIGH, 5),
        Swing(6, 90, SwingKind.LOW, 7),
    ]
    candles = make_candles([(100, 101, 99, 100)] * 8 + [(110, 113, 109, 112)])
    # A bearish sweep doesn't set up a bullish MSS.
    sweep = Sweep(index=7, pool=_ssl_pool(120, 0), penetration=1.0, direction=Direction.BEARISH)
    assert detect_mss(candles, swings, sweeps=[sweep]) == []
