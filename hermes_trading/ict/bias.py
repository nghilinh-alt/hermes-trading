"""
hermes_trading.ict.bias -- HTF bias engine, dealing range, premium/discount/OTE.

Spec sections: S:3.9 (premium/discount, OTE), S:4 (HTF bias engine).
"""
from __future__ import annotations

from typing import Sequence

from hermes_trading.ict.structure import alternate_swings, market_structure
from hermes_trading.ict.types import (
    Bias,
    BiasDirection,
    DealingRange,
    Direction,
    PremiumDiscount,
    PremiumDiscountZone,
    Swing,
    SwingKind,
    TrendState,
)
from hermes_trading.ict.util import Candle

DEFAULT_OTE_LOW = 0.62
DEFAULT_OTE_HIGH = 0.79


def dealing_range(swings: Sequence[Swing], as_of_index: int) -> DealingRange | None:
    """
    Current significant swing low -> swing high on the reference TF. Spec S:3.9.
    None if fewer than one confirmed swing high and one confirmed swing low
    exist yet as of `as_of_index`.
    """
    confirmed = [s for s in swings if s.confirmed_index <= as_of_index]
    alt = alternate_swings(confirmed)
    highs = [s for s in alt if s.kind == SwingKind.HIGH]
    lows = [s for s in alt if s.kind == SwingKind.LOW]
    if not highs or not lows:
        return None
    last_high, last_low = highs[-1], lows[-1]
    if last_low.price <= last_high.price:
        return DealingRange(low=last_low.price, high=last_high.price, low_index=last_low.index, high_index=last_high.index)
    return DealingRange(low=last_high.price, high=last_low.price, low_index=last_high.index, high_index=last_low.index)


def premium_discount(
    price: float,
    drange: DealingRange,
    *,
    direction: Direction | None = None,
    ote_low: float = DEFAULT_OTE_LOW,
    ote_high: float = DEFAULT_OTE_HIGH,
) -> PremiumDiscount:
    """
    Premium/discount + OTE verdict for `price` within `drange`. Spec S:3.9.

    retracement_pct is measured 0.0 at the range low, 1.0 at the range high
    (equilibrium = 0.5). OTE ("0.62-0.79 retracement") is inherently
    direction-relative -- a long setup's OTE is a 62-79% pullback from the
    high, landing in the LOW 21-38% of the range (discount); a short
    setup's OTE is the mirror, landing in the HIGH 62-79% (premium). Pass
    `direction` (the bias direction this check is for) to get the correct
    side; omitting it checks both sides (useful for inspection only, not a
    live gate decision).
    """
    pct = drange.retracement_pct(price)
    zone = PremiumDiscountZone.DISCOUNT if pct < 0.5 else PremiumDiscountZone.PREMIUM
    if direction == Direction.BULLISH:
        in_ote = (1 - ote_high) <= pct <= (1 - ote_low)
    elif direction == Direction.BEARISH:
        in_ote = ote_low <= pct <= ote_high
    else:
        in_ote = (ote_low <= pct <= ote_high) or ((1 - ote_high) <= pct <= (1 - ote_low))
    return PremiumDiscount(dealing_range=drange, retracement_pct=pct, zone=zone, in_ote=in_ote)


def compute_bias(
    weekly_candles: Sequence[Candle],
    weekly_swings: Sequence[Swing],
    daily_candles: Sequence[Candle],
    daily_swings: Sequence[Swing],
    price: float,
) -> Bias:
    """
    HTF bias engine. Spec S:4.

    Long: Weekly and Daily both uptrend, OR Weekly uptrend + Daily range
    with price in discount. Short: mirror. Otherwise no_trade (bias
    conflict is a disqualifier, not scored here -- that's S:9, Phase 2).
    """
    w_idx = len(weekly_candles) - 1
    d_idx = len(daily_candles) - 1
    weekly_trend = market_structure([s for s in weekly_swings if s.confirmed_index <= w_idx])
    daily_trend = market_structure([s for s in daily_swings if s.confirmed_index <= d_idx])

    drange = dealing_range(daily_swings, d_idx)

    if weekly_trend == TrendState.UPTREND and daily_trend == TrendState.UPTREND:
        return Bias(BiasDirection.LONG, weekly_trend, daily_trend, "weekly and daily both uptrend")
    if weekly_trend == TrendState.DOWNTREND and daily_trend == TrendState.DOWNTREND:
        return Bias(BiasDirection.SHORT, weekly_trend, daily_trend, "weekly and daily both downtrend")

    if weekly_trend == TrendState.UPTREND and daily_trend == TrendState.RANGE:
        if drange is not None and premium_discount(price, drange).zone == PremiumDiscountZone.DISCOUNT:
            return Bias(BiasDirection.LONG, weekly_trend, daily_trend, "weekly uptrend, daily range, price in discount")
        return Bias(BiasDirection.NO_TRADE, weekly_trend, daily_trend, "weekly uptrend, daily range, price not in discount")

    if weekly_trend == TrendState.DOWNTREND and daily_trend == TrendState.RANGE:
        if drange is not None and premium_discount(price, drange).zone == PremiumDiscountZone.PREMIUM:
            return Bias(BiasDirection.SHORT, weekly_trend, daily_trend, "weekly downtrend, daily range, price in premium")
        return Bias(BiasDirection.NO_TRADE, weekly_trend, daily_trend, "weekly downtrend, daily range, price not in premium")

    return Bias(BiasDirection.NO_TRADE, weekly_trend, daily_trend, "bias conflict or no clear HTF structure")
