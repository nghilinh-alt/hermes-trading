# HerMES-Trading - Setup on VPS (Step-by-Step)

You're at /opt/trading/hermes_trading/ - the venv doesn't exist yet.

## Quick Setup Commands

```bash
cd /opt/trading/hermes_trading

# 1. Create virtual environment
python3 -m venv venv

# 2. Activate it
source venv/bin/activate

# 3. Upgrade pip and install dependencies
pip install --upgrade pip
pip install ccxt yfinance pyyaml httpx aiofiles numpy pandas rich uv

# 4. Install the hermes_trading package (editable mode)
pip install -e .

# 5. Configure environment (optional - leave blank for paper trading)
nano .env  # Press Ctrl+X, Y, Enter to save empty file
# Or echo > .env if you want it completely empty

# 6. Run the agent
python -m hermes_trading.run
```

---

## Alternative: One-Liner Setup

If you prefer a single command line:

```bash
cd /opt/trading/hermes_trading && python3 -m venv venv && source venv/bin/activate && pip install --upgrade pip && pip install ccxt yfinance pyyaml httpx aiofiles numpy pandas rich uv && pip install -e .
```

Then run:
```bash
python -m hermes_trading.run
```

---

## Check Installation After Setup

```bash
# Verify installation
python -c "import hermes_trading, ccxt, pandas; print('✅ All dependencies installed!')"

# List installed packages
pip list | grep -iE "(hermes|ccxt|pandas)"

# Check Python path
which python
echo $VIRTUAL_ENV
```

---

## Run Without Activating venv (Alternative)

If you prefer not to activate:

```bash
cd /opt/trading/hermes_trading
python3 -m hermes_trading.run
```

---

## Monitor the Agent After Setup

```bash
# Watch for logs in real-time
tail -f state/*.json 2>/dev/null || echo "No state files yet, watching output..."

# Check if process is running (after it starts)
ps aux | grep hermes_trading

# Kill and restart if needed
kill %1  # If running as background job
```

---

## Full Setup Script to Copy-Paste

```bash
cd /opt/trading/hermes_trading

echo "Setting up HerMES-Trading..."

# Create venv
python3 -m venv venv || { echo "venv already exists"; }

# Activate
source venv/bin/activate

# Upgrade pip
pip install --upgrade pip >/dev/null 2>&1

# Install dependencies
pip install ccxt yfinance pyyaml httpx aiofiles numpy pandas rich uv >/dev/null 2>&1

# Install package in editable mode
pip install -e . >/dev/null 2>&1

echo "✅ Installation complete!"
echo ""
echo "Run the agent with:"
echo "  source venv/bin/activate"
echo "  python -m hermes_trading.run"
```

---

After running the setup, you'll be able to run:

```bash
source venv/bin/activate
python -m hermes_trading.run
```

The agent will output trading activity, data loading, and performance metrics!

---

Let me know if you encounter any errors during setup! 🚀
