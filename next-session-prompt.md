# Hermes-Trading — prompt for next session

Copy the block below into a fresh chat. Self-contained, but the agent should immediately read `memory.md` for full context.

---

You are continuing work on Hermes-Trading, a self-improving crypto trading bot for Rogue Night (sole director Linh). The bot runs LIVE on Bybit via ccxt, trades BTC/ETH/SOL/TAO USDT-perp on a 15m timeframe with multi-indicator confidence scoring and a Hermes-driven (Ollama qwen2.5:3b) reflection loop.

## First thing to do this session

Read these files in order. Do not skip:

1. `C:\Users\nghil\Projects\Hermes\Hermes-Trading\memory.md` — full project history, key decisions, VPS layout, known hazards, **and the 12 doctrine items at the end of the session 6 wave-4 block**.
2. `C:\Users\nghil\Projects\Hermes\Hermes-Trading\archive\session-6-phase-2.4.md` — Phase 2.4 deploy details if needed.
3. `C:\Users\nghil\Projects\Hermes\Hermes-Trading\session-7-restart.md` — session-7 emergency restart diagnostic (2026-06-01).

Per CLAUDE.md: at start of every session read team memory; at end of every session update it.

## Current live state (as of 2026-06-01 end of session 7)

- **Bot status**: running on VPS as PID 1525537 (or whatever rotated to since). All 4 workers boot in live mode. Four structural guards active: `max_sl_pct: 5.0`, `min_tp_pct: 3.0`, `min_rr_ratio: 2.0`, **`min_profit_usd: 5.0`** added in session 6, plus `sl_buffer_pct: 0.3`. **Session 7 recovered from a 3-day downtime caused by a partial wave-4 deploy** (loop.py + execution.py copied; reflect.py not initially — bot crashed on next Hermes reflection, was pkilled, never restarted). See `session-7-restart.md` and memory.md session 7 entry for the full diagnosis.
- **Master at commit `004d742` or later** on GitHub. Local + VPS in sync.
- **Dashboard works** at `localhost:8888` with $ figures alongside %, real P&L visible. Restart locally if not already running: `cd C:\Users\nghil\Projects\Hermes\Hermes-Trading; python dashboard.py`.
- **Bot is +$44 net** across 45 closed trades (per Bybit CSV import done in session 6). ~50% win rate.
- **TAO strategy** has Hermes mutation applied: `indicators[volume_spike].params.min_ratio = 2.0` (was 1.5). Reasoning preserved in `state/tao_usdt/hypotheses.jsonl` with full `decision_context`, `trade_range`, `llm_raw_output`, `applied_successfully: true`.
- **Reflection works end-to-end**: drawdown formula fixed (was producing 80%+ false drawdowns), pnl_pct direction-aware (was sign-flipping every short), Ollama at 8K context with 300s timeout, _set_nested has clobber guards.

## What to consider for this session — Linh's call

Open backlog (in rough priority order):

