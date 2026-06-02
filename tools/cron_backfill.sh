#!/bin/bash
# Hermes-Trading periodic Bybit closed-trade backfill.
# Designed to be invoked by cron every 4 hours on the VPS.
#
# - Loads .env so BYBIT_API_KEY/SECRET are available
# - Backfills only the last 1 day per run (well within Bybit's 7-day V5 query window;
#   dedup is by order_id so overlap is harmless and we never need a wider window
#   in steady-state)
# - Appends to /var/log/hermes-backfill.log so we can audit cron runs
#
# Suggested crontab line (run `crontab -e` as root):
#   0 */4 * * * /opt/trading/hermes_trading/tools/cron_backfill.sh
#
# To run manually: bash /opt/trading/hermes_trading/tools/cron_backfill.sh

set -e

REPO=/opt/trading/hermes_trading
LOG=/var/log/hermes-backfill.log

cd "$REPO"

# Load .env into environment for the python invocation
set -a
[ -f .env ] && . ./.env
set +a

TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
echo "=== $TS — cron backfill start ===" >> "$LOG"

"$REPO/.venv/bin/python" -m tools.backfill_trades --lookback-days 1 >> "$LOG" 2>&1

echo "=== $TS — cron backfill done ===" >> "$LOG"
echo "" >> "$LOG"
