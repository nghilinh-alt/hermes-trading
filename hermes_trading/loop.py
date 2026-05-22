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

STATE_DIR   = Path(os.getenv("STATE_DIR", "state"))
TRADES_FILE = STATE_DIR / "trades.jsonl"
HB_FILE     = STATE_DIR / "heartbeat.json"
STRATEGY_FILE = STATE_DIR / "strategy.yaml"
GOAL_FILE   = STATE_DIR / "goal.yaml"

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


def _count_closed_trades() -> int:
    if not TRADES_FILE.exists():
        return 0
    return sum(1 for line in TRADES_FILE.read_text().splitlines() if line.strip())


def _write_heartbeat(status: str, consecutive_failures: int):
    HB_FILE.write_text(json.dumps({
        "status": status,
        "last_tick": datetime.now(timezone.utc).isoformat(),
        "consecutive_failures": consecutive_failures,
    }))


def _log_trade(trade: dict):
    with open(TRADES_FILE, "a") as f:
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


def _maybe_trigger_reflection(goal: dict, trade_count: int):
    """Run reflect when the cadence threshold is crossed."""
    cadence = int(goal.get("reflection_every", 5))
    if trade_count > 0 and trade_count % cadence == 0:
        mode = os.getenv("HERMES_TRADING_MODE", "paper")
        reflect_mode = "--hermes" if mode == "live" else "--fallback"
        console.print(f"[bold cyan]Reflection trigger at {trade_count} trades → running reflect {reflect_mode}[/bold cyan]")
        try:
            subprocess.run(
                ["python", "-m", "hermes_trading.reflect", reflect_mode],
                check=True,
                timeout=120,
            )
        except Exception as e:
            console.print(f"[red]Reflection failed: {e}[/red]")


async def run(asset: str, goal: dict | None = None):
    """Main async loop — runs forever.

    *goal* is the pre-resolved dict from run.py (_resolve_goal).
    If not provided the loop falls back to reading GOAL_FILE each tick.
    """
    console.print(f"[bold green]Booting hermes-trading worker · asset={asset} · mode={os.getenv('HERMES_TRADING_MODE','paper')}[/bold green]")

    consecutive_failures = 0

    while True:
        tick_start = time.monotonic()
        try:
            # Reload goal each tick only if not pre-resolved (supports live edits)
            resolved_goal = goal if goal is not None else _load_yaml(GOAL_FILE)
            strategy = _load_yaml(STRATEGY_FILE)

            # Fetch all adapters concurrently
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

            # Evaluate and potentially trade
            if _evaluate_entry(strategy, price_data, macro_data or {}, news_data or {}):
                trade = _simulate_paper_trade(strategy, price_data)
                _log_trade(trade)
                closed_count = _count_closed_trades()
                console.print(f"[green]Trade #{closed_count}: {trade['direction']} @ {trade['entry_price']} → pnl {trade['pnl_pct']:+.4%}[/green]")
                _maybe_trigger_reflection(resolved_goal, closed_count)
            else:
                rsi_val = price_data.get("rsi_14", "?")
                console.print(f"[dim]No entry · RSI={rsi_val} · price={price_data.get('price')}[/dim]")

            consecutive_failures = 0
            _write_heartbeat("ok", 0)

        except Exception as e:
            consecutive_failures += 1
            console.print(f"[red]Loop error (failure {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): {e}[/red]")
            traceback.print_exc()
            _write_heartbeat("error", consecutive_failures)

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                console.print("[bold red]Circuit breaker tripped — too many consecutive failures. Sleeping 10 minutes.[/bold red]")
                await asyncio.sleep(600)
                consecutive_failures = 0

        # Target: one tick per minute
        elapsed = time.monotonic() - tick_start
        sleep_for = max(0, 60 - elapsed)
        await asyncio.sleep(sleep_for)
