"""
dashboard.py — local Hermes Trading dashboard
Pulls live state from VPS via SSH, serves at http://localhost:8888
Auto-refreshes every 60 seconds.

Usage:
  python dashboard.py

Requires SSH key auth to the VPS (no password prompts).
"""
import http.server
import json
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

AEST = ZoneInfo("Australia/Sydney")

VPS        = "root@187.127.108.173"
VPS_BASE   = "/opt/trading/hermes_trading"
ASSETS     = ["btc_usdt", "eth_usdt", "sol_usdt", "tao_usdt"]
ASSET_LABELS = {
    "btc_usdt": "BTC/USDT",
    "eth_usdt": "ETH/USDT",
    "sol_usdt": "SOL/USDT",
    "tao_usdt": "TAO/USDT",
}
LOCAL_STATE = Path("state")
PORT        = 8888
POLL_SECS   = 60


# ── SSH helpers ────────────────────────────────────────────────────────────────

def _ssh_batch(files: list[str]) -> dict[str, str]:
    """Fetch multiple remote files in a single SSH connection. Returns {} on any error."""
    SEP   = "---HERMES_SEP---"
    parts = [f"echo '{SEP}{path}'; cat {path} 2>/dev/null; echo '{SEP}END'" for path in files]
    script = "; ".join(parts)
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", VPS, script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        output = result.stdout if result.returncode == 0 else ""
    except Exception:
        return {}

    parsed: dict[str, str] = {}
    current_path, current_lines = None, []
    for line in output.splitlines():
        if line.startswith(SEP) and not line.endswith("END"):
            current_path  = line[len(SEP):]
            current_lines = []
        elif line == f"{SEP}END" and current_path:
            parsed[current_path] = "\n".join(current_lines).strip()
            current_path = None
        elif current_path is not None:
            current_lines.append(line)
    return parsed


def _read_file(remote_path: str, local_fallback: Path | None, cache: dict) -> str:
    if remote_path in cache:
        return cache[remote_path]
    if local_fallback and local_fallback.exists():
        return local_fallback.read_text()
    return ""


# ── Parsing (real YAML, not homebrew) ─────────────────────────────────────────

def _parse_yaml(text: str) -> dict:
    if not text:
        return {}
    try:
        return yaml.safe_load(text) or {}
    except Exception:
        return {}


def _parse_heartbeat(text: str) -> dict:
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {}


# ── Data fetch ─────────────────────────────────────────────────────────────────

