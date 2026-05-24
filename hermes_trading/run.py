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
from dotenv import load_dotenv
from rich.console import Console

console = Console()

STATE_DIR = Path(os.getenv("STATE_DIR", "state"))
GOAL_FILE = STATE_DIR / "goal.yaml"

_DEFAULT_STRATEGY = {
    "version": "01",
    "entry": {
        "direction":      "both",   # evaluate long + short each tick; pick best signal
        "min_confidence": 0.3,      # minimum weighted indicator score (0–1)
        "min_indicators": 2,        # at least N indicators must fire (no single required gate)
    },
    "stop_loss_pct":    2.0,
    "position_size_r":  0.5,
    "indicators": [
        {"name": "rsi",          "required": False, "weight": 1.0, "params": {"threshold": 30}},
        {"name": "ema_trend",    "required": False, "weight": 0.6, "params": {"period": 50}},
        {"name": "macd",         "required": False, "weight": 0.5, "params": {}},
        {"name": "vwap",         "required": False, "weight": 0.4, "params": {}},
        {"name": "volume_spike", "required": False, "weight": 0.3, "params": {"min_ratio": 1.5}},
        {"name": "bb_squeeze",   "required": False, "weight": 0.3, "params": {}},
        {"name": "fvg",          "required": False, "weight": 0.4, "params": {}},
        {"name": "order_block",  "required": False, "weight": 0.4, "params": {"tolerance_pct": 0.5}},
        {"name": "sr_zone",      "required": False, "weight": 0.3, "params": {"tolerance_pct": 1.0}},
    ],
}


def _asset_slug(asset: str) -> str:
    """BTC/USDT → btc_usdt"""
    return asset.replace("/", "_").lower()


def _bootstrap_asset(asset: str) -> Path:
    """Create per-asset state dir and missing files. Returns the asset state dir."""
    asset_dir = STATE_DIR / _asset_slug(asset)
    asset_dir.mkdir(parents=True, exist_ok=True)
    (asset_dir / "history").mkdir(exist_ok=True)

    strategy_file = asset_dir / "strategy.yaml"
    if not strategy_file.exists() or strategy_file.stat().st_size == 0:
        console.print(f"[yellow]{asset}: strategy.yaml missing — creating default[/yellow]")
        with open(strategy_file, "w") as f:
            yaml.dump(_DEFAULT_STRATEGY, f, default_flow_style=False, sort_keys=False)

    for fname in ("trades.jsonl", "hypotheses.jsonl"):
        p = asset_dir / fname
        if not p.exists():
            p.touch()

    hb = asset_dir / "heartbeat.json"
    if not hb.exists():
        hb.write_text('{"status": "initializing", "last_tick": null, "consecutive_failures": 0}\n')

    return asset_dir


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
    """Normalise goal.yaml into a flat dict the rest of the codebase expects."""
    risk       = raw.get("risk", {})
    objective  = raw.get("objective", {})
    reflection = raw.get("reflection", {})

    target_return = raw.get(
        "target_return_30d",
        objective.get("target_value", 30.0) / 100.0,
    )
    max_drawdown = raw.get(
        "max_drawdown",
        risk.get("stop_loss_pct", 8.0) / 100.0,
    )
    reflection_every = raw.get(
        "reflection_every",
        max(1, int(reflection.get("interval_seconds", 300) / 60)),
    )

    return {
        "assets":            raw.get("assets", [raw.get("asset", "BTC/USDT")]),
        "asset":             raw.get("asset", "BTC/USDT"),
        "timeframe":         raw.get("timeframe", "5m"),
        "target_return_30d": target_return,
        "max_drawdown":      max_drawdown,
        "min_sharpe":        raw.get("min_sharpe", 1.2),
        "failure_below":     raw.get("failure_below", -0.04),
        "reflection_every":  reflection_every,
        "one_variable_only": raw.get("one_variable_only", True),
        "_raw": raw,
    }


def main() -> None:
    load_dotenv()

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

    args     = _parse_args()
    raw_goal = _load_goal()
    goal     = _resolve_goal(raw_goal)

    # --asset flag overrides the full assets list
    assets = [args.asset] if args.asset else goal["assets"]

    # Set timeframe env var so the price adapter picks it up
    os.environ["HERMES_TIMEFRAME"] = goal["timeframe"]

    console.print("[bold]hermes-trading worker starting[/bold]")
    console.print(f"  assets           : {', '.join(assets)}")
    console.print(f"  timeframe        : {goal['timeframe']}")
    console.print(f"  mode             : {mode}")
    console.print(f"  target return    : {goal['target_return_30d']:.0%} / 30d")
    console.print(f"  max drawdown     : {goal['max_drawdown']:.0%}")
    console.print(f"  min sharpe       : {goal['min_sharpe']}")
    console.print(f"  reflection every : {goal['reflection_every']} trades")

    # Bootstrap per-asset state dirs
    asset_dirs = {asset: _bootstrap_asset(asset) for asset in assets}

    from hermes_trading.loop import run

    async def _run_all() -> None:
        await asyncio.gather(*[
            run(asset, goal, state_dir=asset_dirs[asset])
            for asset in assets
        ])

    asyncio.run(_run_all())


if __name__ == "__main__":
    main()
