"""
hermes_trading.ict.structure -- swing pivots, market structure/trend, BOS vs MSS.

Spec sections: S:3.1 (swing points), S:3.2 (market structure & trend).
All functions are pure and operate as if streaming: a result anchored at bar
`i` never depends on candles after `i + n` (the swing confirmation lag) or,
for detect_bos/detect_mss, after `i` itself.
"""
from __future__ import annotations

from typing import Sequence

from hermes_trading.ict.types import (
    BreakKind,
    Direction,
    Swing,
    SwingKind,
    StructureBreak,
    Sweep,
    TrendState,
)
from hermes_trading.ict.util import Candle

# Per-TF defaults, spec S:3.1.
DEFAULT_SWING_STRENGTH = {
    "15m": 2,
    "1h": 2,
    "4h": 3,
    "1d": 3,
    "1w": 3,
}


def find_swings(candles: Sequence[Candle], n: int) -> list[Swing]:
    """
    Fractal swing pivots. Spec S:3.1.

    Candle i is a swing high if high[i] is strictly greater than every high
    in the window [i-n, i+n] (excluding i itself); mirror for swing lows.
    A pivot at i is only ever returned once candles[i+n] exists in the input
    (`confirmed_index = i + n`) -- calling find_swings on a longer prefix of
    the same series never changes or removes a pivot already returned for a
    shorter prefix, it only ever adds new ones further along.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    swings: list[Swing] = []
    for i in range(n, len(candles) - n):
        window = range(i - n, i + n + 1)
        other_highs = [candles[j].high for j in window if j != i]
        other_lows = [candles[j].low for j in window if j != i]
        if candles[i].high > max(other_highs):
            swings.append(Swing(index=i, price=candles[i].high, kind=SwingKind.HIGH, confirmed_index=i + n))
        if candles[i].low < min(other_lows):
            swings.append(Swing(index=i, price=candles[i].low, kind=SwingKind.LOW, confirmed_index=i + n))
    return swings


def alternate_swings(swings: Sequence[Swing]) -> list[Swing]:
    """
    Collapse consecutive same-kind swings to the single most extreme one,
    producing a strictly alternating HIGH/LOW sequence. Raw fractal scanning
    can surface e.g. two swing highs in a row with no intervening swing low;
    trend/BOS/MSS logic (S:3.2) needs the alternating "zig-zag" reading.
    """
    ordered = sorted(swings, key=lambda s: s.index)
    result: list[Swing] = []
    for s in ordered:
        if result and result[-1].kind == s.kind:
            more_extreme = (
                s.price > result[-1].price
                if s.kind == SwingKind.HIGH
                else s.price < result[-1].price
            )
            if more_extreme:
                result[-1] = s
        else:
            result.append(s)
    return result


def market_structure(swings: Sequence[Swing]) -> TrendState:
    """
    Trend from the ordered confirmed swing sequence. Spec S:3.2.
    Uptrend: latest SH > prior SH and latest SL > prior SL.
    Downtrend: latest SH < prior SH and latest SL < prior SL. Else range.
    """
    alt = alternate_swings(swings)
    highs = [s for s in alt if s.kind == SwingKind.HIGH]
    lows = [s for s in alt if s.kind == SwingKind.LOW]
    if len(highs) < 2 or len(lows) < 2:
        return TrendState.RANGE
    if highs[-1].price > highs[-2].price and lows[-1].price > lows[-2].price:
        return TrendState.UPTREND
    if highs[-1].price < highs[-2].price and lows[-1].price < lows[-2].price:
        return TrendState.DOWNTREND
    return TrendState.RANGE


def _confirmed_as_of(swings: Sequence[Swing], i: int) -> list[Swing]:
    """Swings usable for a decision at bar i -- only those confirmed by i."""
    return [s for s in swings if s.confirmed_index <= i]


class _IncrementalTrend:
    """
    Maintains the alternating swing sequence (last 2 highs, last 2 lows)
    incrementally as swings are fed in confirmed_index order -- avoids
    detect_bos/detect_mss re-sorting and re-scanning the full swing history
    on every single bar (O(bars x swings) -> O(bars + swings log swings)).
    Produces identical results to calling alternate_swings()/market_structure()
    fresh each bar, since it only ever touches the most recent same-kind
    entry, exactly mirroring alternate_swings' own collapse rule.
    """

    __slots__ = ("last_kind", "highs", "lows")

    def __init__(self) -> None:
        self.last_kind: SwingKind | None = None
        self.highs: list[Swing] = []
        self.lows: list[Swing] = []

    def add(self, swing: Swing) -> None:
        target = self.highs if swing.kind == SwingKind.HIGH else self.lows
        if self.last_kind == swing.kind and target:
            more_extreme = (
                swing.price > target[-1].price if swing.kind == SwingKind.HIGH else swing.price < target[-1].price
            )
            if more_extreme:
                target[-1] = swing
        else:
            target.append(swing)
            if len(target) > 2:
                target.pop(0)
            self.last_kind = swing.kind

    @property
    def trend(self) -> TrendState:
        if len(self.highs) < 2 or len(self.lows) < 2:
            return TrendState.RANGE
        if self.highs[-1].price > self.highs[-2].price and self.lows[-1].price > self.lows[-2].price:
            return TrendState.UPTREND
        if self.highs[-1].price < self.highs[-2].price and self.lows[-1].price < self.lows[-2].price:
            return TrendState.DOWNTREND
        return TrendState.RANGE


def detect_bos(candles: Sequence[Candle], swings: Sequence[Swing]) -> list[StructureBreak]:
    """
    Break of Structure: trend-continuation close beyond the most recent
    same-direction confirmed swing extreme. Spec S:3.2.

    Walks bar by bar; at bar i, only swings with confirmed_index <= i are
    considered, so a break at i never depends on future data.
    """
    breaks: list[StructureBreak] = []
    sorted_swings = sorted(swings, key=lambda s: s.confirmed_index)
    tracker = _IncrementalTrend()
    ptr, n_swings = 0, len(sorted_swings)

    for i, c in enumerate(candles):
        while ptr < n_swings and sorted_swings[ptr].confirmed_index <= i:
            tracker.add(sorted_swings[ptr])
            ptr += 1
        trend = tracker.trend
        if trend == TrendState.UPTREND and tracker.highs and c.close > tracker.highs[-1].price:
            breaks.append(
                StructureBreak(index=i, kind=BreakKind.BOS, direction=Direction.BULLISH, broken_swing=tracker.highs[-1], close=c.close)
            )
        elif trend == TrendState.DOWNTREND and tracker.lows and c.close < tracker.lows[-1].price:
            breaks.append(
                StructureBreak(index=i, kind=BreakKind.BOS, direction=Direction.BEARISH, broken_swing=tracker.lows[-1], close=c.close)
            )
    return breaks


def detect_mss(
    candles: Sequence[Candle],
    swings: Sequence[Swing],
    sweeps: Sequence[Sweep],
) -> list[StructureBreak]:
    """
    Market Structure Shift: trend-reversal close beyond the most recent
    confirmed OPPOSING swing point, occurring after a liquidity sweep (S:3.5)
    against the prevailing trend. Spec S:3.2.

    A close beyond the opposing swing with no preceding sweep is not an MSS
    under this spec (the sweep is the precondition that distinguishes MSS
    from an ordinary break) -- it is simply not classified by this function.
    """
    breaks: list[StructureBreak] = []
    sorted_swings = sorted(swings, key=lambda s: s.confirmed_index)
    sorted_sweeps = sorted(sweeps, key=lambda sw: sw.index)
    tracker = _IncrementalTrend()
    swing_ptr, n_swings = 0, len(sorted_swings)
    sweep_ptr, n_sweeps = 0, len(sorted_sweeps)
    last_sweep: Sweep | None = None

    for i, c in enumerate(candles):
        while swing_ptr < n_swings and sorted_swings[swing_ptr].confirmed_index <= i:
            tracker.add(sorted_swings[swing_ptr])
            swing_ptr += 1
        while sweep_ptr < n_sweeps and sorted_sweeps[sweep_ptr].index <= i:
            candidate = sorted_sweeps[sweep_ptr]
            # Ties (multiple sweeps at the same bar) keep the first one seen,
            # matching max(prior_sweeps, key=lambda sw: sw.index)'s tie behavior.
            if last_sweep is None or candidate.index > last_sweep.index:
                last_sweep = candidate
            sweep_ptr += 1
        if last_sweep is None:
            continue

        trend = tracker.trend
        if (
            trend == TrendState.DOWNTREND
            and tracker.highs
            and c.close > tracker.highs[-1].price
            and last_sweep.direction == Direction.BULLISH
        ):
            breaks.append(
                StructureBreak(index=i, kind=BreakKind.MSS, direction=Direction.BULLISH, broken_swing=tracker.highs[-1], close=c.close)
            )
        elif (
            trend == TrendState.UPTREND
            and tracker.lows
            and c.close < tracker.lows[-1].price
            and last_sweep.direction == Direction.BEARISH
        ):
            breaks.append(
                StructureBreak(index=i, kind=BreakKind.MSS, direction=Direction.BEARISH, broken_swing=tracker.lows[-1], close=c.close)
            )
    return breaks
