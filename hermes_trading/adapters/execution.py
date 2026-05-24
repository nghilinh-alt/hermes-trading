"""
execution.py — Live order execution adapter for Bybit via ccxt.

Places market entry orders with stop-loss + take-profit set at creation.
Only active when HERMES_TRADING_MODE=live AND HERMES_TRADING_I_ACCEPT_RISK=true.

Safety constraints:
  - One position per asset at a time (checked via fetch_positions)
  - Position size = MAX_POSITION_PCT * USDT wallet balance (default 20%)
  - Leverage scaled 3–15x based on RSI signal strength
  - SL/TP always attached to every order
"""
import os
from datetime import datetime, timezone
from pathlib import Path

import ccxt

# ── Singleton exchange client ─────────────────────────────────────────────────

_exchange: ccxt.Exchange | None = None


def _get_exchange() -> ccxt.Exchange:
    global _exchange
    if _exchange is None:
        api_key = os.getenv("BYBIT_API_KEY", "")
        if not api_key:
            raise ValueError("BYBIT_API_KEY must be set for live trading")

        # RSA auth if key path provided, otherwise fall back to HMAC secret
        key_path = os.getenv("BYBIT_RSA_PRIVATE_KEY_PATH", "")
        if key_path and Path(key_path).exists():
            secret = Path(key_path).read_text()
        else:
            secret = os.getenv("BYBIT_API_SECRET", "")
            if not secret:
                raise ValueError(
                    "Either BYBIT_RSA_PRIVATE_KEY_PATH or BYBIT_API_SECRET must be set"
                )

        _exchange = ccxt.bybit(
            {
                "apiKey": api_key,
                "secret": secret,
                "enableRateLimit": True,
                "options": {"defaultType": "linear"},  # USDT perpetual
            }
        )
    return _exchange


# ── Symbol helpers ────────────────────────────────────────────────────────────


def _to_perp_symbol(asset: str) -> str:
    """BTC/USDT → BTC/USDT:USDT (ccxt linear perp format)."""
    if ":" in asset:
        return asset
    base, quote = asset.split("/")
    return f"{base}/{quote}:{quote}"


# ── Position sizing ───────────────────────────────────────────────────────────


def _fetch_usdt_balance(exchange: ccxt.Exchange) -> float:
    """Return available USDT balance in the unified/contract wallet."""
    balance = exchange.fetch_balance({"type": "contract"})
    usdt = balance.get("USDT", {})
    return float(usdt.get("free", usdt.get("total", 0)) or 0)


def _calc_position_usd(exchange: ccxt.Exchange) -> float:
    """Return position size in USD: MAX_POSITION_PCT × USDT balance."""
    pct = float(os.getenv("MAX_POSITION_PCT", "0.20"))
    balance = _fetch_usdt_balance(exchange)
    if balance <= 0:
        raise RuntimeError("USDT balance is zero — cannot size position")
    return balance * pct


# ── Leverage scaling ──────────────────────────────────────────────────────────


def _calc_leverage(rsi: float | None, threshold: float, direction: str) -> int:
    """
    Scale leverage linearly between MIN_LEVERAGE and MAX_LEVERAGE based on
    how far RSI is from the entry threshold.

    For longs: RSI at threshold=3x, RSI at 0=15x (max conviction at extreme lows).
    For shorts: RSI at threshold=3x, RSI at 100=15x.
    """
    min_lev = int(os.getenv("MIN_LEVERAGE", "3"))
    max_lev = int(os.getenv("MAX_LEVERAGE", "15"))

    if rsi is None:
        return min_lev

    if direction == "long":
        # distance: 0 at threshold, 1 at RSI=0
        distance = max(0.0, (threshold - rsi) / threshold)
    else:
        # distance: 0 at threshold, 1 at RSI=100
        distance = max(0.0, (rsi - threshold) / (100 - threshold))

    leverage = min_lev + (max_lev - min_lev) * distance
    return max(min_lev, min(max_lev, round(leverage)))


# ── Position guard ────────────────────────────────────────────────────────────


def has_open_position(asset: str) -> bool:
    """Returns True if there is already an open position for this asset on Bybit."""
    exchange = _get_exchange()
    symbol = _to_perp_symbol(asset)
    try:
        positions = exchange.fetch_positions([symbol])
        for pos in positions:
            if abs(float(pos.get("contracts", 0) or 0)) > 0:
                return True
    except Exception as e:
        # If we can't confirm, be safe and assume a position exists
        raise RuntimeError(f"Could not fetch positions for {asset}: {e}")
    return False


