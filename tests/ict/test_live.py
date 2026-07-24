"""
Tests for hermes_trading.ict.live -- the real-money trading state machine.

Two data sources are used deliberately:
  (1) Synthetic candles/swings (tests/ict/helpers.py, direct Swing()
      construction) for replay_management_bars and the differential test
      against _manage_position -- both take exec_swings as an explicit
      parameter, so the exact fixtures already proven correct for
      _manage_position in tests/ict/test_backtest.py can be reused
      directly without needing real detection.
  (2) Real BTC history (data/ict-backtest/BTC_USDT.csv, gitignored, see
      tests/ict/fixtures/README.md) for the state-machine tests that
      route through scan_asset/locate_pending_setup, which -- per
      Phase 1/2's own established precedent -- reliably contain
      qualifying setups where synthetic fixtures don't survive the full
      gate stack. Skips gracefully if the CSV isn't present.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_trading.brokers.base import BrokerAdapter, OrderResult, Position
from hermes_trading.ict.backtest import _manage_position
from hermes_trading.ict.live import (
    AssetStateStore,
    ManagementAction,
    replay_management_bars,
    run_asset_cycle,
    run_full_cycle,
)
from hermes_trading.ict.types import Direction, Grade, SwingKind
from tests.ict.helpers import make_candles
from tests.ict.test_scanner import BTC_CSV, CALIBRATED, TWO_YEARS_OF_15M_BARS, _find_a_real_qualified_and_filled_mss, _load_csv

pytestmark_real_data = pytest.mark.skipif(not BTC_CSV.exists(), reason="real historical BTC data not fetched in this environment")


# ── replay_management_bars -- synthetic, mirrors test_backtest.py's own _manage_position fixtures ──


def test_replay_no_action_when_nothing_crosses():
    exec_full = make_candles([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 99, 100)])
    actions = replay_management_bars(exec_full, [], Direction.BULLISH, 100.0, 96.0, fill_index=0,
                                      partial_taken=False, current_stop=96.0, start_index=1, end_index=2)
    assert actions == []


def test_replay_partial_take_and_breakeven_bullish():
    entry, stop = 100.0, 96.0  # risk=4, partial level (2R) = 108
    exec_full = make_candles([
        (100, 101, 99, 100),  # 0: fill
        (101, 109, 100, 108),  # 1: crosses partial level 108
        (108, 110, 107, 109),  # 2: nothing further
    ])
    actions = replay_management_bars(exec_full, [], Direction.BULLISH, entry, stop, fill_index=0,
                                      partial_taken=False, current_stop=stop, start_index=1, end_index=2)
    assert actions == [
        ManagementAction("partial_take", 1, 108.0),
        ManagementAction("trail_stop", 1, 100.0),  # breakeven = entry
    ]


def test_replay_trails_after_partial_bullish():
    entry, stop = 100.0, 96.0
    higher_low = SwingKind.LOW
    from hermes_trading.ict.types import Swing
    swing = Swing(index=2, price=103.0, kind=higher_low, confirmed_index=2)
    exec_full = make_candles([
        (100, 101, 99, 100),   # 0: fill
        (101, 109, 100, 108),  # 1: partial at 108, stop -> BE (100)
        (108, 110, 103, 109),  # 2: higher low forms at 103, confirmed same bar
        (109, 110, 101, 102),  # 3: nothing further to replay here
    ])
    actions = replay_management_bars(exec_full, [swing], Direction.BULLISH, entry, stop, fill_index=0,
                                      partial_taken=False, current_stop=stop, start_index=1, end_index=3)
    assert actions == [
        ManagementAction("partial_take", 1, 108.0),
        ManagementAction("trail_stop", 1, 100.0),
        ManagementAction("trail_stop", 2, 103.0),
    ]


def test_replay_bearish_partial_and_trail():
    from hermes_trading.ict.types import Swing
    entry, stop = 100.0, 104.0  # risk=4, partial level (2R) = 92
    lower_high = Swing(index=2, price=97.0, kind=SwingKind.HIGH, confirmed_index=2)
    exec_full = make_candles([
        (100, 101, 99, 100),  # 0: fill (short)
        (99, 100, 91, 92),    # 1: partial at 92, stop -> BE (100)
        (92, 97, 90, 91),     # 2: lower high forms at 97
        (91, 98, 90, 96),     # 3: nothing further to replay here
    ])
    actions = replay_management_bars(exec_full, [lower_high], Direction.BEARISH, entry, stop, fill_index=0,
                                      partial_taken=False, current_stop=stop, start_index=1, end_index=3)
    assert actions == [
        ManagementAction("partial_take", 1, 92.0),
        ManagementAction("trail_stop", 1, 100.0),
        ManagementAction("trail_stop", 2, 97.0),
    ]


def test_replay_never_loosens_stop():
    """A later swing that's WORSE than the current stop must not move the stop backward."""
    from hermes_trading.ict.types import Swing
    entry, stop = 100.0, 96.0
    worse_low = Swing(index=2, price=98.0, kind=SwingKind.LOW, confirmed_index=2)  # below current stop 100 after BE
    exec_full = make_candles([
        (100, 101, 99, 100),   # 0: fill
        (101, 109, 100, 108),  # 1: partial at 108, stop -> BE (100)
        (108, 110, 97, 105),   # 2: a lower (worse) low forms at 98 -- must NOT loosen stop below 100
    ])
    actions = replay_management_bars(exec_full, [worse_low], Direction.BULLISH, entry, stop, fill_index=0,
                                      partial_taken=False, current_stop=stop, start_index=1, end_index=2)
    kinds_and_prices = [(a.kind, a.price) for a in actions]
    assert ("trail_stop", 98.0) not in kinds_and_prices
    assert kinds_and_prices == [("partial_take", 108.0), ("trail_stop", 100.0)]


def test_replay_respects_bounded_range():
    """A partial-crossing bar OUTSIDE [start_index, end_index] must not fire."""
    entry, stop = 100.0, 96.0
    exec_full = make_candles([
        (100, 101, 99, 100),   # 0: fill
        (101, 109, 100, 108),  # 1: WOULD cross partial level 108 -- but excluded from the range below
        (105, 106, 104, 105),  # 2: start of the replay range -- stays well under the partial level
    ])
    actions = replay_management_bars(exec_full, [], Direction.BULLISH, entry, stop, fill_index=0,
                                      partial_taken=False, current_stop=stop, start_index=2, end_index=2)
    assert actions == []


def test_replay_resumes_with_partial_already_taken():
    """A cycle resuming after a restart with partial_taken=True must not re-fire it."""
    entry, stop = 100.0, 96.0
    exec_full = make_candles([
        (100, 101, 99, 100),   # 0: fill
        (101, 109, 100, 108),  # 1: (already processed in a prior cycle)
        (108, 130, 107, 109),  # 2: crosses partial level again -- must NOT re-trigger
    ])
    actions = replay_management_bars(exec_full, [], Direction.BULLISH, entry, stop, fill_index=0,
                                      partial_taken=True, current_stop=100.0, start_index=2, end_index=2)
    assert all(a.kind != "partial_take" for a in actions)


