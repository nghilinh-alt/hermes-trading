# Hermes-Trading — Project Memory
_Rogue Night consulting project. Updated at end of each session._

## Last Updated
2026-06-01 (session 7 — emergency restart after 3-day downtime; wave-4 reflect.py was on disk but bot had been pkilled; partial-deploy detection lesson)

## Project Overview
Self-improving live crypto trading agent running on VPS (root@187.127.108.173).
- **VPS layout** (confirmed session 5b, 2026-05-27):
  - **Git pull clone**: `/opt/trading/hermes-trading/` (hyphen) — only used as deploy staging
  - **Running agent dir**: `/opt/trading/hermes_trading/` (underscore) — has `.venv/`, `bot.log` at top level, package at `hermes_trading/`, per-asset state at `state/<slug>/`
  - **State is NOT nested** inside the package dir. It's a sibling: `/opt/trading/hermes_trading/state/<slug>/strategy.yaml` (correct), NOT `/opt/trading/hermes_trading/hermes_trading/state/...` (wrong, this path doesn't exist)
  - **Venv**: `.venv/` (dot-prefixed), activate with `source /opt/trading/hermes_trading/.venv/bin/activate`
  - **Log**: top-level `bot.log` (NOT `logs/hermes.log` — that dir doesn't exist)
  - Fossilized old install at `/opt/trading/` top-level (May-24-dated `strategy.yaml`, `dashboard.py`, `state/`) — unused, safe to clean up later
- **Repo**: https://github.com/nghilinh-alt/hermes-trading.git (master branch)
- **Local path**: `C:\Users\nghil\Projects\Hermes\Hermes-Trading`
- **Trading mode**: Live on Bybit, HMAC key auth
- **Assets**: BTC/USDT, ETH/USDT, SOL/USDT, TAO/USDT
- **VPS Python**: uses `venv/` — always `source venv/bin/activate`

## Architecture Summary
- `run.py` — entrypoint, bootstraps per-asset state dirs under `state/{asset_slug}/`
- `loop.py` — async 15m tick loop; fetches 4 adapters, evaluates strategy, trades, triggers reflection every N trades
- `adapters/price.py` — 15m + 1h + 4h OHLCV; computes RSI, EMA, BB, MACD, ATR, VWAP (15m), FVG/OB (1h), S/R (1h+4h)
- `adapters/execution.py` — Bybit HMAC auth, live order placement
- `reflect.py` — `--fallback` (rule-based) or `--hermes` (AI via local Ollama qwen2.5:3b); changes exactly ONE variable per reflection
- `dashboard.py` — local Windows dashboard at localhost:8888, SSH-fetches VPS state

## Active State
- Strategy version: v03 (all 4 assets, post-Ollama first reflection)
- Trading mode: live
- Reflection cadence: every 5 closed trades (--hermes mode for BTC, --fallback for others until trades accumulate)
- Per-asset state dirs: `state/btc_usdt/`, `state/eth_usdt/`, `state/sol_usdt/`, `state/tao_usdt/`
- Each asset dir contains: `strategy.yaml`, `trades.jsonl`, `hypotheses.jsonl`, `heartbeat.json`, `history/`, `memory.md`
- Ollama running on VPS: `qwen2.5:3b` @ `http://localhost:11434` (CPU-only, ~15-20s inference)
- VPS running code: `/opt/trading/hermes_trading/hermes_trading/` (nested — package inside package dir)
- Git repo on VPS: `/opt/trading/hermes-trading/` — pull here, then copy to running location

## Key Decisions
| Date       | Decision | Rationale |
|------------|----------|-----------|
| 2026-05-24 | Added per-asset state dirs | Scale to 4 assets independently |
| 2026-05-24 | Reflection reads/writes per-asset memory.md | Hermes agent learns from its own history per asset |
| 2026-05-24 | reflect.py refactored — all paths via --state-dir arg | Fixes silent reflection failure in loop.py |
| 2026-05-24 | dashboard.py uses yaml.safe_load for YAML parsing | Homebrew flat-key parser was missing nested goal values |
| 2026-05-24 | Chose Ollama + qwen2.5:3b over Anthropic API | No API credits needed; VPS has sufficient RAM (7.8GB); CPU inference ~15-20s is acceptable for 5-trade cadence |
| 2026-05-24 | _call_llm uses urllib.request (stdlib) | No extra deps; OpenAI-compatible /v1/chat/completions endpoint |
| 2026-05-24 | VPS running code at nested path hermes_trading/hermes_trading/ | Package installed inside its own package dir — SCP files here, not the top-level dir |
| 2026-05-24 (s2) | loop.py reflection subprocess uses sys.executable | `python` not in PATH on VPS — was silently killing all reflections |
| 2026-05-24 (s2) | reflect.py LLM prompt: explicit variable list + one-shot JSON example | Forces qwen2.5:3b to use indicators[name].field bracket notation correctly |
| 2026-05-24 (s3) | RSI no longer required — min_indicators:2 gates entry instead | Any 2+ indicators firing together is safer than 1 mandatory gate |
| 2026-05-24 (s3) | direction:both — agent evaluates long+short each tick | Higher confidence side wins; ambiguous (<0.1 diff) signals are skipped |
| 2026-05-24 (s3) | _reconcile_open_trades: abandons ALL stale open records | Was leaving ghost "open" trades in trades.jsonl indefinitely |
| 2026-05-24 (s3) | execution.py reads direction from entry_detail | Allows direction:both to place the correct long/short side |
| 2026-05-25 (s4) | Phase 1 SMC: structural SL/TP from support_1h4h/resistance_1h4h | Replaces fixed % SL/TP; SL below swing low + sl_buffer_pct, TP at nearest resistance |
| 2026-05-25 (s4) | Risk-based position sizing: (balance × risk_per_trade) / sl_dist_pct | Replaces fixed 20% of balance; risk_per_trade=10%, capped at MAX_POSITION_USD=$500 |
| 2026-05-25 (s4) | Fixed default_leverage=5 replaces RSI-scaled 3–15x | Simpler; SMC trades sized by risk not leverage |
| 2026-05-25 (s4) | max_sl_pct=5.0% guard: skip trade if structural SL too wide | Prevents entering when S/R level is too far from current price |
| 2026-05-25 (s4) | R:R ratio shown in Live Positions dashboard | Structural TP/SL → real R:R per trade; green ≥1.5R, amber <1.5R |
| 2026-05-27 (s5) | Diagnosed 0% confidence / no-entry symptom — root causes in state/<slug>/strategy.yaml shape, NOT in execution.py | ETH/SOL/TAO YAMLs lack `indicators:` registry → loop falls into long-only RSI<30 fallback. BTC still has rsi.required:true. See diagnosis-session-5.md. |
| 2026-05-27 (s5) | retCode 110043 traced to execution.py:243 only catching ccxt.AuthenticationError | Bybit returns ExchangeError "leverage not modified" when leverage already set; current handler doesn't swallow it. Proposed 7-line idempotent wrapper documented in diagnosis-session-5.md, NOT applied. |
| 2026-05-28 (s6) | $5 hard profit floor: new `_guard_min_profit_usd` in execution.py + `min_profit_usd: 5.0` in all 4 yamls | Linh directive "aim to win at least $5 per trade." Hard skip in `place_live_trade` after qty is computed (qty × \|tp − entry\| < $5 → ValueError). Co-located with Phase 2.1 guards as Guard 4. |
| 2026-05-28 (s6) | reflect.py truncated from 702 → 572 lines (IndentationError fix) | Duplicated `run_hermes` + `main` bodies at lines 572+ from prior Edit-tool corruption. Fixed via Python heredoc; py_compile + ast.parse OK. |
| 2026-05-28 (s6) | `tools/backfill_trades.py` (NEW) — direction-correct pnl_pct via closedPnl/cumEntryValue | Sidesteps the still-broken `(exit-entry)/entry` formula in `fetch_last_closed_pnl` (Phase 2.5 will fix that). Idempotent dedup by order_id. 6 unit tests pass. |
| 2026-06-01 (s7) | Emergency restart after 3-day downtime; no code changes | reflect.py None-safety fix was already on disk (Linh manually cp'd during a partial wave-4 deploy attempt) but agent was pkilled and never relaunched. Restart-only recovery; 8-step deploy block per doctrine #5 worked clean. |
| 2026-06-01 (s7) | Add post-deploy `diff -r hyphen underscore` as standard final check | Doctrine #14 — wave 4 partial cp was invisible for 6 h until reflection needed the missing file. A directory-level diff would have caught it pre-launch. |

## Known Issues / TODOs
- VPS running code at `/opt/trading/hermes_trading/hermes_trading/` (nested) — when deploying new code, SCP to this path OR copy from `/opt/trading/hermes-trading/` after git pull
- state/strategy.yaml in root is legacy — per-asset dirs under state/{asset_slug}/ are the source of truth on VPS
- VPS has stale state/hypotheses.jsonl and state/trades.jsonl at root level (old pre-per-asset structure) — safe to ignore
- Dashboard requires SSH key auth to VPS; shows last-known-state banner when unreachable
- GitHub SSH key on VPS not yet set up — snapshot.sh push fails silently; run setup_github_ssh.sh then add public key to GitHub
- [FIXED session 2] LLM bracket notation: prompt now has explicit newline-delimited variable list + one-shot JSON example; `python` → `sys.executable` in reflection subprocess

## Handoffs
- **Status (end of session 6, 2026-05-28)**: Phase 2.4 DEPLOYED to VPS. Commit `f0f0893` pushed; running agent restarted (replaced PID 1320187 with fresh process launched from `/opt/trading/hermes_trading`). New `min_tp_pct` guard confirmed firing in live log (BTC skip at 01:29 UTC: "Structural TP too thin: 0.02% < min 3.00%"). No IndentationError on agent restart → reflect.py truncate fix took effect. Backfill ran with `bybit=0` (likely Bybit V5 7-day query window quirk; TAO already has 23 native-logged trades → reflection unblocked anyway).
- **Deploy mishap logged for doctrine**: 5-step deploy block in `session-6-phase-2.4.md` had Step 2 combining `diff` + `cp` in two code blocks under one heading; Linh ran only the diff and skipped the cp, then hit `ModuleNotFoundError` on backfill. Recovery: one extra ssh cp command. Lesson: every deploy step should be ONE code block, ONE command. Update template before next deploy.
- **Env var pickup**: backfill needed `set -a; source .env; set +a` prefix because `python -m tools.backfill_trades` doesn't auto-load `.env` (execution.py uses `os.getenv` directly, not python-dotenv). Same prefix is needed for the agent restart command. Add to the runbook.
- **Anomaly flagged for separate session**: Bybit account shows `cumRealisedPnl: -18272.77 USDT` per the balance debug print. Could be historical test losses or real accumulated loss. Worth an audit session: pull full closed-PnL history, bucket by date, identify the bleed.
- **Next-session candidate**: Phase 2.5 (`fetch_last_closed_pnl` direction-aware pnl_pct) + Phase 2.3 (`fetch_recent_closed_trades` for dashboard merge) — same function, do together.
- **Status (end of session 5c)**: VPS agent is RUNNING — PID 1290662, all 4 workers booted in live mode. Awaiting first 15m tick to confirm `dir=both · conf=NN%` lines and absence of new 110043 tracebacks.
- **Linh to do next**:
  1. `tail -f /opt/trading/hermes_trading/bot.log` and verify first tick shows real conf% per asset
  2. If first 2-3 ticks show `dir=both · conf=0%` across the board, signal is genuinely flat (different problem — would need to inspect price adapter output)
  3. If `Trade #1` fires: verify on dashboard that R:R column shows the structural value, and that SL/TP are at swing levels not fixed % distances
  4. SCP `/tmp/hermes-backup-<STAMP>/` to local if you want to preserve pre-session-5 yamls (else they'll vanish on VPS reboot)
  5. Consider Option B (collapse to single folder) vs Option A (keep two-folder pattern) for the `/opt/trading/hermes-trading/` + `/opt/trading/hermes_trading/` situation — separate session
- **Carried over from session 4**: dashboard Live Positions R:R display (verify on next live tick)
- **Handoff to**: Linh (monitor + decide on next-session topics)

#### Session 5d — Strategy review document
- Linh raised 6 strategy questions (min R:R 2.0, TP zones, trailing stops, dashboard-Bybit reconciliation, swing vs SMC style, 3% return target).
- Wrote up answers in `strategy-review-session-5.md` with: empirical context from TAO Trade #1 (R:R 1.73, conf 54.76%), per-question analysis, proposed Phase 2 implementation order (min TP guard + min R:R + target-return filter in one session; trailing stops next; Bybit backfill after; TP zones deferred to Phase 3).
- **No code or YAML changes.** Phase 2 starts only after Linh answers 5 open questions in the doc (soft vs hard R:R guard, return-enforcement option A/B/C, trailing stop fixed vs ATR, timeframe 15m vs 1h, duplicate-trade-record bug timing).
- **Anomaly logged for later:** TAO trades.jsonl had 1 line at scp time, 4 identical lines at later ssh-cat time. Only one real Bybit order was placed. Pure bookkeeping bug. Defer to post-Phase-2.

#### Session 5e — Phase 2.1 implementation (2026-05-27)
- **Decisions locked**: soft R:R (extend TP, don't skip), Option B target-return filter, ATR-based trail next, dupe-bug deferred.
- **execution.py rewritten** with three guards in `_structural_sl_tp`: max_sl_pct (existing), min_tp_pct (new), min_rr_ratio (new soft). Written atomically via Python (Write tool truncated on Windows mount AGAIN — same hazard as Edit, now confirmed both tools have this failure mode).
- **All 4 strategy.yamls** got `min_tp_pct: 3.0` and `min_rr_ratio: 2.0`.
- **6 unit tests pass.** Crucially: TAO Trade #1 replay (entry $282, support $278.8 / 1.13% from entry) **correctly skips** under new Option B filter. That trade would NOT fire under new rules.
- **Commit `55cbbaa`** landed locally despite `.git/index.lock` permissions noise on Windows mount.
- **Push to GitHub**: pending Linh's PowerShell.
- **Deploy to VPS**: pending Linh's PowerShell + SSH.
- **Expected post-deploy effect**: trade frequency drops 60-80%; trades that fire have R:R ≥ 2.0 and structural target ≥ 3% price move; the 10001 "TP too close" Bybit rejection on BTC/ETH should disappear since 3% >> typical slippage.

### Persistent operational issue: Windows-mount file tool corruption
- Both `Edit` AND `Write` tools have truncated files on the C:\Users\nghil mount. Always use `python -c '...'` or `python <<EOF` heredoc via bash for Python file changes. Validate with `python -m py_compile` immediately. Never trust the file tool's "success" response for Python files on this mount.

### SSH setup for dashboard (session 5f, 2026-05-27)
- Local Windows machine had NO ssh keys at all (only known_hosts). Dashboard's `BatchMode=yes` SSH was falling through to password auth, which the VPS rejects for root (standard `PermitRootLogin prohibit-password` policy).
- Generated `C:\Users\nghil\.ssh\hermes_vps` (passphraseless ed25519) on Windows.
- Added the pubkey to VPS `/root/.ssh/authorized_keys` (preserved the pre-existing `hermes-dashboard` orphan key).
- Added `Host 187.127.108.173` block to `~/.ssh/config` with `IdentityFile ~/.ssh/hermes_vps` and `IdentitiesOnly yes`.
- `ssh -o BatchMode=yes ... "echo OK"` now succeeds → dashboard's `_ssh_batch` works.
- **Tripwire to remember**: the pre-existing `hermes-dashboard` key entry in authorized_keys had no trailing newline. `echo >> file` concatenated the appended line onto the same line, corrupting both entries. Always rebuild authorized_keys via heredoc (`cat > file <<'EOF' ... EOF`) and verify with `wc -l` afterward.

## Session Log

### 2026-06-01 (session 7) — Emergency restart after 3-day downtime
**Symptom**: Cowork open, asked for `next-session-prompt.md` review. Diagnostic SSH probe revealed bot process gone, all 4 heartbeats stale at `2026-05-29T06:30:04 UTC` — **3 days down**. No process, no auto-reflection had fired, VPS uptime 69 days (no reboot), no OOM, disk fine.

**Root cause (chronology reconstructed)**:
1. Session 6 wave 4 prepared the audit-fields + None-safety patch (commit `ee0dea2`) touching loop.py + execution.py + reflect.py.
2. Deploy block on 2026-05-29 was run partially — loop.py and execution.py made it to underscore clone, **reflect.py did not** initially.
3. Bot restarted with NEW loop/execution + OLD reflect.py.
4. Bot ran ~6h. TAO Trade #25 triggered `--hermes` reflection at 04:00 UTC → crashed at reflect.py:434 with `TypeError: float(None)` (the exact bug wave-4 None-safety was meant to fix).
5. Reflection subprocess error swallowed by loop.py; bot kept ticking until 06:30 UTC.
6. At some point between crash and 06:30: Linh manually `cp`'d the fixed reflect.py (hyphen → underscore) and `pkill`'d to restart — but never re-launched. **Bot dark for 3 days.**

**Restart this session (8 steps, one block per action per doctrine #5)**:
1. `git push origin master` from PowerShell → "Everything up-to-date" (all fixes already on GitHub).
2. `git pull` on hyphen clone → only memory.md updated (reflect.py fix was already pulled previously).
3. `diff` hyphen vs underscore reflect.py → empty (already in sync — confirming Linh did the catch-up cp).
4-5. Skipped — files identical.
6. Verification block: None-safety filter ×5 ✓, audit fields ×12 ✓, wealth-curve marker ×6 ✓, line 434 = `options.num_ctx` (not crash line) ✓, py_compile OK ✓.
7. Restart: `pkill -f hermes_trading.run; set -a; source .env; set +a; nohup .venv/bin/python -m hermes_trading.run >> bot.log 2>&1 & disown`. PID 1525537, cwd `/opt/trading/hermes_trading`, all 4 workers booted.
8. Verify: 4 workers in log, first tick at 10:21 UTC processed reconciles cleanly.

**Reconcile during restart (positions managed exchange-side via SL/TP-on-order during downtime)**:
- ETH: closed by TP / exchange-side at +1.87% (reason `manual_or_other` — meaning closed without bot knowing tp/sl source)
- SOL: closed at +1.75% (same)
- TAO: closed by SL_hit at -0.65% (**first trade post v03 Hermes mutation `volume_spike.min_ratio 1.5→2.0`** — one-sample warning, see watchlist)
- BTC: no position (still blocked by `min_tp_pct: 3.0`)

**Doctrine added this session**:
14. **A partial deploy is invisible until something needs the missing piece.** Wave 4 cp landed 2 of 3 files; bot ran fine for 6 h until reflection demanded the third. **Defense**: after any multi-file deploy, run a `diff -r` between hyphen-clone and underscore-clone for the package dir as a final check. Treat any non-empty diff as deploy-incomplete.
15. **Heartbeat staleness is a higher-signal alarm than process-exists.** Process-down is obvious; heartbeat-stale-but-process-up would catch hangs too. Worth a passive cron on VPS: every 30 min, alert if any heartbeat is >20 min old.
16. **PowerShell here-string → ssh bash carries CR-LF — the LAST argument of the LAST command gets `\r` appended.** Tools that compare arguments to a known set (argparse `--dry-run`, file open, exec lookup) fail: `--dry-run\r` ≠ `--dry-run`; `tail bot.log\r` looks for a literal `bot.log\r` file. **Mandatory workaround for every PowerShell `@'...'@ | ssh bash` block: end the body with a trailing throwaway line like `echo DONE` so the `\r` corruption lands on something tolerant.** Confirmed twice in session 7: `tail bot.log` and `--dry-run` both failed without the throwaway, both worked with it.

**Open watchlist post-restart**:
- TAO v03 SL_hit on first post-mutation trade (1 sample, irrelevant alone — but Phase 2.8 sanity-check is worth landing before the next 5-trade boundary fires Hermes again).
- Intermediate trades during downtime gap (reconcile caught only the most recent close per asset) — `tools/backfill_trades.py` should pull the full 7-day window to recover any missing records.
- cumRealisedPnl on Bybit balance now `-18,251` (was `-18,272` at session-6 end) — tiny improvement from exchange-side TP fills during downtime.

**Files touched this session**: `session-7-restart.md` (NEW, the full diagnostic + deploy block); `memory.md` (this entry).

**Handoff**:
- **To**: Linh (monitor + decide whether to run backfill / start Phase 2.8 / wait for stability before Phase 2.2)
- **Read first next session**: `next-session-prompt.md` (update after this session), then this memory.md entry, then `session-7-restart.md` if context is needed
- **Phase 2.2 (ATR trailing stop) remains pending** — paused until bot is verified stable ≥24 h

### 2026-05-28 (session 6) — Phase 2.4 reflection rebuild + $5 hard guard
- **reflect.py was corrupted** with duplicated `run_hermes` + `main` bodies at lines 572-701 (prior Edit-tool corruption). Final 5 lines had `console.print` truncated to bare `print` at module-level indent → `IndentationError` on line 572. Reflection subprocess had been failing on every invocation since.
- **Fix:** truncated cleanly at the first occurrence of `if __name__ == "__main__":\n    main()\n`. File 702 → 572 lines. Validated with `python -m py_compile` and `ast.parse`. Written atomically via Python heredoc + `.tmp` → `replace()` (never trust Edit/Write on the Windows mount; lesson re-confirmed).
- **TAO trade visibility:** local `state/tao_usdt/trades.jsonl` had 1 line (the open Trade #1 from session 5). Linh reported "lots of small TAO short trades" — those were all on Bybit, invisible locally. `_count_closed_trades` was therefore returning ~0 for every asset and reflection was never crossing its cadence threshold.
- **New `tools/backfill_trades.py`** — one-shot Bybit closed-trade backfill, calls `private_get_v5_position_closed_pnl` per asset (paginated via `nextPageCursor`, 30-day default lookback), appends missing records to `trades.jsonl` deduped by `order_id`. Idempotent. **Direction-correct pnl_pct** computed as `closedPnl / cumEntryValue` (sidesteps the broken `(exit-entry)/entry` in `fetch_last_closed_pnl` — that's Phase 2.5). Synthetic records flagged `backfilled: true`, `strategy_version: "backfilled"`. CLI supports `--asset`, `--dry-run`, `--lookback-days`. New `tools/__init__.py` so `python -m tools.backfill_trades` works.
- **New $5/trade hard guard** (`_guard_min_profit_usd` in execution.py). Called in `place_live_trade` between `_risk_based_qty` and `create_order`. Raises ValueError if `qty × |tp − entry| < min_profit_usd`. Loop catches and logs "Entry skipped — Expected TP profit too small: $X.XX < min $5.00". Added `min_profit_usd: 5.0` to all 4 strategy.yamls.
- **Test coverage:** 6 backfill unit tests (winning-short → +pnl, losing-long → -pnl, malformed → None, dedup, idempotency, dry-run safety) + 6 guard tests (floor pass/fail, custom floor, zero-profit edge, TAO Trade #1 replay still blocked by min_tp_pct, constructed trade passing Phase 2.1 but failing $-guard). All green.
- **TAO Trade #1 (entry $282, R:R 1.73) would not fire under any combination of current guards** — already blocked by `min_tp_pct: 3.0` since `|278.8 − 282|/282 = 1.13% < 3%`. Confirmed in test B.
- **Not committed/pushed yet** — local `.git` clean of locks at session end but Linh deploys from PowerShell. Deploy block in `session-6-phase-2.4.md`.
- **Files I did NOT touch** (still uncommitted from prior sessions, Linh should review separately): `state/goal.yaml`, `snapshot.sh`, `state/tao_usdt/trades.jsonl`, `next-session-prompt.md`, `state/btc_usdt/goal.yaml`, deleted `*.tar.gz` artifacts.
- **Watchlist:** Phase 2.5 `fetch_last_closed_pnl` direction-aware pnl_pct fix; Phase 2.3 `fetch_recent_closed_trades` for dashboard merge (pair with 2.5); Phase 2.2 ATR trailing stop (waits for 5–10 calibration trades).

#### Session 6 cont. (later) — Phase 2.5 pnl_pct fix + dashboard $ figures + Ollama context bump + CSV importer
- **Diagnosis from manual reflection trigger**: fallback fired with "Drawdown 557.33% exceeded goal 5.00%" — absurd. Root cause: existing `fetch_last_closed_pnl` formula `(exit - entry) / entry` is direction-blind, so every short was sign-flipped in `trades.jsonl`. Linh confirmed by comparing dashboard vs Bybit CSV export.
- **Bybit reality from CSV (uploaded by Linh)**: 45 closed trades in window, total realized P&L **+$44.41** (winning bot!). TAO 30 trades +$23.58 (50% win, 12/20 shorts won). BTC 5 trades +$10.41 (60%). ETH 7 +$3.02 (57%). SOL 3 +$7.40 (67%). Dashboard had been showing -3.07% all-time because of the sign flip.
- **Local vs Bybit gap**: 45 Bybit trades vs ~30 in local jsonl. 15 trades fired on Bybit but never landed locally (agent restarted between fire and log, or earlier silent log failures). Backfill tool couldn't fetch them because Bybit's `/v5/position/closed-pnl` endpoint has a 7-day query window.
- **Fixes shipped (wave 3)**:
  1. `execution.fetch_last_closed_pnl` rewrite: direction-correct pnl_pct from `closedPnl / cumEntryValue` (matches the backfill convention). Also returns `direction` and `qty` for richer reconcile records.
  2. `tools/recompute_pnl_pct.py` (NEW, ~250 lines): walks each `state/<slug>/trades.jsonl`, recomputes pnl_pct purely from `entry_price`, `exit_price`, `direction` (no Bybit re-fetch needed). Preserves old value as `pnl_pct_old`. Idempotent. Skips backfilled records by default (they're already direction-correct).
  3. `tools/import_bybit_csv.py` (NEW, ~250 lines): parses Bybit's UI-exported CSV, infers direction from sign(P&L) vs sign(exit-entry), dedupes against existing records by (asset, ts_minute, entry_rounded). Flags imports as `csv_imported: true` and `strategy_version: "csv_import"` so reflection can distinguish them.
  4. `reflect.py` LLM prompt shrink: replaced full 25-trade `recent_trades` with compact summaries (`ts, dir, pnl_pct, conf, fired, close, rr`). Trimmed memory_context tail from 3000 → 1500 chars. Expected prompt size drops from ~18K tokens (over Ollama's 4K limit) to well under 4K.
  5. `reflect.py` Ollama context bump: added `options.num_ctx: 8192` to LLM request payload, configurable via `HERMES_LLM_NUM_CTX` env. qwen2.5:3b natively supports 32K; Ollama defaults to 4K unless told otherwise. RAM cost ~1GB extra for KV cache; VPS has 6.7GB free.
  6. `dashboard.py` $ figure additions: new `_pnl_usd(t)` helper resolves $ P&L (prefers Bybit's `closed_pnl_usdt`, falls back to `pnl_pct × entry × qty`). New `_period_pnl_usd` for aggregates. Added $ row to all 4 Running P&L summary cards. Added "P&L $" column to trade history table alongside existing "P&L %".
- **Test coverage**: 4 pnl_pct correctness tests, 6 importer tests (direction inference truth table, row→trade for known short win, real-CSV dry-run for 45 rows, dedup, idempotency, dry-run safety). All green.
- **Rollback of bad v02**: TAO `strategy.yaml` restored from `history/v0000.yaml` (was bumped by fallback acting on flipped pnl_pct). Backed up to `/tmp/rollback-tao-1780037833.yaml`.
- **Ollama is healthy** — not the bottleneck. Issue was prompt size (18K vs 4K limit) and the model truncating then hanging. Fixed by both shrink + num_ctx bump.
- **Not yet deployed.** Combined deploy block coming next. Files changed: execution.py, reflect.py, dashboard.py (local only — no VPS deploy needed), tools/recompute_pnl_pct.py (NEW), tools/import_bybit_csv.py (NEW), data/bybit-closed-pnl-2026-05-29.csv (NEW reference copy).

#### Session 6 wave 4 — Data repair + first successful Hermes reflection (2026-05-29)
**The big win**: first Hermes-driven (LLM, not fallback) strategy mutation in project history. TAO v02 → v03: `indicators[volume_spike].params.min_ratio 1.5 → 2.0` based on actual decision_context (~30 closed trades, 50% win rate, +5.6% realized).

**Bugs found and fixed (in order encountered)**:
1. `fetch_last_closed_pnl` direction-blind formula — Linh confirmed via Bybit CSV (45 trades total +$44.41) that all short trade pnl_pct signs were inverted in trades.jsonl. Fixed: now uses `closedPnl / cumEntryValue`. Plus wrote `tools/recompute_pnl_pct.py` to repair existing records (11 sign-flips: BTC 2, ETH 2, TAO 7).
2. `_max_drawdown` divide-by-near-zero — old formula `(peak - c) / (abs(peak) + 1e-9)` amplified small dips into 80%+ false drawdowns. Fixed to compound wealth-curve `(peak - wealth) / peak`, bounded [0,1]. Sanity-checked: Linh's 24-trade balanced book now reports ~2% drawdown, not 82%.
3. CSV importer (Bybit truth source) was missing on day 1 — wrote `tools/import_bybit_csv.py`, but dedup logic went through three iterations:
   - v1: (asset, ts_minute, entry) — failed because local ts = OPEN, CSV ts = CLOSE (6–8h gap)
   - v2: (asset, entry, exit, qty) — failed because Bybit's avg-entry differs from local fill price by cents
   - v3: (asset, exit, qty) — works, dedupes the 11 real local closed trades exactly
4. Local trades.jsonl polluted by abandoned stubs from prior agent restarts (17 of 25 TAO records had `entry==exit AND abandoned: true`). Wrote `tools/purge_abandoned.py` — removed 20 stubs across all assets. Backups at `*.jsonl.bak-<unix>`.
5. Ollama timed out at 120s on every Hermes call — root cause was first-call cold reload of model with new num_ctx=8192 (added earlier in session). Fixed by bumping `HERMES_LLM_TIMEOUT=300` in .env + pre-warming with one trivial curl. Reflection now completes in 30–60s.
6. LLM dot-notation clobber risk — first Hermes mutation today's payload looked like it used dot notation (`indicators.params.min_ratio`) because rich library silently stripped `[volume_spike]` from the live display. Actual stored changed_variable was the correct bracket form, and `_set_nested` updated volume_spike correctly. Display-only bug. Pushed two defensive guards anyway: reject any `indicators.*` path without brackets, refuse to overwrite a list with a dict. Also escaped brackets in the rich-print to prevent the display confusion recurring.

**Tools shipped this wave** (all in `tools/`):
- `recompute_pnl_pct.py` — direction-correct pnl_pct from entry+exit+direction (preserves old as pnl_pct_old)
- `import_bybit_csv.py` — Bybit CSV → trades.jsonl, infers direction from sign(P&L) vs sign(exit-entry), dedupes by (exit, qty)
- `purge_abandoned.py` — removes entry==exit OR abandoned=true stubs, writes timestamped backup

**Dashboard ($ figures shipped earlier in wave)**: each Running P&L card now shows % above $; trade history has both "P&L %" and "P&L $" columns. Resolved from `closed_pnl_usdt` if present, else computed from `pnl_pct × entry × qty`.

**Strategy mutation chain on TAO**:
- v01 (original) → v02 fallback (drawdown bug, position_size_r 0.8 → 0.85, rolled back) → re-mutated v01 → v02 fallback (drawdown fixed, position_size_r 0.8 → 0.85, KEPT) → v03 Hermes (volume_spike.min_ratio 1.5 → 2.0, KEPT)

**Remaining known issues (defer)**:
- strategy.yaml lacks a top-level `version` field, so archive always uses default "00" → v0000.yaml overwrites itself. Cosmetic; the version IS being read+bumped (just from somewhere else in the file).
- Dashboard merge with Bybit closed-pnl for full audit (Phase 2.3 originally) — partly addressed by CSV import but not by API merge.
- ATR-based trailing stop (Phase 2.2) — wait for more clean trades.
- Bot continues to win small ($5–15 per trade) on ~50% win rate.

**Doctrine added this session (apply going forward)**:
1. **PowerShell -> SSH -> bash quote tax is real.** PowerShell strips embedded `"..."` from single-quoted strings unpredictably, especially when the string contains `(`, `)`, `|`, or nested quotes. For any complex bash command via ssh, use one of:
   - Double-up single quotes: `'sed -i ''s/x/y/'' file'` (the `''` collapses to one `'` in the literal)
   - PowerShell here-string piped to ssh: `@'...'@ | ssh root@host`
   - SCP a `.sh` file to /tmp and execute it remotely
   Never trust quoted output that worked once — it can break on the next invocation depending on the surrounding command.
2. **Always run a real verify after a `cp` rollback.** The `cp v0001.yaml strategy.yaml` failed silently because v0001.yaml didn't exist, and the diagnostic was assumed to confirm it worked. Verify with `grep` for an EXPECTED value (e.g. `grep position_size_r`) and explicitly confirm the value. Don't just check the file parses.
3. **`rich` markup eats bracket-pair strings in console.print.** Always escape `[name]` in printed paths by replacing `[` with `\[` if the variable might contain bracket notation. Hypotheses.jsonl is the truth source, not stdout. Fixed today in run_hermes display.
4. **LLM hypothesis recording must happen BEFORE strategy.yaml is overwritten.** Currently it does (the failed-application path records `error_reason: key_not_found` before falling back). Keep this invariant.
5. **One-step deploy blocks per logical action.** Today's "Step 2: diff + cp" combined both, only the diff was run, downstream tools failed with `ModuleNotFoundError`. Each `cp`, each `git pull`, each restart should be its own labeled code block.
6. **Backups before destructive ops.** purge_abandoned writes `.jsonl.bak-<unix>`; recompute_pnl_pct preserves `pnl_pct_old`; rollback should backup current strategy.yaml to `/tmp/` first. Every tool that mutates a state file should leave a trail.
7. **`_set_nested` clobber guards added.** Rejects dot-notation indicator paths (`indicators.params.X` without `[name]`) and refuses to overwrite an existing list with a dict. Defense against the next time the LLM forgets bracket notation.

#### Session 6 cont. — Audit trail additions + reflect.py None-safety hotfix
- **Triggered by**: first manual `reflect --hermes` for TAO crashed with `TypeError: float() argument must be a string or a real number, not 'NoneType'` at reflect.py:434. Root cause: `t.get("pnl_pct", 0)` returns `None` when the key exists with value `None` (every open trade), not the `0` default. Same bug pattern in `_realised_return` and `_max_drawdown`. Latent for weeks because the IndentationError masked it.
- **None-safety fix**: filtered `pnl_pct is not None` in `_realised_return`, `_max_drawdown`, and the `run_hermes` performance_summary aggregation. Also restructured `run_hermes` to compute `closed = [...]` once and reuse for `winning/losing/win_rate/total_pnl`, and added explicit `closed_trades` + `open_trades` counts to the prompt the LLM sees (more informative).
- **Trade-record audit additions** (essential scope, per Linh): added 4 new fields to every trade record at entry time:
  - `confidence_breakdown` — per-indicator dict `{name: {fired, required, weight, weight_contributed, value, threshold, comparator}}`. Built by `_evaluate_entry` from the new dict-returning `_check_indicator`.
  - `entry_gates` — snapshot of all gate values from strategy.yaml at decision time (min_confidence, min_indicators, max_sl_pct, min_tp_pct, min_rr_ratio, min_profit_usd, risk_per_trade, default_leverage, sl_buffer_pct, strategy_version). New helper `_entry_gates_snapshot(strategy)` in loop.py.
  - `evaluation_summary` — one-line human-readable narrative like "SHORT | fired=['rsi','ema_trend'] | no-fire=['macd','vwap'] | conf=70.00% | FIRE" or with "SKIP: confidence 24% < min 30%".
  - `close_reason` — set at closure: `TP_hit` | `SL_hit` | `manual_or_other` | `abandoned_stale` | `abandoned_no_pnl` | `unknown`. New `_infer_close_reason(exit, tp, sl, tol=0.5%)` helper. Wired in both `_reconcile_open_trades` paths.
- **Hypothesis-record audit additions** (essential scope): added 4 new fields to every hypothesis record in both `--hermes` and `--fallback` modes:
  - `decision_context` — performance_summary block as seen by LLM (closed/open/wins/losses/win_rate/total_pnl/avg_indicators_on_wins/losses) OR rule-based metrics for fallback.
  - `trade_range` — `{count, from_ts, to_ts, trade_ids}` so audits can re-fetch the exact trade window the decision was based on. New `_trade_range(trades)` helper in reflect.py.
  - `applied_successfully` — bool from `_set_nested` return. If False (LLM proposed an unknown key), the failed attempt is STILL written to hypotheses.jsonl with `error_reason: "key_not_found"`, then run_fallback is invoked. Preserves audit trail even when LLM is wrong.
  - `llm_raw_output` — literal string the LLM returned, BEFORE `json.loads`. Critical for debugging malformed-JSON cases. Set to `None` for fallback mode.
- **Test coverage**: 14 audit tests all green (in addition to the 12 from earlier in session 6). Covers `_check_indicator` dict shape, `_evaluate_entry` breakdown + skip-reason summary + fallback path, `_entry_gates_snapshot` completeness, `_infer_close_reason` for TP/SL/manual/unknown, `_trade_range` empty + populated, None-safety regression, source-level checks confirming all 4 audit fields write in both reflection modes.
- **Files changed this sub-session**: `hermes_trading/loop.py` (5 edits: _check_indicator rewrite, _evaluate_entry rewrite, +2 helpers, _reconcile_open_trades, _simulate_paper_trade), `hermes_trading/adapters/execution.py` (2 edits: import + trade record), `hermes_trading/reflect.py` (3 edits: None-safety, _trade_range helper, both hypothesis-write paths).
- **Not yet deployed.** Combined commit + deploy block coming next.

### 2026-05-27 (session 5) — Diagnosis of 0% confidence / no-entry symptom
- **Read-only session.** No Python, YAML, or runtime state modified.
- **Bug 1 (critical, 3 of 4 assets):** `state/eth_usdt/strategy.yaml`, `state/sol_usdt/strategy.yaml`, `state/tao_usdt/strategy.yaml` are in a legacy flat schema (`position_size_r`, `rsi_entry_oversold`, etc.) with NO `indicators:` block and NO `entry:` block. `loop._evaluate_entry` therefore falls into the no-indicators fallback at line 256–261, which is hard-coded to `direction="long"` and `threshold=30`. RSI in 60–75 → `rsi < 30` is False → fires=False, conf=0%. Matches reported symptom exactly.
- **Bug 2 (high, BTC):** `state/btc_usdt/strategy.yaml` v02 still has `rsi.required:true, threshold:30`. Memory.md sessions 3+4 claimed RSI was demoted to optional via `patch_strategies.py`, but the local file disagrees. Hard gate blocks every long entry while RSI > 30. Short leg passes the gate trivially but optional indicators rarely reach the 0.3 confidence threshold in current tape.
- **Bug 3 (low, leverage):** `execution.py:243` only catches `ccxt.AuthenticationError`. Bybit retCode 110043 ("leverage not modified") surfaces as `ccxt.ExchangeError` and propagates as red error spam. Cosmetic — doesn't block trades, but inflates the consecutive-failure counter. Also a latent `NameError` on line 246 if `default_leverage` is absent on a non-Bybit exchange.
- **Recommendation:** apply `patch_strategies.py` to local + VPS (idempotent, YAML-only) to fix bugs 1 & 2. Defer the 7-line `execution.py` leverage wrapper (bug 3) to a separate session so signal-frequency changes can be attributed cleanly to the YAML fix.
- **No code or YAML changes were made.** Memory file updated, diagnosis report `diagnosis-session-5.md` created in project root.

#### Session 5b (same day) — Linh said "do all", applied
- Ran `patch_strategies.py` locally — all 4 strategy.yaml now have direction:both, min_indicators:2, rsi.required:false, default_leverage:5, sl_buffer_pct:0.3, max_sl_pct:5.0.
- Critical follow-on caught: ETH/SOL/TAO had **no `indicators:` block** before, so they would have fallen into loop._evaluate_entry's fallback (returns conf=1.0 every tick on short side once direction=both). Copied BTC's 9-indicator registry into all three via a small yaml-safe Python script.
- Applied 7-line `execution.py` edit: lifted `leverage` out of `try`, added `except ccxt.ExchangeError` that swallows retCode 110043 and re-raises everything else, removed dead `leverage_display` (confirmed unused via grep).
- **Edit-tool corruption:** the leverage edit truncated the last 8 lines of `fetch_last_closed_pnl`. Detected by `py_compile`. Restored via Python script that re-attached the original tail verbatim. AST parse + py_compile both OK afterwards.
- **PyYAML comment loss:** `patch_strategies.py` uses `yaml.dump` which strips human-written comments. ETH/SOL/TAO YAML structural content is preserved but the inline notes were lost. Acceptable trade-off; original files are in `/tmp/{asset}_before.yaml` in the sandbox for reference if needed.
- **BTC default_leverage change:** BTC YAML had `default_leverage:` commented out (let Bybit use 12.5x default). Patch re-set it to 5. Combined with the new idempotent 110043 handler, this is safe — but it IS a behavior change.
- **Not committed/pushed yet** — `.git/index.lock` exists from Windows and the sandbox can't unlink it. Full PowerShell + VPS deploy block written into `diagnosis-session-5.md` (Session 5 — Applied section).
- **Lesson logged:** `Edit` tool is unsafe on files visible via the Windows mount. Always `python -m py_compile` after every Python edit. Prefer Read + Write (full overwrite) over Edit for `.py` files in this project.

#### Session 5c (same day) — Push, deploy, recovery, agent restart
- Push from sandbox blocked (no GitHub creds); Linh pushed `36bfbb3` from PowerShell.
- VPS deploy went WRONG on first attempt: my deploy block in `diagnosis-session-5.md` used incorrect paths (nested `hermes_trading/hermes_trading/state/...`, `venv` instead of `.venv`, `logs/hermes.log` instead of top-level `bot.log`). Linh ran it, `pkill` killed the agent, all `cp` commands failed, `source venv/bin/activate` failed, `nohup` exited 1 → **agent down for ~15 minutes**.
- Read-only diagnostic block mapped the actual VPS layout (see "VPS layout" section above). Confirmed:
  - `/opt/trading/hermes_trading/hermes_trading/adapters/execution.py` on the VPS was already byte-identical to my session 5 version (someone had applied the 110043 handler locally before; the `M` in earlier `git status` was for that).
  - All 4 `state/<slug>/strategy.yaml` files in running dir still had the pre-session-5 shapes → critical to copy.
  - Per-asset `trades.jsonl` files were all 0 lines → confirms signal generation, not execution, was the root cause.
- Recovery deploy: backup → copy 4 strategy.yamls → restart with correct paths. Agent up as PID 1290662, all 4 workers booted in live mode at correct state dirs.
- **Backup of pre-deploy strategy.yamls at `/tmp/hermes-backup-<STAMP>/` on VPS.** Will persist until VPS reboot or `/tmp` cleanup. Should be SCP'd to local for permanence if Hermes-evolved values matter.
- **Awaiting first 15m tick** to confirm `dir=both · conf=NN%` lines appear and no new `110043` tracebacks.

### Lessons added to deploy doctrine (apply going forward)
1. **`logs/hermes.log` does not exist on this VPS.** Top-level `bot.log` is the log. Update all deploy blocks.
2. **`venv` does not exist; it's `.venv`.** Update all deploy blocks.
3. **State dirs are siblings of the package, not nested inside it.** Path is `/opt/trading/hermes_trading/state/<slug>/strategy.yaml`. The "nested package inside package dir" line in earlier memory was misleading — the package IS nested (`/opt/trading/hermes_trading/hermes_trading/`), but state is not.
4. **Always run a `diff` between SOURCE and RUNNING before any `cp`** — execution.py was already up-to-date on this VPS, so the `cp` would have been a no-op but the discipline matters.
5. **Always backup state files before overwriting** — `mkdir -p /tmp/hermes-backup-$(date +%Y%m%d_%H%M%S)/` then `cp` the running yamls there first.
6. **Prefer single-line semicolon-separated bash blocks for SSH paste.** Multi-line PowerShell-style blocks misbehave at `>>` continuation prompts and break in PowerShell when Linh pastes into the wrong window.

### 2026-05-25 (session 4) — Phase 1 SMC implementation
- **Phase 1 SMC: structural SL/TP** — `execution.py` now derives SL from `support_1h4h` (long) or `resistance_1h4h` (short) with a `sl_buffer_pct` (0.3%) buffer. TP set at nearest structural resistance/support. Both fall back to fixed `stop_loss_pct` when no structural level is available.
- **Risk-based position sizing** — replaced fixed 20% of balance with `(balance × risk_per_trade) / sl_dist_pct`. `risk_per_trade=10%`, capped at `MAX_POSITION_USD=$500` env var.
- **max_sl_pct guard** — if structural SL is >5% from entry, trade is skipped (raises ValueError). loop.py catches this and logs "Entry skipped — structural SL too wide".
- **Fixed leverage** — `default_leverage=5` replaces RSI-scaled 3–15x. Simpler, more consistent.
- **R:R ratio** — stored in trade records (`rr_ratio`); dashboard Live Positions table shows it in colour (green ≥1.5R, amber <1.5R). Computed from structural TP/SL distances.
- **Paper sim updated** — `_simulate_paper_trade` uses `_structural_sl_tp` for consistent paper/live behaviour.
- **reflect.py** — added `risk_per_trade`, `sl_buffer_pct`, `max_sl_pct`, `default_leverage` to AI tunable vars.
- **risk_per_trade set to 10%** per Linh's request (was 1%).
- **Deploy from Windows PowerShell**:
  ```
  cd C:\Users\nghil\Projects\Hermes\Hermes-Trading
  Remove-Item .git\index.lock -Force -ErrorAction SilentlyContinue
  git add hermes_trading/adapters/execution.py hermes_trading/loop.py hermes_trading/run.py hermes_trading/reflect.py dashboard.py patch_strategies.py memory.md
  git commit -m "feat: Phase 1 SMC — structural SL/TP, risk-based sizing (10%), fixed leverage, R:R in dashboard"
  git push origin master
  ```
- **Then on VPS**:
  ```
  cd /opt/trading/hermes-trading && git pull
  cp hermes_trading/adapters/execution.py /opt/trading/hermes_trading/hermes_trading/adapters/execution.py
  cp hermes_trading/loop.py /opt/trading/hermes_trading/hermes_trading/loop.py
  cp hermes_trading/run.py /opt/trading/hermes_trading/hermes_trading/run.py
  cp hermes_trading/reflect.py /opt/trading/hermes_trading/hermes_trading/reflect.py
  cp patch_strategies.py /opt/trading/hermes_trading/hermes_trading/patch_strategies.py
  cd /opt/trading/hermes_trading/hermes_trading
  python3 patch_strategies.py
  pkill -f hermes_trading.run
  cd /opt/trading/hermes_trading && source venv/bin/activate
  nohup python3 -m hermes_trading.run >> logs/hermes.log 2>&1 &
  ```

### 2026-05-24 (session 3)
- **Diagnosed agent DOWN**: goal.yaml line 31 had `%` instead of `#` causing yaml.ParserError on startup. Fixed.
- **Open trade reconciliation bug fixed**: _reconcile_open_trades previously only updated the most recent open trade. Old ghost records (TAO 05-23, ETH 05-23) stayed open forever. Now abandons ALL stale open records (pnl_pct=0, abandoned=True), keeping only the most recent (which matches the live Bybit position).
- **direction:both added**: loop._evaluate_entry now takes force_direction param. run() evaluates long AND short every tick when strategy says "both", picks the higher-confidence side. Ambiguous signals (< 0.1 confidence diff) are skipped.
- **min_indicators added**: entry.min_indicators (default 2) requires at least N optional indicators to fire. Replaces the single required RSI gate.
- **RSI demoted to optional**: patch_strategies.py updates all 4 existing VPS strategy.yaml files to set required:False on RSI, direction:both, min_indicators:2. Run once on VPS after deploy.
- **execution.py**: direction now read from entry_detail (resolved per-tick) not hardcoded from strategy, so direction:both correctly places long or short.
- **Dashboard - Live Positions section**: new table showing open trades with unrealised P&L vs current price, SL/TP levels.
- **Dashboard - Agent Activity**: replaced "Recent log" (showing crash tracebacks) with filtered event feed showing only trades, reflections, errors. "No entry" spam filtered out.
- **Dashboard - trade history**: abandoned trades now show "abandoned" label in italic; conf column replaces RSI column (RSI was always "—" since field renamed).
- **Deploy from Windows**:
  ```
  cd C:\Users\nghil\Projects\Hermes\Hermes-Trading
  git add hermes_trading/loop.py hermes_trading/reflect.py hermes_trading/run.py hermes_trading/adapters/execution.py dashboard.py state/goal.yaml patch_strategies.py memory.md
  git commit -m "fix: goal.yaml; reconcile stale open trades; direction:both; min_indicators; dashboard live positions"
  git push origin master
  ```
- **Then on VPS**:
  ```
  cd /opt/trading/hermes-trading && git pull
  cp hermes_trading/loop.py /opt/trading/hermes_trading/hermes_trading/loop.py
  cp hermes_trading/reflect.py /opt/trading/hermes_trading/hermes_trading/reflect.py
  cp hermes_trading/run.py /opt/trading/hermes_trading/hermes_trading/run.py
  cp hermes_trading/adapters/execution.py /opt/trading/hermes_trading/hermes_trading/adapters/execution.py
  cp state/goal.yaml /opt/trading/hermes_trading/hermes_trading/state/goal.yaml
  cp patch_strategies.py /opt/trading/hermes_trading/hermes_trading/patch_strategies.py
  cd /opt/trading/hermes_trading/hermes_trading
  python3 patch_strategies.py
  pkill -f hermes_trading.run
  cd /opt/trading/hermes_trading && source venv/bin/activate
  nohup python3 -m hermes_trading.run >> logs/hermes.log 2>&1 &
  ```

### 2026-05-24 (session 2)
- Identified critical bug: loop.py called subprocess.run(["python", ...]) — `python` not in PATH on VPS, so all reflections silently failed. Fixed to sys.executable.
- Tightened run_hermes() LLM prompt: replaced inline variable list with newline-delimited list, added explicit "DO NOT use dot notation" rule, added concrete one-shot JSON example showing indicators[name].params.X format.
- Created setup_github_ssh.sh: generates ~/.ssh/id_ed25519_hermes, configures ~/.ssh/config, converts remote from HTTPS to SSH, prints public key for GitHub. Run once on VPS, then add key to GitHub to enable snapshot.sh push.
- Updated memory.md and key decisions table.
- git push blocked by stale index.lock in sandbox (Windows mount) — deploy instructions below.
- **Deploy from Windows PowerShell**:
  ```
  cd C:\Users\nghil\Projects\Hermes\Hermes-Trading
  git add hermes_trading/loop.py hermes_trading/reflect.py setup_github_ssh.sh memory.md
  git commit -m "fix: sys.executable in reflection subprocess; tighten LLM bracket notation; SSH setup"
  git push origin master
  ```
- **Then on VPS**:
  ```
  cd /opt/trading/hermes-trading && git pull
  cp hermes_trading/loop.py /opt/trading/hermes_trading/hermes_trading/loop.py
  cp hermes_trading/reflect.py /opt/trading/hermes_trading/hermes_trading/reflect.py
  cp setup_github_ssh.sh /opt/trading/hermes_trading/setup_github_ssh.sh
  # Restart agent:
  pkill -f hermes_trading.run
  cd /opt/trading/hermes_trading
  source venv/bin/activate
  nohup python3 -m hermes_trading.run >> logs/hermes.log 2>&1 &
  # Then set up GitHub SSH:
  bash /opt/trading/hermes_trading/setup_github_ssh.sh
  # Copy the printed key → GitHub Settings → SSH keys → New key (title: hermes-vps)
  # Verify: ssh -T git@github.com
  # Test snapshot: bash /opt/trading/hermes_trading/snapshot.sh
  ```

### 2026-05-24
- Read full codebase: run.py, loop.py, reflect.py, dashboard.py, adapters
- Found 3 reflect.py bugs: missing --state-dir, module-level globals, broken indicator key parsing
- Fixed reflect.py: added --state-dir arg, refactored all paths to use state_dir, fixed _set_nested/_get_nested for indicators[name].field notation, added win_rate to hypothesis, memory.md update on every reflection
- Found dashboard bugs: homebrew YAML parser, wrong goal key names, no win rate, no indicator weights panel
- Fixed dashboard.py: proper yaml.safe_load, correct nested goal parsing, win rate + indicator weights panel per asset, last-known-state fallback on SSH error, expanded log tail to 25 lines
- Created memory.md (this file) and state/memory.md template
- Pushed to GitHub, user pulled on VPS and restarted agent with venv activated
- All 4 workers confirmed live: BTC/USDT, ETH/USDT, SOL/USDT, TAO/USDT in live mode
- Enriched trade records: added indicators_snapshot (22 keys), indicators_fired, confidence_at_entry to loop.py and execution.py
- Built snapshot.sh: nightly git worktree commit of evolved strategies to state-snapshots branch
- Wired up Ollama local LLM: _call_llm() in reflect.py uses urllib.request to POST to localhost:11434
- Installed Ollama on VPS, pulled qwen2.5:3b (1.9GB), added HERMES_LLM_* env vars
- Verified first AI reflection: v02 -> v03, changed indicators.params.min_ratio 1.5 -> 3.0
- Initialised fallback reflection for eth/sol/tao: all at v03
- Fixed state/goal.yaml YAML parse error (missing # on line 1)
