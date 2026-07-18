"""Tests for hermes_trading.ict.util -- Candle, atr()."""
from __future__ import annotations

from hermes_trading.ict.util import atr
from tests.ict.helpers import make_candles


def test_atr_hand_computed():
    # c0: TR = h-l = 10-0 = 10 (no prior close)
    # c1: TR = max(12-8=4, |12-5|=7, |8-5|=3) = 7
    # c2: TR = max(11-9=2, |11-10|=1, |9-10|=1) = 2
    candles = make_candles([
        (5, 10, 0, 5),
        (5, 12, 8, 10),
        (10, 11, 9, 10),
    ])
    result = atr(candles, period=2)
    assert result[0] is None
    assert result[1] == 8.5   # mean(10, 7)
    assert result[2] == 4.5   # mean(7, 2)


def test_atr_none_until_period_reached():
    candles = make_candles([(100, 101, 99, 100)] * 5)
    result = atr(candles, period=14)
    assert all(v is None for v in result)


def test_atr_never_uses_future_candles():
    """No-lookahead: atr(candles[:k])[i] == atr(candles)[i] for all i < k."""
    candles = make_candles([(100 + i % 3, 102 + i % 3, 98 - i % 2, 100 + (i % 3) - 1) for i in range(20)])
    full = atr(candles, period=4)
    for k in range(4, len(candles) + 1):
        prefix = atr(candles[:k], period=4)
        assert prefix == full[:k]