def _fetch_data(last_known: dict | None = None) -> dict:
    """Pull all state from VPS. Falls back to last_known on SSH failure."""
    data: dict = {
        "updated":  datetime.now(AEST).strftime("%H:%M:%S AEST"),
        "assets":   {},
        "log_tail": [],
        "goal":     {},
        "source":   "vps",
        "ssh_ok":   True,
    }

    all_files = [f"{VPS_BASE}/state/goal.yaml", f"{VPS_BASE}/logs/hermes.log"]
    for slug in ASSETS:
        sd = f"{VPS_BASE}/state/{slug}"
        all_files += [
            f"{sd}/trades.jsonl",
            f"{sd}/strategy.yaml",
            f"{sd}/heartbeat.json",
            f"{sd}/hypotheses.jsonl",
        ]
    cache = _ssh_batch(all_files)

    if not cache:
        data["ssh_ok"] = False
        data["source"] = "stale"
        if last_known:
            data.update({k: last_known[k] for k in ("assets", "goal", "log_tail") if k in last_known})
            data["updated"] = last_known.get("updated", "—") + " (stale — VPS unreachable)"
        return data

    # ── Goal ──────────────────────────────────────────────────────────────────
    goal_raw  = _read_file(f"{VPS_BASE}/state/goal.yaml", LOCAL_STATE / "goal.yaml", cache)
    goal_yaml = _parse_yaml(goal_raw)

    # Support both flat and nested goal.yaml layouts
    obj  = goal_yaml.get("objective", {})
    risk = goal_yaml.get("risk", {})
    data["goal"] = {
        "target":     f"{obj.get('target_value', goal_yaml.get('target_return_30d', 25))}%",
        "drawdown":   f"{risk.get('stop_loss_pct', goal_yaml.get('max_drawdown_pct', 5))}%",
        "timeframe":  goal_yaml.get("timeframe", "5m"),
        "reflection": str(goal_yaml.get("reflection_every", 5)),
        "mode":       goal_yaml.get("mode", "paper"),
    }

    # ── Per-asset ─────────────────────────────────────────────────────────────
    for slug in ASSETS:
        sd = f"{VPS_BASE}/state/{slug}"
        ld = LOCAL_STATE / slug

        trades_text   = _read_file(f"{sd}/trades.jsonl",    None, cache)
        strategy_text = _read_file(f"{sd}/strategy.yaml",   (ld / "strategy.yaml") if ld.exists() else None, cache)
        hb_text       = _read_file(f"{sd}/heartbeat.json",  None, cache)
        hyp_text      = _read_file(f"{sd}/hypotheses.jsonl", None, cache)

        all_trades: list[dict] = []
        for line in trades_text.splitlines():
            line = line.strip()
            if line:
                try:
                    all_trades.append(json.loads(line))
                except Exception:
                    pass

        strategy  = _parse_yaml(strategy_text)
        heartbeat = _parse_heartbeat(hb_text)

        hypotheses: list[dict] = []
        for line in hyp_text.splitlines():
            line = line.strip()
            if line:
                try:
                    hypotheses.append(json.loads(line))
                except Exception:
                    pass

        closed   = [t for t in all_trades if t.get("pnl_pct") is not None]
        win_rate = (
            sum(1 for t in closed if float(t["pnl_pct"]) > 0) / len(closed)
            if closed else None
        )
        total_pnl = sum(float(t["pnl_pct"]) for t in closed) * 100 if closed else None

        indicators = [
            {
                "name":     i.get("name", ""),
                "weight":   float(i.get("weight", 1.0)),
                "required": bool(i.get("required", False)),
                "params":   i.get("params", {}),
            }
            for i in strategy.get("indicators", [])
        ]

        data["assets"][slug] = {
            "label":        ASSET_LABELS[slug],
            "trade_count":  len(all_trades),
            "last_trade":   all_trades[-1] if all_trades else {},
            "trades":       all_trades,
            "strategy_ver": str(strategy.get("version", "—")),
            "indicators":   indicators,
            "stop_loss":    strategy.get("stop_loss_pct", "—"),
            "pos_size":     strategy.get("position_size_r", "—"),
            "min_conf":     strategy.get("entry", {}).get("min_confidence", "—"),
            "hb_status":    heartbeat.get("status", "unknown"),
            "hb_failures":  int(heartbeat.get("consecutive_failures", 0)),
            "last_tick":    heartbeat.get("last_tick", "—"),
            "hypotheses":   hypotheses,
            "win_rate":     win_rate,
            "total_pnl":    total_pnl,
        }

    # ── Log ───────────────────────────────────────────────────────────────────
    import re as _re
    log_text = cache.get(f"{VPS_BASE}/logs/hermes.log", "")
    lines    = [l for l in log_text.splitlines() if l.strip()]
    data["log_tail"] = lines[-25:]

    # Extract latest price per asset from log
    latest_prices: dict[str, float] = {}
    for line in reversed(lines):
        for slug, label in ASSET_LABELS.items():
            if label in line and slug not in latest_prices:
                m = _re.search(r'price=([\d.]+)', line)
                if m:
                    latest_prices[slug] = float(m.group(1))
                m2 = _re.search(r'@ ([\d.]+)', line)
                if m2 and slug not in latest_prices:
                    latest_prices[slug] = float(m2.group(1))
    for slug in ASSETS:
        if slug in data["assets"]:
            data["assets"][slug]["current_price"] = latest_prices.get(slug)

    return data


# ── HTML rendering ─────────────────────────────────────────────────────────────

