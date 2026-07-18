"""Tests for hermes_trading.ict.bias -- spec S:3.9, S:4."""
from __future__ import annotations

from hermes_trading.ict.bias import compute_bias, dealing_range, premium_discount
from hermes_trading.ict.types import (
    BiasDirection,
    DealingRange,
    Direction,
    PremiumDiscountZone,
    Swing,
    SwingKind,
    TrendState,
)
from tests.ict.helpers import make_candles


# ── dealing_range ────────────────────────────────────────────────────────────


def test_dealing_range_from_last_confirmed_high_and_low():
    swings = [
        Swing(0, 100, SwingKind.LOW, 1),
        Swing(2, 110, SwingKind.HIGH, 3),
        Swing(4, 105, SwingKind.LOW, 5),
        Swing(6, 120, SwingKind.HIGH, 7),
    ]
    dr = dealing_range(swings, as_of_index=10)
    assert dr == DealingRange(low=105, high=120, low_index=4, high_index=6)


def test_dealing_range_none_without_both_kinds():
    assert dealing_range([Swing(0, 100, SwingKind.LOW, 1)], as_of_index=5) is None


def test_dealing_range_normalizes_when_last_low_is_above_last_high():
    # Pathological order: the most recent LOW swing sits above the most recent HIGH swing.
    swings = [
        Swing(0, 90, SwingKind.HIGH, 1),
        Swing(2, 100, SwingKind.LOW, 3),
    ]
    dr = dealing_range(swings, as_of_index=5)
    assert dr == DealingRange(low=90, high=100, low_index=0, high_index=2)


# ── premium_discount / OTE ────────────────────────────────────────────────────


def test_premium_discount_zone_classification():
    dr = DealingRange(low=100, high=200, low_index=0, high_index=10)
    assert premium_discount(120, dr).zone == PremiumDiscountZone.DISCOUNT
    assert premium_discount(180, dr).zone == PremiumDiscountZone.PREMIUM
    assert premium_discount(120, dr).retracement_pct == 0.2


def test_ote_band_for_long_setup_is_low_side_of_range():
    dr = DealingRange(low=100, high=200, low_index=0, high_index=10)
    # 62-79% retracement from the high = 21-38% of the range from the low.
    in_range = premium_discount(130, dr, direction=Direction.BULLISH)  # pct=0.30
    out_of_range = premium_discount(160, dr, direction=Direction.BULLISH)  # pct=0.60 -- premium, not OTE for a long
    assert in_range.in_ote is True
    assert out_of_range.in_ote is False


def test_ote_band_for_short_setup_is_high_side_of_range():
    dr = DealingRange(low=100, high=200, low_index=0, high_index=10)
    in_range = premium_discount(170, dr, direction=Direction.BEARISH)  # pct=0.70
    out_of_range = premium_discount(140, dr, direction=Direction.BEARISH)  # pct=0.40 -- discount, not OTE for a short
    assert in_range.in_ote is True
    assert out_of_range.in_ote is False


# ── compute_bias ──────────────────────────────────────────────────────────────


def _trend_swings(direction: str) -> list[Swing]:
    if direction == "up":
        return [
            Swing(0, 100, SwingKind.LOW, 1),
            Swing(2, 110, SwingKind.HIGH, 3),
            Swing(4, 105, SwingKind.LOW, 5),
            Swing(6, 120, SwingKind.HIGH, 7),
        ]
    if direction == "down":
        return [
            Swing(0, 120, SwingKind.HIGH, 1),
            Swing(2, 100, SwingKind.LOW, 3),
            Swing(4, 110, SwingKind.HIGH, 5),
            Swing(6, 90, SwingKind.LOW, 7),
        ]
    raise ValueError(direction)


def test_bias_long_when_weekly_and_daily_both_uptrend():
    weekly = make_candles([(100, 101, 99, 100)] * 8)
    daily = make_candles([(100, 101, 99, 100)] * 8)
    bias = compute_bias(weekly, _trend_swings("up"), daily, _trend_swings("up"), price=115)
    assert bias.direction == BiasDirection.LONG
    assert bias.weekly_trend == TrendState.UPTREND
    assert bias.daily_trend == TrendState.UPTREND


