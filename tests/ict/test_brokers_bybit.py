"""
Tests for hermes_trading.brokers.bybit -- mocked ccxt only, no real
account/network access. Follows the same "explicit Fake, not MagicMock"
style already used in tests/ict/test_brokers_base.py.
"""
from __future__ import annotations

import ccxt
import pytest

from hermes_trading.brokers.base import OrderResult, Position
from hermes_trading.brokers.bybit import BybitBroker, OrderTooSmallError


class FakeExchange:
    def __init__(self):
        self.markets = {"BTC/USDT:USDT": {"limits": {"amount": {"min": 0.001}}}}
        self.rateLimit = 50
        self.id = "bybit"

        self.balance_response = {"USDT": {"free": 1000.0, "total": 1200.0}}
        self.positions_response: list[dict] = []
        self.open_orders_response: list[dict] = []
        self.ohlcv_response: list[list[float]] = []
        self.closed_pnl_response = {"result": {"list": []}}
        self.create_order_response = {"id": "order123", "status": "open"}

        self.leverage_error: Exception | None = None
        self.margin_mode_error: Exception | None = None
        self.cancel_order_error: Exception | None = None

        self.create_order_calls: list[dict] = []
        self.leverage_calls: list[tuple] = []
        self.margin_mode_calls: list[tuple] = []
        self.cancel_order_calls: list[tuple] = []
        self.trading_stop_calls: list[dict] = []
        self.closed_pnl_calls: list[dict] = []

    def load_markets(self):
        pass

    def fetch_ohlcv(self, symbol, timeframe=None, since=None, limit=None):
        return self.ohlcv_response

    def fetch_balance(self, params=None):
        return self.balance_response

    def fetch_positions(self, symbols=None):
        return self.positions_response

    def amount_to_precision(self, symbol, amount):
        return round(float(amount), 6)

    def price_to_precision(self, symbol, price):
        return round(float(price), 4)

    def create_order(self, symbol, order_type, side, qty, price=None, params=None):
        self.create_order_calls.append({"symbol": symbol, "type": order_type, "side": side,
                                         "qty": qty, "price": price, "params": params or {}})
        return self.create_order_response

    def set_leverage(self, leverage, symbol):
        if self.leverage_error is not None:
            raise self.leverage_error
        self.leverage_calls.append((leverage, symbol))

    def set_margin_mode(self, mode, symbol):
        if self.margin_mode_error is not None:
            raise self.margin_mode_error
        self.margin_mode_calls.append((mode, symbol))

    def fetch_open_orders(self, symbol=None):
        return self.open_orders_response

    def cancel_order(self, order_id, symbol):
        if self.cancel_order_error is not None:
            raise self.cancel_order_error
        self.cancel_order_calls.append((order_id, symbol))

    def private_post_v5_position_trading_stop(self, params):
        self.trading_stop_calls.append(params)
        return {}

    def private_get_v5_position_closed_pnl(self, params):
        self.closed_pnl_calls.append(params)
        return self.closed_pnl_response


@pytest.fixture
def fake():
    return FakeExchange()


@pytest.fixture
def broker(fake):
    return BybitBroker(exchange=fake)


def test_get_balance_returns_total_equity_not_free_margin(broker):
    """
    Fixture is {"free": 1000.0, "total": 1200.0} -- must return 1200.

    Regression cover for a live incident (2026-07-21): returning `free`
    meant locked margin was indistinguishable from a loss, and the circuit
    breaker tripped at -49.99% on an account that had lost nothing.
    """
    assert broker.get_balance() == 1200.0


def test_get_balance_falls_back_to_free_when_total_absent(fake, broker):
    fake.balance_response = {"USDT": {"free": 500.0}}
    assert broker.get_balance() == 500.0


def test_get_balance_locked_margin_does_not_reduce_reported_equity(fake, broker):
    """The exact live shape: half the account committed as margin, nothing lost."""
    fake.balance_response = {"USDT": {"free": 404.04, "used": 403.99, "total": 808.03}}
    assert broker.get_balance() == 808.03


def test_get_positions_maps_and_filters_zero_contracts(fake, broker):
    fake.positions_response = [
        {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.5, "entryPrice": 100.0, "unrealizedPnl": 5.0},
        {"symbol": "ETH/USDT:USDT", "side": "short", "contracts": 0.0, "entryPrice": 0.0, "unrealizedPnl": 0.0},
    ]
    positions = broker.get_positions()
    assert positions == [Position(symbol="BTC/USDT:USDT", side="long", contracts=0.5, entry_price=100.0, unrealized_pnl=5.0)]


def test_has_open_position_true_and_false(fake, broker):
    assert broker.has_open_position("BTC/USDT") is False
    fake.positions_response = [{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 1.0, "entryPrice": 100.0, "unrealizedPnl": 0.0}]
    assert broker.has_open_position("BTC/USDT") is True


