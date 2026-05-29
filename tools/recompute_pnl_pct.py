"""
recompute_pnl_pct.py — repair direction-broken pnl_pct in trades.jsonl files.

Background (Phase 2.5, 2026-05-29):
  Prior to today's `execution.fetch_last_closed_pnl` fix, pnl_pct was computed as
  `(exit - entry) / entry`. That formula is direction-blind:
    * Long winning trades (exit > entry):    +pnl_pct → CORRECT
    * Long losing trades (exit < entry):     -pnl_pct → CORRECT
    * Short winning trades (exit < entry):   -pnl_pct → WRONG (should be positive)
    * Short losing trades (exit > entry):    +pnl_pct → WRONG (should be negative)
  TAO is mostly shorts → most pnl_pct values are inverted. This produced absurd
  reflection inputs ("drawdown 557%") and led to bad strategy mutations.

What this tool does:
  Walks each state/<slug>/trades.jsonl. For each CLOSED trade (pnl_pct is not None)
  that has both entry_price and exit_price recorded, recomputes pnl_pct as:
    long:  (exit - entry) / entry
    short: (entry - exit) / entry
  If the new value differs from the stored one, overwrites pnl_pct and preserves
  the old value as pnl_pct_old with a recomputed flag.

Skip rules:
  - open trades (pnl_pct is None): leave alone
  - backfilled records (have closed_pnl_usdt + cum_entry_value): already
    direction-correct from Bybit; leave alone unless --force
  - missing entry_price or exit_price: cannot recompute, leave alone

Usage:
  python3 -m tools.recompute_pnl_pct --state-root state --dry-run
  python3 -m tools.recompute_pnl_pct --state-root state           # write
  python3 -m tools.recompute_pnl_pct --asset tao_usdt              # one asset
  python3 -m tools.recompute_pnl_pct --force                       # also touch backfilled
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_ASSETS = ["btc_usdt", "eth_usdt", "sol_usdt", "tao_usdt"]


def _correct_pnl_pct(direction: str, entry: float, exit_: float) -> float | None:
    """Direction-aware pnl_pct. Returns None if inputs invalid."""
    try:
        entry = float(entry)
        exit_ = float(exit_)
    except (TypeError, ValueError):
        return None
    if entry <= 0:
        return None
    if direction == "long":
        return round((exit_ - entry) / entry, 6)
    if direction == "short":
        return round((entry - exit_) / entry, 6)
    return None


def recompute_file(path: Path, dry_run: bool, force_backfilled: bool) -> dict:
    """
    Recompute pnl_pct for every recomputable closed trade in `path`.
    Returns stats dict.
    """
    if not path.exists():
        return {"path": str(path), "error": "missing", "fixed": 0, "skipped": 0}

    raw_lines = path.read_text().splitlines()
    trades = []
    parse_errors = 0
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            trades.append(json.loads(line))
        except json.JSONDecodeError:
            parse_errors += 1

    fixed = []
    skipped = {"open": 0, "missing_prices": 0, "backfilled": 0, "unchanged": 0, "bad_direction": 0}

    for t in trades:
        if t.get("pnl_pct") is None:
            skipped["open"] += 1
            continue
        if t.get("backfilled") is True and not force_backfilled:
            skipped["backfilled"] += 1
            continue
        direction = t.get("direction")
        if direction not in ("long", "short"):
            skipped["bad_direction"] += 1
            continue
        entry = t.get("entry_price")
        exit_ = t.get("exit_price")
        if entry is None or exit_ is None:
            skipped["missing_prices"] += 1
            continue

        correct = _correct_pnl_pct(direction, entry, exit_)
        if correct is None:
            skipped["missing_prices"] += 1
            continue

        current = float(t["pnl_pct"])
        # Only rewrite if it actually differs (tolerance for floating noise)
        if abs(current - correct) < 1e-6:
            skipped["unchanged"] += 1
            continue

        fixed.append({
            "order_id": t.get("order_id"),
            "direction": direction,
            "old": current,
            "new": correct,
            "delta": round(correct - current, 6),
        })
        t["pnl_pct_old"]        = current
        t["pnl_pct"]            = correct
        t["pnl_pct_recomputed"] = True
        t["pnl_pct_recomputed_at"] = datetime.now(timezone.utc).isoformat()

    if fixed and not dry_run:
        # Atomic write: tmp then rename
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(json.dumps(t) for t in trades) + "\n")
        tmp.replace(path)

    return {
        "path":          str(path),
        "trades_total":  len(trades),
        "parse_errors":  parse_errors,
        "fixed_count":   len(fixed),
        "skipped":       skipped,
        "samples":       fixed[:3],   # first 3 fixes for visibility
        "dry_run":       dry_run,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Recompute direction-correct pnl_pct in trades.jsonl")
    parser.add_argument("--state-root", default="state",
                        help="Path to per-asset state root (default: ./state)")
    parser.add_argument("--asset", action="append",
                        help="Asset slug (repeatable). Default: all 4.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would change, don't write.")
    parser.add_argument("--force", action="store_true",
                        help="Also recompute backfilled records (normally skipped).")
    args = parser.parse_args()

    state_root = Path(args.state_root)
    if not state_root.exists():
        print(f"ERROR: state root {state_root} does not exist", file=sys.stderr)
        return 1

    assets = args.asset or DEFAULT_ASSETS

    print(f"Recompute — state_root={state_root} assets={assets} dry_run={args.dry_run} force={args.force}")
    print()

    grand_fixed = 0
    for slug in assets:
        path = state_root / slug / "trades.jsonl"
        stats = recompute_file(path, args.dry_run, args.force)
        if stats.get("error"):
            print(f"  [{slug}] {stats['error']} ({stats['path']})")
            continue
        skp = stats["skipped"]
        print(f"  [{slug}] total={stats['trades_total']:3d}  fixed={stats['fixed_count']:3d}  "
              f"open={skp['open']}  unchanged={skp['unchanged']}  "
              f"backfilled-skip={skp['backfilled']}  "
              f"missing-prices={skp['missing_prices']}  bad-dir={skp['bad_direction']}"
              f"{'  (dry-run)' if stats['dry_run'] else ''}")
        for s in stats["samples"]:
            print(f"     sample: {s['direction']:5s} order={s['order_id']}  "
                  f"{s['old']:+.4%} -> {s['new']:+.4%}  delta={s['delta']:+.4%}")
        grand_fixed += stats["fixed_count"]

    print()
    print(f"Total fixed: {grand_fixed}{' (dry-run, nothing written)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
