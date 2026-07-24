"""
hermes_trading.ict.live -- real-time (real money) trading state machine.

Reuses scan_asset (new-setup detection), resolve_setup_status (resting-
order pending/invalidated/expired resolution), and
position_size/circuit_breaker_status/max_concurrent_ok (risk.py) exactly
as already tested. The one genuinely new piece is replay_management_bars,
which mirrors _manage_position's (hermes_trading.ict.backtest) 2R-partial/
breakeven/trail thresholds bar-by-bar but -- unlike _manage_position --
never simulates a stop/target hit from candles: live exits are always
ground-truthed from the broker's actual position/closed-pnl state via
reconciliation, since a simulated exit price would silently diverge from
the real fill price and fees.

State model, persisted per asset under state-ict-live/<asset>/ (see
tools/run_ict_live.py for the file layout): a single `position.json`
tagged by status ("flat" | "resting_order" | "open_position"), an
`attempted.json` set of MSS timestamps already acted on (separate from
the alert-only scanner's own `alerted.json` -- different meaning), a
`circuit_breaker.json` day/week PnL bucket, and an append-only
`trades.jsonl` log of real closed trades. Fail-safe by design: local
state the broker's ground truth can't confirm (e.g. broker shows an open
position with no matching local record) stops all automated management
of that asset (`NEEDS_MANUAL_REVIEW.flag`) rather than guessing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from hermes_trading.brokers.base import BrokerAdapter
from hermes_trading.ict.backtest import (
    DEFAULT_MSS_RETRACE_BUFFER_MULT,
    DEFAULT_PARTIAL_FRACTION,
    DEFAULT_PARTIAL_R,
    DEFAULT_STATE_TTL_BARS,
    _bucket_start,
    resolve_setup_status,
)
from hermes_trading.ict.risk import (
    DEFAULT_DAILY_LOSS_LIMIT_PCT,
    DEFAULT_MAX_CONCURRENT_TRADES,
    DEFAULT_WEEKLY_LOSS_LIMIT_PCT,
    circuit_breaker_status,
    max_concurrent_ok,
    position_size,
)
from hermes_trading.ict.scanner import (
    DetectionContext,
    build_detection_context,
    locate_pending_setup,
    scan_asset,
)
from hermes_trading.ict.types import Direction, SwingKind
from hermes_trading.ict.util import Candle

_DETECTION_CONTEXT_KEYS = {"exec_tf", "swing_n_exec", "atr_period", "disp_atr_mult"}


def _order_link_id(asset: str, mss_timestamp: int) -> str:
    """Deterministic client order id for a setup. Being derived from the
    setup (not random) means a crash-and-retry of the SAME setup reuses the
    same id, so the exchange dedupes it (no double-placement), and a
    `pending_placement` record can be tied back to a possibly-orphaned live
    order after a restart. Bybit's orderLinkId limit is 36 chars, [A-Za-z0-9-_];
    `ict-<SYMBOL>-<ms>` stays well under that for every symbol we trade."""
    return f"ict-{asset.replace('/', '')}-{mss_timestamp}"


def _order_matches_link(order: dict, link_id: str | None) -> bool:
    if not link_id:
        return False
    info = order.get("info") or {}
    return order.get("clientOrderId") == link_id or info.get("orderLinkId") == link_id


def _detection_kwargs(scan_params: dict) -> dict:
    return {k: v for k, v in scan_params.items() if k in _DETECTION_CONTEXT_KEYS}


def _index_for_timestamp(exec_full: Sequence[Candle], ts: int) -> int | None:
    for i, c in enumerate(exec_full):
        if c.timestamp == ts:
            return i
    return None


def _index_after_timestamp(exec_full: Sequence[Candle], ts: int) -> int | None:
    for i, c in enumerate(exec_full):
        if c.timestamp > ts:
            return i
    return None


# ── Management-bar replay -- new, NOT a reuse of _manage_position; see module docstring ──


@dataclass(frozen=True)
class ManagementAction:
    kind: str  # "partial_take" | "trail_stop"
    bar_index: int
    price: float  # partial trigger price, or the new stop level


def replay_management_bars(
    exec_full: Sequence[Candle],
    exec_swings,
    direction: Direction,
    entry_price: float,
    initial_stop: float,
    fill_index: int,
    partial_taken: bool,
    current_stop: float,
    start_index: int,
    end_index: int,
    *,
    partial_r: float = DEFAULT_PARTIAL_R,
) -> list[ManagementAction]:
    """
    Walk exec_full[start_index..end_index] (inclusive) replaying ONLY the
    2R-partial-take and post-breakeven trailing-stop decisions, mirroring
    _manage_position's exact thresholds bar-for-bar -- NOT stop/target
    hits, which are always ground-truthed from the live broker's actual
    position state, never simulated from candles. Bounded to
    [start_index, end_index] rather than jumping straight to the latest
    bar so a cycle that's behind (e.g. after downtime) catches up bar by
    bar and can't silently skip an intermediate event. `fill_index` is the
    ORIGINAL fill bar (not necessarily start_index -- a resumed cycle's
    start_index is wherever it last left off, but swing eligibility for
    trailing is always relative to the true fill), matching
    _manage_position's `s.index > fill_index` swing filter exactly.

    See test_replay_management_bars_matches_manage_position_reference for
    the differential regression test against _manage_position.
    """
    risk_per_unit = abs(entry_price - initial_stop)
    partial_level = (
        entry_price + partial_r * risk_per_unit if direction == Direction.BULLISH
        else entry_price - partial_r * risk_per_unit
    )

    actions: list[ManagementAction] = []
    stop = current_stop
    taken = partial_taken

    for j in range(start_index, min(end_index, len(exec_full) - 1) + 1):
        c = exec_full[j]
        if direction == Direction.BULLISH:
            if not taken and c.high >= partial_level:
                actions.append(ManagementAction("partial_take", j, partial_level))
                taken = True
                new_stop = max(stop, entry_price)
                if new_stop != stop:
                    actions.append(ManagementAction("trail_stop", j, new_stop))
                    stop = new_stop
            if taken:
                recent_lows = [s for s in exec_swings if s.kind == SwingKind.LOW and s.index > fill_index and s.confirmed_index <= j]
                if recent_lows:
                    new_stop = max(stop, recent_lows[-1].price)
                    if new_stop != stop:
                        actions.append(ManagementAction("trail_stop", j, new_stop))
                        stop = new_stop
        else:
            if not taken and c.low <= partial_level:
                actions.append(ManagementAction("partial_take", j, partial_level))
                taken = True
                new_stop = min(stop, entry_price)
                if new_stop != stop:
                    actions.append(ManagementAction("trail_stop", j, new_stop))
                    stop = new_stop
            if taken:
                recent_highs = [s for s in exec_swings if s.kind == SwingKind.HIGH and s.index > fill_index and s.confirmed_index <= j]
                if recent_highs:
                    new_stop = min(stop, recent_highs[-1].price)
                    if new_stop != stop:
                        actions.append(ManagementAction("trail_stop", j, new_stop))
                        stop = new_stop

    return actions


# ── Persisted per-asset state ─────────────────────────────────────────────


class AssetStateStore:
    """File-backed state for one asset under state-ict-live/<asset>/."""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _position_path(self) -> Path:
        return self.state_dir / "position.json"

    @property
    def _attempted_path(self) -> Path:
        return self.state_dir / "attempted.json"

    @property
    def _circuit_breaker_path(self) -> Path:
        return self.state_dir / "circuit_breaker.json"

    @property
    def _trades_path(self) -> Path:
        return self.state_dir / "trades.jsonl"

    @property
    def needs_review_flag_path(self) -> Path:
        return self.state_dir / "NEEDS_MANUAL_REVIEW.flag"

    def load_position(self) -> dict:
        if not self._position_path.exists():
            return {"status": "flat"}
        return json.loads(self._position_path.read_text())

    def save_position(self, position: dict) -> None:
        self._position_path.write_text(json.dumps(position, indent=2))

    def load_attempted(self) -> set[int]:
        if not self._attempted_path.exists():
            return set()
        return set(json.loads(self._attempted_path.read_text()))

    def save_attempted(self, attempted: set[int]) -> None:
        self._attempted_path.write_text(json.dumps(sorted(attempted)))

    def load_circuit_breaker(self) -> dict:
        if not self._circuit_breaker_path.exists():
            return {
                "day_bucket_start_ms": None, "equity_at_day_start": None,
                "week_bucket_start_ms": None, "equity_at_week_start": None,
            }
        return json.loads(self._circuit_breaker_path.read_text())

    def save_circuit_breaker(self, cb: dict) -> None:
        self._circuit_breaker_path.write_text(json.dumps(cb))

    def append_trade(self, trade: dict) -> None:
        with self._trades_path.open("a") as f:
            f.write(json.dumps(trade) + "\n")

    def is_needs_review(self) -> bool:
        return self.needs_review_flag_path.exists()

    def flag_needs_review(self, reason: str) -> None:
        self.needs_review_flag_path.write_text(reason)


def _update_circuit_breaker_bucket(cb: dict, now_ms: int, equity: float) -> dict:
    day_bucket = _bucket_start(now_ms, "1d")
    week_bucket = _bucket_start(now_ms, "1w")
    if cb.get("day_bucket_start_ms") != day_bucket:
        cb["day_bucket_start_ms"] = day_bucket
        cb["equity_at_day_start"] = equity
    if cb.get("week_bucket_start_ms") != week_bucket:
        cb["week_bucket_start_ms"] = week_bucket
        cb["equity_at_week_start"] = equity
    return cb


# ── Per-cycle result ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class CycleResult:
    asset: str
    status: str  # "flat" | "resting_order" | "open_position" | "needs_review" | "error"
    action: str  # human-readable description of what happened this cycle
    mutated: bool  # True if a broker-mutating call (or a state write) actually happened


# ── Per-asset cycle ────────────────────────────────────────────────────────


def run_asset_cycle(
    asset: str,
    broker: BrokerAdapter,
    candles_15m: Sequence[Candle],
    store: AssetStateStore,
    busy_count: int,
    *,
    dry_run: bool = False,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT_TRADES,
    daily_limit_pct: float = DEFAULT_DAILY_LOSS_LIMIT_PCT,
    weekly_limit_pct: float = DEFAULT_WEEKLY_LOSS_LIMIT_PCT,
    detection_context: DetectionContext | None = None,
    **scan_params,
) -> CycleResult:
    """
    `detection_context`, when supplied, is reused by every branch below
    instead of each rebuilding its own. All three consumers
    (_look_for_new_setup -> scan_asset, _check_resting_order_status,
    _manage_open_position) already derive theirs from the identical
    _DETECTION_CONTEXT_KEYS subset of scan_params, so one context is valid
    for all of them. Caller must build it from the same candles and the
    same detection params; pass None to have each branch build its own.
    """
    if store.is_needs_review():
        return CycleResult(asset, "needs_review", "flagged for manual review -- no automated action taken", False)

    position = store.load_position()
    status = position.get("status", "flat")

    if status == "pending_placement":
        return _recover_pending_placement(asset, broker, candles_15m, store, position,
                                          dry_run=dry_run, detection_context=detection_context)

    if status == "flat":
        if broker.has_open_position(asset):
            store.flag_needs_review(
                f"local state is flat but the broker shows an open position for {asset} with no local record"
            )
            return CycleResult(asset, "needs_review", "unrecoverable drift: broker has a position, no local record", False)
        return _look_for_new_setup(asset, broker, candles_15m, store, busy_count, dry_run=dry_run,
                                    max_concurrent=max_concurrent, daily_limit_pct=daily_limit_pct,
                                    weekly_limit_pct=weekly_limit_pct,
                                    detection_context=detection_context, **scan_params)

    if status == "resting_order":
        open_orders = broker.get_open_orders(asset)
        still_resting = any(str(o.get("id")) == str(position.get("order_id")) for o in open_orders)
        if not still_resting:
            if broker.has_open_position(asset):
                return _handle_resting_order_filled(asset, broker, candles_15m, store, position, dry_run=dry_run,
                                                     detection_context=detection_context)
            if not dry_run:
                store.save_position({"status": "flat"})
            return CycleResult(asset, "flat", "resting order gone at the exchange with no fill -- reset to flat", not dry_run)
        return _check_resting_order_status(asset, broker, candles_15m, store, position, dry_run=dry_run,
                                            detection_context=detection_context, **scan_params)

    if status == "open_position":
        if not broker.has_open_position(asset):
            return _handle_position_closed(asset, broker, store, position, dry_run=dry_run)
        return _manage_open_position(asset, broker, candles_15m, store, position, dry_run=dry_run,
                                      detection_context=detection_context, **scan_params)

    raise ValueError(f"unknown local position status {status!r} for {asset}")


def _recover_pending_placement(asset, broker, candles_15m, store, position, *, dry_run,
                               detection_context=None) -> CycleResult:
    """Reconcile a crash between order placement and its confirmation write.
    The intent record was persisted BEFORE the order went to the exchange, so
    the setup's `order_link_id` ties it back to whatever (if anything) is
    live. Each case resolves deterministically:
      - order still resting at the exchange   -> adopt as resting_order
      - order filled into an open position     -> adopt as open_position
      - order never reached the exchange / cancelled / rejected -> reset flat
      - order filled AND already closed while down, or an open position that
        can't be tied to our link id           -> flag for manual review
    Never resets to flat while a position is open -- that would hand an
    unmanaged live position back to the new-setup path."""
    link_id = position.get("order_link_id")

    # Fast path: the resting limit order is still visible in open orders.
    for o in broker.get_open_orders(asset):
        if _order_matches_link(o, link_id):
            if not dry_run:
                store.save_position({**position, "status": "resting_order", "order_id": str(o.get("id"))})
            return CycleResult(asset, "resting_order",
                               "recovered pending_placement: order still resting -- adopted", not dry_run)

    # Authoritative client-id lookup for every other case.
    order = broker.find_order_by_link_id(asset, link_id) if link_id else None
    st = (order or {}).get("status", "")

    if st in ("filled", "partiallyfilled"):
        if broker.has_open_position(asset):
            recovered = {**position, "status": "resting_order", "order_id": order["order_id"]}
            return _handle_resting_order_filled(asset, broker, candles_15m, store, recovered,
                                                dry_run=dry_run, detection_context=detection_context)
        store.flag_needs_review(
            f"pending_placement order {link_id} shows {st} at the exchange but no open position remains "
            f"-- it likely filled and closed during downtime; reconcile P&L manually")
        return CycleResult(asset, "needs_review",
                           "recovered pending_placement: filled and closed during downtime -- flagged", False)

    if st in ("new", "untriggered"):
        # Live at the exchange but not surfaced by get_open_orders this instant.
        if not dry_run:
            store.save_position({**position, "status": "resting_order", "order_id": order["order_id"]})
        return CycleResult(asset, "resting_order",
                           "recovered pending_placement: order live at exchange -- adopted", not dry_run)

    # Nothing of ours is resting or filled. Guard against ever resetting to
    # flat while a position is open (can't confidently call it ours).
    if broker.has_open_position(asset):
        store.flag_needs_review(
            f"pending_placement for {asset}: an open position exists but could not be tied to order_link_id "
            f"{link_id} -- reconcile manually before the worker manages it")
        return CycleResult(asset, "needs_review",
                           "recovered pending_placement: unmatched open position -- flagged", False)

    if not dry_run:
        store.save_position({"status": "flat"})
    reason = f"order {st} at exchange" if order else "order never reached the exchange"
    return CycleResult(asset, "flat", f"recovered pending_placement: {reason} -- reset to flat", not dry_run)


def _look_for_new_setup(
    asset, broker, candles_15m, store, busy_count, *, dry_run,
    max_concurrent, daily_limit_pct, weekly_limit_pct,
    detection_context=None, **scan_params,
) -> CycleResult:
    if not max_concurrent_ok(busy_count, max_concurrent=max_concurrent):
        return CycleResult(asset, "flat", f"max_concurrent reached ({busy_count}/{max_concurrent}) -- skipping new-entry check", False)
    if not candles_15m:
        return CycleResult(asset, "flat", "no candle history yet", False)

    equity = broker.get_balance()
    now_ms = candles_15m[-1].timestamp
    cb = _update_circuit_breaker_bucket(store.load_circuit_breaker(), now_ms, equity)
    daily_pnl_pct = ((equity - cb["equity_at_day_start"]) / cb["equity_at_day_start"]) if cb["equity_at_day_start"] else 0.0
    weekly_pnl_pct = ((equity - cb["equity_at_week_start"]) / cb["equity_at_week_start"]) if cb["equity_at_week_start"] else 0.0
    if not dry_run:
        store.save_circuit_breaker(cb)

    if circuit_breaker_status(daily_pnl_pct, weekly_pnl_pct, daily_limit_pct=daily_limit_pct, weekly_limit_pct=weekly_limit_pct):
        return CycleResult(asset, "flat", "circuit breaker active -- standing down", False)

    attempted = store.load_attempted()
    alerts = scan_asset(candles_15m, asset, equity, already_alerted=attempted,
                        detection_context=detection_context, **scan_params)
    if not alerts:
        return CycleResult(asset, "flat", "no qualified pending setup", False)

    alert = alerts[0]
    entry_price = (alert.entry_zone[0] + alert.entry_zone[1]) / 2
    size = position_size(equity, entry_price, alert.stop, alert.grade)

    if not dry_run:
        attempted.add(alert.timestamp)
        store.save_attempted(attempted)

    if size is None:
        # Not expected in practice (a qualified alert is always A_PLUS/B, and
        # position_size only returns None for Grade.NONE) but this crosses a
        # module boundary (setup.py's qualification guarantee), so it's worth
        # a real check rather than trusting it silently -- real money.
        return CycleResult(asset, "flat", "position_size returned None for this grade -- skipping", False)

    side = "buy" if alert.direction == Direction.BULLISH else "sell"
    link_id = _order_link_id(asset, alert.timestamp)

    # Fields common to the pre-placement intent and the confirmed resting
    # order. `order_id` and `status` are the only things that differ between
    # the two writes below.
    base = {
        "direction": alert.direction.value,
        "entry_price": entry_price,
        "stop_price": alert.stop,
        "target_price": alert.target,
        "grade": alert.grade.value,
        "qty": size.qty,
        "leverage": size.leverage,
        "notional": size.notional,
        "risk_usd": size.risk_usd,
        "mss_timestamp": alert.timestamp,
        "placed_at_ms": now_ms,
        "order_link_id": link_id,
    }

    if dry_run:
        return CycleResult(asset, "resting_order",
                           f"[dry-run] would place {side} limit @ {entry_price} qty={size.qty} lev={size.leverage}x", False)

    # Write-ahead: persist the intent BEFORE the order reaches the exchange.
    # If the worker dies between here and the confirmation write below, the
    # order can't be orphaned -- the next cycle's pending_placement branch
    # reconciles this record against the exchange by order_link_id. The
    # deterministic link_id also makes placement idempotent at the exchange.
    store.save_position({**base, "status": "pending_placement", "order_id": None})
    result = broker.place_order(
        asset, side, size.qty, order_type="limit", price=entry_price,
        stop_loss=alert.stop, take_profit=alert.target, leverage=size.leverage,
        order_link_id=link_id,
    )
    store.save_position({**base, "status": "resting_order", "order_id": result.order_id})
    return CycleResult(asset, "resting_order", f"placed {side} limit @ {entry_price} qty={size.qty} lev={size.leverage}x", True)


def _check_resting_order_status(asset, broker, candles_15m, store, position, *, dry_run,
                                 detection_context=None, **scan_params) -> CycleResult:
    ctx = detection_context if detection_context is not None else build_detection_context(
        candles_15m, **_detection_kwargs(scan_params))
    if not ctx.exec_full:
        return CycleResult(asset, "resting_order", "no candle history yet -- leaving order resting", False)

    found = locate_pending_setup(ctx, position["mss_timestamp"])
    if found is None:
        store.flag_needs_review(f"resting order's original MSS (ts={position['mss_timestamp']}) not found in current detection context for {asset}")
        return CycleResult(asset, "needs_review", "resting order's MSS vanished from detection context", False)
    sweep, mss = found

    as_of_index = len(ctx.exec_full) - 1
    status, _ = resolve_setup_status(
        ctx.exec_full, mss.index + 1, position["entry_price"], Direction(position["direction"]),
        sweep, mss, scan_params.get("state_ttl_bars", DEFAULT_STATE_TTL_BARS), as_of_index,
        atr_series=ctx.atr_series,
        mss_retrace_buffer_mult=scan_params.get("mss_retrace_buffer_mult", DEFAULT_MSS_RETRACE_BUFFER_MULT),
    )

    if status == "pending":
        return CycleResult(asset, "resting_order", "still pending, left resting", False)
    if status == "filled":
        # Candle walk suggests a fill, but the broker's open-orders check
        # this same cycle still listed it as resting -- a small timing
        # race. Leave it; next cycle's broker-truth check picks up the
        # actual fill (or, if it truly never filled, this will resolve
        # itself to invalidated/expired on a later cycle).
        return CycleResult(asset, "resting_order", "candle walk suggests filled -- awaiting broker confirmation next cycle", False)

    if not dry_run:
        broker.cancel_order(position["order_id"], asset)
        store.save_position({"status": "flat"})
    return CycleResult(asset, "flat", f"resting order cancelled ({status})", not dry_run)


def _handle_resting_order_filled(asset, broker, candles_15m, store, position, *, dry_run,
                                  detection_context=None) -> CycleResult:
    broker_positions = broker.get_positions(asset)
    if not broker_positions:
        return CycleResult(asset, "resting_order", "order gone but no position found yet -- rechecking next cycle", False)
    pos = broker_positions[0]
    direction = Direction.BULLISH if pos.side == "long" else Direction.BEARISH
    fill_price = pos.entry_price

    # Defensive re-verification: guarantee protective SL/TP are attached to
    # the now-open position regardless of whether Bybit's native
    # stopLoss/takeProfit-on-a-resting-limit-order carried through to the
    # fill (see the live-worker deployment plan's pre-flight verification
    # note -- this call is the safety net either way).
    if not dry_run:
        broker.set_position_protection(asset, stop_loss=position["stop_price"], take_profit=position["target_price"])

    # NOTE (session 21c): this call passes NO detection params, so on its own
    # it builds with library defaults (disp_atr_mult=1.5) rather than the
    # worker's calibrated 0.75 -- unlike every other build_detection_context
    # call in this module, which routes through _detection_kwargs. Detection
    # is param-sensitive, so a mismatched context can fail to re-locate the
    # MSS and silently fall back to fill_index=0, which in turn feeds
    # replay_management_bars' swing-eligibility filter. Passing the shared
    # context (as the live worker now does) resolves that inconsistency;
    # the no-context fallback is left as-is to avoid changing behaviour for
    # callers that don't supply one.
    ctx = detection_context if detection_context is not None else build_detection_context(candles_15m)
    found = locate_pending_setup(ctx, position["mss_timestamp"])
    fill_index = found[1].index if found is not None else 0
    fill_timestamp = ctx.exec_full[fill_index].timestamp if ctx.exec_full else position.get("placed_at_ms", 0)
    last_bar_ts = ctx.exec_full[-1].timestamp if ctx.exec_full else fill_timestamp

    new_position = {
        "status": "open_position",
        "order_id": position["order_id"],
        "direction": direction.value,
        "entry_price": fill_price,
        "initial_stop_price": position["stop_price"],
        "current_stop_price": position["stop_price"],
        "target_price": position["target_price"],
        "grade": position["grade"],
        "qty_total": position["qty"],
        "qty_remaining": position["qty"],
        "leverage": position["leverage"],
        "partial_taken": False,
        "partial_realized_pnl_usd": 0.0,
        "mss_timestamp": position["mss_timestamp"],
        "fill_timestamp": fill_timestamp,
        "last_processed_bar_ts": last_bar_ts,
    }
    if not dry_run:
        store.save_position(new_position)
    return CycleResult(asset, "open_position", f"resting order filled at {fill_price}, SL/TP re-verified", not dry_run)


def _manage_open_position(asset, broker, candles_15m, store, position, *, dry_run,
                           detection_context=None, **scan_params) -> CycleResult:
    ctx = detection_context if detection_context is not None else build_detection_context(
        candles_15m, **_detection_kwargs(scan_params))
    if not ctx.exec_full:
        return CycleResult(asset, "open_position", "no candle history yet", False)

    start_index = _index_after_timestamp(ctx.exec_full, position["last_processed_bar_ts"])
    end_index = len(ctx.exec_full) - 1
    if start_index is None or start_index > end_index:
        return CycleResult(asset, "open_position", "no new bars since last check", False)

    fill_index = _index_for_timestamp(ctx.exec_full, position["fill_timestamp"])
    if fill_index is None:
        store.flag_needs_review(f"open position's fill_timestamp not found in current candle history for {asset}")
        return CycleResult(asset, "needs_review", "fill bar vanished from candle history", False)

    direction = Direction(position["direction"])
    actions = replay_management_bars(
        ctx.exec_full, ctx.exec_swings, direction,
        position["entry_price"], position["initial_stop_price"], fill_index,
        position["partial_taken"], position["current_stop_price"], start_index, end_index,
    )

    mutated = False
    for action in actions:
        if action.kind == "partial_take":
            partial_qty = position["qty_remaining"] * DEFAULT_PARTIAL_FRACTION
            close_side = "sell" if direction == Direction.BULLISH else "buy"
            # `action.price` is the 2R trigger level, NOT a fill price -- the
            # reduce goes out as a market order, so the real fill can differ.
            # Prefer the broker's reported average fill; fall back to the
            # trigger level only when the adapter can't supply one.
            partial_exit_price = action.price
            if not dry_run:
                result = broker.reduce_position(asset, close_side, partial_qty)
                partial_qty = result.qty
                if result.price:
                    partial_exit_price = float(result.price)
            # Record the partial's realised PnL. Previously this was never
            # accumulated -- `partial_realized_pnl_usd` was initialised to 0.0
            # at fill and read back as 0.0 at close, silently dropping the
            # entire 2R leg from every partialled trade's recorded pnl_usd.
            # This local figure is gross of fees and is only a FALLBACK: at
            # close, _handle_position_closed prefers the sum of Bybit's own
            # closed-pnl records (fee-inclusive) whenever they reconcile.
            sign = 1.0 if direction == Direction.BULLISH else -1.0
            position["partial_realized_pnl_usd"] = (
                position.get("partial_realized_pnl_usd", 0.0)
                + sign * (partial_exit_price - position["entry_price"]) * partial_qty
            )
            position["partial_qty"] = position.get("partial_qty", 0.0) + partial_qty
            position["partial_exit_price"] = partial_exit_price
            position["qty_remaining"] -= partial_qty
            position["partial_taken"] = True
        elif action.kind == "trail_stop":
            if not dry_run:
                broker.update_trailing_stop(asset, position["direction"], action.price)
            position["current_stop_price"] = action.price
        if not dry_run:
            # Persist after EACH action, not just once at the end -- if a
            # later action raises, this one's effect is already durably
            # recorded and won't be re-attempted next cycle.
            store.save_position(position)
            mutated = True

    position["last_processed_bar_ts"] = ctx.exec_full[end_index].timestamp
    if not dry_run:
        store.save_position(position)

    summary = f"replayed {len(actions)} management action(s)" if actions else "no management action needed"
    return CycleResult(asset, "open_position", summary, mutated)


_QTY_RECONCILE_TOLERANCE = 0.01  # 1% -- absorbs exchange qty-precision rounding on a partial


def _handle_position_closed(asset, broker, store, position, *, dry_run) -> CycleResult:
    closed_trades = broker.fetch_recent_closed_trades(asset, limit=5)
    matching = [t for t in closed_trades if t.get("created_ms", 0) >= position.get("fill_timestamp", 0)]
    if not matching:
        # Bybit's closed-pnl feed can lag slightly -- leave local state as
        # open_position and recheck next cycle rather than guessing at PnL.
        return CycleResult(asset, "open_position", "position closed at exchange but no matching closed-pnl record yet -- rechecking next cycle", False)

    # A partialled trade produces MORE THAN ONE closed-pnl record (one per
    # reduce, one for the final close). Taking only matching[0] -- as this
    # did before -- silently dropped whichever leg wasn't first. Sum every
    # record belonging to this position instead: that's Bybit's own
    # fee-inclusive ground truth for the whole trade.
    qty_total = position["qty_total"]
    summed_qty = sum(float(t.get("qty", 0) or 0) for t in matching)
    summed_pnl = sum(float(t["closed_pnl_usd"]) for t in matching)

    if qty_total > 0 and abs(summed_qty - qty_total) <= _QTY_RECONCILE_TOLERANCE * qty_total:
        # The records account for the full position -- trust them entirely,
        # and do NOT add the local partial estimate (it's already included).
        total_pnl = summed_pnl
        pnl_source = "exchange_sum"
    else:
        # Feed lag or an unexpected record set: fall back to the newest
        # record plus our own locally-tracked partial estimate, and mark the
        # trade so the dashboard can flag it rather than presenting an
        # unreconciled figure as if it were exchange truth.
        total_pnl = float(matching[0]["closed_pnl_usd"]) + position.get("partial_realized_pnl_usd", 0.0)
        pnl_source = "local_estimate"

    # Qty-weighted average exit across every leg, so a partialled trade's
    # recorded exit_price reflects both the 2R partial and the final close.
    exit_price = (
        sum(float(t["exit_price"]) * float(t.get("qty", 0) or 0) for t in matching) / summed_qty
        if summed_qty > 0 else float(matching[0]["exit_price"])
    )

    entry_price = position["entry_price"]
    initial_stop = position["initial_stop_price"]
    target_price = position.get("target_price")
    is_long = position["direction"] == Direction.BULLISH.value

    # R is the unit this strategy is actually built in -- risk_per_unit is
    # the initial (never the trailed) stop distance, so realised R stays
    # comparable across trades regardless of how the stop was later managed.
    risk_per_unit = abs(entry_price - initial_stop)
    total_risk_usd = risk_per_unit * qty_total
    realised_r = (total_pnl / total_risk_usd) if total_risk_usd > 0 else None
    planned_rr = (abs(target_price - entry_price) / risk_per_unit
                  if target_price is not None and risk_per_unit > 0 else None)
    pnl_pct = ((exit_price - entry_price) / entry_price * (1 if is_long else -1)
               if entry_price else None)

    record = {
        "asset": asset,
        "direction": position["direction"],
        "grade": position["grade"],
        "entry_price": entry_price,
        "exit_price": exit_price,
        "stop_price": initial_stop,
        "final_stop_price": position.get("current_stop_price"),
        "target_price": target_price,
        "qty": qty_total,
        "pnl_usd": total_pnl,
        "pnl_pct": pnl_pct,
        "realised_r": realised_r,
        "planned_rr": planned_rr,
        "risk_usd": total_risk_usd,
        "partial_taken": position.get("partial_taken", False),
        "partial_realized_pnl_usd": position.get("partial_realized_pnl_usd", 0.0),
        "pnl_source": pnl_source,
        "legs": len(matching),
        "close_reason": "exchange_native",
        "entry_utc": position.get("fill_timestamp"),
        "exit_utc": matching[0].get("closed_ms"),
        "order_id": matching[0].get("order_id"),
    }
    if not dry_run:
        store.append_trade(record)
        store.save_position({"status": "flat"})
    r_str = f"{realised_r:+.2f}R" if realised_r is not None else "R n/a"
    return CycleResult(asset, "flat", f"position closed natively, pnl_usd={total_pnl:.2f} ({r_str}, {pnl_source})", not dry_run)


# ── Full-account cycle ──────────────────────────────────────────────────────


def run_full_cycle(
    assets: Sequence[str],
    broker: BrokerAdapter,
    state_root: Path,
    candles_by_asset: dict[str, Sequence[Candle]],
    *,
    dry_run: bool = False,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT_TRADES,
    daily_limit_pct: float = DEFAULT_DAILY_LOSS_LIMIT_PCT,
    weekly_limit_pct: float = DEFAULT_WEEKLY_LOSS_LIMIT_PCT,
    detection_contexts: dict[str, DetectionContext] | None = None,
    **scan_params,
) -> list[CycleResult]:
    """
    One full scan across all assets. Broker ground truth for `busy_count`
    (positions + resting orders) is fetched once up front so a stale count
    from one asset's cycle can't leak into another's max_concurrent check
    within the same pass -- and so two assets each placing a resting order
    in the same pass can't jointly exceed max_concurrent (resting orders
    count as busy too; see the live-worker plan's rationale).

    `busy_count` is scoped to `assets` -- the symbols this worker actually
    trades -- NOT the whole account. It was account-wide until session 21c,
    which meant any manual order Linh placed on any symbol silently consumed
    a slot and (at the then-current max_concurrent=1) stopped the worker
    trading entirely; that's exactly what happened on 2026-07-21 and it is
    invisible in the logs beyond a generic "max_concurrent reached" line.
    The tradeoff accepted here: total account exposure is now this worker's
    slots PLUS whatever is held manually, and nothing reconciles the
    combined margin -- the worker can no longer see, let alone account for,
    positions outside its own universe.

    Each asset's cycle is isolated in its own try/except (matching
    tools/run_ict_scanner.py's own per-asset isolation) so an unexpected
    failure on one asset -- a transient exchange error, an
    OrderTooSmallError, anything -- can't take down monitoring/management
    for the other three assets in the same pass.
    """
    # Per-asset rather than one unfiltered call: the BrokerAdapter interface
    # already supports symbol filtering on both, and matching venue-specific
    # symbol formats (ccxt returns "BTC/USDT:USDT" for "BTC/USDT") in this
    # layer would put exchange-format knowledge on the wrong side of the seam.
    busy_count = sum(
        len(broker.get_positions(asset)) + len(broker.get_open_orders(asset))
        for asset in assets
    )

    results = []
    for asset in assets:
        store = AssetStateStore(Path(state_root) / asset.replace("/", "_"))
        candles = candles_by_asset.get(asset, [])
        try:
            result = run_asset_cycle(
                asset, broker, candles, store, busy_count, dry_run=dry_run,
                max_concurrent=max_concurrent, daily_limit_pct=daily_limit_pct,
                weekly_limit_pct=weekly_limit_pct,
                detection_context=(detection_contexts or {}).get(asset), **scan_params,
            )
        except Exception as e:
            result = CycleResult(asset, "error", f"{type(e).__name__}: {e}", False)
        results.append(result)
    return results
