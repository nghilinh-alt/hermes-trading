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

Phase 2.1 structural guards (sessions 6+):
  - max_sl_pct       — skip trade if SL is too far from entry  (existing)
  - min_tp_pct       — skip trade if structural TP is too close to entry (Option B filter)
  - min_rr_ratio     — soft: if R:R below threshold, extend TP to hit it (do not skip)
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
    raw_bal = usdt.get("free", usdt.get("total", 0)) or 0

    # DEBUG: Log actual balance for troubleshooting
    import sys
    print(f"DEBUG [_fetch_usdt_balance]: Exchange={exchange.id}, Balance=${raw_bal:.2f}, Symbol={balance.get('info', {})}", file=sys.stderr)

    return float(raw_bal)


# ── Structural SL/TP ──────────────────────────────────────────────────────────


def _structural_sl_tp(
    price_data: dict,
    direction: str,
    strategy: dict,
) -> tuple[float, float]:
    """
    Derive stop-loss and take-profit from structural swing levels with three guards.

    Long:
      SL = support_1h4h × (1 - sl_buffer_pct)   (fallback: entry × (1 - stop_loss_pct))
      TP = resistance_1h4h                         (fallback: entry + 2×SL-distance)
    Short:
      SL = resistance_1h4h × (1 + sl_buffer_pct) (fallback: entry × (1 + stop_loss_pct))
      TP = support_1h4h                            (fallback: entry - 2×SL-distance)

    Guards (in order):
      1. max_sl_pct        — raise ValueError if SL distance from entry exceeds threshold (default 5%)
      2. min_tp_pct        — raise ValueError if structural TP distance from entry is below threshold
                             (default 3%). Implements "Option B" target-return filter.
      3. min_rr_ratio      — soft guard: if structural R:R is below threshold (default 2.0), extend
                             TP to (entry ± sl_dist × min_rr_ratio). Does NOT skip the trade.
    """
    entry        = float(price_data.get("price", 0))
    sl_buffer    = float(strategy.get("sl_buffer_pct", 0.3)) / 100
    fallback_sl  = float(strategy.get("stop_loss_pct", 2.0)) / 100
    max_sl       = float(strategy.get("max_sl_pct", 5.0)) / 100
    min_tp_pct   = float(strategy.get("min_tp_pct", 3.0)) / 100
    min_rr_ratio = float(strategy.get("min_rr_ratio", 2.0))
    support      = price_data.get("support_1h4h")
    resistance   = price_data.get("resistance_1h4h")

    if entry <= 0:
        raise ValueError(f"Invalid entry price: {entry}")

    if direction == "long":
        if support and float(support) < entry:
            sl_price = round(float(support) * (1 - sl_buffer), 4)
        else:
            sl_price = round(entry * (1 - fallback_sl), 4)
        sl_dist = abs(entry - sl_price)
        if resistance and float(resistance) > entry:
            tp_price = round(float(resistance), 4)
        else:
            tp_price = round(entry + sl_dist * 2, 4)
    else:  # short
        if resistance and float(resistance) > entry:
            sl_price = round(float(resistance) * (1 + sl_buffer), 4)
        else:
            sl_price = round(entry * (1 + fallback_sl), 4)
        sl_dist = abs(sl_price - entry)
        if support and float(support) < entry:
            tp_price = round(float(support), 4)
        else:
            tp_price = round(entry - sl_dist * 2, 4)

    # Guard 1: SL not too wide
    sl_dist_pct = abs(entry - sl_price) / entry
    if sl_dist_pct > max_sl:
        raise ValueError(
            f"Structural SL too wide: {sl_dist_pct:.2%} > max {max_sl:.2%} "
            f"(entry={entry}, sl={sl_price})"
        )

    # Guard 2 (Option B filter): TP must be at least min_tp_pct from entry
    tp_dist_pct = abs(tp_price - entry) / entry
    if tp_dist_pct < min_tp_pct:
        raise ValueError(
            f"Structural TP too thin: {tp_dist_pct:.2%} < min {min_tp_pct:.2%} "
            f"(entry={entry}, tp={tp_price}) — Option B target-return filter"
        )

    # Guard 3 (Soft R:R): extend TP if R:R below threshold
    if sl_dist > 0:
        rr_ratio = abs(tp_price - entry) / sl_dist
        if rr_ratio < min_rr_ratio:
            if direction == "long":
                tp_price = round(entry + sl_dist * min_rr_ratio, 4)
            else:
                tp_price = round(entry - sl_dist * min_rr_ratio, 4)

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
    risk_per_trade = float(strategy.get("risk_per_trade", 0.10))
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


# ── Minimum-profit guard ─────────────────────────────────────────────────────


