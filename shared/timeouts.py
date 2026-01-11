"""
Timeouts centralizados do sistema

Consolida todos os timeouts hardcoded em um único lugar para fácil manutenibilidade.
"""

from enum import Enum


class Timeouts(float, Enum):
    """Timeouts centralizados do trading bot (em segundos)"""

    # API Client - Connection
    CLIENT_CREATE = 20.0
    CLIENT_PING = 5.0
    CLIENT_RECONNECT = 10.0
    CLIENT_HEALTH_CHECK = 3.0

    # API Client - Requests
    REQUEST_DEFAULT = 15.0
    REQUEST_PING = 3.0
    REQUEST_TICKER = 5.0
    REQUEST_ORDERBOOK = 8.0
    REQUEST_KLINES = 10.0
    REQUEST_ACCOUNT = 15.0
    REQUEST_ALL_TICKERS = 20.0

    # Connection Pool
    POOL_INIT_CLIENT = 25.0
    POOL_INIT_FULL = 30.0
    POOL_VALIDATION = 20.0

    # Orders
    ORDER_CREATE = 15.0
    ORDER_CANCEL = 10.0
    ORDER_OCO_CREATE = 20.0
    ORDER_STATUS_CHECK = 5.0

    # Emergency Operations
    EMERGENCY_CLOSE = 1.5
    EMERGENCY_TOTAL = 30.0
    EMERGENCY_RECONNECT = 5.0

    # Database
    DB_CONNECTION = 1.0
    DB_QUERY = 3.0
    DB_TRANSACTION = 5.0
    DB_BACKUP = 30.0

    # WebSocket
    WS_CONNECT = 5.0
    WS_PING = 3.0
    WS_RECONNECT = 8.0
    WS_CLOSE = 3.0

    # Health Checks & Monitoring
    HEALTH_CHECK = 3.0
    METRICS_COLLECTION = 5.0

    # Position Management
    POSITION_ENTRY = 10.0
    POSITION_EXIT = 10.0
    POSITION_UPDATE = 5.0

    # Recovery & Sync
    RECOVERY_SYNC = 20.0
    RECOVERY_VALIDATION = 15.0

    # Monitoring & Maintenance
    MONITOR_CHECK = 2.0
    MONITOR_EXTENDED = 10.0
    COMPONENT_STARTUP = 90.0
    MAINTENANCE_FULL = 300.0

    # Startup & Shutdown
    STARTUP_CHECK = 10.0
    CLEANUP_TIMEOUT = 3.0


# Backwards compatibility - pode usar Timeouts.ORDER_CREATE ou TIMEOUTS.ORDER_CREATE
TIMEOUTS = Timeouts
