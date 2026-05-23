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


def _evaluate_entry(strategy: dict, price_data: dict, macro_data: dict, news_data: dict) -> bool:
    """Returns True if entry conditions are met."""
    entry = strategy.get("entry", {})
    indicator = entry.get("indicator", "rsi")
    threshold = float(entry.get("threshold", 30))
    direction = entry.get("direction", "long")
    ema_period = entry.get("ema_period")  # optional EMA trend filter

    rsi_signal = False
    if indicator == "rsi":
        rsi = price_data.get("rsi_14", 50.0)
        if direction == "long" and rsi < threshold:
            rsi_signal = True
        elif direction == "short" and rsi > threshold:
            rsi_signal = True

    if not rsi_signal:
        return False

    price = price_data.get("price", 0)

    # EMA trend filter: only enter if price is on the right side of the EMA
    if ema_period == 9:
        ema = price_data.get("ema_9")
        if ema is not None:
            if direction == "long" and price < ema:
                return False   # price below EMA-9 — no uptrend confirmation
            if direction == "short" and price > ema:
                return False

    # Bollinger Band filter: long only when price touches or breaks below lower band
    if entry.get("bb_filter"):
        bb_lower = price_data.get("bb_lower")
        bb_upper = price_data.get("bb_upper")
        if bb_lower is not None:
            if direction == "long" and price > bb_lower:
                return False   # not at the lower band — wait for the squeeze
            if direction == "short" and price < bb_upper:
                return False

    return True


def _execute_trade(strategy: dict, price_data: dict) -> dict:
    """Dispatch to live execution or paper simulator based on HERMES_TRADING_MODE."""
    if os.getenv("HERMES_TRADING_MODE", "paper") == "live":
        from hermes_trading.adapters import execution as execution_adapter
        return execution_adapter.place_live_trade(strategy, price_data)
    return _simulate_paper_trade(strategy, price_data)


def _simulate_paper_trade(strategy: dict, price_data: dict) -> dict:
    """Generate a paper trade record (no real money moves)."""
    entry_price = price_data.get("price", 0)
    stop_loss_pct = float(strategy.get("stop_loss_pct", 2.0)) / 100
    position_size_r = float(strategy.get("position_size_r", 0.5))
    direction = strategy.get("entry", {}).get("direction", "long")

    # Simulate a very short hold: assume 0.1–0.5% move for paper purposes
    import random
    move_pct = random.uniform(-stop_loss_pct, stop_loss_pct * 1.5)
    pnl_pct = move_pct * position_size_r if direction == "long" else -move_pct * position_size_r

    exit_price = entry_price * (1 + move_pct)

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": os.getenv("HERMES_TRADING_MODE", "paper"),
        "asset": price_data.get("asset", "BTC/USDT"),
        "direction": direction,
        "entry_price": entry_price,
        "exit_price": round(exit_price, 4),
        "pnl_pct": round(pnl_pct, 6),
        "strategy_version": strategy.get("version", "01"),
        "rsi_at_entry": price_data.get("rsi_14"),
        "ema_9_at_entry": price_data.get("ema_9"),
        "bb_lower_at_entry": price_data.get("bb_lower"),
        "bb_upper_at_entry": price_data.get("bb_upper"),
    }


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

            ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
            if _evaluate_entry(strategy, price_data, macro_data or {}, news_data or {}):
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

                trade = _execute_trade(strategy, price_data)
                _log_trade(state_dir, trade)
                closed_count = _count_closed_trades(state_dir)
                pnl_display = f"{trade['pnl_pct']:+.4%}" if trade.get("pnl_pct") is not None else "pending"
                console.print(f"[green]{ts} {tag} Trade #{closed_count}: {trade['direction']} @ {trade['entry_price']} → pnl {pnl_display}[/green]")
                _maybe_trigger_reflection(resolved_goal, closed_count, state_dir)
            else:
                rsi_val = price_data.get("rsi_14", "?")
                console.print(f"[dim]{ts} {tag} No entry · RSI={rsi_val} · price={price_data.get('price')}[/dim]")

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
