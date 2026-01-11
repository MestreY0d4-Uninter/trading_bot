# 🚀 Cryptocurrency Scalping Trading Bot

[![Python](https://img.shields.io/badge/Python-3.13+-blue.svg)](https://python.org)
[![Binance](https://img.shields.io/badge/Exchange-Binance-yellow.svg)](https://binance.com)
[![Architecture](https://img.shields.io/badge/Architecture-Modular_Monolith-purple.svg)]()
[![Indicators](https://img.shields.io/badge/Indicators-TA--Lib_0.6.8-blue.svg)]()
[![Code Quality](https://img.shields.io/badge/Complexity-A_(3.90)-brightgreen.svg)]()
[![Linting](https://img.shields.io/badge/Ruff-Passed-success.svg)]()

> **⚠️ DISCLAIMER:** This software is for educational purposes only. Cryptocurrency trading involves substantial risk of loss. Always test thoroughly in TESTNET before using real funds.

## 📋 Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Project Structure](#project-structure)
- [Monitoring](#monitoring--logs)
- [Development](#development)
- [Troubleshooting](#troubleshooting)

## 🎯 Overview

A high-frequency cryptocurrency scalping bot optimized for short-term trades with tight risk management. Built with Python 3.13+ and designed for millisecond-level decision making in volatile crypto markets.

### Key Characteristics
- **Strategy Type:** High-frequency scalping with momentum indicators
- **Timeframes:** 5-minute candles with 10-second check intervals
- **Profit Targets:** 2.0% take profit, 0.8% stop loss
- **Position Duration:** Target 8 hours, maximum 72 hours
- **Risk Profile:** Aggressive momentum-based entries with strict risk controls
- **Precision:** All financial calculations use Decimal with 28-digit precision

## ✨ Features

### Core Trading
- 🎯 **Momentum-based scalping** with RSI, MACD, and Bollinger Bands (TA-Lib 0.6.8)
- ⚡ **Sub-second execution** with comprehensive order management
- 🛡️ **Advanced risk management** with position sizing and daily limits
- 📊 **Real-time market analysis** with trend and volatility detection (25+ indicators)
- 🔄 **Automated position monitoring** with intelligent alerts
- 🔒 **Idempotent order handling** prevents duplicate trades

### Technical Features
- 🏗️ **Modular architecture** with loosely coupled components
- 🔧 **Configuration-driven** behavior via YAML settings
- 📈 **Real-time dashboard** with rich console interface
- 🗃️ **SQLite database** for trade history and performance metrics
- 🔍 **Structured logging** with automatic routing (structlog + JSON output)
- ⚖️ **Thread-safe operations** with proper concurrency controls
- 💯 **Decimal precision** for all financial calculations (including indicators)
- 🔮 **Advanced observability** with real-time flow tracking and diagnostics
- ⚡ **High-performance indicators** using TA-Lib C library (30-50% faster)

### Risk & Monitoring
- 💰 **Position sizing:** 6.0% of capital per trade
- 🚫 **Daily loss limits:** 8.0% maximum drawdown
- 📈 **Performance tracking:** Win rate, P&L, and duration analytics
- 🎚️ **Dynamic cooldowns:** 90-second intervals between trades
- 🔔 **Position alerts:** Automated notifications for long-running positions
- 🔁 **Order deduplication:** UUID-based idempotency protection

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Shared Infrastructure                    │
│    state.py | cache.py | logger.py | metrics.py | constants.py  │
└─────────────────────────────────────────────────────────────────┘
                                    │
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│  UI Dashboard   │    │  Data Fetcher   │    │ Risk Manager    │
│  (Rich Console) │    │  (REST/Cache)   │    │ (Limits/Valid)  │
└─────────────────┘    └─────────────────┘    └─────────────────┘
         │                       │                       │
         └───────────────────────┼───────────────────────┘
                                 │
         ┌─────────────────────────────────────────────────────┐
         │            Trading Coordinator                      │
         │         (Central Orchestration)                     │
         └─────────────────────────────────────────────────────┘
                                 │
    ┌────────────────────────────┼────────────────────────────┐
    │                            │                            │
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│ Signal Analyzer │    │Position Manager │    │ Order Executor  │
│ (Entry/Exit)    │    │ (Trade Tracking)│    │ (Binance API)   │
└─────────────────┘    └─────────────────┘    └─────────────────┘
         │                       │                       │
┌─────────────────────────────────────────────────────────────────┐
│                      Core Validators                            │
│  TradingValidator | MarketValidator | OrderValidator            │
└─────────────────────────────────────────────────────────────────┘
         │                       │                       │
┌─────────────────────────────────────────────────────────────────┐
│                         Utilities                               │
│    DecimalMath | TimeUtils | FormatUtils | ValidationUtils     │
└─────────────────────────────────────────────────────────────────┘
```

### Component Interaction
- **Centralized State:** Thread-safe global state management in `shared/`
- **Event-driven:** Asynchronous task coordination
- **Fail-safe:** Circuit breakers and error recovery
- **Scalable:** Modular design for easy feature addition
- **Validated:** Centralized validation layer for all inputs
- **Precise:** Decimal-based math for financial accuracy

## 📋 Requirements

### System Requirements
- **Python:** 3.13 or higher
- **OS:** Linux, macOS, or Windows
- **RAM:** Minimum 512MB, recommended 1GB
- **Storage:** 100MB free space
- **Network:** Stable internet connection (low latency preferred)

### API Requirements
- **Binance Account:** Spot trading enabled
- **API Keys:** Read, Trade permissions
- **Testnet Access:** Highly recommended for testing

## 🔧 Installation

### 1. Clone Repository
```bash
git clone https://github.com/yourusername/trading_bot.git
cd trading_bot
```

### 2. Create Virtual Environment & Install Dependencies

**Using uv (Recommended):**
```bash
uv venv venv --python 3.13
source venv/bin/activate  # Linux/macOS
# venv\Scripts\activate   # Windows
uv pip install .
```

**Using pip:**
```bash
python -m venv venv
source venv/bin/activate  # Linux/macOS
pip install .
```

### 3. Configure Environment Variables

Copy the example environment file and add your API keys:
```bash
cp .env.example .env
# Edit .env with your Binance API credentials
```

### 4. Verify Installation
```bash
python -c "import pandas, numpy, talib, sqlalchemy; print('✅ Dependencies OK')"
python -c "import talib; print(f'✅ TA-Lib {talib.__version__} - {len(talib.get_functions())} functions available')"
pytest tests/ -v  # Run test suite
```

## ⚙️ Configuration

### 1. Environment Variables (Required)

Create a `.env` file from the example:
```bash
cp .env.example .env
```

Edit `.env` with your credentials:
```bash
# Binance Testnet API Keys (for development)
BINANCE_TESTNET_API_KEY=your_testnet_key
BINANCE_TESTNET_API_SECRET=your_testnet_secret

# Binance Production API Keys (for live trading)
BINANCE_API_KEY=your_production_key
BINANCE_API_SECRET=your_production_secret

# Trading mode: testnet or production
TRADING_MODE=testnet

# Log level: DEBUG, INFO, WARNING, ERROR
LOG_LEVEL=INFO
```

> **🔒 Security:** Never commit `.env` to version control. It's already in `.gitignore`.

### 2. Trading Configuration

The bot auto-creates `config/settings.yaml` with safe defaults on first run.

Customize as needed:
```yaml
# Risk Management
risk:
  position_size_pct: 6.0      # % of balance per trade
  max_positions: 8            # Max concurrent positions
  daily_loss_limit_pct: 8.0   # Stop trading if exceeded
  stop_loss_pct: 0.8          # Stop loss percentage
  take_profit_pct: 2.0        # Take profit percentage

# Trading Parameters
strategy:
  min_entry_score: 25         # Minimum signal score
  cooldown_seconds: 90        # Between trades per symbol
  check_interval: 10          # Analysis frequency (seconds)

# Trading Pairs
trading_pairs:
  - BTCUSDT
  - ETHUSDT
  - BNBUSDT
  - ADAUSDT
  - DOTUSDT
  - SOLUSDT
  - XRPUSDT
  - AVAXUSDT
  - POLUSDT
  - LINKUSDT
  - ATOMUSDT
  - UNIUSDT
  - ALGOUSDT
```

## 🚀 Usage

### Starting the Bot

#### Production Mode
```bash
python main.py
```

#### Debug Mode
```bash
LOG_LEVEL=DEBUG python main.py
```

### Web Dashboard

#### Quick Start

**Terminal 1: Bot Principal**
```bash
python main.py
```

**Terminal 2: Dashboard Web**

**Production (Recommended):**
```bash
python run_dashboard.py --production
```

**Development:**
```bash
python run_dashboard.py
```

Access: **http://localhost:8050**

#### Features
- 📊 Real-time metrics (balance, P&L, win rate, open positions, total trades, avg duration, signal accept rate)
- 📋 Active positions table with live P&L
- 🎯 Trading pairs overview with status badges
- 🕐 Recent trades history (24h)
- ⚡ Auto-refresh every 2 seconds
- 📱 Responsive layout (Bootstrap 5)

#### Customization
```bash
# Custom port
python run_dashboard.py --port 8080

# Debug mode (hot reload)
python run_dashboard.py --debug

# Custom database
python run_dashboard.py --db-path /path/to/db

# Remote access (production with firewall/VPN)
python run_dashboard.py --production --host 0.0.0.0 --port 8050
```

#### Remote Access

**SSH Tunnel:**
```bash
ssh -L 8050:localhost:8050 user@server
```

**NGINX Proxy:**
```nginx
location / {
    proxy_pass http://localhost:8050;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

### Console Dashboard

The bot displays a real-time console UI showing:
- Current positions and P&L
- Account balance and equity
- Market conditions
- Recent trades
- Performance metrics

## 📁 Project Structure

```
trading_bot/
├── main.py                           # Entry point
├── run_dashboard.py                  # Web dashboard (dev/prod)
├── pyproject.toml                    # Project config + dependencies
├── .env.example                      # Environment template
├── .gitignore                        # Git ignore rules
├── config/
│   ├── settings.yaml                 # Configuration (auto-created)
│   ├── config_loader.py              # Config management
│   ├── config_defaults.py            # Default configuration
│   └── config_validator.py           # Config validation
├── shared/                           # Shared infrastructure
│   ├── types/state.py                # Global state management
│   ├── infra/cache.py                # TTL caching
│   ├── observability/                # Logging & metrics
│   │   ├── logger.py                 # Structured logging (structlog)
│   │   ├── metrics.py                # Performance metrics
│   │   └── flow_tracker.py           # Component flow tracking
│   ├── constants.py                  # All constants
│   ├── enums.py                      # Enumerations
│   └── rate_limiter.py               # API rate limiting
├── core/                             # Core business logic
│   ├── analysis/indicators/          # Technical indicators (TA-Lib)
│   ├── coordinator/                  # Trading coordination
│   ├── engine/                       # Trading engine (main loop)
│   ├── execution/                    # Order execution
│   ├── market/                       # Market data management
│   ├── position/                     # Position management
│   ├── risk/                         # Risk management
│   ├── signals/                      # Signal analysis
│   └── validators/                   # Centralized validators
├── infrastructure/
│   ├── api/                          # Exchange API integration
│   │   ├── api_client.py             # Binance client (async)
│   │   └── idempotency_handler.py    # Order deduplication
│   ├── data_fetch/                   # Data collection
│   └── websocket/                    # WebSocket management
├── utils/                            # Utilities
│   ├── decimal_math.py               # Decimal precision math
│   └── validation_utils.py           # Validation helpers
├── services/
│   ├── bot_orchestrator.py           # Service orchestration
│   └── bot_lifecycle.py              # Lifecycle management
├── monitoring/
│   ├── unified_monitor.py            # Unified monitoring (2s/60s)
│   └── performance_monitor.py        # Performance tracking
├── database/
│   └── db_handler.py                 # SQLite operations (async)
├── api/
│   └── dashboard_api.py              # Dash web dashboard
├── mcp_server/                       # MCP Server (Bot Analysis)
│   ├── server.py                     # MCP tool dispatch
│   ├── models/                       # Pydantic models
│   └── tools/                        # Analysis tools
├── tests/                            # Test suite
├── logs/                             # Auto-routed JSON logs
│   ├── trading_operations.log        # Trading operations
│   └── system_maintenance.log        # System events
└── data/
    └── trading_bot.db                # SQLite database
```

## 📊 Monitoring & Logs

### Structured Logging (structlog)

The bot uses **structured logging** with automatic log routing:

**Trading Operations Log** (auto-routed: position, order, trade, signal, market, risk)
```bash
tail -f logs/trading_operations.log | jq '.'
```

**System Maintenance Log** (auto-routed: general system messages)
```bash
tail -f logs/system_maintenance.log | jq '.'
```

### Log Structure

All logs are in **JSON format** with automatic routing based on keywords:

**Trading Log Example:**
```json
{
  "module": "position_manager",
  "diagnostic": false,
  "symbol": "BTCUSDT",
  "price": 50000.0,
  "quantity": 0.001,
  "event": "POSIÇÃO ABERTA",
  "level": "info",
  "timestamp": "2025-11-17T20:47:27.710686",
  "_route": "operations"
}
```

**System Log Example:**
```json
{
  "ts": "2025-11-17T20:47:43.692803+00:00",
  "lvl": "INF",
  "mod": "logger",
  "func": "_log",
  "line": 263,
  "msg": "Bot inicializado",
  "event": "Bot inicializado"
}
```

### Log Features

- ✅ **Automatic routing** based on keywords (position, order, trade → trading_operations.log)
- ✅ **Message aggregation** (suppresses duplicates within 5s)
- ✅ **Debug levels** (BASIC, DETAILED, VERBOSE via `LOG_LEVEL` env var)
- ✅ **JSON output** for easy parsing and analysis
- ✅ **Performance optimized** using structlog (~0.3ms per log call)

### Debug Levels

```bash
# Basic debug (errors/critical only)
LOG_LEVEL=DEBUG python main.py

# Detailed debug (specific modules)
LOG_LEVEL=DEBUG_DETAILED DEBUG_MODULES=position_manager,risk_manager python main.py

# Verbose debug (everything)
LOG_LEVEL=DEBUG_VERBOSE python main.py
```

### Check Database
```bash
sqlite3 data/trading_bot.db
.tables
SELECT * FROM trades ORDER BY id DESC LIMIT 10;
```

**Database Tables:**
- `trades` - Trade history with P&L
- `circuit_breaker_state` - Emergency circuit breaker status
- `market_data` - Historical market snapshots
- `daily_metrics` - Daily performance summaries
- `performance_metrics` - Granular performance tracking

**Note:** Use MCP server for bot analysis instead of direct database/log access.

### Performance Metrics
- **Win Rate:** Percentage of profitable trades
- **Daily P&L:** Realized and unrealized profit/loss
- **Average Duration:** Mean position holding time
- **Sharpe Ratio:** Risk-adjusted returns
- **Max Drawdown:** Largest peak-to-trough decline

## 🔧 Development

### Code Quality
```bash
# Format code
black .

# Lint and fix
ruff check . --fix

# Type checking
mypy .

# Run all quality checks
make quality
```

### Testing

#### Run All Tests
```bash
pytest tests/ -v
```

#### Run Specific Tests
```bash
pytest tests/test_decimal_math.py -v
pytest tests/test_validators.py::TestTradingValidator -v
```

#### Test Coverage
```bash
pytest --cov=core --cov=shared --cov=utils tests/
```

### Adding Features

1. **Check validators** in `core/validators/` first
2. **Use DecimalMath** for all financial calculations
3. **Add constants** to `shared/constants.py`
4. **Write tests first** for financial logic
5. **Use IdempotencyHandler** for all orders

## ❗ Troubleshooting

### Common Issues

#### "Module not found" Error
```bash
# Reinstall dependencies
uv pip install --python venv/bin/python . --reinstall
```

#### Database Locked
```bash
# Check for running instances
ps aux | grep main.py
# Kill if necessary
kill -9 <PID>
```

#### API Rate Limits
- Check `shared/rate_limiter.py` configuration
- Reduce `check_interval` in settings
- Enable circuit breaker in risk settings

### Optimization Tips
- Use TESTNET for strategy optimization
- Monitor API latency with metrics
- Adjust `check_interval` based on market volatility
- Fine-tune `min_entry_score` for signal quality
- Review logs for rejected signals

---

## ⚠️ Risk Disclaimer

**IMPORTANT:** Trading cryptocurrencies involves substantial risk of loss and is not suitable for every investor. The valuation of cryptocurrencies may fluctuate, and, as a result, you may lose more than you invest. Past performance is not indicative of future results.

This bot is provided as-is for educational purposes. Always:
- Test thoroughly in TESTNET first
- Start with small amounts
- Never invest more than you can afford to lose
- Monitor the bot regularly
- Have proper risk management in place

---

**Built with ❤️ for crypto trading automation**
