"""Tests for hermes_trading.brokers.base -- interface-only this phase."""
from __future__ import annotations

import pytest

from hermes_trading.brokers.base import BrokerAdapter, OrderResult, Position


def test_broker_adapter_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        BrokerAdapter()  # abstract methods unimplemented


def test_incomplete_subclass_still_abstract():
    class Partial(BrokerAdapter):
        def get_ohlcv(self, symbol, timeframe, limit):
            return []

    with pytest.raises(TypeError):
        Partial()


def test_complete_subclass_can_be_instantiated_and_used():
    class Fake(BrokerAdapter):
        def get_ohlcv(self, symbol, timeframe, limit):
            return []

        def get_balance(self):
            return 1000.0

        def get_positions(self, symbol=None):
            return [Position(symbol="BTC/USDT", side="long", contracts=1.0, entry_price=100.0, unrealized_pnl=5.0)]

        def place_order(self, symbol, side, qty, *, order_type="limit", price=None, stop_loss=None, take_profit=None, leverage=None):
            return OrderResult(order_id="1", symbol=symbol, side=side, qty=qty, price=price, status="open")

    adapter = Fake()
    assert adapter.get_balance() == 1000.0
    assert adapter.get_positions()[0].side == "long"
    result = adapter.place_order("BTC/USDT", "buy", 1.0, price=100.0)
    assert result.status == "open"
