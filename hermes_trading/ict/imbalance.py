"""
hermes_trading.ict.imbalance -- displacement, FVG, order blocks, breaker blocks.

Spec sections: S:3.6 (displacement), S:3.7 (FVG), S:3.8 (order block),
S:3.8b (breaker block).
"""
from __future__ import annotations

from typing import Sequence

from hermes_trading.ict.types import (
    Breaker,
    Direction,
    FVG,
    OrderBlock,
    StructureBreak,
)
from hermes_trading.ict.util import Candle, atr

DEFAULT_ATR_PERIOD = 14
DEFAULT_DISP_ATR_MULT = 1.5
DEFAULT_MIN_FVG_MULT = 0.25


def _body(c: Candle) -> float:
    return abs(c.close - c.open)


def is_displacement(
    candles: Sequence[Candle],
    i: int,
    *,
    atr_period: int = DEFAULT_ATR_PERIOD,
    disp_atr_mult: float = DEFAULT_DISP_ATR_MULT,
    atr_series: list[float | None] | None = None,
) -> bool:
    """
    Displacement candle test: body >= disp_atr x ATR(14). Spec S:3.6.

    Only the body-size half of the spec definition -- "and it is the candle
    that produces the BOS/MSS close" is a contextual fact checked by matching
    this candle's index against a StructureBreak.index, done by the callers
    below (find_order_blocks, find_breakers), not by this function itself.
    """
    if atr_series is None:
        atr_series = atr(candles, atr_period)
    ref_atr = atr_series[i]
    if ref_atr is None:
        return False
    return _body(candles[i]) >= disp_atr_mult * ref_atr


def _find_mitigation(candles: Sequence[Candle], start_index: int, zone_low: float, zone_high: float) -> int | None:
    """First bar after start_index whose range overlaps [zone_low, zone_high]."""
    for j in range(start_index + 1, len(candles)):
        c = candles[j]
        if c.low <= zone_high and c.high >= zone_low:
            return j
    return None


def find_fvg(
    candles: Sequence[Candle],
    *,
    atr_period: int = DEFAULT_ATR_PERIOD,
    disp_atr_mult: float = DEFAULT_DISP_ATR_MULT,
    min_fvg_mult: float = DEFAULT_MIN_FVG_MULT,
) -> list[FVG]:
    """
    3-candle Fair Value Gaps. Spec S:3.7.

    A gap at i (comparing candles[i-2] and candles[i]) is only a valid FVG if
    its size >= min_fvg x ATR(14) AND the middle candle (i-1) is itself a
    displacement candle (S:3.6) -- both required, per spec "Valid only if".
    mitigated_index is the first later bar whose range re-enters the zone.
    """
    atr_series = atr(candles, atr_period)
    fvgs: list[FVG] = []
    for i in range(2, len(candles)):
        ref_atr = atr_series[i]
        if ref_atr is None:
            continue
        if not is_displacement(candles, i - 1, disp_atr_mult=disp_atr_mult, atr_series=atr_series):
            continue
        c0, c2 = candles[i - 2], candles[i]
        min_size = min_fvg_mult * ref_atr
        if c0.high < c2.low and (c2.low - c0.high) >= min_size:
            mit = _find_mitigation(candles, i, c0.high, c2.low)
            fvgs.append(FVG(index=i, low=c0.high, high=c2.low, kind=Direction.BULLISH, displacement=True, mitigated_index=mit))
        elif c0.low > c2.high and (c0.low - c2.high) >= min_size:
            mit = _find_mitigation(candles, i, c2.high, c0.low)
            fvgs.append(FVG(index=i, low=c2.high, high=c0.low, kind=Direction.BEARISH, displacement=True, mitigated_index=mit))
    return fvgs


def find_order_blocks(
    candles: Sequence[Candle],
    breaks: Sequence[StructureBreak],
    *,
    ob_body_only: bool = False,
    atr_period: int = DEFAULT_ATR_PERIOD,
    disp_atr_mult: float = DEFAULT_DISP_ATR_MULT,
) -> list[OrderBlock]:
    """
    Order blocks. Spec S:3.8.

    Bullish OB: last down-close candle before the displacement candle that
    produced a bullish BOS/MSS. Bearish OB: last up-close candle before a
    bearish-break displacement. The break's own candle must itself pass the
    displacement test, per spec ("before a ... displacement that causes a
    BOS/MSS") -- a break whose candle isn't a displacement candle yields no
    OB. mitigated_index tracks the first re-test after the break.
    """
    atr_series = atr(candles, atr_period)
    obs: list[OrderBlock] = []
    for brk in breaks:
        i = brk.index
        if not is_displacement(candles, i, disp_atr_mult=disp_atr_mult, atr_series=atr_series):
            continue
        want_down = brk.direction == Direction.BULLISH
        ob_index = None
        for j in range(i - 1, -1, -1):
            c = candles[j]
            if want_down and c.close < c.open:
                ob_index = j
                break
            if not want_down and c.close > c.open:
                ob_index = j
                break
        if ob_index is None:
            continue
        c = candles[ob_index]
        if ob_body_only:
            low, high = min(c.open, c.close), max(c.open, c.close)
        else:
            low, high = c.low, c.high
        mit = _find_mitigation(candles, i, low, high)
        obs.append(OrderBlock(index=ob_index, low=low, high=high, kind=brk.direction, break_index=i, mitigated_index=mit))
    return obs


def find_breakers(
    order_blocks: Sequence[OrderBlock],
    breaks: Sequence[StructureBreak],
    candles: Sequence[Candle],
    *,
    atr_period: int = DEFAULT_ATR_PERIOD,
    disp_atr_mult: float = DEFAULT_DISP_ATR_MULT,
) -> list[Breaker]:
    """
    Breaker blocks: a failed OB that flips polarity. Spec S:3.8b.

    A bearish OB flips bullish (breaker) when a later bullish-direction
    structure break's displacement candle closes above the OB's high;
    mirror for bullish OB -> bearish breaker. The flip candle must itself be
    a displacement candle. mitigated_index tracks re-tests since the flip
    (spec: "the zone must be untested since the flip").
    """
    atr_series = atr(candles, atr_period)
    breakers: list[Breaker] = []
    for ob in order_blocks:
        flip_kind = Direction.BULLISH if ob.kind == Direction.BEARISH else Direction.BEARISH
        for brk in sorted(breaks, key=lambda b: b.index):
            if brk.direction != flip_kind or brk.index <= ob.index:
                continue
            if not is_displacement(candles, brk.index, disp_atr_mult=disp_atr_mult, atr_series=atr_series):
                continue
            close = candles[brk.index].close
            violated = close > ob.high if flip_kind == Direction.BULLISH else close < ob.low
            if not violated:
                continue
            mit = _find_mitigation(candles, brk.index, ob.low, ob.high)
            breakers.append(Breaker(order_block=ob, flip_index=brk.index, kind=flip_kind, mitigated_index=mit))
            break
    return breakers
