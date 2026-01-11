from enum import Enum


class PositionState(Enum):
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    ERROR = "error"


class MarketCondition(Enum):
    STRONG_UPTREND = "strong_uptrend"
    UPTREND = "uptrend"
    SIDEWAYS = "sideways"
    DOWNTREND = "downtrend"
    STRONG_DOWNTREND = "strong_downtrend"
    VOLATILE = "volatile"
    UNKNOWN = "unknown"


class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_LOSS = "STOP_LOSS"
    STOP_LOSS_LIMIT = "STOP_LOSS_LIMIT"
    TAKE_PROFIT = "TAKE_PROFIT"
    TAKE_PROFIT_LIMIT = "TAKE_PROFIT_LIMIT"
    LIMIT_MAKER = "LIMIT_MAKER"


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    PENDING_CANCEL = "PENDING_CANCEL"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class TimeInForce(Enum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class TradingMode(Enum):
    TESTNET = "testnet"
    REAL = "real"


class SignalType(Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    CLOSE = "close"


class RiskLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTREME = "extreme"


class PriceValidationStatus(Enum):
    FRESH = "fresh"
    STALE = "stale"
    VALIDATION_FAILED = "failed"
    VALIDATION_TIMEOUT = "timeout"


class CacheEvent(Enum):
    """Eventos de cache para invalidação"""

    MARKET_DATA_UPDATE = "market_data_update"
    POSITION_CHANGE = "position_change"
    ORDER_STATUS_CHANGE = "order_status_change"
    VOLATILITY_CHANGE = "volatility_change"


class MarketVolatility(Enum):
    """Níveis de volatilidade do mercado"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTREME = "extreme"


class ShutdownMode(Enum):
    """Modos de shutdown do bot"""

    GRACEFUL = "graceful"
    EMERGENCY = "emergency"
    FORCED = "forced"