# ── Differential test against _manage_position (hermes_trading.ict.backtest) ──


def test_replay_management_bars_matches_manage_position_reference():
    """
    Runs the exact same fixtures test_backtest.py uses for _manage_position
    through both functions and asserts agreement on the OBSERVABLE overlap:
    _manage_position's close_reason/exit_price for the partial+trail path
    is exactly what replay_management_bars' action sequence would produce
    up to that same bar. This is the regression net against the two
    implementations drifting apart -- see the live-worker deployment plan.
    """
    from hermes_trading.ict.types import Swing

    # Case 1: stop hit before any partial -- replay must report zero actions.
    exec_full = make_candles([(105, 106, 104, 105), (105, 105.5, 99, 100), (100, 100.5, 95, 96)])
    trade = _manage_position(exec_full, [], fill_index=0, asset="TEST", direction=Direction.BULLISH,
                              grade=Grade.A_PLUS, entry=105.0, initial_stop=96.0, target=130.0, qty=10.0, fee_pct=0.0)
    assert trade.close_reason == "stop"
    actions = replay_management_bars(exec_full, [], Direction.BULLISH, 105.0, 96.0, fill_index=0,
                                      partial_taken=False, current_stop=96.0, start_index=1, end_index=trade.exit_index)
    assert actions == []

    # Case 2: partial then target -- replay must show partial+breakeven by the exit bar.
    entry, stop = 100.0, 96.0
    exec_full = make_candles([(100, 101, 99, 100), (101, 109, 100, 108), (108, 121, 107, 120)])
    trade = _manage_position(exec_full, [], fill_index=0, asset="TEST", direction=Direction.BULLISH,
                              grade=Grade.A_PLUS, entry=entry, initial_stop=stop, target=120.0, qty=10.0, fee_pct=0.0)
    assert trade.close_reason == "target"
    actions = replay_management_bars(exec_full, [], Direction.BULLISH, entry, stop, fill_index=0,
                                      partial_taken=False, current_stop=stop, start_index=1, end_index=trade.exit_index)
    assert [a.kind for a in actions] == ["partial_take", "trail_stop"]
    assert actions[1].price == entry  # breakeven

    # Case 3: partial then trail-stop close -- replay's final trail price must equal _manage_position's exit_price.
    higher_low = Swing(index=2, price=103.0, kind=SwingKind.LOW, confirmed_index=2)
    exec_full = make_candles([
        (100, 101, 99, 100), (101, 109, 100, 108), (108, 110, 103, 109), (109, 110, 101, 102),
    ])
    trade = _manage_position(exec_full, [higher_low], fill_index=0, asset="TEST", direction=Direction.BULLISH,
                              grade=Grade.A_PLUS, entry=entry, initial_stop=stop, target=200.0, qty=10.0, fee_pct=0.0)
    assert trade.close_reason == "trail_stop"
    actions = replay_management_bars(exec_full, [higher_low], Direction.BULLISH, entry, stop, fill_index=0,
                                      partial_taken=False, current_stop=stop, start_index=1, end_index=trade.exit_index - 1)
    trail_actions = [a for a in actions if a.kind == "trail_stop"]
    assert trail_actions[-1].price == trade.exit_price == 103.0

    # Case 4 (bearish mirror of case 3).
    lower_high = Swing(index=2, price=97.0, kind=SwingKind.HIGH, confirmed_index=2)
    entry_s, stop_s = 100.0, 104.0
    exec_full = make_candles([
        (100, 101, 99, 100), (99, 100, 91, 92), (92, 97, 90, 91), (91, 98, 90, 96),
    ])
    trade = _manage_position(exec_full, [lower_high], fill_index=0, asset="TEST", direction=Direction.BEARISH,
                              grade=Grade.A_PLUS, entry=entry_s, initial_stop=stop_s, target=20.0, qty=10.0, fee_pct=0.0)
    assert trade.close_reason == "trail_stop"
    actions = replay_management_bars(exec_full, [lower_high], Direction.BEARISH, entry_s, stop_s, fill_index=0,
                                      partial_taken=False, current_stop=stop_s, start_index=1, end_index=trade.exit_index - 1)
    trail_actions = [a for a in actions if a.kind == "trail_stop"]
    assert trail_actions[-1].price == trade.exit_price == 97.0


# ── AssetStateStore ────────────────────────────────────────────────────────


def test_asset_state_store_position_round_trip(tmp_path):
    store = AssetStateStore(tmp_path / "BTC_USDT")
    assert store.load_position() == {"status": "flat"}
    store.save_position({"status": "open_position", "entry_price": 100.0})
    assert store.load_position() == {"status": "open_position", "entry_price": 100.0}


def test_asset_state_store_attempted_round_trip(tmp_path):
    store = AssetStateStore(tmp_path / "BTC_USDT")
    assert store.load_attempted() == set()
    store.save_attempted({1700000000000, 1700000900000})
    assert store.load_attempted() == {1700000000000, 1700000900000}


def test_asset_state_store_circuit_breaker_round_trip(tmp_path):
    store = AssetStateStore(tmp_path / "BTC_USDT")
    default = store.load_circuit_breaker()
    assert default["equity_at_day_start"] is None
    store.save_circuit_breaker({"day_bucket_start_ms": 1, "equity_at_day_start": 1000.0,
                                 "week_bucket_start_ms": 1, "equity_at_week_start": 1000.0})
    reloaded = store.load_circuit_breaker()
    assert reloaded["equity_at_day_start"] == 1000.0


def test_asset_state_store_append_trade(tmp_path):
    store = AssetStateStore(tmp_path / "BTC_USDT")
    store.append_trade({"pnl_usd": 10.0})
    store.append_trade({"pnl_usd": -5.0})
    lines = (tmp_path / "BTC_USDT" / "trades.jsonl").read_text().strip().splitlines()
    assert [json.loads(line)["pnl_usd"] for line in lines] == [10.0, -5.0]


def test_asset_state_store_needs_review_flag(tmp_path):
    store = AssetStateStore(tmp_path / "BTC_USDT")
    assert store.is_needs_review() is False
    store.flag_needs_review("broker shows an unexplained position")
    assert store.is_needs_review() is True
    assert "unexplained" in store.needs_review_flag_path.read_text()


# ── FakeBroker for state-machine tests ──────────────────────────────────────


