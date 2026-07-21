"""
hermes_trading.brokers.bybit -- concrete Bybit USDT-perp BrokerAdapter.

Auth, idempotent-leverage, and order-placement patterns are lifted
directly from the archived (but proven-in-production for months)
hermes_trading.adapters.execution -- this is a clean rewrite behind the
BrokerAdapter interface, not a fork of that module's RSI-strategy-specific
sizing/guard logic (position sizing for ICT lives in hermes_trading.ict.risk).

Real network calls only happen via the injected/constructed ccxt exchange
client -- tests inject a mock in place of `exchange` and never touch a
real account.
"""
from __future__ import annotations

import os
from pathlib import Path

import ccxt

from hermes_trading.brokers.base import BrokerAdapter, OrderResult, Position


class OrderTooSmallError(Exception):
    """Computed order quantity is below the exchange's minimum for the symbol."""


def _build_exchange() -> ccxt.Exchange:
    api_key = os.getenv("BYBIT_API_KEY", "")
    if not api_key:
        raise ValueError("BYBIT_API_KEY must be set for live trading")

    key_path = os.getenv("BYBIT_RSA_PRIVATE_KEY_PATH", "")
    if key_path and Path(key_path).exists():
        secret = Path(key_path).read_text()
    else:
        secret = os.getenv("BYBIT_API_SECRET", "")
        if not secret:
            raise ValueError("Either BYBIT_RSA_PRIVATE_KEY_PATH or BYBIT_API_SECRET must be set")

    return ccxt.bybit({
        "apiKey": api_key,
        "secret": secret,
        "enableRateLimit": True,
        "options": {"defaultType": "linear"},  # USDT perpetual
    })


def _to_perp_symbol(asset: str) -> str:
    """BTC/USDT -> BTC/USDT:USDT (ccxt linear perp format)."""
    if ":" in asset:
        return asset
    base, quote = asset.split("/")
    return f"{base}/{quote}:{quote}"


def _symbol_clean(perp_symbol: str) -> str:
    """BTC/USDT:USDT -> BTCUSDT, for Bybit's raw v5 endpoints."""
    return perp_symbol.replace("/", "").replace(":USDT", "")


