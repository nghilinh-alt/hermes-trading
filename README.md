# Hermes Trading Agent

A self-improving autonomous trading agent built with Python, CCXT, and AI/ML capabilities.

## Features

- **Autonomous Trading Loop**: Self-improving agent that learns from market data and execution results
- **Multi-Source Data Adaptation**: Intelligently ingests macroeconomic, news, on-chain, and price data
- **Reflection & Learning**: Built-in reflection mechanism for continuous self-improvement
- **Scoring System**: Performance evaluation and optimization tracking

## Tech Stack

- **Core**: Python 3.10+, pandas, numpy
- **Trading**: CCXT (crypto exchanges)
- **Data**: yfinance, web scraping capabilities
- **AI/ML**: Self-learning architecture with reflection patterns

## Project Structure

```
hermes-trading/
├── hermes_trading/           # Main package
│   ├── __init__.py
│   ├── run.py                 # Entry point
│   ├── loop.py                # Trading loop logic
│   ├── score.py               # Scoring system
│   └── reflect.py             # Reflection/self-improvement
├── hermes_trading/adapters/  # Data source adapters
│   ├── __init__.py
│   ├── macro.py              # Macroeconomic data
│   ├── news.py               # News feeds
│   ├── onchain.py            # On-chain blockchain data
│   └── price.py              # Price/market data
├── .env                       # Environment variables (gitignored)
├── pyproject.toml             # Project config & dependencies
├── Dockerfile                 # Container definition
└── state/                     # Runtime state (gitignored)
```

## Setup & Installation

### Option 1: Direct Clone on VPS

```bash
# Clone the repository
git clone https://github.com/nghil/HerMES-Trading.git /path/to/your/hermes-trading
cd hermes-trading

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or: venv\Scripts\activate  # Windows

# Install dependencies
pip install -e ".[dev]"
# or for production:
pip install ccxt yfinance pyyaml httpx aiofiles numpy pandas rich
```

### Option 2: Using Docker

```bash
# Build the image
docker build -t hermes-trading .

# Run the container
docker run -d --name hermes-trading \
  -v $(pwd)/.env:/app/.env \
  -v $(pwd)/state:/app/state \
  hermes-trading
```

### Option 3: Using uv (Recommended for Python projects)

```bash
# Install uv if not present
pip install uv

# Install the project
uv sync

# Run the agent
uv run python -m hermes_trading.run
```

## Configuration

Create a `.env` file with your configuration:

```env
# Trading API Keys
CCXT_API_KEY=your_api_key_here
CCXT_API_SECRET=your_api_secret_here

# Exchange settings
EXCHANGE=binance  # or bybit, kucoin, etc.
TRADING_MODE=spot  # or futures

# AI/ML Configuration
MODEL_PATH=/path/to/model
REFLECTION_INTERVAL=3600  # seconds between reflection cycles

# Logging
LOG_LEVEL=INFO
LOG_FILE=logs/trading.log
```

## Usage

### Running the Agent

```bash
# Basic run
python -m hermes_trading.run

# With custom configuration
python -m hermes_trading.run --config=config.yaml

# Debug mode
python -m hermes_trading.run --debug
```

### Docker Compose Setup

Create `docker-compose.yml`:

```yaml
version: '3.8'
services:
  hermes-trading:
    build: .
    volumes:
      - ./state:/app/state
      - ./logs:/app/logs
    environment:
      - CCXT_API_KEY=${CCXT_API_KEY}
      - CCXT_API_SECRET=${CCXT_API_SECRET}
    restart: unless-stopped
```

```bash
docker-compose up -d
```

## Architecture Overview

### Data Flow

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Macro Data │───▶│   Adapter   │───▶│  Agent Core  │
└─────────────┘     └─────────────┘     └──────┬──────┘
                                                │
┌─────────────┐     ┌─────────────┐           │
│   News Data │───▶│   Adapter   │────────────┘
└─────────────┘     └─────────────┘
    ▲                    │
    │              ┌─────▼─────┐
┌─────────────┐  │  Score &   │◀─────── Execution Results
│ On-chain D. │───▶ Reflect   │       (for learning)
└─────────────┘  └─────────────┘
```

### Key Components

1. **Adapters**: Handle data ingestion from various sources
2. **Loop**: Core trading logic and decision making
3. **Score**: Performance metrics and evaluation
4. **Reflect**: Self-improvement and learning mechanisms

## Dependencies

See `pyproject.toml` for complete list:

```toml
ccxt>=4.0.0          # Crypto exchange library
yfinance>=0.2.0      # Stock/crypto data
pyyaml>=6.0          # YAML config
httpx>=0.27.0        # HTTP client
aiofiles>=23.0.0     # Async file I/O
numpy>=1.26.0        # Numerical computing
pandas>=2.0.0        # Data analysis
rich>=13.0.0         # Console rendering
```

## Environment Variables

| Variable | Description | Required | Default |
|----------|-------------|----------|---------|
| `CCXT_API_KEY` | API key for exchange | Yes | - |
| `CCXT_API_SECRET` | API secret for exchange | Yes | - |
| `EXCHANGE` | Exchange name | Yes | binance |
| `TRADING_MODE` | Spot or futures | Yes | spot |
| `REFLECTION_INTERVAL` | Seconds between reflection cycles | No | 3600 |
| `LOG_LEVEL` | Logging level | No | INFO |

## Deployment Checklist

- [ ] API keys configured securely (use `.env`, not git)
- [ ] Docker image tested in staging environment
- [ ] Trading strategy validated with backtesting
- [ ] Monitoring/logging configured
- [ ] Resource limits set (memory, CPU)
- [ ] Backup/recovery procedures documented
- [ ] Error handling and alerts configured

## Troubleshooting

### Common Issues

**"No module named 'hermes_trading'"**
```bash
pip install -e .
# or
uv sync
```

**"API key error"**
- Check `.env` file is loaded
- Verify keys in exchange dashboard
- Ensure permissions (read/trade) are enabled

**"Port already in use"**
```bash
# Find and kill the process
lsof -i :5175 | grep LISTEN
kill -9 <PID>
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests
5. Submit a pull request

## License

MIT License - see LICENSE file for details

## Support

For issues or questions, open an issue on GitHub.