class FakeBroker(BrokerAdapter):
    def __init__(self):
        self.balance = 1000.0
        self.positions: dict[str, Position] = {}
        self.open_orders: dict[str, dict] = {}
        self.closed_trades: dict[str, list[dict]] = {}
        self.order_registry: dict[str, dict] = {}  # link_id -> {order_id, status} for find_order_by_link_id
        self.place_order_calls = []
        self.reduce_position_calls = []
        self.update_trailing_stop_calls = []
        self.set_position_protection_calls = []
        self.cancel_order_calls = []
        self._next_order_id = 1

    def get_ohlcv(self, symbol, timeframe, limit):
        return []

    def get_balance(self) -> float:
        return self.balance

    def get_positions(self, symbol: str | None = None) -> list[Position]:
        if symbol:
            pos = self.positions.get(symbol)
            return [pos] if pos else []
        return list(self.positions.values())

    def place_order(self, symbol, side, qty, *, order_type="limit", price=None,
                     stop_loss=None, take_profit=None, leverage=None, order_link_id=None) -> OrderResult:
        order_id = f"order{self._next_order_id}"
        self._next_order_id += 1
        self.place_order_calls.append({
            "symbol": symbol, "side": side, "qty": qty, "order_type": order_type,
            "price": price, "stop_loss": stop_loss, "take_profit": take_profit, "leverage": leverage,
            "order_link_id": order_link_id,
        })
        self.open_orders[symbol] = {"id": order_id, "symbol": symbol, "side": side, "qty": qty,
                                    "price": price, "clientOrderId": order_link_id,
                                    "info": {"orderLinkId": order_link_id}}
        if order_link_id is not None:
            self.order_registry[order_link_id] = {"order_id": order_id, "status": "new"}
        return OrderResult(order_id=order_id, symbol=symbol, side=side, qty=qty, price=price, status="open")

    def find_order_by_link_id(self, symbol: str, link_id: str) -> dict | None:
        entry = self.order_registry.get(link_id)
        if entry is None:
            return None
        return {"order_id": entry["order_id"], "status": entry["status"], "link_id": link_id}

    # Extras beyond the ABC that hermes_trading.ict.live calls:

    def has_open_position(self, symbol: str) -> bool:
        return symbol in self.positions

    def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        if symbol:
            o = self.open_orders.get(symbol)
            return [o] if o else []
        return list(self.open_orders.values())

    def cancel_order(self, order_id, symbol) -> bool:
        self.cancel_order_calls.append((order_id, symbol))
        self.open_orders.pop(symbol, None)
        return True

    def reduce_position(self, symbol, side, qty) -> OrderResult:
        self.reduce_position_calls.append({"symbol": symbol, "side": side, "qty": qty})
        return OrderResult(order_id=f"reduce{self._next_order_id}", symbol=symbol, side=side, qty=qty, price=None, status="closed")

    def update_trailing_stop(self, symbol, direction, new_sl) -> bool:
        self.update_trailing_stop_calls.append({"symbol": symbol, "direction": direction, "new_sl": new_sl})
        return True

    def set_position_protection(self, symbol, *, stop_loss=None, take_profit=None) -> bool:
        self.set_position_protection_calls.append({"symbol": symbol, "stop_loss": stop_loss, "take_profit": take_profit})
        return True

    def fetch_recent_closed_trades(self, symbol, limit=5) -> list[dict]:
        return self.closed_trades.get(symbol, [])

    # Test helpers simulating exchange-side state changes:

    def simulate_fill(self, symbol, side, entry_price, contracts):
        self.open_orders.pop(symbol, None)
        pos_side = "long" if side == "buy" else "short"
        self.positions[symbol] = Position(symbol=symbol, side=pos_side, contracts=contracts,
                                           entry_price=entry_price, unrealized_pnl=0.0)

    def simulate_native_close(self, symbol):
        self.positions.pop(symbol, None)


# ── State-machine tests using synthetic candles (no scan_asset involved) ───


def test_circuit_breaker_blocks_new_entry_without_marking_attempted(tmp_path):
    from hermes_trading.ict.backtest import _bucket_start

    broker = FakeBroker()
    broker.balance = 750.0  # -25% vs a $1000 day-start equity -- past the -20% daily limit
    store = AssetStateStore(tmp_path / "BTC_USDT")
    now_ms = 1_000_000_000_000
    # Bucket starts must match now_ms's own day/week, or _update_circuit_breaker_bucket
    # treats this as a fresh bucket and resets equity_at_day_start to the CURRENT
    # (already-losing) balance instead of preserving the seeded baseline.
    store.save_circuit_breaker({"day_bucket_start_ms": _bucket_start(now_ms, "1d"), "equity_at_day_start": 1000.0,
                                 "week_bucket_start_ms": _bucket_start(now_ms, "1w"), "equity_at_week_start": 1000.0})
    candles = make_candles([(100, 101, 99, 100)], start_ts=now_ms)

    result = run_asset_cycle("BTC/USDT", broker, candles, store, busy_count=0)

    assert result.status == "flat"
    assert "circuit breaker" in result.action
    assert broker.place_order_calls == []
    assert store.load_attempted() == set()


def test_max_concurrent_blocks_on_another_assets_position(tmp_path):
    broker = FakeBroker()
    store = AssetStateStore(tmp_path / "ETH_USDT")
    result = run_asset_cycle("ETH/USDT", broker, [], store, busy_count=1, max_concurrent=1)
    assert result.status == "flat"
    assert "max_concurrent" in result.action
    assert broker.place_order_calls == []


def test_max_concurrent_blocks_on_another_assets_resting_order(tmp_path):
    """busy_count counts resting orders too, not just filled positions -- see live-worker plan."""
    broker = FakeBroker()
    store = AssetStateStore(tmp_path / "ETH_USDT")
    # busy_count=1 here represents ANOTHER asset's resting order -- the caller
    # (run_full_cycle) is what actually sums positions+open_orders account-wide.
    result = run_asset_cycle("ETH/USDT", broker, [], store, busy_count=1, max_concurrent=1)
    assert result.status == "flat"
    assert "max_concurrent" in result.action


def test_run_full_cycle_busy_count_includes_resting_orders_across_assets(tmp_path):
    """BTC has a resting order (no filled position anywhere) -- ETH's own
    cycle must still see busy_count=1 and refuse to look for a new entry,
    proving run_full_cycle sums positions+open_orders across the traded
    asset set before any per-asset cycle runs, not just per-asset in
    isolation."""
    broker = FakeBroker()
    broker.open_orders["BTC/USDT"] = {"id": "order1"}
    candles = {"ETH/USDT": make_candles([(100, 101, 99, 100)], start_ts=1_000_000_000_000)}

    results = run_full_cycle(["BTC/USDT", "ETH/USDT"], broker, tmp_path, candles, max_concurrent=1)

    eth_result = next(r for r in results if r.asset == "ETH/USDT")
    assert eth_result.status == "flat"
    assert "max_concurrent" in eth_result.action
    assert broker.place_order_calls == []


def test_busy_count_ignores_positions_outside_the_traded_asset_set(tmp_path):
    """
    A manual position/order on a symbol this worker doesn't trade must NOT
    consume a slot. Session 21c: busy_count was account-wide, so a manual
    limit order Linh placed silently saturated max_concurrent and stopped
    the worker trading entirely, surfacing only as a generic
    "max_concurrent reached" line.
    """
    broker = FakeBroker()
    broker.open_orders["XRP/USDT"] = {"id": "manual1"}          # not in the traded set
    broker.positions["DOGE/USDT"] = Position(symbol="DOGE/USDT", side="long", contracts=5.0,
                                             entry_price=0.4, unrealized_pnl=0.0)
    candles = {"ETH/USDT": make_candles([(100, 101, 99, 100)], start_ts=1_000_000_000_000)}

    results = run_full_cycle(["BTC/USDT", "ETH/USDT"], broker, tmp_path, candles, max_concurrent=1)

    eth_result = next(r for r in results if r.asset == "ETH/USDT")
    assert "max_concurrent" not in eth_result.action


