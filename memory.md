# Hermes-Trading — Project Memory
_Rogue Night consulting project. Updated at end of each session._

## Last Updated
2026-05-24

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
- `reflect.py` — `--fallback` (rule-based) or `--hermes` (AI via CLI); changes exactly ONE variable per reflection
- `dashboard.py` — local Windows dashboard at localhost:8888, SSH-fetches VPS state

## Active State
- Strategy version: v02 (on VPS)
- Trading mode: live
- Reflection cadence: every 5 closed trades
- Per-asset state dirs: `state/btc_usdt/`, `state/eth_usdt/`, `state/sol_usdt/`, `state/tao_usdt/`
- Each asset dir contains: `strategy.yaml`, `trades.jsonl`, `hypotheses.jsonl`, `heartbeat.json`, `history/`, `memory.md`

## Key Decisions
| Date       | Decision | Rationale |
|------------|----------|-----------|
| 2026-05-24 | Added per-asset state dirs | Scale to 4 assets independently |
| 2026-05-24 | Reflection reads/writes per-asset memory.md | Hermes agent learns from its own history per asset |
| 2026-05-24 | reflect.py refactored — all paths via --state-dir arg | Fixes silent reflection failure in loop.py |
| 2026-05-24 | dashboard.py uses yaml.safe_load for YAML parsing | Homebrew flat-key parser was missing nested goal values |

## Known Issues / TODOs
- state/strategy.yaml in root is legacy — per-asset dirs under state/{asset_slug}/ are the source of truth on VPS
- VPS has stale state/hypotheses.jsonl and state/trades.jsonl at root level (old pre-per-asset structure) — safe to ignore, not read by loop.py
- Dashboard requires SSH key auth to VPS; shows last-known-state banner when unreachable
- Always activate venv before running agent: `source venv/bin/activate`

## Handoffs
- Agent is live ✓ — next session: check trade count and first reflection firing
- Handoff to: Linh (monitor dashboard, check state/btc_usdt/memory.md after 5 trades)

## Session Log
### 2026-05-24
- Read full codebase: run.py, loop.py, reflect.py, dashboard.py, adapters
- Found 3 reflect.py bugs: missing --state-dir, module-level globals, broken indicator key parsing
- Fixed reflect.py: added --state-dir arg, refactored all paths to use state_dir, fixed _set_nested/_get_nested for indicators[name].field notation, added win_rate to hypothesis, memory.md update on every reflection
- Found dashboard bugs: homebrew YAML parser, wrong goal key names, no win rate, no indicator weights panel
- Fixed dashboard.py: proper yaml.safe_load, correct nested goal parsing, win rate + indicator weights panel per asset, last-known-state fallback on SSH error, expanded log tail to 25 lines
- Created memory.md (this file) and state/memory.md template
- Pushed to GitHub, user pulled on VPS and restarted agent with venv activated
- All 4 workers confirmed live: BTC/USDT, ETH/USDT, SOL/USDT, TAO/USDT in live mode
