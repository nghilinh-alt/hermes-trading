"""
hermes_trading.ict.liquidity -- S/R zones, liquidity pools, equal highs/lows, sweeps.

Spec sections: S:3.3 (S/R zones), S:3.4 (liquidity pools), S:3.5 (sweeps).
"""
from __future__ import annotations

from typing import Callable, Sequence

from hermes_trading.ict.types import (
    Direction,
    LiquidityKind,
    LiquidityPool,
    LiquiditySource,
    Swing,
    SwingKind,
    Sweep,
    Zone,
    ZoneKind,
)
from hermes_trading.ict.util import Candle, atr

DEFAULT_ATR_PERIOD = 14
DEFAULT_SR_TOL_MULT = 0.15
DEFAULT_MIN_TOUCHES = 2
DEFAULT_SR_LOOKBACK = 150
DEFAULT_EQL_TOL_MULT = 0.10
DEFAULT_SWEEP_PENETRATION_MULT = 0.10
DEFAULT_SWEEP_MAX_BARS = 1


def _cluster_swings(swings_sorted: list[Swing], tol: float) -> list[list[Swing]]:
    """Greedy chain-clustering by price: consecutive members within `tol`."""
    if not swings_sorted:
        return []
    clusters: list[list[Swing]] = [[swings_sorted[0]]]
    for s in swings_sorted[1:]:
        if s.price - clusters[-1][-1].price <= tol:
            clusters[-1].append(s)
        else:
            clusters.append([s])
    return clusters


def _avg_volume(candles: Sequence[Candle], as_of_index: int, lookback: int) -> float:
    start = max(0, as_of_index - lookback + 1)
    window = candles[start : as_of_index + 1]
    if not window:
        return 0.0
    return sum(c.volume for c in window) / len(window)


def sr_zones(
    candles: Sequence[Candle],
    swings: Sequence[Swing],
    *,
    atr_period: int = DEFAULT_ATR_PERIOD,
    sr_tol_mult: float = DEFAULT_SR_TOL_MULT,
    min_touches: int = DEFAULT_MIN_TOUCHES,
    sr_lookback: int = DEFAULT_SR_LOOKBACK,
    as_of_index: int | None = None,
) -> list[Zone]:
    """
    Cluster confirmed swing pivots into S/R zones. Spec S:3.3.

    strength = touches x recency_weight x volume_weight, where recency_weight
    and volume_weight are averaged across the zone's member pivots (recency
    decays linearly to 0 over `sr_lookback` bars measured from `as_of_index`;
    volume_weight is a pivot's own-candle volume relative to the trailing
    `sr_lookback`-bar average volume as of `as_of_index`).
    """
    if as_of_index is None:
        as_of_index = len(candles) - 1
    atr_series = atr(candles, atr_period)
    ref_atr = atr_series[as_of_index]
    if ref_atr is None:
        return []
    tol = sr_tol_mult * ref_atr
    avg_vol = _avg_volume(candles, as_of_index, sr_lookback)

    confirmed = [s for s in swings if s.confirmed_index <= as_of_index]
    zones: list[Zone] = []
    for kind, swing_kind in ((ZoneKind.RESISTANCE, SwingKind.HIGH), (ZoneKind.SUPPORT, SwingKind.LOW)):
        pts = sorted((s for s in confirmed if s.kind == swing_kind), key=lambda s: s.price)
        for cluster in _cluster_swings(pts, tol):
            if len(cluster) < min_touches:
                continue
            prices = [s.price for s in cluster]
            recency = sum(max(0.0, 1 - (as_of_index - s.index) / sr_lookback) for s in cluster) / len(cluster)
            volume = sum((candles[s.index].volume / avg_vol if avg_vol else 1.0) for s in cluster) / len(cluster)
            zones.append(
                Zone(
                    price_low=min(prices),
                    price_high=max(prices),
                    kind=kind,
                    touches=len(cluster),
                    strength=len(cluster) * recency * volume,
                    member_indices=tuple(s.index for s in cluster),
                )
            )
    return zones


