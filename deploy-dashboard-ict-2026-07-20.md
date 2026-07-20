# Deploy runbook — ICT dashboard + context dump + partial-P&L fix

**Date:** 2026-07-20 (session 21b)
**Status:** NOT YET RUN — awaiting Linh's go-ahead.
**Why a runbook rather than an agent-executed deploy:** SSH to `187.127.108.173` was
`Network is unreachable` from the sandbox for this entire session. Per the standing rule,
reachability is re-verified per session and never assumed — this session it was not
available, so these are commands for Linh to run.

**Real money is on this account** (~$808 as of session 20). Every step below is ordered so
that nothing touching the live worker happens before the safety checks pass.

---

## What is being deployed

| Component | Change | Requires worker restart? |
|---|---|---|
| `hermes_trading/ict/live.py` | Partial-P&L fix + R fields on closed trades | **Yes** |
| `hermes_trading/ict/context.py` | New — market-context builder | **Yes** |
| `tools/run_ict_live.py` | Context dump + extended heartbeat | **Yes** |
| `dashboard.py` | Rewritten — runs on Linh's machine only | No |

Behavioural change to trading logic: **none**. The partial-P&L fix changes what gets
*recorded* at close, not what gets traded. The context dump runs after the trading cycle and
is try/except-isolated per asset.

---

## Step 0 — Local: clear the git lock, run the full test suite

A stale `.git/index.lock` was created during the session and could not be removed from the
sandbox (`Operation not permitted` on the Windows mount). In PowerShell:

```powershell
cd C:\Users\nghil\Projects\Hermes\Hermes-Trading
Get-Process git -ErrorAction SilentlyContinue          # expect nothing
Remove-Item .git\index.lock -Force -ErrorAction SilentlyContinue
git stash list                                          # expect one leftover entry
git stash drop                                          # its contents are already in the working tree
```

Then run the tests that the sandbox's 45-second command ceiling prevented:

```powershell
python -m pytest tests/ict/ -q
```

**Expect all green.** The sandbox verified 114 + 23 + 15 tests; the ones it could not
finish are `test_scanner.py` (needs the 2-year BTC fixture) and the real-CSV half of
`test_live.py`. **Do not proceed if anything fails here.**

---

## Step 1 — Commit and push

> **Run git from Windows, never from the Cowork sandbox.** A `git add` attempted from the
> sandbox left 73 stray `.git/objects/tmp_obj_*` files it could not unlink
> (`Operation not permitted` on the mount) and staged nothing. No corruption — `git fsck`
> is clean and HEAD is untouched — but the writes don't complete. Clear the clutter with
> `git gc --prune=now` at any convenient point.
>
> Note also that `.git/index.lock` and the stash entry the sandbox reported as stuck did
> **not** exist on Windows at all — they were artefacts of the sandbox's view of the mount.

Commit with **explicit paths**. Five files (`hermes_trading/ict/backtest.py`,
`snapshot.sh`, `tests/ict/test_backtest.py`, `tools/dedup_trades.py`, and an archived
`goal.yaml`) show as modified but contain zero content change — pure CRLF churn from the
Windows mount, confirmed via `git diff --ignore-cr-at-eol`. Explicit paths keep them out.

```powershell
cd C:\Users\nghil\Projects\Hermes\Hermes-Trading
git add hermes_trading/ict/context.py hermes_trading/ict/live.py tools/run_ict_live.py `
        dashboard.py archive/dashboard-indicator-weight-2026-07-20.py `
        tests/ict/test_context.py tests/ict/test_live.py `
        tests/ict/fixtures/context_btc_near_miss.json `
        dashboard-ict-plan-2026-07-20.md deploy-dashboard-ict-2026-07-20.md memory.md
git status --short          # confirm ONLY the above are staged
git commit -m "feat: ICT dashboard rebuild + market-context dump; fix partial-take P&L accounting"
git push origin master
```

---

## Step 2 — VPS: pre-flight safety checks (READ ONLY)

Nothing here changes anything. Run it and read the output before continuing.

```bash
ssh root@187.127.108.173
cd /opt/trading/hermes-trading

# (a) Is the worker alive, and how long has it been running?
#     NOTE the exact command line printed here — reuse it verbatim in Step 4
#     rather than trusting this document's guess at the invocation form.
pgrep -af run_ict_live
ps -p "$(pgrep -f run_ict_live | head -1)" -o lstart,etime 2>/dev/null
tail -20 live.log

# (b) Any asset flagged for manual review? MUST be empty.
ls -la state-ict-live/*/NEEDS_MANUAL_REVIEW.flag 2>/dev/null || echo "no review flags — good"

# (c) What is each asset's local state? MUST all be "flat" before restarting.
for d in state-ict-live/*/; do echo -n "$d: "; cat "$d/position.json" 2>/dev/null | head -3 | tr -d '\n'; echo; done
```

**STOP if any asset shows `open_position` or `resting_order`.** A restart is safe by design
(state is persisted after every mutating action) but there is no reason to prove that with
real money on the line for a dashboard feature. Wait for the position to close, or ask
before proceeding.

Then confirm the broker agrees there is nothing open:

```bash
cd /opt/trading/hermes-trading
.venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv()
from hermes_trading.brokers.bybit import BybitBroker
b = BybitBroker()
print('balance:', b.get_balance())
for a in ['BTC/USDT','ETH/USDT','SOL/USDT','TAO/USDT']:
    print(a, 'open position:', b.has_open_position(a), '| open orders:', len(b.get_open_orders(a)))
"
```

