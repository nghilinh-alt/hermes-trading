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

    # RSI-14
    rsi = _rsi(closes, period=14)

    result = {
        "schema_version": SCHEMA_VERSION,
        "asset": asset,
        "price": ticker["last"],
        "bid": ticker.get("bid"),
        "ask": ticker.get("ask"),
        "volume_24h": ticker.get("quoteVolume"),
        "rsi_14": rsi,
        "ohlcv_1m": ohlcv[-10:],   # last 10 candles for loop decisions
        "high_24h": ticker.get("high"),
        "low_24h":  ticker.get("low"),
    }
    return result


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
