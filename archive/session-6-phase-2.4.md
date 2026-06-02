# Session 6 — Phase 2.4 (Reflection Rebuild + $5 Floor)

_Date: 2026-05-28. Author: Claude (Frontier agent). **Flagged for Linh's review before deploy.**_

## What landed (local, on `master` branch unstaged)

| File | Change | Lines |
|------|--------|-------|
| `hermes_trading/reflect.py` | Truncated duplicated tail (IndentationError fix) | 702 → 572 |
| `hermes_trading/adapters/execution.py` | New `_guard_min_profit_usd` helper + call in `place_live_trade` + docstring update | +30 |
| `state/{btc,eth,sol,tao}_usdt/strategy.yaml` | Appended `min_profit_usd: 5.0` | +1 each |
| `tools/backfill_trades.py` (NEW) | One-shot Bybit closed-trade backfill, dedupes by `order_id`, direction-correct `pnl_pct` from `closedPnl / cumEntryValue` | +275 |
| `tools/__init__.py` (NEW) | Makes `tools` a package so `python -m tools.backfill_trades` works | +1 |
| `memory.md` | Session 6 log entry + Key Decisions row + Active State update | _separate commit ok_ |

**Not touched:** `state/goal.yaml`, `snapshot.sh`, `state/tao_usdt/trades.jsonl`, any other file showing in `git status`. Those are stale from prior sessions; Linh should review separately.

## Verification done in sandbox

- `python -m py_compile` and `ast.parse` both clean for `reflect.py` and `execution.py`.
- 6 backfill unit tests pass: winning-short → +pnl_pct, losing-long → −pnl_pct, malformed-item → None, dedup against existing, idempotent re-run is no-op, `--dry-run` writes nothing.
- 6 guard unit tests pass: $5 floor pass/fail, custom floor, zero-profit edge, TAO Trade #1 still correctly rejected by min_tp_pct (defense-in-depth), and a constructed trade that passes all Phase 2.1 guards but tiny notional is correctly blocked by the new $-floor.
- `python3 -m tools.backfill_trades --help` renders.

## Rationale for the $5 floor (decision locked this session)

Linh's directive: "aim to win at least $5 per trade." Interpretation chosen: **hard guard** in `place_live_trade` after qty is computed. Skip the trade if `qty × |tp − entry| < min_profit_usd`. Default `min_profit_usd: 5.0` in all 4 strategy.yamls; reflection can tune it later. Co-located with the existing `max_sl_pct / min_tp_pct / min_rr_ratio` guards as "Guard 4 ($-floor)."

Why a hard guard and not just a reflection target: with `min_tp_pct: 3.0` and `risk_per_trade: 0.10`, the trade clears $5 only when `balance × risk_per_trade ≥ 5` → balance ≥ $50. Below that, the bot would otherwise place positions whose expected USDT gain is < $5 even on perfect entries. The guard makes that impossible.

## PowerShell push block (Linh)