def _render_html(d: dict) -> str:
    goal    = d.get("goal", {})
    assets  = d.get("assets", {})
    logs    = d.get("log_tail", [])
    updated = d.get("updated", "—")
    mode    = goal.get("mode", "paper")
    ssh_ok  = d.get("ssh_ok", True)

    # helpers
    def _pnl_col(val: float) -> str:
        return "#1D9E75" if val >= 0 else "#E24B4A"

    def _pnl_chip(val: float, decimals: int = 2) -> str:
        sign = "+" if val >= 0 else ""
        return f"<span style='color:{_pnl_col(val)};font-weight:500'>{sign}{val:.{decimals}f}%</span>"

    def _convert_log_utc(line: str) -> str:
        import re
        m = re.match(r'^(\d{2}:\d{2}) UTC (.+)', line)
        if not m:
            return line
        try:
            now_utc = datetime.now(timezone.utc)
            t = datetime.strptime(m.group(1), "%H:%M").replace(
                year=now_utc.year, month=now_utc.month, day=now_utc.day, tzinfo=timezone.utc,
            )
            return f"{t.astimezone(AEST).strftime('%H:%M')} AEST {m.group(2)}"
        except Exception:
            return line

    def _log_line(line: str) -> str:
        line = _convert_log_utc(line)
        col  = ""
        if "Trade #" in line:
            col = "color:#1D9E75"
        elif "error" in line.lower():
            col = "color:#E24B4A"
        elif "reflect" in line.lower() or "Reflection" in line:
            col = "color:#EF9F27"
        safe = line.replace("<", "&lt;").replace(">", "&gt;")
        return f"<div style='font-size:11px;line-height:1.9;{col}'>{safe}</div>"

    # SSH warning banner
    ssh_banner = ""
    if not ssh_ok:
        ssh_banner = (
            "<div style='background:rgba(226,75,74,0.1);border:0.5px solid #E24B4A;"
            "border-radius:8px;padding:10px 14px;margin-bottom:1rem;font-size:12px;color:#E24B4A'>"
            "&#9888; VPS unreachable — showing last known state</div>"
        )

    # top stats
    total_trades = sum(assets.get(s, {}).get("trade_count", 0) for s in ASSETS)
    top_stats = f"""
<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:1.5rem">
  <div style="background:var(--surface);border:0.5px solid var(--border);border-radius:8px;padding:12px">
    <div style="font-size:11px;color:var(--muted);margin-bottom:4px">target / 30d</div>
    <div style="font-size:20px;font-weight:500">{goal.get("target", "—")}</div>
  </div>
  <div style="background:var(--surface);border:0.5px solid var(--border);border-radius:8px;padding:12px">
    <div style="font-size:11px;color:var(--muted);margin-bottom:4px">max drawdown</div>
    <div style="font-size:20px;font-weight:500">{goal.get("drawdown", "—")}</div>
  </div>
  <div style="background:var(--surface);border:0.5px solid var(--border);border-radius:8px;padding:12px">
    <div style="font-size:11px;color:var(--muted);margin-bottom:4px">total trades</div>
    <div style="font-size:20px;font-weight:500">{total_trades}</div>
  </div>
  <div style="background:var(--surface);border:0.5px solid var(--border);border-radius:8px;padding:12px">
    <div style="font-size:11px;color:var(--muted);margin-bottom:4px">reflect every</div>
    <div style="font-size:20px;font-weight:500">{goal.get("reflection", "5")} trades</div>
  </div>
</div>"""

    # P&L summary
    def _cutoff(days: int) -> str:
        aest_today = datetime.now(AEST).date()
        midnight   = datetime(aest_today.year, aest_today.month, aest_today.day, tzinfo=AEST) - timedelta(days=days)
        return midnight.astimezone(timezone.utc).isoformat()

    all_trades_flat = [t for s in ASSETS for t in assets.get(s, {}).get("trades", [])]

    def _period_pnl(since_iso: str) -> float:
        return sum(
            float(t["pnl_pct"])
            for t in all_trades_flat
            if t.get("pnl_pct") is not None and t.get("ts", "") >= since_iso
        ) * 100

    agg = {
        "day":   _period_pnl(_cutoff(1)),
        "week":  _period_pnl(_cutoff(7)),
        "month": _period_pnl(_cutoff(30)),
        "all":   _period_pnl(""),
    }
    pnl_summary = f"""
<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:1.5rem">
  <div style="background:var(--surface);border:0.5px solid var(--border);border-radius:8px;padding:12px">
    <div style="font-size:11px;color:var(--muted);margin-bottom:4px">today P&L</div>
    <div style="font-size:20px;font-weight:500">{_pnl_chip(agg["day"])}</div>
  </div>
  <div style="background:var(--surface);border:0.5px solid var(--border);border-radius:8px;padding:12px">
    <div style="font-size:11px;color:var(--muted);margin-bottom:4px">7-day P&L</div>
    <div style="font-size:20px;font-weight:500">{_pnl_chip(agg["week"])}</div>
  </div>
  <div style="background:var(--surface);border:0.5px solid var(--border);border-radius:8px;padding:12px">
    <div style="font-size:11px;color:var(--muted);margin-bottom:4px">30-day P&L</div>
    <div style="font-size:20px;font-weight:500">{_pnl_chip(agg["month"])}</div>
  </div>
  <div style="background:var(--surface);border:0.5px solid var(--border);border-radius:8px;padding:12px">
    <div style="font-size:11px;color:var(--muted);margin-bottom:4px">all-time P&L</div>
    <div style="font-size:20px;font-weight:500">{_pnl_chip(agg["all"])}</div>
  </div>
</div>"""

    # asset cards
    def _status_dot(status: str, failures: int) -> str:
        if failures >= 3:
            return "<span style='color:#E24B4A' title='circuit breaker tripped'>&#9679;</span>"
        cols = {"ok": "#1D9E75", "error": "#E24B4A"}
        col = cols.get(status, "#888")
        return f"<span style='color:{col}'>&#9679;</span>"

    def _position_badge(last_trade: dict, cur_price) -> str:
        if not last_trade or last_trade.get("pnl_pct") is not None:
            return ""
        entry = last_trade.get("entry_price")
        col   = "#1D9E75" if (entry and cur_price and float(cur_price) >= float(entry)) else "#E24B4A"
        return f"<span style='font-size:9px;padding:1px 5px;border-radius:3px;background:rgba(0,0,0,0.06);color:{col}'>IN TRADE</span>"

    def _indicator_bars(indicators: list[dict]) -> str:
        if not indicators:
            return ""
        rows = []
        for ind in indicators:
            name = ind.get("name", "")
            w    = float(ind.get("weight", 0))
            req  = ind.get("required", False)
            col  = "#EF9F27" if req else ("#1D9E75" if w > 0 else "#555")
            tag  = "★" if req else ""
            bw   = max(3, int(w * 42))
            rows.append(
                f"<div style='display:flex;align-items:center;gap:5px;margin-bottom:2px'>"
                f"<span style='font-size:9px;width:70px;color:var(--muted);overflow:hidden;white-space:nowrap'>{tag}{name}</span>"
                f"<div style='height:4px;width:{bw}px;background:{col};border-radius:2px;flex-shrink:0'></div>"
                f"<span style='font-size:9px;color:var(--muted)'>{w}</span>"
                f"</div>"
            )
        return "<div style='margin-top:10px;border-top:0.5px solid var(--border);padding-top:8px'>" + "".join(rows) + "</div>"

    asset_cards = ""
    for slug in ASSETS:
        a      = assets.get(slug, {})
        t      = a.get("last_trade", {})
        ver    = a.get("strategy_ver", "—")
        count  = a.get("trade_count", 0)
        status = a.get("hb_status", "unknown")
        fails  = a.get("hb_failures", 0)
        win_r  = a.get("win_rate")
        tot    = a.get("total_pnl")
        cur    = a.get("current_price")

        cur_str  = f"${float(cur):,.2f}" if cur else "—"
        win_str  = f"{win_r * 100:.0f}%" if win_r is not None else "—"
        tot_col  = _pnl_col(tot) if tot is not None else "inherit"
        tot_str  = (f"{'+' if tot >= 0 else ''}{tot:.2f}%") if tot is not None else "—"

        last_pnl = t.get("pnl_pct")
        if last_pnl is not None:
            lp_col = _pnl_col(float(last_pnl))
            lp_str = f"{'+' if float(last_pnl) >= 0 else ''}{float(last_pnl)*100:.3f}%"
        else:
            lp_col, lp_str = "var(--muted)", ("open" if t else "—")

        ep      = t.get("entry_price")
        ep_str  = f"${float(ep):,.2f}" if ep else "—"

        lt_raw  = a.get("last_tick", "—")
        try:
            lt_str = datetime.fromisoformat(lt_raw).astimezone(AEST).strftime("%H:%M")
        except Exception:
            lt_str = "—"

        ind_bars = _indicator_bars(a.get("indicators", []))

        asset_cards += f"""
        <div style="background:var(--bg);border:0.5px solid var(--border);border-radius:12px;padding:1rem">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <div style="display:flex;align-items:center;gap:6px">
              {_status_dot(status, fails)}
              <span style="font-size:13px;font-weight:500">{a.get("label", slug)}</span>
              {_position_badge(t, cur)}
            </div>
            <span style="font-size:11px;color:var(--muted)">v{ver}</span>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:3px 0">
            <span style="font-size:11px;color:var(--muted)">price</span>
            <span style="font-size:12px;font-weight:500;text-align:right">{cur_str}</span>
            <span style="font-size:11px;color:var(--muted)">trades</span>
            <span style="font-size:12px;font-weight:500;text-align:right">{count}</span>
            <span style="font-size:11px;color:var(--muted)">win rate</span>
            <span style="font-size:12px;font-weight:500;text-align:right">{win_str}</span>
            <span style="font-size:11px;color:var(--muted)">total P&L</span>
            <span style="font-size:12px;font-weight:500;text-align:right;color:{tot_col}">{tot_str}</span>
            <span style="font-size:11px;color:var(--muted)">last entry</span>
            <span style="font-size:12px;text-align:right">{ep_str}</span>
            <span style="font-size:11px;color:var(--muted)">last P&L</span>
            <span style="font-size:12px;text-align:right;color:{lp_col}">{lp_str}</span>
            <span style="font-size:11px;color:var(--muted)">last tick</span>
            <span style="font-size:12px;text-align:right;color:var(--muted)">{lt_str}</span>
          </div>
          {ind_bars}
        </div>"""

    # ── Live positions ────────────────────────────────────────────────────────
    open_trades = [
        {**t, "_slug": s}
        for s in ASSETS
        for t in assets.get(s, {}).get("trades", [])
        if t.get("pnl_pct") is None and not t.get("abandoned")
    ]
    open_trades.sort(key=lambda t: t.get("ts", ""), reverse=True)

    def _unrealised(t: dict) -> str:
        slug      = t.get("_slug", "")
        cur_price = assets.get(slug, {}).get("current_price")
        entry     = t.get("entry_price")
        if not cur_price or not entry:
            return "—"
        direction = t.get("direction", "long")
        move = (float(cur_price) - float(entry)) / float(entry)
        pct  = move if direction == "long" else -move
        col  = _pnl_col(pct * 100)
        sign = "+" if pct >= 0 else ""
        return f"<span style='color:{col};font-weight:500'>{sign}{pct*100:.2f}%</span>"

    def _open_trade_row(t: dict) -> str:
        ts_raw  = t.get("ts", "")
        try:
            ts_s = datetime.fromisoformat(ts_raw).astimezone(AEST).strftime("%m-%d %H:%M")
        except Exception:
            ts_s = ts_raw[:16].replace("T", " ")
        asset_s = t.get("asset", "—")
        dirn    = t.get("direction", "long")
        dc      = "#1D9E75" if dirn == "long" else "#E24B4A"
        entry   = t.get("entry_price", 0)
        slug    = t.get("_slug", "")
        cur     = assets.get(slug, {}).get("current_price")
        cur_s   = f"${float(cur):,.2f}" if cur else "—"
        sl      = t.get("sl_price")
        tp      = t.get("tp_price")
        sl_s    = f"${float(sl):,.2f}" if sl else "—"
        tp_s    = f"${float(tp):,.2f}" if tp else "—"
        unr     = _unrealised(t)
        return (
            f"<tr style='border-top:0.5px solid var(--border)'>"
            f"<td style='padding:5px 8px;font-size:11px;color:var(--muted)'>{ts_s}</td>"
            f"<td style='padding:5px 8px;font-size:12px;font-weight:500'>{asset_s}</td>"
            f"<td style='padding:5px 8px;font-size:11px;color:{dc};font-weight:500'>{dirn}</td>"
            f"<td style='padding:5px 8px;font-size:11px'>${float(entry):,.2f}</td>"
            f"<td style='padding:5px 8px;font-size:11px'>{cur_s}</td>"
            f"<td style='padding:5px 8px;font-size:12px'>{unr}</td>"
            f"<td style='padding:5px 8px;font-size:11px;color:#E24B4A'>{sl_s}</td>"
            f"<td style='padding:5px 8px;font-size:11px;color:#1D9E75'>{tp_s}</td>"
            f"</tr>"
        )

    if open_trades:
        positions_html = f"""
<div style="background:var(--bg);border:0.5px solid var(--border);border-radius:8px;overflow:auto;margin-bottom:1.5rem">
  <table style="width:100%;border-collapse:collapse;white-space:nowrap">
    <thead>
      <tr style="background:var(--surface)">
        <th style="padding:7px 8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">opened (AEST)</th>
        <th style="padding:7px 8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">asset</th>
        <th style="padding:7px 8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">side</th>
        <th style="padding:7px 8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">entry</th>
        <th style="padding:7px 8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">mark</th>
        <th style="padding:7px 8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">unreal P&L</th>
        <th style="padding:7px 8px;font-size:11px;font-weight:500;color:#E24B4A;text-align:left">stop loss</th>
        <th style="padding:7px 8px;font-size:11px;font-weight:500;color:#1D9E75;text-align:left">take profit</th>
      </tr>
    </thead>
    <tbody>{"".join(_open_trade_row(t) for t in open_trades)}</tbody>
  </table>
</div>"""
    else:
        positions_html = "<div style='font-size:12px;color:var(--muted);margin-bottom:1.5rem'>No open positions.</div>"

    # ── Trade history table ───────────────────────────────────────────────────
    recent_trades = sorted(all_trades_flat, key=lambda t: t.get("ts", ""), reverse=True)[:50]

    # Short display names for indicators — keeps chips compact
    _IND_SHORT = {
        "rsi": "RSI", "ema_trend": "EMA", "macd": "MACD", "vwap": "VWAP",
        "volume_spike": "VOL", "bb_squeeze": "BB", "fvg": "FVG",
        "order_block": "OB", "sr_zone": "SR",
    }

    # Snapshot keys to show in the tooltip, with short labels
    _SNAP_LABELS = [
        ("rsi_14",        "RSI"),
        ("macd_line",     "MACD"),
        ("macd_hist",     "Hist"),
        ("vwap",          "VWAP"),
        ("volume_ratio",  "VolR"),
        ("ema_50",        "EMA50"),
        ("bb_upper",      "BB↑"),
        ("bb_lower",      "BB↓"),
        ("atr_14",        "ATR"),
        ("support_1h4h",  "Sup"),
        ("resistance_1h4h","Res"),
    ]

    def _signals_cell(t: dict) -> str:
        fired    = t.get("indicators_fired") or {}
        snapshot = t.get("indicators_snapshot") or {}

        if not fired:
            return "<td style='padding:5px 8px;font-size:11px;color:var(--muted)'>—</td>"

        # Build hover tooltip from snapshot values
        tip_parts = []
        for key, label in _SNAP_LABELS:
            val = snapshot.get(key)
            if val is not None:
                try:
                    tip_parts.append(f"{label}:{float(val):.2f}")
                except Exception:
                    pass
        tooltip = "  |  ".join(tip_parts) if tip_parts else ""

        # Build chips — green for fired, dim for not fired, skip None (no data)
        chips = []
        for name, result in sorted(fired.items()):
            if result is None:
                continue
            short = _IND_SHORT.get(name, name[:3].upper())
            if result:
                chips.append(
                    f"<span style='display:inline-block;padding:1px 5px;margin:1px;border-radius:3px;"
                    f"background:rgba(29,158,117,0.15);color:#1D9E75;font-size:10px'>{short}</span>"
                )
            else:
                chips.append(
                    f"<span style='display:inline-block;padding:1px 5px;margin:1px;border-radius:3px;"
                    f"background:rgba(0,0,0,0.04);color:var(--muted);font-size:10px'>{short}</span>"
                )

        chips_html = "".join(chips) if chips else "<span style='color:var(--muted);font-size:11px'>—</span>"
        title_attr = f" title='{tooltip}'" if tooltip else ""
        return f"<td style='padding:5px 8px'{title_attr}>{chips_html}</td>"

    def _trade_row(t: dict) -> str:
        ts_raw = t.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_raw).astimezone(AEST).strftime("%m-%d %H:%M")
        except Exception:
            ts = ts_raw[:16].replace("T", " ")
        asset_l   = t.get("asset", "—")
        dirn      = t.get("direction", "—")
        dc        = "#1D9E75" if dirn == "long" else "#E24B4A"
        entry     = t.get("entry_price", 0)
        exitp     = t.get("exit_price")
        abandoned = t.get("abandoned", False)
        pnl_r     = t.get("pnl_pct")
        if abandoned:
            exit_s = "<span style='color:var(--muted);font-style:italic'>abandoned</span>"
            ps, pc = "—", "var(--muted)"
        elif pnl_r is not None:
            pv     = float(pnl_r) * 100
            ps     = f"{'+' if pv >= 0 else ''}{pv:.3f}%"
            pc     = _pnl_col(pv)
            exit_s = f"${float(exitp):,.2f}" if exitp is not None else "—"
        else:
            exit_s = "<span style='color:#EF9F27;font-weight:500'>open</span>"
            ps, pc = "open", "#EF9F27"
        conf = t.get("confidence_at_entry")
        cs   = f"{float(conf)*100:.0f}%" if conf is not None else "—"
        ver  = t.get("strategy_version", "—")
        return (
            f"<tr style='border-top:0.5px solid var(--border)'>"
            f"<td style='padding:5px 8px;font-size:11px;color:var(--muted)'>{ts}</td>"
            f"<td style='padding:5px 8px;font-size:11px'>{asset_l}</td>"
            f"<td style='padding:5px 8px;font-size:11px;color:{dc}'>{dirn}</td>"
            f"<td style='padding:5px 8px;font-size:11px'>${float(entry):,.2f}</td>"
            f"<td style='padding:5px 8px;font-size:11px;color:var(--muted)'>{exit_s}</td>"
            f"<td style='padding:5px 8px;font-size:11px;color:var(--muted)'>{cs}</td>"
            + _signals_cell(t) +
            f"<td style='padding:5px 8px;font-size:12px;font-weight:500;color:{pc}'>{ps}</td>"
            f"<td style='padding:5px 8px;font-size:11px;color:var(--muted)'>v{ver}</td>"
            f"</tr>"
        )

    if recent_trades:
        trade_table = f"""
<div style="background:var(--bg);border:0.5px solid var(--border);border-radius:8px;overflow:auto;margin-bottom:1.5rem">
  <table style="width:100%;border-collapse:collapse;white-space:nowrap">
    <thead>
      <tr style="background:var(--surface)">
        <th style="padding:7px 8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">time (AEST)</th>
        <th style="padding:7px 8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">asset</th>
        <th style="padding:7px 8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">side</th>
        <th style="padding:7px 8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">entry</th>
        <th style="padding:7px 8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">exit</th>
        <th style="padding:7px 8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">conf</th>
        <th style="padding:7px 8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">signals fired</th>
        <th style="padding:7px 8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">P&L</th>
        <th style="padding:7px 8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">strat</th>
      </tr>
    </thead>
    <tbody>{"".join(_trade_row(t) for t in recent_trades)}</tbody>
  </table>
</div>"""
    else:
        trade_table = "<div style='font-size:12px;color:var(--muted);margin-bottom:1.5rem'>No trades yet.</div>"

    # reflection cards
    all_reflections = []
    for slug in ASSETS:
        for h in assets.get(slug, {}).get("hypotheses", []):
            all_reflections.append({**h, "_asset": ASSET_LABELS.get(slug, slug)})
    all_reflections.sort(key=lambda x: x.get("ts", ""), reverse=True)

    def _refl_card(h: dict) -> str:
        asset  = h.get("_asset", "")
        ts_raw = h.get("ts", "")
        try:
            tsd = datetime.fromisoformat(ts_raw).astimezone(AEST).strftime("%Y-%m-%d %H:%M") if ts_raw else "—"
        except Exception:
            tsd = ts_raw[:16].replace("T", " ") if ts_raw else "—"
        v_from = h.get("version_from", "?")
        v_to   = h.get("version_to", "?")
        var    = h.get("changed_variable", "—")
        old_v  = h.get("old_value", "—")
        new_v  = h.get("new_value", "—")
        reason = h.get("reasoning", "")
        nt     = h.get("trades_evaluated", "—")
        ret    = h.get("realised_return")
        win_r  = h.get("win_rate")
        ret_s  = f"{float(ret)*100:+.2f}%" if ret is not None else "—"
        ret_c  = _pnl_col(float(ret)) if ret is not None else "var(--muted)"
        win_s  = f" · {float(win_r)*100:.0f}% win" if win_r is not None else ""
        conf   = h.get("confidence")
        conf_s = f" · {float(conf)*100:.0f}% conf" if conf is not None else ""
        mbadge = h.get("mode", "fallback")

        meta = ""
        if nt != "—":
            meta = f"<span style='font-size:11px;color:var(--muted)'>· {nt} trades · return <span style='color:{ret_c}'>{ret_s}</span>{win_s}{conf_s}</span>"

        return (
            f"<div style='background:var(--bg);border:0.5px solid var(--border);border-radius:10px;padding:1rem;margin-bottom:10px'>"
            f"<div style='display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px'>"
            f"<div style='display:flex;align-items:center;gap:8px'>"
            f"<span style='font-size:12px;font-weight:500'>{asset}</span>"
            f"<span style='font-size:11px;color:var(--muted)'>v{v_from}→v{v_to}</span>"
            f"<span style='font-size:10px;padding:1px 6px;border-radius:4px;background:var(--surface);color:var(--muted)'>{mbadge}</span>"
            f"</div>"
            f"<span style='font-size:11px;color:var(--muted)'>{tsd}</span>"
            f"</div>"
            f"<div style='display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap'>"
            f"<code style='font-size:11px;background:var(--surface);padding:2px 8px;border-radius:4px;border:0.5px solid var(--border)'>{var}</code>"
            f"<span style='font-size:12px;color:var(--muted)'>{old_v} → <strong>{new_v}</strong></span>"
            f"{meta}"
            f"</div>"
            f"<div style='font-size:12px;color:var(--muted);line-height:1.6'>{reason}</div>"
            f"</div>"
        )

    reflection_html = (
        "".join(_refl_card(h) for h in all_reflections)
        if all_reflections
        else "<div style='font-size:12px;color:var(--muted)'>No reflections yet — fires after every N closed trades.</div>"
    )

    # Filter log to only meaningful events — skip constant "No entry" noise
    _KEEP_PATTERNS = ("Trade #", "reflect", "Reflection", "error", "Error",
                      "circuit breaker", "Reconcil", "Abandoned", "Booting",
                      "ParserError", "Traceback", "Exception", "CRITICAL")
    _SKIP_PATTERNS = ("No entry",)

    def _is_meaningful(line: str) -> bool:
        if any(p in line for p in _SKIP_PATTERNS):
            return False
        return any(p in line for p in _KEEP_PATTERNS)

    activity_lines = [l for l in logs if _is_meaningful(l)]

    log_lines = (
        "\n".join(_log_line(l) for l in activity_lines)
        if activity_lines
        else "<div style='font-size:11px;color:var(--muted)'>No significant activity — agent is running quietly (no trades, reflections, or errors since last check).</div>"
    )

    mode_bg  = "rgba(226,75,74,0.15)" if mode == "live" else "rgba(29,158,117,0.15)"
    mode_col = "#E24B4A" if mode == "live" else "#1D9E75"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes Trading</title>
