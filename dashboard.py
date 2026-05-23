"""
dashboard.py — local Hermes Trading dashboard
Pulls live state from VPS via SSH, serves at http://localhost:8888
Auto-refreshes every 60 seconds.

Usage:
  python dashboard.py

Requires SSH key auth to the VPS (no password prompts).
See README if you need to set up SSH keys.
"""
import http.server
import json
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

VPS        = "root@187.127.108.173"
VPS_BASE   = "/opt/trading/hermes_trading"
ASSETS     = ["btc_usdt", "eth_usdt", "sol_usdt", "tao_usdt"]
ASSET_LABELS = {
    "btc_usdt": "BTC/USDT",
    "eth_usdt":  "ETH/USDT",
    "sol_usdt":  "SOL/USDT",
    "tao_usdt":  "TAO/USDT",
}
LOCAL_STATE = Path("state")
PORT        = 8888
POLL_SECS   = 60


def _ssh(cmd: str) -> str:
    """Run a command on the VPS via SSH. Returns stdout or empty string on error."""
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", VPS, cmd],
        capture_output=True, text=True, timeout=15,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _read_file(remote_path: str, local_fallback: Path | None = None) -> str:
    """Try SSH first, fall back to local file."""
    content = _ssh(f"cat {remote_path} 2>/dev/null")
    if content:
        return content
    if local_fallback and local_fallback.exists():
        return local_fallback.read_text()
    return ""


def _parse_jsonl_last(text: str) -> dict:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return {}
    try:
        return json.loads(lines[-1])
    except Exception:
        return {}


def _parse_strategy(text: str) -> dict:
    """Very light YAML parser — only reads top-level key: value pairs."""
    result = {}
    for line in text.splitlines():
        if ":" in line and not line.strip().startswith("#"):
            k, _, v = line.partition(":")
            result[k.strip()] = v.strip()
    return result


