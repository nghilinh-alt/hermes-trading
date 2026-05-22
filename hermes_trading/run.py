"""
run.py — entrypoint for the hermes-trading worker.

Usage:
  python -m hermes_trading.run [--asset BTC/USDT]

Reads asset from state/goal.yaml unless overridden by --asset flag.
Bootstraps any missing state files so a fresh clone starts cleanly.
"""
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import yaml
from rich.console import Console

console = Console()

STATE_DIR     = Path(os.getenv("STATE_DIR", "state"))
GOAL_FILE     = STATE_DIR / "goal.yaml"
STRATEGY_FILE = STATE_DIR / "strategy.yaml"
TRADES_FILE   = STATE_DIR / "trades.jsonl"
HYPOTHESES_FILE = STATE_DIR / "hypotheses.jsonl"
HEARTBEAT_FILE  = STATE_DIR / "heartbeat.json"
HISTORY_DIR   = STATE_DIR / "history"

_DEFAULT_STRATEGY = {
    "version": "01",
    "entry": {"indicator": "rsi", "threshold": 30, "direction": "long"},
    "stop_loss_pct": 2.0,
    "position_size_r": 0.5,
}


def _bootstrap_state() -> None:
    """Create any missing state files/dirs so a fresh clone starts cleanly."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    if not STRATEGY_FILE.exists() or STRATEGY_FILE.stat().st_size == 0:
        console.print("[yellow]state/strategy.yaml missing — creating default v01[/yellow]")
        with open(STRATEGY_FILE, "w") as f:
            yaml.dump(_DEFAULT_STRATEGY, f, default_flow_style=False, sort_keys=False)

    for f in (TRADES_FILE, HYPOTHESES_FILE):
        if not f.exists():
            f.touch()

    if not HEARTBEAT_FILE.exists():
        HEARTBEAT_FILE.write_text('{"status": "initializing", "last_tick": null, "consecutive_failures": 0}\n')


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hermes Trading Worker")
    parser.add_argument(
        "--asset",
        type=str,
        default=None,
        help="Override the asset from goal.yaml (e.g. ETH/USDT)",
    )
    return parser.parse_args()


def _load_goal() -> dict:
    """Load goal.yaml, returning {} if missing or empty."""
    if not GOAL_FILE.exists() or GOAL_FILE.stat().st_size == 0:
        console.print("[yellow]state/goal.yaml missing or empty — using built-in defaults[/yellow]")
        return {}
    with open(GOAL_FILE) as f:
        return yaml.safe_load(f) or {}


def _resolve_goal(raw: dict) -> dict:
    """
    Normalise both goal.yaml formats into a flat dict the rest of the
    codebase expects.

    Supports the original flat format:
      target_return_30d, max_drawdown, min_sharpe, reflection_every

    And the richer nested format:
      asset, mode, risk.stop_loss_pct, objective.target_value,
      objective.timeline_days, reflection.interval_seconds, ...
    """
    risk = raw.get("risk", {})
    objective = raw.get("objective", {})
    reflection = raw.get("reflection", {})

    # target_return_30d: flat key wins; else derive from objective.target_value (treat as %)
    target_return = raw.get(
        "target_return_30d",
        objective.get("target_value", 30.0) / 100.0,   # 5.0 → 0.05, 30.0 → 0.30
    )

    # max_drawdown: flat key wins; else risk.stop_loss_pct
    max_drawdown = raw.get(
        "max_drawdown",
        risk.get("stop_loss_pct", 8.0) / 100.0,        # 5.0 → 0.05
    )

    # reflection cadence: flat key wins; else interval_seconds / 60 as proxy trade count
    reflection_every = raw.get(
        "reflection_every",
        max(1, int(reflection.get("interval_seconds", 300) / 60)),  # 3600s → 60 trades
    )

    return {
        "asset":             raw.get("asset", "BTC/USDT"),
        "target_return_30d": target_return,
        "max_drawdown":      max_drawdown,
        "min_sharpe":        raw.get("min_sharpe", 1.2),
        "failure_below":     raw.get("failure_below", -0.04),
        "reflection_every":  reflection_every,
        "one_variable_only": raw.get("one_variable_only", True),
        # pass through extra keys so loop/score can optionally use them
        "_raw": raw,
    }


def main() -> None:
    # Safety check — refuse to run in live mode unless the risk flag is set
    mode        = os.getenv("HERMES_TRADING_MODE", "paper")
    accept_risk = os.getenv("HERMES_TRADING_I_ACCEPT_RISK", "false").lower()

    if mode == "live" and accept_risk != "true":
        console.print(
            "[bold red]ERROR: HERMES_TRADING_MODE=live requires "
            "HERMES_TRADING_I_ACCEPT_RISK=true in your .env file.\n"
            "Set both variables explicitly before running in live mode.[/bold red]"
        )
        sys.exit(1)

    _bootstrap_state()

    args     = _parse_args()
    raw_goal = _load_goal()
    goal     = _resolve_goal(raw_goal)

    asset = args.asset or goal.get("asset", "BTC/USDT")

    console.print("[bold]hermes-trading worker starting[/bold]")
    console.print(f"  asset            : {asset}")
    console.print(f"  mode             : {mode}")
    console.print(f"  target return    : {goal['target_return_30d']:.0%} / 30d")
    console.print(f"  max drawdown     : {goal['max_drawdown']:.0%}")
    console.print(f"  min sharpe       : {goal['min_sharpe']}")
    console.print(f"  reflection every : {goal['reflection_every']} trades")
    console.print(f"  goal file        : {GOAL_FILE}")

    # Pass resolved goal to the loop so it doesn't re-parse every tick
    from hermes_trading.loop import run
    asyncio.run(run(asset, goal))


if __name__ == "__main__":
    main()