def test_bias_short_when_weekly_and_daily_both_downtrend():
    weekly = make_candles([(100, 101, 99, 100)] * 8)
    daily = make_candles([(100, 101, 99, 100)] * 8)
    bias = compute_bias(weekly, _trend_swings("down"), daily, _trend_swings("down"), price=95)
    assert bias.direction == BiasDirection.SHORT


def test_bias_long_when_weekly_uptrend_daily_range_and_price_in_discount():
    weekly = make_candles([(100, 101, 99, 100)] * 8)
    daily = make_candles([(100, 101, 99, 100)] * 8)
    # Conflicting HH/LL on daily -> RANGE, with a dealing range [100(low idx4), 120(high idx2)]... use a
    # range-producing sequence: higher high but lower low.
    daily_swings = [
        Swing(0, 100, SwingKind.LOW, 1),
        Swing(2, 110, SwingKind.HIGH, 3),
        Swing(4, 95, SwingKind.LOW, 5),    # lower low -> range
        Swing(6, 120, SwingKind.HIGH, 7),  # higher high -> range
    ]
    bias = compute_bias(weekly, _trend_swings("up"), daily, daily_swings, price=100)  # low in [95,120] -> discount
    assert bias.daily_trend == TrendState.RANGE
    assert bias.direction == BiasDirection.LONG


def test_bias_no_trade_on_weekly_daily_conflict():
    weekly = make_candles([(100, 101, 99, 100)] * 8)
    daily = make_candles([(100, 101, 99, 100)] * 8)
    bias = compute_bias(weekly, _trend_swings("up"), daily, _trend_swings("down"), price=100)
    assert bias.direction == BiasDirection.NO_TRADE


def test_bias_no_trade_when_weekly_uptrend_daily_range_but_price_in_premium():
    weekly = make_candles([(100, 101, 99, 100)] * 8)
    daily = make_candles([(100, 101, 99, 100)] * 8)
    daily_swings = [
        Swing(0, 100, SwingKind.LOW, 1),
        Swing(2, 110, SwingKind.HIGH, 3),
        Swing(4, 95, SwingKind.LOW, 5),
        Swing(6, 120, SwingKind.HIGH, 7),
    ]  # range [95, 120]
    bias = compute_bias(weekly, _trend_swings("up"), daily, daily_swings, price=118)  # premium, not discount
    assert bias.daily_trend == TrendState.RANGE
    assert bias.direction == BiasDirection.NO_TRADE


def test_bias_short_when_weekly_downtrend_daily_range_and_price_in_premium():
    weekly = make_candles([(100, 101, 99, 100)] * 8)
    daily = make_candles([(100, 101, 99, 100)] * 8)
    daily_swings = [
        Swing(0, 100, SwingKind.LOW, 1),
        Swing(2, 110, SwingKind.HIGH, 3),
        Swing(4, 95, SwingKind.LOW, 5),
        Swing(6, 120, SwingKind.HIGH, 7),
    ]  # range [95, 120]
    bias = compute_bias(weekly, _trend_swings("down"), daily, daily_swings, price=118)  # premium
    assert bias.daily_trend == TrendState.RANGE
    assert bias.direction == BiasDirection.SHORT


def test_bias_no_trade_when_weekly_downtrend_daily_range_but_price_in_discount():
    weekly = make_candles([(100, 101, 99, 100)] * 8)
    daily = make_candles([(100, 101, 99, 100)] * 8)
    daily_swings = [
        Swing(0, 100, SwingKind.LOW, 1),
        Swing(2, 110, SwingKind.HIGH, 3),
        Swing(4, 95, SwingKind.LOW, 5),
        Swing(6, 120, SwingKind.HIGH, 7),
    ]  # range [95, 120]
    bias = compute_bias(weekly, _trend_swings("down"), daily, daily_swings, price=97)  # discount, not premium
    assert bias.daily_trend == TrendState.RANGE
    assert bias.direction == BiasDirection.NO_TRADE
