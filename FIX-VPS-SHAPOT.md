# Quick Fix for Your VPS

## Option 1: Re-clone from GitHub (Recommended - Gets Everything)

Run this on your VPS terminal:

```bash
cd /opt/trading/

# Remove the incomplete directory
rm -rf hermes_trading

# Clone fresh from GitHub (includes pyproject.toml, hermes_trading/, etc.)
git clone https://github.com/nghilinh-alt/hermes-trading.git .
cd hermes_trading

# Activate environment and install
source venv/bin/activate
pip install -e .

# Run the agent!
python -m hermes_trading.run
```

---

## Option 2: Copy Missing Files via SCP from Windows

From your Windows terminal, run these commands to upload files:

```cmd
cd C:\Users\nghil\projects\hermes\hermes-trading

# Upload the source package
scp hermes_trading/ root@187.127.108.173:/opt/trading/hermes_trading/

# Upload project configuration files
scp pyproject.toml root@187.127.108.173:/opt/trading/hermes_trading/
scp requirements.txt root@187.127.108.173:/opt/trading/hermes_trading/
scp Dockerfile root@187.127.108.173:/opt/trading/hermes_trading/

# On your VPS after files are uploaded:
cd /opt/trading/hermes_trading

source venv/bin/activate
pip install -e .
python -m hermes_trading.run
```

---

## Option 3: One-Liner Fix (Clone Everything Fresh)

From your VPS terminal, run this single command:

```bash
cd /opt/trading && rm -rf hermes_trading && git clone https://github.com/nghilinh-alt/hermes-trading.git . && source venv/bin/activate && pip install -e . && python -m hermes_trading.run
```

---

## Expected Output After Fix

You should see:

```
✅ HerMES-Trading initialized
📊 Loading macroeconomic data...
📰 Fetching news feeds...
🔗 Checking on-chain analytics...
💹 Loading market prices from CCXT...
🔄 Starting trading loop...
```

---

Your agent will now run with all adapters and reflection capabilities! 🚀
