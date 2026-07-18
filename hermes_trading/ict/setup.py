"""
hermes_trading.ict.setup -- entry-zone selection, stop/target/RR, session
filter, Stage 1 gates, Stage 2 score/grade, and the build_setup orchestrator.

Spec sections: S:6 (entry/stop/target), S:8 (session filter), S:9
(qualification gates + score).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from hermes_trading.ict.types import (
    Bias,
    BiasDirection,
    Breaker,
    DealingRange,
    Direction,
    EntryZone,
    EntryZoneKind,
    FVG,
    Grade,
    LiquidityKind,
    LiquidityPool,
    OrderBlock,
    PositionSize,
    StructureBreak,
    Sweep,
    TradeSetup,
    TrendState,
)
from hermes_trading.ict.risk import position_size as _position_size

DEFAULT_MIN_RR = 2.0
DEFAULT_SL_BUFFER_MULT = 0.25
DEFAULT_MIN_TARGET_ATR_MULT = 2.0
DEFAULT_OTE_LOW = 0.62
DEFAULT_OTE_HIGH = 0.79
# (start_hour_utc, end_hour_utc) -- London and New York kill zones, spec S:8.
DEFAULT_KILL_ZONES: tuple[tuple[int, int], ...] = ((7, 10), (12, 15))


def _zone_bounds(zone: EntryZone, kind: EntryZoneKind) -> tuple[float, float]:
    if kind == EntryZoneKind.BREAKER:
        ob = zone.order_block
        return ob.low, ob.high
    return zone.low, zone.high


def _ote_price_band(dealing_range: DealingRange, direction: Direction, ote_low: float, ote_high: float) -> tuple[float, float]:
    """OTE is direction-relative (see bias.premium_discount docstring for why)."""
    span = dealing_range.high - dealing_range.low
    if direction == Direction.BULLISH:
        lo_pct, hi_pct = 1 - ote_high, 1 - ote_low
    else:
        lo_pct, hi_pct = ote_low, ote_high
    return dealing_range.low + lo_pct * span, dealing_range.low + hi_pct * span


def _overlaps(a_lo: float, a_hi: float, b_lo: float, b_hi: float) -> bool:
    return a_lo <= b_hi and a_hi >= b_lo


DEFAULT_MAX_BARS_AFTER_MSS = 10


def select_entry_zone(
    direction: Direction,
    mss: StructureBreak,
    fvgs: Sequence[FVG],
    order_blocks: Sequence[OrderBlock],
    breakers: Sequence[Breaker],
    dealing_range: DealingRange | None = None,
    *,
    ote_low: float = DEFAULT_OTE_LOW,
    ote_high: float = DEFAULT_OTE_HIGH,
    max_bars_after_mss: int = DEFAULT_MAX_BARS_AFTER_MSS,
    as_of_index: int | None = None,
) -> tuple[EntryZone, EntryZoneKind] | None:
    """
    Best unmitigated (as of `as_of_index`, default mss.index) entry zone
    left by the MSS's OWN displacement leg, matching direction. Spec S:6,
    S:8b, S:9: "locate the unmitigated ... FVG/OB/Breaker left by the
    displacement".

    Two bounds matter here, both guarding against callers that precompute
    fvgs/order_blocks/breakers once over an entire multi-year series:
    (1) candidates are bounded to `max_bars_after_mss` bars after
    mss.index, not an unbounded future -- otherwise "most recently formed,
    ties broken by highest index" would happily pick a zone from months or
    years later at a completely unrelated price level; (2) mitigation is
    checked AS OF `as_of_index`, not "ever mitigated across the whole
    series" (a zone's `.mitigated` flag reflects a retest at any point in
    the full precomputed series, including bars that haven't happened yet
    relative to this MSS -- using that raw boolean would wrongly reject
    almost every zone in a long backtest, since most price levels get
    retested eventually).

    "OB + overlapping FVG = highest-quality zone"; OTE overlap is
    separately preferred. Scored as (2 if composite with another candidate
    else 0) + (2 if overlaps OTE else 0), highest wins; ties broken by
    most recently formed. None if no candidate exists within the window.
    """
    cutoff = mss.index + max_bars_after_mss
    ref_index = mss.index if as_of_index is None else as_of_index

    def _unmitigated(mitigated_index: int | None) -> bool:
        return mitigated_index is None or mitigated_index > ref_index

    candidates: list[tuple[EntryZone, EntryZoneKind, int, float, float]] = []
    for fvg in fvgs:
        if fvg.kind == direction and _unmitigated(fvg.mitigated_index) and mss.index <= fvg.index <= cutoff:
            candidates.append((fvg, EntryZoneKind.FVG, fvg.index, fvg.low, fvg.high))
    for ob in order_blocks:
        if ob.kind == direction and _unmitigated(ob.mitigated_index) and mss.index <= ob.break_index <= cutoff:
            candidates.append((ob, EntryZoneKind.ORDER_BLOCK, ob.index, ob.low, ob.high))
    for brk in breakers:
        if brk.kind == direction and _unmitigated(brk.mitigated_index) and mss.index <= brk.flip_index <= cutoff:
            candidates.append((brk, EntryZoneKind.BREAKER, brk.flip_index, brk.order_block.low, brk.order_block.high))

    if not candidates:
        return None

    ote_band = _ote_price_band(dealing_range, direction, ote_low, ote_high) if dealing_range is not None else None

    def score(c: tuple) -> tuple[int, int]:
        _, kind, idx, lo, hi = c
        composite = any(_overlaps(lo, hi, o_lo, o_hi) for (_, o_kind, _, o_lo, o_hi) in candidates if o_kind != kind)
        in_ote = ote_band is not None and _overlaps(lo, hi, ote_band[0], ote_band[1])
        return (int(composite) * 2 + int(in_ote) * 2, idx)

    best = max(candidates, key=score)
    return best[0], best[1]


def compute_stop(sweep: Sweep, atr_value: float, *, sl_buffer_mult: float = DEFAULT_SL_BUFFER_MULT) -> float:
    """Beyond the sweep's wick extreme + sl_buffer x ATR. Spec S:6."""
    buffer = sl_buffer_mult * atr_value
    if sweep.direction == Direction.BULLISH:
        wick_extreme = sweep.pool.price - sweep.penetration
        return wick_extreme - buffer
    wick_extreme = sweep.pool.price + sweep.penetration
    return wick_extreme + buffer