**All four must report `False` and `0`.**

---

## Step 3 — VPS: update the code

`git fetch` FIRST — this clone's `git status` has lied before when its cached ref was
stale (session 19-20: it reported "up to date" while 18 commits behind).

```bash
cd /opt/trading/hermes-trading
git fetch origin
git status                      # now trustworthy
git log --oneline -1            # note the current HEAD in case of rollback
git merge --ff-only origin/master
```

If the fast-forward is refused, **stop** — that means the VPS clone has local commits.
Diff before doing anything else.

Run the tests on the VPS itself (it has ccxt and the full venv):

```bash
.venv/bin/python -m pytest tests/ict/ -q
```

Note: the scanner parity tests skip on the VPS — they need the gitignored BTC CSV, which
isn't there. That's expected and pre-existing.

---

## Step 4 — VPS: restart the worker

```bash
cd /opt/trading/hermes-trading
pkill -f run_ict_live
sleep 3
pgrep -af run_ict_live || echo "stopped — good"
```

Note the `pkill` self-match gotcha from memory.md: don't put the kill and the re-check in
one SSH command whose own text contains the pattern. The two-command form above is fine
from an interactive session.

Restart using **the exact command line captured in Step 2(a)**. It will be one of the two
forms below — both work (`tools/__init__.py` exists, so the `-m` form resolves), but use
whichever was actually running so nothing else changes in this deploy:

```bash
cd /opt/trading/hermes-trading
set -a; source .env; set +a          # run_ict_live also calls load_dotenv(), but the
                                     # safety gate is read at import — keep this
setsid nohup .venv/bin/python -m tools.run_ict_live >> live.log 2>&1 &
#   ...or, if that's what Step 2(a) showed:
# setsid nohup .venv/bin/python tools/run_ict_live.py >> live.log 2>&1 &
sleep 5
pgrep -af run_ict_live
```

The worker hard-fails at startup unless `HERMES_TRADING_MODE=live` and
`HERMES_TRADING_I_ACCEPT_RISK=true` are both set — if it exits immediately, check `live.log`
for the `FATAL: refusing to start` line before assuming anything else broke.

---

## Step 5 — VPS: verify one clean cycle

A cycle takes 90-150 seconds. Wait, then check:

```bash
cd /opt/trading/hermes-trading
tail -30 live.log

# The new artefacts — four context files, one per asset:
ls -la state-ict-live/*/context.json

# Spot-check one is real (not an error stub):
python3 -c "
import json; c=json.load(open('state-ict-live/BTC_USDT/context.json'))
print('bias:', (c.get('bias') or {}).get('direction'))
print('price:', c.get('price'), 'atr:', c.get('atr'))
print('zones:', {k: len(v) for k, v in c['zones'].items()})
print('candidates:', len(c['candidates']), 'gate_summary:', c['gate_summary'])
"

# Heartbeat should now carry equity / circuit breaker / scan_params:
python3 -c "
import json; h=json.load(open('state-ict-live/heartbeat.json')); print(sorted(h))
print('equity:', h.get('equity_usd'), 'cycle_s:', h.get('cycle_seconds'))
"
```

**What to check:**

- Four `context.json` files exist and are a few KB each
- `heartbeat.json` has `equity_usd`, `circuit_breaker`, `scan_params`, `cycle_seconds`
- `cycle_seconds` is still comfortably under 900. It will rise — a flat asset now builds
  the detection context twice per cycle (once for trading, once for display). Expected
  cost is roughly +3 s per asset. **If it approaches 900 s, that's the thing to fix**
  (hoist the detection context and pass it into both) — tell the next agent.
- No `[WARN] market-context dump failed` lines. If present, trading is unaffected by
  design, but the dashboard will be blind for that asset — investigate.
- No `NEEDS_MANUAL_REVIEW.flag` appeared.

---

## Step 6 — Local: run the dashboard

```powershell
cd C:\Users\nghil\Projects\Hermes\Hermes-Trading
python dashboard.py
```

Opens `http://localhost:8888`. Confirm:

- Header shows real equity, `0/1` positions, circuit breaker "ok"
- Each of the four assets renders a card — most will say WATCHING with a bias, a price
  ladder, and either a proposed scenario or "no candidate setup in the last 61 bars"
- Click **🔔 alerts** once to grant notification permission. The first grant adopts current
  state silently rather than firing a burst for things that already happened.

---

## Rollback

The context dump is additive and try/except-wrapped; the partial-P&L fix only changes what
is written to `trades.jsonl` at close. There is no state migration and no file the worker
would trip over.

```bash
cd /opt/trading/hermes-trading
git reset --hard <the HEAD noted in Step 3>
pkill -f run_ict_live
set -a; source .env; set +a
setsid nohup .venv/bin/python -m tools.run_ict_live >> live.log 2>&1 &
```

Any `context.json` files left behind are inert — nothing in the trading path reads them.

---

## After deploy

- Watch the first cycle's `cycle_seconds` (Step 5).
- **The partial-P&L fix is verified against a FakeBroker, not against Bybit's real
  multi-record closed-pnl behaviour.** On the first live trade that takes its 2R partial,
  compare the dashboard's `realised R` and `P&L $` against the Bybit UI. If the trade shows
  `pnl_source: local_estimate` (amber "estimate" in the dashboard), the legs did not
  reconcile — capture `state-ict-live/<ASSET>/trades.jsonl` and the Bybit closed-pnl export
  for that trade and hand both to the next session.
- Update `memory.md` with the deploy result.
