import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from shared.observability.flow_tracker import track_component
from shared.observability.logger import debug, error, production, warning


class IdempotencyHandler:
    """Gerencia idempotência de ordens com persistência para prevenir duplicações após restart"""

    def __init__(self, db_handler=None) -> None:
        self._db_handler = db_handler
        self._order_cache: dict[str, dict] = {}
        self._oco_cache: dict[str, dict] = {}
        self._cleanup_interval = 3600
        self._cleanup_task: asyncio.Task | None = None
        self._cache_ttl_hours = 24

    @track_component("idempotency_handler", slow_threshold=10)
    def generate_order_id(self, side: str, symbol: str) -> str:
        """Gera ID único idempotente para ordem"""
        timestamp = int(datetime.now().timestamp())
        unique_id = uuid.uuid4().hex[:8]
        return f"{side.lower()}_{symbol.lower()}_{timestamp}_{unique_id}"

    @track_component("idempotency_handler", slow_threshold=10)
    def generate_oco_id(self, symbol: str) -> str:
        """Gera ID único para OCO"""
        timestamp = int(datetime.now().timestamp())
        unique_id = uuid.uuid4().hex[:8]
        return f"oco_{symbol.lower()}_{timestamp}_{unique_id}"

    @track_component("idempotency_handler", slow_threshold=100)
    async def load_cache_from_db(self):
        """Restaurar cache após restart (últimas 24h)"""
        if not self._db_handler:
            debug("DB handler not available - idempotency cache não será restaurado")
            return

        try:
            cutoff = datetime.now() - timedelta(hours=self._cache_ttl_hours)
            async with self._db_handler.pool.acquire() as conn:
                cursor = await conn.cursor()
                await cursor.execute(
                    "SELECT * FROM idempotency_cache WHERE timestamp > ?",
                    (cutoff.timestamp(),),
                )

                rows = await cursor.fetchall()
                column_names = [desc[0] for desc in cursor.description]

                for row in rows:
                    row_dict = dict(zip(column_names, row, strict=True))
                    client_order_id = row_dict["client_order_id"]

                    self._order_cache[client_order_id] = {
                        "order_id": row_dict["order_id"],
                        "status": row_dict["status"],
                        "timestamp": datetime.fromtimestamp(row_dict["timestamp"]),
                        "response": (
                            json.loads(row_dict["response_data"])
                            if row_dict.get("response_data")
                            else None
                        ),
                        "symbol": row_dict["symbol"],
                        "operation_type": row_dict["operation_type"],
                    }

                production(
                    "Idempotency cache restored from database",
                    entries_loaded=len(rows),
                    cache_ttl_hours=self._cache_ttl_hours,
                )

        except Exception as e:
            error("Failed to restore idempotency cache from DB", error=str(e))

    @track_component("idempotency_handler", slow_threshold=5)
    async def _save_to_db(
        self,
        client_order_id: str,
        status: str,
        symbol: str,
        operation_type: str,
        order_id: int | None = None,
        response_data: dict | None = None,
    ):
        """Salvar entrada de idempotência no banco"""
        if not self._db_handler:
            return

        try:
            async with self._db_handler.pool.acquire() as conn:
                cursor = await conn.cursor()
                await cursor.execute(
                    """
                    INSERT OR REPLACE INTO idempotency_cache
                    (client_order_id, order_id, status, symbol, operation_type, timestamp, response_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        client_order_id,
                        order_id,
                        status,
                        symbol,
                        operation_type,
                        datetime.now().timestamp(),
                        json.dumps(response_data) if response_data else None,
                    ),
                )
                await conn.commit()

        except Exception as e:
            error(
                "Failed to save idempotency entry to DB",
                error=str(e),
                client_order_id=client_order_id,
            )

    @track_component("idempotency_handler", slow_threshold=5)
    def mark_success(self, client_order_id: str, order_response: dict):
        """Registra ordem executada com sucesso"""
        self._order_cache[client_order_id] = {
            "order_id": order_response.get("orderId"),
            "status": "FILLED",
            "timestamp": datetime.now(),
        }

    @track_component("idempotency_handler", slow_threshold=5)
    def mark_oco_success(self, list_client_order_id: str, oco_response: dict):
        """Registra OCO executada com sucesso"""
        self._oco_cache[list_client_order_id] = {
            "order_list_id": oco_response.get("orderListId"),
            "status": "EXECUTING",
            "timestamp": datetime.now(),
        }

    def was_executed(self, operation_id: str) -> bool:
        return operation_id in self._order_cache

    def get_cached_result(self, operation_id: str) -> dict | None:
        cached = self._order_cache.get(operation_id)
        if cached:
            return cached.get("response")
        return None

    async def _check_db_for_operation(self, operation_id: str) -> dict | None:
        """Verificar DB para operação (proteção após restart)"""
        if not self._db_handler:
            return None

        try:
            async with self._db_handler.pool.acquire() as conn:
                cursor = await conn.cursor()
                await cursor.execute(
                    "SELECT * FROM idempotency_cache WHERE client_order_id = ?",
                    (operation_id,),
                )

                row = await cursor.fetchone()
                if row:
                    column_names = [desc[0] for desc in cursor.description]
                    row_dict = dict(zip(column_names, row, strict=True))

                    if row_dict["status"] in ("FILLED", "EXECUTED"):
                        return (
                            json.loads(row_dict["response_data"])
                            if row_dict.get("response_data")
                            else {"orderId": row_dict["order_id"]}
                        )

        except Exception as e:
            error("Failed to check DB for operation", error=str(e))

        return None

    @track_component("idempotency_handler", slow_threshold=100)
    async def execute_with_idempotency(
        self,
        operation_id: str,
        operation: Callable[[], Awaitable[Any]],
        symbol: str,
        operation_type: str,
    ) -> dict | None:
        # 1. Verificar cache em memória (rápido)
        if self.was_executed(operation_id):
            cached = self.get_cached_result(operation_id)
            warning(
                "Duplicate operation detected in memory cache",
                operation_id=operation_id,
                symbol=symbol,
                operation_type=operation_type,
            )
            return cached

        # 2. Verificar DB (proteção após restart)
        db_result = await self._check_db_for_operation(operation_id)
        if db_result:
            warning(
                "Duplicate operation detected in DB after restart",
                operation_id=operation_id,
                symbol=symbol,
                operation_type=operation_type,
            )
            # Restaurar para cache em memória
            self._order_cache[operation_id] = {
                "order_id": db_result.get("orderId"),
                "status": "FILLED",
                "timestamp": datetime.now(),
                "response": db_result,
                "symbol": symbol,
                "operation_type": operation_type,
            }
            return db_result

        # 3. Marcar como PENDING no DB ANTES de executar
        await self._save_to_db(
            client_order_id=operation_id,
            status="PENDING",
            symbol=symbol,
            operation_type=operation_type,
        )

        debug(
            "Executing operation with idempotency",
            operation_id=operation_id,
            symbol=symbol,
            operation_type=operation_type,
        )

        # 4. Executar operação
        try:
            result = await operation()

            if result:
                # 5. Marcar como FILLED/EXECUTED no DB
                await self._save_to_db(
                    client_order_id=operation_id,
                    status=result.get("status", "EXECUTED"),
                    symbol=symbol,
                    operation_type=operation_type,
                    order_id=result.get("orderId"),
                    response_data=result,
                )

                # 6. Atualizar cache em memória
                self._order_cache[operation_id] = {
                    "order_id": result.get("orderId"),
                    "status": result.get("status", "EXECUTED"),
                    "timestamp": datetime.now(),
                    "response": result,
                    "symbol": symbol,
                    "operation_type": operation_type,
                }

                production(
                    "Operation executed and cached (memory + DB)",
                    operation_id=operation_id,
                    symbol=symbol,
                    operation_type=operation_type,
                    order_id=result.get("orderId"),
                )

            return result

        except Exception as e:
            # 7. Marcar como FAILED no DB
            await self._save_to_db(
                client_order_id=operation_id,
                status="FAILED",
                symbol=symbol,
                operation_type=operation_type,
                response_data={"error": str(e)},
            )
            raise

    @track_component("idempotency_handler", slow_threshold=10)
    async def cleanup_old_entries(self):
        """Remove entradas antigas do cache (>24h) - memória e DB"""
        cutoff = datetime.now() - timedelta(hours=self._cache_ttl_hours)
        initial_count = len(self._order_cache) + len(self._oco_cache)

        # Limpar cache em memória
        self._order_cache = {
            k: v for k, v in self._order_cache.items() if v["timestamp"] > cutoff
        }
        self._oco_cache = {
            k: v for k, v in self._oco_cache.items() if v["timestamp"] > cutoff
        }

        memory_removed = initial_count - (len(self._order_cache) + len(self._oco_cache))

        # Limpar DB
        db_removed = 0
        if self._db_handler:
            try:
                async with self._db_handler.pool.acquire() as conn:
                    cursor = await conn.cursor()
                    await cursor.execute(
                        "DELETE FROM idempotency_cache WHERE timestamp < ?",
                        (cutoff.timestamp(),),
                    )
                    db_removed = cursor.rowcount
                    await conn.commit()

            except Exception as e:
                error("Failed to cleanup old idempotency entries from DB", error=str(e))

        if memory_removed > 0 or db_removed > 0:
            debug(
                "Idempotency cache cleanup completed",
                memory_removed=memory_removed,
                db_removed=db_removed,
                memory_remaining=len(self._order_cache) + len(self._oco_cache),
                ttl_hours=self._cache_ttl_hours,
            )

    async def _periodic_cleanup(self):
        """Task periódica de cleanup do cache"""
        while True:
            try:
                await asyncio.sleep(self._cleanup_interval)
                await self.cleanup_old_entries()
            except asyncio.CancelledError:
                debug("Idempotency cleanup task cancelled")
                break
            except Exception as e:
                warning("Error in periodic cleanup", error=str(e))

    def start_cleanup_task(self):
        """Inicia task periódica de cleanup"""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
            production(
                "Idempotency cleanup task started",
                interval_seconds=self._cleanup_interval,
                ttl_hours=self._cache_ttl_hours,
            )

    async def stop_cleanup_task(self):
        """Para task de cleanup gracefully"""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            production("Idempotency cleanup task stopped")
