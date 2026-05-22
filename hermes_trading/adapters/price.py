"""
Price adapter — fetches OHLCV data via ccxt (free public endpoints).
Override exchange via EXCHANGE_API_KEY / EXCHANGE_API_SECRET in .env.
"""
import os
import asyncio
import ccxt.async_support as ccxt

SCHEMA_VERSION = "price/v1"


class SchemaError(Exception):
    pass


async def fetch(asset: str = "BTC/USDT") -> dict:
    """Return latest OHLCV snapshot for *asset*."""
    exchange_id = os.getenv("EXCHANGE_ID", "binance")
    api_key = os.getenv("EXCHANGE_API_KEY", "")
    api_secret = os.getenv("EXCHANGE_API_SECRET", "")

    config = {}
    if api_key:
        config["apiKey"] = api_key
        config["secret"] = api_secret

    exchange_cls = getattr(ccxt, exchange_id)
    exchange = exchange_cls(config)

    try:
        ohlcv = await exchange.fetch_ohlcv(asset, timeframe="1m", limit=100)
        ticker = await exchange.fetch_ticker(asset)
    finally:
        await exchange.close()

    if not ohlcv or len(ohlcv[0]) != 6:
        raise SchemaError(f"Unexpected OHLCV shape from {exchange_id}")

    closes = [c[4] for c in ohlcv]
    highs  = [c[2] for c in ohlcv]
    lows   = [c[3] for c in ohlcv]

    # RSI-14, EMA-9, Bollinger Bands-20
    rsi   = _rsi(closes, period=14)
    ema_9 = _ema(closes, period=9)
    bb_upper, bb_mid, bb_lower = _bollinger(closes, period=20, num_std=2)

    result = {
        "schema_version": SCHEMA_VERSION,
        "asset": asset,
        "price": ticker["last"],
        "bid": ticker.get("bid"),
        "ask": ticker.get("ask"),
        "volume_24h": ticker.get("quoteVolume"),
        "rsi_14": rsi,
        "ema_9": ema_9,
        "bb_upper": bb_upper,
        "bb_mid": bb_mid,
        "bb_lower": bb_lower,
        "ohlcv_1m": ohlcv[-10:],   # last 10 candles for loop decisions
        "high_24h": ticker.get("high"),
        "low_24h":  ticker.get("low"),
    }
    return result


def _bollinger(closes: list, period: int = 20, num_std: float = 2) -> tuple[float, float, float]:
    if len(closes) < period:
        mid = closes[-1] if closes else 0.0
        return round(mid, 4), round(mid, 4), round(mid, 4)
    window = closes[-period:]
    mid = sum(window) / period
    variance = sum((p - mid) ** 2 for p in window) / period
    std = variance ** 0.5
    return round(mid + num_std * std, 4), round(mid, 4), round(mid - num_std * std, 4)


def _ema(closes: list, period: int = 9) -> float:
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period   # seed with SMA
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 4)


def _rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)
