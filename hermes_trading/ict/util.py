"""
hermes_trading.ict.util -- shared candle type and ATR helper for the ICT package.

Not part of the spec's named modules (types/structure/liquidity/imbalance/bias);
factored out because ATR (spec S:3.3-3.8) is needed by liquidity.py, imbalance.py,
and bias.py alike, and this avoids a cross-import between sibling modules.
"""
from __future__ import annotations

from typing import NamedTuple, Sequence


class Candle(NamedTuple):
    """One OHLCV bar. timestamp is epoch milliseconds (ccxt convention)."""

    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def candles_from_ohlcv(rows: Sequence[Sequence[float]]) -> list[Candle]:
    """Convert ccxt-style [ts, open, high, low, close, volume] rows to Candle."""
    return [Candle(*row) for row in rows]


def true_range(candles: Sequence[Candle], i: int) -> float:
    """True range of candle i. Uses close[i-1] if it exists, else just high-low."""
    c = candles[i]
    if i == 0:
        return c.high - c.low
    prev_close = candles[i - 1].close
    return max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))


def atr(candles: Sequence[Candle], period: int = 14) -> list[float | None]:
    """
    Simple (non-Wilder) Average True Range, one value per candle.

    atr[i] is the mean true range over candles[i-period+1 : i+1] -- strictly
    backward-looking, so atr[i] never depends on candles after i (no lookahead).
    None where fewer than `period` true-range samples are available yet.
    """
    tr = [true_range(candles, i) for i in range(len(candles))]
    out: list[float | None] = [None] * len(candles)
    for i in range(period - 1, len(candles)):
        window = tr[i - period + 1 : i + 1]
        out[i] = sum(window) / period
    return out
