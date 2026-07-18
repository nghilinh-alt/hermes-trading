# Hermes-Trading — prompt for next session

Copy the block below into a fresh chat. Self-contained, but the agent should immediately read `memory.md` for full context.

---

You are continuing work on Hermes-Trading, a self-improving crypto trading bot for Rogue Night (sole director Linh). The bot runs LIVE on Bybit via ccxt, trades BTC/ETH/SOL/XRP USDT-perp on a 15m timeframe (TAO is currently disabled — `trading_enabled: false`) with multi-indicator confidence scoring and a Hermes-driven (Ollama qwen2.5:3b) reflection loop.

## First thing to do this session

1. Read `C:\Users\nghil\Projects\Hermes\Hermes-Trading\memory.md` in full — Last Updated, Key Decisions table, Handoffs section. Session 14 (2026-07-08) is the most recent and has the live open item.
2. Read `C:\Users\nghil\Projects\Hermes\Hermes-Trading\diagnosis-session-14-2026-07-08.md` — full trade-frequency root-cause analysis.

Per CLAUDE.md: read team memory at session start, update it before ending.

## Where things stand (as of session 14, 2026-07-08)

**Trade frequency root cause found, decision pending with Linh — nothing deployed yet:**

- Session 12 (07-03) fixed a hard AND-gate in the trend filter (daily EMA20 vs 4h EMA50) that was blocking BTC/XRP on nearly every tick. Confirmed via a restart-scoped live diagnostic (process running continuously since the 07-03 11:35 deploy, no crashes since) that this fix **works exactly as intended** — 4h-disagreement and ambiguous-band skips are both negligible (6 and 59 respectively over 4d20h).
- The actual dominant blocker, previously misdiagnosed as SOL-specific, is **`min_tp_pct: 3.0%`** in `execution.py::_structural_sl_tp()` — the structural-TP guard that only runs after a signal clears every upstream gate. It rejected 3255 attempts across BTC(982)/ETH(1057)/SOL(673)/TAO(332)/XRP(211) over the same window — more rejections than the session-hours time block itself (2240).
- Reflection-driven gate drift was ruled out: `hypotheses.jsonl` is empty for all 5 assets on the live VPS — no asset has reached the 5-closed-trade `reflection_every` threshold, so Hermes hasn't mutated anything since the deploy.
- **Open decision for Linh**: lower `min_tp_pct` (frequency should recover sharply — this is the whole gap) or leave it as a deliberate 3% quality floor. **Ask Linh first thing this session whether a decision has been made.** If yes, implement one variable at a time per the project's standing discipline, write a `deploy-*.md` doc before touching the VPS, and don't bundle it with anything else.
- Separately, unrelated to trade frequency: the position-sizing/leverage fix from session 12 (`d8a49c7`, `75ef76b` — `position_notional` now actually multiplies margin × leverage) is implemented locally but **still not deployed**. It was deliberately held back to isolate the trend-filter fix's effect; that isolation period has now run its course, so it may be ready to deploy once Linh has separately weighed in — confirm with Linh, don't assume.

## Known standing issues (not yet fixed, don't need re-diagnosis)

- **BTC/ETH/SOL `trades.jsonl` were 0 lines as of the 07-08 diagnostic** — flagged since session 11, still unresolved. Low trade count so far is largely explained by `min_tp_pct`, but worth re-checking after any frequency fix to confirm the files are actually being written to, not silently broken.
- **TAO disabled** (`trading_enabled: false`) but still shadow-evaluates entries for logging/paper purposes — confirm with Linh whether TAO should stay off or be re-enabled once XRP/other assets are healthier.

## Hard-won operational lessons — do not relearn

1. **This sandbox has no VPS network access** (`/dev/tcp/187.127.108.173/22` unreachable — confirmed every session since at least session 12). All VPS diagnostics require Linh to run an SSH block from her own PowerShell and paste back output. Don't attempt to work around this via any other network path.

2. **`bot.log` is append-mode across restarts** — a full-log `grep -c` can silently include history from before the change you're trying to measure. Always scope counts to the current process run: `ps -p $(pgrep -f hermes_trading.run) -o lstart,etime` to get the current process's start time, then find the matching `nohup: ignoring input` line in `bot.log` (each restart writes one) and `tail -n +N` from there before counting anything.

3. **Some skip-reason strings live in `execution.py` (raised as `ValueError`, caught and logged by `loop.py`'s generic "Entry skipped — {e}" wrapper), not in the per-tick entry-evaluation logging in `loop.py`.** `min_tp_pct`, `max_sl_pct`, and `min_profit_usd` failures all come through this path. Grep the specific exception text (e.g. `"Structural TP too thin"`), not just the generic wrapper string — the wrapper uses an em-dash (`—`) that doesn't always survive the SSH/PowerShell round-trip cleanly and can silently under-count.

4. **The `FIRE`/`NO-FIRE` string only exists in `evaluation_summary`, a field written into trade records** — it is never printed to `bot.log`. Don't grep the log for it as a proxy for "did a signal fire."

5. **PowerShell quote tax via SSH**: embedded `"..."` in single-quoted PowerShell strings get stripped unpredictably, especially around `(`, `)`, `|`, nested quotes, and unquoted `echo` arguments containing parentheses will break `bash` with a syntax error. Workarounds:
   - Quote every `echo` argument that contains punctuation: `echo "--- like this ---"`, never `echo --- like this ---`.
   - Here-string piped to ssh: `@'...'@ | ssh root@host bash`
   - Doubled single quotes inside a PowerShell single-quoted string collapse to one literal `'`.
   - Never trust a quoting pattern that worked once; it may break on the next invocation.

6. **VPS layout is two clones**:
   - `/opt/trading/hermes-trading/` (HYPHEN) — git pull target only, staging.
   - `/opt/trading/hermes_trading/` (UNDERSCORE) — running agent. Has `.venv/`, top-level `bot.log`, `state/<slug>/`.
   - Deploy pattern: `git pull` in hyphen clone (or `scp` directly), then `cp`/`scp` files into the underscore clone.
   - **Never blind-`cp` strategy.yamls** between them — they may be reflection-evolved. Diff first.

7. **One deploy step per PowerShell block.** Never combine diff+cp or pull+cp+restart in the same block — Linh may only run part of it, or a mid-block failure leaves things in a partial state that's hard to diagnose after the fact.

8. **Daemonizing over non-interactive SSH**: use `setsid .venv/bin/python -m hermes_trading.run >> bot.log 2>&1 &`, not `nohup ... & disown` (disown doesn't work in non-interactive one-liners) — confirmed working in the session 12 deploy after an earlier restart failure.

9. **Git locks on the Windows mount** (`.git/index.lock`): can't be deleted, but can be moved — `Remove-Item .git\index.lock -Force -ErrorAction SilentlyContinue` from PowerShell before `git add`.

## Style + protocol reminders

- All outputs saved as `.md` to the project folder.
- Flag client-facing deliverables for Linh's review before sending. (This project has no clients yet — everything so far is internal, but the rule stands.)
- Update `memory.md` before ending each session: Last Updated, Key Decisions, Handoffs.
- No VPS deploy or gate change without Linh's explicit sign-off — this project has been burned before by bundling multiple changes into one deploy and losing the ability to attribute effects.
- Change one variable at a time, measure over a fixed window (3–5 days minimum) before stacking the next change.

## Start of session

Read `memory.md`, then ask Linh: has a decision been made on `min_tp_pct`? If yes, implement + write a `deploy-*.md` doc, one variable at a time. If not, don't propose new changes unprompted — the diagnosis is done, the ball is in Linh's court.