def _guard_min_profit_usd(qty, entry_price: float, tp_price: float, strategy: dict) -> None:
    """
    Raise ValueError if the expected profit at TP is below min_profit_usd.

    Complements the percentage-based min_tp_pct and ratio-based min_rr_ratio guards
    by enforcing an absolute dollar floor. Triggers when notional is too small —
    typically due to low USDT balance or the MAX_POSITION_USD cap forcing a tiny
    position even though the structural setup itself is valid.

    Default $5/trade floor per Linh's directive (2026-05-28).
    """
    min_profit = float(strategy.get("min_profit_usd", 5.0))
    expected_profit = float(qty) * abs(float(tp_price) - float(entry_price))
    if expected_profit < min_profit:
        raise ValueError(
            f"Expected TP profit too small: ${expected_profit:.2f} < min ${min_profit:.2f} "
            f"(qty={qty}, entry={entry_price}, tp={tp_price})"
        )


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
        raise RuntimeError(f"Could not fetch positions for {asset}: {e}")
    return False


# ── Order placement ───────────────────────────────────────────────────────────


def place_live_trade(strategy: dict, price_data: dict, entry_detail: dict | None = None) -> dict:
    """
    Place a real market order on Bybit USDT perpetual.

    - SL/TP: structural swing levels with max_sl_pct / min_tp_pct / min_rr_ratio guards.
    - Position size: risk-based, capped at MAX_POSITION_USD.
    - $-floor: skipped if expected profit at TP < min_profit_usd (default $5).
    - Leverage: fixed default_leverage. Idempotent against Bybit retCode 110043.
    - entry_detail: dict from loop._evaluate_entry with indicators_fired + confidence + direction.

    Returns a trade record dict matching loop.py schema.
    Raises ValueError if any structural guard fails (caller catches and skips).
    """
    from hermes_trading.loop import _snapshot_indicators, _entry_gates_snapshot

    exchange = _get_exchange()

    asset     = price_data.get("asset", "BTC/USDT")
    symbol    = _to_perp_symbol(asset)

    ed        = entry_detail or {}
    direction = ed.get("direction") or strategy.get("entry", {}).get("direction", "long")
    if direction not in ("long", "short"):
        direction = "long"
    side      = "buy" if direction == "long" else "sell"

    entry_price = float(price_data.get("price", 0))

    sl_price, tp_price = _structural_sl_tp(price_data, direction, strategy)

    leverage_val = strategy.get("default_leverage")
    leverage = int(leverage_val or 5)
    if exchange.id != "bybit" or leverage_val:
        try:
            exchange.set_leverage(leverage, symbol)
        except ccxt.AuthenticationError:
            pass
        except ccxt.ExchangeError as e:
            msg = str(e)
            if "110043" not in msg and "not modified" not in msg.lower():
                raise

    qty = _risk_based_qty(exchange, entry_price, sl_price, strategy, symbol)

    # Guard 4 ($-floor): expected TP profit must clear min_profit_usd
    _guard_min_profit_usd(qty, entry_price, tp_price, strategy)

    sl_dist = abs(entry_price - sl_price)
    tp_dist = abs(tp_price - entry_price)
    rr_ratio = round(tp_dist / sl_dist, 2) if sl_dist > 0 else None

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
        "exit_price":       None,
        "pnl_pct":          None,
        "order_id":         order.get("id"),
        "qty":              qty,
        "leverage":         leverage,
        "sl_price":         sl_price,
        "tp_price":         tp_price,
        "rr_ratio":         rr_ratio,
        "strategy_version": strategy.get("version", "01"),
        "indicators_snapshot":  _snapshot_indicators(price_data),
        "indicators_fired":     ed.get("indicators_fired", {}),
        "confidence_at_entry":  ed.get("confidence"),
        # Audit additions (session 6, 2026-05-28): WHY the trade was opened
        "confidence_breakdown": ed.get("confidence_breakdown", {}),
        "evaluation_summary":   ed.get("evaluation_summary", ""),
        "entry_gates":          _entry_gates_snapshot(strategy),
        # close_reason is set later by _reconcile_open_trades when the position closes
        "close_reason":         None,
    }


# ── Closed PnL polling ────────────────────────────────────────────────────────


