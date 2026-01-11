# Trading Bot - Claude Code Guidelines

## Critical Rules (Financial Trading)

**NEVER:**
- Use `float` for money → use `Decimal` (28-digit precision)
- Send orders without idempotency key
- Modify state without `async with self._state_lock`
- Update state inside atomic operations
- Use pandas-ta (removed, migrated to TA-Lib 0.6.8)
- Log inside atomic operations (log AFTER)

**ALWAYS:**
- Use `Decimal` for ALL financial calculations (including indicators)
- Validate inputs with `core/validators/` before operations
- Use Database as SSOT (Single Source of Truth)
- Handle API rate limits
- Add `@track_component` decorator for observability
- Use `IdempotencyHandler` for ALL orders
- Convert TA-Lib outputs to Decimal: `Decimal(str(float(value)))`

## Architecture Quick Reference

**Type:** Modular Monolith | **Strategy:** High-frequency crypto scalping
**Timeframes:** 5min candles, 10s checks | **Targets:** 2% TP, 0.8% SL | **Duration:** 8h target, 72h max

**Critical Components:**
- Orchestration: `services/bot_orchestrator.py`
- Trading Logic: `core/coordinator/trading_coordinator.py`
- Position Mgmt: `core/position/` (position_entry, position_tracker, repository)
- Risk: `core/risk/risk_manager.py`
- State: `shared/types/state.py`
- Validators: `core/validators/` (trading, market, order)
- Decimal Math: `utils/decimal_math.py`
- Observability: `shared/observability/flow_tracker.py`
- Indicators: `core/analysis/indicators/` (momentum, volatility, volume, trend)

**Technical Indicators:**
- Library: TA-Lib 0.6.8 ONLY (C-optimized, pandas-ta removed)
- Output: ALWAYS convert to Decimal: `Decimal(str(float(value)))`
- Classes: `MomentumIndicators`, `VolatilityIndicators`, `VolumeIndicators`, `TrendIndicators`

## Quick Commands

**Setup:**
```bash
uv venv venv --python 3.14 && source venv/bin/activate
uv pip install --python venv/bin/python .
```

**Quality (run BEFORE any code modification):**
```bash
black . && ruff check . --fix && mypy .
pytest tests/ -v
```

**Run:**
```bash
python main.py                              # Production
LOG_LEVEL=DEBUG python main.py              # Debug
python run_dashboard.py --production        # Web dashboard (Waitress)
tail -f logs/trading_operations.log | jq '.'   # Monitor trading logs
tail -f logs/system_maintenance.log | jq '.'   # Monitor system logs
```

## Code Style

- Philosophy: `python -m this` (Zen of Python)
- Self-documenting code: no comments
- Prefer PyPI libraries over custom implementations
- Use centralized validators in `core/validators/`
- Add constants to `shared/constants.py`
- Simple is better than complex

## Development Workflow

**TDD Requirements (MANDATORY for financial risk):**
- `core/position/` - Position tracking, P&L
- `core/risk/` - Risk validation
- `infrastructure/api/idempotency_handler.py`
- `utils/decimal_math.py`

**Feature Addition Checklist:**
1. Research PyPI/ecosystem first
2. Check existing validators in `core/validators/`
3. Use `utils/decimal_math.py` for financial ops
4. Add constants to `shared/constants.py`
5. Write tests BEFORE implementation (TDD)
6. Use `IdempotencyHandler` for orders
7. Add `@track_component` for observability
8. Run quality checks: `black . && ruff check . --fix && mypy .`

**Critical Files (require full validation):**
- `shared/`
- `core/position/`, `core/risk/`, `core/validators/`
- `infrastructure/api/`
- `utils/decimal_math.py`

## Project Paths

**Data:**
- Config: `config/settings.yaml` (auto-created, safe testnet defaults)
- Environment: `.env` (API keys)
- Database: `data/trading_bot.db` (SQLite, auto-created)
- Logs: `logs/trading_operations.log` and `logs/system_maintenance.log` (JSON, auto-routed)

**Observability:**
- Flow Tracking: Unix socket `/tmp/flow_tracker.sock` (22 components)
- MCP Server: Use for bot analysis (NEVER access DB/logs directly)

**Key Directories:**
- `shared/` - Infrastructure (constants, enums, exceptions, rate_limiter)
  - `shared/types/` - State management (state.py)
  - `shared/infra/` - Infrastructure (cache.py)
  - `shared/observability/` - Logging, metrics, flow tracking
- `core/` - Trading logic (coordinator, position, risk, analysis, validators)
- `infrastructure/` - Exchange integration (api, data_fetch, websocket)
- `utils/` - Utilities (decimal_math, format, validation)
- `services/` - Orchestration (bot_orchestrator, health, metrics)
- `api/` - Web dashboard (Dash)
- `mcp_server/` - Bot analysis server
- `tests/` - Test suite

## Common Pitfalls

**State Management:**
- ❌ `self.state.positions[symbol] = pos`
- ✅ `async with self._state_lock: self.state.positions[symbol] = pos`

**Decimal Math:**
- ❌ `price * 0.02` (float)
- ✅ `DecimalMath.multiply(price, Decimal("0.02"))`

**Indicators:**
- ❌ `rsi = ta.rsi(close, 14)` (pandas-ta removed)
- ✅ `rsi_raw = talib.RSI(close_np, 14); rsi = Decimal(str(float(rsi_raw[-1])))`

**Orders:**
- ❌ `await exchange.create_order(...)`
- ✅ `await idempotency_handler.execute_with_idempotency(...)`

**Logging:**
- ❌ Log inside `async with self._state_lock`
- ✅ Log AFTER lock release

For detailed documentation, see README.md.
