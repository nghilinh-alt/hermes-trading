"""
hermes_trading.ict.scanner -- bar-close detection + alert schema. Spec S:12.

Alert-only: no order placement, no position tracking, no state beyond
"which MSS events have already been alerted." Reuses every Phase 1/2
building block exactly as the backtest engine does, so what this alerts
on live matches what the (already-tested) backtest logic would have
called qualified at the same point -- proven directly by
tests/ict/test_scanner.py rather than assumed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from hermes_trading.ict.backtest import (
    DEFAULT_FEE_PCT,  # noqa: F401  (re-exported for callers building a full pipeline)
    DEFAULT_MSS_RETRACE_BUFFER_MULT,
    DEFAULT_STATE_TTL_BARS,
    DEFAULT_SWING_N,
    _htf_series_asof,
    _matching_sweep,
    resample,
    resolve_setup_status,
)
from hermes_trading.ict.bias import compute_bias, dealing_range
from hermes_trading.ict.imbalance import find_breakers, find_fvg, find_order_blocks, is_displacement
from hermes_trading.ict.liquidity import detect_sweep, liquidity_pools
from hermes_trading.ict.setup import (
    DEFAULT_KILL_ZONES,
    DEFAULT_MAX_BARS_AFTER_MSS,
    DEFAULT_MIN_RR,
    DEFAULT_MIN_TARGET_ATR_MULT,
    _zone_bounds,
    build_setup,
)
from hermes_trading.ict.structure import detect_bos, detect_mss, find_swings
from hermes_trading.ict.types import BiasDirection, Direction, Grade, StructureBreak, Sweep
from hermes_trading.ict.util import Candle, atr


@dataclass(frozen=True)
class DetectionContext:
    """Shared output of the resample -> swings -> sweeps -> MSS -> FVG/OB/breaker -> ATR pipeline."""

    exec_full: list[Candle]
    daily_full: list[Candle]
    weekly_full: list[Candle]
    exec_swings: list
    sweeps: list
    mss_events: list
    fvgs: list
    order_blocks: list
    breakers: list
    atr_series: list


def build_detection_context(
    candles_15m: Sequence[Candle],
    *,
    exec_tf: str = "1h",
    swing_n_exec: int = DEFAULT_SWING_N["1h"],
    atr_period: int = 14,
    disp_atr_mult: float = 1.5,
) -> DetectionContext:
    """
    Compute the shared detection pipeline once. Pulled out of `scan_asset`
    (pure extraction, no behavior change -- `scan_asset`'s own test suite
    is the acceptance bar) so `hermes_trading.ict.live` can re-locate an
    already-resting order's `Sweep`/`StructureBreak` each cycle via
    `locate_pending_setup` without re-deriving detection logic in a second
    place.
    """
    exec_full = resample(candles_15m, exec_tf)
    if not exec_full:
        # Matches the original scan_asset's short-circuit -- avoids feeding
        # empty series into liquidity_pools/atr, which index as_of_index=-1
        # against an equally-empty ATR series and raise IndexError.
        return DetectionContext(
            exec_full=[], daily_full=[], weekly_full=[], exec_swings=[], sweeps=[],
            mss_events=[], fvgs=[], order_blocks=[], breakers=[], atr_series=[],
        )
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

    return DetectionContext(
        exec_full=exec_full, daily_full=daily_full, weekly_full=weekly_full,
        exec_swings=exec_swings, sweeps=sweeps, mss_events=mss_events,
        fvgs=fvgs, order_blocks=order_blocks, breakers=breakers, atr_series=atr_series,
    )


def locate_pending_setup(ctx: DetectionContext, mss_timestamp: int) -> tuple[Sweep, StructureBreak] | None:
    """
    Re-locate the Sweep/StructureBreak for a specific MSS bar by timestamp
    in a freshly (re)computed DetectionContext. Detection is deterministic
    and lookahead-safe, so the same historical MSS reappears identically
    every cycle -- this lets a caller that only persisted a timestamp
    (e.g. a resting live order) recover the objects `resolve_setup_status`
    needs without keeping the detection pipeline's output alive itself.
    Returns None if the MSS isn't found or its matching sweep doesn't
    agree in direction (mirrors `scan_asset`'s own skip condition).
    """
    for mss in ctx.mss_events:
        if ctx.exec_full[mss.index].timestamp != mss_timestamp:
            continue
        sweep = _matching_sweep(ctx.sweeps, mss)
        if sweep is None or sweep.direction != mss.direction:
            return None
        return sweep, mss
    return None


@dataclass(frozen=True)
class Alert:
    """Spec S:12's alert schema, plus score/grade (informative extras)."""

    asset: str
    tf: str
    direction: Direction
    state: str  # always "ENTRY_ARMED" for now -- the only state this scanner emits
    entry_zone: tuple[float, float]
    stop: float
    target: float
    rr: float
    checklist: dict
    score: int
    grade: Grade
    swept_level: float
    mss_level: float
    timestamp: int  # epoch ms of the MSS bar; also the dedup key callers should track


def scan_asset(
    candles_15m: Sequence[Candle],
    asset: str,
    equity: float,
    *,
    exec_tf: str = "1h",
    swing_n_exec: int = DEFAULT_SWING_N["1h"],
    swing_n_daily: int = DEFAULT_SWING_N["1d"],
    swing_n_weekly: int = DEFAULT_SWING_N["1w"],
    atr_period: int = 14,
    state_ttl_bars: int = DEFAULT_STATE_TTL_BARS,
    kill_zones=DEFAULT_KILL_ZONES,
    lev_max: int = 10,
    min_rr: float = DEFAULT_MIN_RR,
    max_bars_after_mss: int = DEFAULT_MAX_BARS_AFTER_MSS,
    min_target_atr_mult: float = DEFAULT_MIN_TARGET_ATR_MULT,
    a_plus_threshold: int = 14,
    b_threshold: int = 11,
    disp_atr_mult: float = 1.5,
    mss_retrace_buffer_mult: float = DEFAULT_MSS_RETRACE_BUFFER_MULT,
    as_of_index: int | None = None,
    already_alerted: set[int] | None = None,
) -> list[Alert]:
    """
    Detect currently-pending (ENTRY_ARMED, not yet filled/invalidated/
    expired) qualified setups for one asset, as of `as_of_index` (default:
    the most recent bar). Mirrors `run_backtest_single_asset`'s per-MSS
    pipeline exactly, just without the fill-simulation/position-management
    tail -- this function only detects and grades, it never assumes a fill
    happened. `already_alerted` is a set of Alert.timestamp values already
    emitted for this asset; matching MSS events are skipped so a still-
    pending setup isn't re-alerted every scan cycle.

    All default parameters match `run_backtest_single_asset`'s so a scan at
    the last bar of a historical series reproduces exactly what the
    backtest engine would have called a qualified, still-open setup at
    that point (spec S:12's "also used live" reusability, verified in
    tests/ict/test_scanner.py).
    """
    ctx = build_detection_context(candles_15m, exec_tf=exec_tf, swing_n_exec=swing_n_exec,
                                   atr_period=atr_period, disp_atr_mult=disp_atr_mult)
    exec_full = ctx.exec_full
    if not exec_full:
        return []
    if as_of_index is None:
        as_of_index = len(exec_full) - 1
    daily_full = ctx.daily_full
    weekly_full = ctx.weekly_full
    exec_swings = ctx.exec_swings
    sweeps = ctx.sweeps
    mss_events = ctx.mss_events
    fvgs = ctx.fvgs
    order_blocks = ctx.order_blocks
    breakers = ctx.breakers
    atr_series = ctx.atr_series

    already_alerted = already_alerted or set()
    alerts: list[Alert] = []

    # Only MSS events recent enough to still be inside their TTL window could
    # possibly be "pending" -- anything older is guaranteed already resolved
    # (filled/invalidated/expired) by construction of resolve_setup_status's
    # own TTL bound, so skip re-evaluating the full history every cycle.
    lookback = state_ttl_bars + max_bars_after_mss + 1
    relevant_mss = [m for m in mss_events if m.index <= as_of_index and m.index >= as_of_index - lookback]

    for mss in sorted(relevant_mss, key=lambda b: b.index):
        ts = exec_full[mss.index].timestamp
        if ts in already_alerted:
            continue

        ref_atr = atr_series[mss.index]
        if ref_atr is None:
            continue

        sweep = _matching_sweep(sweeps, mss)
        if sweep is None or sweep.direction != mss.direction:
            continue

        daily_trunc = _htf_series_asof(daily_full, "1d", ts)
        weekly_trunc = _htf_series_asof(weekly_full, "1w", ts)
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

        setup = build_setup(bias, sweep, mss, fvgs, order_blocks, breakers, pools_asof, dr, ref_atr,
                             displacement, ts, equity, kill_zones=kill_zones, lev_max=lev_max,
                             min_rr=min_rr, max_bars_after_mss=max_bars_after_mss,
                             min_target_atr_mult=min_target_atr_mult,
                             a_plus_threshold=a_plus_threshold, b_threshold=b_threshold)
        if setup is None or not setup.qualified:
            continue

        status, _ = resolve_setup_status(exec_full, mss.index + 1, setup.entry_price, setup.direction, sweep, mss,
                                          state_ttl_bars, as_of_index, atr_series=atr_series,
                                          mss_retrace_buffer_mult=mss_retrace_buffer_mult)
        if status != "pending":
            continue

        lo, hi = _zone_bounds(setup.entry_zone, setup.entry_zone_kind)
        alerts.append(Alert(
            asset=asset,
            tf=exec_tf,
            direction=setup.direction,
            state="ENTRY_ARMED",
            entry_zone=(lo, hi),
            stop=setup.stop_price,
            target=setup.target_price,
            rr=setup.rr,
            checklist={
                "htf_bias": True, "liquidity_event": True, "mss": True,
                "entry_zone": True, "rr": True, "session": True, "risk_filter": True,
            },
            score=setup.score,
            grade=setup.grade,
            swept_level=sweep.pool.price,
            mss_level=mss.broken_swing.price,
            timestamp=ts,
        ))

    return alerts