class BybitBroker(BrokerAdapter):
    """Bybit USDT linear-perpetual adapter. One authenticated ccxt client per instance."""

    def __init__(self, *, exchange: ccxt.Exchange | None = None):
        self._exchange = exchange if exchange is not None else _build_exchange()
        self._exchange.load_markets()

    # ── BrokerAdapter interface ───────────────────────────────────────────

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        return self._exchange.fetch_ohlcv(_to_perp_symbol(symbol), timeframe=timeframe, limit=limit)

    def get_balance(self) -> float:
        """
        Account equity in USDT -- `total` (wallet balance, INCLUDING margin
        currently locked in open positions), NOT `free`.

        This preferred `free` until session 21c, which was a real bug with
        real consequences. Callers use this value for two things -- position
        sizing and the circuit breaker -- and both want equity, not
        available margin:

          * The circuit breaker computes
            (equity - equity_at_day_start) / equity_at_day_start. With
            `free`, merely OPENING a position looks identical to losing the
            margin it locked. Observed live on 2026-07-21: a manual order
            locked $403.99 of a $808.03 account, `free` read $404.04, and
            the breaker tripped at -49.99% against its -20% limit having
            lost nothing at all. Worse, this is self-inflicted at scale --
            position_size can take notional up to equity x lev_max, so the
            worker opening ONE of its own A+ trades can collapse `free` and
            stand the whole account down for the rest of the UTC day.
          * Sizing should likewise be a function of account equity, not of
            whatever margin happens to be unencumbered this second.

        Note `total` is wallet balance and may exclude unrealised PnL on
        open positions, so the breaker measures REALISED drawdown. That
        suits this strategy (exchange-native stops mean a loss realises when
        it stops out, and the spec frames the limits as "-20% daily = 1
        loss") but it does mean an open, deeply-underwater position won't
        trip it before the stop does.
        """
        balance = self._exchange.fetch_balance({"type": "contract"})
        usdt = balance.get("USDT", {})
        # Explicit None check rather than `usdt.get("total", usdt.get("free"))`:
        # a present-but-null `total` would satisfy .get() and then collapse to
        # 0.0 through `or 0`, reporting zero equity. That isn't merely wrong,
        # it's silently disabling -- zero equity reads as a -100% day and
        # stands the whole account down.
        for key in ("total", "free"):
            value = usdt.get(key)
            if value is not None:
                return float(value)
        return 0.0

    def get_positions(self, symbol: str | None = None) -> list[Position]:
        symbols = [_to_perp_symbol(symbol)] if symbol else None
        rows = self._exchange.fetch_positions(symbols)
        result = []
        for pos in rows:
            contracts = abs(float(pos.get("contracts", 0) or 0))
            if contracts == 0:
                continue
            result.append(Position(
                symbol=pos.get("symbol", ""),
                side=(pos.get("side") or "").lower(),
                contracts=contracts,
                entry_price=float(pos.get("entryPrice") or 0),
                unrealized_pnl=float(pos.get("unrealizedPnl") or 0),
            ))
        return result

    def place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        *,
        order_type: str = "limit",
        price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        leverage: float | None = None,
    ) -> OrderResult:
        perp_symbol = _to_perp_symbol(symbol)

        if leverage is not None:
            self.set_leverage(symbol, int(leverage))

        qty = float(self._exchange.amount_to_precision(perp_symbol, qty))
        min_qty = self._exchange.markets.get(perp_symbol, {}).get("limits", {}).get("amount", {}).get("min")
        if min_qty is not None and qty < float(min_qty):
            raise OrderTooSmallError(f"qty {qty} below exchange minimum {min_qty} for {perp_symbol}")

        if price is not None:
            price = float(self._exchange.price_to_precision(perp_symbol, price))

        params = {}
        if stop_loss is not None:
            params["stopLoss"] = str(stop_loss)
        if take_profit is not None:
            params["takeProfit"] = str(take_profit)

        order = self._exchange.create_order(perp_symbol, order_type, side, qty, price, params=params)
        return OrderResult(
            order_id=str(order.get("id")),
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            status=order.get("status") or "open",
        )

    # ── Extras beyond the ABC, needed for live position management ───────

    def has_open_position(self, symbol: str) -> bool:
        return len(self.get_positions(symbol)) > 0

    def set_leverage(self, symbol: str, leverage: int) -> None:
        """Idempotent: Bybit's "leverage not modified" (code 110043) is not an error."""
        perp_symbol = _to_perp_symbol(symbol)
        try:
            self._exchange.set_leverage(leverage, perp_symbol)
        except ccxt.AuthenticationError:
            pass
        except ccxt.ExchangeError as e:
            msg = str(e)
            if "110043" not in msg and "not modified" not in msg.lower():
                raise

    def set_margin_mode_isolated(self, symbol: str) -> None:
        """Idempotent: 'already in that margin mode' is not an error."""
        perp_symbol = _to_perp_symbol(symbol)
        try:
            self._exchange.set_margin_mode("isolated", perp_symbol)
        except ccxt.ExchangeError as e:
            if "not modified" not in str(e).lower():
                raise

    def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        perp_symbol = _to_perp_symbol(symbol) if symbol else None
        return self._exchange.fetch_open_orders(perp_symbol)

    def cancel_order(self, order_id: str, symbol: str) -> bool:
        perp_symbol = _to_perp_symbol(symbol)
        try:
            self._exchange.cancel_order(order_id, perp_symbol)
        except ccxt.OrderNotFound:
            pass  # already filled or cancelled -- desired end state either way
        return True

    def reduce_position(self, symbol: str, side: str, qty: float) -> OrderResult:
        """
        Reduce-only market order for a partial close. `side` is the CLOSING
        side (sell to reduce a long, buy to reduce a short) -- the caller
        (hermes_trading.ict.live) is responsible for picking it correctly.
        """
        perp_symbol = _to_perp_symbol(symbol)
        qty = float(self._exchange.amount_to_precision(perp_symbol, qty))
        order = self._exchange.create_order(perp_symbol, "market", side, qty, params={"reduceOnly": True})
        return OrderResult(
            order_id=str(order.get("id")),
            symbol=symbol,
            side=side,
            qty=qty,
            price=float(order.get("average") or order.get("price") or 0) or None,
            status=order.get("status") or "closed",
        )

    def update_trailing_stop(self, symbol: str, direction: str, new_sl: float) -> bool:
        """
        Amend the stop-loss on an already-open position via Bybit's v5
        trading-stop endpoint. `direction` is accepted (matching the
        archived execution.py's signature this is lifted from) but not
        itself validated here -- the caller must ensure new_sl only
        tightens (higher for longs, lower for shorts); enforcing that is
        hermes_trading.ict.live's responsibility (and its own tests'), not
        this thin adapter's.
        """
        perp_symbol = _to_perp_symbol(symbol)
        self._exchange.private_post_v5_position_trading_stop({
            "category": "linear",
            "symbol": _symbol_clean(perp_symbol),
            "stopLoss": str(round(new_sl, 8)),
            "tpslMode": "Full",
            "slTriggerBy": "MarkPrice",
        })
        return True

    def set_position_protection(self, symbol: str, *, stop_loss: float | None = None, take_profit: float | None = None) -> bool:
        """
        Set (or re-set) stopLoss/takeProfit on an already-open position via
        the same v5 trading-stop endpoint update_trailing_stop uses.
        Called defensively right after a resting limit order transitions
        to a filled position, to guarantee protection is in place even if
        Bybit's native stopLoss/takeProfit-on-a-resting-limit-order attach
        didn't carry through to the fill.
        """
        perp_symbol = _to_perp_symbol(symbol)
        params = {"category": "linear", "symbol": _symbol_clean(perp_symbol), "tpslMode": "Full"}
        if stop_loss is not None:
            params["stopLoss"] = str(round(stop_loss, 8))
            params["slTriggerBy"] = "MarkPrice"
        if take_profit is not None:
            params["takeProfit"] = str(round(take_profit, 8))
            params["tpTriggerBy"] = "MarkPrice"
        self._exchange.private_post_v5_position_trading_stop(params)
        return True

    def fetch_recent_closed_trades(self, symbol: str, limit: int = 5) -> list[dict]:
        """Most recent closed positions from Bybit's v5 closed-pnl endpoint, newest first."""
        perp_symbol = _to_perp_symbol(symbol)
        result = self._exchange.private_get_v5_position_closed_pnl({
            "category": "linear",
            "symbol": _symbol_clean(perp_symbol),
            "limit": min(limit, 100),
        })
        items = result.get("result", {}).get("list", [])
        trades = []
        for item in items:
            closed_pnl = float(item.get("closedPnl", 0) or 0)
            cum_entry = float(item.get("cumEntryValue", 0) or 0)
            exit_price = float(item.get("avgExitPrice", 0) or 0)
            entry_price = float(item.get("avgEntryPrice", 0) or 0)
            qty = float(item.get("qty", 0) or 0)
            side = (item.get("side") or "").lower()
            direction = "long" if side == "buy" else ("short" if side == "sell" else None)
            created_ms = int(item.get("createdTime", 0) or 0)
            closed_ms = int(item.get("updatedTime", created_ms) or created_ms)
            trades.append({
                "order_id": item.get("orderId"),
                "symbol": symbol,
                "direction": direction,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "qty": qty,
                "closed_pnl_usd": closed_pnl,
                "cum_entry_value": cum_entry,
                "created_ms": created_ms,
                "closed_ms": closed_ms,
            })
        return trades
