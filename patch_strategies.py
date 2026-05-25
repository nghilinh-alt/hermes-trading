"""
patch_strategies.py — One-time patch for existing per-asset strategy.yaml files.

Run from the working directory where state/{asset}/ dirs live:
  cd /opt/trading/hermes_trading
  python3 patch_strategies.py

Changes applied (version preserved):
  - entry.direction      → "both"   (was "long")
  - entry.min_indicators → 2        (new field)
  - entry.min_confidence → 0.3      (new if missing)
  - indicators[rsi].required → False (was True — RSI now optional like all others)
  --- Phase 1 SMC risk fields (added if missing) ---
  - risk_per_trade       → 0.01     (1% account risk per trade)
  - sl_buffer_pct        → 0.3      (% buffer below/above structural level for SL)
  - max_sl_pct           → 5.0      (max allowed SL distance %; skip trade if exceeded)
  - default_leverage     → 5        (fixed leverage; no longer RSI-scaled)
"""
from pathlib import Path
import yaml

ASSETS     = ["btc_usdt", "eth_usdt", "sol_usdt", "tao_usdt"]
STATE_BASE = Path("state")


def patch(path: Path) -> None:
    with open(path) as f:
        s = yaml.safe_load(f) or {}

    old_version = s.get("version", "?")

    # ── Entry config ──────────────────────────────────────────────────────────
    entry = s.setdefault("entry", {})
    entry["direction"]       = "both"
    entry["min_indicators"]  = 2
    entry.setdefault("min_confidence", 0.3)
    # Remove legacy flat-entry fields that are superseded by indicator registry
    for stale_key in ("indicator", "threshold", "ema_period", "bb_filter"):
        entry.pop(stale_key, None)

    # ── Make RSI non-required ─────────────────────────────────────────────────
    for ind in s.get("indicators", []):
        if ind.get("name") == "rsi":
            ind["required"] = False

    # ── Phase 1 SMC risk fields (setdefault = only add if missing) ────────────
    s.setdefault("risk_per_trade",   0.10)   # 10% account risk per trade
    s.setdefault("sl_buffer_pct",    0.3)    # % buffer below/above structural SL level
    s.setdefault("max_sl_pct",       5.0)    # max allowed SL distance before skipping
    s.setdefault("default_leverage", 5)      # fixed leverage (replaces RSI-scaled)

    with open(path, "w") as f:
        yaml.dump(s, f, default_flow_style=False, sort_keys=False)

    print(f"  ✓ {path.parent.name}/strategy.yaml  v{old_version}  "
          f"→ direction=both, min_indicators=2, rsi.required=False, "
          f"risk_per_trade=0.01, sl_buffer_pct=0.3, max_sl_pct=5.0, default_leverage=5")


def main() -> None:
    print("Patching per-asset strategy.yaml files...")
    any_found = False
    for slug in ASSETS:
        p = STATE_BASE / slug / "strategy.yaml"
        if p.exists():
            patch(p)
            any_found = True
        else:
            print(f"  – {slug}/strategy.yaml not found, skipping")

    if any_found:
        print("\nDone. Restart the agent to pick up the new config.")
    else:
        print("\nNo strategy files found — check you're in the right directory.")


if __name__ == "__main__":
    main()