def test_run_full_cycle_isolates_one_assets_exception(tmp_path):
    """An unexpected exception in one asset's cycle must not prevent the
    other assets' cycles from running -- matches run_ict_scanner.py's own
    per-asset try/except isolation."""
    broker = FakeBroker()
    broker.positions["BTC/USDT"] = Position(symbol="BTC/USDT", side="long", contracts=1.0, entry_price=100.0, unrealized_pnl=0.0)
    # BTC/USDT is "flat" locally but the broker shows a position -- this
    # path returns a needs_review CycleResult cleanly (not an exception), so
    # force a genuine exception instead by handing ETH a status the state
    # machine doesn't understand.
    store = AssetStateStore(tmp_path / "ETH_USDT")
    store.save_position({"status": "not_a_real_status"})
    candles = {"BTC/USDT": [], "ETH/USDT": []}

    results = run_full_cycle(["BTC/USDT", "ETH/USDT"], broker, tmp_path, candles)

    eth_result = next(r for r in results if r.asset == "ETH/USDT")
    btc_result = next(r for r in results if r.asset == "BTC/USDT")
    assert eth_result.status == "error"
    assert btc_result.status == "needs_review"  # BTC's own cycle still ran normally


def test_unrecoverable_drift_flags_and_goes_inert(tmp_path):
    broker = FakeBroker()
    broker.positions["BTC/USDT"] = Position(symbol="BTC/USDT", side="long", contracts=1.0, entry_price=100.0, unrealized_pnl=0.0)
    store = AssetStateStore(tmp_path / "BTC_USDT")

    result = run_asset_cycle("BTC/USDT", broker, [], store, busy_count=1)
    assert result.status == "needs_review"
    assert store.is_needs_review()

    # A second cycle must short-circuit immediately without touching the broker further.
    broker.positions.clear()  # even if the drift "resolves" itself, the flag keeps it inert
    result2 = run_asset_cycle("BTC/USDT", broker, [], store, busy_count=0)
    assert result2.status == "needs_review"
    assert "no automated action" in result2.action


def test_reconciliation_of_native_close_logs_trade_and_resets_to_flat(tmp_path):
    broker = FakeBroker()
    store = AssetStateStore(tmp_path / "BTC_USDT")
    store.save_position({
        "status": "open_position", "order_id": "order1", "direction": "bullish",
        "entry_price": 100.0, "initial_stop_price": 96.0, "current_stop_price": 100.0,
        "target_price": 120.0, "grade": "a_plus", "qty_total": 10.0, "qty_remaining": 5.0,
        "leverage": 5, "partial_taken": True, "partial_realized_pnl_usd": 40.0,
        "mss_timestamp": 1_000_000_000_000, "fill_timestamp": 1_000_000_000_000,
        "last_processed_bar_ts": 1_000_003_600_000,
    })
    # broker.positions has no entry for BTC/USDT -- simulates the native SL/TP already closed it.
    broker.closed_trades["BTC/USDT"] = [{
        "order_id": "close1", "exit_price": 120.0, "closed_pnl_usd": 100.0, "created_ms": 1_000_003_600_000, "closed_ms": 1_000_007_200_000,
    }]

    result = run_asset_cycle("BTC/USDT", broker, [], store, busy_count=1)

    assert result.status == "flat"
    assert store.load_position() == {"status": "flat"}
    trade_lines = (tmp_path / "BTC_USDT" / "trades.jsonl").read_text().strip().splitlines()
    assert len(trade_lines) == 1
    logged = json.loads(trade_lines[0])
    assert logged["pnl_usd"] == pytest.approx(140.0)  # 100 native + 40 already-realized partial
    assert logged["close_reason"] == "exchange_native"


def test_reconciliation_of_native_close_waits_if_no_matching_record_yet(tmp_path):
    broker = FakeBroker()
    store = AssetStateStore(tmp_path / "BTC_USDT")
    store.save_position({
        "status": "open_position", "order_id": "order1", "direction": "bullish",
        "entry_price": 100.0, "initial_stop_price": 96.0, "current_stop_price": 100.0,
        "target_price": 120.0, "grade": "a_plus", "qty_total": 10.0, "qty_remaining": 10.0,
        "leverage": 5, "partial_taken": False, "partial_realized_pnl_usd": 0.0,
        "mss_timestamp": 1_000_000_000_000, "fill_timestamp": 1_000_000_000_000,
        "last_processed_bar_ts": 1_000_000_000_000,
    })
    broker.closed_trades["BTC/USDT"] = []  # closed-pnl feed hasn't caught up yet

    result = run_asset_cycle("BTC/USDT", broker, [], store, busy_count=1)

    assert result.status == "open_position"
    assert "rechecking next cycle" in result.action
    assert store.load_position()["status"] == "open_position"  # unchanged


def test_manage_open_position_partial_take_and_breakeven_wiring(tmp_path):
    """Integration-level: does the partial-take/breakeven ManagementAction
    correctly translate into real broker.reduce_position + update_trailing_stop
    calls and correctly-updated persisted state? (The deeper swing-trailing
    logic itself is proven at the replay_management_bars unit level above and
    cross-checked against _manage_position in the differential test -- this
    test is about the live.py wiring, not re-proving that logic.)
    """
    broker = FakeBroker()
    broker.positions["BTC/USDT"] = Position(symbol="BTC/USDT", side="long", contracts=10.0, entry_price=100.0, unrealized_pnl=0.0)
    store = AssetStateStore(tmp_path / "BTC_USDT")

    fill_ts = 1_000_000_000_000
    # exec_tf="15m" makes build_detection_context treat these bars as the
    # execution timeframe directly (resample("15m") is a no-op passthrough),
    # sidestepping wall-clock 1h bucketing for a small, precise fixture.
    candles = make_candles([
        (100, 101, 99, 100),   # 0: fill bar
        (101, 109, 100, 108),  # 1: crosses the 2R partial level (108)
    ], start_ts=fill_ts, step_ms=900_000)  # 15m spacing

    store.save_position({
        "status": "open_position", "order_id": "order1", "direction": "bullish",
        "entry_price": 100.0, "initial_stop_price": 96.0, "current_stop_price": 96.0,
        "target_price": 200.0, "grade": "a_plus", "qty_total": 10.0, "qty_remaining": 10.0,
        "leverage": 5, "partial_taken": False, "partial_realized_pnl_usd": 0.0,
        "mss_timestamp": fill_ts, "fill_timestamp": fill_ts, "last_processed_bar_ts": fill_ts,
    })

    result = run_asset_cycle("BTC/USDT", broker, candles, store, busy_count=1, exec_tf="15m")

    assert result.status == "open_position"
    assert result.mutated is True
    assert len(broker.reduce_position_calls) == 1
    assert broker.reduce_position_calls[0]["side"] == "sell"
    assert broker.reduce_position_calls[0]["qty"] == pytest.approx(5.0)
    assert len(broker.update_trailing_stop_calls) == 1
    assert broker.update_trailing_stop_calls[0]["new_sl"] == pytest.approx(100.0)  # breakeven

    saved = store.load_position()
    assert saved["partial_taken"] is True
    assert saved["qty_remaining"] == pytest.approx(5.0)
    assert saved["current_stop_price"] == pytest.approx(100.0)


