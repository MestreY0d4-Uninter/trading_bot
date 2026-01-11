import asyncio
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from shared.observability.flow_tracker import track_component
from shared.observability.logger import debug, error, production
from shared.observability.metrics import metrics
from shared.types.state import _GlobalState
from utils.validation_utils import validate_sql_identifier


class DatabaseMaintenanceMixin:
    def _schedule_maintenance(self):
        async def maintenance_worker():
            while True:
                try:
                    await asyncio.sleep(3600)
                    await self._auto_backup()
                    await self._optimize_database()
                except Exception as e:
                    error("Maintenance worker error", error=str(e))

        self._maintenance_task = asyncio.create_task(maintenance_worker())

    async def _auto_backup(self):
        if datetime.now() - self.last_backup_time >= timedelta(
            hours=self.backup_interval_hours
        ):
            backup_path = await self.create_backup()
            if backup_path:
                await self._cleanup_old_backups()
                self.last_backup_time = datetime.now()

    async def _cleanup_old_backups(self, keep_count: int = 7):
        try:
            backup_dir = Path(self.db_path).parent
            backup_files = list(backup_dir.glob(f"{Path(self.db_path).name}.backup_*"))

            if len(backup_files) > keep_count:
                backup_files.sort(key=lambda x: x.stat().st_mtime)
                for backup_file in backup_files[:-keep_count]:
                    backup_file.unlink()
                    debug("Old backup removed", path=str(backup_file))

        except Exception as e:
            error("Failed to cleanup old backups", error=str(e))

    async def _optimize_database(self):
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("PRAGMA optimize")
                await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                await conn.commit()
            debug("Database optimization completed")
        except Exception as e:
            error("Database optimization failed", error=str(e))

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
                        float(trade_data["entry_price"]),
                        float(trade_data["quantity"]),
                        trade_data.get("entry_time", datetime.now()).isoformat(),
                        float(trade_data.get("entry_score", 0)),
                        str(trade_data.get("market_condition", "unknown")),
                        float(trade_data.get("stop_loss", 0)),
                        float(trade_data.get("take_profit", 0)),
                        trade_data.get("timestamp", datetime.now()).isoformat(),
                        "OPEN",
                    ),
                )

                trade_id = cursor.lastrowid
                await conn.commit()

                production(
                    "Trade saved",
                    symbol=trade_data["symbol"],
                    price=trade_data["entry_price"],
                    trade_id=trade_id,
                )

                return trade_id

        except Exception as e:
            error(
                "Failed to save trade",
                error=str(e),
                symbol=trade_data.get("symbol"),
            )
            await metrics.record_error("db_save_trade")
            return None

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
                            float(exit_price),
                            exit_time.isoformat(),
                            float(realized_pnl),
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
                    return {
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
                return None

        except Exception as e:
            error("Failed to get open trade", error=str(e), symbol=symbol)
            return None

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
                trades = [dict(row) for row in rows]

                from shared.observability.logger import debug

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
                trades = [dict(row) for row in rows]
                return trades

        except Exception as e:
            error("Failed to get trades", error=str(e), limit=limit, symbol=symbol)
            await metrics.record_error("db_get_trades")
            return []

    @track_component("db_handler", slow_threshold=200)
    async def save_daily_metrics(self, metrics_data: dict[str, Any]) -> bool:
        try:
            from utils.decimal_math import dict_decimals_to_str

            async with self.pool.acquire() as conn:
                cursor = await conn.cursor()

                date_str = (
                    metrics_data["date"].isoformat()
                    if hasattr(metrics_data["date"], "isoformat")
                    else str(metrics_data["date"])
                )

                await cursor.execute(
                    """
                    INSERT OR REPLACE INTO daily_metrics (
                        date, starting_balance, ending_balance, total_pnl,
                        total_trades, winning_trades, losing_trades,
                        best_trade, worst_trade, data
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        date_str,
                        float(metrics_data.get("starting_balance", 0)),
                        float(metrics_data.get("ending_balance", 0)),
                        float(metrics_data.get("total_pnl", 0)),
                        int(metrics_data.get("total_trades", 0)),
                        int(metrics_data.get("winning_trades", 0)),
                        int(metrics_data.get("losing_trades", 0)),
                        float(metrics_data.get("best_trade", 0)),
                        float(metrics_data.get("worst_trade", 0)),
                        json.dumps(dict_decimals_to_str(metrics_data.get("data", {}))),
                    ),
                )

                await conn.commit()

                production(
                    "Daily metrics saved",
                    date=date_str,
                    pnl=metrics_data.get("total_pnl", 0),
                )

                return True

        except Exception as e:
            error("Failed to save daily metrics", error=str(e))
            await metrics.record_error("db_save_daily_metrics")
            return False

    async def get_daily_metrics(self, date: str | None = None) -> dict[str, Any] | None:
        try:
            async with self.pool.acquire() as conn:
                conn.row_factory = aiosqlite.Row
                cursor = await conn.cursor()

                if date:
                    await cursor.execute(
                        """
                        SELECT * FROM daily_metrics WHERE date = ?
                    """,
                        (str(date),),
                    )
                else:
                    await cursor.execute(
                        """
                        SELECT * FROM daily_metrics
                        ORDER BY date DESC
                        LIMIT 1
                    """
                    )

                row = await cursor.fetchone()

                if row:
                    result = dict(row)
                    try:
                        result["data"] = json.loads(result.get("data", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        result["data"] = {}
                    return result
                return None

        except Exception as e:
            error("Failed to get daily metrics", error=str(e), date=date)
            await metrics.record_error("db_get_daily_metrics")
            return None

    async def get_performance_summary(self, days: int = 30) -> dict[str, Any]:
        try:
            async with self.pool.acquire() as conn:
                cursor = await conn.cursor()

                since_date = (datetime.now() - timedelta(days=days)).strftime(
                    "%Y-%m-%d"
                )

                await cursor.execute(
                    """
                    SELECT
                        COUNT(*) as total_trades,
                        SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                        SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
                        SUM(realized_pnl) as total_pnl,
                        AVG(realized_pnl) as avg_pnl,
                        MAX(realized_pnl) as best_trade,
                        MIN(realized_pnl) as worst_trade
                    FROM trades
                    WHERE status = 'CLOSED'
                    AND DATE(exit_time) >= ?
                """,
                    (since_date,),
                )

                row = await cursor.fetchone()

                if row and row[0]:
                    return {
                        "total_trades": row[0] or 0,
                        "winning_trades": row[1] or 0,
                        "losing_trades": row[2] or 0,
                        "total_pnl": float(row[3] or 0),
                        "avg_pnl": float(row[4] or 0),
                        "best_trade": float(row[5] or 0),
                        "worst_trade": float(row[6] or 0),
                        "win_rate": _GlobalState.calculate_win_rate(
                            int(row[1]), int(row[0])
                        ),
                    }

                return {
                    "total_trades": 0,
                    "winning_trades": 0,
                    "losing_trades": 0,
                    "total_pnl": 0.0,
                    "avg_pnl": 0.0,
                    "best_trade": 0.0,
                    "worst_trade": 0.0,
                    "win_rate": 0.0,
                }

        except Exception as e:
            error("Failed to get performance summary", error=str(e), days=days)
            await metrics.record_error("db_get_performance_summary")
            return {}

    async def get_daily_realized_pnl(self, date_str: str | None = None) -> float:
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")

        try:
            async with self.pool.acquire() as conn:
                cursor = await conn.cursor()

                await cursor.execute(
                    """
                    SELECT COALESCE(SUM(realized_pnl), 0) as daily_realized_pnl
                    FROM trades
                    WHERE status = 'CLOSED'
                    AND (date(exit_time) = ? OR (exit_time IS NULL AND date(entry_time) = ?))
                """,
                    (str(date_str), str(date_str)),
                )

                result = await cursor.fetchone()
                return float(result[0]) if result and result[0] is not None else 0.0

        except Exception as e:
            error("Failed to get daily realized P&L", error=str(e), date=date_str)
            return 0.0

    async def save_market_data(
        self, symbol: str, price: float, volume: float | None = None
    ) -> bool:
        try:
            async with self.pool.acquire() as conn:
                cursor = await conn.cursor()

                await cursor.execute(
                    """
                    INSERT INTO market_data (symbol, price, volume, timestamp)
                    VALUES (?, ?, ?, ?)
                """,
                    (
                        str(symbol),
                        float(price),
                        float(volume) if volume is not None else None,
                        datetime.now().isoformat(),
                    ),
                )

                await conn.commit()
                return True

        except Exception as e:
            error("Failed to save market data", error=str(e), symbol=symbol)
            await metrics.record_error("db_save_market_data")
            return False

    @track_component("db_handler", slow_threshold=100)
    async def save_performance_metric(
        self, metric_name: str, metric_value: float, metadata: dict | None = None
    ) -> bool:
        try:
            async with self.pool.acquire() as conn:
                cursor = await conn.cursor()

                await cursor.execute(
                    """
                    INSERT INTO performance_metrics (metric_name, metric_value, timestamp, metadata)
                    VALUES (?, ?, ?, ?)
                """,
                    (
                        str(metric_name),
                        float(metric_value),
                        datetime.now().isoformat(),
                        json.dumps(metadata) if metadata else None,
                    ),
                )

                await conn.commit()
                return True

        except Exception as e:
            error(
                "Failed to save performance metric",
                error=str(e),
                metric=metric_name,
            )
            await metrics.record_error("db_save_performance_metric")
            return False

    async def vacuum_database(self) -> bool:
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("VACUUM")
                await conn.commit()

            production("Database vacuum completed")
            return True

        except Exception as e:
            error("Failed to vacuum database", error=str(e))
            await metrics.record_error("db_vacuum")
            return False

    async def create_backup(self) -> str | None:
        backup_path = (
            f"{self.db_path}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )

        try:
            await asyncio.to_thread(shutil.copy2, self.db_path, backup_path)

            async with aiosqlite.connect(backup_path) as backup_conn:
                cursor = await backup_conn.cursor()
                await cursor.execute("PRAGMA integrity_check")
                result = await cursor.fetchone()
                if result[0] != "ok":
                    await asyncio.to_thread(Path(backup_path).unlink)
                    error("Backup integrity check failed")
                    return None

            production("Database backup created", path=backup_path)
            return backup_path

        except Exception as e:
            error("Failed to create database backup", error=str(e))
            await metrics.record_error("db_backup")
            if Path(backup_path).exists():
                try:
                    await asyncio.to_thread(Path(backup_path).unlink)
                except Exception:
                    pass
            return None

    @track_component("db_handler", slow_threshold=200)
    async def cleanup_old_data(self, days_to_keep: int = 90) -> bool:
        try:
            async with self.pool.acquire() as conn:
                cursor = await conn.cursor()

                cutoff_date = (datetime.now() - timedelta(days=days_to_keep)).strftime(
                    "%Y-%m-%d"
                )

                await cursor.execute(
                    """
                    DELETE FROM trades
                    WHERE status = 'CLOSED'
                    AND DATE(exit_time) < ?
                """,
                    (cutoff_date,),
                )

                deleted_trades = cursor.rowcount

                await cursor.execute(
                    """
                    DELETE FROM daily_metrics
                    WHERE date < ?
                """,
                    (cutoff_date,),
                )

                deleted_metrics = cursor.rowcount

                await cursor.execute(
                    """
                    DELETE FROM market_data
                    WHERE DATE(timestamp) < ?
                """,
                    (cutoff_date,),
                )

                deleted_market_data = cursor.rowcount

                await cursor.execute(
                    """
                    DELETE FROM performance_metrics
                    WHERE DATE(timestamp) < ?
                """,
                    (cutoff_date,),
                )

                deleted_performance = cursor.rowcount

                await conn.commit()

                production(
                    "Old data cleaned up",
                    trades_deleted=deleted_trades,
                    metrics_deleted=deleted_metrics,
                    market_data_deleted=deleted_market_data,
                    performance_deleted=deleted_performance,
                )

                return True

        except Exception as e:
            error("Failed to cleanup old data", error=str(e))
            await metrics.record_error("db_cleanup")
            return False

    async def get_database_stats(self) -> dict[str, Any]:
        try:
            async with self.pool.acquire() as conn:
                cursor = await conn.cursor()

                stats = {}

                valid_tables = {
                    "trades",
                    "daily_metrics",
                    "market_data",
                    "performance_metrics",
                }
                for table in valid_tables:
                    if not validate_sql_identifier(table, valid_tables):
                        error("Invalid table name", table=table)
                        continue
                    query = f"SELECT COUNT(*) FROM {table}"
                    await cursor.execute(query)
                    result = await cursor.fetchone()
                    stats[f"{table}_count"] = result[0]

                await cursor.execute("PRAGMA page_count")
                page_count = (await cursor.fetchone())[0]

                await cursor.execute("PRAGMA page_size")
                page_size = (await cursor.fetchone())[0]

                stats["database_size_bytes"] = page_count * page_size
                stats["database_size_mb"] = stats["database_size_bytes"] / (1024 * 1024)

                await cursor.execute("PRAGMA freelist_count")
                stats["free_pages"] = (await cursor.fetchone())[0]

                return stats

        except Exception as e:
            error("Failed to get database stats", error=str(e))
            return {}

    async def validate_database_integrity(self) -> dict[str, Any]:
        try:
            async with self.pool.acquire() as conn:
                cursor = await conn.cursor()
                validation_results: dict[str, Any] = {
                    "integrity_check": False,
                    "consistency_check": False,
                    "open_positions_count": 0,
                    "total_trades": 0,
                    "status_breakdown": {},
                    "issues": [],
                }

                await cursor.execute("PRAGMA integrity_check")
                integrity_result = (await cursor.fetchone())[0]
                validation_results["integrity_check"] = integrity_result == "ok"

                if integrity_result != "ok":
                    validation_results["issues"].append(
                        f"SQLite integrity: {integrity_result}"
                    )

                await cursor.execute("SELECT COUNT(*) FROM trades")
                validation_results["total_trades"] = (await cursor.fetchone())[0]

                await cursor.execute(
                    "SELECT status, COUNT(*) FROM trades GROUP BY status"
                )
                for status, count in await cursor.fetchall():
                    validation_results["status_breakdown"][status] = count

                validation_results["open_positions_count"] = validation_results[
                    "status_breakdown"
                ].get("OPEN", 0)

                consistency_issues = []

                await cursor.execute(
                    "SELECT COUNT(*) FROM trades WHERE status NOT IN ('OPEN', 'CLOSED')"
                )
                invalid_status = (await cursor.fetchone())[0]
                if invalid_status > 0:
                    consistency_issues.append(
                        f"{invalid_status} trades with invalid status"
                    )

                await cursor.execute(
                    "SELECT COUNT(*) FROM trades WHERE symbol IS NULL OR entry_price IS NULL OR quantity IS NULL"
                )
                missing_fields = (await cursor.fetchone())[0]
                if missing_fields > 0:
                    consistency_issues.append(
                        f"{missing_fields} trades with missing required fields"
                    )

                await cursor.execute(
                    "SELECT COUNT(*) FROM trades WHERE status = 'OPEN' AND (stop_loss IS NULL OR take_profit IS NULL)"
                )
                incomplete_open = (await cursor.fetchone())[0]
                if incomplete_open > 0:
                    consistency_issues.append(
                        f"{incomplete_open} OPEN trades missing stop_loss/take_profit"
                    )

                validation_results["consistency_check"] = len(consistency_issues) == 0
                validation_results["issues"].extend(consistency_issues)

                from shared.observability.logger import production, warning

                if (
                    validation_results["integrity_check"]
                    and validation_results["consistency_check"]
                ):
                    production(
                        f"✅ Database validation successful: {validation_results['total_trades']} trades, {validation_results['open_positions_count']} OPEN"
                    )
                else:
                    warning(
                        f"⚠️ Database validation issues found: {validation_results['issues']}"
                    )

                return validation_results

        except Exception as e:
            error("Failed to validate database integrity", error=str(e))
            return {
                "integrity_check": False,
                "consistency_check": False,
                "issues": [str(e)],
            }

    async def close(self):
        try:
            if self._maintenance_task:
                self._maintenance_task.cancel()
                try:
                    await self._maintenance_task
                except asyncio.CancelledError:
                    pass

            await self.pool.close()
            production("Database connections closed")
        except Exception as e:
            error("Error closing database", error=str(e))
