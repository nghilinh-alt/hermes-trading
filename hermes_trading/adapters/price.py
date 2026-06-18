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
        ohlcv, ohlcv_1h, ohlcv_4h, ohlcv_daily, ticker = await asyncio.gather(
            exchange.fetch_ohlcv(asset, timeframe=timeframe, limit=100),
            exchange.fetch_ohlcv(asset, timeframe="1h", limit=100),
            exchange.fetch_ohlcv(asset, timeframe="4h", limit=100),
            exchange.fetch_ohlcv(asset, timeframe="1d", limit=50),
            exchange.fetch_ticker(asset),
        )
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

    current_price      = ticker["last"]
    fvg_bull, fvg_bear = _fair_value_gaps(ohlcv_1h)
    ob_bull, ob_bear   = _order_blocks(ohlcv_1h)
    support, resistance = _support_resistance(ohlcv_1h, ohlcv_4h, current_price)

    # ── Daily + 4h trend indicators (session 11, 2026-06-18) ─────────────────
    daily_closes = [c[4] for c in ohlcv_daily]
    ema20_daily  = _ema(daily_closes, period=20)

    closes_4h  = [c[4] for c in ohlcv_4h]
    ema50_4h   = _ema(closes_4h, period=50)

    # ── Candle patterns + trend lines (session 12, 2026-06-18) ───────────────
    patterns = _candlestick_patterns(ohlcv)
    flags    = _flag_pattern(ohlcv)
    tl       = _trend_lines(ohlcv_1h, current_price)

    return {
        "schema_version": SCHEMA_VERSION,
        "asset": asset,
        "price": current_price,
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
        # SMC indicators (1h/4h)
        "fvg_bull_low":    fvg_bull[0] if fvg_bull else None,
        "fvg_bull_high":   fvg_bull[1] if fvg_bull else None,
        "fvg_bear_low":    fvg_bear[0] if fvg_bear else None,
        "fvg_bear_high":   fvg_bear[1] if fvg_bear else None,
        "ob_bull_low":     ob_bull[0] if ob_bull else None,
        "ob_bull_high":    ob_bull[1] if ob_bull else None,
        "ob_bear_low":     ob_bear[0] if ob_bear else None,
        "ob_bear_high":    ob_bear[1] if ob_bear else None,
        "support_1h4h":    support,
        "resistance_1h4h": resistance,
        # Daily + 4h trend EMAs (session 11)
        "ema20_daily": ema20_daily,
        "ema50_4h":    ema50_4h,
        # Candlestick patterns (primary timeframe)
        "candle_bull": patterns["bull"],
        "candle_bear": patterns["bear"],
        # Flag patterns (primary timeframe)
        "bull_flag":   flags["bull_flag"],
        "bear_flag":   flags["bear_flag"],
        # Trend lines (1h swing projection)
        "tl_support":    tl["tl_support"],
        "tl_resistance": tl["tl_resistance"],
        "tl_bull":       tl["tl_bull"],
        "tl_bear":       tl["tl_bear"],
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


def _fair_value_gaps(ohlcv: list) -> tuple:
    """
    Find the most recent bullish and bearish Fair Value Gap in 1h candles.
    Bullish FVG: candle[i-2].high < candle[i].low  (gap above, price may retrace into it)
    Bearish FVG: candle[i-2].low  > candle[i].high (gap below)
    Returns (bull_fvg, bear_fvg) where each is (low, high) of the gap or None.
    """
    if len(ohlcv) < 3:
        return None, None
    bull_fvg = bear_fvg = None
    for i in range(2, len(ohlcv)):
        c1_high = ohlcv[i - 2][2]
        c1_low  = ohlcv[i - 2][3]
        c3_high = ohlcv[i][2]
        c3_low  = ohlcv[i][3]
        if c1_high < c3_low:          # bullish gap
            bull_fvg = (round(c1_high, 4), round(c3_low, 4))
        if c1_low > c3_high:          # bearish gap
            bear_fvg = (round(c3_high, 4), round(c1_low, 4))
    return bull_fvg, bear_fvg


def _order_blocks(ohlcv: list, min_move_pct: float = 0.003) -> tuple:
    """
    Find the most recent bullish and bearish Order Block in 1h candles.
    Bullish OB: last bearish candle whose next candle moves up >= min_move_pct.
    Bearish OB: last bullish candle whose next candle moves down >= min_move_pct.
    Returns (bull_ob, bear_ob) where each is (low, high) of the OB candle or None.
    """
    if len(ohlcv) < 2:
        return None, None
    bull_ob = bear_ob = None
    for i in range(len(ohlcv) - 1):
        c      = ohlcv[i]
        c_next = ohlcv[i + 1]
        c_open, c_high, c_low, c_close = c[1], c[2], c[3], c[4]
        next_open, next_close = c_next[1], c_next[4]
        if next_open == 0:
            continue
        next_move = (next_close - next_open) / next_open
        if c_close < c_open and next_move >= min_move_pct:   # bearish candle before up move
            bull_ob = (round(c_low, 4), round(c_high, 4))
        if c_close > c_open and next_move <= -min_move_pct:  # bullish candle before down move
            bear_ob = (round(c_low, 4), round(c_high, 4))
    return bull_ob, bear_ob


def _support_resistance(ohlcv_1h: list, ohlcv_4h: list, current_price: float, lookback: int = 3) -> tuple:
    """
    Identify nearest support and resistance from swing highs/lows across 1h and 4h candles.
    A swing low is a candle whose low is the lowest within +/- lookback candles.
    Returns (support, resistance) as float prices or None.
    """
    swing_lows: list[float] = []
    swing_highs: list[float] = []

    for ohlcv in (ohlcv_1h, ohlcv_4h):
        if len(ohlcv) < lookback * 2 + 1:
            continue
        for i in range(lookback, len(ohlcv) - lookback):
            low  = ohlcv[i][3]
            high = ohlcv[i][2]
            if all(ohlcv[j][3] >= low  for j in range(i - lookback, i + lookback + 1) if j != i):
                swing_lows.append(low)
            if all(ohlcv[j][2] <= high for j in range(i - lookback, i + lookback + 1) if j != i):
                swing_highs.append(high)

    support    = max((l for l in swing_lows  if l < current_price), default=None)
    resistance = min((h for h in swing_highs if h > current_price), default=None)
    return (round(support, 4) if support else None,
            round(resistance, 4) if resistance else None)


def _candlestick_patterns(ohlcv: list) -> dict:
    """
    Detect reversal patterns on the last 3 candles of the primary timeframe.
    Priority: morning/evening star > engulfing > hammer/shooting_star.
    Returns {"bull": name|None, "bear": name|None}.
    """
    if len(ohlcv) < 3:
        return {"bull": None, "bear": None}

    c1, c2, c3 = ohlcv[-3], ohlcv[-2], ohlcv[-1]

    def body(c):       return abs(c[4] - c[1])
    def upper_wick(c): return c[2] - max(c[1], c[4])
    def lower_wick(c): return min(c[1], c[4]) - c[3]
    def rng(c):        return c[2] - c[3]
    def is_bull(c):    return c[4] > c[1]
    def is_bear(c):    return c[4] < c[1]

    bull = bear = None

    # 3-candle: morning / evening star
    c1_mid = (c1[1] + c1[4]) / 2
    if is_bear(c1) and body(c2) < 0.35 * body(c1) and is_bull(c3) and c3[4] > c1_mid:
        bull = "morning_star"
    if is_bull(c1) and body(c2) < 0.35 * body(c1) and is_bear(c3) and c3[4] < c1_mid:
        bear = "evening_star"

    # 2-candle: engulfing
    if bull is None and is_bear(c2) and is_bull(c3) and c3[1] <= c2[4] and c3[4] >= c2[1]:
        bull = "bullish_engulfing"
    if bear is None and is_bull(c2) and is_bear(c3) and c3[1] >= c2[4] and c3[4] <= c2[1]:
        bear = "bearish_engulfing"

    # 1-candle: hammer / shooting star
    r3, b3, lw3, uw3 = rng(c3), body(c3), lower_wick(c3), upper_wick(c3)
    if r3 > 0:
        if bull is None and lw3 >= 2 * b3 and uw3 <= 0.3 * r3:
            bull = "hammer"
        if bear is None and uw3 >= 2 * b3 and lw3 <= 0.3 * r3:
            bear = "shooting_star"

    return {"bull": bull, "bear": bear}


def _flag_pattern(ohlcv: list, pole_min_pct: float = 0.025, flag_max_pct: float = 0.015) -> dict:
    """
    Bull/bear flag: strong directional move over candles -15..-5 (pole),
    followed by tight consolidation in candles -4..-1 (flag, range < 1.5%).
    Returns {"bull_flag": bool, "bear_flag": bool}.
    """
    if len(ohlcv) < 20:
        return {"bull_flag": False, "bear_flag": False}

    flag_candles = ohlcv[-4:]
    pole_candles = ohlcv[-15:-4]

    flag_high  = max(c[2] for c in flag_candles)
    flag_low   = min(c[3] for c in flag_candles)
    flag_range = (flag_high - flag_low) / flag_low if flag_low > 0 else 1.0

    if flag_range > flag_max_pct:
        return {"bull_flag": False, "bear_flag": False}

    pole_open  = pole_candles[0][1]
    pole_close = pole_candles[-1][4]
    pole_move  = (pole_close - pole_open) / pole_open if pole_open > 0 else 0.0

    return {
        "bull_flag": pole_move >= pole_min_pct,
        "bear_flag": pole_move <= -pole_min_pct,
    }


def _trend_lines(ohlcv_1h: list, current_price: float, lookback: int = 3) -> dict:
    """
    Project trend lines through the last two swing highs and last two swing lows on 1h.
    Fires (tl_bull/tl_bear) when price is within 1% of the projected level.
    Returns tl_support, tl_resistance (float|None) and tl_bull, tl_bear (bool).
    """
    lk = lookback
    if len(ohlcv_1h) < lk * 2 + 5:
        return {"tl_support": None, "tl_resistance": None, "tl_bull": False, "tl_bear": False}

    swing_lows  = []
    swing_highs = []
    for i in range(lk, len(ohlcv_1h) - lk):
        lo = ohlcv_1h[i][3]
        hi = ohlcv_1h[i][2]
        if all(ohlcv_1h[j][3] >= lo for j in range(i - lk, i + lk + 1) if j != i):
            swing_lows.append((i, lo))
        if all(ohlcv_1h[j][2] <= hi for j in range(i - lk, i + lk + 1) if j != i):
            swing_highs.append((i, hi))

    n = len(ohlcv_1h)

    def project(points):
        if len(points) < 2:
            return None
        (i1, p1), (i2, p2) = points[-2], points[-1]
        if i2 == i1:
            return None
        return round(p2 + (p2 - p1) / (i2 - i1) * (n - 1 - i2), 4)

    tl_sup = project(swing_lows)
    tl_res = project(swing_highs)
    tol    = 0.01

    return {
        "tl_support":    tl_sup,
        "tl_resistance": tl_res,
        "tl_bull": tl_sup is not None and abs(current_price - tl_sup) / tl_sup <= tol,
        "tl_bear": tl_res is not None and abs(current_price - tl_res) / tl_res <= tol,
    }