def compute_target(direction: Direction, entry: float, pools: Sequence[LiquidityPool], *, min_distance: float = 0.0) -> float | None:
    """
    Nearest EXTERNAL liquidity pool beyond entry, in the bias direction.
    Spec S:6 ("opposing prior high/low, PDH/PWH, weekly liquidity" --
    "external" as opposed to internal/immediate structure).

    `liquidity_pools()` treats every confirmed swing as a pool, so without
    a floor, "nearest" is almost always some trivial swing a few ticks
    away, not a meaningful target -- min_distance (pass `min_target_atr_mult
    x ATR` from the caller) excludes pools too close to count as "external".
    """
    if direction == Direction.BULLISH:
        candidates = [p.price for p in pools if p.kind == LiquidityKind.BUYSIDE and p.price - entry >= min_distance]
        return min(candidates) if candidates else None
    candidates = [p.price for p in pools if p.kind == LiquidityKind.SELLSIDE and entry - p.price >= min_distance]
    return max(candidates) if candidates else None


def compute_rr(entry: float, stop: float, target: float) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    return abs(target - entry) / risk


def in_kill_zone(timestamp_ms: int, kill_zones: Sequence[tuple[int, int]] = DEFAULT_KILL_ZONES) -> bool:
    """Spec S:8: trade only inside London/NY kill zones (UTC hours)."""
    hour = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).hour
    return any(start <= hour < end for start, end in kill_zones)


