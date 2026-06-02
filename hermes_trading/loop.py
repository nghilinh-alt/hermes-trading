"""
loop.py — the 24/7 async trading loop.

Every minute:
  1. Pull data from all four adapters (with per-adapter retries + circuit breaker)
  2. Evaluate the current strategy in state/strategy.yaml
  3. If entry condition fires → paper trade
  4. Log outcome to state/trades.jsonl
  5. Write heartbeat to state/heartbeat.json
  6. Check reflection cadence; if N trades closed, run reflect --fallback or --hermes
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml
from rich.console import Console

from hermes_trading.adapters import price as price_adapter
from hermes_trading.adapters import onchain as onchain_adapter
from hermes_trading.adapters import news as news_adapter
from hermes_trading.adapters import macro as macro_adapter

console = Console()

_BASE_STATE_DIR = Path(os.getenv("STATE_DIR", "state"))


def _timeframe_to_seconds(tf: str) -> int:
    """Convert a ccxt-style timeframe string to seconds. e.g. '15m' → 900, '1h' → 3600."""
    _map = {"m": 60, "h": 3600, "d": 86400}
    try:
        unit = tf[-1].lower()
        return int(tf[:-1]) * _map.get(unit, 60)
    except Exception:
        return 900   # default to 15m if unparseable

MAX_CONSECUTIVE_FAILURES = 5
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 2  # seconds

# All indicator keys captured from price_data at entry — extend here when new
# indicators are added to adapters/price.py
_INDICATOR_SNAPSHOT_KEYS: list[str] = [
    "rsi_14",
    "ema_9", "ema_50",
    "bb_upper", "bb_mid", "bb_lower",
    "macd_line", "macd_signal", "macd_hist",
    "atr_14",
    "vwap",
    "volume_ratio",
    "fvg_bull_low", "fvg_bull_high",
    "fvg_bear_low", "fvg_bear_high",
    "ob_bull_low",  "ob_bull_high",
    "ob_bear_low",  "ob_bear_high",
    "support_1h4h", "resistance_1h4h",
]


def _snapshot_indicators(price_data: dict) -> dict:
    """Capture a clean snapshot of all indicator values from price_data."""
    return {k: price_data.get(k) for k in _INDICATOR_SNAPSHOT_KEYS}


def _entry_gates_snapshot(strategy: dict) -> dict:
    """
    Snapshot of all entry-time gates from strategy.yaml at the moment of decision.
    Embedded in trade records so audits show what limits were in force when
    the bot decided to take the trade.
    """
    entry = strategy.get("entry", {}) or {}
    return {
        "min_confidence":   float(entry.get("min_confidence", 0.0)),
        "min_indicators":   int(entry.get("min_indicators", 1)),
        "direction_config": entry.get("direction", "long"),
        "max_sl_pct":       float(strategy.get("max_sl_pct", 5.0)),
        "min_tp_pct":       float(strategy.get("min_tp_pct", 3.0)),
        "min_rr_ratio":     float(strategy.get("min_rr_ratio", 2.0)),
        "min_profit_usd":   float(strategy.get("min_profit_usd", 5.0)),
        "risk_per_trade":   float(strategy.get("risk_per_trade", 0.10)),
        "default_leverage": int(strategy.get("default_leverage", 5) or 5),
        "sl_buffer_pct":    float(strategy.get("sl_buffer_pct", 0.3)),
        "strategy_version": str(strategy.get("version", "01")),
    }


def _infer_close_reason(exit_price, tp_price, sl_price, tolerance_pct: float = 0.5) -> str:
    """
    Best-effort attribution of why a trade closed.

    Compares the realised exit price against the originally-set TP and SL levels.
    Tolerance is a percentage of the level (default 0.5%) to account for slippage.
    Returns: 'TP_hit' | 'SL_hit' | 'manual_or_other' | 'unknown'.
    """
    if exit_price is None:
        return "unknown"
    tol = tolerance_pct / 100
    try:
        if tp_price is not None and abs(float(exit_price) - float(tp_price)) / float(tp_price) <= tol:
            return "TP_hit"
        if sl_price is not None and abs(float(exit_price) - float(sl_price)) / float(sl_price) <= tol:
            return "SL_hit"
    except (TypeError, ValueError, ZeroDivisionError):
        return "unknown"
    return "manual_or_other"


async def _fetch_with_retry(adapter, asset: str, name: str) -> dict | None:
    """Fetch from an adapter with exponential backoff. Returns None on total failure."""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return await adapter.fetch(asset)
        except adapter.SchemaError as e:
            console.print(f"[red][{name}] SchemaError (halting loop): {e}[/red]")
            raise
        except Exception as e:
            delay = RETRY_BASE_DELAY ** attempt
            console.print(f"[yellow][{name}] attempt {attempt} failed: {e}. Retrying in {delay}s[/yellow]")
            if attempt < RETRY_ATTEMPTS:
                await asyncio.sleep(delay)
            else:
                console.print(f"[red][{name}] all {RETRY_ATTEMPTS} attempts failed[/red]")
                return None


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _count_closed_trades(state_dir: Path) -> int:
    trades_file = state_dir / "trades.jsonl"
    if not trades_file.exists():
        return 0
    return sum(1 for line in trades_file.read_text().splitlines() if line.strip())


def _write_heartbeat(state_dir: Path, status: str, consecutive_failures: int) -> None:
    (state_dir / "heartbeat.json").write_text(json.dumps({
        "status": status,
        "last_tick": datetime.now(timezone.utc).isoformat(),
        "consecutive_failures": consecutive_failures,
    }))


def _log_trade(state_dir: Path, trade: dict) -> None:
    with open(state_dir / "trades.jsonl", "a") as f:
        f.write(json.dumps(trade) + "\n")


def _check_indicator(name: str, params: dict, direction: str, price_data: dict) -> dict:
    """
    Evaluate a single named indicator against price_data.

    Returns a dict for audit-quality logging:
      result      (bool | None) — True (signal fires), False (no fire), None (no data)
      value       (any)         — what was observed (e.g. RSI=33, price=70200)
      threshold   (any)         — what was compared against
      comparator  (str)         — short text of the test (e.g. "rsi<30")

    Callers wanting only the bool can read result.
    """
    price = price_data.get("price", 0)

    def _d(result, value, threshold, comparator):
        return {"result": result, "value": value, "threshold": threshold, "comparator": comparator}

    if name == "rsi":
        rsi = price_data.get("rsi_14")
        threshold = float(params.get("threshold", 30))
        if rsi is None:
            return _d(None, None, threshold, f"rsi<{threshold}" if direction == "long" else f"rsi>{threshold}")
        if direction == "long":
            return _d(rsi < threshold, rsi, threshold, f"rsi<{threshold}")
        return _d(rsi > threshold, rsi, threshold, f"rsi>{threshold}")

    if name == "ema_trend":
        period = int(params.get("period", 50))
        ema = price_data.get(f"ema_{period}")
        if ema is None:
            return _d(None, None, period, f"price vs ema_{period}")
        if direction == "long":
            return _d(price > ema, price, ema, f"price>ema_{period}")
        return _d(price < ema, price, ema, f"price<ema_{period}")

    if name == "macd":
        macd_line   = price_data.get("macd_line")
        macd_signal = price_data.get("macd_signal")
        if macd_line is None or macd_signal is None:
            return _d(None, None, None, "macd_line vs macd_signal")
        if direction == "long":
            return _d(macd_line > macd_signal, macd_line, macd_signal, "macd_line>macd_signal")
        return _d(macd_line < macd_signal, macd_line, macd_signal, "macd_line<macd_signal")

    if name == "vwap":
        vwap = price_data.get("vwap")
        if vwap is None:
            return _d(None, None, None, "price vs vwap")
        if direction == "long":
            return _d(price < vwap, price, vwap, "price<vwap (mean-revert long)")
        return _d(price > vwap, price, vwap, "price>vwap (mean-revert short)")

    if name == "volume_spike":
        ratio = price_data.get("volume_ratio")
        min_ratio = float(params.get("min_ratio", 1.5))
        if ratio is None:
            return _d(None, None, min_ratio, f"volume_ratio>={min_ratio}")
        return _d(ratio >= min_ratio, ratio, min_ratio, f"volume_ratio>={min_ratio}")

    if name == "bb_squeeze":
        bb_lower = price_data.get("bb_lower")
        bb_upper = price_data.get("bb_upper")
        if bb_lower is None or bb_upper is None:
            return _d(None, None, None, "price vs bb_lower/upper")
        if direction == "long":
            return _d(price <= bb_lower, price, bb_lower, "price<=bb_lower")
        return _d(price >= bb_upper, price, bb_upper, "price>=bb_upper")

    if name == "fvg":
        if direction == "long":
            low, high = price_data.get("fvg_bull_low"), price_data.get("fvg_bull_high")
            zone = "fvg_bull"
        else:
            low, high = price_data.get("fvg_bear_low"), price_data.get("fvg_bear_high")
            zone = "fvg_bear"
        if low is None or high is None:
            return _d(None, None, None, f"price within {zone}")
        return _d(low <= price <= high, price, [low, high], f"price within {zone}[{low},{high}]")

    if name == "order_block":
        tolerance = float(params.get("tolerance_pct", 0.5)) / 100
        if direction == "long":
            low, high = price_data.get("ob_bull_low"), price_data.get("ob_bull_high")
            zone = "ob_bull"
        else:
            low, high = price_data.get("ob_bear_low"), price_data.get("ob_bear_high")
            zone = "ob_bear"
        if low is None or high is None:
            return _d(None, None, None, f"price within {zone}")
        return _d(
            low * (1 - tolerance) <= price <= high * (1 + tolerance),
            price, [low, high],
            f"price within {zone}[{low},{high}] (+/-{tolerance*100:.1f}%)",
        )

    if name == "sr_zone":
        tolerance = float(params.get("tolerance_pct", 1.0)) / 100
        if direction == "long":
            level = price_data.get("support_1h4h")
            level_name = "support_1h4h"
        else:
            level = price_data.get("resistance_1h4h")
            level_name = "resistance_1h4h"
        if level is None:
            return _d(None, None, None, f"price near {level_name}")
        return _d(
            abs(price - level) / level <= tolerance, price, level,
            f"|price-{level_name}|/{level_name}<={tolerance*100:.1f}%",
        )

    return _d(None, None, None, f"unknown indicator: {name}")


def _evaluate_entry(
    strategy: dict,
    price_data: dict,
    macro_data: dict,
    news_data: dict,
    force_direction: str | None = None,
) -> dict:
    """
    Modular weighted indicator registry.

    Each indicator in strategy['indicators'] has:
      required (bool)  — if True, failure blocks the trade outright (legacy; prefer min_indicators)
      weight   (float) — contribution toward optional confidence score
      params   (dict)  — indicator-specific config

    strategy['entry'] may include:
      min_confidence  (float) — minimum weighted confidence score required (0–1)
      min_indicators  (int)   — minimum number of indicators that must fire (default 1)

    force_direction overrides strategy['entry']['direction'] — used when evaluating
    both long and short in the same tick.

    Returns a dict:
      fires            (bool)              — whether entry condition is met
      confidence       (float)             — weighted confidence score 0–1
      indicators_fired (dict[str, bool|None]) — per-indicator result (None = no data)
      direction        (str)               — the direction evaluated ("long" or "short")
    """
    indicators = strategy.get("indicators", [])
    entry      = strategy.get("entry", {})
    direction  = force_direction or entry.get("direction", "long")
    min_conf   = float(entry.get("min_confidence", 0.0))
    min_ind    = int(entry.get("min_indicators", 1))
    indicators_fired: dict[str, bool | None] = {}
    confidence_breakdown: dict[str, dict] = {}

    def _result(fires: bool, confidence: float = 0.0, summary: str = "") -> dict:
        return {
            "fires":                fires,
            "confidence":           round(confidence, 4),
            "indicators_fired":     indicators_fired,
            "direction":            direction,
            "confidence_breakdown": confidence_breakdown,
            "evaluation_summary":   summary,
        }

    # Fallback: if no indicator registry defined, use simple RSI check
    if not indicators:
        rsi       = price_data.get("rsi_14", 50.0)
        threshold = float(entry.get("threshold", 30))
        fired     = rsi < threshold if direction == "long" else rsi > threshold
        indicators_fired["rsi"] = fired
        confidence_breakdown["rsi"] = {
            "fired":              fired,
            "required":           True,
            "weight":             1.0,
            "weight_contributed": 1.0 if fired else 0.0,
            "value":              rsi,
            "threshold":          threshold,
            "comparator":         f"rsi<{threshold}" if direction == "long" else f"rsi>{threshold}",
        }
        summary = f"{direction.upper()} fallback: RSI={rsi:.2f} vs threshold {threshold} -> {'FIRE' if fired else 'NO-FIRE'}"
        return _result(fired, 1.0 if fired else 0.0, summary)

    optional_total  = sum(float(i.get("weight", 1.0)) for i in indicators if not i.get("required", False))
    optional_passed = 0.0
    fires           = True

    for ind in indicators:
        name     = ind.get("name", "")
        params   = ind.get("params", {})
        required = ind.get("required", False)
        weight   = float(ind.get("weight", 1.0))

        detail = _check_indicator(name, params, direction, price_data)
        result = detail["result"]
        indicators_fired[name] = result   # True / False / None (no data)

        weight_contributed = 0.0
        if result is True and not required:
            optional_passed += weight
            weight_contributed = weight

        confidence_breakdown[name] = {
            "fired":              result,
            "required":           required,
            "weight":             weight,
            "weight_contributed": weight_contributed,
            "value":              detail.get("value"),
            "threshold":          detail.get("threshold"),
            "comparator":         detail.get("comparator", ""),
        }

        if required and result is False:
            fires = False   # hard gate failed — keep evaluating to log all indicators

    # Weighted confidence from optional indicators
    confidence = optional_passed / optional_total if optional_total > 0 else 1.0

    # Gate 1: minimum confidence threshold
    skip_reasons = []
    if fires and optional_total > 0 and min_conf > 0 and confidence < min_conf:
        fires = False
        skip_reasons.append(f"confidence {confidence:.2%} < min {min_conf:.2%}")

    # Gate 2: minimum indicator count (count of optional indicators that fired)
    fired_optional_count = sum(
        1 for ind in indicators
        if not ind.get("required", False) and indicators_fired.get(ind.get("name")) is True
    )
    if fires and fired_optional_count < min_ind:
        fires = False
        skip_reasons.append(f"fired {fired_optional_count} < min_indicators {min_ind}")

    # Build a human-readable evaluation summary
    fired_names   = [n for n, r in indicators_fired.items() if r is True]
    nofire_names  = [n for n, r in indicators_fired.items() if r is False]
    nodata_names  = [n for n, r in indicators_fired.items() if r is None]
    parts = [direction.upper()]
    if fired_names:   parts.append(f"fired={fired_names}")
    if nofire_names:  parts.append(f"no-fire={nofire_names}")
    if nodata_names:  parts.append(f"no-data={nodata_names}")
    parts.append(f"conf={confidence:.2%}")
    if skip_reasons:  parts.append("SKIP: " + "; ".join(skip_reasons))
    else:             parts.append("FIRE" if fires else "NO-FIRE")
    summary = " | ".join(parts)

    return _result(fires, confidence, summary)


def _execute_trade(strategy: dict, price_data: dict, entry_detail: dict) -> dict | None:
    """
    Dispatch to live execution or paper simulator based on HERMES_TRADING_MODE.

    Returns None (and logs a skip message) if the structural SL is too wide —
    this lets the loop continue normally rather than crashing the tick.
    """
    if os.getenv("HERMES_TRADING_MODE", "paper") == "live":
        from hermes_trading.adapters import execution as execution_adapter
        try:
            return execution_adapter.place_live_trade(strategy, price_data, entry_detail)
        except ValueError as e:
            asset = price_data.get("asset", "?")
            console.print(f"[yellow][{asset}] Entry skipped — {e}[/yellow]")
            return None
    return _simulate_paper_trade(strategy, price_data, entry_detail)


def _simulate_paper_trade(strategy: dict, price_data: dict, entry_detail: dict) -> dict:
    """
    Generate a paper trade record (no real money moves).

    Uses structural SL/TP from price_data when available (matching live behaviour).
    Falls back to fixed stop_loss_pct when structural levels are absent.
    """
    import random
    from hermes_trading.adapters.execution import _structural_sl_tp

    entry_price     = float(price_data.get("price", 0))
    position_size_r = float(strategy.get("position_size_r", 0.5))
    risk_per_trade  = float(strategy.get("risk_per_trade", 0.10))
    # Prefer the resolved direction from evaluation (supports direction:both)
    direction = entry_detail.get("direction") or strategy.get("entry", {}).get("direction", "long")

    # Attempt structural SL/TP (same logic as live); fall back to fixed %
    try:
        sl_price, tp_price = _structural_sl_tp(price_data, direction, strategy)
    except ValueError:
        # SL too wide — skip (matches live behaviour)
        asset = price_data.get("asset", "?")
        console.print(f"[yellow][{asset}] Paper entry skipped — structural SL too wide[/yellow]")
        raise   # re-raise so caller can handle it (same as live path returning None)

    sl_dist_pct = abs(entry_price - sl_price) / entry_price if entry_price else 0.02
    tp_dist     = abs(tp_price - entry_price)
    sl_dist_abs = abs(entry_price - sl_price)
    rr_ratio    = round(tp_dist / sl_dist_abs, 2) if sl_dist_abs > 0 else None

    # Simulate a random outcome between SL and TP
    move_pct   = random.uniform(-sl_dist_pct, sl_dist_pct * (rr_ratio or 2.0))
    pnl_pct    = move_pct * (1 / sl_dist_pct) * risk_per_trade if direction == "long" \
                 else -move_pct * (1 / sl_dist_pct) * risk_per_trade
    exit_price = entry_price * (1 + move_pct)

    return {
        "ts":               datetime.now(timezone.utc).isoformat(),
        "mode":             os.getenv("HERMES_TRADING_MODE", "paper"),
        "asset":            price_data.get("asset", "BTC/USDT"),
        "direction":        direction,
        "entry_price":      entry_price,
        "exit_price":       round(exit_price, 4),
        "pnl_pct":          round(pnl_pct, 6),
        "sl_price":         sl_price,
        "tp_price":         tp_price,
        "rr_ratio":         rr_ratio,
        "strategy_version": strategy.get("version", "01"),
        # Full indicator snapshot at entry — used by reflect.py for richer learning
        "indicators_snapshot":  _snapshot_indicators(price_data),
        "indicators_fired":     entry_detail.get("indicators_fired", {}),
        "confidence_at_entry":  entry_detail.get("confidence"),
        # Audit additions (session 6, 2026-05-28): WHY the trade was opened
        "confidence_breakdown": entry_detail.get("confidence_breakdown", {}),
        "evaluation_summary":   entry_detail.get("evaluation_summary", ""),
        "entry_gates":          _entry_gates_snapshot(strategy),
        # Paper trades resolve immediately — close reason inferred from sim outcome
        "close_reason":         _infer_close_reason(round(exit_price, 4), tp_price, sl_price),
    }


def _reconcile_open_trades(asset: str, state_dir: Path) -> None:
    """
    In live mode: reconcile trades.jsonl against actual Bybit position state.

    Rules:
      - If a live position exists  → keep the most recent open trade as-is;
        mark all earlier open trades as abandoned (pnl_pct=0, abandoned=True).
      - If no live position exists → fetch the most recent closed PnL from Bybit,
        update the most recent open trade with it, and mark all earlier open
        trades as abandoned.

    This ensures trades.jsonl never accumulates permanent "open" ghost records
    from previous agent runs.
    """
    if os.getenv("HERMES_TRADING_MODE", "paper") != "live":
        return
    trades_file = state_dir / "trades.jsonl"
    if not trades_file.exists():
        return

    lines = [l.strip() for l in trades_file.read_text().splitlines() if l.strip()]
    trades = []
    for l in lines:
        try:
            trades.append(json.loads(l))
        except Exception:
            pass

    open_indices = [i for i, t in enumerate(trades) if t.get("pnl_pct") is None]
    if not open_indices:
        return

    try:
        from hermes_trading.adapters import execution as execution_adapter
        has_position = execution_adapter.has_open_position(asset)

        # Always abandon all open trades EXCEPT the most recent one
        stale_indices = open_indices[:-1]
        for idx in stale_indices:
            trades[idx]["pnl_pct"]      = 0.0
            trades[idx]["exit_price"]   = trades[idx].get("entry_price")
            trades[idx]["abandoned"]    = True
            trades[idx]["close_reason"] = "abandoned_stale"

        if has_position:
            # Live position matches the most recent open trade — leave it open
            pass
        else:
            # No live position — reconcile the most recent open trade with actual PnL
            closed = execution_adapter.fetch_last_closed_pnl(asset)
            most_recent_idx = open_indices[-1]
            if closed:
                trades[most_recent_idx]["exit_price"]      = closed["exit_price"]
                trades[most_recent_idx]["pnl_pct"]         = closed["pnl_pct"]
                trades[most_recent_idx]["closed_pnl_usdt"] = closed.get("closed_pnl_usdt")
                trades[most_recent_idx]["close_reason"]    = _infer_close_reason(
                    closed.get("exit_price"),
                    trades[most_recent_idx].get("tp_price"),
                    trades[most_recent_idx].get("sl_price"),
                )
                console.print(
                    f"[cyan][{asset}] Reconciled: exit={closed['exit_price']} "
                    f"pnl={closed['pnl_pct']:+.4%} reason={trades[most_recent_idx]['close_reason']}[/cyan]"
                )
            else:
                # No closed PnL available — mark as abandoned with zero PnL
                trades[most_recent_idx]["pnl_pct"]      = 0.0
                trades[most_recent_idx]["exit_price"]   = trades[most_recent_idx].get("entry_price")
                trades[most_recent_idx]["abandoned"]    = True
                trades[most_recent_idx]["close_reason"] = "abandoned_no_pnl"

        if stale_indices:
            console.print(f"[cyan][{asset}] Abandoned {len(stale_indices)} stale open record(s)[/cyan]")

        trades_file.write_text("\n".join(json.dumps(t) for t in trades) + "\n")

    except Exception as e:
        console.print(f"[yellow][{asset}] Reconcile error: {e}[/yellow]")


def _maybe_trigger_reflection(goal: dict, trade_count: int, state_dir: Path) -> None:
    """Run reflect when the cadence threshold is crossed."""
    cadence = int(goal.get("reflection_every", 5))
    if trade_count > 0 and trade_count % cadence == 0:
        mode = os.getenv("HERMES_TRADING_MODE", "paper")
        reflect_mode = "--hermes" if mode == "live" else "--fallback"
        console.print(f"[bold cyan]{state_dir.name}: reflection at {trade_count} trades → {reflect_mode}[/bold cyan]")
        try:
            subprocess.run(
                [sys.executable, "-m", "hermes_trading.reflect", reflect_mode, "--state-dir", str(state_dir)],
                check=True,
                timeout=120,
            )
        except Exception as e:
            console.print(f"[red]Reflection failed: {e}[/red]")


def _maybe_update_trailing_stops(asset: str, price_data: dict, strategy: dict) -> None:
    """Check open positions and advance the SL if the trailing condition is met.

    Activation: price must have moved >= 1R in favour (i.e. unrealised PnL >= SL distance).
    Trail distance: 2 * ATR_14 behind current mark price.
    SL only ever moves in the favourable direction (never against the position).
    Skipped silently in paper mode.
    """
    if os.getenv("HERMES_TRADING_MODE", "paper") != "live":
        return

    atr = float(price_data.get("atr_14") or 0)
    if atr <= 0:
        return

    trail_mult = float(strategy.get("trail_atr_mult", 2.0))
    trail_dist = atr * trail_mult

    from hermes_trading.adapters import execution as execution_adapter
    try:
        positions = execution_adapter.fetch_open_positions_with_marks(asset)
    except RuntimeError as e:
        console.print(f"[yellow]Trail SL fetch failed ({asset}): {e}[/yellow]")
        return

    for pos in positions:
        entry      = pos["entry_price"]
        mark       = pos["mark_price"]
        current_sl = pos["sl_price"]
        direction  = pos["direction"]

        if entry <= 0 or mark <= 0 or current_sl is None:
            continue

        sl_dist = abs(entry - current_sl)
        if sl_dist <= 0:
            continue

        if direction == "long":
            unrealised_r = (mark - entry) / sl_dist
            new_sl       = round(mark - trail_dist, 4)
            # Activate only after 1R profit; never move SL below current SL
            if unrealised_r < 1.0 or new_sl <= current_sl:
                continue
        else:  # short
            unrealised_r = (entry - mark) / sl_dist
            new_sl       = round(mark + trail_dist, 4)
            if unrealised_r < 1.0 or new_sl >= current_sl:
                continue

        try:
            execution_adapter.update_trailing_stop(asset, direction, new_sl)
            console.print(
                f"[cyan]Trail SL {asset} {direction}: "
                f"{current_sl} → {new_sl} "
                f"(mark={mark}, ATR={atr:.3f}, R={unrealised_r:.2f})[/cyan]"
            )
        except RuntimeError as e:
            console.print(f"[yellow]Trail SL update failed ({asset}): {e}[/yellow]")

async def run(asset: str, goal: dict | None = None, state_dir: Path | None = None) -> None:
    """Main async loop — runs forever.

    *goal*      pre-resolved dict from run.py. Falls back to reading goal.yaml if None.
    *state_dir* per-asset state directory (e.g. state/btc_usdt/). Falls back to state/.
    """
    state_dir = state_dir or _BASE_STATE_DIR
    strategy_file = state_dir / "strategy.yaml"
    tag = f"[{asset}]"

    console.print(f"[bold green]Booting hermes-trading worker · {asset} · {state_dir} · mode={os.getenv('HERMES_TRADING_MODE','paper')}[/bold green]")

    consecutive_failures = 0

    while True:
        tick_start = time.monotonic()
        try:
            resolved_goal = goal if goal is not None else _load_yaml(_BASE_STATE_DIR / "goal.yaml")
            strategy = _load_yaml(strategy_file)

            results = await asyncio.gather(
                _fetch_with_retry(price_adapter, asset, "price"),
                _fetch_with_retry(onchain_adapter, asset, "onchain"),
                _fetch_with_retry(news_adapter, asset, "news"),
                _fetch_with_retry(macro_adapter, asset, "macro"),
                return_exceptions=False,
            )
            price_data, onchain_data, news_data, macro_data = results

            if price_data is None:
                raise RuntimeError("price adapter returned None — cannot evaluate strategy")

            # Trailing stop: advance SL on open positions before evaluating new entry
            if os.getenv("HERMES_TRADING_MODE", "paper") == "live":
                _maybe_update_trailing_stops(asset, price_data, strategy)

            ts = datetime.now(timezone.utc).strftime("%H:%M UTC")

            # Evaluate entry — support direction:both (long and short simultaneously)
            strategy_direction = strategy.get("entry", {}).get("direction", "long")
            if strategy_direction == "both":
                long_result  = _evaluate_entry(strategy, price_data, macro_data or {}, news_data or {}, force_direction="long")
                short_result = _evaluate_entry(strategy, price_data, macro_data or {}, news_data or {}, force_direction="short")
                if long_result["fires"] and short_result["fires"]:
                    # Both sides signal — take the higher confidence; skip if too close (ambiguous)
                    if abs(long_result["confidence"] - short_result["confidence"]) < 0.1:
                        entry_result = {"fires": False, "confidence": 0.0, "indicators_fired": {}, "direction": "both"}
                        console.print(f"[dim]{ts} [{asset}] Ambiguous signal (long {long_result['confidence']:.0%} vs short {short_result['confidence']:.0%}) — skipping[/dim]")
                    elif long_result["confidence"] >= short_result["confidence"]:
                        entry_result = long_result
                    else:
                        entry_result = short_result
                elif long_result["fires"]:
                    entry_result = long_result
                elif short_result["fires"]:
                    entry_result = short_result
                else:
                    best_conf    = max(long_result["confidence"], short_result["confidence"])
                    entry_result = {"fires": False, "confidence": best_conf, "indicators_fired": {}, "direction": "both"}
            else:
                entry_result = _evaluate_entry(strategy, price_data, macro_data or {}, news_data or {})

            if entry_result["fires"]:
                # Live mode: skip if already in a position for this asset
                if os.getenv("HERMES_TRADING_MODE", "paper") == "live":
                    from hermes_trading.adapters import execution as execution_adapter
                    if execution_adapter.has_open_position(asset):
                        console.print(f"[dim]{ts} {tag} Entry signal — position already open, skipping[/dim]")
                        _write_heartbeat(state_dir, "ok", 0)
                        consecutive_failures = 0
                        elapsed      = time.monotonic() - tick_start
                        tick_seconds = _timeframe_to_seconds(os.getenv("HERMES_TIMEFRAME", "15m"))
                        await asyncio.sleep(max(0, tick_seconds - elapsed))
                        continue

                try:
                    trade = _execute_trade(strategy, price_data, entry_result)
                except ValueError:
                    # Paper mode re-raises ValueError when structural SL is too wide — skip gracefully
                    trade = None

                if trade is None:
                    # Entry skipped (structural SL too wide) — treat as no-entry tick
                    pass
                else:
                    _log_trade(state_dir, trade)
                    closed_count = _count_closed_trades(state_dir)
                    pnl_display  = f"{trade['pnl_pct']:+.4%}" if trade.get("pnl_pct") is not None else "pending"
                    rr_display   = f" · RR={trade['rr_ratio']}" if trade.get("rr_ratio") else ""
                    fired_names  = [k for k, v in entry_result.get("indicators_fired", {}).items() if v]
                    console.print(
                        f"[green]{ts} {tag} Trade #{closed_count}: {trade['direction']} "
                        f"@ {trade['entry_price']} · conf={entry_result['confidence']:.0%} "
                        f"· fired={fired_names} · pnl {pnl_display}{rr_display}[/green]"
                    )
                    _maybe_trigger_reflection(resolved_goal, closed_count, state_dir)
            else:
                rsi_val  = price_data.get("rsi_14", "?")
                conf_val = entry_result.get("confidence", 0)
                dir_val  = entry_result.get("direction", strategy_direction)
                console.print(
                    f"[dim]{ts} {tag} No entry · dir={dir_val} · RSI={rsi_val} · "
                    f"conf={conf_val:.0%} · price={price_data.get('price')}[/dim]"
                )

            _reconcile_open_trades(asset, state_dir)
            consecutive_failures = 0
            _write_heartbeat(state_dir, "ok", 0)

        except Exception as e:
            consecutive_failures += 1
            console.print(f"[red]{tag} Loop error (failure {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): {e}[/red]")
            traceback.print_exc()
            _write_heartbeat(state_dir, "error", consecutive_failures)

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                console.print(f"[bold red]{tag} Circuit breaker tripped — sleeping 10 minutes.[/bold red]")
                await asyncio.sleep(600)
                consecutive_failures = 0

        # Tick interval matches the candle timeframe so we evaluate once per new candle
        elapsed        = time.monotonic() - tick_start
        tick_seconds   = _timeframe_to_seconds(os.getenv("HERMES_TIMEFRAME", "15m"))
        sleep_for      = max(0, tick_seconds - elapsed)
        await asyncio.sleep(sleep_for)
               