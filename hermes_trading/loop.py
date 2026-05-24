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


def _check_indicator(name: str, params: dict, direction: str, price_data: dict) -> bool | None:
    """
    Evaluate a single named indicator against price_data.
    Returns True (signal fires), False (signal does not fire), or None (data unavailable).
    """
    price = price_data.get("price", 0)

    if name == "rsi":
        rsi = price_data.get("rsi_14")
        if rsi is None:
            return None
        threshold = float(params.get("threshold", 30))
        if direction == "long":
            return rsi < threshold
        return rsi > threshold

    if name == "ema_trend":
        period = int(params.get("period", 50))
        ema = price_data.get(f"ema_{period}")
        if ema is None:
            return None
        if direction == "long":
            return price > ema
        return price < ema

    if name == "macd":
        macd_line   = price_data.get("macd_line")
        macd_signal = price_data.get("macd_signal")
        if macd_line is None or macd_signal is None:
            return None
        if direction == "long":
            return macd_line > macd_signal   # bullish crossover territory
        return macd_line < macd_signal

    if name == "vwap":
        vwap = price_data.get("vwap")
        if vwap is None:
            return None
        if direction == "long":
            return price < vwap   # price below VWAP — mean-reversion long
        return price > vwap

    if name == "volume_spike":
        ratio     = price_data.get("volume_ratio")
        if ratio is None:
            return None
        min_ratio = float(params.get("min_ratio", 1.5))
        return ratio >= min_ratio

    if name == "bb_squeeze":
        bb_lower = price_data.get("bb_lower")
        bb_upper = price_data.get("bb_upper")
        if bb_lower is None or bb_upper is None:
            return None
        if direction == "long":
            return price <= bb_lower
        return price >= bb_upper

    if name == "fvg":
        # Price retracing into a Fair Value Gap (1h candles)
        if direction == "long":
            low  = price_data.get("fvg_bull_low")
            high = price_data.get("fvg_bull_high")
        else:
            low  = price_data.get("fvg_bear_low")
            high = price_data.get("fvg_bear_high")
        if low is None or high is None:
            return None
        return low <= price <= high

    if name == "order_block":
        # Price touching a bullish/bearish Order Block zone (1h candles)
        tolerance = float(params.get("tolerance_pct", 0.5)) / 100
        if direction == "long":
            low  = price_data.get("ob_bull_low")
            high = price_data.get("ob_bull_high")
        else:
            low  = price_data.get("ob_bear_low")
            high = price_data.get("ob_bear_high")
        if low is None or high is None:
            return None
        # Allow a small tolerance above/below the zone
        return low * (1 - tolerance) <= price <= high * (1 + tolerance)

    if name == "sr_zone":
        # Price near a support (long) or resistance (short) level from 1h/4h swing points
        tolerance = float(params.get("tolerance_pct", 1.0)) / 100
        if direction == "long":
            level = price_data.get("support_1h4h")
        else:
            level = price_data.get("resistance_1h4h")
        if level is None:
            return None
        return abs(price - level) / level <= tolerance

    return None  # unknown indicator — treat as unavailable


def _evaluate_entry(strategy: dict, price_data: dict, macro_data: dict, news_data: dict) -> dict:
    """
    Modular weighted indicator registry.

    Each indicator in strategy['indicators'] has:
      required (bool)  — if True, failure blocks the trade outright
      weight   (float) — contribution toward optional confidence score
      params   (dict)  — indicator-specific config

    Returns a dict:
      fires            (bool)              — whether entry condition is met
      confidence       (float)             — optional-indicator confidence score 0–1
      indicators_fired (dict[str, bool|None]) — per-indicator result (None = no data)
    """
    indicators       = strategy.get("indicators", [])
    direction        = strategy.get("entry", {}).get("direction", "long")
    min_conf         = float(strategy.get("entry", {}).get("min_confidence", 0.0))
    indicators_fired: dict[str, bool | None] = {}

    def _result(fires: bool, confidence: float = 0.0) -> dict:
        return {"fires": fires, "confidence": round(confidence, 4), "indicators_fired": indicators_fired}

    # Fallback: if no indicator registry defined, replicate original RSI behaviour
    if not indicators:
        entry     = strategy.get("entry", {})
        rsi       = price_data.get("rsi_14", 50.0)
        threshold = float(entry.get("threshold", 30))
        fired     = rsi < threshold if direction == "long" else rsi > threshold
        indicators_fired["rsi"] = fired
        return _result(fired, 1.0 if fired else 0.0)

    optional_total  = sum(float(i.get("weight", 1.0)) for i in indicators if not i.get("required", False))
    optional_passed = 0.0
    fires           = True

    for ind in indicators:
        name     = ind.get("name", "")
        params   = ind.get("params", {})
        required = ind.get("required", False)
        weight   = float(ind.get("weight", 1.0))

        result = _check_indicator(name, params, direction, price_data)
        indicators_fired[name] = result   # True / False / None (no data)

        if result is None:
            continue   # data unavailable — skip gracefully

        if required and not result:
            fires = False   # hard gate failed — keep evaluating to log all indicators

        if not required and result:
            optional_passed += weight

    # Check optional confidence threshold
    confidence = optional_passed / optional_total if optional_total > 0 else 1.0
    if fires and optional_total > 0 and min_conf > 0 and confidence < min_conf:
        fires = False

    return _result(fires, confidence)