def equal_highs(
    swings: Sequence[Swing],
    candles: Sequence[Candle],
    *,
    atr_period: int = DEFAULT_ATR_PERIOD,
    eql_tol_mult: float = DEFAULT_EQL_TOL_MULT,
    as_of_index: int | None = None,
) -> list[LiquidityPool]:
    """Equal highs magnet -- >=2 swing highs within eql_tol of each other. Spec S:3.4."""
    return _equal_pools(swings, candles, SwingKind.HIGH, LiquidityKind.BUYSIDE, LiquiditySource.EQUAL_HIGHS,
                         atr_period=atr_period, eql_tol_mult=eql_tol_mult, as_of_index=as_of_index)


def equal_lows(
    swings: Sequence[Swing],
    candles: Sequence[Candle],
    *,
    atr_period: int = DEFAULT_ATR_PERIOD,
    eql_tol_mult: float = DEFAULT_EQL_TOL_MULT,
    as_of_index: int | None = None,
) -> list[LiquidityPool]:
    """Equal lows magnet -- >=2 swing lows within eql_tol of each other. Spec S:3.4."""
    return _equal_pools(swings, candles, SwingKind.LOW, LiquidityKind.SELLSIDE, LiquiditySource.EQUAL_LOWS,
                         atr_period=atr_period, eql_tol_mult=eql_tol_mult, as_of_index=as_of_index)


def _equal_pools(
    swings: Sequence[Swing],
    candles: Sequence[Candle],
    swing_kind: SwingKind,
    liq_kind: LiquidityKind,
    source: LiquiditySource,
    *,
    atr_period: int,
    eql_tol_mult: float,
    as_of_index: int | None,
) -> list[LiquidityPool]:
    if as_of_index is None:
        as_of_index = len(candles) - 1
    atr_series = atr(candles, atr_period)
    ref_atr = atr_series[as_of_index]
    if ref_atr is None:
        return []
    tol = eql_tol_mult * ref_atr
    confirmed = [s for s in swings if s.kind == swing_kind and s.confirmed_index <= as_of_index]
    pts = sorted(confirmed, key=lambda s: s.price)
    pools: list[LiquidityPool] = []
    for cluster in _cluster_swings(pts, tol):
        if len(cluster) < 2:
            continue
        price = sum(s.price for s in cluster) / len(cluster)
        last = max(cluster, key=lambda s: s.index)
        pools.append(
            LiquidityPool(price=price, kind=liq_kind, source=source, index=last.index,
                          member_indices=tuple(s.index for s in cluster))
        )
    return pools


def prior_period_high_low(closed_period_candles: Sequence[Candle]) -> tuple[float, float]:
    """
    (high, low) of the most recently CLOSED bar of a higher-timeframe series.
    Feed daily candles for PDH/PDL, weekly candles for PWH/PWL (S:3.3/S:3.4)
    -- the caller is responsible for only passing already-closed HTF bars.
    """
    if not closed_period_candles:
        raise ValueError("need at least one closed period candle")
    last = closed_period_candles[-1]
    return last.high, last.low


