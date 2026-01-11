from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any


class DatabaseClient:
    _instance: DatabaseClient | None = None
    _initialized: bool = False

    def __new__(cls, db_path: Path | str | None = None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, db_path: Path | str | None = None):
        if self._initialized:
            return

        if db_path is None:
            project_root = Path(__file__).parent.parent.parent
            db_path = project_root / "data" / "trading_bot.db"

        self.db_path = Path(db_path)
        self._initialized = True

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _deserialize_trade(self, row: sqlite3.Row) -> dict[str, Any]:
        trade = dict(row)

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
            if field in trade and trade[field] is not None:
                try:
                    trade[field] = str(Decimal(str(trade[field])))
                except Exception:
                    pass

        return trade

    def get_trades(
        self,
        status: str | None = None,
        symbol: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()

        query = "SELECT * FROM trades WHERE 1=1"
        params: list[Any] = []

        if status:
            query += " AND status = ?"
            params.append(status)

        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        try:
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [self._deserialize_trade(row) for row in rows]
        finally:
            conn.close()

    def get_trade_by_id(self, trade_id: int) -> dict[str, Any] | None:
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
            row = cursor.fetchone()
            return self._deserialize_trade(row) if row else None
        finally:
            conn.close()

    def get_daily_metrics(self, days: int = 30) -> list[dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                """
                SELECT * FROM daily_metrics
                ORDER BY date DESC
                LIMIT ?
                """,
                (days,),
            )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_market_data(
        self, symbol: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()

        query = "SELECT * FROM market_data WHERE 1=1"
        params: list[Any] = []

        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        try:
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def get_performance_metrics(self) -> dict[str, Any]:
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                """
                SELECT * FROM performance_metrics
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
            row = cursor.fetchone()
            return dict(row) if row else {}
        except sqlite3.OperationalError:
            return {}
        finally:
            conn.close()

    def get_circuit_breaker_state(self) -> dict[str, Any] | None:
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                """
                SELECT * FROM circuit_breaker_state
                ORDER BY updated_at DESC
                LIMIT 1
                """
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.OperationalError:
            return None
        finally:
            conn.close()

    def get_open_trades(self) -> list[dict[str, Any]]:
        return self.get_trades(status="OPEN")

    def get_closed_trades(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.get_trades(status="CLOSED", limit=limit)

    def get_problem_trades(self, hours: int = 24) -> list[dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                """
                SELECT * FROM trades
                WHERE (
                    CAST(realized_pnl AS REAL) < 0
                    OR exit_reason LIKE '%emergency%'
                    OR exit_reason LIKE '%stop%'
                    OR status != 'CLOSED'
                )
                AND datetime(created_at) >= datetime('now', '-' || ? || ' hours')
                ORDER BY created_at DESC
                """,
                (hours,),
            )
            rows = cursor.fetchall()
            return [self._deserialize_trade(row) for row in rows]
        finally:
            conn.close()

    def get_symbol_performance(self, days: int = 7) -> dict[str, dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                """
                SELECT
                    symbol,
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN CAST(realized_pnl AS REAL) > 0 THEN 1 ELSE 0 END) as winning_trades,
                    SUM(CASE WHEN CAST(realized_pnl AS REAL) < 0 THEN 1 ELSE 0 END) as losing_trades,
                    AVG(CAST(realized_pnl AS REAL)) as avg_pnl,
                    MAX(CAST(realized_pnl AS REAL)) as best_trade,
                    MIN(CAST(realized_pnl AS REAL)) as worst_trade,
                    SUM(CAST(realized_pnl AS REAL)) as total_pnl
                FROM trades
                WHERE datetime(created_at) >= datetime('now', '-' || ? || ' days')
                AND status = 'CLOSED'
                GROUP BY symbol
                """,
                (days,),
            )
            rows = cursor.fetchall()

            performance: dict[str, dict[str, Any]] = {}
            for row in rows:
                row_dict = dict(row)
                symbol = row_dict.pop("symbol")
                total = row_dict["total_trades"]
                winning = row_dict["winning_trades"]

                row_dict["win_rate"] = (
                    round((winning / total) * 100, 2) if total > 0 else 0.0
                )

                performance[symbol] = row_dict

            return performance
        finally:
            conn.close()

    def get_trades_count_by_status(self) -> dict[str, int]:
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                """
                SELECT status, COUNT(*) as count
                FROM trades
                GROUP BY status
                """
            )
            rows = cursor.fetchall()
            return {row["status"]: row["count"] for row in rows}
        finally:
            conn.close()

    def execute_query(
        self, query: str, params: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
