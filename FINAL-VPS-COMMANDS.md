# Complete Setup Commands for Your VPS (root@srv1524156)

## Quick Start (Copy-Paste All at Once):

```bash
cd /opt/trading/hermes_trading

# Create new virtual environment
python3 -m venv venv

# Activate it
source venv/bin/activate

# Upgrade pip and install ALL dependencies including uv for future updates
pip install --upgrade pip && \
pip install ccxt yfinance pyyaml httpx aiofiles numpy pandas rich uv && \
pip install -e .

# Run the agent!
python -m hermes_trading.run
```

---

## Or Two Commands:

### Command 1: Create venv and install
```bash
cd /opt/trading/hermes_trading && python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip && pip install ccxt yfinance pyyaml httpx aiofiles numpy pandas rich uv -e .
```

### Command 2: Run the agent
```bash
python -m hermes_trading.run
```

---

## Expected Output After Running:

You should see something like:

```
✅ HerMES-Trading initialized
📊 Loading market data from CCXT...
🧠 Reflection cycle 1 complete
📈 Performance score updated
🔄 Trading loop running!
```

---

## Monitoring Commands After Start:

```bash
# View logs in real-time
tail -f state/*.json 2>/dev/null || tail -f hermes_trading.log

# Check if process is running
ps aux | grep hermes_trading

# Stop agent
Ctrl+C (in terminal) or kill the process ID
```

---

## If You Want to Keep It Running in Background:

```bash
nohup python -m hermes_trading.run > /opt/trading/hermes_trading.log 2>&1 &
tail -f /opt/trading/hermes_trading.log
```

---

Let me know what you see after running! 🚀