<meta http-equiv="refresh" content="{POLL_SECS}">
<style>
  :root{{--bg:#fff;--border:rgba(0,0,0,0.12);--muted:#888;--surface:#f6f6f4}}
  @media(prefers-color-scheme:dark){{:root{{--bg:#1a1a18;--border:rgba(255,255,255,0.12);--muted:#888;--surface:#232320}}}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--surface);padding:1.5rem;max-width:860px;margin:0 auto}}
  h2{{font-size:12px;font-weight:500;color:var(--muted);margin:1.5rem 0 8px}}
</style>
</head>
<body>

<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:1.5rem">
  <div>
    <div style="font-size:18px;font-weight:500">Hermes Trading</div>
    <div style="font-size:12px;color:var(--muted);margin-top:3px">
      synced {updated} &nbsp;·&nbsp; <span id="clk"></span> &nbsp;·&nbsp; refreshes every {POLL_SECS}s
    </div>
  </div>
  <span style="font-size:11px;padding:3px 10px;border-radius:6px;background:{mode_bg};color:{mode_col}">{mode} mode</span>
</div>

<script>
(function tick(){{
  var el=document.getElementById('clk');
  if(el) el.textContent=new Date().toLocaleTimeString('en-AU',{{timeZone:'Australia/Sydney',hour12:false}})+' AEST';
  setTimeout(tick,1000);
}})();
</script>

{ssh_banner}
{top_stats}

<h2>Assets</h2>
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(185px,1fr));gap:12px;margin-bottom:1.5rem">
  {asset_cards}
</div>

<h2>Live Positions <span style="font-weight:400">({len(open_trades)} open)</span></h2>
{positions_html}

<h2>Running P&L</h2>
{pnl_summary}

<h2>Trade history <span style="font-weight:400">(last 50 · newest first)</span></h2>
{trade_table}

<h2>Strategy reflections</h2>
{reflection_html}

<h2>Agent Activity <span style="font-weight:400">(trades · reflections · errors)</span></h2>
<div style="background:var(--bg);border:0.5px solid var(--border);border-radius:8px;padding:12px;font-family:monospace;overflow-x:auto;margin-bottom:1.5rem">
  {log_lines}
</div>

<div style="font-size:11px;color:var(--muted);text-align:center;padding-bottom:1.5rem">
  Hermes Trading &nbsp;·&nbsp; {VPS} &nbsp;·&nbsp; <a href="/" style="color:var(--muted)">force refresh</a>
</div>

</body></html>"""


# ── Polling server ─────────────────────────────────────────────────────────────

_cache: dict = {"data": {}, "lock": threading.Lock()}


def _poll_loop() -> None:
    while True:
        try:
            with _cache["lock"]:
                last = dict(_cache["data"])
            fresh = _fetch_data(last_known=last or None)
            with _cache["lock"]:
                _cache["data"] = fresh
        except Exception as e:
            print(f"[poll] error: {e}")
        time.sleep(POLL_SECS)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        with _cache["lock"]:
            data = _cache["data"]
        if not data:
            body = b"<html><body style='font-family:sans-serif;padding:2rem'>Loading&hellip; refresh in a few seconds.</body></html>"
        else:
            body = _render_html(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def main() -> None:
    print("Hermes Trading Dashboard")
    print(f"Connecting to {VPS}...")

    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()
    time.sleep(3)

    print(f"Dashboard at http://localhost:{PORT}")
    print("Press Ctrl+C to stop.\n")

    import webbrowser
    webbrowser.open(f"http://localhost:{PORT}")

    with http.server.HTTPServer(("", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