def fetch_last_closed_pnl(asset: str) -> dict | None:
    """
    Fetch the most recently closed position P&L from Bybit's closed-pnl endpoint.
    Returns {exit_price, pnl_pct, closed_pnl_usdt, direction, qty} or None.

    pnl_pct is computed from closedPnl / cumEntryValue, which is direction-correct
    for both long and short (positive for wins, negative for losses regardless of side).
    The naive (exit-entry)/entry formula was wrong for shorts — fixed Phase 2.5.
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
        cum_entry   = float(item.get("cumEntryValue", 0) or 0)
        qty         = float(item.get("qty", 0) or 0)
        side        = (item.get("side") or "").lower()
        direction   = "long" if side == "buy" else ("short" if side == "sell" else None)

        # Direction-correct pnl_pct: closedPnl is signed (negative for losses),
        # cumEntryValue is always positive (entry notional in USDT).
        if cum_entry > 0:
            pnl_pct = round(closed_pnl / cum_entry, 6)
        elif entry_price > 0 and direction:
            # Fallback if cumEntryValue absent: compute from prices, direction-aware
            move = (exit_price - entry_price) / entry_price
            pnl_pct = round(move if direction == "long" else -move, 6)
        else:
            pnl_pct = 0.0

        return {
            "exit_price":       round(exit_price, 4),
            "pnl_pct":          pnl_pct,
            "closed_pnl_usdt":  round(closed_pnl, 4),
            "direction":        direction,
            "qty":              qty,
        }
    except Exception:
        return None


# ── Recent closed trades (Phase 2.3) ─────────────────────────────────────────


def fetch_recent_closed_trades(asset: str, limit: int = 50) -> list[dict]:
    """
    Fetch the most recent closed positions from Bybit and return as trade-like dicts.

    Used by dashboard.py to merge live Bybit data with local trades.jsonl so the
    history table is always complete even when the cron backfill hasn't run yet.

    Each returned dict has:
      order_id, asset, direction, entry_price, exit_price, pnl_pct,
      closed_pnl_usdt, qty, ts (close time ISO string), strategy_version='bybit_live'.
    Returns [] on error.
    """
    exchange     = _get_exchange()
    symbol_clean = _to_perp_symbol(asset).replace("/", "").replace(":USDT", "")
    try:
        result = exchange.private_get_v5_position_closed_pnl({
            "category": "linear",
            "symbol":   symbol_clean,
            "limit":    min(limit, 100),
        })
        items = result.get("result", {}).get("list", [])
    except Exception:
        return []

    trades = []
    for item in items:
        closed_pnl  = float(item.get("closedPnl",     0) or 0)
        cum_entry   = float(item.get("cumEntryValue",  0) or 0)
        exit_price  = float(item.get("avgExitPrice",   0) or 0)
        entry_price = float(item.get("avgEntryPrice",  0) or 0)
        qty         = float(item.get("qty",            0) or 0)
        side        = (item.get("side") or "").lower()
        direction   = "long" if side == "buy" else ("short" if side == "sell" else None)
        created_ms  = int(item.get("createdTime", 0) or 0)
        ts = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).isoformat() if created_ms else None

        if cum_entry > 0:
            pnl_pct = round(closed_pnl / cum_entry, 6)
        elif entry_price > 0 and exit_price > 0 and direction:
            move = (exit_price - entry_price) / entry_price
            pnl_pct = round(move if direction == "long" else -move, 6)
        else:
            pnl_pct = 0.0

        trades.append({
            "order_id":         item.get("orderId"),
            "asset":            asset,
            "direction":        direction,
            "entry_price":      round(entry_price, 4),
            "exit_price":       round(exit_price,  4),
            "pnl_pct":          pnl_pct,
            "closed_pnl_usdt":  round(closed_pnl,  4),
            "qty":              qty,
            "ts":               ts,
            "strategy_version": "bybit_live",
        })
    return trades

# ── Trailing stop ─────────────────────────────────────────────────────────────


def fetch_open_positions_with_marks(asset: str) -> list[dict]:
    """Return open positions for asset with current mark price.

    Each item: {symbol, direction, entry_price, mark_price, sl_price, tp_price, qty, contracts}
    Returns [] if no open position or on error.
    """
    exchange = _get_exchange()
    symbol   = _to_perp_symbol(asset)
    try:
        positions = exchange.fetch_positions([symbol])
    except Exception as e:
        raise RuntimeError(f"fetch_open_positions_with_marks failed for {asset}: {e}")

    result = []
    for pos in positions:
        contracts = abs(float(pos.get("contracts", 0) or 0))
        if contracts == 0:
            continue
        side      = (pos.get("side") or "").lower()
        direction = "long" if side == "long" else "short"
        info      = pos.get("info", {})
        result.append({
            "symbol":      symbol,
            "direction":   direction,
            "entry_price": float(pos.get("entryPrice") or info.get("avgPrice") or 0),
            "mark_price":  float(pos.get("markPrice")  or info.get("markPrice") or 0),
            "sl_price":    float(info.get("stopLoss")   or 0) or None,
            "tp_price":    float(info.get("takeProfit") or 0) or None,
            "qty":         float(pos.get("contracts")   or 0),
        })
    return result


def update_trailing_stop(asset: str, direction: str, new_sl: float) -> bool:
    """Move the stop-loss on an open position to new_sl via Bybit trading-stop API.

    Only call this after confirming new_sl improves on the current SL
    (higher for longs, lower for shorts). Returns True on success.
    """
    exchange     = _get_exchange()
    symbol       = _to_perp_symbol(asset)
    symbol_clean = symbol.replace("/", "").replace(":USDT", "")
    try:
        exchange.private_post_v5_position_trading_stop({
            "category":      "linear",
            "symbol":        symbol_clean,
            "stopLoss":      str(round(new_sl, 4)),
            "tpslMode":      "Full",
            "slTriggerBy":   "MarkPrice",
        })
        return True
    except Exception as e:
        raise RuntimeError(f"update_trailing_stop failed for {asset}: {e}")