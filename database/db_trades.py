import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from database.db_connection import DatabasePool
from shared.observability.flow_tracker import track_component
from shared.observability.logger import debug, error, production
from shared.observability.metrics import metrics
from utils.decimal_math import safe_decimal_convert, to_decimal


class DatabaseTradesMixin:
    def __init__(
        self, db_path: str, max_connections: int = 10, backup_interval_hours: int = 24
    ):
        self.db_path = db_path
        self.backup_interval_hours = backup_interval_hours
        self.last_backup_time = datetime.now()

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        self.pool = DatabasePool(db_path, max_connections)

        self._maintenance_task: asyncio.Task | None = None

    @staticmethod
    def _deserialize_trade(trade_dict: dict[str, Any]) -> dict[str, Any]:
        if not trade_dict:
            return trade_dict

        decimal_fields = [
            "entry_price",
            "exit_price",
            "quantity",
            "realized_pnl",
            "stop_loss",
            "take_profit",
            "score",
        ]

        for field in decimal_fields:
            if field in trade_dict and trade_dict[field] is not None:
                try:
                    trade_dict[field] = safe_decimal_convert(trade_dict[field])
                except Exception:
                    pass

        return trade_dict

    async def initialize(self):
        await self.pool.initialize()
        await self._init_database()
        self._schedule_maintenance()

    async def _init_database(self):
        async with self.pool.acquire() as conn:
            cursor = await conn.cursor()

            await cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    entry_price TEXT NOT NULL,
                    exit_price TEXT,
                    quantity TEXT NOT NULL,
                    entry_time TEXT NOT NULL,
                    exit_time TEXT,
                    realized_pnl TEXT DEFAULT '0',
                    status TEXT DEFAULT 'OPEN',
                    score TEXT,
                    market_condition TEXT,
                    exit_reason TEXT,
                    stop_loss TEXT,
                    take_profit TEXT,
                    timestamp TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    protection_level TEXT DEFAULT 'FULL',
                    has_emergency_stop INTEGER DEFAULT 0,
                    emergency_stop_id INTEGER,
                    has_oco INTEGER DEFAULT 0,
                    oco_id TEXT
                )
            """
            )

            await cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT UNIQUE NOT NULL,
                    starting_balance TEXT,
                    ending_balance TEXT,
                    total_pnl TEXT,
                    total_trades INTEGER,
                    winning_trades INTEGER,
                    losing_trades INTEGER,
                    best_trade TEXT,
                    worst_trade TEXT,
                    data TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            await cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS market_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    price TEXT NOT NULL,
                    volume TEXT,
                    timestamp TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            await cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS performance_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric_name TEXT NOT NULL,
                    metric_value TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    metadata TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            await cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS circuit_breaker_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    is_open INTEGER NOT NULL DEFAULT 0,
                    triggered_at TEXT,
                    trigger_reason TEXT,
                    updated_at TEXT NOT NULL
                )
            """
            )

            await cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency_cache (
                    client_order_id TEXT PRIMARY KEY,
                    order_id INTEGER,
                    status TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    response_data TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            indexes = [
                "CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)",
                "CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)",
                "CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time)",
                "CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time)",
                "CREATE INDEX IF NOT EXISTS idx_trades_symbol_status ON trades(symbol, status)",
                "CREATE INDEX IF NOT EXISTS idx_daily_metrics_date ON daily_metrics(date)",
                "CREATE INDEX IF NOT EXISTS idx_market_data_symbol ON market_data(symbol)",
                "CREATE INDEX IF NOT EXISTS idx_market_data_timestamp ON market_data(timestamp)",
                "CREATE INDEX IF NOT EXISTS idx_performance_metrics_name ON performance_metrics(metric_name)",
                "CREATE INDEX IF NOT EXISTS idx_performance_metrics_timestamp ON performance_metrics(timestamp)",
                "CREATE INDEX IF NOT EXISTS idx_idempotency_timestamp ON idempotency_cache(timestamp)",
                "CREATE INDEX IF NOT EXISTS idx_idempotency_status ON idempotency_cache(status)",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_open_symbol ON trades(symbol) WHERE status = 'OPEN'",
            ]

            for index_sql in indexes:
                await cursor.execute(index_sql)

            await cursor.execute(
                """
                CREATE TRIGGER IF NOT EXISTS update_trades_timestamp
                AFTER UPDATE ON trades
                BEGIN
                    UPDATE trades SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
                END
            """
            )

            await cursor.execute(
                """
                CREATE TRIGGER IF NOT EXISTS update_daily_metrics_timestamp
                AFTER UPDATE ON daily_metrics
                BEGIN
                    UPDATE daily_metrics SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
                END
            """
            )

            await cursor.execute("PRAGMA table_info(trades)")
            columns = [col[1] for col in await cursor.fetchall()]

            if "protection_level" not in columns:
                await cursor.execute(
                    "ALTER TABLE trades ADD COLUMN protection_level TEXT DEFAULT 'FULL'"
                )

            if "has_emergency_stop" not in columns:
                await cursor.execute(
                    "ALTER TABLE trades ADD COLUMN has_emergency_stop INTEGER DEFAULT 0"
                )

            if "emergency_stop_id" not in columns:
                await cursor.execute(
                    "ALTER TABLE trades ADD COLUMN emergency_stop_id INTEGER"
                )

            await conn.commit()

        production("Database initialized with optimized schema", path=self.db_path)

    @track_component("db_handler", slow_threshold=100)
    async def save_trade(self, trade_data: dict[str, Any]) -> int | None:
        try:
            async with self.pool.acquire() as conn:
                cursor = await conn.cursor()

                await cursor.execute(
                    """
                    INSERT INTO trades (
                        symbol, entry_price, quantity, entry_time,
                        score, market_condition, stop_loss, take_profit,
                        timestamp, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        str(trade_data["symbol"]),
                        str(to_decimal(trade_data["entry_price"])),
                        str(to_decimal(trade_data["quantity"])),
                        trade_data.get("entry_time", datetime.now()).isoformat(),
                        str(to_decimal(trade_data.get("entry_score", 0))),
                        str(trade_data.get("market_condition", "unknown")),
                        str(to_decimal(trade_data.get("stop_loss", 0))),
                        str(to_decimal(trade_data.get("take_profit", 0))),
                        trade_data.get("timestamp", datetime.now()).isoformat(),
                        "OPEN",
                    ),
                )

                trade_id = cursor.lastrowid
                await conn.commit()

                return trade_id

        except Exception as e:
            error(
                "Failed to save trade",
                error=str(e),
                symbol=trade_data.get("symbol"),
            )
            await metrics.record_error("db_save_trade")
            return None

    @track_component("db_handler", slow_threshold=100)
    async def update_trade_exit(
        self,
        symbol: str,
        exit_price: float,
        exit_time: datetime,
        realized_pnl: float,
        exit_reason: str,
    ) -> bool:
        try:
            async with self.pool.acquire() as conn:
                cursor = await conn.cursor()

                await cursor.execute(
                    """
                    SELECT id FROM trades
                    WHERE symbol = ? AND status = 'OPEN'
                    ORDER BY id DESC
                    LIMIT 1
                """,
                    (str(symbol),),
                )

                result = await cursor.fetchone()
                if result:
                    trade_id = result[0]
                    await cursor.execute(
                        """
                        UPDATE trades
                        SET exit_price = ?, exit_time = ?, realized_pnl = ?,
                            status = 'CLOSED', exit_reason = ?
                        WHERE id = ?
                    """,
                        (
                            str(to_decimal(exit_price)),
                            exit_time.isoformat(),
                            str(to_decimal(realized_pnl)),
                            str(exit_reason),
                            int(trade_id),
                        ),
                    )

                    await conn.commit()

                    production(
                        "Trade closed",
                        symbol=symbol,
                        pnl=realized_pnl,
                        reason=exit_reason,
                        trade_id=trade_id,
                    )
                    return True
                else:
                    debug("No open trade found", symbol=symbol)
                    return False

        except Exception as e:
            error("Failed to update trade exit", error=str(e), symbol=symbol)
            await metrics.record_error("db_update_trade_exit")
            return False

    @track_component("db_handler", slow_threshold=50)
    async def update_trade_status(self, trade_id: int, status: str) -> bool:
        try:
            async with self.pool.acquire() as conn:
                cursor = await conn.cursor()

                await cursor.execute(
                    """
                    UPDATE trades SET status = ? WHERE id = ?
                """,
                    (str(status), int(trade_id)),
                )

                await conn.commit()
                updated = cursor.rowcount > 0

                if updated:
                    debug("Trade status updated", trade_id=trade_id, status=status)

                return updated

        except Exception as e:
            error("Failed to update trade status", error=str(e), trade_id=trade_id)
            await metrics.record_error("db_update_trade_status")
            return False

    @track_component("db_handler", slow_threshold=50)
    async def delete_trade_by_symbol(self, symbol: str) -> bool:
        try:
            async with self.pool.acquire() as conn:
                cursor = await conn.cursor()

                await cursor.execute(
                    "DELETE FROM trades WHERE symbol = ? AND status = 'OPEN'",
                    (str(symbol),),
                )

                deleted_count = cursor.rowcount
                await conn.commit()

                if deleted_count > 0:
                    production(
                        "Trade deleted from database",
                        symbol=symbol,
                        count=deleted_count,
                    )
                    return True
                else:
                    debug("No open trade found to delete", symbol=symbol)
                    return False

        except Exception as e:
            error("Failed to delete trade", error=str(e), symbol=symbol)
            await metrics.record_error("db_delete_trade")
            return False

    @track_component("db_handler", slow_threshold=50)
    async def get_open_trade_by_symbol(self, symbol: str) -> dict | None:
        try:
            async with self.pool.acquire() as conn:
                cursor = await conn.cursor()

                await cursor.execute(
                    """
                    SELECT id, symbol, entry_price, quantity, entry_time,
                           stop_loss, take_profit, score, market_condition, status
                    FROM trades
                    WHERE symbol = ? AND status = 'OPEN'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (str(symbol),),
                )

                result = await cursor.fetchone()
                if result:
                    trade_dict = {
                        "id": result[0],
                        "symbol": result[1],
                        "entry_price": result[2],
                        "quantity": result[3],
                        "entry_time": result[4],
                        "stop_loss": result[5],
                        "take_profit": result[6],
                        "score": result[7],
                        "market_condition": result[8],
                        "status": result[9],
                    }
                    return self._deserialize_trade(trade_dict)
                return None

        except Exception as e:
            error("Failed to get open trade", error=str(e), symbol=symbol)
            return None

    @track_component("db_handler", slow_threshold=100)
    async def get_open_trades(self) -> list[dict[str, Any]]:
        try:
            async with self.pool.acquire() as conn:
                conn.row_factory = aiosqlite.Row
                cursor = await conn.cursor()

                await cursor.execute(
                    """
                    SELECT * FROM trades
                    WHERE status = 'OPEN'
                    ORDER BY entry_time DESC
                """
                )

                rows = await cursor.fetchall()
                trades = [self._deserialize_trade(dict(row)) for row in rows]

                debug(
                    "get_open_trades",
                    count=len(trades),
                    symbols=[t["symbol"] for t in trades] if trades else [],
                )

                return trades

        except Exception as e:
            error("Failed to get open trades", error=str(e))
            await metrics.record_error("db_get_open_trades")
            return []

    @track_component("db_handler", slow_threshold=100)
    async def get_trades(
        self, limit: int = 100, symbol: str | None = None
    ) -> list[dict[str, Any]]:
        try:
            async with self.pool.acquire() as conn:
                conn.row_factory = aiosqlite.Row
                cursor = await conn.cursor()

                if symbol:
                    await cursor.execute(
                        """
                        SELECT * FROM trades
                        WHERE symbol = ?
                        ORDER BY id DESC
                        LIMIT ?
                    """,
                        (str(symbol), int(limit)),
                    )
                else:
                    await cursor.execute(
                        """
                        SELECT * FROM trades
                        ORDER BY id DESC
                        LIMIT ?
                    """,
                        (int(limit),),
                    )

                rows = await cursor.fetchall()
                trades = [self._deserialize_trade(dict(row)) for row in rows]
                return trades

        except Exception as e:
            error("Failed to get trades", error=str(e), limit=limit, symbol=symbol)
            await metrics.record_error("db_get_trades")
            return []

    @track_component("db_handler", slow_threshold=100)
    async def get_trade_history(
        self, hours: int = 24, symbol: str | None = None
    ) -> list[dict[str, Any]]:
        try:
            async with self.pool.acquire() as conn:
                conn.row_factory = aiosqlite.Row
                cursor = await conn.cursor()

                from datetime import datetime, timedelta

                cutoff_time = datetime.now() - timedelta(hours=hours)
                cutoff_str = cutoff_time.strftime("%Y-%m-%d %H:%M:%S")

                if symbol:
                    await cursor.execute(
                        """
                        SELECT * FROM trades
                        WHERE symbol = ? AND exit_time >= ?
                        ORDER BY exit_time DESC
                    """,
                        (str(symbol), cutoff_str),
                    )
                else:
                    await cursor.execute(
                        """
                        SELECT * FROM trades
                        WHERE exit_time >= ?
                        ORDER BY exit_time DESC
                    """,
                        (cutoff_str,),
                    )

                rows = await cursor.fetchall()
                trades = [self._deserialize_trade(dict(row)) for row in rows]
                return trades

        except Exception as e:
            error(
                "Failed to get trade history",
                error=str(e),
                hours=hours,
                symbol=symbol,
            )
            await metrics.record_error("db_get_trade_history")
            return []
