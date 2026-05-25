"""
execution.py — Live order execution adapter for Bybit via ccxt.

Places market entry orders with stop-loss + take-profit set at creation.
Only active when HERMES_TRADING_MODE=live AND HERMES_TRADING_I_ACCEPT_RISK=true.

Safety constraints:
  - One position per asset at a time (checked via fetch_positions)
  - Position size = risk-based: (balance × risk_per_trade) / sl_dist_pct, capped at MAX_POSITION_USD
  - Leverage = fixed default_leverage (strategy.default_leverage, default 5x)
  - SL = structural support/resistance ± sl_buffer_pct; fallback to fixed stop_loss_pct
  - TP = nearest structural resistance/support; fallback to 2:1 RR from SL
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


# ── Balance & sizing helpers ──────────────────────────────────────────────────


def _fetch_usdt_balance(exchange: ccxt.Exchange) -> float:
    """Return available USDT balance in the unified/contract wallet."""
    balance = exchange.fetch_balance({"type": "contract"})
    usdt = balance.get("USDT", {})
    return float(usdt.get("free", usdt.get("total", 0)) or 0)


# ── Structural SL/TP ──────────────────────────────────────────────────────────


def _structural_sl_tp(
    price_data: dict,
    direction: str,
    strategy: dict,
) -> tuple[float, float]:
    """
    Derive stop-loss and take-profit from structural swing levels.

    Long:
      SL = support_1h4h × (1 - sl_buffer_pct)   (fallback: entry × (1 - stop_loss_pct))
      TP = resistance_1h4h                         (fallback: entry + 2×SL-distance)
    Short:
      SL = resistance_1h4h × (1 + sl_buffer_pct) (fallback: entry × (1 + stop_loss_pct))
      TP = support_1h4h                            (fallback: entry - 2×SL-distance)

    Raises ValueError if the structural SL distance exceeds max_sl_pct.
    """
    entry      = float(price_data.get("price", 0))
    sl_buffer  = float(strategy.get("sl_buffer_pct", 0.3)) / 100
    fallback_sl = float(strategy.get("stop_loss_pct", 2.0)) / 100
    max_sl     = float(strategy.get("max_sl_pct", 5.0)) / 100
    support    = price_data.get("support_1h4h")
    resistance = price_data.get("resistance_1h4h")

    if direction == "long":
        # SL: below structural support with buffer, or fixed fallback
        if support and float(support) < entry:
            sl_price = round(float(support) * (1 - sl_buffer), 4)
        else:
            sl_price = round(entry * (1 - fallback_sl), 4)

        sl_dist = abs(entry - sl_price)

        # TP: structural resistance above entry, or 2:1 RR fallback
        if resistance and float(resistance) > entry:
            tp_price = round(float(resistance), 4)
        else:
            tp_price = round(entry + sl_dist * 2, 4)

    else:  # short
        # SL: above structural resistance with buffer, or fixed fallback
        if resistance and float(resistance) > entry:
            sl_price = round(float(resistance) * (1 + sl_buffer), 4)
        else:
            sl_price = round(entry * (1 + fallback_sl), 4)

        sl_dist = abs(sl_price - entry)

        # TP: structural support below entry, or 2:1 RR fallback
        if support and float(support) < entry:
            tp_price = round(float(support), 4)
        else:
            tp_price = round(entry - sl_dist * 2, 4)

    # Guard: reject if SL is too far from entry
    sl_dist_pct = abs(entry - sl_price) / entry
    if sl_dist_pct > max_sl:
        raise ValueError(
            f"Structural SL too wide: {sl_dist_pct:.2%} > max {max_sl:.2%} "
            f"(entry={entry}, sl={sl_price})"
        )

    return sl_price, tp_price


# ── Risk-based position sizing ────────────────────────────────────────────────


def _risk_based_qty(
    exchange: ccxt.Exchange,
    entry_price: float,
    sl_price: float,
    strategy: dict,
    symbol: str,
) -> float:
    """
    Size the position so that a full SL hit costs exactly risk_per_trade × balance.

    qty_usd = (balance × risk_per_trade) / sl_dist_pct
    Capped at MAX_POSITION_USD env var (default $500).
    """
    risk_per_trade = float(strategy.get("risk_per_trade", 0.10))   # 10% default
    max_pos_usd    = float(os.getenv("MAX_POSITION_USD", "500"))

    balance = _fetch_usdt_balance(exchange)
    if balance <= 0:
        raise RuntimeError("USDT balance is zero — cannot size position")

    sl_dist_pct = abs(entry_price - sl_price) / entry_price
    if sl_dist_pct == 0:
        raise ValueError("SL distance is zero — cannot size position")

    risk_usd = balance * risk_per_trade
    qty_usd  = min(risk_usd / sl_dist_pct, max_pos_usd)
    return exchange.amount_to_precision(symbol, qty_usd / entry_price)


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

    - SL/TP: structural swing levels (support_1h4h / resistance_1h4h) with sl_buffer_pct.
             Falls back to fixed stop_loss_pct when no structural level is available.
             Raises ValueError if structural SL > max_sl_pct — caller should skip entry.
    - Position size: risk-based — (balance × risk_per_trade) / sl_dist_pct, capped at MAX_POSITION_USD.
    - Leverage: fixed default_leverage (default 5x).
    - entry_detail: dict from loop._evaluate_entry with indicators_fired + confidence + direction.

    Returns a trade record dict matching the schema used by loop.py.
    Raises ValueError if structural SL is too wide (caller should catch and skip).
    """
    from hermes_trading.loop import _snapshot_indicators

    exchange = _get_exchange()

    asset     = price_data.get("asset", "BTC/USDT")
    symbol    = _to_perp_symbol(asset)

    # Prefer the resolved direction from evaluation (entry_detail); fall back to strategy config.
    # This correctly handles direction:both where the resolved side is set per-tick.
    ed        = entry_detail or {}
    direction = ed.get("direction") or strategy.get("entry", {}).get("direction", "long")
    if direction not in ("long", "short"):
        direction = "long"   # safety: never send an invalid side to the exchange
    side      = "buy" if direction == "long" else "sell"

    entry_price = float(price_data.get("price", 0))

    # ── Structural SL/TP (may raise ValueError if SL too wide) ───────────────
    sl_price, tp_price = _structural_sl_tp(price_data, direction, strategy)

    # ── Fixed leverage (no longer RSI-scaled) ─────────────────────────────────
    leverage = int(strategy.get("default_leverage", 5))
    exchange.set_leverage(leverage, symbol)

    # ── Risk-based position size ───────────────────────────────────────────────
    qty = _risk_based_qty(exchange, entry_price, sl_price, strategy, symbol)

    # ── R:R ratio for logging / dashboard ─────────────────────────────────────
    sl_dist = abs(entry_price - sl_price)
    tp_dist = abs(tp_price - entry_price)
    rr_ratio = round(tp_dist / sl_dist, 2) if sl_dist > 0 else None

    # ── Place market order with SL/TP attached ─────────────────────────────────
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
        "exit_price":       None,      # filled when position closes via reconcile
        "pnl_pct":          None,      # filled when position closes via reconcile
        "order_id":         order.get("id"),
        "qty":              qty,
        "leverage":         leverage,
        "sl_price":         sl_price,
        "tp_price":         tp_price,
        "rr_ratio":         rr_ratio,
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
