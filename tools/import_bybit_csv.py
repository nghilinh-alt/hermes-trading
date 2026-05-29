"""
import_bybit_csv.py — backfill closed trades from a Bybit "All Perp Closed PnL" CSV export.

Why this exists:
  Bybit's API endpoint /v5/position/closed-pnl is limited to a 7-day window per call.
  The earlier `backfill_trades.py` tool couldn't reach trades older than 7 days, leaving
  ~15 trades on Bybit invisible to local trades.jsonl. This tool reads the CSV that
  Bybit exports from the trading UI (covers up to 6 months) and fills the gap.

CSV format (Bybit UI export):
  Market,Order Quantity,Entry Price,Exit Price,Opening Fee,Closing Fee,Funding Fee,
  cumClosedPzOpenFeeInfo,cumClosedPzTradeFeeInfo,Trade Type,Realized P&L,Trade time

Direction inference (CSV has no side column):
  Bybit's CSV doesn't tell you long vs short directly, but it can be inferred from the
  sign of (Realized P&L) vs the sign of (Exit - Entry):
    exit < entry AND P&L > 0  →  SHORT win
    exit < entry AND P&L < 0  →  LONG  loss
    exit > entry AND P&L > 0  →  LONG  win
    exit > entry AND P&L < 0  →  SHORT loss

Dedup: by (asset, ts, entry_price_rounded). Doesn't use order_id because the CSV
doesn't include it. Conservative — won't overwrite native-logged records.

Usage:
  python3 -m tools.import_bybit_csv --csv /path/to/Bybit-export.csv --state-root state --dry-run
  python3 -m tools.import_bybit_csv --csv /path/to/Bybit-export.csv --state-root state
"""
import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _parse_bybit_ts(ts_str: str) -> str | None:
    """
    Bybit CSV format: '04:09 2026-05-29' (HH:MM YYYY-MM-DD), timezone implied UTC.
    Returns ISO 8601 string with +00:00 timezone, or None on failure.
    """
    try:
        dt = datetime.strptime(ts_str.strip(), "%H:%M %Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except (ValueError, AttributeError):
        return None


def _market_to_asset(market: str) -> str | None:
    """TAOUSDT → TAO/USDT"""
    if not market or not market.endswith("USDT"):
        return None
    return f"{market[:-4]}/USDT"


def _market_to_slug(market: str) -> str | None:
    """TAOUSDT → tao_usdt"""
    if not market or not market.endswith("USDT"):
        return None
    return f"{market[:-4].lower()}_usdt"


def _infer_direction(entry: float, exit_: float, realized_pnl: float) -> str | None:
    """
    Direction inference from price movement + PnL sign.
    Returns 'long', 'short', or None if undecidable.
    """
    if entry == exit_ or realized_pnl == 0:
        return None
    price_up = exit_ > entry
    pnl_positive = realized_pnl > 0
    if price_up and pnl_positive:    return "long"
    if not price_up and not pnl_positive: return "long"
    return "short"


def _row_to_trade(row: dict) -> dict | None:
    """Convert one CSV row into a trades.jsonl-shaped record."""
    market = row.get("Market", "").strip()
    asset = _market_to_asset(market)
    if asset is None:
        return None

    try:
        entry  = float(row["Entry Price"])
        exit_  = float(row["Exit Price"])
        qty    = float(row["Order Quantity"])
        pnl    = float(row["Realized P&L"])
        open_f = float(row.get("Opening Fee", 0) or 0)
        close_f = float(row.get("Closing Fee", 0) or 0)
        fund_f = float(row.get("Funding Fee", 0) or 0)
    except (KeyError, ValueError, TypeError):
        return None

    if entry <= 0 or qty <= 0:
        return None

    direction = _infer_direction(entry, exit_, pnl)
    if direction is None:
        return None

    # Direction-correct pnl_pct (matches recompute_pnl_pct convention).
    pnl_pct = round((exit_ - entry) / entry if direction == "long"
                    else (entry - exit_) / entry, 6)

    ts_iso = _parse_bybit_ts(row.get("Trade time", "")) or datetime.now(timezone.utc).isoformat()

    return {
        "ts":                  ts_iso,
        "mode":                "live",
        "asset":               asset,
        "direction":           direction,
        "entry_price":         round(entry, 6),
        "exit_price":          round(exit_, 6),
        "pnl_pct":             pnl_pct,
        "order_id":            None,  # CSV doesn't include order_id
        "qty":                 qty,
        "leverage":            None,
        "sl_price":            None,
        "tp_price":            None,
        "rr_ratio":            None,
        "strategy_version":    "csv_import",
        "indicators_snapshot": {},
        "indicators_fired":    {},
        "confidence_at_entry": None,
        # Audit additions
        "confidence_breakdown": {},
        "evaluation_summary":   "",
        "entry_gates":          {},
        "close_reason":         "csv_imported",
        # CSV-specific provenance
        "csv_imported":        True,
        "closed_pnl_usdt":     round(pnl, 6),
        "fees_usdt":           round(open_f + close_f - fund_f, 6),
    }


def _trade_key(asset: str, entry, exit_, qty) -> tuple | None:
    """
    Time-independent dedup fingerprint: (asset, entry@2dp, exit@2dp, qty@4dp).

    Local trades record OPEN time; Bybit CSV records CLOSE time — they're typically
    several hours apart for the same trade. We deduplicate on entry+exit+qty instead,
    which is highly unique in practice. Open trades (no exit yet) are matched on
    (asset, entry, None, qty) and will only collide with another open at exact same
    entry and qty — vanishingly unlikely.
    """
    try:
        e = round(float(entry), 2) if entry is not None else None
        x = round(float(exit_), 2) if exit_ is not None else None
        q = round(float(qty), 4)   if qty   is not None else None
    except (TypeError, ValueError):
        return None
    if asset and e is not None and q is not None:
        return (asset, e, x, q)
    return None


def _existing_keys(trades_path: Path) -> set[tuple]:
    """Build dedup key set from existing trades.jsonl using _trade_key fingerprint."""
    if not trades_path.exists():
        return set()
    keys: set[tuple] = set()
    for line in trades_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            t = json.loads(line)
        except json.JSONDecodeError:
            continue
        k = _trade_key(t.get("asset"), t.get("entry_price"), t.get("exit_price"), t.get("qty"))
        if k is not None:
            keys.add(k)
    return keys


def import_csv(csv_path: Path, state_root: Path, dry_run: bool) -> dict:
    """Walk the CSV; append non-duplicates to each per-asset trades.jsonl."""
    if not csv_path.exists():
        return {"error": f"CSV not found: {csv_path}"}

    by_slug: dict[str, list[dict]] = {}
    bad_rows = 0
    total_rows = 0
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            total_rows += 1
            slug = _market_to_slug(row.get("Market", ""))
            if not slug:
                bad_rows += 1
                continue
            trade = _row_to_trade(row)
            if trade is None:
                bad_rows += 1
                continue
            by_slug.setdefault(slug, []).append(trade)

    stats_by_slug: dict[str, dict] = {}
    for slug, trades in by_slug.items():
        trades_path = state_root / slug / "trades.jsonl"
        existing = _existing_keys(trades_path)
        new_records = []
        skipped_dupe = 0
        for t in trades:
            key = _trade_key(t.get("asset"), t.get("entry_price"), t.get("exit_price"), t.get("qty"))
            if key is None or key in existing:
                skipped_dupe += 1
                continue
            new_records.append(t)
            existing.add(key)

        if new_records and not dry_run:
            trades_path.parent.mkdir(parents=True, exist_ok=True)
            # Sort by ts before append so file stays chronological
            combined = []
            if trades_path.exists():
                for line in trades_path.read_text().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        combined.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            combined.extend(new_records)
            combined.sort(key=lambda x: x.get("ts", ""))
            tmp = trades_path.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(json.dumps(t) for t in combined) + "\n")
            tmp.replace(trades_path)

        stats_by_slug[slug] = {
            "csv_rows":    len(trades),
            "added":       len(new_records),
            "skipped_dupe": skipped_dupe,
        }

    return {
        "total_rows":   total_rows,
        "bad_rows":     bad_rows,
        "by_slug":      stats_by_slug,
        "dry_run":      dry_run,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill trades.jsonl from a Bybit closed-PnL CSV export"
    )
    parser.add_argument("--csv",        required=True, help="Path to Bybit CSV export")
    parser.add_argument("--state-root", default="state", help="Per-asset state root")
    parser.add_argument("--dry-run",    action="store_true", help="Don't write")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    state_root = Path(args.state_root)
    if not state_root.exists():
        print(f"ERROR: state root {state_root} missing", file=sys.stderr)
        return 1

    stats = import_csv(csv_path, state_root, args.dry_run)
    if "error" in stats:
        print(f"ERROR: {stats['error']}", file=sys.stderr)
        return 1

    print(f"CSV: {csv_path}  total_rows={stats['total_rows']}  bad={stats['bad_rows']}  dry_run={stats['dry_run']}")
    print()
    grand_added = 0
    for slug, s in sorted(stats["by_slug"].items()):
        print(f"  [{slug}] csv_rows={s['csv_rows']:3d}  added={s['added']:3d}  dupe={s['skipped_dupe']:3d}"
              f"{'  (dry-run)' if stats['dry_run'] else ''}")
        grand_added += s["added"]
    print()
    print(f"Total added: {grand_added}{' (dry-run, nothing written)' if stats['dry_run'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
