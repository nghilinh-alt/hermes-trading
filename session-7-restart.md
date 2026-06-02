# Session 7 — Restart after 3-day downtime

**Date**: 2026-06-01
**Status**: Bot has been DOWN since 2026-05-29 ~06:30 UTC. Diagnostics complete. Targeted single-file fix + restart ahead.

## Diagnosis

### Symptom
- `ps -ef | grep hermes_trading.run` empty on VPS.
- All 4 heartbeats stale at `2026-05-29T06:30:04 UTC`.
- 0 new hypotheses since session 6 end.

### Root cause: incomplete wave-4 deploy
Commit `ee0dea2` (wave-4 audit + None-safety) touched three files:
- `hermes_trading/loop.py` — deployed on VPS ✓
- `hermes_trading/adapters/execution.py` — deployed on VPS ✓
- `hermes_trading/reflect.py` — **NOT deployed on VPS** ✗

VPS reflect.py crashed with the exact pre-fix bug at line 434:

```python
"total_pnl": round(sum(float(t.get("pnl_pct", 0)) for t in trades), 6),
# t.get("pnl_pct", 0) returns None (not 0) when key exists with value None
# → float(None) → TypeError
```

Reflection subprocess died, was swallowed by loop.py (so bot kept ticking until 06:30), then **process killed externally** (no traceback in log, log ends mid-tick, no OOM, no reboot, VPS up 69 days). Most likely: pkill for an intended redeploy that never completed.

### What's on VPS now (fingerprint check)
| Wave | Marker | VPS state |
|------|--------|-----------|
| 3 | `cumEntryValue` in execution.py | ✓ ×4 |
| 3 | `num_ctx` in reflect.py | ✓ ×3 |
| 4 | `confidence_breakdown` in execution.py | ✓ ×1 |
| 4 | `_entry_gates_snapshot` in loop.py | ✓ ×2 |
| 4 | `pnl_pct is not None` in reflect.py | **✗ MISSING (5 expected)** |

### Commits to land on VPS (all touch reflect.py only)
- `ee0dea2` — wave-4 audit fields + None-safety filter
- `2905485` — `_max_drawdown` wealth-curve fix (prevents 80%+ false drawdowns → bad fallback mutations)
- `004d742` — `_set_nested` clobber guards (defense against LLM dot-notation)

## Side notes (do not block restart)
- **Open positions on Bybit** have been managed by exchange-side SL/TP since 2026-05-29 (params on order creation). Any closures during downtime are NOT reflected in local trades.jsonl. On restart, `_reconcile_open_trades` will catch up the most recent close per asset; older intermediate closes are lost unless `tools/backfill_trades.py` is run (it has a 7-day Bybit window — should still cover the downtime).
- **cumRealisedPnl on Bybit balance still shows -$18,251** (was -$18,272 at session-6 end). Tiny improvement consistent with continued small wins on TP-set positions.
- **All 4 strategy.yamls unchanged on VPS** (no auto-reflection fired during downtime).

## Deploy block — execute one section at a time

### Step 1 — Push local commits to GitHub (PowerShell)
```powershell
cd C:\Users\nghil\Projects\Hermes\Hermes-Trading
git push origin master
```
Expected: pushes commits up to `3e8c64c` (or "Everything up-to-date" if already pushed).

### Step 2 — On VPS: git pull in hyphen clone (one command via SSH)
```powershell
ssh root@187.127.108.173 "cd /opt/trading/hermes-trading && git pull"
```
Expected: fast-forward to latest. Note the new commits.

### Step 3 — Diff hyphen reflect.py vs running underscore reflect.py
```powershell
ssh root@187.127.108.173 "diff /opt/trading/hermes-trading/hermes_trading/reflect.py /opt/trading/hermes_trading/hermes_trading/reflect.py | head -80"
```
Expected: clear diff showing the None-safety + audit field additions. If empty → already in sync (would be surprising given the crash signature).

### Step 4 — Backup current VPS reflect.py
```powershell
ssh root@187.127.108.173 "cp /opt/trading/hermes_trading/hermes_trading/reflect.py /tmp/reflect.py.bak-$(date +%s) && ls -la /tmp/reflect.py.bak-*"
```
Expected: shows backup file with current timestamp.

### Step 5 — Copy fixed reflect.py into running underscore clone
```powershell
ssh root@187.127.108.173 "cp /opt/trading/hermes-trading/hermes_trading/reflect.py /opt/trading/hermes_trading/hermes_trading/reflect.py"
```
Expected: silent success.

### Step 6 — Verify the fix landed + py_compile clean
```powershell
@'
echo "=== None-safety filter count (expect >= 5) ==="
grep -c "pnl_pct.*is not None\|is not None.*pnl_pct" /opt/trading/hermes_trading/hermes_trading/reflect.py
echo
echo "=== Wave-4 audit fields in reflect.py (expect 8 hits) ==="
grep -c "decision_context\|trade_range\|applied_successfully\|llm_raw_output" /opt/trading/hermes_trading/hermes_trading/reflect.py
echo
echo "=== _max_drawdown wealth-curve formula (expect 'wealth' literal) ==="
grep -c "wealth" /opt/trading/hermes_trading/hermes_trading/reflect.py
echo
echo "=== py_compile ==="
cd /opt/trading/hermes_trading && .venv/bin/python -m py_compile hermes_trading/reflect.py && echo "OK"
'@ | ssh root@187.127.108.173 bash
```
Expected: ≥5, 8, ≥1, OK. If any fail → STOP, do not restart, ping for advice.

### Step 7 — Restart agent
```powershell
@'
cd /opt/trading/hermes_trading
pkill -f hermes_trading.run 2>/dev/null
sleep 2
set -a; source .env; set +a
nohup .venv/bin/python -m hermes_trading.run >> bot.log 2>&1 &
sleep 3
echo "=== New PID ==="
ps -ef | grep hermes_trading.run | grep -v grep
echo "=== bot.log tail right after start ==="
tail -20 bot.log
'@ | ssh root@187.127.108.173 bash
```
Expected: shows fresh PID + 4 worker boot lines.

### Step 8 — Verify all 4 workers ticking (run ~16 minutes after Step 7)
```powershell
@'
echo "=== Heartbeats (should all show today) ==="
for slug in btc_usdt eth_usdt sol_usdt tao_usdt; do
  printf "%s: " "$slug"
  cat /opt/trading/hermes_trading/state/$slug/heartbeat.json
  echo
done
echo
echo "=== Last 30 lines of bot.log (should show 4 assets with dir=both lines) ==="
tail -30 /opt/trading/hermes_trading/bot.log
'@ | ssh root@187.127.108.173 bash
```
Expected: 4 heartbeats with `last_tick` after current minus 16 min; 4 assets seen in tail.

## After restart — open follow-ups
1. **Run `tools/backfill_trades.py`** to recover any trades that closed during the 3-day downtime (7-day Bybit window should cover it).
2. **Reconsider Phase 2.8** (volume_spike hallucination check) before next Hermes reflection — current TAO v03 may be acting on a flawed mutation.
3. **Phase 2.2 (ATR trailing stop)** resumes only after bot is verified stable for ≥24 h.

## Handoff
After Step 8 confirms 4 workers ticking:
- **To**: Linh (monitor)
- **Read**: `session-7-restart.md` (this file), `memory.md` for next-session item 2.8
- **Next session opener**: "Bot was restored 2026-06-01 after 3-day downtime caused by reflect.py None-safety patch not landing in wave-4 deploy. Resume Phase 2 backlog per `next-session-prompt.md`, starting with whichever item Linh prioritises."
