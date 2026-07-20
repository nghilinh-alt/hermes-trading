"""
hermes_trading.ict.context -- market-context snapshot for the dashboard.

The trading path is deliberately lossy: `scan_asset` returns ONLY setups
that are fully qualified AND still pending, `continue`-ing past every
rejected candidate and discarding the whole `DetectionContext` when it
returns. That's correct for trading -- but it means the operator has no
way to see *why* nothing was taken, or what structure the worker is
currently looking at.

This module re-runs the same detection pipeline (literally the same
`build_detection_context` call the trading path makes, so what the
dashboard shows is by construction what the worker saw) and serialises a
bounded, JSON-safe snapshot: HTF bias, dealing range/OTE, the FVG /
order-block / breaker / S-R / liquidity structure near current price, and
every recent candidate setup -- qualified AND near-miss -- with its score,
grade and failed gates.

Two things here are NOT in the live trading path and are computed only
for display: `sr_zones()` (the trading path uses `liquidity_pools()` only)
and a current-bar bias (the trading path computes bias per-MSS, so a quiet
asset would otherwise have no bias at all).

PURE AND READ-ONLY BY DESIGN. No broker calls, no file I/O, no mutation of
anything passed in. The caller (tools/run_ict_live.py) is responsible for
writing the result and for isolating any failure -- a dashboard data bug
must never be able to affect trading.
"""
from __future__ import annotations

from typing import Sequence

from hermes_trading.ict.backtest import (
    DEFAULT_MSS_RETRACE_BUFFER_MULT,
    DEFAULT_STATE_TTL_BARS,
    DEFAULT_SWING_N,
    _htf_series_asof,
    _matching_sweep,
    resolve_setup_status,
)
from hermes_trading.ict.bias import compute_bias, dealing_range, premium_discount
from hermes_trading.ict.imbalance import is_displacement
from hermes_trading.ict.liquidity import liquidity_pools, sr_zones
from hermes_trading.ict.risk import position_size
from hermes_trading.ict.scanner import build_detection_context
from hermes_trading.ict.setup import (
    DEFAULT_KILL_ZONES,
    DEFAULT_MAX_BARS_AFTER_MSS,
    DEFAULT_MIN_RR,
    DEFAULT_MIN_TARGET_ATR_MULT,
    DEFAULT_OTE_HIGH,
    DEFAULT_OTE_LOW,
    _ote_price_band,
    _zone_bounds,
    build_setup,
)
from hermes_trading.ict.structure import find_swings
from hermes_trading.ict.types import BiasDirection, Direction, Grade
from hermes_trading.ict.util import Candle

# Display bounds. Over a 2-year cache there are thousands of FVGs and order
# blocks, almost all of them at prices nowhere near the current market --
# these keep context.json to a few KB and the dashboard readable.
ZONE_ATR_RANGE = 3.0     # FVG / OB / breaker: within +/- N x ATR of price
LEVEL_ATR_RANGE = 5.0    # S/R zones and liquidity pools: wider, they're reference levels
EVENT_LOOKBACK_BARS = 40  # sweeps / MSS events shown as "recent"
MAX_CANDIDATES = 5
MAX_ZONES_PER_KIND = 8
MAX_EVENTS_PER_KIND = 10

_CONTEXT_VERSION = 1


def _f(value):
    """None-safe float coercion -- keeps json.dumps happy and the schema stable."""
    return None if value is None else float(value)


def _near(price: float, lo: float, hi: float, atr_value: float, mult: float) -> bool:
    """True if [lo, hi] comes within `mult` x ATR of `price` (overlap counts)."""
    if atr_value is None or atr_value <= 0:
        return True
    window = mult * atr_value
    return lo <= price + window and hi >= price - window