def _parse_heartbeat(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        return {}


def _fetch_data() -> dict:
    """Pull all state from VPS (or local fallback) and return a structured dict."""
    data = {
        "updated": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
        "assets": {},
        "log_tail": [],
        "goal": {},
        "source": "vps",
    }

    # Goal config
    goal_text = _read_file(f"{VPS_BASE}/state/goal.yaml", LOCAL_STATE / "goal.yaml")
    goal = _parse_strategy(goal_text)
    data["goal"] = {
        "target": goal.get("target_value", "25") + "%",
        "drawdown": goal.get("stop_loss_pct", "5") + "%",
        "timeframe": goal.get("timeframe", "5m"),
        "reflection": goal.get("reflection_every", "5"),
        "mode": goal.get("mode", "paper"),
    }

    # Per-asset state
    for slug in ASSETS:
        label = ASSET_LABELS[slug]
        state_dir = f"{VPS_BASE}/state/{slug}"
        local_dir = LOCAL_STATE / slug

        trades_text    = _read_file(f"{state_dir}/trades.jsonl")
        strategy_text  = _read_file(f"{state_dir}/strategy.yaml", (local_dir / "strategy.yaml") if local_dir.exists() else None)
        hb_text        = _read_file(f"{state_dir}/heartbeat.json")
        hyp_text       = _read_file(f"{state_dir}/hypotheses.jsonl")

        all_trades: list[dict] = []
        for line in trades_text.splitlines():
            line = line.strip()
            if line:
                try:
                    all_trades.append(json.loads(line))
                except Exception:
                    pass

        last_trade = all_trades[-1] if all_trades else {}
        strategy   = _parse_strategy(strategy_text)
        heartbeat  = _parse_heartbeat(hb_text)

        hypotheses = []
        for line in hyp_text.splitlines():
            line = line.strip()
            if line:
                try:
                    hypotheses.append(json.loads(line))
                except Exception:
                    pass

        data["assets"][slug] = {
            "label":        label,
            "trade_count":  len(all_trades),
            "last_trade":   last_trade,
            "trades":       all_trades,
            "strategy_ver": strategy.get("version", "—"),
            "hb_status":    heartbeat.get("status", "unknown"),
            "hb_failures":  heartbeat.get("consecutive_failures", 0),
            "last_tick":    heartbeat.get("last_tick", "—"),
            "hypotheses":   hypotheses,
        }

    # Log tail (last 15 lines)
    log_text = _read_file(f"{VPS_BASE}/state/worker.log")
    if not log_text:
        log_text = _ssh(f"tail -15 {VPS_BASE}/state/worker.log")
    lines = [l for l in log_text.splitlines() if l.strip()]
    data["log_tail"] = lines[-15:]

    if not any(data["assets"][s]["trade_count"] > 0 or data["log_tail"] for s in ASSETS):
        data["source"] = "local"

    return data


_cache: dict = {"data": {}, "lock": threading.Lock()}


def _poll_loop() -> None:
    while True:
        try:
            fresh = _fetch_data()
            with _cache["lock"]:
                _cache["data"] = fresh
        except Exception as e:
            print(f"[poll] error: {e}")
        time.sleep(POLL_SECS)


def _render_html(d: dict) -> str:
    goal  = d.get("goal", {})
    assets = d.get("assets", {})
    logs  = d.get("log_tail", [])
    updated = d.get("updated", "—")
    mode  = goal.get("mode", "paper")

    def _pnl_style(pnl: float) -> str:
        if pnl > 0:  return "color:#1D9E75;font-weight:500"
        if pnl < 0:  return "color:#E24B4A;font-weight:500"
        return ""

    def _rsi_style(rsi) -> str:
        try:
            r = float(rsi)
            if r < 30: return "color:#1D9E75;font-weight:500"
            if r > 70: return "color:#E24B4A;font-weight:500"
        except Exception:
            pass
        return "color:var(--muted)"

    def _dot(status: str, failures: int) -> str:
        if failures and int(failures) > 0:
            return "<span style='color:#EF9F27'>&#9679;</span>"
        if status == "ok":
            return "<span style='color:#1D9E75'>&#9679;</span>"
        return "<span style='color:#E24B4A'>&#9679;</span>"

    def _trade_row(t: dict) -> str:
        if not t:
            return "<td>—</td><td>—</td>"
        pnl = t.get("pnl_pct", 0)
        sign = "+" if float(pnl) >= 0 else ""
        pnl_str = f"{sign}{float(pnl)*100:.3f}%"
        price = t.get("entry_price", "—")
        return f"<td>${float(price):,.2f}</td><td style='{_pnl_style(float(pnl))}'>{pnl_str}</td>"

    def _log_line(line: str) -> str:
        cls = ""
        if "Trade #" in line:
            cls = "color:#1D9E75"
        elif "error" in line.lower() or "Error" in line:
            cls = "color:#E24B4A"
        elif "Reflection" in line or "reflect" in line:
            cls = "color:#EF9F27"
        safe = line.replace("<", "&lt;").replace(">", "&gt;")
        return f"<div style='font-size:11px;line-height:1.9;{cls}'>{safe}</div>"

    asset_cards = ""
    for slug in ASSETS:
        a = assets.get(slug, {})
        label = a.get("label", slug)
        t = a.get("last_trade", {})
        ver = a.get("strategy_ver", "—")
        count = a.get("trade_count", 0)
        status = a.get("hb_status", "unknown")
        failures = a.get("hb_failures", 0)

        rsi = t.get("rsi_at_entry", "—")
        rsi_disp = f"{float(rsi):.1f}" if rsi != "—" else "—"
        entry_price = t.get("entry_price", "—")
        pnl = t.get("pnl_pct", None)
        pnl_str = f"+{float(pnl)*100:.3f}%" if pnl and float(pnl) >= 0 else (f"{float(pnl)*100:.3f}%" if pnl else "—")
        pnl_style = _pnl_style(float(pnl)) if pnl else ""

        asset_cards += f"""
        <div style="background:var(--bg);border:0.5px solid var(--border);border-radius:12px;padding:1rem">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <span style="font-size:13px;font-weight:500">{label}</span>
            {_dot(status, failures)}
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 0">
            <span style="font-size:11px;color:var(--muted)">trades</span>
            <span style="font-size:12px;font-weight:500;text-align:right">{count}</span>
            <span style="font-size:11px;color:var(--muted)">strategy</span>
            <span style="font-size:12px;font-weight:500;text-align:right">v{ver}</span>
            <span style="font-size:11px;color:var(--muted)">last entry</span>
            <span style="font-size:12px;text-align:right">{"$"+f"{float(entry_price):,.2f}" if entry_price != "—" else "—"}</span>
            <span style="font-size:11px;color:var(--muted)">last P&L</span>
            <span style="font-size:12px;text-align:right;{pnl_style}">{pnl_str}</span>
          </div>
        </div>"""

    log_lines = "\n".join(_log_line(l) for l in logs) if logs else "<div style='font-size:11px;color:var(--muted)'>No log data yet</div>"

    total_trades = sum(assets.get(s, {}).get("trade_count", 0) for s in ASSETS)

    # ── P&L calculations ────────────────────────────────────────────
    now_utc = datetime.now(timezone.utc)

    def _cutoff(days: int) -> str:
        from datetime import timedelta
        return (now_utc - timedelta(days=days)).isoformat()

    def _period_pnl(trades: list[dict], since_iso: str) -> float:
        return sum(float(t.get("pnl_pct", 0)) for t in trades if t.get("ts", "") >= since_iso) * 100

    def _pnl_chip(val: float) -> str:
        col = "#1D9E75" if val >= 0 else "#E24B4A"
        sign = "+" if val >= 0 else ""
        return f"<span style='color:{col};font-weight:500'>{sign}{val:.2f}%</span>"

    # Aggregate P&L across all assets per period
    all_trades_flat = [t for s in ASSETS for t in assets.get(s, {}).get("trades", [])]
    agg = {
        "day":   _period_pnl(all_trades_flat, _cutoff(1)),
        "week":  _period_pnl(all_trades_flat, _cutoff(7)),
        "month": _period_pnl(all_trades_flat, _cutoff(30)),
        "all":   _period_pnl(all_trades_flat, ""),
    }

    pnl_summary = f"""
<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:1.5rem">
  <div style="background:var(--surface);border:0.5px solid var(--border);border-radius:8px;padding:12px">
    <div style="font-size:11px;color:var(--muted);margin-bottom:4px">today P&L</div>
    <div style="font-size:20px;font-weight:500">{_pnl_chip(agg['day'])}</div>
  </div>
  <div style="background:var(--surface);border:0.5px solid var(--border);border-radius:8px;padding:12px">
    <div style="font-size:11px;color:var(--muted);margin-bottom:4px">7-day P&L</div>
    <div style="font-size:20px;font-weight:500">{_pnl_chip(agg['week'])}</div>
  </div>
  <div style="background:var(--surface);border:0.5px solid var(--border);border-radius:8px;padding:12px">
    <div style="font-size:11px;color:var(--muted);margin-bottom:4px">30-day P&L</div>
    <div style="font-size:20px;font-weight:500">{_pnl_chip(agg['month'])}</div>
  </div>
  <div style="background:var(--surface);border:0.5px solid var(--border);border-radius:8px;padding:12px">
    <div style="font-size:11px;color:var(--muted);margin-bottom:4px">all-time P&L</div>
    <div style="font-size:20px;font-weight:500">{_pnl_chip(agg['all'])}</div>
  </div>
</div>"""

    # Trade history table — all trades newest first
    recent_trades = sorted(all_trades_flat, key=lambda t: t.get("ts", ""), reverse=True)[:50]

    def _trade_row_html(t: dict) -> str:
        ts    = t.get("ts", "")[:16].replace("T", " ")
        asset = t.get("asset", "—")
        dirn  = t.get("direction", "—")
        dirn_col = "#1D9E75" if dirn == "long" else "#E24B4A"
        entry = t.get("entry_price", 0)
        pnl   = float(t.get("pnl_pct", 0)) * 100
        pnl_col = "#1D9E75" if pnl >= 0 else "#E24B4A"
        sign  = "+" if pnl >= 0 else ""
        ver   = t.get("strategy_version", "—")
        rsi   = t.get("rsi_at_entry")
        rsi_str = f"{float(rsi):.1f}" if rsi is not None else "—"
        return (f"<tr style='border-top:0.5px solid var(--border)'>"
                f"<td style='padding:6px 8px;font-size:11px;color:var(--muted)'>{ts}</td>"
                f"<td style='padding:6px 8px;font-size:12px'>{asset}</td>"
                f"<td style='padding:6px 8px;font-size:12px;color:{dirn_col}'>{dirn}</td>"
                f"<td style='padding:6px 8px;font-size:12px'>${float(entry):,.2f}</td>"
                f"<td style='padding:6px 8px;font-size:12px;color:var(--muted)'>{rsi_str}</td>"
                f"<td style='padding:6px 8px;font-size:12px;font-weight:500;color:{pnl_col}'>{sign}{pnl:.3f}%</td>"
                f"<td style='padding:6px 8px;font-size:11px;color:var(--muted)'>v{ver}</td>"
                f"</tr>")

    if recent_trades:
        trade_rows = "\n".join(_trade_row_html(t) for t in recent_trades)
        trade_table = f"""
<div style="background:var(--bg);border:0.5px solid var(--border);border-radius:8px;overflow:hidden;margin-bottom:1.5rem">
  <table style="width:100%;border-collapse:collapse">
    <thead>
      <tr style="background:var(--surface)">
        <th style="padding:8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">time (UTC)</th>
        <th style="padding:8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">asset</th>
        <th style="padding:8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">side</th>
        <th style="padding:8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">entry</th>
        <th style="padding:8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">RSI</th>
        <th style="padding:8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">P&L</th>
        <th style="padding:8px;font-size:11px;font-weight:500;color:var(--muted);text-align:left">strat</th>
      </tr>
    </thead>
    <tbody>{trade_rows}</tbody>
  </table>
</div>"""
    else:
        trade_table = "<div style='font-size:12px;color:var(--muted);margin-bottom:1.5rem'>No trades yet — entry fires when RSI &lt; 30 + price at lower Bollinger Band.</div>"

    # ── Reflection history ───────────────────────────────────────────
    # Build reflection history across all assets, sorted newest first
    all_reflections = []
    for slug in ASSETS:
        for h in assets.get(slug, {}).get("hypotheses", []):
            all_reflections.append({**h, "_asset": ASSET_LABELS.get(slug, slug)})
    all_reflections.sort(key=lambda x: x.get("ts", ""), reverse=True)

    def _reflection_card(h: dict) -> str:
        asset   = h.get("_asset", "")
        ts_raw  = h.get("ts", "")
        ts_disp = ts_raw[:16].replace("T", " ") if ts_raw else "—"
        v_from  = h.get("version_from", "?")
        v_to    = h.get("version_to", "?")
        var     = h.get("changed_variable", "—")
        old_v   = h.get("old_value", "—")
        new_v   = h.get("new_value", "—")
        reason  = h.get("reasoning", "")
        trades  = h.get("trades_evaluated", "—")
        ret     = h.get("realised_return")
        ret_str = f"{float(ret)*100:+.2f}%" if ret is not None else "—"
        ret_col = "#1D9E75" if ret and float(ret) >= 0 else "#E24B4A"
        conf    = h.get("confidence")
        conf_str = f"{float(conf)*100:.0f}%" if conf is not None else ""

        return f"""<div style="background:var(--bg);border:0.5px solid var(--border);border-radius:10px;padding:1rem;margin-bottom:10px">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px">
    <div>
      <span style="font-size:12px;font-weight:500">{asset}</span>
      <span style="font-size:11px;color:var(--muted);margin-left:8px">v{v_from} → v{v_to}</span>
    </div>
    <span style="font-size:11px;color:var(--muted)">{ts_disp}</span>
  </div>
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap">
    <span style="font-size:12px;font-family:monospace;background:var(--surface);padding:2px 8px;border-radius:4px;border:0.5px solid var(--border)">{var}</span>
    <span style="font-size:12px;color:var(--muted)">{old_v}</span>
    <span style="font-size:12px;color:var(--muted)">→</span>
    <span style="font-size:12px;font-weight:500">{new_v}</span>
    {f'<span style="font-size:11px;color:var(--muted)">· {trades} trades · return <span style=color:{ret_col}>{ret_str}</span></span>' if trades != "—" else ""}
    {f'<span style="font-size:11px;color:var(--muted)">· confidence {conf_str}</span>' if conf_str else ""}
  </div>
  <div style="font-size:12px;color:var(--muted);line-height:1.6">{reason}</div>
</div>"""

    reflection_html = "".join(_reflection_card(h) for h in all_reflections) if all_reflections else \
        "<div style='font-size:12px;color:var(--muted)'>No reflections yet — fires after every 5 closed trades.</div>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes Trading</title>
<meta http-equiv="refresh" content="{POLL_SECS}">
<style>
  :root {{--bg:#fff;--border:rgba(0,0,0,0.12);--muted:#888;--surface:#f6f6f4}}
  @media(prefers-color-scheme:dark){{:root{{--bg:#1a1a18;--border:rgba(255,255,255,0.12);--muted:#888;--surface:#232320}}}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--surface);color:inherit;padding:1.5rem;max-width:800px;margin:0 auto}}
  h1{{font-size:18px;font-weight:500}}
</style>
</head>
<body>

<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:1.5rem">
  <div>
    <h1>Hermes Trading</h1>
    <div style="font-size:12px;color:var(--muted);margin-top:3px">
      synced {updated} &nbsp;·&nbsp; <span id="clk"></span> &nbsp;·&nbsp; refreshes every {POLL_SECS}s
    </div>
  </div>
  <span style="font-size:11px;padding:3px 10px;border-radius:6px;background:{'rgba(29,158,117,0.15)' if mode=='paper' else 'rgba(226,75,74,0.15)'};color:{'#1D9E75' if mode=='paper' else '#E24B4A'}">{mode} mode</span>
</div>
<script>
  (function tick(){{
    var n=new Date();
    var s=n.toUTCString().slice(17,25)+' UTC';
    var el=document.getElementById('clk');
    if(el) el.textContent=s;
    setTimeout(tick,1000);
  }})();
</script>

<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:1.5rem">
  <div style="background:var(--surface);border:0.5px solid var(--border);border-radius:8px;padding:12px">
    <div style="font-size:11px;color:var(--muted);margin-bottom:4px">target / 30d</div>
    <div style="font-size:20px;font-weight:500">{goal.get("target","—")}</div>
  </div>
  <div style="background:var(--surface);border:0.5px solid var(--border);border-radius:8px;padding:12px">
    <div style="font-size:11px;color:var(--muted);margin-bottom:4px">max drawdown</div>
    <div style="font-size:20px;font-weight:500">{goal.get("drawdown","—")}</div>
  </div>
  <div style="background:var(--surface);border:0.5px solid var(--border);border-radius:8px;padding:12px">
    <div style="font-size:11px;color:var(--muted);margin-bottom:4px">total trades</div>
    <div style="font-size:20px;font-weight:500">{total_trades}</div>
  </div>
  <div style="background:var(--surface);border:0.5px solid var(--border);border-radius:8px;padding:12px">
    <div style="font-size:11px;color:var(--muted);margin-bottom:4px">reflect every</div>
    <div style="font-size:20px;font-weight:500">{goal.get("reflection","5")} trades</div>
  </div>
</div>

<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:1.5rem">
  {asset_cards}
</div>

<div style="font-size:12px;font-weight:500;color:var(--muted);margin-bottom:8px">Running P&L</div>
{pnl_summary}

<div style="font-size:12px;font-weight:500;color:var(--muted);margin-bottom:8px">Trade history <span style="font-weight:400;color:var(--muted)">(last 50 · newest first)</span></div>
{trade_table}

<div style="font-size:12px;font-weight:500;color:var(--muted);margin-bottom:8px">Strategy reflections</div>
{reflection_html}

<div style="font-size:12px;font-weight:500;color:var(--muted);margin-bottom:8px;margin-top:1.5rem">Recent log</div>
<div style="background:var(--bg);border:0.5px solid var(--border);border-radius:8px;padding:12px;font-family:monospace;overflow-x:auto">
  {log_lines}
</div>

<div style="font-size:11px;color:var(--muted);margin-top:1rem;text-align:center">
  Hermes Trading · {VPS} · <a href="/" style="color:var(--muted)">force refresh</a>
</div>

</body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        with _cache["lock"]:
            data = _cache["data"]
        if not data:
            body = b"<html><body>Loading... refresh in a few seconds.</body></html>"
        else:
            body = _render_html(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # silence request logs


def main():
    print(f"Hermes Trading Dashboard")
    print(f"Connecting to {VPS}...")

    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()

    time.sleep(3)

    print(f"Dashboard running at http://localhost:{PORT}")
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
