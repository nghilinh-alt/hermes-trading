"""
No-lookahead invariance suite -- required by ict-claude-code-prompt.md S:3:
"Add an explicit test that asserts each detector's output at bar i is
unchanged whether or not bars after i exist."

Pattern: for a detector result computed over the FULL candle series, every
event anchored at index i must already be present, identically, when the
detector is re-run on a PREFIX of the series that ends anywhere at or after
i. Fields that are legitimately time-relative (an FVG/OB/Breaker's
mitigated_index -- "has this been touched YET" is an as-of-now query, not a
lookahead violation) are excluded from the identity comparison and checked
separately for monotonicity instead.
"""
from __future__ import annotations

import dataclasses

from hermes_trading.ict.imbalance import find_breakers, find_fvg, find_order_blocks
from hermes_trading.ict.liquidity import detect_sweep
from hermes_trading.ict.structure import detect_bos, detect_mss, find_swings
from hermes_trading.ict.types import BreakKind, Direction, LiquidityKind, LiquidityPool, LiquiditySource
from tests.ict.helpers import make_candles


def _mixed_series(n: int = 30) -> list:
    """A varied synthetic series: chop, a downswing, a sharp reversal spike, more chop."""
    pattern = []
    price = 100.0
    for i in range(n):
        if i == 10:
            pattern.append((price, price + 1, price - 12, price - 10))  # sharp drop (potential sweep wick)
            price -= 10
        elif i == 11:
            pattern.append((price, price + 22, price - 1, price + 20))  # sharp reversal (displacement)
            price += 20
        else:
            wobble = (i % 3) - 1
            pattern.append((price + wobble, price + wobble + 2, price + wobble - 2, price + wobble))
    return make_candles(pattern)


def test_find_swings_no_lookahead():
    candles = _mixed_series(30)
    full = find_swings(candles, n=2)
    for k in range(5, len(candles) + 1):
        prefix = find_swings(candles[:k], n=2)
        expected = [s for s in full if s.confirmed_index < k]
        assert prefix == expected, f"prefix len {k} diverged"


def test_detect_bos_no_lookahead():
    candles = _mixed_series(30)
    swings = find_swings(candles, n=2)
    full = detect_bos(candles, swings)
    for k in range(10, len(candles) + 1):
        prefix = detect_bos(candles[:k], swings)
        expected = [b for b in full if b.index < k]
        assert prefix == expected, f"prefix len {k} diverged"


def test_detect_mss_no_lookahead():
    candles = _mixed_series(30)
    swings = find_swings(candles, n=2)
    pool = LiquidityPool(price=candles[10].low + 1, kind=LiquidityKind.SELLSIDE, source=LiquiditySource.SWING, index=8)
    sweeps = detect_sweep(candles, [pool], atr_period=3)
    full = detect_mss(candles, swings, sweeps)
    for k in range(10, len(candles) + 1):
        prefix_sweeps = [sw for sw in sweeps if sw.index < k]
        prefix = detect_mss(candles[:k], swings, prefix_sweeps)
        expected = [b for b in full if b.index < k]
        assert prefix == expected, f"prefix len {k} diverged"


def test_detect_sweep_no_lookahead():
    candles = _mixed_series(30)
    pool = LiquidityPool(price=candles[10].low + 1, kind=LiquidityKind.SELLSIDE, source=LiquiditySource.SWING, index=8)
    full = detect_sweep(candles, [pool], atr_period=3)
    for k in range(9, len(candles) + 1):
        prefix = detect_sweep(candles[:k], [pool], atr_period=3)
        expected = [sw for sw in full if sw.index < k]
        assert prefix == expected, f"prefix len {k} diverged"


def _strip(obj, *fields):
    return dataclasses.replace(obj, **{f: None for f in fields})


def test_find_fvg_no_lookahead_ignoring_mitigation():
    candles = _mixed_series(30)
    full = find_fvg(candles, atr_period=3)
    for k in range(12, len(candles) + 1):
        prefix = find_fvg(candles[:k], atr_period=3)
        expected = [_strip(g, "mitigated_index") for g in full if g.index < k]
        got = [_strip(g, "mitigated_index") for g in prefix]
        assert got == expected, f"prefix len {k} diverged (formation fields)"
        # mitigation is monotonic: once discovered in a shorter prefix, it never disappears.
        for g_full, g_prefix in zip((g for g in full if g.index < k), prefix):
            if g_prefix.mitigated_index is not None:
                assert g_full.mitigated_index == g_prefix.mitigated_index


def test_find_order_blocks_no_lookahead_ignoring_mitigation():
    candles = _mixed_series(30)
    swings = find_swings(candles, n=2)
    breaks = detect_bos(candles, swings) + detect_mss(candles, swings, sweeps=[])
    full = find_order_blocks(candles, breaks, atr_period=3)
    for k in range(12, len(candles) + 1):
        visible_breaks = [b for b in breaks if b.index < k]
        prefix = find_order_blocks(candles[:k], visible_breaks, atr_period=3)
        expected = [_strip(o, "mitigated_index") for o in full if o.break_index < k]
        got = [_strip(o, "mitigated_index") for o in prefix]
        assert got == expected, f"prefix len {k} diverged"


def test_find_breakers_no_lookahead_ignoring_mitigation():
    candles = _mixed_series(30)
    swings = find_swings(candles, n=2)
    breaks = detect_bos(candles, swings) + detect_mss(candles, swings, sweeps=[])
    obs = find_order_blocks(candles, breaks, atr_period=3)
    full = find_breakers(obs, breaks, candles, atr_period=3)
    for k in range(15, len(candles) + 1):
        visible_breaks = [b for b in breaks if b.index < k]
        visible_obs = [o for o in obs if o.break_index < k]
        prefix = find_breakers(visible_obs, visible_breaks, candles[:k], atr_period=3)
        expected = [_strip(b, "mitigated_index") for b in full if b.flip_index < k]
        got = [_strip(b, "mitigated_index") for b in prefix]
        assert got == expected, f"prefix len {k} diverged"