def test_manage_open_position_no_action_persists_last_processed_ts(tmp_path):
    broker = FakeBroker()
    broker.positions["BTC/USDT"] = Position(symbol="BTC/USDT", side="long", contracts=10.0, entry_price=100.0, unrealized_pnl=0.0)
    store = AssetStateStore(tmp_path / "BTC_USDT")
    fill_ts = 1_000_000_000_000
    candles = make_candles([(100, 101, 99, 100), (100, 101, 99, 100)], start_ts=fill_ts, step_ms=900_000)
    store.save_position({
        "status": "open_position", "order_id": "order1", "direction": "bullish",
        "entry_price": 100.0, "initial_stop_price": 96.0, "current_stop_price": 96.0,
        "target_price": 200.0, "grade": "a_plus", "qty_total": 10.0, "qty_remaining": 10.0,
        "leverage": 5, "partial_taken": False, "partial_realized_pnl_usd": 0.0,
        "mss_timestamp": fill_ts, "fill_timestamp": fill_ts, "last_processed_bar_ts": fill_ts,
    })

    result = run_asset_cycle("BTC/USDT", broker, candles, store, busy_count=1, exec_tf="15m")

    assert broker.reduce_position_calls == []
    assert broker.update_trailing_stop_calls == []
    saved = store.load_position()
    assert saved["last_processed_bar_ts"] == candles[-1].timestamp


def test_dry_run_never_calls_mutating_broker_methods_or_persists_state(tmp_path):
    broker = FakeBroker()
    broker.positions["BTC/USDT"] = Position(symbol="BTC/USDT", side="long", contracts=10.0, entry_price=100.0, unrealized_pnl=0.0)
    store = AssetStateStore(tmp_path / "BTC_USDT")
    fill_ts = 1_000_000_000_000
    candles = make_candles([(100, 101, 99, 100), (101, 109, 100, 108)], start_ts=fill_ts, step_ms=900_000)
    original_position = {
        "status": "open_position", "order_id": "order1", "direction": "bullish",
        "entry_price": 100.0, "initial_stop_price": 96.0, "current_stop_price": 96.0,
        "target_price": 200.0, "grade": "a_plus", "qty_total": 10.0, "qty_remaining": 10.0,
        "leverage": 5, "partial_taken": False, "partial_realized_pnl_usd": 0.0,
        "mss_timestamp": fill_ts, "fill_timestamp": fill_ts, "last_processed_bar_ts": fill_ts,
    }
    store.save_position(dict(original_position))

    result = run_asset_cycle("BTC/USDT", broker, candles, store, busy_count=1, dry_run=True, exec_tf="15m")

    assert result.mutated is False
    assert broker.reduce_position_calls == []
    assert broker.update_trailing_stop_calls == []
    assert store.load_position() == original_position  # completely untouched on disk


# ── State-machine tests requiring real detection (scan_asset / locate_pending_setup) ──


@pytest.fixture(scope="module")
def anchor():
    candles = _load_csv(BTC_CSV, limit=TWO_YEARS_OF_15M_BARS)
    found = _find_a_real_qualified_and_filled_mss(candles)
    assert found is not None, "expected a real qualified-then-filled BTC setup in the first 2 years to anchor these tests on"
    mss, setup, fill_index = found
    return candles, mss, setup, fill_index


@pytestmark_real_data
def test_new_entry_placement_on_qualified_alert(tmp_path, anchor):
    candles, mss, setup, fill_index = anchor
    broker = FakeBroker()
    store = AssetStateStore(tmp_path / "BTC_USDT")

    # Scan strictly before the fill -- a pending alert must exist here (same
    # anchor already proven pending-before-fill by test_scanner.py's parity test).
    result = run_asset_cycle("BTC/USDT", broker, candles, store, busy_count=0,
                              as_of_index=fill_index - 1, **CALIBRATED)

    assert result.status == "resting_order"
    assert len(broker.place_order_calls) == 1
    call = broker.place_order_calls[0]
    assert call["order_type"] == "limit"
    assert call["stop_loss"] == setup.stop_price
    assert call["take_profit"] == setup.target_price

    saved = store.load_position()
    assert saved["status"] == "resting_order"
    assert saved["mss_timestamp"] in store.load_attempted()


def _mss_timestamp(candles, mss):
    from hermes_trading.ict.scanner import build_detection_context
    ctx = build_detection_context(candles, disp_atr_mult=CALIBRATED["disp_atr_mult"])
    return ctx.exec_full[mss.index].timestamp


def _candles_through_exec_index(candles, exec_index):
    """
    Truncate 15m candles so resample(..., "1h") ends exactly at
    exec_full[exec_index] inclusive -- simulates "the live worker has only
    seen data up to this point," which _check_resting_order_status needs
    (it derives as_of_index from len(candles_15m)'s own resample, it
    doesn't take an explicit override the way scan_asset does).
    """
    from hermes_trading.ict.scanner import build_detection_context
    ctx = build_detection_context(candles, disp_atr_mult=CALIBRATED["disp_atr_mult"])
    if exec_index + 1 >= len(ctx.exec_full):
        return list(candles)
    # Inclusive of the cutoff bucket's own first 15m candle: a 1h bucket
    # only counts as "closed" (see resample()) once the NEXT bucket has at
    # least started, so exec_full[exec_index] itself would be dropped
    # entirely by an exclusive "< cutoff_ts" boundary.
    cutoff_ts = ctx.exec_full[exec_index + 1].timestamp
    return [c for c in candles if c.timestamp <= cutoff_ts]


@pytestmark_real_data
def test_resting_order_stays_pending_before_fill(tmp_path, anchor):
    candles, mss, setup, fill_index = anchor
    broker = FakeBroker()
    broker.open_orders["BTC/USDT"] = {"id": "order1"}
    store = AssetStateStore(tmp_path / "BTC_USDT")
    mss_ts = _mss_timestamp(candles, mss)
    store.save_position({
        "status": "resting_order", "order_id": "order1", "direction": setup.direction.value,
        "entry_price": setup.entry_price, "stop_price": setup.stop_price, "target_price": setup.target_price,
        "grade": setup.grade.value, "qty": 1.0, "leverage": 5, "notional": 100.0, "risk_usd": 20.0,
        "mss_timestamp": mss_ts, "placed_at_ms": 0,
    })

    candles_before_fill = _candles_through_exec_index(candles, fill_index - 1)
    result = run_asset_cycle("BTC/USDT", broker, candles_before_fill, store, busy_count=1, **CALIBRATED)

    assert result.status == "resting_order"
    assert result.mutated is False
    assert broker.cancel_order_calls == []


