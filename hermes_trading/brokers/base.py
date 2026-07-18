"""
hermes_trading.brokers.base -- BrokerAdapter interface.

This is the futures-ready seam: strategy/detection code (hermes_trading.ict)
never talks to an exchange directly, only through this interface, so a CME
adapter (ES/NQ/YM/Gold/Oil) can be added later with zero changes to the
strategy code. Interface only this phase -- no concrete implementation
(Bybit or otherwise), no network calls, no order placement. See
ict-claude-code-prompt.md S:2 and ict-strategy-plan-2026-07-18.md S:13.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class Position:
    symbol: str
    side: str  # "long" | "short"
    contracts: float
    entry_price: float
    unrealized_pnl: float


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    symbol: str
    side: str  # "buy" | "sell"
    qty: float
    price: float | None  # None for market orders
    status: str


class BrokerAdapter(ABC):
    """Abstract broker interface. One concrete subclass per venue."""

    @abstractmethod
    def get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        """Return [timestamp, open, high, low, close, volume] rows, oldest first."""
        raise NotImplementedError

    @abstractmethod
    def get_balance(self) -> float:
        """Return available account balance in quote currency."""
        raise NotImplementedError

    @abstractmethod
    def get_positions(self, symbol: str | None = None) -> list[Position]:
        """Return open positions, optionally filtered to one symbol."""
        raise NotImplementedError

    @abstractmethod
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
        """Place an order. Not implemented by any adapter this phase."""
        raise NotImplementedError
