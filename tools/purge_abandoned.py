"""
purge_abandoned.py — remove abandoned-stub trade records from trades.jsonl files.

Background:
  `_reconcile_open_trades` in loop.py marks orphaned open trades as abandoned by
  setting exit_price = entry_price, pnl_pct = 0, abandoned = true. Over many agent
  restarts these stubs accumulate (e.g. 17 of 25 TAO records were abandoned).
  They have no exit information, so they can't be reconciled against Bybit's CSV
  via (asset, entry, exit, qty) dedup, and they pollute the trade history with
  fake $0 P&L entries.

What it does:
  For each state/<slug>/trades.jsonl, removes records where:
    - abandoned == true   (explicit flag from _reconcile_open_trades)
    - OR entry_price == exit_price AND pnl_pct == 0   (silently-abandoned stubs)
  Preserves open trades (pnl_pct is None), closed real trades, and backfilled/
  CSV-imported records. Backup written to trades.jsonl.bak-<timestamp>.

Usage:
  python3 -m tools.purge_abandoned --state-root state --dry-run
  python3 -m tools.purge_abandoned --state-root state
  python3 -m tools.purge_abandoned --asset tao_usdt
"""
import argparse
import json
import sys
import time
from pathlib import Path


DEFAULT_ASSETS = ["btc_usdt", "eth_usdt", "sol_usdt", "tao_usdt"]


def _is_abandoned(t: dict) -> bool:
    """True if this trade record is an abandoned stub."""
    if t.get("abandoned") is True:
        return True
    # Silently-abandoned: entry==exit AND pnl_pct==0
    entry, exit_ = t.get("entry_price"), t.get("exit_price")
    pnl = t.get("pnl_pct")
    if entry is not None and exit_ is not None and pnl is not None:
        try:
            if float(entry) == float(exit_) and float(pnl) == 0.0:
                return True
        except (TypeError, ValueError):
            pass
    return False


def purge_file(path: Path, dry_run: bool) -> dict:
    if not path.exists():
        return {"path": str(path), "error": "missing"}

    lines = path.read_text().splitlines()
    kept, purged, parse_errors = [], [], 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            t = json.loads(line)
        except json.JSONDecodeError:
            parse_errors += 1
            kept.append(line)
            continue
        if _is_abandoned(t):
            purged.append(t)
        else:
            kept.append(line)

    if purged and not dry_run:
        # Backup original
        backup = path.with_suffix(f".jsonl.bak-{int(time.time())}")
        backup.write_text("\n".join(lines) + "\n")
        # Atomic write of kept lines
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(kept) + "\n" if kept else "")
        tmp.replace(path)
        backup_str = str(backup)
    else:
        backup_str = None

    return {
        "path":         str(path),
        "total":        len(lines),
        "kept":         len(kept),
        "purged":       len(purged),
        "parse_errors": parse_errors,
        "backup":       backup_str,
        "dry_run":      dry_run,
        "sample_purged": [
            {
                "ts":     p.get("ts"),
                "dir":    p.get("direction"),
                "entry":  p.get("entry_price"),
                "exit":   p.get("exit_price"),
                "abandoned_flag": p.get("abandoned"),
            }
            for p in purged[:3]
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Purge abandoned-stub trade records")
    parser.add_argument("--state-root", default="state")
    parser.add_argument("--asset", action="append")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    state_root = Path(args.state_root)
    if not state_root.exists():
        print(f"ERROR: state root {state_root} missing", file=sys.stderr)
        return 1

    assets = args.asset or DEFAULT_ASSETS
    print(f"Purge — state_root={state_root} assets={assets} dry_run={args.dry_run}")
    print()

    total_purged = 0
    for slug in assets:
        stats = purge_file(state_root / slug / "trades.jsonl", args.dry_run)
        if stats.get("error"):
            print(f"  [{slug}] {stats['error']}")
            continue
        print(f"  [{slug}] total={stats['total']:3d}  kept={stats['kept']:3d}  purged={stats['purged']:3d}"
              f"  parse_err={stats['parse_errors']}"
              f"{'  (dry-run)' if stats['dry_run'] else ''}")
        if stats["backup"]:
            print(f"     backup: {stats['backup']}")
        for s in stats["sample_purged"]:
            print(f"     sample: {s['dir']} entry={s['entry']} exit={s['exit']} flag={s['abandoned_flag']}")
        total_purged += stats["purged"]

    print()
    print(f"Total purged: {total_purged}{' (dry-run)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