@pytestmark_real_data
def test_resting_order_cancelled_on_expired_status(tmp_path, anchor):
    candles, mss, setup, fill_index = anchor
    broker = FakeBroker()
    broker.open_orders["BTC/USDT"] = {"id": "order1"}
    store = AssetStateStore(tmp_path / "BTC_USDT")
    mss_ts = _mss_timestamp(candles, mss)
    store.save_position({
        "status": "resting_order", "order_id": "order1", "direction": setup.direction.value,
        "entry_price": setup.entry_price, "stop_price": setup.stop_price, "target_price": setup.target_price,
        "grade": setup.grade.value, "qty": 1.0, "leverage": 5, "notional": 100.0, "risk_usd": 20.0,
        "mss_timestamp": mss_ts, "placed_at_ms": 0,
    })

    with patch("hermes_trading.ict.live.resolve_setup_status", return_value=("expired", None)):
        result = run_asset_cycle("BTC/USDT", broker, candles, store, busy_count=1, **CALIBRATED)

    assert result.status == "flat"
    assert broker.cancel_order_calls == [("order1", "BTC/USDT")]
    assert store.load_position() == {"status": "flat"}


@pytestmark_real_data
def test_resting_order_cancelled_on_invalidated_status(tmp_path, anchor):
    candles, mss, setup, fill_index = anchor
    broker = FakeBroker()
    broker.open_orders["BTC/USDT"] = {"id": "order1"}
    store = AssetStateStore(tmp_path / "BTC_USDT")
    mss_ts = _mss_timestamp(candles, mss)
    store.save_position({
        "status": "resting_order", "order_id": "order1", "direction": setup.direction.value,
        "entry_price": setup.entry_price, "stop_price": setup.stop_price, "target_price": setup.target_price,
        "grade": setup.grade.value, "qty": 1.0, "leverage": 5, "notional": 100.0, "risk_usd": 20.0,
        "mss_timestamp": mss_ts, "placed_at_ms": 0,
    })

    with patch("hermes_trading.ict.live.resolve_setup_status", return_value=("invalidated", None)):
        result = run_asset_cycle("BTC/USDT", broker, candles, store, busy_count=1, **CALIBRATED)

    assert result.status == "flat"
    assert broker.cancel_order_calls == [("order1", "BTC/USDT")]


@pytestmark_real_data
def test_resting_order_filled_transition_reverifies_sl_tp(tmp_path, anchor):
    candles, mss, setup, fill_index = anchor
    broker = FakeBroker()
    store = AssetStateStore(tmp_path / "BTC_USDT")
    mss_ts = _mss_timestamp(candles, mss)
    # No open_orders entry for "order1" -- simulates the resting order being gone.
    broker.positions["BTC/USDT"] = Position(symbol="BTC/USDT", side="long" if setup.direction == Direction.BULLISH else "short",
                                             contracts=1.0, entry_price=setup.entry_price, unrealized_pnl=0.0)
    store.save_position({
        "status": "resting_order", "order_id": "order1", "direction": setup.direction.value,
        "entry_price": setup.entry_price, "stop_price": setup.stop_price, "target_price": setup.target_price,
        "grade": setup.grade.value, "qty": 1.0, "leverage": 5, "notional": 100.0, "risk_usd": 20.0,
        "mss_timestamp": mss_ts, "placed_at_ms": 0,
    })

    result = run_asset_cycle("BTC/USDT", broker, candles, store, busy_count=1, **CALIBRATED)

    assert result.status == "open_position"
    assert len(broker.set_position_protection_calls) == 1
    call = broker.set_position_protection_calls[0]
    assert call["stop_loss"] == setup.stop_price
    assert call["take_profit"] == setup.target_price

    saved = store.load_position()
    assert saved["status"] == "open_position"
    assert saved["partial_taken"] is False
    assert saved["qty_remaining"] == 1.0


@pytestmark_real_data
def test_restart_recovery_resumes_open_position_without_double_opening(tmp_path, anchor):
    """Simulates a daemon restart mid-trade: local state says open_position,
    the broker still shows the same position -- must route straight to
    management, never re-enter _look_for_new_setup or place a new order."""
    candles, mss, setup, fill_index = anchor
    broker = FakeBroker()
    broker.positions["BTC/USDT"] = Position(symbol="BTC/USDT", side="long" if setup.direction == Direction.BULLISH else "short",
                                             contracts=1.0, entry_price=setup.entry_price, unrealized_pnl=0.0)
    store = AssetStateStore(tmp_path / "BTC_USDT")
    # fill_timestamp must be an actual exec_full (1h) bucket timestamp, not
    # an arbitrary 15m candle timestamp -- _manage_open_position looks it up
    # via exact match against ctx.exec_full.
    from hermes_trading.ict.scanner import build_detection_context as _bdc
    fill_ts = _bdc(candles, disp_atr_mult=CALIBRATED["disp_atr_mult"]).exec_full[fill_index].timestamp
    store.save_position({
        "status": "open_position", "order_id": "order1", "direction": setup.direction.value,
        "entry_price": setup.entry_price, "initial_stop_price": setup.stop_price, "current_stop_price": setup.stop_price,
        "target_price": setup.target_price, "grade": setup.grade.value, "qty_total": 1.0, "qty_remaining": 1.0,
        "leverage": 5, "partial_taken": False, "partial_realized_pnl_usd": 0.0,
        "mss_timestamp": _mss_timestamp(candles, mss), "fill_timestamp": fill_ts, "last_processed_bar_ts": fill_ts,
    })

    result = run_asset_cycle("BTC/USDT", broker, candles, store, busy_count=1, **CALIBRATED)

    assert result.status == "open_position"
    assert broker.place_order_calls == []  # never treated as a fresh flat/new-entry cycle


# ── Partial-take P&L accounting (session 21 fix) ───────────────────────────
#
# Regression cover for a real bug: the partial_take branch reduced
# qty_remaining and set partial_taken=True but NEVER accumulated
# partial_realized_pnl_usd, so _handle_position_closed's
# "closed_pnl + partial_realized" always added zero and every partialled
# trade under-reported its P&L by the whole 2R leg.


def _open_position(**overrides):
    base = {
        "status": "open_position", "order_id": "order1", "direction": "bullish",
        "entry_price": 100.0, "initial_stop_price": 96.0, "current_stop_price": 96.0,
        "target_price": 120.0, "grade": "a_plus", "qty_total": 10.0, "qty_remaining": 10.0,
        "leverage": 5, "partial_taken": False, "partial_realized_pnl_usd": 0.0,
        "mss_timestamp": 1_000_000_000_000, "fill_timestamp": 1_000_000_000_000,
        "last_processed_bar_ts": 1_000_000_000_000,
    }
    base.update(overrides)
    return base


QUARTER_HOUR_MS = 900_000


def _two_exec_bars():
    """
    Sixteen 15m candles -> wall-clock-aligned 1H exec bars starting at
    timestamp 0, so fill_timestamp/last_processed_bar_ts can be pinned
    deterministically. Note `resample` drops the still-forming final bucket,
    so this deliberately supplies more 15m bars than the exec bars needed.
    """
    return make_candles([(100, 101, 99, 100)] * 16, step_ms=QUARTER_HOUR_MS)


