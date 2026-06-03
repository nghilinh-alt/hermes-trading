"""
backfill_trades.py — One-shot Bybit closed-trade backfill.

Why this exists (Phase 2.4, 2026-05-28):
  Trades that fired on Bybit *before* the per-asset state structure existed —
  or trades where execution._log_trade failed silently — never landed in
  state/<slug>/trades.jsonl. _count_closed_trades in loop.py only counts
  lines in that file, so the reflection cadence threshold never crosses,
  and Hermes never learns from those trades. Example: TAO had ~20 closed
  trades on Bybit at session start, 0 in local trades.jsonl.

What it does:
  For each asset, calls Bybit's private_get_v5_position_closed_pnl, loads
  the existing trades.jsonl, and appends any closed-trade entries not
  already present (dedup by order_id and exit/qty fingerprint). Synthetic records are flagged
  "backfilled": true and use strategy_version "backfilled" so reflection
  can recognize them.

  pnl_pct is computed as closedPnl / cumEntryValue, which is
  direction-correct (positive for winning trades regardless of long/short).
  This sidesteps the still-broken (exit-entry)/entry formula in
  execution.fetch_last_closed_pnl — Phase 2.5 will fix that separately.

Idempotent: running twice is a no-op (dedup by order_id + exit/qty fingerprint).

Usage (on VPS):
  cd /opt/trading/hermes_trading
  source .venv/bin/activate
  python3 -m tools.backfill_trades --state-root /opt/trading/hermes_trading/state
  # or for a single asset:
  python3 -m tools.backfill_trades --asset tao_usdt
  # preview-only:
  python3 -m tools.backfill_trades --dry-run

Env vars (same as execution.py):
  BYBIT_API_KEY                  — required
  BYBIT_API_SECRET               — required (HMAC) or
  BYBIT_RSA_PRIVATE_KEY_PATH     — alternative (RSA)
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import ccxt
except ImportError:
    print("ERROR: ccxt not installed. Run: pip install ccxt", file=sys.stderr)
    sys.exit(1)


DEFAULT_ASSETS = ["btc_usdt", "eth_usdt", "sol_usdt", "tao_usdt"]
DEFAULT_LOOKBACK_DAYS = 30


# ── ccxt client (mirrors execution._get_exchange) ─────────────────────────────


def _get_exchange() -> "ccxt.bybit":
    api_key = os.getenv("BYBIT_API_KEY", "")
    if not api_key:
        raise SystemExit("BYBIT_API_KEY must be set")

    key_path = os.getenv("BYBIT_RSA_PRIVATE_KEY_PATH", "")
    if key_path and Path(key_path).exists():
        secret = Path(key_path).read_text()
    else:
        secret = os.getenv("BYBIT_API_SECRET", "")
        if not secret:
            raise SystemExit(
                "Either BYBIT_RSA_PRIVATE_KEY_PATH or BYBIT_API_SECRET must be set"
            )

    return ccxt.bybit({
        "apiKey": api_key,
        "secret": secret,
        "enableRateLimit": True,
        "options": {"defaultType": "linear"},
    })


# ── Asset helpers ─────────────────────────────────────────────────────────────


def _slug_to_asset(slug: str) -> str:
    """btc_usdt → BTC/USDT"""
    base, quote = slug.upper().split("_")
    return f"{base}/{quote}"


def _slug_to_bybit_symbol(slug: str) -> str:
    """btc_usdt → BTCUSDT"""
    return slug.upper().replace("_", "")


# ── trades.jsonl I/O ──────────────────────────────────────────────────────────


def _exit_qty_fp(exit_price, qty):
    """Secondary fingerprint (exit@2dp, qty@4dp) — catches native vs backfill dedup."""
    try:
        if exit_price is None or qty is None:
            return None
        return (round(float(exit_price), 2), round(float(qty), 4))
    except (TypeError, ValueError):
        return None


def _load_existing_keys(trades_path: Path) -> tuple[set[str], set[tuple], set[float]]:
    """Return (order_id_set, exit_qty_fingerprint_set, open_qty_set).

    Three-key dedup:
      - order_id: catches same-source duplicates
      - (exit@2dp, qty@4dp): catches native-vs-backfill for already-reconciled trades
      - open_qty@4dp: catches native-vs-backfill for trades that are STILL OPEN locally
        (exit_price=None) but already closed on Bybit. The bot can only have one open
        position per asset, so any backfill record whose qty matches an open native
        record is the same trade and should be skipped.
    """
    if not trades_path.exists():
        return set(), set(), set()
    ids: set[str] = set()
    fps: set[tuple] = set()
    open_qtys: set[float] = set()
    for line in trades_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        oid = rec.get("order_id")
        if oid:
            ids.add(str(oid))
        if rec.get("exit_price") is None:
            # Open trade — track qty so backfill won't add a duplicate while it's unreconciled
            try:
                q = round(float(rec["qty"]), 4)
                open_qtys.add(q)
            except (KeyError, TypeError, ValueError):
                pass
        else:
            fp = _exit_qty_fp(rec.get("exit_price"), rec.get("qty"))
            if fp is not None:
                fps.add(fp)
    return ids, fps, open_qtys


def _append_trades(trades_path: Path, records: list[dict]) -> None:
    """Append records to trades.jsonl atomically (jsonl supports plain append)."""
    trades_path.parent.mkdir(parents=True, exist_ok=True)
    with open(trades_path, "a") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


# ── Bybit closed-pnl → trade record ───────────────────────────────────────────


def _bybit_item_to_trade(item: dict, asset: str) -> dict | None:
    """
    Convert one Bybit closed-pnl list item into a trades.jsonl-compatible record.

    Returns None if the item is missing critical fields (defensive).
    """
    order_id = item.get("orderId") or item.get("orderID")
    if not order_id:
        return None

    side = (item.get("side") or "").lower()
    direction = "long" if side == "buy" else ("short" if side == "sell" else None)
    if direction is None:
        return None

    try:
        entry_price = float(item.get("avgEntryPrice") or 0)
        exit_price  = float(item.get("avgExitPrice")  or 0)
        qty         = float(item.get("qty")           or 0)
        leverage    = int(float(item.get("leverage")  or 1))
        cum_entry   = float(item.get("cumEntryValue") or 0)
        closed_pnl  = float(item.get("closedPnl")     or 0)
    except (TypeError, ValueError):
        return None

    if entry_price <= 0 or cum_entry <= 0:
        return None

    # Direction-correct pnl_pct: realized PnL as fraction of entry notional.
    # closed_pnl is already signed (negative for losses), so dividing by
    # the always-positive cum_entry gives the right sign for both long and short.
    pnl_pct = round(closed_pnl / cum_entry, 6)

    # Bybit timestamps are ms-since-epoch as strings
    created_ms = item.get("createdTime") or item.get("updatedTime") or "0"
    try:
        ts_iso = datetime.fromtimestamp(int(created_ms) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        ts_iso = datetime.now(timezone.utc).isoformat()

    return {
        "ts":                  ts_iso,
        "mode":                "live",
        "asset":               asset,
        "direction":           direction,
        "entry_price":         round(entry_price, 6),
        "exit_price":          round(exit_price, 6),
        "pnl_pct":             pnl_pct,
        "order_id":            str(order_id),
        "qty":                 qty,
        "leverage":            leverage,
        "sl_price":            None,
        "tp_price":            None,
        "rr_ratio":            None,
        "strategy_version":    "backfilled",
        "indicators_snapshot": {},
        "indicators_fired":    {},
        "confidence_at_entry": None,
        # Backfill-specific flags so reflection can tell these apart
        "backfilled":          True,
        "closed_pnl_usdt":     round(closed_pnl, 6),
        "cum_entry_value":     round(cum_entry, 6),
    }


# ── Per-asset backfill ────────────────────────────────────────────────────────


def _fetch_closed_pnl_paginated(
    exchange: "ccxt.bybit",
    symbol: str,
    since_ms: int,
    page_limit: int = 100,
    max_pages: int = 10,
) -> list[dict]:
    """
    Page through Bybit's closed-pnl endpoint using nextPageCursor.

    Bybit returns at most 'page_limit' items per call (Bybit's own max is 100).
    Stops early when no more pages or when items predate since_ms.
    """
    all_items: list[dict] = []
    cursor: str | None = None
    for _ in range(max_pages):
        params = {
            "category": "linear",
            "symbol":   symbol,
            "limit":    page_limit,
            "startTime": since_ms,
        }
        if cursor:
            params["cursor"] = cursor

        resp = exchange.private_get_v5_position_closed_pnl(params)
        items = (resp.get("result") or {}).get("list") or []
        all_items.extend(items)

        cursor = (resp.get("result") or {}).get("nextPageCursor") or ""
        if not cursor or not items:
            break
        time.sleep(0.2)  # gentle rate-limit

    return all_items


def backfill_asset(
    exchange: "ccxt.bybit",
    slug: str,
    state_root: Path,
    since_ms: int,
    dry_run: bool,
) -> dict:
    """
    Backfill one asset. Returns a stats dict.
    """
    asset        = _slug_to_asset(slug)
    symbol       = _slug_to_bybit_symbol(slug)
    trades_path  = state_root / slug / "trades.jsonl"

    existing_ids, existing_fps, existing_open_qtys = _load_existing_keys(trades_path)

    try:
        items = _fetch_closed_pnl_paginated(exchange, symbol, since_ms)
    except Exception as e:
        return {"slug": slug, "error": f"fetch failed: {e}", "added": 0, "skipped_dupe": 0}

    new_records: list[dict] = []
    skipped_dupe = 0
    skipped_bad  = 0
    for item in items:
        rec = _bybit_item_to_trade(item, asset)
        if rec is None:
            skipped_bad += 1
            continue
        # Primary dedup: order_id (same-source)
        if rec["order_id"] in existing_ids:
            skipped_dupe += 1
            continue
        # Secondary dedup: (exit@2dp, qty@4dp) catches native vs backfill pairs
        # where opening order_id != closing order_id on Bybit
        fp = _exit_qty_fp(rec.get("exit_price"), rec.get("qty"))
        if fp is not None and fp in existing_fps:
            skipped_dupe += 1
            continue
        # Tertiary dedup: if a native open record (exit=None) has the same qty,
        # this backfill record is the same trade — skip until bot reconciles it
        try:
            rec_qty = round(float(rec.get("qty", 0)), 4)
        except (TypeError, ValueError):
            rec_qty = None
        if rec_qty is not None and rec_qty in existing_open_qtys:
            skipped_dupe += 1
            continue
        new_records.append(rec)
        existing_ids.add(rec["order_id"])
        if fp is not None:
            existing_fps.add(fp)
        if rec_qty is not None:
            existing_open_qtys.discard(rec_qty)  # consumed once trade is reconciled

    if new_records and not dry_run:
        _append_trades(trades_path, new_records)

    return {
        "slug":             slug,
        "bybit_returned":   len(items),
        "added":            len(new_records),
        "skipped_dupe":     skipped_dupe,
        "skipped_bad":      skipped_bad,
        "existing_before":  len(existing_ids) - len(new_records),
        "existing_after":   len(existing_ids),
        "dry_run":          dry_run,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill Bybit closed trades into state/<slug>/trades.jsonl"
    )
    parser.add_argument(
        "--state-root",
        default="state",
        help="Path to per-asset state root (default: ./state)",
    )
    parser.add_argument(
        "--asset",
        action="append",
        help="Asset slug (e.g. tao_usdt). Repeatable. Default: all 4.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=f"How many days back to query (default: {DEFAULT_LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be appended without writing.",
    )
    args = parser.parse_args()

    state_root = Path(args.state_root)
    if not state_root.exists():
        print(f"ERROR: state root {state_root} does not exist", file=sys.stderr)
        return 1

    assets = args.asset or DEFAULT_ASSETS
    since_ms = int((time.time() - args.lookback_days * 86400) * 1000)

    exchange = _get_exchange()

    print(f"Backfill — state_root={state_root} assets={assets} lookback={args.lookback_days}d dry_run={args.dry_run}")
    print()

    grand_added = 0
    for slug in assets:
        stats = backfill_asset(exchange, slug, state_root, since_ms, args.dry_run)
        if "error" in stats:
            print(f"  [{slug}] ERROR: {stats['error']}")
            continue
        print(
            f"  [{slug}] bybit={stats['bybit_returned']:3d}  "
            f"added={stats['added']:3d}  "
            f"dupe={stats['skipped_dupe']:3d}  "
            f"bad={stats['skipped_bad']:3d}  "
            f"local: {stats['existing_before']} -> {stats['existing_after']}"
            f"{'  (dry-run)' if stats['dry_run'] else ''}"
        )
        grand_added += stats["added"]

    print()
    print(f"Total added: {grand_added}{' (dry-run, nothing written)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