def _execute_trade(strategy: dict, price_data: dict, entry_detail: dict) -> dict:
    """Dispatch to live execution or paper simulator based on HERMES_TRADING_MODE."""
    if os.getenv("HERMES_TRADING_MODE", "paper") == "live":
        from hermes_trading.adapters import execution as execution_adapter
        return execution_adapter.place_live_trade(strategy, price_data, entry_detail)
    return _simulate_paper_trade(strategy, price_data, entry_detail)


def _simulate_paper_trade(strategy: dict, price_data: dict, entry_detail: dict) -> dict:
    """Generate a paper trade record (no real money moves)."""
    import random

    entry_price     = price_data.get("price", 0)
    stop_loss_pct   = float(strategy.get("stop_loss_pct", 2.0)) / 100
    position_size_r = float(strategy.get("position_size_r", 0.5))
    direction       = strategy.get("entry", {}).get("direction", "long")

    move_pct  = random.uniform(-stop_loss_pct, stop_loss_pct * 1.5)
    pnl_pct   = move_pct * position_size_r if direction == "long" else -move_pct * position_size_r
    exit_price = entry_price * (1 + move_pct)

    return {
        "ts":               datetime.now(timezone.utc).isoformat(),
        "mode":             os.getenv("HERMES_TRADING_MODE", "paper"),
        "asset":            price_data.get("asset", "BTC/USDT"),
        "direction":        direction,
        "entry_price":      entry_price,
        "exit_price":       round(exit_price, 4),
        "pnl_pct":          round(pnl_pct, 6),
        "strategy_version": strategy.get("version", "01"),
        # Full indicator snapshot at entry — used by reflect.py for richer learning
        "indicators_snapshot": _snapshot_indicators(price_data),
        "indicators_fired":    entry_detail.get("indicators_fired", {}),
        "confidence_at_entry": entry_detail.get("confidence"),
    }


def _reconcile_open_trades(asset: str, state_dir: Path) -> None:
    """
    In live mode: if a trade is recorded as open (pnl_pct=None) but Bybit
    shows no open position, fetch the closed PnL and update trades.jsonl.
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
        if execution_adapter.has_open_position(asset):
            return  # position still live — nothing to reconcile
        closed = execution_adapter.fetch_last_closed_pnl(asset)
        if not closed:
            return
        # Update the most recent open trade with real exit data
        idx = open_indices[-1]
        trades[idx]["exit_price"]      = closed["exit_price"]
        trades[idx]["pnl_pct"]         = closed["pnl_pct"]
        trades[idx]["closed_pnl_usdt"] = closed["closed_pnl_usdt"]
        trades_file.write_text("\n".join(json.dumps(t) for t in trades) + "\n")
        console.print(
            f"[cyan][{asset}] Reconciled: exit={closed['exit_price']} "
            f"pnl={closed['pnl_pct']:+.4%}[/cyan]"
        )
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
                ["python", "-m", "hermes_trading.reflect", reflect_mode, "--state-dir", str(state_dir)],
                check=True,
                timeout=120,
            )
        except Exception as e:
            console.print(f"[red]Reflection failed: {e}[/red]")


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

            ts           = datetime.now(timezone.utc).strftime("%H:%M UTC")
            entry_result = _evaluate_entry(strategy, price_data, macro_data or {}, news_data or {})

            if entry_result["fires"]:
                # Live mode: skip if already in a position for this asset
                if os.getenv("HERMES_TRADING_MODE", "paper") == "live":
                    from hermes_trading.adapters import execution as execution_adapter
                    if execution_adapter.has_open_position(asset):
                        console.print(f"[dim]{ts} {tag} Entry signal — position already open, skipping[/dim]")
                        _write_heartbeat(state_dir, "ok", 0)
                        consecutive_failures = 0
                        elapsed = time.monotonic() - tick_start
                        await asyncio.sleep(max(0, 300 - elapsed))
                        continue

                trade = _execute_trade(strategy, price_data, entry_result)
                _log_trade(state_dir, trade)
                closed_count = _count_closed_trades(state_dir)
                pnl_display  = f"{trade['pnl_pct']:+.4%}" if trade.get("pnl_pct") is not None else "pending"
                fired_names  = [k for k, v in entry_result.get("indicators_fired", {}).items() if v]
                console.print(
                    f"[green]{ts} {tag} Trade #{closed_count}: {trade['direction']} "
                    f"@ {trade['entry_price']} · conf={entry_result['confidence']:.0%} "
                    f"· fired={fired_names} · pnl {pnl_display}[/green]"
                )
                _maybe_trigger_reflection(resolved_goal, closed_count, state_dir)
            else:
                rsi_val   = price_data.get("rsi_14", "?")
                conf_val  = entry_result.get("confidence", 0)
                console.print(
                    f"[dim]{ts} {tag} No entry · RSI={rsi_val} · "
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

        # Tick every 5 minutes (matches the 5m candle timeframe)
        elapsed = time.monotonic() - tick_start
        sleep_for = max(0, 300 - elapsed)
        await asyncio.sleep(sleep_for)
