"""
dedup_trades.py -- remove duplicate trade records caused by mismatched order IDs
between native bot records and backfill/CSV-import records.

Background:
  The bot stores the OPENING order_id when a trade is placed. Bybit
  closed-pnl API returns the CLOSING order_id -- a different value. So
  backfill_trades.py cannot dedup against native records by order_id,
  and a second record gets appended for the same real trade.

What it does:
  Groups records by fingerprint (asset, exit@2dp, qty@4dp). Within each
  group, if duplicates exist:
    - Keeps the native record (strategy_version not in backfill/csv sets).
    - If all are synthetic, keeps the one with the most fields.
    - Open trades (exit_price is None) are never touched.
  Writes a timestamped .bak before mutating.

Usage:
  python -m tools.dedup_trades --state-root state --dry-run
  python -m tools.dedup_trades --state-root state
  python -m tools.dedup_trades --asset tao_usdt --dry-run
"""
import argparse, json, sys, time
from pathlib import Path

DEFAULT_ASSETS = ["btc_usdt", "eth_usdt", "sol_usdt", "tao_usdt"]
SYNTHETIC_VERSIONS = {"backfilled", "csv_import", "vbackfill", "vcsv_im"}


def _fingerprint(t):
    exit_ = t.get("exit_price")
    qty   = t.get("qty")
    asset = t.get("asset", "")
    if exit_ is None or qty is None:
        return None
    try:
        return (asset, round(float(exit_), 2), round(float(qty), 4))
    except (TypeError, ValueError):
        return None


def _is_synthetic(t):
    sv = (t.get("strategy_version") or "").lower()
    return sv in SYNTHETIC_VERSIONS


def _prefer(records):
    """Pick the best record from a duplicate group: native > synthetic, then most fields."""
    native = [r for r in records if not _is_synthetic(r)]
    if native:
        return max(native, key=lambda r: len(r))
    return max(records, key=lambda r: len(r))


def dedup_file(path, dry_run):
    if not path.exists():
        return {"skipped": True, "reason": "file not found"}
    raw = path.read_text(encoding="utf-8").splitlines()
    records = []
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    open_trades, fp_groups, ungrouped = [], {}, []
    for rec in records:
        if rec.get("exit_price") is None:
            open_trades.append(rec)
            continue
        fp = _fingerprint(rec)
        if fp is None:
            ungrouped.append(rec)
            continue
        fp_groups.setdefault(fp, []).append(rec)

    kept, removed = [], []
    for fp, group in fp_groups.items():
        if len(group) == 1:
            kept.append(group[0])
        else:
            winner = _prefer(group)
            kept.append(winner)
            for r in group:
                if r is not winner:
                    removed.append(r)

    result_records = open_trades + kept + ungrouped
    stats = {"total_in": len(records), "duplicates": len(removed),
             "total_out": len(result_records), "dry_run": dry_run}

    if removed and not dry_run:
        bak = path.parent / (path.stem + f".jsonl.bak-{int(time.time())}")
        bak.write_text("\n".join(raw), encoding="utf-8")
        tmp = path.parent / (path.stem + ".jsonl.tmp")
        lines_out = [json.dumps(r, separators=(",", ":")) for r in result_records]
        tmp.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
        tmp.replace(path)
        stats["backup"] = str(bak)

    if removed:
        stats["removed_samples"] = [
            {"asset": r.get("asset"), "exit": r.get("exit_price"),
             "qty": r.get("qty"), "strat": r.get("strategy_version"),
             "dir": r.get("direction")}
            for r in removed[:10]
        ]
    return stats


def main():
    ap = argparse.ArgumentParser(description="Dedup trades.jsonl — keep native over backfill/csv records")
    ap.add_argument("--state-root", default="state")
    ap.add_argument("--asset", default=None, help="Single asset slug, e.g. tao_usdt")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = Path(args.state_root)
    assets = [args.asset] if args.asset else DEFAULT_ASSETS
    total_removed = 0

    for slug in assets:
        path = root / slug / "trades.jsonl"
        res = dedup_file(path, args.dry_run)
        if res.get("skipped"):
            print(f"{slug}: skipped ({res['reason']})")
            continue
        n = res["duplicates"]
        total_removed += n
        tag = "[DRY RUN] " if args.dry_run else ""
        print(f"{slug}: {tag}{res['total_in']} in -> {res['total_out']} out  ({n} duplicates removed)")
        for s in res.get("removed_samples", []):
            print(f"  - dropped {s['strat']:12s} {s['dir'] or '?':5s}  exit={s['exit']}  qty={s['qty']}")
        if res.get("backup"):
            print(f"  backup -> {res['backup']}")

    print(f"\nTotal duplicates {'would remove' if args.dry_run else 'removed'}: {total_removed}")


if __name__ == "__main__":
    sys.exit(main())
