"""Tests for hermes_trading.ict.imbalance -- spec S:3.6, S:3.7, S:3.8, S:3.8b."""
from __future__ import annotations

from hermes_trading.ict.imbalance import (
    find_breakers,
    find_fvg,
    find_order_blocks,
    is_displacement,
)
from hermes_trading.ict.types import BreakKind, Direction, StructureBreak, Swing, SwingKind
from tests.ict.helpers import make_candles


# ── is_displacement ──────────────────────────────────────────────────────────


def test_is_displacement_true_for_large_body():
    # filler TR ~2, so ATR small; a body of 20 is way over 1.5xATR.
    candles = make_candles([(100, 101, 99, 100)] * 3 + [(100, 121, 99, 120)])
    assert is_displacement(candles, 3, atr_period=2) is True


def test_is_displacement_false_for_small_body():
    candles = make_candles([(100, 101, 99, 100)] * 4)
    assert is_displacement(candles, 3, atr_period=2) is False


# ── find_fvg ─────────────────────────────────────────────────────────────────


def test_find_fvg_bullish_gap_with_displacement():
    # c0 high=101; c1 is the displacement candle (huge body, low>c0.high and high<c2.low
    # isn't required of c1 itself -- only c0/c2 form the gap); c2 low=115 > c0.high=101.
    candles = make_candles([
        (100, 101, 99, 100),   # 0
        (100, 101, 99, 100),   # 1 filler (ATR warm-up)
        (100, 101, 99, 100),   # 2 = c0 for the gap (index i-2 where i=4)
        (100, 122, 99, 120),   # 3 = displacement candle (middle, i-1)
        (120, 125, 115, 124),  # 4 = c2, low=115 > c0.high=101 -> bullish gap [101, 115]
    ])
    fvgs = find_fvg(candles, atr_period=2, min_fvg_mult=0.25)
    assert len(fvgs) == 1
    g = fvgs[0]
    assert g.index == 4
    assert (g.low, g.high) == (101, 115)
    assert g.kind == Direction.BULLISH
    assert g.displacement is True
    assert g.mitigated_index is None


def test_find_fvg_none_without_displacement_middle_candle():
    # Same gap geometry, but middle candle body is tiny -> not a displacement candle.
    candles = make_candles([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),   # c0, high=101
        (100, 101, 99, 100),   # tiny-body middle candle
        (100, 105, 103, 104),  # c2, low=103 > 101 -- geometrically a gap, but no displacement
    ])
    assert find_fvg(candles, atr_period=2, min_fvg_mult=0.25) == []


def test_find_fvg_bearish_gap_and_mitigation():
    candles = make_candles([
        (100, 101, 99, 100),   # 0
        (100, 101, 99, 100),   # 1
        (100, 101, 99, 100),   # 2 = c0, low=99
        (100, 99, 78, 80),     # 3 = displacement candle (middle)
        (80, 85, 75, 76),      # 4 = c2, high=85 < c0.low=99 -> bearish gap [85, 99]
        (80, 90, 80, 88),      # 5 later candle trades back into [85,99] -> mitigates at 5
    ])
    fvgs = find_fvg(candles, atr_period=2, min_fvg_mult=0.25)
    assert len(fvgs) == 1
    g = fvgs[0]
    assert (g.low, g.high) == (85, 99)
    assert g.kind == Direction.BEARISH
    assert g.mitigated_index == 5


def test_find_fvg_rejects_gap_smaller_than_min_size():
    candles = make_candles([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),    # c0, high=101
        (100, 130, 99, 129),    # displacement middle candle
        (129, 132, 101.05, 130),  # c2, low=101.05 -- gap of only 0.05, tiny
    ])
    assert find_fvg(candles, atr_period=2, min_fvg_mult=0.25) == []


# ── find_order_blocks ─────────────────────────────────────────────────────────