# ── Order placement ───────────────────────────────────────────────────────────


def place_live_trade(strategy: dict, price_data: dict, entry_detail: dict | None = None) -> dict:
    """
    Place a real market order on Bybit USDT perpetual.

    - Position size = 20% of USDT balance (configurable via MAX_POSITION_PCT)
    - Leverage = 3–15x scaled by RSI signal strength (MIN_LEVERAGE / MAX_LEVERAGE)
    - SL/TP attached at order creation
    - entry_detail: dict from loop._evaluate_entry with indicators_fired + confidence
    Returns a trade record dict matching the schema used by loop.py.
    """
    from hermes_trading.loop import _snapshot_indicators

    exchange = _get_exchange()

    asset      = price_data.get("asset", "BTC/USDT")
    symbol     = _to_perp_symbol(asset)
    # Prefer the resolved direction from evaluation (entry_detail); fall back to strategy config.
    # This correctly handles direction:both where the resolved side is set per-tick.
    ed        = entry_detail or {}
    direction = ed.get("direction") or strategy.get("entry", {}).get("direction", "long")
    if direction not in ("long", "short"):
        direction = "long"   # safety: never send an invalid side to the exchange
    side      = "buy" if direction == "long" else "sell"
    entry_price   = float(price_data.get("price", 0))
    stop_loss_pct = float(strategy.get("stop_loss_pct", 2.0)) / 100

    # Dynamic leverage based on RSI signal strength
    rsi       = price_data.get("rsi_14")
    threshold = float(strategy.get("entry", {}).get("threshold", 30))
    leverage  = _calc_leverage(rsi, threshold, direction)
    exchange.set_leverage(leverage, symbol)

    # Position size = 20% of balance, notional value
    pos_usd = _calc_position_usd(exchange)
    qty     = exchange.amount_to_precision(symbol, pos_usd / entry_price)

    # SL/TP prices — 2:1 reward-to-risk
    if direction == "long":
        sl_price = round(entry_price * (1 - stop_loss_pct), 2)
        tp_price = round(entry_price * (1 + stop_loss_pct * 2), 2)
    else:
        sl_price = round(entry_price * (1 + stop_loss_pct), 2)
        tp_price = round(entry_price * (1 - stop_loss_pct * 2), 2)

    # Place market order with SL/TP attached
    order = exchange.create_order(
        symbol, "market", side, qty,
        params={"stopLoss": str(sl_price), "takeProfit": str(tp_price)},
    )

    fill_price = float(order.get("average") or order.get("price") or entry_price)

    return {
        "ts":               datetime.now(timezone.utc).isoformat(),
        "mode":             "live",
        "asset":            asset,
        "direction":        direction,
        "entry_price":      fill_price,
        "exit_price":       None,     # filled when position closes via reconcile
        "pnl_pct":          None,     # filled when position closes via reconcile
        "order_id":         order.get("id"),
        "qty":              qty,
        "leverage":         leverage,
        "sl_price":         sl_price,
        "tp_price":         tp_price,
        "strategy_version": strategy.get("version", "01"),
        # Full indicator snapshot at entry — used by reflect.py for richer learning
        "indicators_snapshot": _snapshot_indicators(price_data),
        "indicators_fired":    ed.get("indicators_fired", {}),
        "confidence_at_entry": ed.get("confidence"),
    }


# ── Closed PnL polling ────────────────────────────────────────────────────────


def fetch_last_closed_pnl(asset: str) -> dict | None:
    """
    Fetch the most recently closed position P&L from Bybit's closed-pnl endpoint.
    Returns {exit_price, pnl_pct, closed_pnl_usdt} or None if unavailable.
    """
    exchange = _get_exchange()
    symbol_clean = _to_perp_symbol(asset).replace("/", "").replace(":USDT", "")
    try:
        result = exchange.private_get_v5_position_closed_pnl({
            "category": "linear",
            "symbol": symbol_clean,
            "limit": 1,
        })
        items = result.get("result", {}).get("list", [])
        if not items:
            return None
        item = items[0]
        exit_price  = float(item.get("avgExitPrice", 0) or 0)
        entry_price = float(item.get("avgEntryPrice", 0) or 0)
        closed_pnl  = float(item.get("closedPnl", 0) or 0)
        pnl_pct = round((exit_price - entry_price) / entry_price, 6) if entry_price else 0.0
        return {
            "exit_price":       round(exit_price, 4),
            "pnl_pct":          pnl_pct,
            "closed_pnl_usdt":  round(closed_pnl, 4),
        }
    except Exception:
        return None
