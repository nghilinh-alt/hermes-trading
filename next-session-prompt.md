# Hermes-Trading — prompt for next session

Copy the block below into a fresh chat. Self-contained, but the agent should immediately read `memory.md` for full context.

---

You are continuing work on Hermes-Trading for Rogue Night (sole director Linh). **As of session 18 (2026-07-18), the old indicator-weight live agent is HALTED and archived.** The project pivoted to building a new **ICT (Inner Circle Trader) swing strategy** — Phase 1 (mechanical detection primitives + tests) is built and shipped; nothing beyond that is built yet.

## First thing to do this session

1. Read `C:\Users\nghil\Projects\Hermes\Hermes-Trading\memory.md` in full — Last Updated (session 18), Handoffs section (session 18 entry).
2. Read `C:\Users\nghil\Projects\Hermes\Hermes-Trading\ict-strategy-plan-2026-07-18.md` — the mechanical spec, all sections, especially S:11 (backtester design) if Phase 2 is the next task.
3. Read `C:\Users\nghil\Projects\Hermes\Hermes-Trading\ict-claude-code-prompt.md` — the Phase 1 build prompt (context on what's already done and why).

Per CLAUDE.md: read team memory at session start, update it before ending.

## Where things stand (as of session 18, 2026-07-18)

- **Old live agent**: halted. Pre-halt check confirmed zero open positions on Bybit for all 5 assets, then `pkill -f hermes_trading.run` on the VPS. Fully archived at `archive/live-paper-archived-2026-07-18/` (full `state/` trees, `state-scalp/`, legacy root `strategy.yaml`). Rollback command is in that archive's README if ever needed — VPS `.venv`/code/state were left in place, only the process was killed.
- **ICT Phase 1**: `hermes_trading/ict/` (types, structure, liquidity, imbalance, bias) + `hermes_trading/brokers/base.py` (interface only), built exactly to the Phase 1 scope in `ict-claude-code-prompt.md`. 66 tests, 96% coverage, all green. Committed (`61d3af6` archive, `1b6efaa` ICT) and pushed to GitHub + the VPS staging clone.
- **Not started**: Phase 2 (event-driven backtester, spec S:11), Phase 3 (scanner), Phase 4+ (paper/live worker, futures adapter). The build prompt explicitly gates Phase 2 behind Linh's sign-off on Phase 1 — **do not start the backtester unless Linh has reviewed/approved Phase 1 first.**
- **Open item**: `tests/ict/fixtures/README.md` flags that Phase 1's fixtures are synthetic (hand-built + a seeded stress test), not real exchange data, because this session's Python env had a broken project `.venv` and no `ccxt` in the system Python. Worth a real-data validation pass before backtesting if a working venv becomes available.

## Hard-won operational lessons — do not relearn

1. **VPS network access status is UNCERTAIN — re-verify each session, don't assume.** Sessions 12-17 all noted "no VPS network access from this sandbox." Session 18 found direct SSH worked fine (`ssh 187.127.108.173` using `~/.ssh/hermes_vps`, configured in `~/.ssh/config`). Test with a harmless read-only command (`ssh 187.127.108.173 whoami`) before either assuming you're blocked (and asking Linh to run SSH blocks manually) or assuming you have access.

2. **The local project `.venv` is broken in this sandbox**: `.venv/pyvenv.cfg` has `home = /usr/bin` and `.venv/bin/python` is a dangling symlink (works in a different environment, not here). Use the system Python (`python`, `python -m pytest` — both on PATH; `pytest` is installed globally even though only recently added to `pyproject.toml`'s `dev` extra; `ccxt` is NOT in the system Python). Check `python -c "import hermes_trading"` works before assuming an import path problem is a real bug.

3. **`bot.log` is append-mode across restarts** — a full-log `grep -c` can silently include history from before the change you're trying to measure. Always scope counts to the current process run: `ps -p $(pgrep -f hermes_trading.run) -o lstart,etime` to get the current process's start time, then find the matching `nohup: ignoring input` line in `bot.log` (each restart writes one) and `tail -n +N` from there before counting anything. (Only relevant if the old agent is ever restarted — it's halted as of session 18.)

4. **PowerShell quote tax via SSH** (if working from PowerShell rather than this sandbox's bash): embedded `"..."` in single-quoted PowerShell strings get stripped unpredictably, especially around `(`, `)`, `|`. Quote every `echo` argument that contains punctuation; prefer a here-string piped to ssh (`@'...'@ | ssh root@host bash`) for anything multi-line.

5. **VPS layout is two clones**:
   - `/opt/trading/hermes-trading/` (HYPHEN) — git pull / staging target. Now also holds the ICT Phase 1 code (synced via scp session 18, not yet `git pull`-ed there — do that before making further edits in that clone).
   - `/opt/trading/hermes_trading/` (UNDERSCORE) — old agent's runtime dir. Has `.venv/`, top-level `bot.log`, `state/<slug>/`. Worker process is halted; files untouched.
   - **Never blind-`cp` strategy.yamls** between them if the old system is ever revived — they may be reflection-evolved. Diff first.

6. **Git locks on the Windows mount** (`.git/index.lock`, `.git/HEAD.lock`): both showed up stale (3+ days old, no active git process) this session and were safely removed before retrying. If you hit `fatal: Unable to create '.../.git/*.lock': File exists`, check `ps aux | grep git` first (should be empty) and the lock file's mtime before deleting it.

7. **`pkill -f <pattern>` self-match gotcha**: if your SSH command's own text contains the same pattern you're killing (e.g. `ssh host "pkill -f hermes_trading.run; pgrep -af hermes_trading.run"` — the command line itself contains "hermes_trading.run"), `pkill` may kill the wrapping shell too, dropping the SSH connection with a bare exit code before any output prints. Don't read that as failure — re-check with a plain `pgrep -af <target>` in a fresh, minimal SSH call.

## Style + protocol reminders

- All outputs saved as `.md` to the project folder.
- Flag client-facing deliverables for Linh's review before sending. (This project has no clients yet — everything so far is internal, but the rule stands.)
- Update `memory.md` before ending each session: Last Updated, Key Decisions, Handoffs.
- No VPS deploy or gate change without Linh's explicit sign-off — this project has been burned before by bundling multiple changes into one deploy and losing the ability to attribute effects. (Applies to the old system; the ICT system has no live/VPS execution path yet at all.)
- Change one variable at a time, measure over a fixed window before stacking the next change.

## Start of session

Read `memory.md`, then ask Linh: has Phase 1 (ICT primitives + tests) been reviewed? If approved, move to Phase 2 (backtester, spec S:11) — read that section closely before writing any code, since it introduces new constraints (multi-TF alignment, fills/costs, walk-forward validation). If not yet reviewed, don't start Phase 2 unprompted.
