"""
execution.py — Live order execution adapter for Bybit via ccxt.

Places market entry orders with stop-loss + take-profit set at creation.
Only active when HERMES_TRADING_MODE=live AND HERMES_TRADING_I_ACCEPT_RISK=true.

Safety constraints:
  - One position per asset at a time (checked via fetch_positions)
  - Position size capped by MAX_POSITION_USD env var (default $100)
  - SL/TP always attached to every order
"""
import os
from datetime import datetime, timezone

import ccxt

# ── Singleton exchange client ─────────────────────────────────────────────────

_exchange: ccxt.Exchange | None = None


def _get_exchange() -> ccxt.Exchange:
    global _exchange
    if _exchange is None:
        api_key = os.getenv("BYBIT_API_KEY", "")
        api_secret = os.getenv("BYBIT_API_SECRET", "")
        if not api_key or not api_secret:
            raise ValueError(
                "BYBIT_API_KEY and BYBIT_API_SECRET must be set for live trading"
            )
        _exchange = ccxt.bybit(
            {
                "apiKey": api_key,
                "secret": api_secret,
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


def place_live_trade(strategy: dict, price_data: dict) -> dict:
    """
    Place a real market order on Bybit USDT perpetual.

    Attaches stop-loss and take-profit at order creation.
    Returns a trade record dict matching the schema used by loop.py.
    """
    exchange = _get_exchange()

    asset = price_data.get("asset", "BTC/USDT")
    symbol = _to_perp_symbol(asset)
    direction = strategy.get("entry", {}).get("direction", "long")
    side = "buy" if direction == "long" else "sell"

    entry_price = float(price_data.get("price", 0))
    stop_loss_pct = float(strategy.get("stop_loss_pct", 2.0)) / 100
    max_pos_usd = float(os.getenv("MAX_POSITION_USD", "100"))

    # Quantity in base currency (e.g. BTC), rounded to exchange precision
    qty = exchange.amount_to_precision(symbol, max_pos_usd / entry_price)

    # SL/TP prices — 2:1 reward-to-risk
    if direction == "long":
        sl_price = round(entry_price * (1 - stop_loss_pct), 2)
        tp_price = round(entry_price * (1 + stop_loss_pct * 2), 2)
    else:
        sl_price = round(entry_price * (1 + stop_loss_pct), 2)
        tp_price = round(entry_price * (1 - stop_loss_pct * 2), 2)

    # Place market order with SL/TP attached
    order = exchange.create_order(
        symbol,
        "market",
        side,
        qty,
        params={
            "stopLoss": str(sl_price),
            "takeProfit": str(tp_price),
        },
    )

    fill_price = float(order.get("average") or order.get("price") or entry_price)

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "live",
        "asset": asset,
        "direction": direction,
        "entry_price": fill_price,
        "exit_price": None,       # filled when position closes
        "pnl_pct": None,          # filled when position closes
        "order_id": order.get("id"),
        "qty": qty,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "strategy_version": strategy.get("version", "01"),
        "rsi_at_entry": price_data.get("rsi_14"),
        "ema_9_at_entry": price_data.get("ema_9"),
        "bb_lower_at_entry": price_data.get("bb_lower"),
        "bb_upper_at_entry": price_data.get("bb_upper"),
    }


# ── Closed PnL polling ────────────────────────────────────────────────────────


def fetch_recent_closed_pnl(asset: str, limit: int = 10) -> list[dict]:
    """
    Fetch recently closed positions from Bybit for reconciliation.
    Returns a list of dicts with keys: ts, asset, pnl_pct, exit_price.
    """
    exchange = _get_exchange()
    symbol = _to_perp_symbol(asset)
    try:
        raw = exchange.fetch_closed_orders(symbol, limit=limit)
        results = []
        for o in raw:
            if o.get("status") == "closed":
                results.append(
                    {
                        "ts": datetime.fromtimestamp(
                            o["timestamp"] / 1000, tz=timezone.utc
                        ).isoformat(),
                        "asset": asset,
                        "exit_price": o.get("average"),
                        "order_id": o.get("id"),
                    }
                )
        return results
    except Exception:
        return []