- **Stability watch (24 h)** — bot was just restarted 2026-06-01 10:21 UTC. Confirm heartbeats refresh on every 15 m tick, no fresh tracebacks in `bot.log`, no rapid abandons. Pause Phase 2.2 until stable.
- **Backfill intermediate downtime trades** — `tools/backfill_trades.py` to pull the 7-day Bybit closed-pnl window and recover any positions that closed during 2026-05-29 06:30 → 2026-06-01 10:21 (the reconcile only captured the most recent close per asset). One asset at a time, `--dry-run` first.
- **Phase 2.8 (raised priority post-s7)** — TAO closed by SL_hit on its first trade under the v03 Hermes mutation. One sample is irrelevant, but the LLM's "80% of losers fired volume_spike" claim was already suspect (decision_context showed wins had HIGHER volume). Worth adding the sanity-check before the next 5-trade boundary triggers another Hermes call.
- **Phase 2.7 — Confirm auto-reflection cadence is firing.** TAO will cross the next 5-trade boundary in the ~24h after session 6 end; verify Hermes is invoked automatically (not just manually) and produces sensible mutations. Check `state/<slug>/hypotheses.jsonl` tail counts grow.
- **Phase 2.8 — Volume_spike Hermes claim discrepancy.** The successful session-6 Hermes mutation reasoned "Volume spike fired on 80% of losing trades at ratio 1.5", but `decision_context` showed avg volume_ratio of 1.051 on wins vs 0.788 on losses — wins actually had HIGHER volume. LLM may have hallucinated the 80% figure. Options: (a) add a sanity-check to reflect.py that ground-truths the LLM's claim against decision_context before applying, (b) investigate whether qwen2.5:3b is too small for reliable causal reasoning, (c) try `qwen3:4b` as a swap.
- **Phase 2.9 — `$-18,272 cumRealisedPnl` audit.** Bybit account shows historical accumulated loss. Most predates session 4 (Phase 1 SMC). Worth a one-session audit: pull full closed-PnL history via paginated 7-day chunks, bucket by date + strategy version, identify the bleed.
- **Phase 2.2 — ATR-based trailing stop.** ~80 lines. Now that we have 30+ clean closed trades on TAO, calibration window is reasonable. New `update_position_sl` + `fetch_open_positions_with_marks`.
- **Phase 2.10 — strategy.yaml version field cleanup.** Currently every reflection archive uses default "00" name (overwrites `v0000.yaml`) because the version field is missing or oddly placed. Fix: ensure each strategy.yaml has a top-level `version: 'NN'`. Cosmetic but enables proper version-archive history.
- **Phase 2.3 — fetch_recent_closed_trades + dashboard merge.** CSV import already gave us the missing 15 trades, but for ongoing freshness the dashboard should merge live Bybit closed-PnL with local trades.jsonl. ~40 lines.

Natural sequence: 2.7 (passive monitor) → 2.8 (Hermes reasoning quality) → 2.10 (small cleanup) → 2.2 (real feature) → 2.9 (audit). Ask Linh which to start with.

## Hard-won lessons — do not relearn

1. **Edit and Write tools both corrupt files on the Windows mount** (`C:\Users\nghil\...`). For any `.py` file change: use bash + Python heredoc (`python3 <<'PYEOF' ... PYEOF`) to write atomically, then `python -m py_compile` to validate, then `ast.parse` to double-check. Never trust the file tool's "success" response for Python on this mount.

2. **PowerShell quote tax via SSH**: embedded `"..."` in single-quoted PowerShell strings get stripped unpredictably, especially around `(`, `)`, `|`, nested quotes. Workarounds:
   - Doubled single quotes: `'sed -i ''s/x/y/'' file'` — `''` collapses to one `'` in the literal
   - Here-string piped to ssh: `@'...'@ | ssh root@host`
   - SCP a `.sh` file then execute remotely
   Never trust a quoting pattern that worked once; it may break on the next invocation.

3. **VPS layout is two clones**:
   - `/opt/trading/hermes-trading/` (HYPHEN) — git pull target only
   - `/opt/trading/hermes_trading/` (UNDERSCORE) — running agent. Has `.venv/`, `bot.log`, `state/<slug>/`.
   - Deploy pattern: `git pull` in hyphen clone, then `cp` files into underscore clone.
   - **Never `cp` strategy.yamls** between them — they may be reflection-evolved. Use in-place edits.

4. **Git locks on Windows mount** (`.git/index.lock`, `.git/HEAD.lock`) cannot be deleted but CAN be moved with `Move-Item .git\index.lock ".git\index.lock.$(Get-Date -UFormat %s)" -Force` from PowerShell.

5. **SSH from local Windows**: key is `~/.ssh/hermes_vps`, configured via `~/.ssh/config` Host block for `187.127.108.173`. Use PowerShell's built-in `ssh`, NOT PuTTY (PuTTY can't read OpenSSH format keys and doesn't read `~/.ssh/config`).