def test_find_order_blocks_bullish():
    # Bullish break at index 5: last down-close candle before it is index 3.
    candles = make_candles([
        (100, 101, 99, 100),   # 0
        (100, 101, 99, 100),   # 1
        (100, 101, 99, 100),   # 2
        (101, 102, 96, 97),    # 3 down-close candle (OB candidate), low=96 high=102
        (97, 99, 96, 98),      # 4 up-close filler
        (98, 130, 97, 128),    # 5 displacement candle, huge body -> the break's candle
    ])
    brk = StructureBreak(index=5, kind=BreakKind.BOS, direction=Direction.BULLISH,
                          broken_swing=Swing(0, 100, SwingKind.HIGH, 1), close=128)
    obs = find_order_blocks(candles, [brk], atr_period=2)
    assert len(obs) == 1
    ob = obs[0]
    assert ob.index == 3
    assert (ob.low, ob.high) == (96, 102)
    assert ob.kind == Direction.BULLISH
    assert ob.break_index == 5
    assert ob.mitigated_index is None


def test_find_order_blocks_none_when_break_candle_is_not_displacement():
    candles = make_candles([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (101, 102, 99, 100),   # down-close candle
        (100, 101, 99, 100.5),  # tiny-body "break" candle -- not a displacement candle
    ])
    brk = StructureBreak(index=3, kind=BreakKind.BOS, direction=Direction.BULLISH,
                          broken_swing=Swing(0, 100, SwingKind.HIGH, 1), close=100.5)
    assert find_order_blocks(candles, [brk], atr_period=2) == []


def test_find_order_blocks_ob_body_only():
    candles = make_candles([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (101, 102, 96, 97),    # down-close OB candle: body [97, 101], wick [96, 102]
        (97, 99, 96, 98),
        (98, 130, 97, 128),
    ])
    brk = StructureBreak(index=5, kind=BreakKind.BOS, direction=Direction.BULLISH,
                          broken_swing=Swing(0, 100, SwingKind.HIGH, 1), close=128)
    obs = find_order_blocks(candles, [brk], atr_period=2, ob_body_only=True)
    assert len(obs) == 1
    assert (obs[0].low, obs[0].high) == (97, 101)


# ── find_breakers ──────────────────────────────────────────────────────────────


def test_find_breakers_bearish_ob_flips_bullish():
    from hermes_trading.ict.types import OrderBlock

    # Existing bearish OB (formed earlier) at [95, 100].
    ob = OrderBlock(index=2, low=95, high=100, kind=Direction.BEARISH, break_index=3, mitigated_index=None)
    candles = make_candles([
        (100, 101, 99, 100),   # 0
        (100, 101, 99, 100),   # 1
        (100, 101, 99, 100),   # 2 (the OB candle itself, high=101 -- close enough for setup)
        (100, 101, 99, 100),   # 3 filler (ATR warm-up)
        (100, 101, 99, 100),   # 4 filler
        (100, 130, 99, 129),   # 5 bullish displacement closes above ob.high (100) -> flips OB
    ])
    flip_break = StructureBreak(index=5, kind=BreakKind.MSS, direction=Direction.BULLISH,
                                 broken_swing=Swing(0, 100, SwingKind.HIGH, 1), close=129)
    breakers = find_breakers([ob], [flip_break], candles, atr_period=2)
    assert len(breakers) == 1
    b = breakers[0]
    assert b.order_block is ob
    assert b.flip_index == 5
    assert b.kind == Direction.BULLISH
    assert b.mitigated_index is None


def test_find_breakers_none_when_close_does_not_violate_ob():
    from hermes_trading.ict.types import OrderBlock

    ob = OrderBlock(index=2, low=95, high=100, kind=Direction.BEARISH, break_index=3, mitigated_index=None)
    candles = make_candles([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 99.5, 90, 91),   # bearish move, closes well BELOW ob.high -- no violation upward
    ])
    flip_break = StructureBreak(index=5, kind=BreakKind.BOS, direction=Direction.BULLISH,
                                 broken_swing=Swing(0, 100, SwingKind.HIGH, 1), close=91)
    assert find_breakers([ob], [flip_break], candles, atr_period=2) == []
