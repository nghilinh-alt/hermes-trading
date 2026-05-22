"""
run.py — entrypoint for the hermes-trading worker.

Usage:
  python -m hermes_trading.run [--asset BTC/USDT]

Reads asset from state/goal.yaml unless overridden by --asset flag.
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

import yaml
from rich.console import Console

console = Console()

STATE_DIR = Path(os.getenv("STATE_DIR", "state"))
GOAL_FILE = STATE_DIR / "goal.yaml"


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
    if GOAL_FILE.exists():
        with open(GOAL_FILE) as f:
            return yaml.safe_load(f) or {}
    return {}


def main():
    # Safety check — refuse to run in live mode unless the risk flag is explicitly set
    mode = os.getenv("HERMES_TRADING_MODE", "paper")
    accept_risk = os.getenv("HERMES_TRADING_I_ACCEPT_RISK", "false").lower()

    if mode == "live" and accept_risk != "true":
        console.print(
            "[bold red]ERROR: HERMES_TRADING_MODE=live requires "
            "HERMES_TRADING_I_ACCEPT_RISK=true in your .env file.\n"
            "Set both variables explicitly before running in live mode.[/bold red]"
        )
        sys.exit(1)

    args = _parse_args()
    goal = _load_goal()

    asset = args.asset or goal.get("asset", "BTC/USDT")

    console.print(f"[bold]hermes-trading worker starting[/bold]")
    console.print(f"  asset : {asset}")
    console.print(f"  mode  : {mode}")
    console.print(f"  goal  : {GOAL_FILE}")

    from hermes_trading.loop import run
    asyncio.run(run(asset))


if __name__ == "__main__":
    main()
