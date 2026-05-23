"""
Price adapter -- fetches OHLCV data via ccxt (free public endpoints).
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
        timeframe = os.getenv("HERMES_TIMEFRAME", "5m")
        ohlcv = await exchange.fetch_ohlcv(asset, timeframe=timeframe, limit=100)
        ticker = await exchange.fetch_ticker(asset)
    finally:
        await exchange.close()

    if not ohlcv or len(ohlcv[0]) != 6:
        raise SchemaError(f"Unexpected OHLCV shape from {exchange_id}")

    closes  = [c[4] for c in ohlcv]
    highs   = [c[2] for c in ohlcv]
    lows    = [c[3] for c in ohlcv]
    volumes = [c[5] for c in ohlcv]

    rsi    = _rsi(closes, period=14)
    ema_9  = _ema(closes, period=9)
    ema_50 = _ema(closes, period=50)
    bb_upper, bb_mid, bb_lower = _bollinger(closes, period=20, num_std=2)
    macd_line, signal_line, macd_hist = _macd(closes)
    atr_14       = _atr(ohlcv, period=14)
    vwap         = _vwap(ohlcv)
    volume_ratio = _volume_ratio(volumes, period=20)

    return {
        "schema_version": SCHEMA_VERSION,
        "asset": asset,
        "price": ticker["last"],
        "bid": ticker.get("bid"),
        "ask": ticker.get("ask"),
        "volume_24h": ticker.get("quoteVolume"),
        "rsi_14": rsi,
        "ema_9": ema_9,
        "ema_50": ema_50,
        "bb_upper": bb_upper,
        "bb_mid": bb_mid,
        "bb_lower": bb_lower,
        "macd_line": macd_line,
        "macd_signal": signal_line,
        "macd_hist": macd_hist,
        "atr_14": atr_14,
        "vwap": vwap,
        "volume_ratio": volume_ratio,
        "ohlcv_1m": ohlcv[-10:],
        "high_24h": ticker.get("high"),
        "low_24h":  ticker.get("low"),
    }


def _bollinger(closes, period=20, num_std=2):
    if len(closes) < period:
        mid = closes[-1] if closes else 0.0
        return round(mid, 4), round(mid, 4), round(mid, 4)
    window = closes[-period:]
    mid = sum(window) / period
    variance = sum((p - mid) ** 2 for p in window) / period
    std = variance ** 0.5
    return round(mid + num_std * std, 4), round(mid, 4), round(mid - num_std * std, 4)


def _ema(closes, period=9):
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 4)


def _rsi(closes, period=14):
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


def _macd(closes, fast=12, slow=26, signal=9):
    """Returns (macd_line, signal_line, histogram)."""
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0
    ema_fast  = _ema(closes, period=fast)
    ema_slow  = _ema(closes, period=slow)
    macd_line = round(ema_fast - ema_slow, 4)
    macd_series = []
    for i in range(signal, 0, -1):
        window = closes[: len(closes) - i + 1]
        if len(window) >= slow:
            macd_series.append(_ema(window, period=fast) - _ema(window, period=slow))
    macd_series.append(macd_line)
    signal_line = round(_ema(macd_series, period=signal), 4)
    histogram   = round(macd_line - signal_line, 4)
    return macd_line, signal_line, histogram


def _atr(ohlcv, period=14):
    """Average True Range over *period* candles."""
    if len(ohlcv) < period + 1:
        return 0.0
    true_ranges = []
    for i in range(1, len(ohlcv)):
        high       = ohlcv[i][2]
        low        = ohlcv[i][3]
        prev_close = ohlcv[i - 1][4]
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return round(sum(true_ranges[-period:]) / period, 4)


def _vwap(ohlcv):
    """Volume-Weighted Average Price across all candles in the window."""
    total_vol = sum(c[5] for c in ohlcv)
    if total_vol == 0:
        return ohlcv[-1][4] if ohlcv else 0.0
    typical_prices = [((c[2] + c[3] + c[4]) / 3) * c[5] for c in ohlcv]
    return round(sum(typical_prices) / total_vol, 4)


def _volume_ratio(volumes, period=20):
    """Current candle volume divided by rolling average. Above 1.5 indicates a spike."""
    if len(volumes) < period + 1:
        return 1.0
    avg = sum(volumes[-period - 1 : -1]) / period
    if avg == 0:
        return 1.0
    return round(volumes[-1] / avg, 3)