def build_market_context(
    candles_15m: Sequence[Candle],
    asset: str,
    equity: float,
    *,
    as_of_index: int | None = None,
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
    ote_low: float = DEFAULT_OTE_LOW,
    ote_high: float = DEFAULT_OTE_HIGH,
) -> dict:
    """
    Build a JSON-serialisable market-context snapshot for one asset.

    Accepts the same `**scan_params` the live worker passes to
    `run_full_cycle`, so the caller can forward `SCAN_PARAMS` unchanged and
    the snapshot is guaranteed to reflect the parameters actually trading.
    Unknown keys are tolerated (see `context_kwargs`) rather than raising --
    this runs in a worker whose primary job is trading, and an unexpected
    kwarg must not be able to take the cycle down.

    Returns a dict with an "error" key (and nothing else meaningful) only
    if there isn't enough history to say anything.
    """
    ctx = build_detection_context(candles_15m, exec_tf=exec_tf, swing_n_exec=swing_n_exec,
                                  atr_period=atr_period, disp_atr_mult=disp_atr_mult)
    exec_full = ctx.exec_full
    if not exec_full:
        return {"schema_version": _CONTEXT_VERSION, "asset": asset, "error": "no candle history"}

    if as_of_index is None:
        as_of_index = len(exec_full) - 1
    now_bar = exec_full[as_of_index]
    price = now_bar.close
    ref_atr = ctx.atr_series[as_of_index]

    out: dict = {
        "schema_version": _CONTEXT_VERSION,
        "asset": asset,
        "exec_tf": exec_tf,
        "last_bar_ts": now_bar.timestamp,
        "price": _f(price),
        "atr": _f(ref_atr),
        "equity": _f(equity),
        "bias": None,
        "dealing_range": None,
        "zones": {"fvg": [], "order_blocks": [], "breakers": [], "sr": [], "liquidity": []},
        "recent_events": {"sweeps": [], "mss": []},
        "candidates": [],
        "gate_summary": {},
    }

    # ── HTF bias at the CURRENT bar ───────────────────────────────────────
    # Not computed anywhere in the trading path (which only derives bias
    # per-MSS), so on a quiet asset this is the only bias figure that exists.
    daily_trunc = _htf_series_asof(ctx.daily_full, "1d", now_bar.timestamp)
    weekly_trunc = _htf_series_asof(ctx.weekly_full, "1w", now_bar.timestamp)
    daily_swings = find_swings(daily_trunc, swing_n_daily) if len(daily_trunc) >= 2 else []
    weekly_swings = find_swings(weekly_trunc, swing_n_weekly) if len(weekly_trunc) >= 2 else []

    dr = None
    if len(daily_trunc) >= 2 and len(weekly_trunc) >= 2:
        bias = compute_bias(weekly_trunc, weekly_swings, daily_trunc, daily_swings, price=price)
        out["bias"] = {
            "direction": bias.direction.value,
            "weekly_trend": bias.weekly_trend.value,
            "daily_trend": bias.daily_trend.value,
            "reason": bias.reason,
        }
        dr = dealing_range(daily_swings, as_of_index=len(daily_trunc) - 1)

    if dr is not None:
        # OTE is direction-relative, so it only has a defined band once a
        # bias direction exists. Fall back to the long band for display when
        # the bias is NO_TRADE, and say so via "ote_for".
        bias_dir = out["bias"]["direction"] if out["bias"] else "no_trade"
        disp_dir = Direction.BEARISH if bias_dir == "short" else Direction.BULLISH
        band = _ote_price_band(dr, disp_dir, ote_low, ote_high)
        pd = premium_discount(price, dr, direction=disp_dir, ote_low=ote_low, ote_high=ote_high)
        out["dealing_range"] = {
            "low": _f(dr.low),
            "high": _f(dr.high),
            "retracement_pct": _f(pd.retracement_pct),
            "zone": pd.zone.value,
            "in_ote": bool(pd.in_ote),
            "ote_band": [_f(band[0]), _f(band[1])],
            # OTE is direction-relative, so the band shown is the one for the
            # current bias. With no bias direction we display the long band.
            "ote_for": "short" if disp_dir == Direction.BEARISH else "long",
        }

    # ── Structure near price ──────────────────────────────────────────────
    def _ts(i: int):
        return exec_full[i].timestamp if 0 <= i < len(exec_full) else None

    for fvg in ctx.fvgs:
        if fvg.index > as_of_index:
            continue
        if fvg.mitigated_index is not None and fvg.mitigated_index <= as_of_index:
            continue
        if not _near(price, fvg.low, fvg.high, ref_atr, ZONE_ATR_RANGE):
            continue
        out["zones"]["fvg"].append({
            "low": _f(fvg.low), "high": _f(fvg.high), "kind": fvg.kind.value,
            "bar_ts": _ts(fvg.index), "displacement": bool(fvg.displacement), "mitigated": False,
        })

    for ob in ctx.order_blocks:
        if ob.break_index > as_of_index:
            continue
        if ob.mitigated_index is not None and ob.mitigated_index <= as_of_index:
            continue
        if not _near(price, ob.low, ob.high, ref_atr, ZONE_ATR_RANGE):
            continue
        out["zones"]["order_blocks"].append({
            "low": _f(ob.low), "high": _f(ob.high), "kind": ob.kind.value,
            "bar_ts": _ts(ob.index), "break_bar_ts": _ts(ob.break_index), "mitigated": False,
        })

    for brk in ctx.breakers:
        ob = brk.order_block
        if brk.flip_index > as_of_index:
            continue
        if brk.mitigated_index is not None and brk.mitigated_index <= as_of_index:
            continue
        if not _near(price, ob.low, ob.high, ref_atr, ZONE_ATR_RANGE):
            continue
        out["zones"]["breakers"].append({
            "low": _f(ob.low), "high": _f(ob.high), "kind": brk.kind.value,
            "bar_ts": _ts(brk.flip_index), "mitigated": False,
        })

    # S/R zones -- built and tested, but never called in the live trading
    # path (which uses liquidity_pools only). Display-only.
    try:
        zones = sr_zones(exec_full, ctx.exec_swings, atr_period=atr_period, as_of_index=as_of_index)
    except Exception:
        zones = []
    for z in zones:
        if not _near(price, z.price_low, z.price_high, ref_atr, LEVEL_ATR_RANGE):
            continue
        out["zones"]["sr"].append({
            "price_low": _f(z.price_low), "price_high": _f(z.price_high),
            "kind": z.kind.value, "touches": int(z.touches), "strength": _f(z.strength),
        })

    pools = liquidity_pools(exec_full, ctx.exec_swings, as_of_index=as_of_index, atr_period=atr_period,
                            daily_candles=daily_trunc, weekly_candles=weekly_trunc)
    for p in pools:
        if not _near(price, p.price, p.price, ref_atr, LEVEL_ATR_RANGE):
            continue
        out["zones"]["liquidity"].append({
            "price": _f(p.price), "kind": p.kind.value, "source": p.source.value, "bar_ts": _ts(p.index),
        })

    # Keep only the zones closest to price -- the ladder can't usefully
    # render more than a handful per kind anyway.
    def _dist_zone(z):
        lo = z.get("low", z.get("price_low", z.get("price")))
        hi = z.get("high", z.get("price_high", z.get("price")))
        return 0.0 if lo <= price <= hi else min(abs(price - lo), abs(price - hi))

    for kind in out["zones"]:
        out["zones"][kind] = sorted(out["zones"][kind], key=_dist_zone)[:MAX_ZONES_PER_KIND]

    # ── Recent sweeps / MSS ───────────────────────────────────────────────
    event_floor = as_of_index - EVENT_LOOKBACK_BARS
    # Every confirmed swing is a liquidity pool, so a single displacement bar
    # routinely sweeps dozens of them at once -- a raw list runs to hundreds
    # of near-identical entries. Collapse to one row per (bar, direction),
    # keeping the deepest penetration as the representative level.
    by_bar: dict[tuple, dict] = {}
    for s in ctx.sweeps:
        if not (event_floor <= s.index <= as_of_index):
            continue
        key = (s.index, s.direction.value)
        existing = by_bar.get(key)
        if existing is None or s.penetration > existing["penetration"]:
            by_bar[key] = {
                "bar_ts": _ts(s.index), "pool_price": _f(s.pool.price), "pool_kind": s.pool.kind.value,
                "source": s.pool.source.value, "penetration": _f(s.penetration),
                "direction": s.direction.value, "pools_swept": 0,
            }
        by_bar[key]["pools_swept"] += 1
    out["recent_events"]["sweeps"] = sorted(
        by_bar.values(), key=lambda e: e["bar_ts"] or 0, reverse=True
    )[:MAX_EVENTS_PER_KIND]

    for m in ctx.mss_events:
        if event_floor <= m.index <= as_of_index:
            out["recent_events"]["mss"].append({
                "bar_ts": _ts(m.index), "direction": m.direction.value,
                "broken_swing_price": _f(m.broken_swing.price), "close": _f(m.close),
            })
    out["recent_events"]["mss"] = sorted(
        out["recent_events"]["mss"], key=lambda e: e["bar_ts"] or 0, reverse=True
    )[:MAX_EVENTS_PER_KIND]

    # ── Candidate setups: qualified AND near-miss ─────────────────────────
    # Same lookback window scan_asset uses, so the qualified subset here is
    # exactly the set scan_asset would have alerted on.
    lookback = state_ttl_bars + max_bars_after_mss + 1
    relevant = [m for m in ctx.mss_events if as_of_index - lookback <= m.index <= as_of_index]
    gate_counts: dict[str, int] = {}

    for mss in sorted(relevant, key=lambda b: b.index, reverse=True):
        if len(out["candidates"]) >= MAX_CANDIDATES:
            break
        mss_atr = ctx.atr_series[mss.index]
        if mss_atr is None:
            continue
        sweep = _matching_sweep(ctx.sweeps, mss)
        if sweep is None or sweep.direction != mss.direction:
            continue

        ts = exec_full[mss.index].timestamp
        d_trunc = _htf_series_asof(ctx.daily_full, "1d", ts)
        w_trunc = _htf_series_asof(ctx.weekly_full, "1w", ts)
        if len(d_trunc) < 2 or len(w_trunc) < 2:
            continue
        d_swings = find_swings(d_trunc, swing_n_daily)
        w_swings = find_swings(w_trunc, swing_n_weekly)
        c_bias = compute_bias(w_trunc, w_swings, d_trunc, d_swings, price=exec_full[mss.index].close)
        if c_bias.direction == BiasDirection.NO_TRADE:
            # build_setup returns None here, so there's no setup to describe.
            # Still worth surfacing as a rejection reason.
            gate_counts["htf_bias"] = gate_counts.get("htf_bias", 0) + 1
            continue

        c_dr = dealing_range(d_swings, as_of_index=len(d_trunc) - 1)
        pools_asof = liquidity_pools(exec_full, ctx.exec_swings, as_of_index=mss.index, atr_period=atr_period,
                                     daily_candles=d_trunc, weekly_candles=w_trunc)
        disp = is_displacement(exec_full, mss.index, atr_period=atr_period,
                               disp_atr_mult=disp_atr_mult, atr_series=ctx.atr_series)

        setup = build_setup(c_bias, sweep, mss, ctx.fvgs, ctx.order_blocks, ctx.breakers, pools_asof,
                            c_dr, mss_atr, disp, ts, equity, kill_zones=kill_zones, lev_max=lev_max,
                            min_rr=min_rr, max_bars_after_mss=max_bars_after_mss,
                            min_target_atr_mult=min_target_atr_mult,
                            a_plus_threshold=a_plus_threshold, b_threshold=b_threshold)
        if setup is None:
            continue

        for gate in setup.gate_failures:
            gate_counts[gate] = gate_counts.get(gate, 0) + 1

        status = "unknown"
        if setup.entry_price is not None:
            status, _ = resolve_setup_status(
                exec_full, mss.index + 1, setup.entry_price, setup.direction, sweep, mss,
                state_ttl_bars, as_of_index, atr_series=ctx.atr_series,
                mss_retrace_buffer_mult=mss_retrace_buffer_mult,
            )

        zone_lo = zone_hi = None
        if setup.entry_zone is not None and setup.entry_zone_kind is not None:
            zone_lo, zone_hi = _zone_bounds(setup.entry_zone, setup.entry_zone_kind)

        size = None
        if setup.entry_price is not None and setup.stop_price is not None and setup.grade != Grade.NONE:
            ps = position_size(equity, setup.entry_price, setup.stop_price, setup.grade, lev_max=lev_max)
            if ps is not None:
                size = {"qty": _f(ps.qty), "leverage": int(ps.leverage),
                        "notional": _f(ps.notional), "risk_usd": _f(ps.risk_usd),
                        "stop_pct": _f(ps.stop_pct)}

        bars_since = as_of_index - mss.index
        out["candidates"].append({
            "mss_ts": ts,
            "direction": setup.direction.value,
            "status": status,
            "qualified": bool(setup.qualified),
            "score": int(setup.score),
            "grade": setup.grade.value,
            "gate_failures": list(setup.gate_failures),
            "entry_zone": (
                {"kind": setup.entry_zone_kind.value, "low": _f(zone_lo), "high": _f(zone_hi)}
                if zone_lo is not None else None
            ),
            "entry_price": _f(setup.entry_price),
            "stop_price": _f(setup.stop_price),
            "target_price": _f(setup.target_price),
            "rr": _f(setup.rr),
            "swept_level": _f(sweep.pool.price),
            "mss_level": _f(mss.broken_swing.price),
            "displacement": bool(disp),
            "bars_since_mss": bars_since,
            "bars_until_expiry": max(0, state_ttl_bars - bars_since),
            "size": size,
        })

    # Qualified first, then most recent -- the dashboard renders in this order.
    out["candidates"].sort(key=lambda c: (not c["qualified"], -c["mss_ts"]))
    out["gate_summary"] = gate_counts
    return out


_CONTEXT_KWARG_NAMES = {
    "exec_tf", "swing_n_exec", "swing_n_daily", "swing_n_weekly", "atr_period",
    "state_ttl_bars", "kill_zones", "lev_max", "min_rr", "max_bars_after_mss",
    "min_target_atr_mult", "a_plus_threshold", "b_threshold", "disp_atr_mult",
    "mss_retrace_buffer_mult", "ote_low", "ote_high",
}


def context_kwargs(scan_params: dict) -> dict:
    """
    Filter a live worker's SCAN_PARAMS down to what build_market_context
    accepts, so the caller can forward its params verbatim without a
    TypeError if the two ever drift apart. Mirrors live._detection_kwargs.
    """
    return {k: v for k, v in scan_params.items() if k in _CONTEXT_KWARG_NAMES}
