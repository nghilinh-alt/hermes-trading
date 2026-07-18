"""Shared candle-building helpers for ICT detector tests. Not a test module itself."""
from __future__ import annotations

from hermes_trading.ict.util import Candle

HOUR_MS = 3_600_000


def make_candles(
    ohlc: list[tuple[float, float, float, float]],
    *,
    start_ts: int = 0,
    step_ms: int = HOUR_MS,
    volume: float | list[float] = 100.0,
) -> list[Candle]:
    """Build a Candle list from (open, high, low, close) tuples. Timestamps are synthetic but deterministic."""
    volumes = volume if isinstance(volume, list) else [volume] * len(ohlc)
    return [
        Candle(timestamp=start_ts + i * step_ms, open=o, high=h, low=l, close=c, volume=v)
        for i, ((o, h, l, c), v) in enumerate(zip(ohlc, volumes))
    ]


def flat_candles(n: int, price: float = 100.0, *, high_offset: float = 1.0, low_offset: float = 1.0, volume: float = 100.0) -> list[Candle]:
    """n identical small-range candles centered on `price` -- a no-signal/ranging fixture."""
    return make_candles([(price, price + high_offset, price - low_offset, price)] * n, volume=volume)