def gate_htf_bias(direction: Direction, bias: Bias) -> bool:
    """
    Stage 1 gate 1: the setup's direction must match the already-computed
    S:4 HTF bias. Spec S:9 phrases this gate as "at least Daily aligned and
    Weekly not opposing" -- an EARLIER version of this function re-derived
    that independently from raw weekly/daily TrendState values, which was
    inconsistent with bias.compute_bias's own S:4 rule: every non-NO_TRADE
    bias.direction already has Weekly exactly aligned (compute_bias has no
    branch that permits Weekly=RANGE), and Daily is *either* strictly
    aligned *or* RANGE-with-correct-discount/premium (compute_bias's own
    permitted exception) -- the old literal "Daily must be UPTREND/DOWNTREND"
    re-check rejected that second, spec-valid case. Trusting bias.direction
    (computed once, upstream) avoids re-litigating it here with a stricter,
    inconsistent rule.
    """
    if direction == Direction.BULLISH:
        return bias.direction == BiasDirection.LONG
    return bias.direction == BiasDirection.SHORT


def stage1_gates(
    direction: Direction,
    bias: Bias,
    sweep: Sweep | None,
    mss: StructureBreak | None,
    entry_zone: EntryZone | None,
    rr: float | None,
    timestamp_ms: int,
    size: PositionSize | None,
    *,
    min_rr: float = DEFAULT_MIN_RR,
    kill_zones: Sequence[tuple[int, int]] = DEFAULT_KILL_ZONES,
) -> tuple[bool, list[str]]:
    """The 7 mandatory gates -- fail any -> disqualified regardless of score. Spec S:9 Stage 1."""
    failures: list[str] = []
    if not gate_htf_bias(direction, bias):
        failures.append("htf_bias")
    if sweep is None:
        failures.append("liquidity_event")
    if mss is None:
        failures.append("mss")
    if entry_zone is None:
        failures.append("entry_zone")
    if rr is None or rr < min_rr:
        failures.append("rr")
    if not in_kill_zone(timestamp_ms, kill_zones):
        failures.append("session")
    if size is None:
        failures.append("risk_filter")
    return (len(failures) == 0, failures)


def stage2_score(
    weekly_trend: TrendState,
    daily_trend: TrendState,
    direction: Direction,
    sweep: Sweep | None,
    mss: StructureBreak | None,
    displacement: bool,
    fvg_present: bool,
    ob_present: bool,
    entry_zone: EntryZone | None,
    entry_zone_kind: EntryZoneKind | None,
    dealing_range: DealingRange | None,
    rr: float | None,
    *,
    ote_low: float = DEFAULT_OTE_LOW,
    ote_high: float = DEFAULT_OTE_HIGH,
) -> int:
    """Weighted score, max 20. Spec S:9 Stage 2 table."""
    weekly_aligned = (
        (direction == Direction.BULLISH and weekly_trend == TrendState.UPTREND)
        or (direction == Direction.BEARISH and weekly_trend == TrendState.DOWNTREND)
    )
    daily_aligned = (
        (direction == Direction.BULLISH and daily_trend == TrendState.UPTREND)
        or (direction == Direction.BEARISH and daily_trend == TrendState.DOWNTREND)
    )

    score = 0
    score += 2 if weekly_aligned else 0
    score += 2 if daily_aligned else 0
    score += 3 if sweep is not None else 0
    score += 3 if mss is not None else 0
    score += 2 if displacement else 0
    score += 2 if fvg_present else 0
    score += 2 if ob_present else 0
    if entry_zone is not None and entry_zone_kind is not None and dealing_range is not None:
        lo, hi = _zone_bounds(entry_zone, entry_zone_kind)
        band = _ote_price_band(dealing_range, direction, ote_low, ote_high)
        if _overlaps(lo, hi, band[0], band[1]):
            score += 2
    score += 2 if (rr is not None and rr > 2.0) else 0
    return score


def grade_from_score(score: int, *, a_plus_threshold: int = 14, b_threshold: int = 11) -> Grade:
    """A+ >= 14, B 11-13, else NONE. Spec S:9."""
    if score >= a_plus_threshold:
        return Grade.A_PLUS
    if score >= b_threshold:
        return Grade.B
    return Grade.NONE


