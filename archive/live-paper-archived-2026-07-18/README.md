# Archived strategy configs — 2026-07-18

Full archive of the retired indicator-weight system, ahead of building the
new **ICT swing strategy** (`hermes_trading/ict/`, see repo root
`ict-strategy-plan-2026-07-18.md`).

## What's here
- `state/` — the full live state tree (`git mv`'d from repo root): per-asset
  `strategy.yaml` / `trades.jsonl` / `hypotheses.jsonl` for
  `btc_usdt / eth_usdt / sol_usdt / tao_usdt / xrp_usdt`, plus the stale
  root-level `state/strategy.yaml`, `state/goal.yaml`, `state/trades.jsonl`,
  `state/hypotheses.jsonl`, `state/memory.md` (pre-per-asset remnants, kept
  for completeness).
- `state-scalp/` — the never-deployed session-15 scalp stubs
  (`_scalp-strategy-template.yaml`, `goal.yaml`).
- `root-strategy.yaml.legacy-root` — the top-level `strategy.yaml` that
  predates the per-asset `state/` structure entirely.
- `patch_strategies.py` — the one-time migration script that patched the
  per-asset yamls (session context, already applied).

(An earlier pass at this archive kept flat single-file YAML *copies*
alongside the live originals. Those copies are now superseded by the full
trees above and have been removed to avoid duplication.)

## Status at archival — HALT CONFIRMED
- **Live worker halted 2026-07-18** (this session): PID 2433063 (07-14
  guard-reorder deploy) was still running at session start.
- **Pre-halt safety check**: read-only `has_open_position()` call against
  live Bybit for all 5 assets confirmed **flat** (BTC/ETH/SOL/XRP/TAO all
  `flat`, zero open positions) immediately before halting.
- **Halt executed**: `pkill -f hermes_trading.run` on the VPS. Confirmed
  stopped — no `hermes_trading.run` process in the VPS process list, and
  `bot.log`'s last entry matches the pre-halt heartbeat timestamp with no
  activity since.
- The VPS `.venv/`, `bot.log`, and running-code directories were left in
  place untouched (rollback material) — nothing was deleted on the VPS,
  only the worker process was stopped.

## Rollback
If the old system ever needs to run again: the code is unchanged in git
history before this archival commit, and the VPS files are untouched
(only the process was killed). Restart with:
```
cd /opt/trading/hermes_trading && source .venv/bin/activate
setsid .venv/bin/python -m hermes_trading.run >> bot.log 2>&1 &
```
The new ICT system (`hermes_trading/ict/`) is a fully separate, isolated
package — building/testing it never touched this archive or the VPS
worker process.
