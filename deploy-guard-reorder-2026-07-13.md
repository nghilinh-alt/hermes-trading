# Deploy — Structural guard reorder (min_tp_pct fix) — 2026-07-13 (session 15)

_Linh's decision on the session-14/15 root cause: **reorder the guards**. This is the fix for near-zero trades. One variable. Local commit `c78dcf8`._

## What changed (one variable)

In `execution.py::_structural_sl_tp()`, the soft R:R extension (Guard 3) now runs **before** the `min_tp_pct` hard-reject (was Guard 2). So the 3% floor is applied to the **final** take-profit — after a thin structural TP has been widened to `entry ± sl_dist × min_rr_ratio` — instead of to the raw nearest-swing level. No config change, no signature change, no new parameter. `min_tp_pct` stays 3.0.

**Why this is the fix:** the nearest 1h/4h structural TP on a 15m chart is usually <3% away, so it was being rejected before the R:R logic that would have widened it (to ~4% when SL≈2%) ever ran. 3,255 rejections over 4d20h were dying at exactly this step. Reordering keeps a real 3% floor on the TP we actually trade while recovering the trades that were being killed prematurely.

## Local verification (done)

- `python3 -m py_compile` clean.
- Function extracted from both the pre-patch backup and the patched file and run head-to-head on three scenarios:

  | Scenario | OLD (min_tp first) | NEW (R:R first) |
  |---|---|---|
  | Thin structural TP ~1%, SL ~2% (the common killed case) | **REJECT** | **OK**, TP widened to 4.59% |
  | Genuinely thin structure (SL & TP both tiny) | REJECT | **REJECT** (floor still bites) |
  | Already-good 5% structural TP | OK, TP 5% | OK, TP 5% (identical) |

  Confirms: recovers the dominant killed case, still rejects genuinely thin structure, no regression on good setups.
- Committed locally as `c78dcf8` (execution.py only).

## ⚠️ Deploy by patch, NOT by scp — read this

The local `execution.py` **also contains the session-12 position-sizing fix (`d8a49c7`, `75ef76b`) that was deliberately never deployed.** Copying the whole file to the VPS would ship two changes at once and break the one-variable rule. So we deploy with a **content-based patch script** (`tools/patch_guard_reorder.py`) that reorders only the guard blocks in whatever `execution.py` is currently on the VPS. It matches by exact text, backs up first, py_compiles, and is idempotent (safe to re-run). It has been self-tested against a copy of the VPS-equivalent (pre-position-sizing) file and applied cleanly.

## ⚠️ This is a LIVE-money change

BTC/ETH/SOL/XRP all trade live. This fix makes signals actually reach execution, so **real positions will start firing** — expect trades at ~3–5% TP distances. The undeployed position-sizing fix means notionals stay at the current (un-leveraged) sizing until that's separately deployed. Watch the first live trades closely.

## Deploy steps (Linh runs — ONE block at a time)

VPS running (underscore) clone: `/opt/trading/hermes_trading/`; target file `/opt/trading/hermes_trading/hermes_trading/adapters/execution.py`.

**1. Copy the patch script up** (from the repo root on your PC):
```powershell
scp tools/patch_guard_reorder.py root@187.127.108.173:/opt/trading/hermes_trading/tools/patch_guard_reorder.py
```

**2. Apply it to the running clone's execution.py** (backs up + py_compiles):
```powershell
ssh root@187.127.108.173 "cd /opt/trading/hermes_trading && python3 tools/patch_guard_reorder.py hermes_trading/adapters/execution.py"
```
Expect: `Patched OK. Backup at ...execution.py.bak ; py_compile clean.` If it says `ABORT: expected exactly 1 match`, STOP — the VPS file differs from expectation; do not force, inspect first.

**3. Restart the agent** (setsid per doctrine #8; loads .env for live mode):
```powershell
@'
pkill -f hermes_trading.run; sleep 3
cd /opt/trading/hermes_trading
set -a; source .env; set +a
setsid .venv/bin/python -m hermes_trading.run >> bot.log 2>&1 &
sleep 5; pgrep -af hermes_trading.run
'@ | ssh root@187.127.108.173 bash
```
Expect a new PID (different from 2248353).

**4. Verify (immediately, then again in a few hours):**
```powershell
@'
echo "--- process ---"; pgrep -af hermes_trading.run
echo "--- new-run start marker ---"; grep -n "ignoring input" /opt/trading/hermes_trading/bot.log | tail -1
echo "--- TP-too-thin rejects since restart (should fall sharply vs before) ---"
tail -2000 /opt/trading/hermes_trading/bot.log | grep -c "Structural TP too thin"
echo "--- any trades opening ---"
for f in /opt/trading/hermes_trading/state/*/trades.jsonl; do echo "$(basename $(dirname $f)): $(wc -l < $f)"; done
echo DONE
'@ | ssh root@187.127.108.173 bash
```

## Rollback

```powershell
ssh root@187.127.108.173 "cp /opt/trading/hermes_trading/hermes_trading/adapters/execution.py.bak /opt/trading/hermes_trading/hermes_trading/adapters/execution.py"
```
then re-run step 3.

## Measurement + next gate

- Measure over **3–5 days**, one variable held. Expect trade frequency to recover from ~0.
- **Next gate to watch: `min_profit_usd: 5.0`.** Once TPs clear, small un-leveraged positions may fail the $5 floor next — check its skip count in the same window before proposing any further change.
- Once any asset reaches 5 closed trades, the reflection loop finally activates for the first time — watch `hypotheses.jsonl` for the first mutations.

## Handoff

- **Status**: Implemented + verified locally (commit `c78dcf8`), patch script self-tested. **Not yet on the VPS** — Linh runs the 4 blocks above.
- **Flag**: Internal. Live-money change; deploy consciously. Position-sizing fix remains intentionally undeployed and out of scope here.
