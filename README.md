# Hermes-Trading

A self-improving live crypto trading agent for Bybit USDT-perp markets, with multi-indicator confidence scoring, structural SL/TP from market microstructure, and an LLM-driven reflection loop that mutates its own strategy.

Built as a Rogue Night consulting project. Currently runs live on BTC/USDT, ETH/USDT, SOL/USDT, TAO/USDT — 15-minute timeframe, exchange-side SL/TP, risk-based position sizing.

---

## Architecture

```
            ┌─────────────────────────────────────────┐
            │  run.py — async multi-worker bootstrap  │
            └────────────────┬────────────────────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
   ┌────▼────┐         ┌────▼────┐         ┌────▼────┐    ... per asset
   │BTC/USDT │         │ETH/USDT │         │SOL/USDT │    (4 workers)
   └────┬────┘         └─────────┘         └─────────┘
        │
        │  every 15-min candle:
        │
        ├─▶ adapters/price.py      → OHLCV + 22 indicators (RSI, EMA, BB,
        │                            MACD, ATR, VWAP, FVG, OB, S/R 1h+4h)
        │
        ├─▶ loop._evaluate_entry   → multi-indicator confidence score,
        │                            direction=long|short|both, gate checks
        │
        ├─▶ adapters/execution.py  → Bybit live order (HMAC auth) with
        │                            structural SL/TP, risk-based qty,
        │                            min_profit_usd $5 floor
        │
        ├─▶ loop._reconcile_open   → match local trades.jsonl to Bybit
        │                            positions; close + record pnl on fill
        │
        └─▶ reflect.py (every 5 closed trades)
            ├─ fallback: rule-based mutation (drawdown/return targets)
            └─ hermes:   Ollama qwen2.5:3b proposes ONE variable change
                         (writes hypothesis with decision_context +
                          trade_range + llm_raw_output for full audit)
```

State is per-asset under `state/<slug>/`: `strategy.yaml` (mutated by reflection), `trades.jsonl`, `hypotheses.jsonl`, `heartbeat.json`, `history/v<NN>.yaml` (version archive).

---

## Key features

- **Live trading on Bybit** via ccxt (HMAC). Paper mode supported via `HERMES_TRADING_MODE=paper`.
- **Structural SL/TP**: SL at swing low/high ± `sl_buffer_pct`, TP at nearest structural resistance/support. Falls back to fixed % only if S/R unavailable.
- **Risk-based sizing**: `qty = (balance × risk_per_trade) / sl_dist_pct`, capped at `MAX_POSITION_USD`.
- **Four structural guards** before every order:
  - `max_sl_pct` — skip if SL too far from entry (default 5%)
  - `min_tp_pct` — skip if structural TP too thin (default 3%, Option B target-return filter)
  - `min_rr_ratio` — soft: extend TP if R:R below threshold (default 2.0)
  - `min_profit_usd` — hard: skip if expected TP profit < $5
- **Hermes reflection loop**: every 5 closed trades, the bot calls a local Ollama model (qwen2.5:3b, CPU) which reads recent trade history + `decision_context` and proposes one variable to mutate. Every hypothesis is logged with the LLM's raw output for audit.
- **Audit fields on every trade**: `confidence_breakdown` (per-indicator fire/weight), `entry_gates` (snapshot at decision time), `evaluation_summary` (human-readable), `close_reason` (TP_hit / SL_hit / abandoned / manual_or_other).
- **Local dashboard** at `localhost:8888` with $ and % P&L, per-asset cards, indicator-weight panels, live position R:R.

---

## Project layout

```
hermes-trading/
├── hermes_trading/
│   ├── run.py                    # async worker bootstrap, per-asset state init
│   ├── loop.py                   # 15m tick loop, entry eval, reconcile, reflection cadence
│   ├── reflect.py                # --fallback (rule-based) | --hermes (Ollama)
│   └── adapters/
│       ├── price.py              # OHLCV + 22 indicators (15m, 1h, 4h)
│       └── execution.py          # Bybit HMAC live orders, structural SL/TP, guards
├── tools/                        # operational scripts
│   ├── backfill_trades.py        # Bybit closed-pnl → trades.jsonl (7-day window)
│   ├── import_bybit_csv.py       # Bybit UI CSV export → trades.jsonl
│   ├── recompute_pnl_pct.py      # direction-correct pnl_pct repair
│   └── purge_abandoned.py        # clean entry==exit stub records
├── state/<slug>/                 # per-asset runtime state (gitignored)
├── dashboard.py                  # local Windows dashboard, SSH-fetches VPS state
├── memory.md                     # project memory (single source of truth)
├── next-session-prompt.md        # handoff prompt for next agent session
├── strategy.yaml                 # legacy top-level (per-asset under state/<slug>/ is canonical)
├── pyproject.toml
└── requirements.txt
```

---

## Quick start (VPS, live mode)

```bash
# On VPS
cd /opt/trading
git clone https://github.com/nghilinh-alt/hermes-trading.git
cd hermes-trading
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# .env (required for live mode)
cat > .env <<'EOF'
HERMES_TRADING_MODE=live
HERMES_TRADING_I_ACCEPT_RISK=true
BYBIT_API_KEY=...
BYBIT_API_SECRET=...
MAX_POSITION_USD=500
HERMES_LLM_TIMEOUT=300
HERMES_LLM_NUM_CTX=8192
EOF

# Run
set -a && source .env && set +a
nohup .venv/bin/python -m hermes_trading.run >> bot.log 2>&1 &
disown
```

For local dashboard:

```powershell
# On Windows
cd C:\Users\nghil\Projects\Hermes\Hermes-Trading
python dashboard.py
# Open http://localhost:8888
```

SSH key setup for dashboard is documented in `memory.md` (session 5f).

---

## Operational protocol

1. **Read `memory.md` first** — full project history, key decisions, VPS layout, doctrine items.
2. **Update `memory.md` at end of every session** — Last Updated, Active state, Session Log entry, Key Decisions, Handoffs.
3. **Deploy is split across two VPS clones**: `hermes-trading/` (hyphen, git pull staging) and `hermes_trading/` (underscore, running). After every `git pull`, `cp` the changed files from hyphen → underscore. One deploy step per code block.
4. **Never `cp` `strategy.yaml`** between clones — they may carry reflection-evolved values. Use in-place edits.
5. **After any multi-file deploy: `diff -r hyphen underscore`** for the package dir. A partial cp is invisible until something triggers the missing piece (doctrine #14).

Full doctrine list (16 items as of 2026-06-01) lives in `memory.md` and `next-session-prompt.md`.

---

## License

Private — Rogue Night consulting project.

## Risk disclaimer

Live trading. Real capital at risk. The `HERMES_TRADING_I_ACCEPT_RISK=true` flag is required to enable order placement.