6. **Always verify after a `cp` rollback** with `grep` for an EXPECTED value. `cp` silently succeeds even if source is missing. The session-6 rollback of `v0001.yaml` (didn't exist) appeared to succeed for two hours before we caught it.

7. **`rich` library eats bracket-pair strings** in console.print. `[volume_spike]` gets interpreted as a markup tag and stripped from display. Escape with `changed_var.replace("[", "\\[")` before printing. Truth source for changed_variable is `hypotheses.jsonl`, not stdout.

8. **`_max_drawdown` formula** now uses compound wealth-curve `(peak - wealth) / peak`, bounded [0,1]. Old `(peak - c) / (abs(peak) + 1e-9)` was broken — amplified small dips into 80%+ false drawdowns near zero crossings.

9. **`fetch_last_closed_pnl` uses `closedPnl / cumEntryValue`** (direction-correct). The old `(exit - entry) / entry` formula sign-flipped every short.

10. **`_set_nested` clobber guards** reject dot-notation indicator paths (`indicators.params.X` without `[name]`) and refuse list→dict overwrites. Defense against LLM forgetting bracket notation.

11. **Ollama 8K context** via `options.num_ctx: 8192` in payload. First call after num_ctx change takes ~40s to reload model. Use `HERMES_LLM_TIMEOUT=300` in `.env`. Pre-warm with one trivial curl before reflection if doing the first call manually.

12. **`.env` not auto-loaded by python scripts**. For any `tools/` or `reflect` invocation: `set -a && source .env && set +a` before running.

13. **One deploy step per code block.** Never combine `diff + cp` or `pull + cp` in the same block — Linh may run only one part. Session 6 hit this twice (ModuleNotFoundError downstream).

## Open issues for the watchlist (don't fix yet)

- **Volume_spike Hermes reasoning may be hallucinated** (Phase 2.8 above).
- **strategy.yaml version field placement** causes archive name collisions (Phase 2.10 above).
- **Cumulative -$18k historical loss** on Bybit account (Phase 2.9 above).
- **`fetch_recent_closed_trades`** for dashboard live-merge not yet built (CSV import handles batch case).
- **`snapshot.sh`** still doesn't push to GitHub (SSH key on VPS not set up; trades.jsonl etc. stay VPS-only).
- **Duplicate trade-record bug** noted in session 5d still deferred.

## Tools available (in `tools/`)

- `backfill_trades.py` — Bybit closed-pnl API (7-day window limit per call)
- `import_bybit_csv.py` — Bybit UI CSV export → trades.jsonl, dedup by (asset, exit, qty)
- `recompute_pnl_pct.py` — direction-correct pnl_pct from entry+exit+direction
- `purge_abandoned.py` — removes `entry==exit AND abandoned=true` stubs with `.jsonl.bak-<unix>` backup
- `dashboard.py` — local Windows dashboard with $ + % columns

All tools support `--dry-run` and `--state-root`. All do atomic writes via tmp + replace.

## Style + protocol reminders

- All outputs saved as `.md` to the project folder.
- Flag client-facing deliverables for Linh's review.
- Update `memory.md` before ending each session: Last Updated date, Active state, Key Decisions, Handoffs, Session Log entry.
- Push from PowerShell (sandbox has no GitHub credentials). Deploy to VPS from Linh's PowerShell + SSH.
- One deploy step per code block.
- Always `diff $SOURCE/state/<slug>/strategy.yaml $RUNNING/...` before any `cp` to avoid blowing away Hermes-evolved strategy values.
- Backup before destructive ops: tools that mutate state files should write `.bak-<unix>` first.

## Start of session

Read `memory.md` first. Then ask Linh which Phase 2 sub-task to start with, or whether they have new questions/issues. Don't begin coding until you've confirmed scope. **Especially: ask whether the auto-reflection cadence has fired since session 6 (TAO should have crossed its next 5-trade boundary by ~24h after session end). If yes, inspect the most recent hypothesis to verify Hermes is still producing data-grounded mutations.**