def test_place_order_limit_attaches_sl_tp_params(fake, broker):
    result = broker.place_order("BTC/USDT", "buy", 0.01, order_type="limit", price=100.0,
                                 stop_loss=95.0, take_profit=110.0, leverage=5)
    assert result == OrderResult(order_id="order123", symbol="BTC/USDT", side="buy", qty=0.01, price=100.0, status="open")
    call = fake.create_order_calls[0]
    assert call["symbol"] == "BTC/USDT:USDT"
    assert call["type"] == "limit"
    assert call["side"] == "buy"
    assert call["params"] == {"stopLoss": "95.0", "takeProfit": "110.0"}
    assert fake.leverage_calls == [(5, "BTC/USDT:USDT")]


def test_place_order_below_minimum_qty_raises(broker):
    with pytest.raises(OrderTooSmallError):
        broker.place_order("BTC/USDT", "buy", 0.0001, order_type="limit", price=100.0)


def test_place_order_without_leverage_does_not_call_set_leverage(fake, broker):
    broker.place_order("BTC/USDT", "sell", 0.01, order_type="market")
    assert fake.leverage_calls == []


def test_set_leverage_idempotent_swallows_110043(fake, broker):
    fake.leverage_error = ccxt.ExchangeError("bybit {\"retCode\":110043,\"retMsg\":\"leverage not modified\"}")
    broker.set_leverage("BTC/USDT", 5)  # must not raise


def test_set_leverage_swallows_authentication_error(fake, broker):
    fake.leverage_error = ccxt.AuthenticationError("bad key")
    broker.set_leverage("BTC/USDT", 5)  # must not raise


def test_set_leverage_reraises_other_exchange_errors(fake, broker):
    fake.leverage_error = ccxt.ExchangeError("some other real failure")
    with pytest.raises(ccxt.ExchangeError):
        broker.set_leverage("BTC/USDT", 5)


def test_set_margin_mode_isolated_idempotent(fake, broker):
    fake.margin_mode_error = ccxt.ExchangeError("margin mode not modified")
    broker.set_margin_mode_isolated("BTC/USDT")  # must not raise
    fake.margin_mode_error = ccxt.ExchangeError("some other real failure")
    with pytest.raises(ccxt.ExchangeError):
        broker.set_margin_mode_isolated("BTC/USDT")


def test_update_trailing_stop_call_shape(fake, broker):
    ok = broker.update_trailing_stop("BTC/USDT", "bullish", 101.2345)
    assert ok is True
    call = fake.trading_stop_calls[0]
    assert call["category"] == "linear"
    assert call["symbol"] == "BTCUSDT"
    assert call["stopLoss"] == "101.2345"
    assert call["tpslMode"] == "Full"


def test_set_position_protection_sets_both_sl_and_tp(fake, broker):
    broker.set_position_protection("BTC/USDT", stop_loss=95.0, take_profit=110.0)
    call = fake.trading_stop_calls[0]
    assert call["stopLoss"] == "95.0"
    assert call["takeProfit"] == "110.0"


def test_set_position_protection_sl_only(fake, broker):
    broker.set_position_protection("BTC/USDT", stop_loss=95.0)
    call = fake.trading_stop_calls[0]
    assert call["stopLoss"] == "95.0"
    assert "takeProfit" not in call


def test_get_open_orders_passes_through(fake, broker):
    fake.open_orders_response = [{"id": "abc"}]
    assert broker.get_open_orders("BTC/USDT") == [{"id": "abc"}]


def test_cancel_order_swallows_order_not_found(fake, broker):
    fake.cancel_order_error = ccxt.OrderNotFound("already filled")
    assert broker.cancel_order("abc", "BTC/USDT") is True


def test_cancel_order_success(fake, broker):
    assert broker.cancel_order("abc", "BTC/USDT") is True
    assert fake.cancel_order_calls == [("abc", "BTC/USDT:USDT")]


def test_reduce_position_uses_reduce_only_param(fake, broker):
    fake.create_order_response = {"id": "order999", "status": "closed", "average": 101.5}
    result = broker.reduce_position("BTC/USDT", "sell", 0.005)
    call = fake.create_order_calls[0]
    assert call["type"] == "market"
    assert call["params"] == {"reduceOnly": True}
    assert result == OrderResult(order_id="order999", symbol="BTC/USDT", side="sell", qty=0.005, price=101.5, status="closed")


def test_fetch_recent_closed_trades_parses_items(fake, broker):
    fake.closed_pnl_response = {"result": {"list": [{
        "orderId": "xyz", "closedPnl": "12.5", "cumEntryValue": "100", "avgExitPrice": "110",
        "avgEntryPrice": "100", "qty": "1", "side": "Buy", "createdTime": "1700000000000",
        "updatedTime": "1700000600000",
    }]}}
    trades = broker.fetch_recent_closed_trades("BTC/USDT", limit=5)
    assert len(trades) == 1
    t = trades[0]
    assert t["direction"] == "long"
    assert t["closed_pnl_usd"] == 12.5
    assert t["entry_price"] == 100.0
    assert t["exit_price"] == 110.0
    call = fake.closed_pnl_calls[0]
    assert call["symbol"] == "BTCUSDT"
    assert call["limit"] == 5
