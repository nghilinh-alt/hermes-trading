"""
hermes_trading.ict.backtest -- event-driven bar-replay engine.

Spec sections: S:5 (full setup state machine), S:11 (backtest design).

Simplification flagged for a follow-up: this first pass uses ONE execution
timeframe (1H, resampled from a 15m base) for MSS confirmation, entry
fills, and management -- spec S:2's full 5-TF cascade (weekly/daily bias,
4h setup zone, 1h confirmation, 15m execution) can be layered in
incrementally once this engine is validated. Weekly/daily bias itself IS
computed properly per S:4 (see `_bias_as_of`), so the HTF-permission gate
is faithful; only the entry-trigger/fill granularity is simplified.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Sequence

from hermes_trading.ict.bias import compute_bias, dealing_range
from hermes_trading.ict.imbalance import find_breakers, find_fvg, find_order_blocks, is_displacement
from hermes_trading.ict.liquidity import detect_sweep, liquidity_pools
from hermes_trading.ict.risk import circuit_breaker_status, max_concurrent_ok, position_size
from hermes_trading.ict.setup import (
    DEFAULT_KILL_ZONES,
    DEFAULT_MAX_BARS_AFTER_MSS,
    DEFAULT_MIN_RR,
    DEFAULT_MIN_TARGET_ATR_MULT,
    build_setup,
)
from hermes_trading.ict.structure import detect_bos, detect_mss, find_swings
from hermes_trading.ict.types import BiasDirection, Direction, Grade, SwingKind
from hermes_trading.ict.util import Candle, atr

_MS_PER_MIN = 60_000
_TF_MS = {
    "15m": 15 * _MS_PER_MIN,
    "1h": 60 * _MS_PER_MIN,
    "4h": 4 * 60 * _MS_PER_MIN,
    "1d": 24 * 60 * _MS_PER_MIN,
    "1w": 7 * 24 * _MS_PER_MIN,
}

DEFAULT_STATE_TTL_BARS = 20
DEFAULT_FEE_PCT = 0.00055  # Bybit maker/taker ~0.055% per side
DEFAULT_PARTIAL_R = 2.0
DEFAULT_PARTIAL_FRACTION = 0.5
DEFAULT_SWING_N = {"15m": 2, "1h": 2, "4h": 3, "1d": 3, "1w": 3}


def _bucket_start(ts_ms: int, target_tf: str) -> int:
    if target_tf == "1w":
        day_ms = _TF_MS["1d"]
        day_index = ts_ms // day_ms
        weekday = (day_index + 3) % 7  # epoch day 0 (1970-01-01) was a Thursday -> weekday index 3 (0=Monday)
        monday_day_index = day_index - weekday
        return monday_day_index * day_ms
    bucket_ms = _TF_MS[target_tf]
    return (ts_ms // bucket_ms) * bucket_ms


def resample(candles_15m: Sequence[Candle], target_tf: str) -> list[Candle]:
    """
    Aggregate a 15m base series into wall-clock-aligned target_tf bars. A
    bucket is only emitted once the input reaches (or passes) the START of
    the NEXT bucket -- i.e. only fully-closed HTF bars are ever produced.
    This is the piece that actually prevents HTF lookahead in the backtest
    (Phase 1's detectors are lookahead-safe *given* correctly-closed bars;
    this is what makes bars correctly closed). Spec S:11.
    """
    if target_tf == "15m":
        return list(candles_15m)
    if not candles_15m:
        return []
    bucket_ms = _TF_MS[target_tf]
    last_ts = candles_15m[-1].timestamp

    buckets: dict[int, list[Candle]] = {}
    order: list[int] = []
    for c in candles_15m:
        b = _bucket_start(c.timestamp, target_tf)
        if b not in buckets:
            buckets[b] = []
            order.append(b)
        buckets[b].append(c)

    result: list[Candle] = []
    for b in order:
        if last_ts < b + bucket_ms:
            continue
        group = buckets[b]
        result.append(
            Candle(
                timestamp=b,
                open=group[0].open,
                high=max(g.high for g in group),
                low=min(g.low for g in group),
                close=group[-1].close,
                volume=sum(g.volume for g in group),
            )
        )
    return result


@dataclass(frozen=True)
class ClosedTrade:
    asset: str
    direction: Direction
    grade: Grade
    entry_index: int
    entry_price: float
    exit_index: int
    exit_price: float
    stop_price: float
    target_price: float
    pnl_usd: float
    r_multiple: float
    close_reason: str  # "target" | "stop" | "trail_stop" | "timeout_no_fill" (never a trade) | "invalidated"
    hold_bars: int
    entry_timestamp: int = 0  # epoch ms of the fill bar; 0 for callers that predate this field


@dataclass(frozen=True)
class BacktestResult:
    asset: str
    trades: list[ClosedTrade]
    final_equity: float
    considered_setups: int
    qualified_setups: int


def _htf_series_asof(htf_full: Sequence[Candle], target_tf: str, timestamp_ms: int) -> list[Candle]:
    """Bars of htf_full that had fully closed by timestamp_ms (no lookahead into unclosed HTF bars)."""
    period_ms = _TF_MS[target_tf]
    closes = [c.timestamp + period_ms for c in htf_full]
    idx = bisect.bisect_right(closes, timestamp_ms)
    return list(htf_full[:idx])


def _matching_sweep(sweeps, mss):
    prior = [sw for sw in sweeps if sw.index <= mss.index]
    if not prior:
        return None
    return max(prior, key=lambda sw: sw.index)


DEFAULT_MSS_RETRACE_BUFFER_MULT = 0.25


def _invalidated(direction: Direction, candle: Candle, sweep, mss, atr_value: float | None,
                  mss_retrace_buffer_mult: float = DEFAULT_MSS_RETRACE_BUFFER_MULT) -> bool:
    """
    Spec S:5: price closes beyond the sweep extreme, or the MSS is fully
    retraced.

    "Fully retraced" requires the bar's CLOSE to clear mss.broken_swing.price
    (the level whose break confirmed the MSS) by a meaningful ATR-scaled
    margin, not merely graze it. A first-attempt retracement bar that dips
    into the entry zone -- which typically sits close to (often just below)
    the break level by construction -- will very often close somewhere
    near that level without having fully reclaimed it within a single 1H
    bar; treating any close-below-the-line as "fully retraced" invalidated
    ~38% of same-bar touches purely on this graze, discarding what was
    usually a normal retracement-in-progress, not a failed reversal.
    Requiring a real margin (matching how every other spec threshold in
    this codebase is ATR-scaled, e.g. sl_buffer, sweep_penetration) keeps
    this a distinct, meaningful signal from the sweep-extreme check.
    `atr_value=None` (insufficient history) disables this specific check.
    """
    if direction == Direction.BULLISH:
        wick_extreme = sweep.pool.price - sweep.penetration
        if candle.close < wick_extreme:
            return True
        if atr_value is not None and candle.close < mss.broken_swing.price - mss_retrace_buffer_mult * atr_value:
            return True
    else:
        wick_extreme = sweep.pool.price + sweep.penetration
        if candle.close > wick_extreme:
            return True
        if atr_value is not None and candle.close > mss.broken_swing.price + mss_retrace_buffer_mult * atr_value:
            return True
    return False


def _search_fill(exec_full, start_index, entry_price, direction, sweep, mss, ttl_bars, atr_series=None,
                  mss_retrace_buffer_mult: float = DEFAULT_MSS_RETRACE_BUFFER_MULT):
    """Walk forward looking for a limit fill, invalidation, or TTL timeout. Spec S:5."""
    end = min(start_index + ttl_bars, len(exec_full) - 1)
    for j in range(start_index, end + 1):
        c = exec_full[j]
        atr_value = atr_series[j] if atr_series is not None else None
        if _invalidated(direction, c, sweep, mss, atr_value, mss_retrace_buffer_mult):
            return None
        if c.low <= entry_price <= c.high:
            return j
    return None


def _manage_position(
    exec_full: Sequence[Candle],
    exec_swings,
    fill_index: int,
    asset: str,
    direction: Direction,
    grade: Grade,
    entry: float,
    initial_stop: float,
    target: float,
    qty: float,
    *,
    partial_r: float = DEFAULT_PARTIAL_R,
    partial_fraction: float = DEFAULT_PARTIAL_FRACTION,
    fee_pct: float = DEFAULT_FEE_PCT,
) -> ClosedTrade:
    """
    Manage a filled position per spec S:6: 50% off at 2R, stop to
    breakeven, trail the remainder under new confirmed swing lows (long) /
    highs (short). Uses the already-computed `exec_swings` (lookahead-safe
    by construction, S:3.1) rather than recomputing per bar.
    """
    risk_per_unit = abs(entry - initial_stop)
    partial_level = entry + partial_r * risk_per_unit if direction == Direction.BULLISH else entry - partial_r * risk_per_unit
    entry_timestamp = exec_full[fill_index].timestamp

    remaining_qty = qty
    current_stop = initial_stop
    partial_taken = False
    realized_pnl = 0.0
    fees = qty * entry * fee_pct  # entry fee, charged once upfront on full size

    last_j = fill_index
    for j in range(fill_index + 1, len(exec_full)):
        last_j = j
        c = exec_full[j]

        if direction == Direction.BULLISH:
            if c.low <= current_stop:
                exit_price = current_stop
                realized_pnl += remaining_qty * (exit_price - entry)
                fees += remaining_qty * exit_price * fee_pct
                reason = "trail_stop" if partial_taken else "stop"
                return ClosedTrade(asset, direction, grade, fill_index, entry, j, exit_price, initial_stop, target,
                                    realized_pnl - fees, realized_pnl / (qty * risk_per_unit), reason, j - fill_index, entry_timestamp)
            if not partial_taken and c.high >= partial_level:
                partial_qty = remaining_qty * partial_fraction
                realized_pnl += partial_qty * (partial_level - entry)
                fees += partial_qty * partial_level * fee_pct
                remaining_qty -= partial_qty
                partial_taken = True
                current_stop = max(current_stop, entry)  # breakeven, never loosen
            if c.high >= target:
                exit_price = target
                realized_pnl += remaining_qty * (exit_price - entry)
                fees += remaining_qty * exit_price * fee_pct
                return ClosedTrade(asset, direction, grade, fill_index, entry, j, exit_price, initial_stop, target,
                                    realized_pnl - fees, realized_pnl / (qty * risk_per_unit), "target", j - fill_index, entry_timestamp)
            if partial_taken:
                recent_lows = [s for s in exec_swings if s.kind == SwingKind.LOW and s.index > fill_index and s.confirmed_index <= j]
                if recent_lows:
                    current_stop = max(current_stop, recent_lows[-1].price)
        else:
            if c.high >= current_stop:
                exit_price = current_stop
                realized_pnl += remaining_qty * (entry - exit_price)
                fees += remaining_qty * exit_price * fee_pct
                reason = "trail_stop" if partial_taken else "stop"
                return ClosedTrade(asset, direction, grade, fill_index, entry, j, exit_price, initial_stop, target,
                                    realized_pnl - fees, realized_pnl / (qty * risk_per_unit), reason, j - fill_index, entry_timestamp)
            if not partial_taken and c.low <= partial_level:
                partial_qty = remaining_qty * partial_fraction
                realized_pnl += partial_qty * (entry - partial_level)
                fees += partial_qty * partial_level * fee_pct
                remaining_qty -= partial_qty
                partial_taken = True
                current_stop = min(current_stop, entry)
            if c.low <= target:
                exit_price = target
                realized_pnl += remaining_qty * (entry - exit_price)
                fees += remaining_qty * exit_price * fee_pct
                return ClosedTrade(asset, direction, grade, fill_index, entry, j, exit_price, initial_stop, target,
                                    realized_pnl - fees, realized_pnl / (qty * risk_per_unit), "target", j - fill_index, entry_timestamp)
            if partial_taken:
                recent_highs = [s for s in exec_swings if s.kind == SwingKind.HIGH and s.index > fill_index and s.confirmed_index <= j]
                if recent_highs:
                    current_stop = min(current_stop, recent_highs[-1].price)

    # Ran off the end of the data still open -- mark-to-market at the last close.
    exit_price = exec_full[last_j].close
    if direction == Direction.BULLISH:
        realized_pnl += remaining_qty * (exit_price - entry)
    else:
        realized_pnl += remaining_qty * (entry - exit_price)
    fees += remaining_qty * exit_price * fee_pct
    return ClosedTrade(asset, direction, grade, fill_index, entry, last_j, exit_price, initial_stop, target,
                        realized_pnl - fees, realized_pnl / (qty * risk_per_unit), "end_of_data", last_j - fill_index, entry_timestamp)


def run_backtest_single_asset(
    candles_15m: Sequence[Candle],
    asset: str,
    equity0: float,
    *,
    exec_tf: str = "1h",
    swing_n_exec: int = DEFAULT_SWING_N["1h"],
    swing_n_daily: int = DEFAULT_SWING_N["1d"],
    swing_n_weekly: int = DEFAULT_SWING_N["1w"],
    atr_period: int = 14,
    state_ttl_bars: int = DEFAULT_STATE_TTL_BARS,
    fee_pct: float = DEFAULT_FEE_PCT,
    kill_zones=DEFAULT_KILL_ZONES,
    lev_max: int = 10,
    max_concurrent: int = 1,
    daily_loss_limit_pct: float = -0.20,
    weekly_loss_limit_pct: float = -0.40,
    min_rr: float = DEFAULT_MIN_RR,
    max_bars_after_mss: int = DEFAULT_MAX_BARS_AFTER_MSS,
    min_target_atr_mult: float = DEFAULT_MIN_TARGET_ATR_MULT,
    a_plus_threshold: int = 14,
    b_threshold: int = 11,
    disp_atr_mult: float = 1.5,
    mss_retrace_buffer_mult: float = DEFAULT_MSS_RETRACE_BUFFER_MULT,
) -> BacktestResult:
    """Walk one asset's history end to end per the S:5 state machine, spec S:11."""
    exec_full = resample(candles_15m, exec_tf)
    daily_full = resample(candles_15m, "1d")
    weekly_full = resample(candles_15m, "1w")

    exec_swings = find_swings(exec_full, swing_n_exec)
    pools_full = liquidity_pools(exec_full, exec_swings, atr_period=atr_period)
    sweeps = detect_sweep(exec_full, pools_full, atr_period=atr_period)
    bos_events = detect_bos(exec_full, exec_swings)
    mss_events = detect_mss(exec_full, exec_swings, sweeps)
    fvgs = find_fvg(exec_full, atr_period=atr_period, disp_atr_mult=disp_atr_mult)
    order_blocks = find_order_blocks(exec_full, bos_events + mss_events, atr_period=atr_period, disp_atr_mult=disp_atr_mult)
    breakers = find_breakers(order_blocks, bos_events + mss_events, exec_full, atr_period=atr_period, disp_atr_mult=disp_atr_mult)
    atr_series = atr(exec_full, atr_period)

    equity = equity0
    trades: list[ClosedTrade] = []
    considered = 0
    qualified = 0
    busy_until_index = -1  # max_concurrent=1 enforced by blocking new entries while a position is open

    day_bucket = week_bucket = None
    equity_at_day_start = equity_at_week_start = equity0

    for mss in sorted(mss_events, key=lambda b: b.index):
        if mss.index <= busy_until_index:
            continue
        ref_atr = atr_series[mss.index]
        if ref_atr is None:
            continue

        ts = exec_full[mss.index].timestamp
        this_day = _bucket_start(ts, "1d")
        this_week = _bucket_start(ts, "1w")
        if this_day != day_bucket:
            day_bucket, equity_at_day_start = this_day, equity
        if this_week != week_bucket:
            week_bucket, equity_at_week_start = this_week, equity
        daily_pnl_pct = (equity - equity_at_day_start) / equity_at_day_start if equity_at_day_start else 0.0
        weekly_pnl_pct = (equity - equity_at_week_start) / equity_at_week_start if equity_at_week_start else 0.0
        if circuit_breaker_status(daily_pnl_pct, weekly_pnl_pct,
                                   daily_limit_pct=daily_loss_limit_pct, weekly_limit_pct=weekly_loss_limit_pct):
            continue  # flattened & standing down for this day/week, spec S:7

        sweep = _matching_sweep(sweeps, mss)
        if sweep is None or sweep.direction != mss.direction:
            continue

        daily_trunc = _htf_series_asof(daily_full, "1d", exec_full[mss.index].timestamp)
        weekly_trunc = _htf_series_asof(weekly_full, "1w", exec_full[mss.index].timestamp)
        if len(daily_trunc) < 2 or len(weekly_trunc) < 2:
            continue

        daily_swings = find_swings(daily_trunc, swing_n_daily)
        weekly_swings = find_swings(weekly_trunc, swing_n_weekly)
        bias = compute_bias(weekly_trunc, weekly_swings, daily_trunc, daily_swings, price=exec_full[mss.index].close)

        if bias.direction == BiasDirection.NO_TRADE:
            continue
        if (mss.direction == Direction.BULLISH) != (bias.direction == BiasDirection.LONG):
            continue

        dr = dealing_range(daily_swings, as_of_index=len(daily_trunc) - 1)
        pools_asof = liquidity_pools(exec_full, exec_swings, as_of_index=mss.index, atr_period=atr_period,
                                      daily_candles=daily_trunc, weekly_candles=weekly_trunc)
        displacement = is_displacement(exec_full, mss.index, atr_period=atr_period, disp_atr_mult=disp_atr_mult, atr_series=atr_series)
        timestamp_ms = exec_full[mss.index].timestamp

        considered += 1
        setup = build_setup(bias, sweep, mss, fvgs, order_blocks, breakers, pools_asof, dr, ref_atr,
                             displacement, timestamp_ms, equity, kill_zones=kill_zones, lev_max=lev_max,
                             min_rr=min_rr, max_bars_after_mss=max_bars_after_mss,
                             min_target_atr_mult=min_target_atr_mult,
                             a_plus_threshold=a_plus_threshold, b_threshold=b_threshold)
        if setup is None or not setup.qualified:
            continue
        qualified += 1

        size = position_size(equity, setup.entry_price, setup.stop_price, setup.grade, lev_max=lev_max)
        if size is None or not max_concurrent_ok(0, max_concurrent=max_concurrent):
            continue

        fill_index = _search_fill(exec_full, mss.index + 1, setup.entry_price, setup.direction, sweep, mss,
                                   state_ttl_bars, atr_series=atr_series, mss_retrace_buffer_mult=mss_retrace_buffer_mult)
        if fill_index is None:
            continue

        trade = _manage_position(exec_full, exec_swings, fill_index, asset, setup.direction, setup.grade,
                                  setup.entry_price, setup.stop_price, setup.target_price, size.qty,
                                  fee_pct=fee_pct)
        trades.append(trade)
        equity += trade.pnl_usd
        busy_until_index = trade.exit_index

    return BacktestResult(asset=asset, trades=trades, final_equity=equity, considered_setups=considered, qualified_setups=qualified)


def compute_metrics(trades: Sequence[ClosedTrade], equity0: float, final_equity: float) -> dict:
    """Expectancy (R), win rate, profit factor, max drawdown, avg hold time. Spec S:11."""
    if not trades:
        return {
            "trades": 0, "win_rate": None, "expectancy_r": None, "profit_factor": None,
            "max_drawdown_pct": 0.0, "avg_hold_bars": None, "total_pnl_usd": 0.0,
        }

    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    win_rate = len(wins) / len(trades)
    expectancy_r = sum(t.r_multiple for t in trades) / len(trades)
    gross_win = sum(t.pnl_usd for t in wins)
    gross_loss = -sum(t.pnl_usd for t in losses)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    avg_hold_bars = sum(t.hold_bars for t in trades) / len(trades)

    equity_curve = [equity0]
    eq = equity0
    for t in trades:
        eq += t.pnl_usd
        equity_curve.append(eq)
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)

    return {
        "trades": len(trades),
        "win_rate": win_rate,
        "expectancy_r": expectancy_r,
        "profit_factor": profit_factor,
        "max_drawdown_pct": max_dd,
        "avg_hold_bars": avg_hold_bars,
        "total_pnl_usd": final_equity - equity0,
    }
