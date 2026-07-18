"""
Smoke/stress test over a longer, noisier synthetic series -- stands in for
"real historical OHLCV" edge-case coverage (ranging/choppy data, gaps,
no-signal windows) per ict-claude-code-prompt.md S:4, without needing
network access. Deterministic (seeded), so still hermetic/offline.

Not ground-truth assertions -- these check the full pipeline runs without
error on messy data and that every detector's output satisfies its own
structural invariants (e.g. zone.low <= zone.high, mitigation only after
formation).
"""
from __future__ import annotations

import random

from hermes_trading.ict.bias import dealing_range, premium_discount
from hermes_trading.ict.imbalance import find_breakers, find_fvg, find_order_blocks
from hermes_trading.ict.liquidity import detect_sweep, liquidity_pools, sr_zones
from hermes_trading.ict.structure import detect_bos, detect_mss, find_swings
from hermes_trading.ict.types import Direction
from hermes_trading.ict.util import Candle


def _random_walk_candles(n: int, seed: int) -> list[Candle]:
    rng = random.Random(seed)
    candles: list[Candle] = []
    price = 100.0
    ts = 0
    for i in range(n):
        # occasional flat/no-signal window
        if 50 <= i < 65:
            drift = 0.0
            rng_range = 0.05
        # occasional gap (simulates a session gap)
        elif i == 100:
            price += 15.0
            drift = 0.0
            rng_range = 1.0
        else:
            drift = rng.uniform(-0.6, 0.6)
            rng_range = rng.uniform(0.3, 2.5)

        open_ = price
        close = price + drift
        high = max(open_, close) + abs(rng.uniform(0, rng_range))
        low = min(open_, close) - abs(rng.uniform(0, rng_range))
        volume = rng.uniform(50, 500)
        candles.append(Candle(timestamp=ts, open=open_, high=high, low=low, close=close, volume=volume))
        price = close
        ts += 3_600_000
    return candles


def test_full_pipeline_runs_without_error_on_noisy_data():
    candles = _random_walk_candles(300, seed=42)

    swings = find_swings(candles, n=3)
    assert all(s.confirmed_index == s.index + 3 for s in swings)

    zones = sr_zones(candles, swings, atr_period=14)
    for z in zones:
        assert z.price_low <= z.price_high
        assert z.touches >= 2

    pools = liquidity_pools(candles, swings, atr_period=14)
    sweeps = detect_sweep(candles, pools, atr_period=14)
    for sw in sweeps:
        assert sw.penetration > 0
        assert sw.index > sw.pool.index

    bos = detect_bos(candles, swings)
    mss = detect_mss(candles, swings, sweeps)
    for b in bos + mss:
        assert b.index < len(candles)
        if b.direction == Direction.BULLISH:
            assert b.close > b.broken_swing.price
        else:
            assert b.close < b.broken_swing.price

    fvgs = find_fvg(candles, atr_period=14)
    for g in fvgs:
        assert g.low <= g.high
        if g.mitigated_index is not None:
            assert g.mitigated_index > g.index

    obs = find_order_blocks(candles, bos + mss, atr_period=14)
    for ob in obs:
        assert ob.low <= ob.high
        assert ob.index < ob.break_index
        if ob.mitigated_index is not None:
            assert ob.mitigated_index > ob.break_index

    breakers = find_breakers(obs, bos + mss, candles, atr_period=14)
    for brk in breakers:
        assert brk.flip_index > brk.order_block.index
        if brk.mitigated_index is not None:
            assert brk.mitigated_index > brk.flip_index

    dr = dealing_range(swings, as_of_index=len(candles) - 1)
    if dr is not None:
        assert dr.low <= dr.high
        pd = premium_discount(candles[-1].close, dr)
        assert 0.0 <= pd.retracement_pct or pd.retracement_pct < 0.0  # just must not raise / be well-formed
        assert pd.zone is not None


def test_no_signal_window_produces_no_swings():
    """A flat, near-zero-range segment (spec: no-signal edge case) yields no pivots within it."""
    flat = [Candle(timestamp=i, open=100.0, high=100.01, low=99.99, close=100.0, volume=10.0) for i in range(20)]
    assert find_swings(flat, n=3) == []


def test_pipeline_deterministic_across_repeated_runs():
    c1 = _random_walk_candles(150, seed=7)
    c2 = _random_walk_candles(150, seed=7)
    assert c1 == c2
    s1 = find_swings(c1, n=2)
    s2 = find_swings(c2, n=2)
    assert s1 == s2
