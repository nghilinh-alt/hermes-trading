# HerMES-Trading - Quick Start Commands for Your VPS

You're currently in /opt/trading/hermes_trading/ - here are the commands:

## ✅ Run the Agent

### Option 1: Using Python3 (Recommended)
```bash
source venv/bin/activate  # Activate virtual environment
python -m hermes_trading.run
```

### Option 2: Direct with python3
```bash
# Skip activation, use python3 directly
cd /opt/trading/hermes_trading
python3 -m hermes_trading.run
```

### Option 3: Interactive Session
```bash
source venv/bin/activate
python
>>> from hermes_trading import run
>>> run.main()
>>> exit()
```

---

## 🔍 Check Deployment Status

```bash
# List installed packages
pip list | grep -i "hermes\|ccxt\|pandas"

# Verify the module is importable
python -c "import hermes_trading; print('✅ Module loaded')"

# Check Python environment
which python
which python3
echo $VIRTUAL_ENV
```

---

## 📊 Monitor the Agent

```bash
# View state files (trading data, reflections)
ls -lh state/
tail -f state/*.json 2>/dev/null || cat state/*.json

# Or watch for log output on terminal
python -m hermes_trading.run  # Run and watch console output
```

---

## 🔐 Configuration Check

```bash
# View current .env settings
cat /opt/trading/.env  # or: source venv/bin/activate && cat ~/.venv/envvars.txt

# If running without activation, check if .env is sourced
ls -lh /opt/trading/hermes_trading/.env 2>/dev/null || echo ".env not found (using defaults)"
```

---

## 🚀 Start Agent as a Background Service

To keep it running after your terminal closes:

### Using nohup (Simple):
```bash
nohup python -m hermes_trading.run > /opt/trading/hermes_trading.log 2>&1 &
# Check logs later:
tail -f /opt/trading/hermes_trading.log
```

### With systemd (Recommended for production):
```bash
sudo mkdir -p /etc/systemd/system/
sudo tee /etc/systemd/system/hermes-trading.service > /dev/null << 'EOF'
[Unit]
Description=HerMES Trading Agent
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/trading/hermes_trading
Environment="PATH=/opt/trading/hermes_trading/venv/bin"
ExecStart=/opt/trading/hermes_trading/venv/bin/python -m hermes_trading.run
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl start hermes-trading
sudo systemctl enable hermes-trading

# Check status
sudo systemctl status hermes-trading
```

---

## 📈 Monitoring Commands

```bash
# Check running processes
ps aux | grep hermes_trading

# Memory usage
free -h

# Disk space for logs
df -h /opt/trading/

# Recent state files
ls -lh /opt/trading/hermes_trading/state/*.json 2>/dev/null || echo "No JSON states yet"
```

---

## ⚙️ Quick Status Checks

### Is the agent running?
```bash
python -c "import hermes_trading; print('✅ Installed')"
ls state/  # Should have .env or config files
```

### Check for errors in imports:
```bash
source venv/bin/activate
python -c "import ccxt, pandas, numpy; print('✅ All dependencies OK')"
```

### View last log lines (if any):
```bash
tail -20 /opt/trading/hermes_trading.log 2>/dev/null || echo "No log file yet"
```

---

## 🎯 Recommended First Steps

```bash
# 1. Verify installation
source venv/bin/activate
python -c "import hermes_trading; print('✅ HerMES-Trading installed!')"

# 2. Check what's in state directory
ls -la /opt/trading/hermes_trading/state/

# 3. Run the agent (first test)
python -m hermes_trading.run

# 4. If you want it to keep running, use nohup:
nohup python -m hermes_trading.run > /opt/trading/log.txt 2>&1 &
tail -f /opt/trading/log.txt
```

---

## ❓ Common Issues & Solutions

**"Module not found" error:**
```bash
source venv/bin/activate
pip install -e .
```

**"Permission denied":**
```bash
chmod +x hermes_trading/*.py
sudo chown -R root:root /opt/trading/hermes_trading  # If needed
```

**ImportError (missing packages):**
```bash
source venv/bin/activate
pip install --upgrade pip
pip install ccxt yfinance pyyaml httpx aiofiles numpy pandas rich
```

---

## 📝 Next Steps After Running

1. **Monitor output** - Watch for "cycle complete" messages
2. **Check state files** - Verify .env or config was loaded
3. **Review logs** - Look at /opt/trading/*.log for any errors

If you see any specific errors, copy-paste them and I'll help troubleshoot! 🎯
