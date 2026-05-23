"""
reflect.py — strategy reflection module.

Two modes:
  --fallback   Deterministic rule-based reflection (used before Hermes is installed)
  --hermes     Production mode: passes trades + strategy to Hermes CLI for AI reflection

In both modes:
  - Reads the latest trades and current strategy
  - Changes EXACTLY ONE variable
  - Bumps the strategy version
  - Saves the prior version to state/history/v{NNNN}.yaml
  - Appends the hypothesis to state/hypotheses.jsonl
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from rich.console import Console

console = Console()

STATE_DIR   = Path(os.getenv("STATE_DIR", "state"))
STRATEGY_FILE = STATE_DIR / "strategy.yaml"
GOAL_FILE   = STATE_DIR / "goal.yaml"
TRADES_FILE = STATE_DIR / "trades.jsonl"
HYPOTHESES_FILE = STATE_DIR / "hypotheses.jsonl"
HISTORY_DIR = STATE_DIR / "history"


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _save_yaml(path: Path, data: dict):
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def _load_trades(limit: int = 25) -> list[dict]:
    if not TRADES_FILE.exists():
        return []
    lines = [l.strip() for l in TRADES_FILE.read_text().splitlines() if l.strip()]
    return [json.loads(l) for l in lines[-limit:]]


def _bump_version(current: str) -> str:
    try:
        n = int(current)
        return str(n + 1).zfill(len(current))
    except ValueError:
        return current + "_next"


def _archive_strategy(strategy: dict):
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    version = strategy.get("version", "00")
    archive_path = HISTORY_DIR / f"v{version.zfill(4)}.yaml"
    _save_yaml(archive_path, strategy)
    return archive_path


def _append_hypothesis(hypothesis: dict):
    with open(HYPOTHESES_FILE, "a") as f:
        f.write(json.dumps(hypothesis) + "\n")


def _realised_return(trades: list[dict]) -> float:
    return sum(t.get("pnl_pct", 0.0) for t in trades)


def _max_drawdown(trades: list[dict]) -> float:
    pnl_pcts = [t.get("pnl_pct", 0.0) for t in trades]
    if not pnl_pcts:
        return 0.0
    cumulative = []
    running = 0.0
    for p in pnl_pcts:
        running += p
        cumulative.append(running)
    peak = cumulative[0]
    max_dd = 0.0
    for c in cumulative:
        if c > peak:
            peak = c
        dd = (peak - c) / (abs(peak) + 1e-9)
        if dd > max_dd:
            max_dd = dd
    return max_dd


def run_fallback():
    """Deterministic rule-based reflection — one variable, always."""
    console.print("[bold cyan]reflect --fallback: loading strategy and trades...[/bold cyan]")

    strategy = _load_yaml(STRATEGY_FILE)
    goal = _load_yaml(GOAL_FILE)
    trades = _load_trades(25)

    target_ret = goal.get("target_return_30d", 0.05)
    max_dd_goal = goal.get("max_drawdown", 0.08)

    realised = _realised_return(trades)
    drawdown = _max_drawdown(trades)

    old_version = strategy.get("version", "01")
    new_version = _bump_version(old_version)

    # Archive current
    archive_path = _archive_strategy(strategy)
    console.print(f"  Archived v{old_version} → {archive_path}")

    # Determine which single variable to change
    changed_var = None
    old_val = None
    new_val = None
    reasoning = ""

    if drawdown > max_dd_goal:
        # Priority: tighten stop-loss first
        old_val = float(strategy.get("stop_loss_pct", 2.0))
        new_val = round(old_val - 0.2, 2)
        strategy["stop_loss_pct"] = new_val
        changed_var = "stop_loss_pct"
        reasoning = (
            f"Drawdown {drawdown:.2%} exceeded goal {max_dd_goal:.2%}. "
            f"Tightening stop_loss_pct {old_val} → {new_val} to reduce tail exposure."
        )
    elif realised < target_ret:
        # Loosen entry threshold to catch more opportunities
        old_val = float(strategy["entry"]["threshold"])
        new_val = round(old_val + 2, 2)
        strategy["entry"]["threshold"] = new_val
        changed_var = "entry.threshold"
        reasoning = (
            f"Realised return {realised:.2%} below target {target_ret:.2%}. "
            f"Loosening entry threshold {old_val} → {new_val} to increase trade frequency."
        )
    else:
        # Performing well — nudge position size up slightly, or boost MACD weight
        # Alternate between the two so fallback doesn't only ever touch position size
        old_pos = float(strategy.get("position_size_r", 0.5))
        if old_pos < 1.0:
            new_val = round(min(1.0, old_pos + 0.05), 2)
            strategy["position_size_r"] = new_val
            old_val = old_pos
            changed_var = "position_size_r"
            reasoning = (
                f"Realised return {realised:.2%} on track. "
                f"Nudging position_size_r {old_val} → {new_val} to compound gains."
            )
        else:
            # Position size maxed — boost MACD confirmation weight instead
            indicators = strategy.get("indicators", [])
            macd_ind = next((i for i in indicators if i.get("name") == "macd"), None)
            if macd_ind is not None:
                old_val = float(macd_ind.get("weight", 0.5))
                new_val = round(min(1.0, old_val + 0.1), 2)
                macd_ind["weight"] = new_val
                changed_var = "indicators[macd].weight"
                reasoning = (
                    f"Realised return {realised:.2%} on track, position_size_r maxed. "
                    f"Increasing MACD weight {old_val} → {new_val} for stronger confirmation."
                )
            else:
                old_val = old_pos
                new_val = old_pos
                changed_var = "position_size_r"
                reasoning = "No actionable change — strategy performing within targets."

    strategy["version"] = new_version
    _save_yaml(STRATEGY_FILE, strategy)

    hypothesis = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "fallback",
        "version_from": old_version,
        "version_to": new_version,
        "changed_variable": changed_var,
        "old_value": old_val,
        "new_value": new_val,
        "reasoning": reasoning,
        "trades_evaluated": len(trades),
        "realised_return": round(realised, 6),
        "max_drawdown": round(drawdown, 6),
    }
    _append_hypothesis(hypothesis)

    console.print(f"[green]✓ v{old_version} → v{new_version}: changed {changed_var} {old_val} → {new_val}[/green]")
    console.print(f"  Reasoning: {reasoning}")


def run_hermes():
    """AI-powered reflection — calls hermes CLI with the trade/strategy context."""
    console.print("[bold cyan]reflect --hermes: loading context for Hermes...[/bold cyan]")

    strategy = _load_yaml(STRATEGY_FILE)
    goal = _load_yaml(GOAL_FILE)
    trades = _load_trades(25)

    context = {
        "goal": goal,
        "current_strategy": strategy,
        "recent_trades": trades,
        "instruction": (
            "You are the reflection engine for a self-improving trading agent. "
            "Review the recent trades and current strategy. "
            "Generate 1-3 hypotheses. Each must change exactly ONE variable and predict "
            "the score direction. Choose the highest-confidence hypothesis. "
            "Tunable variables include: stop_loss_pct, position_size_r, entry.min_confidence, "
            "and any indicator field using dot notation such as "
            "'indicators[rsi].params.threshold', 'indicators[ema_trend].weight', "
            "'indicators[macd].weight', 'indicators[vwap].weight', "
            "'indicators[volume_spike].params.min_ratio', 'indicators[volume_spike].weight', "
            "'indicators[bb_squeeze].weight', "
            "'indicators[fvg].weight', "
            "'indicators[order_block].weight', 'indicators[order_block].params.tolerance_pct', "
            "'indicators[sr_zone].weight', 'indicators[sr_zone].params.tolerance_pct'. "
            "SMC context: fvg fires when price is inside a 1h Fair Value Gap. "
            "order_block fires when price is within tolerance_pct of a 1h Order Block zone. "
            "sr_zone fires when price is within tolerance_pct of a 1h/4h support or resistance level. "
            "You may also set an indicator's weight to 0.0 to effectively disable it, "
            "or increase a weight to give it more influence. "
            "Do NOT change the 'required' field on rsi — it must stay true. "
            "Output JSON: {changed_variable, old_value, new_value, reasoning, confidence}. "
            "Only output JSON, nothing else."
        ),
    }

    prompt = json.dumps(context, indent=2)

    try:
        result = subprocess.run(
            ["hermes"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout.strip()
    except FileNotFoundError:
        console.print("[red]hermes command not found — falling back to --fallback mode[/red]")
        run_fallback()
        return
    except subprocess.TimeoutExpired:
        console.print("[red]Hermes timed out — falling back to --fallback mode[/red]")
        run_fallback()
        return

    # Parse hermes output
    try:
        hypothesis_data = json.loads(output)
    except json.JSONDecodeError:
        console.print(f"[yellow]Could not parse Hermes output as JSON. Raw output:\n{output}[/yellow]")
        console.print("[yellow]Falling back to deterministic reflection.[/yellow]")
        run_fallback()
        return

    old_version = strategy.get("version", "01")
    new_version = _bump_version(old_version)
    archive_path = _archive_strategy(strategy)
    console.print(f"  Archived v{old_version} → {archive_path}")

    # Apply the hypothesis
    changed_var = hypothesis_data.get("changed_variable", "")
    new_val = hypothesis_data.get("new_value")
    old_val = hypothesis_data.get("old_value")

    # Navigate nested keys (e.g. "entry.threshold")
    keys = changed_var.split(".")
    target = strategy
    for k in keys[:-1]:
        target = target.setdefault(k, {})
    target[keys[-1]] = new_val
    strategy["version"] = new_version

    _save_yaml(STRATEGY_FILE, strategy)

    hypothesis = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "hermes",
        "version_from": old_version,
        "version_to": new_version,
        "changed_variable": changed_var,
        "old_value": old_val,
        "new_value": new_val,
        "reasoning": hypothesis_data.get("reasoning", ""),
        "confidence": hypothesis_data.get("confidence"),
        "trades_evaluated": len(trades),
    }
    _append_hypothesis(hypothesis)

    console.print(f"[green]✓ Hermes: v{old_version} → v{new_version}: {changed_var} {old_val} → {new_val}[/green]")


def main():
    parser = argparse.ArgumentParser(description="Hermes Trading Reflect")
    parser.add_argument("--fallback", action="store_true", help="Deterministic fallback reflection")
    parser.add_argument("--hermes", action="store_true", help="AI-powered reflection via Hermes CLI")
    args = parser.parse_args()

    if args.hermes:
        run_hermes()
    elif args.fallback:
        run_fallback()
    else:
        console.print("[red]Specify --fallback or --hermes[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
