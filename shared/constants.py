from decimal import Decimal
from typing import Any

MAX_POSITIONS = 7
MIN_POSITION_SIZE = Decimal("10.0")
MAX_POSITION_SIZE_PCT = Decimal("0.085")

DEFAULT_STOP_LOSS = Decimal("0.025")
DEFAULT_TAKE_PROFIT = Decimal("0.02")

MIN_VOLUME_USD = Decimal("40000")
MIN_ENTRY_SCORE = 40
AGGRESSIVE_MODE_MIN_SCORE = 30
MAX_SPREAD_PCT = Decimal("0.003")
MIN_PROFIT_TARGET = Decimal("0.008")
MIN_VOLUME_SPIKE = Decimal("1.2")
MIN_VOLATILITY_THRESHOLD = Decimal("0.008")

CACHE_TTL = 300
API_RATE_LIMIT = 1200
API_RESET_INTERVAL = 300

CHECK_INTERVAL = 30
COOLDOWN_SECONDS = 100
MAX_SLIPPAGE = Decimal("0.003")
SYMBOL_COOLDOWN_MINUTES = 1.7

MAX_CONCURRENT_ANALYSES = 4
MIN_GLOBAL_ENTRY_INTERVAL = 10
MAX_ENTRIES_PER_MINUTE = 3
ACTIVITY_LOG_INTERVAL = 60
CORRUPTION_THRESHOLD_HOURS = 1
LONG_COOLDOWN_THRESHOLD = 300
MAX_COOLDOWN_SLEEP = 1
INTER_SYMBOL_DELAY = 0.5
ERROR_RECOVERY_DELAY = 30

BALANCE_PRECISION = 8
PRICE_PRECISION = 8
QUANTITY_PRECISION = 8

DATABASE_PATH = "data/trading_bot.db"
LOG_PATH = "logs/trading_bot.log"

SHUTDOWN_TIMEOUTS = {
    "graceful": {"total": 30.0, "component": 8.0},
    "emergency": {"total": 15.0, "component": 3.0},
    "forced": {"total": 5.0, "component": 1.0},
}

EMERGENCY_EXIT_REASONS = frozenset(
    ["EMERGENCY", "SHUTDOWN", "MAX_TIME", "EXTREME_LOSS", "STOP_LOSS"]
)

BINANCE_VALID_INTERVALS = frozenset(
    [
        "1m",
        "3m",
        "5m",
        "15m",
        "30m",
        "1h",
        "2h",
        "4h",
        "6h",
        "8h",
        "12h",
        "1d",
        "3d",
        "1w",
        "1M",
    ]
)

FINANCIAL_VALIDATORS: dict[str, dict[str, Any]] = {
    "price": {"min": 0, "exclude_zero": True, "check_nan": True, "check_inf": True},
    "quantity": {"min": 0, "exclude_zero": True, "check_nan": True, "check_inf": True},
    "pnl": {"check_nan": True, "check_inf": True},
    "percentage": {"min": -100, "max": 1000, "check_nan": True},
}

VALID_POSITION_STATUSES = frozenset(["open", "closed", "pending", "cancelled", "error"])

POSITION_ERROR_CODES = {
    "insufficient_balance": {"severity": "high", "retry": False, "action": "abort"},
    "invalid_quantity": {"severity": "medium", "retry": False, "action": "validate"},
    "price_deviation": {"severity": "low", "retry": True, "action": "refresh"},
    "network_error": {"severity": "medium", "retry": True, "action": "retry"},
}

STRONG_TREND_MULTIPLIER = Decimal("1.67")
WEAK_TREND_MULTIPLIER = Decimal("0.5")

TREND_WEIGHT_SHORT = Decimal("0.6")
TREND_WEIGHT_MID = Decimal("0.3")
TREND_WEIGHT_PRICE = Decimal("0.1")

TREND_SLOPE_WEIGHT_SHORT = Decimal("0.3")
TREND_SLOPE_WEIGHT_MID = Decimal("0.2")

VOLATILITY_VERY_LOW_THRESHOLD = Decimal("0.005")
VOLATILITY_LOW_THRESHOLD = Decimal("0.01")
VOLATILITY_NORMAL_THRESHOLD = Decimal("0.02")
VOLATILITY_HIGH_THRESHOLD = Decimal("0.04")

BINANCE_ERROR_RECOVERY_STRATEGIES = {
    -1003: {
        "recoverable": True,
        "wait_multiplier": 2,
        "max_retries": 3,
        "description": "WAF Limit (IP banned)",
    },
    -1021: {
        "recoverable": True,
        "wait_multiplier": 1,
        "max_retries": 5,
        "description": "Timestamp out of recv window",
    },
    -2010: {"recoverable": False, "description": "NEW_ORDER_REJECTED"},
    -2011: {"recoverable": False, "description": "CANCEL_REJECTED"},
    1100: {
        "recoverable": True,
        "wait_multiplier": 1.5,
        "max_retries": 3,
        "description": "Illegal characters",
    },
    1101: {
        "recoverable": True,
        "wait_multiplier": 1,
        "max_retries": 3,
        "description": "Too many parameters",
    },
    1102: {"recoverable": False, "description": "Mandatory parameter missing"},
    -1001: {
        "recoverable": True,
        "wait_multiplier": 1,
        "max_retries": 3,
        "description": "Internal error",
    },
    -1006: {
        "recoverable": True,
        "wait_multiplier": 1.5,
        "max_retries": 3,
        "description": "Unexpected response",
    },
    -1007: {
        "recoverable": True,
        "wait_multiplier": 1,
        "max_retries": 3,
        "description": "Timeout",
    },
}

BINANCE_RECOVERABLE_ERROR_CODES = frozenset(
    [-1003, -1021, 1100, 1101, -1001, -1006, -1007]
)

BINANCE_API_RESPONSE_VALIDATORS = {
    "order": ["symbol", "orderId", "status", "side"],
    "balance": ["asset", "free", "locked"],
    "ticker": ["symbol", "price"],
    "orderbook": ["bids", "asks"],
    "account": ["balances"],
    "exchange_info": ["symbols"],
}

REQUIRED_SIGNAL_FIELDS = ("entry_score", "current_price")
CLEANUP_INTERVAL_ITERATIONS = 20
DEFAULT_MIN_ENTRY_SCORE = 25