def _drive_partial(broker, store, position, candles):
    """
    Run _manage_open_position with the management replay stubbed to a single
    2R partial-take at the level replay_management_bars would itself have
    produced for entry=100/stop=96.

    replay_management_bars has its own thorough tests above; stubbing it here
    isolates the P&L ACCOUNTING branch -- the part that was broken -- instead
    of re-testing threshold detection through a fixture that would have to be
    reverse-engineered to make the real detector fire.
    """
    from hermes_trading.ict.live import _manage_open_position

    with patch("hermes_trading.ict.live.replay_management_bars",
               return_value=[ManagementAction("partial_take", 1, 108.0)]):
        return _manage_open_position("BTC/USDT", broker, candles, store, position, dry_run=False)


def test_partial_take_records_realised_pnl(tmp_path):
    """The 2R partial's P&L must actually be recorded, not silently dropped."""
    broker = FakeBroker()
    store = AssetStateStore(tmp_path / "BTC_USDT")
    position = _open_position(fill_timestamp=0, last_processed_bar_ts=0)
    broker.positions["BTC/USDT"] = Position(symbol="BTC/USDT", side="long", contracts=10.0,
                                            entry_price=100.0, unrealized_pnl=0.0)

    _drive_partial(broker, store, position, _two_exec_bars())

    saved = store.load_position()
    assert saved["partial_taken"] is True
    # FakeBroker.reduce_position reports price=None, so the 2R trigger level
    # (108) is used as the fill: (108 - 100) * 5 units = 40.0
    assert saved["partial_realized_pnl_usd"] == pytest.approx(40.0)
    assert saved["partial_qty"] == pytest.approx(5.0)
    assert saved["qty_remaining"] == pytest.approx(5.0)


def test_partial_uses_actual_fill_price_when_broker_reports_one(tmp_path):
    """A market reduce can slip; the reported average fill wins over the trigger level."""
    broker = FakeBroker()

    def slipped(symbol, side, qty):
        broker.reduce_position_calls.append({"symbol": symbol, "side": side, "qty": qty})
        return OrderResult(order_id="r1", symbol=symbol, side=side, qty=qty,
                           price=107.0, status="closed")  # filled 1.0 worse than the 108 trigger

    broker.reduce_position = slipped
    store = AssetStateStore(tmp_path / "BTC_USDT")
    position = _open_position(fill_timestamp=0, last_processed_bar_ts=0)
    broker.positions["BTC/USDT"] = Position(symbol="BTC/USDT", side="long", contracts=10.0,
                                            entry_price=100.0, unrealized_pnl=0.0)

    _drive_partial(broker, store, position, _two_exec_bars())

    # (107 - 100) * 5 = 35.0, not the 40.0 the trigger level alone would imply.
    assert store.load_position()["partial_realized_pnl_usd"] == pytest.approx(35.0)


def test_close_sums_all_exchange_legs_of_a_partialled_trade(tmp_path):
    """
    A partialled trade produces MORE THAN ONE closed-pnl record. Taking only
    matching[0] dropped a leg; the sum is the exchange's own fee-inclusive
    truth and must be preferred over the local estimate.
    """
    broker = FakeBroker()
    store = AssetStateStore(tmp_path / "BTC_USDT")
    store.save_position(_open_position(
        qty_remaining=5.0, partial_taken=True, partial_realized_pnl_usd=40.0,
        current_stop_price=100.0, last_processed_bar_ts=1_000_003_600_000,
    ))
    broker.closed_trades["BTC/USDT"] = [
        {"order_id": "close2", "exit_price": 120.0, "qty": 5.0, "closed_pnl_usd": 99.0,
         "created_ms": 1_000_007_200_000, "closed_ms": 1_000_007_200_000},
        {"order_id": "close1", "exit_price": 108.0, "qty": 5.0, "closed_pnl_usd": 39.5,
         "created_ms": 1_000_003_600_000, "closed_ms": 1_000_003_600_000},
    ]

    result = run_asset_cycle("BTC/USDT", broker, [], store, busy_count=1)
    assert result.status == "flat"

    logged = json.loads((tmp_path / "BTC_USDT" / "trades.jsonl").read_text().strip())
    # 99.0 + 39.5 = 138.5 from the exchange -- NOT 99.0 + the 40.0 local estimate.
    assert logged["pnl_usd"] == pytest.approx(138.5)
    assert logged["pnl_source"] == "exchange_sum"
    assert logged["legs"] == 2
    # Qty-weighted average exit across both legs: (120*5 + 108*5) / 10 = 114
    assert logged["exit_price"] == pytest.approx(114.0)


def test_close_falls_back_to_local_estimate_when_legs_dont_reconcile(tmp_path):
    """Feed lag: only one leg present. Flag it rather than under-reporting silently."""
    broker = FakeBroker()
    store = AssetStateStore(tmp_path / "BTC_USDT")
    store.save_position(_open_position(
        qty_remaining=5.0, partial_taken=True, partial_realized_pnl_usd=40.0,
        current_stop_price=100.0, last_processed_bar_ts=1_000_003_600_000,
    ))
    broker.closed_trades["BTC/USDT"] = [
        {"order_id": "close2", "exit_price": 120.0, "qty": 5.0, "closed_pnl_usd": 99.0,
         "created_ms": 1_000_007_200_000, "closed_ms": 1_000_007_200_000},
    ]

    run_asset_cycle("BTC/USDT", broker, [], store, busy_count=1)

    logged = json.loads((tmp_path / "BTC_USDT" / "trades.jsonl").read_text().strip())
    assert logged["pnl_usd"] == pytest.approx(139.0)  # 99 exchange + 40 local partial
    assert logged["pnl_source"] == "local_estimate"


def test_closed_trade_records_r_multiple(tmp_path):
    """R is the unit this strategy is built in -- it must be recorded at close."""
    broker = FakeBroker()
    store = AssetStateStore(tmp_path / "BTC_USDT")
    store.save_position(_open_position(last_processed_bar_ts=1_000_003_600_000))
    broker.closed_trades["BTC/USDT"] = [
        {"order_id": "c1", "exit_price": 120.0, "qty": 10.0, "closed_pnl_usd": 200.0,
         "created_ms": 1_000_003_600_000, "closed_ms": 1_000_007_200_000},
    ]

    run_asset_cycle("BTC/USDT", broker, [], store, busy_count=1)

    logged = json.loads((tmp_path / "BTC_USDT" / "trades.jsonl").read_text().strip())
    # risk/unit = |100 - 96| = 4, total risk = 4 * 10 = 40; 200 / 40 = 5R
    assert logged["realised_r"] == pytest.approx(5.0)
    assert logged["risk_usd"] == pytest.approx(40.0)
    # planned R:R from the initial stop and target: |120-100| / 4 = 5
    assert logged["planned_rr"] == pytest.approx(5.0)
    assert logged["pnl_pct"] == pytest.approx(0.20)