def liquidity_pools(
    candles: Sequence[Candle],
    swings: Sequence[Swing],
    *,
    atr_period: int = DEFAULT_ATR_PERIOD,
    eql_tol_mult: float = DEFAULT_EQL_TOL_MULT,
    as_of_index: int | None = None,
    daily_candles: Sequence[Candle] | None = None,
    weekly_candles: Sequence[Candle] | None = None,
) -> list[LiquidityPool]:
    """
    Full liquidity map: every confirmed swing (prior high/low), equal-high/low
    clusters, and PDH/PDL/PWH/PWL if HTF series are supplied. Spec S:3.4.
    """
    if as_of_index is None:
        as_of_index = len(candles) - 1
    confirmed = [s for s in swings if s.confirmed_index <= as_of_index]

    pools: list[LiquidityPool] = []
    for s in confirmed:
        if s.kind == SwingKind.HIGH:
            pools.append(LiquidityPool(price=s.price, kind=LiquidityKind.BUYSIDE, source=LiquiditySource.SWING,
                                        index=s.index, member_indices=(s.index,)))
        else:
            pools.append(LiquidityPool(price=s.price, kind=LiquidityKind.SELLSIDE, source=LiquiditySource.SWING,
                                        index=s.index, member_indices=(s.index,)))

    pools += equal_highs(swings, candles, atr_period=atr_period, eql_tol_mult=eql_tol_mult, as_of_index=as_of_index)
    pools += equal_lows(swings, candles, atr_period=atr_period, eql_tol_mult=eql_tol_mult, as_of_index=as_of_index)

    if daily_candles:
        pdh, pdl = prior_period_high_low(daily_candles)
        idx = len(daily_candles) - 1
        pools.append(LiquidityPool(price=pdh, kind=LiquidityKind.BUYSIDE, source=LiquiditySource.PDH, index=idx))
        pools.append(LiquidityPool(price=pdl, kind=LiquidityKind.SELLSIDE, source=LiquiditySource.PDL, index=idx))

    if weekly_candles:
        pwh, pwl = prior_period_high_low(weekly_candles)
        idx = len(weekly_candles) - 1
        pools.append(LiquidityPool(price=pwh, kind=LiquidityKind.BUYSIDE, source=LiquiditySource.PWH, index=idx))
        pools.append(LiquidityPool(price=pwl, kind=LiquidityKind.SELLSIDE, source=LiquiditySource.PWL, index=idx))

    return pools


def _find_close_back(candles: Sequence[Candle], wick_index: int, max_bars: int, ok: Callable[[Candle], bool]) -> int | None:
    for offset in range(max_bars):
        idx = wick_index + offset
        if idx >= len(candles):
            break
        if ok(candles[idx]):
            return idx
    return None


def detect_sweep(
    candles: Sequence[Candle],
    pools: Sequence[LiquidityPool],
    *,
    atr_period: int = DEFAULT_ATR_PERIOD,
    penetration_mult: float = DEFAULT_SWEEP_PENETRATION_MULT,
    max_bars: int = DEFAULT_SWEEP_MAX_BARS,
) -> list[Sweep]:
    """
    Liquidity sweep / stop hunt. Spec S:3.5.

    For each pool, scans forward from the bar after it was established: price
    must trade beyond the level by >= penetration_mult x ATR, then close back
    on the origin side within `max_bars` bars (default 1 = the same candle).
    A pool may be swept more than once over time (re-sweeps); each sweep's
    event index is the close-back bar, never earlier than the wick bar, so a
    sweep at index i is unaffected by any candle after i.
    """
    atr_series = atr(candles, atr_period)
    sweeps: list[Sweep] = []
    for pool in pools:
        i = pool.index + 1
        while i < len(candles):
            ref_atr = atr_series[i]
            if ref_atr is None:
                i += 1
                continue
            needed = penetration_mult * ref_atr
            c = candles[i]
            found: Sweep | None = None
            if pool.kind == LiquidityKind.SELLSIDE and c.low <= pool.price - needed:
                close_idx = _find_close_back(candles, i, max_bars, lambda cc: cc.close > pool.price)
                if close_idx is not None:
                    found = Sweep(index=close_idx, pool=pool, penetration=pool.price - c.low, direction=Direction.BULLISH)
            elif pool.kind == LiquidityKind.BUYSIDE and c.high >= pool.price + needed:
                close_idx = _find_close_back(candles, i, max_bars, lambda cc: cc.close < pool.price)
                if close_idx is not None:
                    found = Sweep(index=close_idx, pool=pool, penetration=c.high - pool.price, direction=Direction.BEARISH)
            if found is not None:
                sweeps.append(found)
                i = found.index + 1
            else:
                i += 1
    sweeps.sort(key=lambda sw: sw.index)
    return sweeps
