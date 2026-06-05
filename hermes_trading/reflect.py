"""
reflect.py — strategy reflection module.

Two modes:
  --fallback   Deterministic rule-based reflection (used before Hermes is installed)
  --hermes     Production mode: passes trades + strategy to Hermes CLI for AI reflection

In both modes:
  - Reads the latest trades and current strategy
  - Changes EXACTLY ONE variable
  - Bumps the strategy version
  - Saves the prior version to <state_dir>/history/v{NNNN}.yaml
  - Appends the hypothesis to <state_dir>/hypotheses.jsonl
  - Updates <state_dir>/memory.md with a brief reflection note

Usage:
  python -m hermes_trading.reflect --fallback --state-dir state/btc_usdt
  python -m hermes_trading.reflect --hermes  --state-dir state/eth_usdt
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from rich.console import Console

console = Console()


# ── Path helpers (no module-level globals — everything is per state_dir) ──────

def _paths(state_dir: Path) -> dict:
    return {
        "strategy":   state_dir / "strategy.yaml",
        "goal":       state_dir.parent / "goal.yaml",   # goal lives one level up (state/)
        "trades":     state_dir / "trades.jsonl",
        "hypotheses": state_dir / "hypotheses.jsonl",
        "history":    state_dir / "history",
        "memory":     state_dir / "memory.md",
    }


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _save_yaml(path: Path, data: dict) -> None:
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def _load_trades(trades_path: Path, limit: int = 25) -> list[dict]:
    if not trades_path.exists():
        return []
    lines = [l.strip() for l in trades_path.read_text().splitlines() if l.strip()]
    return [json.loads(l) for l in lines[-limit:]]


def _bump_version(current: str) -> str:
    try:
        n = int(current)
        return str(n + 1).zfill(len(current))
    except ValueError:
        return current + "_next"


def _archive_strategy(strategy: dict, history_dir: Path) -> Path:
    history_dir.mkdir(parents=True, exist_ok=True)
    version = strategy.get("version", "00")
    archive_path = history_dir / f"v{str(version).zfill(4)}.yaml"
    _save_yaml(archive_path, strategy)
    return archive_path


def _append_hypothesis(hypotheses_path: Path, hypothesis: dict) -> None:
    hypotheses_path.parent.mkdir(parents=True, exist_ok=True)
    with open(hypotheses_path, "a") as f:
        f.write(json.dumps(hypothesis) + "\n")


def _update_memory(memory_path: Path, hypothesis: dict, asset_slug: str) -> None:
    """Append a brief reflection note to memory.md so Hermes can learn over time."""
    ts      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    var     = hypothesis.get("changed_variable", "?")
    old_v   = hypothesis.get("old_value", "?")
    new_v   = hypothesis.get("new_value", "?")
    reason  = hypothesis.get("reasoning", "")
    v_from  = hypothesis.get("version_from", "?")
    v_to    = hypothesis.get("version_to", "?")
    mode    = hypothesis.get("mode", "fallback")
    ret     = hypothesis.get("realised_return")
    win_r   = hypothesis.get("win_rate")
    ret_str = f"{float(ret)*100:+.2f}%" if ret is not None else "n/a"
    win_str = f"{float(win_r)*100:.0f}%" if win_r is not None else "n/a"

    entry = (
        f"\n## {ts} · {asset_slug} · v{v_from}→v{v_to} [{mode}]\n"
        f"- **Changed**: `{var}` {old_v} → {new_v}\n"
        f"- **Return at reflection**: {ret_str}  |  **Win rate**: {win_str}\n"
        f"- **Reasoning**: {reason}\n"
    )

    # Initialise file with header if it doesn't exist
    if not memory_path.exists():
        memory_path.write_text(
            f"# Hermes Trading — Reflection Memory\n"
            f"_Auto-updated by reflect.py. Each entry is one strategy change._\n"
            f"_Asset: {asset_slug}_\n"
        )

    with open(memory_path, "a") as f:
        f.write(entry)


# ── Audit helpers (session 6, 2026-05-28) ────────────────────────────────────


def _trade_range(trades: list[dict]) -> dict:
    """
    Compact summary of which trades a reflection decision was based on.
    Stored on every hypothesis record so audits can re-fetch the exact trade
    window the LLM (or rule engine) was reasoning over.
    """
    if not trades:
        return {"count": 0, "from_ts": None, "to_ts": None, "trade_ids": []}
    return {
        "count":     len(trades),
        "from_ts":   trades[0].get("ts"),
        "to_ts":     trades[-1].get("ts"),
        "trade_ids": [t.get("order_id") for t in trades if t.get("order_id")],
    }


# ── Performance metrics ────────────────────────────────────────────────────────

def _realised_return(trades: list[dict]) -> float:
    # Open trades have pnl_pct=None; exclude them from the sum.
    return sum(float(t["pnl_pct"]) for t in trades if t.get("pnl_pct") is not None)


def _max_drawdown(trades: list[dict]) -> float:
    """
    Max drawdown as a fraction (0.0..1.0) using compound returns.

    Wealth path: start at 1.0, multiply by (1 + pnl_pct) for each closed trade.
    Drawdown at each point = (peak_so_far - current) / peak_so_far.
    Result is bounded in [0, 1] — fixes the prior bug where dividing by a
    near-zero peak amplified small dips into 80%+ false drawdowns.

    Open trades (pnl_pct=None) are excluded.
    """
    pnl_pcts = [float(t["pnl_pct"]) for t in trades if t.get("pnl_pct") is not None]
    if not pnl_pcts:
        return 0.0

    wealth = 1.0
    peak = 1.0
    max_dd = 0.0
    for p in pnl_pcts:
        # Cap per-trade pnl at -99% to avoid wealth going negative
        wealth = wealth * (1 + max(p, -0.99))
        if wealth > peak:
            peak = wealth
        dd = (peak - wealth) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 6)


def _win_rate(trades: list[dict]) -> float:
    closed = [t for t in trades if t.get("pnl_pct") is not None]
    if not closed:
        return 0.0
    wins = sum(1 for t in closed if float(t["pnl_pct"]) > 0)
    return wins / len(closed)


# ── Strategy variable mutation ─────────────────────────────────────────────────

def _get_nested(obj: dict, key_path: str):
    """
    Get a value by dot-path, supporting indicators[name].field notation.
    Examples:
      "indicators[rsi].params.threshold"
      "entry.min_confidence"
      "stop_loss_pct"
    """
    m = re.match(r'^indicators\[(\w+)\]\.(.+)$', key_path)
    if m:
        ind_name, rest = m.group(1), m.group(2)
        indicators = obj.get("indicators", [])
        ind = next((i for i in indicators if i.get("name") == ind_name), None)
        return _get_nested(ind, rest) if ind is not None else None

    parts = key_path.split(".", 1)
    val = obj.get(parts[0])
    if len(parts) == 1 or val is None:
        return val
    if isinstance(val, dict):
        return _get_nested(val, parts[1])
    return None


def _set_nested(obj: dict, key_path: str, new_value) -> bool:
    """
    Set a value by dot-path, supporting indicators[name].field notation.
    Returns True on success, False if the target path doesn't exist or is invalid.

    Safety guards:
      1. Rejects dot-notation paths that target the indicators list
         (e.g. "indicators.params.X" without [name]) — these would
         silently clobber the entire 9-indicator registry by overwriting
         the list with a dict. Forces the caller to use bracket notation
         "indicators[name].field" for any indicator-targeted change.
      2. Refuses to overwrite an existing list with a new dict.
    """
    # Guard 1: bare "indicators.*" without bracket notation is a registry-clobber
    # attempt — common LLM mistake. Reject explicitly so the caller can fall back.
    if key_path.startswith("indicators.") and not key_path.startswith("indicators[" + ""):
        return False

    m = re.match(r'^indicators\[(\w+)\]\.(.+)$', key_path)
    if m:
        ind_name, rest = m.group(1), m.group(2)
        indicators = obj.get("indicators", [])
        ind = next((i for i in indicators if i.get("name") == ind_name), None)
        if ind is None:
            return False
        return _set_nested(ind, rest, new_value)

    parts = key_path.split(".", 1)
    if len(parts) == 1:
        obj[parts[0]] = new_value
        return True
    # Guard 2: never replace an existing list with a dict
    existing = obj.get(parts[0])
    if isinstance(existing, list):
        return False
    if parts[0] not in obj or not isinstance(obj[parts[0]], dict):
        obj[parts[0]] = {}
    return _set_nested(obj[parts[0]], parts[1], new_value)


# ── Fallback (rule-based) reflection ──────────────────────────────────────────

def run_fallback(state_dir: Path) -> None:
    """Deterministic rule-based reflection — one variable, always."""
    p          = _paths(state_dir)
    asset_slug = state_dir.name

    console.print(f"[bold cyan]reflect --fallback [{asset_slug}]: loading strategy and trades...[/bold cyan]")

    strategy = _load_yaml(p["strategy"])
    goal     = _load_yaml(p["goal"])
    trades   = _load_trades(p["trades"], limit=25)

    # Normalise goal values — support both flat and nested goal.yaml formats
    target_ret  = goal.get(
        "target_return_30d",
        goal.get("objective", {}).get("target_value", 5.0) / 100,
    )
    max_dd_goal = goal.get(
        "max_drawdown",
        goal.get("risk", {}).get("stop_loss_pct", 8.0) / 100,
    )

    realised = _realised_return(trades)
    drawdown = _max_drawdown(trades)
    win_r    = _win_rate(trades)

    old_version = str(strategy.get("version", "01"))
    new_version = _bump_version(old_version)

    archive_path = _archive_strategy(strategy, p["history"])
    console.print(f"  Archived v{old_version} → {archive_path}")

    changed_var = old_val = new_val = None
    reasoning   = ""

    if drawdown > max_dd_goal:
        # Priority 1: tighten stop-loss to reduce tail exposure
        old_val = float(strategy.get("stop_loss_pct", 2.0))
        new_val = round(max(0.5, old_val - 0.2), 2)
        _set_nested(strategy, "stop_loss_pct", new_val)
        changed_var = "stop_loss_pct"
        reasoning = (
            f"Drawdown {drawdown:.2%} exceeded goal {max_dd_goal:.2%}. "
            f"Tightening stop_loss_pct {old_val} → {new_val} to reduce tail exposure."
        )
    elif realised < target_ret and win_r < 0.4:
        # Priority 2: win rate too low — loosen RSI entry threshold
        old_val = (
            _get_nested(strategy, "indicators[rsi].params.threshold")
            or float(strategy.get("entry", {}).get("threshold", 30))
        )
        new_val = round(min(40, float(old_val) + 2), 2)
        ok = _set_nested(strategy, "indicators[rsi].params.threshold", new_val)
        if not ok:
            _set_nested(strategy, "entry.threshold", new_val)
        changed_var = "indicators[rsi].params.threshold"
        reasoning = (
            f"Win rate {win_r:.0%} < 40% and return {realised:.2%} below target. "
            f"Loosening RSI threshold {old_val} → {new_val} to capture more entries."
        )
    elif realised < target_ret:
        # Priority 3: return low but win rate ok — increase position size
        old_val = float(strategy.get("position_size_r", 0.5))
        new_val = round(min(1.0, old_val + 0.05), 2)
        _set_nested(strategy, "position_size_r", new_val)
        changed_var = "position_size_r"
        reasoning = (
            f"Return {realised:.2%} below target {target_ret:.2%}, win rate ok ({win_r:.0%}). "
            f"Nudging position_size_r {old_val} → {new_val} to increase exposure."
        )
    else:
        # On track — compound by nudging position size up, or boost MACD weight if maxed
        old_pos = float(strategy.get("position_size_r", 0.5))
        if old_pos < 1.0:
            new_val = round(min(1.0, old_pos + 0.05), 2)
            _set_nested(strategy, "position_size_r", new_val)
            old_val, changed_var = old_pos, "position_size_r"
            reasoning = (
                f"Return {realised:.2%} on track, win rate {win_r:.0%}. "
                f"Compounding: nudging position_size_r {old_val} → {new_val}."
            )
        else:
            macd_w = _get_nested(strategy, "indicators[macd].weight")
            if macd_w is not None:
                new_val = round(min(1.0, float(macd_w) + 0.1), 2)
                _set_nested(strategy, "indicators[macd].weight", new_val)
                old_val, changed_var = float(macd_w), "indicators[macd].weight"
                reasoning = (
                    f"Return {realised:.2%} on track, position_size_r maxed. "
                    f"Increasing MACD weight {old_val} → {new_val} for stronger confirmation."
                )
            else:
                old_val = new_val = float(strategy.get("position_size_r", 1.0))
                changed_var = "position_size_r"
                reasoning = "No actionable change — strategy performing within all targets."

    strategy["version"] = new_version
    _save_yaml(p["strategy"], strategy)

    # Audit additions (session 6, 2026-05-28): record WHY the change was made
    decision_context = {
        "total_trades":    len(trades),
        "closed_trades":   sum(1 for t in trades if t.get("pnl_pct") is not None),
        "open_trades":     sum(1 for t in trades if t.get("pnl_pct") is None),
        "realised_return": round(realised, 6),
        "max_drawdown":    round(drawdown, 6),
        "win_rate":        round(win_r, 4),
        "target_return":   target_ret,
        "max_dd_goal":     max_dd_goal,
    }

    hypothesis = {
        "ts":                   datetime.now(timezone.utc).isoformat(),
        "mode":                 "fallback",
        "version_from":         old_version,
        "version_to":           new_version,
        "changed_variable":     changed_var,
        "old_value":            old_val,
        "new_value":            new_val,
        "reasoning":            reasoning,
        "trades_evaluated":     len(trades),
        "realised_return":      round(realised, 6),
        "max_drawdown":         round(drawdown, 6),
        "win_rate":             round(win_r, 4),
        # Audit additions
        "decision_context":     decision_context,
        "trade_range":          _trade_range(trades),
        "applied_successfully": True,   # fallback always operates on known keys
        "llm_raw_output":       None,   # not an LLM mode
    }
    _append_hypothesis(p["hypotheses"], hypothesis)
    _update_memory(p["memory"], hypothesis, asset_slug)

    console.print(f"[green]v{old_version} -> v{new_version}: changed {changed_var} {old_val} -> {new_val}[/green]")
    console.print(f"  Reasoning: {reasoning}")


# -- Hermes (AI-powered) reflection --------------------------------------------

def _call_llm(prompt: str) -> str:
    """
    Call a local Ollama instance (or any OpenAI-compatible endpoint).

    Configured via env vars (set in .env):
      HERMES_LLM_URL    base URL of the LLM server  (default: http://localhost:11434)
      HERMES_LLM_MODEL  model name to use            (default: qwen2.5:3b)
      HERMES_LLM_TIMEOUT seconds to wait             (default: 120)

    Returns the raw text response, or raises RuntimeError on failure.
    """
    import urllib.request

    base_url = os.getenv("HERMES_LLM_URL",     "http://localhost:11434")
    model    = os.getenv("HERMES_LLM_MODEL",   "qwen2.5:3b")
    timeout  = int(os.getenv("HERMES_LLM_TIMEOUT", "120"))

    # System prompt — keep it tight so small models stay on task
    system = (
        "You are a trading strategy optimiser. "
        "You receive JSON with recent trades and the current strategy. "
        "You must output a single JSON object with exactly these keys: "
        "changed_variable, old_value, new_value, reasoning, confidence. "
        "No markdown, no explanation, no code blocks — raw JSON only."
    )

    # num_ctx bumps Ollama's context window from the default 4096 to 8192 tokens.
    # qwen2.5:3b natively supports 32K, but Ollama caps at 4K unless told otherwise.
    # Combined with the compact-trades prompt shrink, 8K leaves comfortable headroom.
    # RAM cost: ~1GB extra for KV cache (VPS has 6GB+ free).
    num_ctx = int(os.getenv("HERMES_LLM_NUM_CTX", "8192"))
    payload = json.dumps({
        "model":  model,
        "stream": False,
        "messages": [
            {"role": "system",  "content": system},
            {"role": "user",    "content": prompt},
        ],
        "options": {"num_ctx": num_ctx},
    }).encode()

    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except OSError as exc:
        raise RuntimeError(f"LLM unreachable at {base_url}: {exc}") from exc

    content = body["choices"][0]["message"]["content"].strip()

    # Strip markdown code fences if the model wrapped its output anyway
    if content.startswith("```"):
        lines = content.splitlines()
        content = "\n".join(
            l for l in lines if not l.startswith("```")
        ).strip()

    return content


def run_hermes(state_dir: Path) -> None:
    """AI-powered reflection via local Ollama (or any OpenAI-compatible LLM)."""
    p          = _paths(state_dir)
    asset_slug = state_dir.name
    model      = os.getenv("HERMES_LLM_MODEL", "qwen2.5:3b")
    base_url   = os.getenv("HERMES_LLM_URL",   "http://localhost:11434")

    console.print(
        f"[bold cyan]reflect --hermes [{asset_slug}]: "
        f"calling {model} @ {base_url}...[/bold cyan]"
    )

    strategy = _load_yaml(p["strategy"])
    goal     = _load_yaml(p["goal"])
    trades   = _load_trades(p["trades"], limit=25)

    memory_context = p["memory"].read_text() if p["memory"].exists() else ""

    # Build a focused prompt — include win rate + indicator snapshot summaries
    # so the model can reason about which signals are performing well
    closed  = [t for t in trades if t.get("pnl_pct") is not None]
    winning = [t for t in closed if float(t["pnl_pct"]) > 0]
    losing  = [t for t in closed if float(t["pnl_pct"]) <= 0]

    def _avg_indicators(trade_list: list[dict]) -> dict:
        """Average each indicator snapshot value across a list of trades."""
        keys = set()
        for t in trade_list:
            keys.update((t.get("indicators_snapshot") or {}).keys())
        result = {}
        for k in keys:
            vals = [float(t["indicators_snapshot"][k])
                    for t in trade_list
                    if t.get("indicators_snapshot", {}).get(k) is not None]
            if vals:
                result[k] = round(sum(vals) / len(vals), 4)
        return result

    def _indicator_fire_table(winning: list[dict], losing: list[dict]) -> list[dict]:
        """Pre-compute per-indicator fire rates on wins vs losses.

        Gives the LLM a ground-truth table so it cannot hallucinate fire-rate
        statistics. Each row: {indicator, win_fire_rate, loss_fire_rate, delta}.
        delta > 0 means the indicator fires more on wins (healthy signal).
        delta < 0 means it fires more on losses (noisy — consider raising threshold).
        Only includes indicators that fired in at least one trade.
        """
        all_keys: set[str] = set()
        for t in winning + losing:
            all_keys.update((t.get("indicators_fired") or {}).keys())
        rows = []
        for k in sorted(all_keys):
            w_fired = sum(1 for t in winning if (t.get("indicators_fired") or {}).get(k))
            l_fired = sum(1 for t in losing  if (t.get("indicators_fired") or {}).get(k))
            w_rate  = round(w_fired / len(winning), 3) if winning else 0.0
            l_rate  = round(l_fired / len(losing),  3) if losing  else 0.0
            if w_fired + l_fired == 0:
                continue
            rows.append({
                "indicator":      k,
                "win_fire_rate":  w_rate,
                "loss_fire_rate": l_rate,
                "delta":          round(w_rate - l_rate, 3),
            })
        # Sort by abs(delta) descending so the most actionable signals appear first
        rows.sort(key=lambda r: abs(r["delta"]), reverse=True)
        return rows

    # Compact per-trade summary so the prompt fits Ollama's 4K context window.
    # Full audit data (indicators_snapshot, confidence_breakdown, entry_gates) stays
    # in trades.jsonl; we only send what the LLM needs for hypothesis generation.
    def _compact(t: dict) -> dict:
        return {
            "ts":       t.get("ts"),
            "dir":      t.get("direction"),
            "pnl_pct":  t.get("pnl_pct"),
            "conf":     t.get("confidence_at_entry"),
            "fired":    [k for k, v in (t.get("indicators_fired") or {}).items() if v],
            "close":    t.get("close_reason"),
            "rr":       t.get("rr_ratio"),
        }

    compact_trades = [_compact(t) for t in trades]

    prompt_data = {
        "asset":            asset_slug,
        "goal":             goal,
        "current_strategy": strategy,
        "recent_trades":    compact_trades,
        "performance_summary": {
            "total_trades":    len(trades),
            "closed_trades":   len(closed),
            "open_trades":     len(trades) - len(closed),
            "winning_trades":  len(winning),
            "losing_trades":   len(losing),
            "win_rate":        round(len(winning) / len(closed), 3) if closed else 0,
            "total_pnl":       round(sum(float(t["pnl_pct"]) for t in closed), 6),
            "avg_indicators_on_wins":   _avg_indicators(winning),
            "avg_indicators_on_losses": _avg_indicators(losing),
            "indicator_fire_table":     _indicator_fire_table(winning, losing),
        },
        "memory":           memory_context[-1500:] if memory_context else "",
        "instruction": (
            "Review the recent trades, current strategy, performance summary, "
            "and memory of past decisions. "
            "The performance_summary.indicator_fire_table shows — for each indicator — "
            "the exact fraction of winning and losing trades where it fired. "
            "delta = win_fire_rate - loss_fire_rate: "
            "positive means the indicator fires more on wins (healthy); "
            "negative means it fires more on losses (noisy — consider raising its threshold or reducing its weight). "
            "Base your reasoning on these ground-truth numbers, not estimates. "
            "Generate the single highest-confidence hypothesis that changes "
            "exactly ONE variable to improve win rate or PnL. "
            "\n\n"
            "TUNABLE VARIABLE NAMES (copy exactly — bracket notation is required for indicators):\n"
            "  stop_loss_pct\n"
            "  position_size_r\n"
            "  risk_per_trade\n"
            "  sl_buffer_pct\n"
            "  max_sl_pct\n"
            "  default_leverage\n"
            "  entry.min_confidence\n"
            "  entry.min_indicators\n"
            "  indicators[rsi].params.threshold\n"
            "  indicators[ema_trend].weight\n"
            "  indicators[macd].weight\n"
            "  indicators[vwap].weight\n"
            "  indicators[volume_spike].params.min_ratio\n"
            "  indicators[volume_spike].weight\n"
            "  indicators[bb_squeeze].weight\n"
            "  indicators[fvg].weight\n"
            "  indicators[order_block].weight\n"
            "  indicators[order_block].params.tolerance_pct\n"
            "  indicators[sr_zone].weight\n"
            "  indicators[sr_zone].params.tolerance_pct\n"
            "\n"
            "RULES:\n"
            "  - Do NOT use dot notation like 'indicators.params.X' — always use 'indicators[name].field'.\n"
            "  - Do NOT change the 'required' field on any indicator.\n"
            "  - Do NOT repeat a change that memory shows was tried recently without improvement.\n"
            "  - Your reasoning MUST cite specific numbers from indicator_fire_table, not estimates.\n"
            "\n"
            "OUTPUT FORMAT — raw JSON only, no markdown, no code fences, no explanation.\n"
            "Example output:\n"
            '{"changed_variable": "indicators[ema_trend].weight", '
            '"old_value": 0.3, "new_value": 0.4, '
            '"reasoning": "ema_trend win_fire_rate=0.72 vs loss_fire_rate=0.41 (delta=+0.31); '
            'highest positive delta in fire table — increasing weight to reward this signal.", "confidence": 0.8}\n'
            "\n"
            "Your output must follow that exact structure with these five keys: "
            "changed_variable, old_value, new_value, reasoning, confidence."
        ),
    }

    prompt = json.dumps(prompt_data, indent=2)

    try:
        output = _call_llm(prompt)
    except RuntimeError as exc:
        console.print(f"[red]LLM call failed: {exc} -- falling back to --fallback[/red]")
        run_fallback(state_dir)
        return

    try:
        hypothesis_data = json.loads(output)
    except json.JSONDecodeError:
        console.print(f"[yellow]LLM output was not valid JSON:\n{output}[/yellow]")
        console.print("[yellow]Falling back to deterministic reflection.[/yellow]")
        run_fallback(state_dir)
        return

    old_version = str(strategy.get("version", "01"))
    new_version = _bump_version(old_version)
    archive_path = _archive_strategy(strategy, p["history"])
    console.print(f"  Archived v{old_version} -> {archive_path}")

    changed_var = hypothesis_data.get("changed_variable", "")
    new_val     = hypothesis_data.get("new_value")
    old_val     = hypothesis_data.get("old_value")

    # Audit additions (session 6, 2026-05-28): capture the LLM's-eye view
    decision_context = prompt_data.get("performance_summary", {})
    trade_range_info = _trade_range(trades)

    # Phase 2.8 sanity check: verify LLM's stated old_value matches actual current value.
    # Flags hallucinated values without blocking the mutation (the LLM's new_value may
    # still be directionally correct even when its old_value is wrong).
    actual_old = _get_nested(strategy, changed_var)
    old_value_mismatch = False
    if actual_old is not None and old_val is not None:
        try:
            if abs(float(actual_old) - float(old_val)) > 0.01:
                old_value_mismatch = True
                console.print(
                    f"[yellow]Sanity: LLM stated old_value={old_val} but actual={actual_old} "
                    f"for '{changed_var}'. Applying new_value anyway — flagging mismatch.[/yellow]"
                )
        except (TypeError, ValueError):
            if str(actual_old) != str(old_val):
                old_value_mismatch = True

    applied_ok = _set_nested(strategy, changed_var, new_val)

    # Build hypothesis upfront — recorded whether application succeeded or not,
    # so the LLM's reasoning is preserved even when its proposed key is unknown.
    base_hypothesis = {
        "ts":                   datetime.now(timezone.utc).isoformat(),
        "mode":                 "hermes",
        "version_from":         old_version,
        "version_to":           new_version if applied_ok else old_version,
        "changed_variable":     changed_var,
        "old_value":            old_val,
        "new_value":            new_val,
        "reasoning":            hypothesis_data.get("reasoning", ""),
        "confidence":           hypothesis_data.get("confidence"),
        "trades_evaluated":     len(trades),
        # Audit additions
        "decision_context":     decision_context,
        "trade_range":          trade_range_info,
        "applied_successfully": applied_ok,
        "old_value_mismatch":   old_value_mismatch,
        "llm_raw_output":       output,   # literal string the LLM returned (pre-parse)
    }

    if not applied_ok:
        console.print(f"[yellow]Could not apply '{changed_var}' -- key not found. Recording attempt + falling back.[/yellow]")
        base_hypothesis["error_reason"] = "key_not_found"
        _append_hypothesis(p["hypotheses"], base_hypothesis)
        _update_memory(p["memory"], base_hypothesis, asset_slug)
        run_fallback(state_dir)
        return

    strategy["version"] = new_version
    _save_yaml(p["strategy"], strategy)

    _append_hypothesis(p["hypotheses"], base_hypothesis)
    _update_memory(p["memory"], base_hypothesis, asset_slug)

    # Escape brackets so rich doesn't strip indicator names like [volume_spike]
    # from the display (real changed_var stored in hypotheses.jsonl is untouched).
    display_var = changed_var.replace("[", "\\[")
    console.print(f"[green]Hermes: v{old_version} -> v{new_version}: {display_var} {old_val} -> {new_val}[/green]")


# -- CLI -----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes Trading Reflect")
    parser.add_argument("--fallback",  action="store_true", help="Deterministic fallback reflection")
    parser.add_argument("--hermes",    action="store_true", help="AI-powered reflection via Hermes CLI")
    parser.add_argument(
        "--state-dir",
        type=str,
        default=None,
        help="Per-asset state directory (e.g. state/btc_usdt). "
             "Falls back to STATE_DIR env var, then 'state/'.",
    )
    args = parser.parse_args()

    raw_dir   = args.state_dir or os.getenv("STATE_DIR", "state")
    state_dir = Path(raw_dir)

    if not state_dir.exists():
        console.print(f"[red]state-dir '{state_dir}' does not exist[/red]")
        sys.exit(1)

    if args.hermes:
        run_hermes(state_dir)
    elif args.fallback:
        run_fallback(state_dir)
    else:
        console.print("[red]Specify --fallback or --hermes[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