```powershell
cd C:\Users\nghil\Projects\Hermes\Hermes-Trading

# Clear any Windows-mount git locks (per session 5e hazard)
if (Test-Path .git\index.lock)  { Move-Item .git\index.lock  ".git\index.lock.$(Get-Date -UFormat %s)" -Force }
if (Test-Path .git\HEAD.lock)   { Move-Item .git\HEAD.lock   ".git\HEAD.lock.$(Get-Date -UFormat %s)" -Force }

# Stage ONLY Phase 2.4 files (avoid catching stale prior-session edits)
git add hermes_trading/reflect.py
git add hermes_trading/adapters/execution.py
git add state/btc_usdt/strategy.yaml state/eth_usdt/strategy.yaml state/sol_usdt/strategy.yaml state/tao_usdt/strategy.yaml
git add tools/__init__.py tools/backfill_trades.py
git add memory.md session-6-phase-2.4.md

git status   # sanity-check before commit

git commit -m "feat(phase2.4): reflect.py truncate + min_profit_usd `$5 guard + Bybit backfill tool"
git push origin master
```

## VPS deploy block (Linh — SSH paste)

Single-line semicolons throughout per session 5c lesson. **Read both before running.**

### Step 1: backup + deploy code (no agent restart yet)

```bash
ssh root@187.127.108.173 'STAMP=$(date +%Y%m%d_%H%M%S); BACKUP=/tmp/hermes-backup-$STAMP; mkdir -p $BACKUP/state; cp /opt/trading/hermes_trading/hermes_trading/reflect.py $BACKUP/; cp /opt/trading/hermes_trading/hermes_trading/adapters/execution.py $BACKUP/; cp -r /opt/trading/hermes_trading/state/*/strategy.yaml $BACKUP/state/ 2>/dev/null; for slug in btc_usdt eth_usdt sol_usdt tao_usdt; do cp /opt/trading/hermes_trading/state/$slug/strategy.yaml $BACKUP/state/${slug}_strategy.yaml; done; echo "Backup at $BACKUP"; ls -la $BACKUP/ $BACKUP/state/'
```

### Step 2: pull, diff, copy code (do NOT cp the strategy.yamls — they may be reflection-evolved)

```bash
ssh root@187.127.108.173 'cd /opt/trading/hermes-trading && git pull && echo "--- diff reflect.py ---" && diff hermes_trading/reflect.py /opt/trading/hermes_trading/hermes_trading/reflect.py | head -20 && echo "--- diff execution.py ---" && diff hermes_trading/adapters/execution.py /opt/trading/hermes_trading/hermes_trading/adapters/execution.py | head -20'
```

If diffs look like the Phase 2.4 changes only (no surprise drift), proceed:

```bash
ssh root@187.127.108.173 'cp /opt/trading/hermes-trading/hermes_trading/reflect.py /opt/trading/hermes_trading/hermes_trading/reflect.py && cp /opt/trading/hermes-trading/hermes_trading/adapters/execution.py /opt/trading/hermes_trading/hermes_trading/adapters/execution.py && mkdir -p /opt/trading/hermes_trading/tools && cp /opt/trading/hermes-trading/tools/__init__.py /opt/trading/hermes_trading/tools/ && cp /opt/trading/hermes-trading/tools/backfill_trades.py /opt/trading/hermes_trading/tools/ && cd /opt/trading/hermes_trading && python3 -m py_compile hermes_trading/reflect.py hermes_trading/adapters/execution.py tools/backfill_trades.py && echo "py_compile OK"'
```

### Step 3: append `min_profit_usd: 5.0` to RUNNING yamls if absent (preserves evolved values)

```bash
ssh root@187.127.108.173 'for slug in btc_usdt eth_usdt sol_usdt tao_usdt; do F=/opt/trading/hermes_trading/state/$slug/strategy.yaml; if grep -q min_profit_usd $F; then echo "[$slug] already has min_profit_usd"; else echo "min_profit_usd: 5.0" >> $F && echo "[$slug] appended"; fi; done; for slug in btc_usdt eth_usdt sol_usdt tao_usdt; do printf "%-10s " $slug; grep min_profit_usd /opt/trading/hermes_trading/state/$slug/strategy.yaml; done'
```

### Step 4: run the backfill (dry-run first)

```bash
ssh root@187.127.108.173 'cd /opt/trading/hermes_trading && source .venv/bin/activate && python3 -m tools.backfill_trades --state-root /opt/trading/hermes_trading/state --dry-run'
```

If the dry-run summary looks sane (TAO `added` ≈ 20, BTC/ETH/SOL likely 0 or small), run for real:

```bash
ssh root@187.127.108.173 'cd /opt/trading/hermes_trading && source .venv/bin/activate && python3 -m tools.backfill_trades --state-root /opt/trading/hermes_trading/state'
```

### Step 5: restart agent, tail log

```bash
ssh root@187.127.108.173 'pkill -f hermes_trading.run; sleep 2; cd /opt/trading/hermes_trading && source .venv/bin/activate && nohup python3 -m hermes_trading.run >> bot.log 2>&1 & sleep 3; ps aux | grep hermes_trading.run | grep -v grep; echo "--- last 30 log lines ---"; tail -30 bot.log'
```

## Post-deploy verification (Linh)

1. **All 4 workers booted in live mode.** `tail -f bot.log` should show 4 `[BTC/USDT|ETH/USDT|SOL/USDT|TAO/USDT]` worker lines.
2. **`trades.jsonl` for TAO has the backfilled rows.** `ssh root@... 'wc -l /opt/trading/hermes_trading/state/tao_usdt/trades.jsonl'` should jump from 1 (current) to ~20.
3. **No more `IndentationError` traceback** from reflection subprocess in bot.log.
4. **On next 15m tick + closed trade,** reflection should fire for TAO (it now has > 5 closed trades). Watch for `Hermes: vNN -> v(NN+1)` line in the log.
5. **If a new live trade fires,** verify it cleared all four guards: `max_sl_pct`, `min_tp_pct`, `min_rr_ratio`, `min_profit_usd`. The trade record will not include the $-guard reason field (the guard only logs on skip), but you'll see "Entry skipped — Expected TP profit too small: $X.XX < min $5.00" in the log when the floor blocks.

## Watchlist (do not fix this session)

- **Phase 2.5 `fetch_last_closed_pnl` pnl_pct bug** (line 350): still uses `(exit - entry) / entry` which is wrong for shorts. Backfill sidesteps this by using `closedPnl / cumEntryValue` directly. Fix in next session.
- **Duplicate trade-record bug** (session 5d): noted, still deferred.
- **Local trades.jsonl has 1 stale open TAO Trade #1** from session 5: leave it — the bot's `_reconcile_open_trades` will resolve once the real position closes on Bybit.
- **TAO Trade #1 (R:R 1.73, entry $282) would NOT fire under current guards.** Confirmed in unit test B. If similar setups disappear from the log after deploy, that's the guard working, not a bug.

## Effect prediction

The $5 floor is the most restrictive new guard only when balance is small (< ~$50 USDT) or when `MAX_POSITION_USD` clips the position. In normal operation with balance ≥ $100 and the existing 3% TP / R:R 2.0 already in place, expected USDT gain is well above $5 — the new guard mostly acts as a safety net for edge cases. Trade frequency should not drop noticeably from current Phase 2.1 levels.

Backfill effect is more visible: TAO reflection will start firing immediately on the next post-trade tick because `_count_closed_trades` will return ~20 instead of 0. Watch for the AI to propose its first hypothesis change to TAO's `strategy.yaml`.

## Handoff

- **Status:** all code + tests done locally. Awaiting Linh's PowerShell push + SSH deploy.
- **Receives:** Linh.
- **Read first:** this file (`session-6-phase-2.4.md`), then `memory.md` Session Log → Session 6 entry.
- **Decision needed before deploy:** none — Linh already locked the $5 floor (hard guard) and confirmed PowerShell+SSH deploy pattern.
- **Next-session candidate:** Phase 2.5 (direction-aware `fetch_last_closed_pnl`) — pair with Phase 2.3 (`fetch_recent_closed_trades` for dashboard merge) since both touch the same function. Phase 2.2 (ATR trailing stop) waits for 5–10 real closed trades to calibrate.
