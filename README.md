# HerMES-Trading - Complete Self-Improving Trading Agent

## 🚀 Overview

A **self-improving autonomous trading agent** that leverages market data, macroeconomic indicators, news sentiment, and on-chain analytics to execute intelligent trading strategies with continuous reflection and learning.

---

## ✨ Key Features

- **Autonomous Trading Loop**: Self-optimizing agent that learns from execution results
- **Multi-Source Data Integration**: 
  - 📊 Market data via CCXT (crypto exchanges)
  - 🌐 Macroeconomic indicators
  - 📰 News sentiment analysis
  - 🔗 On-chain blockchain analytics
- **Reflection & Self-Improvement**: Built-in reflection mechanism for continuous optimization
- **Performance Scoring**: Real-time evaluation and adaptive strategy adjustment
- **Docker Native**: Easy deployment with `docker-compose`

---

## 🏗️ Tech Stack

| Category | Technology |
|----------|-----------|
| Core | Python 3.10+ |
| Trading API | CCXT (crypto exchanges) |
| Data Analysis | pandas, numpy |
| HTTP/Async | httpx, aiofiles |
| Documentation | rich console renderer |
| Deployment | Docker, uv package manager |

---

## 📁 Project Structure

```
hermes-trading/
├── hermes_trading/           # Core Python package
│   ├── __init__.py
│   ├── run.py                 # Entry point / main orchestrator
│   ├── loop.py                # Trading loop logic & decision engine
│   ├── score.py               # Performance metrics & scoring system
│   └── reflect.py             # Reflection/self-improvement mechanisms
│
├── hermes_trading/adapters/  # Data source adapters
│   ├── __init__.py
│   ├── macro.py              # Macroeconomic data ingestion
│   ├── news.py               # News feeds & sentiment analysis
│   ├── onchain.py            # On-chain blockchain analytics
│   └── price.py              # Price/market data (CCXT)
│
├── pyproject.toml             # Modern Python packaging config
├── requirements.txt           # Simple pip dependency list
├── Dockerfile                 # Container definition
├── docker-compose.yml         # Orchestration setup
├── .env                       # Environment variables template
├── .gitignore                 # Git exclusion rules
│
└── state/                     # Runtime state (gitignored)
```

---

## 🚀 Quick Start

### Option 1: VPS Deployment (Recommended)

```bash
# Clone from GitHub
git clone https://github.com/nghilinh-alt/hermes-trading.git /opt/trading
cd /opt/trading/hermes-trading

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -e .

# Configure API keys (optional for paper trading)
cp .env.example .env  # Edit with your secrets or leave blank

# Run the agent
python -m hermes_trading.run
```

### Option 2: Docker Deployment

```bash
git clone https://github.com/nghilinh-alt/hermes-trading.git /opt/trading
cd /opt/trading/hermes-trading

# Build and run with docker-compose
docker-compose up -d --build

# View logs
docker-compose logs -f trading-agent
```

### Option 3: Using uv (Fast & Modern)

```bash
pip install uv
cd /opt/trading/hermes-trading
uv sync
uv run python -m hermes_trading.run
```

---

## 🔧 Configuration

Create/edit your `.env` file:

```env
# Trading Mode
HERMES_TRADING_MODE=paper        # or 'live'

# Risk Settings
HERMES_TRADING_I_ACCEPT_RISK=false

# Exchange API (optional - leave blank for free tier)
EXCHANGE_API_KEY=your_api_key_here
EXCHANGE_API_SECRET=your_secret_here
EXCHANGE=binance  # Options: binance, bybit, kucoin, etc.
TRADING_MODE=spot  # or 'futures'

# Third-party APIs (optional)
GLASSNODE_API_KEY=  # On-chain analytics
NEWS_API_KEY=       # News sentiment

# Runtime
LOG_LEVEL=INFO
REFLECTION_INTERVAL=3600  # Seconds between reflection cycles
```

---

## 📊 Architecture Overview

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ Macro Data │───▶│   Adapter    │───▶│ Agent Core   │
└─────────────┘     └─────────────┘     └──────┬──────┘
                                                │
┌─────────────┐     ┌─────────────┐           │
│ News Data   │───▶│   Adapter    │────────────┘
└─────────────┘     └─────────────┘
    ▲                    │
    │              ┌─────▼─────┐
┌─────────────┐  │  Score &   │◀─────── Execution Results
│On-chain D.  │───▶ Reflect   │       (for learning)
└─────────────┘  └─────────────┘
```

### Core Components

1. **Adapters**: Handle data ingestion from various sources (CCXT, macro APIs, news feeds, on-chain analytics)
2. **Loop**: Core trading logic - makes decisions based on synthesized market intelligence
3. **Score**: Performance metrics, PnL tracking, risk assessment
4. **Reflect**: Self-improvement mechanism - learns from past trades and optimizes strategies

---

## 🔐 Security Best Practices

### Never commit secrets!

- API keys go in `.env` (gitignored)
- Use GitHub Secrets for production deployments
- Consider using a secrets manager (HashiCorp Vault, AWS Secrets Manager)

### Environment Variables by Sensitivity

| Variable | Default | When to set |
|----------|---------|-------------|
| `HERMES_TRADING_MODE` | `paper` | Always - use `paper` for testing |
| `EXCHANGE_API_KEY` | (blank) | Only for live trading |
| `GLASSNODE_API_KEY` | (blank) | For on-chain analytics |

---

## 📈 Monitoring & Logs

### Log Output

The agent outputs rich console logs including:
- Market data ingestion events
- Reflection cycles and learnings
- Trading decisions and executions
- Performance metrics

### Production Setup

Add logging configuration to `logging.conf`:

```ini
[loggers]
keys=root,hermes_trading

[handlers]
keys=console,file

[formatters]
key=simple,detailed

[logger_root]
level=INFO
handlers=console,file

[logger_hermes_trading]
level=DEBUG
handlers=console,file
```

---

## 🔍 Troubleshooting

### "Module not found" errors

```bash
pip install -e .  # Or: uv sync
```

### Port already in use

The agent may bind to ports for webhooks. Change in config or kill existing processes:

```bash
lsof -i :5175 | grep LISTEN
kill -9 <PID>
```

### API rate limit errors

Reduce `REFLECTION_INTERVAL` or use premium exchange tiers.

---

## 📚 Development Guidelines

### Adding New Data Sources

1. Create adapter in `hermes_trading/adapters/`
2. Follow existing patterns (e.g., `price.py`)
3. Update adapters' `__init__.py`

### Writing Unit Tests

```bash
# Install dev dependencies
pip install pytest pytest-asyncio

# Run tests
pytest tests/
```

---

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Run tests
5. Submit a pull request

---

## 📄 License

MIT License - See `LICENSE` file for details.

---

## 🔗 Resources

- **GitHub Repository**: https://github.com/nghilinh-alt/hermes-trading
- **Documentation**: `README.md` + `DEPLOY-GUIDE.md`
- **Issues & Bug Reports**: [GitHub Issues](https://github.com/nghilinh-alt/hermes-trading/issues)

---

## ⚠️ Risk Disclaimer

Trading involves substantial risk of loss and is not suitable for every investor. The `HERMES_TRADING_I_ACCEPT_RISK=false` setting in the `.env` file indicates that this is a research/training project, not production financial advice.

Always test with paper trading (mock mode) before deploying live capital.
