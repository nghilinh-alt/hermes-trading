"""One-shot: replace default_leverage with min_leverage/max_leverage in all 4 strategy.yamls on VPS."""
import re, os

BASE = "/opt/trading/hermes_trading/state"
ASSETS = ["btc_usdt", "eth_usdt", "sol_usdt", "tao_usdt"]

for a in ASSETS:
    p = os.path.join(BASE, a, "strategy.yaml")
    s = open(p).read()
    s = re.sub(r"^max_leverage:.*\n", "", s, flags=re.MULTILINE)
    s = re.sub(r"^default_leverage:.*\n", "min_leverage: 3\nmax_leverage: 10\n", s, flags=re.MULTILINE)
    open(p, "w").write(s)
    lines = [l for l in s.splitlines() if "leverage" in l]
    print(a, lines)
