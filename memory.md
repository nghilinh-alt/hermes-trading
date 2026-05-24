# Hermes-Trading — Project Memory
_Rogue Night consulting project. Updated at end of each session._

## Last Updated
2026-05-24 (session 2)

## Project Overview
Self-improving live crypto trading agent running on VPS (root@187.127.108.173).
- **VPS path**: `/opt/trading/hermes_trading`
- **Repo**: https://github.com/nghilinh-alt/hermes-trading.git (master branch)
- **Local path**: `C:\Users\nghil\Projects\Hermes\Hermes-Trading`
- **Trading mode**: Live on Bybit, HMAC key auth
- **Assets**: BTC/USDT, ETH/USDT, SOL/USDT, TAO/USDT
- **VPS Python**: uses `venv/` — always `source venv/bin/activate`

## Architecture Summary
- `run.py` — entrypoint, bootstraps per-asset state dirs under `state/{asset_slug}/`
- `loop.py` — async 5-min tick loop; fetches 4 adapters, evaluates strategy, trades, triggers reflection every N trades
- `adapters/price.py` — 5m + 1h + 4h OHLCV; computes RSI, EMA, BB, MACD, ATR, VWAP, FVG, OB, S/R
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

## Known Issues / TODOs
- VPS running code at `/opt/trading/hermes_trading/hermes_trading/` (nested) — when deploying new code, SCP to this path OR copy from `/opt/trading/hermes-trading/` after git pull
- state/strategy.yaml in root is legacy — per-asset dirs under state/{asset_slug}/ are the source of truth on VPS
- VPS has stale state/hypotheses.jsonl and state/trades.jsonl at root level (old pre-per-asset structure) — safe to ignore
- Dashboard requires SSH key auth to VPS; shows last-known-state banner when unreachable
- GitHub SSH key on VPS not yet set up — snapshot.sh push fails silently; run setup_github_ssh.sh then add public key to GitHub
- [FIXED session 2] LLM bracket notation: prompt now has explicit newline-delimited variable list + one-shot JSON example; `python` → `sys.executable` in reflection subprocess

## Handoffs
- Agent is live ✓, Ollama reflection working ✓
- **Pending deploy**: push loop.py + reflect.py fixes to VPS (see Session Log for commands)
- **Pending**: run setup_github_ssh.sh on VPS, add public key to GitHub → enables snapshot.sh
- Next check: after 5 trades close on any asset, tail state/{asset}/hypotheses.jsonl — confirm changed_variable uses bracket notation e.g. `indicators[rsi].params.threshold` not `indicators.params.threshold`
- Handoff to: Linh (deploy via Windows terminal, monitor dashboard)

## Session Log
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