def test_short_trade_pnl_pct_is_direction_aware(tmp_path):
    """A short that exits BELOW entry is a win -- the old system's sign-flip bug."""
    broker = FakeBroker()
    store = AssetStateStore(tmp_path / "BTC_USDT")
    store.save_position(_open_position(
        direction="bearish", entry_price=100.0, initial_stop_price=104.0,
        current_stop_price=104.0, target_price=80.0, last_processed_bar_ts=1_000_003_600_000,
    ))
    broker.closed_trades["BTC/USDT"] = [
        {"order_id": "c1", "exit_price": 90.0, "qty": 10.0, "closed_pnl_usd": 100.0,
         "created_ms": 1_000_003_600_000, "closed_ms": 1_000_007_200_000},
    ]

    run_asset_cycle("BTC/USDT", broker, [], store, busy_count=1)

    logged = json.loads((tmp_path / "BTC_USDT" / "trades.jsonl").read_text().strip())
    assert logged["pnl_pct"] == pytest.approx(0.10)   # positive: price fell, short won
    assert logged["realised_r"] == pytest.approx(2.5)  # 100 / (4 * 10)


# ── Write-ahead intent + pending_placement recovery ─────────────────────────
#
# The order goes to the exchange, THEN the local record is written. A crash in
# that gap used to orphan a live order. The fix persists a `pending_placement`
# intent BEFORE placing, tagged with a deterministic order_link_id, and adds a
# recovery branch that reconciles that intent against the exchange on restart.


def _pending(link_id, **overrides):
    base = {
        "status": "pending_placement", "order_id": None, "order_link_id": link_id,
        "direction": "bullish", "entry_price": 100.0, "stop_price": 96.0, "target_price": 112.0,
        "grade": "A_PLUS", "qty": 1.0, "leverage": 5, "notional": 500.0, "risk_usd": 40.0,
        "mss_timestamp": int(link_id.rsplit("-", 1)[1]), "placed_at_ms": int(link_id.rsplit("-", 1)[1]),
    }
    base.update(overrides)
    return base


def test_write_ahead_persists_intent_before_order_is_placed(tmp_path):
    """The core guarantee: at the instant place_order is called, the local
    record is ALREADY a pending_placement carrying the same order_link_id
    that's passed to the exchange -- so a crash mid-placement can't orphan
    the order."""
    broker = FakeBroker()
    store = AssetStateStore(tmp_path / "BTC_USDT")
    candles = make_candles([(100, 101, 99, 100)], start_ts=1_000_000_000_000)
    alert = SimpleNamespace(direction=Direction.BULLISH, entry_zone=(99.0, 100.0),
                            stop=96.0, target=112.0, grade=Grade.A_PLUS, timestamp=1_000_000_000_000)

    seen = {}
    real_place = broker.place_order

    def spy(*a, **k):
        pos = store.load_position()
        seen["status_at_place"] = pos.get("status")
        seen["link_at_place"] = pos.get("order_link_id")
        seen["link_arg"] = k.get("order_link_id")
        return real_place(*a, **k)

    broker.place_order = spy
    with patch("hermes_trading.ict.live.scan_asset", return_value=[alert]):
        result = run_asset_cycle("BTC/USDT", broker, candles, store, busy_count=0)

    assert result.status == "resting_order"
    assert seen["status_at_place"] == "pending_placement"
    assert seen["link_at_place"] == "ict-BTCUSDT-1000000000000"
    assert seen["link_arg"] == "ict-BTCUSDT-1000000000000"
    final = store.load_position()
    assert final["status"] == "resting_order"
    assert final["order_id"] == "order1"
    assert final["order_link_id"] == "ict-BTCUSDT-1000000000000"


def test_recover_pending_placement_adopts_resting_order(tmp_path):
    broker = FakeBroker()
    store = AssetStateStore(tmp_path / "BTC_USDT")
    link = "ict-BTCUSDT-123"
    store.save_position(_pending(link))
    broker.open_orders["BTC/USDT"] = {"id": "orderX", "clientOrderId": link, "info": {"orderLinkId": link}}

    result = run_asset_cycle("BTC/USDT", broker, [], store, busy_count=0)

    assert result.status == "resting_order"
    pos = store.load_position()
    assert pos["status"] == "resting_order"
    assert pos["order_id"] == "orderX"
    assert broker.place_order_calls == []  # never re-placed


def test_recover_pending_placement_adopts_filled_position(tmp_path):
    broker = FakeBroker()
    store = AssetStateStore(tmp_path / "BTC_USDT")
    link = "ict-BTCUSDT-777"
    store.save_position(_pending(link))
    broker.order_registry[link] = {"order_id": "orderX", "status": "filled"}
    broker.positions["BTC/USDT"] = Position(symbol="BTC/USDT", side="long", contracts=1.0,
                                            entry_price=100.0, unrealized_pnl=0.0)
    candles = make_candles([(100, 101, 99, 100), (100, 101, 99, 100)], start_ts=1_000_000_000_000)

    result = run_asset_cycle("BTC/USDT", broker, candles, store, busy_count=0)

    assert result.status == "open_position"
    pos = store.load_position()
    assert pos["status"] == "open_position"
    assert pos["entry_price"] == 100.0
    assert broker.set_position_protection_calls  # SL/TP re-verified on adopt


def test_recover_pending_placement_resets_flat_when_never_placed(tmp_path):
    broker = FakeBroker()
    store = AssetStateStore(tmp_path / "BTC_USDT")
    store.save_position(_pending("ict-BTCUSDT-1"))  # nothing at the exchange, no registry entry

    result = run_asset_cycle("BTC/USDT", broker, [], store, busy_count=0)

    assert result.status == "flat"
    assert "never reached" in result.action
    assert store.load_position() == {"status": "flat"}


def test_recover_pending_placement_resets_flat_when_cancelled(tmp_path):
    broker = FakeBroker()
    store = AssetStateStore(tmp_path / "BTC_USDT")
    link = "ict-BTCUSDT-2"
    store.save_position(_pending(link))
    broker.order_registry[link] = {"order_id": "orderX", "status": "cancelled"}

    result = run_asset_cycle("BTC/USDT", broker, [], store, busy_count=0)

    assert result.status == "flat"
    assert store.load_position() == {"status": "flat"}


def test_recover_pending_placement_flags_filled_and_closed_during_downtime(tmp_path):
    broker = FakeBroker()
    store = AssetStateStore(tmp_path / "BTC_USDT")
    link = "ict-BTCUSDT-3"
    store.save_position(_pending(link))
    broker.order_registry[link] = {"order_id": "orderX", "status": "filled"}  # filled, but no open position

    result = run_asset_cycle("BTC/USDT", broker, [], store, busy_count=0)

    assert result.status == "needs_review"
    assert store.is_needs_review()


def test_recover_pending_placement_flags_unmatched_open_position(tmp_path):
    broker = FakeBroker()
    store = AssetStateStore(tmp_path / "BTC_USDT")
    link = "ict-BTCUSDT-4"
    store.save_position(_pending(link))
    # no matching open order, link not in registry, yet a position exists -> can't
    # confidently call it ours -> flag rather than reset flat over a live position.
    broker.positions["BTC/USDT"] = Position(symbol="BTC/USDT", side="long", contracts=1.0,
                                            entry_price=100.0, unrealized_pnl=0.0)

    result = run_asset_cycle("BTC/USDT", broker, [], store, busy_count=0)

    assert result.status == "needs_review"
    assert store.is_needs_review()