def build_setup(
    bias: Bias,
    sweep: Sweep,
    mss: StructureBreak,
    fvgs: Sequence[FVG],
    order_blocks: Sequence[OrderBlock],
    breakers: Sequence[Breaker],
    pools: Sequence[LiquidityPool],
    dealing_range: DealingRange | None,
    atr_value: float,
    displacement: bool,
    timestamp_ms: int,
    equity: float,
    *,
    min_rr: float = DEFAULT_MIN_RR,
    sl_buffer_mult: float = DEFAULT_SL_BUFFER_MULT,
    ote_low: float = DEFAULT_OTE_LOW,
    ote_high: float = DEFAULT_OTE_HIGH,
    kill_zones: Sequence[tuple[int, int]] = DEFAULT_KILL_ZONES,
    lev_max: int = 10,
    max_bars_after_mss: int = DEFAULT_MAX_BARS_AFTER_MSS,
    min_target_atr_mult: float = DEFAULT_MIN_TARGET_ATR_MULT,
    a_plus_threshold: int = 14,
    b_threshold: int = 11,
) -> TradeSetup | None:
    """
    Evaluate one bias+sweep+MSS combination end to end: pick the entry
    zone, compute stop/target/RR, score, grade, size. Returns None only
    when there's no bias direction to evaluate at all (Bias.direction is
    NO_TRADE) -- otherwise always returns a TradeSetup so the caller can
    see why it was or wasn't qualified (`.qualified`, `.gate_failures`).
    `displacement` is whether the MSS's own breaking candle passed the
    S:3.6 displacement test -- computed by the caller (imbalance.is_displacement)
    since this module deliberately never touches raw OHLCV. Spec S:6, S:9.
    """
    if bias.direction == BiasDirection.NO_TRADE:
        return None
    direction = Direction.BULLISH if bias.direction == BiasDirection.LONG else Direction.BEARISH

    zone_pick = select_entry_zone(direction, mss, fvgs, order_blocks, breakers, dealing_range, ote_low=ote_low,
                                   ote_high=ote_high, max_bars_after_mss=max_bars_after_mss, as_of_index=mss.index)
    entry_zone, entry_zone_kind = zone_pick if zone_pick is not None else (None, None)

    entry_price = stop_price = target_price = rr = None
    if entry_zone is not None:
        lo, hi = _zone_bounds(entry_zone, entry_zone_kind)
        entry_price = (lo + hi) / 2
        stop_price = compute_stop(sweep, atr_value, sl_buffer_mult=sl_buffer_mult)
        target_price = compute_target(direction, entry_price, pools, min_distance=min_target_atr_mult * atr_value)
        if target_price is not None:
            rr = compute_rr(entry_price, stop_price, target_price)

    cutoff = mss.index + max_bars_after_mss
    fvg_present = any(
        f.kind == direction and (f.mitigated_index is None or f.mitigated_index > mss.index) and mss.index <= f.index <= cutoff
        for f in fvgs
    )
    ob_present = any(
        o.kind == direction and (o.mitigated_index is None or o.mitigated_index > mss.index) and mss.index <= o.break_index <= cutoff
        for o in order_blocks
    )

    score = stage2_score(
        bias.weekly_trend, bias.daily_trend, direction, sweep, mss, displacement,
        fvg_present, ob_present, entry_zone, entry_zone_kind, dealing_range, rr,
        ote_low=ote_low, ote_high=ote_high,
    )
    grade = grade_from_score(score, a_plus_threshold=a_plus_threshold, b_threshold=b_threshold)

    size = None
    if entry_price is not None and stop_price is not None and grade != Grade.NONE:
        size = _position_size(equity, entry_price, stop_price, grade, lev_max=lev_max)

    passed, failures = stage1_gates(
        direction, bias, sweep, mss, entry_zone, rr,
        timestamp_ms, size, min_rr=min_rr, kill_zones=kill_zones,
    )
    if grade == Grade.NONE:
        failures = failures + ["score_below_b"]

    return TradeSetup(
        direction=direction,
        bias=bias,
        sweep=sweep,
        mss=mss,
        entry_zone=entry_zone,
        entry_zone_kind=entry_zone_kind,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        rr=rr,
        score=score,
        grade=grade,
        gate_failures=tuple(failures),
    )
