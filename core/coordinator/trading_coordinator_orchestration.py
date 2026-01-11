import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any

from core.engine.trading_loop import TradingLoop
from core.market.market_updater import MarketUpdater
from core.market.websocket_trading_loop import WebSocketTradingLoop
from core.position.position_manager import PositionManager
from shared.observability.flow_tracker import track_component
from shared.observability.logger import debug, error, production, warning
from shared.observability.metrics import metrics
from shared.types.state import state
from utils.decimal_math import ROUND_HALF_UP, InvalidOperation, to_decimal
from utils.validation_utils import is_numeric_valid

TRADING_STATE_CHECKS = {
    "client_ready": lambda self: self.coordinator.client
    and hasattr(self.coordinator.client, "ping"),
    "balance_valid": lambda self: self.coordinator.risk_manager
    and self.coordinator.risk_manager.current_balance > 0,
    "positions_loaded": lambda self: self.coordinator.position_manager
    and hasattr(self.coordinator.position_manager, "get_all_positions"),
    "risk_limits_ok": lambda self: self.coordinator.risk_manager
    and hasattr(self.coordinator.risk_manager, "can_trade"),
}


class TradingCoordinatorOrchestration:
    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator

    def _validate_trading_state(self) -> dict[str, bool]:
        validation_results = {}
        for check_name, check_func in TRADING_STATE_CHECKS.items():
            try:
                validation_results[check_name] = check_func(self)
            except Exception as e:
                warning(f"Erro na validação {check_name}", error=str(e))
                validation_results[check_name] = False
        return validation_results

    @track_component("trading_coordinator")
    async def run(self):
        production("Iniciando coordenação de trading...")

        if not TradingLoop or not MarketUpdater:
            error("CRÍTICO: Componentes de trading não disponíveis")
            raise RuntimeError(
                "Componentes críticos TradingLoop/MarketUpdater ausentes"
            )

        self.coordinator.running = True
        await self.coordinator.metrics_scheduler.start()

        try:
            trading_loop = self._create_trading_loop()
            market_updater = MarketUpdater(self.coordinator)
            self._create_tasks(trading_loop, market_updater)

            await self._wait_for_shutdown_or_completion()

        except Exception as e:
            error("Erro na execução do coordenador", error=str(e))
            raise
        finally:
            self.coordinator.running = False
            await self.coordinator.lifecycle.shutdown()

    def _create_trading_loop(self):
        market_data_source = self.coordinator.config.get("data_sources", {}).get(
            "market_data_source", "rest"
        )
        production(f"Market data source: {market_data_source}")

        if market_data_source == "websocket" and self.coordinator.websocket_manager:
            try:
                loop = WebSocketTradingLoop(
                    self.coordinator, self.coordinator.websocket_manager
                )
                production("Using WebSocket Trading Loop")
                return loop
            except Exception as e:
                error(f"Erro WebSocket loop: {e}")

        production("Using REST Trading Loop")
        return TradingLoop(self.coordinator)

    def _create_tasks(self, trading_loop, market_updater) -> None:
        self.coordinator.tasks = [
            asyncio.create_task(trading_loop.run(), name="trading_loop"),
            asyncio.create_task(market_updater.run(), name="market_updater"),
            asyncio.create_task(
                self.coordinator.unified_monitor.run(), name="unified_monitor"
            ),
            asyncio.create_task(
                self.coordinator.performance_monitor.run(), name="performance_monitor"
            ),
        ]
        production("Tasks criadas: trading, market, monitor, performance")

    async def _wait_for_shutdown_or_completion(self) -> None:
        shutdown_task = asyncio.create_task(
            self.coordinator.shutdown_event.wait(), name="shutdown_waiter"
        )
        all_tasks = self.coordinator.tasks + [shutdown_task]

        done, pending = await asyncio.wait(
            all_tasks, return_when=asyncio.FIRST_COMPLETED
        )

        production("Shutdown ou task completou")
        await self._cancel_pending_tasks(pending)
        self._log_completed_task_errors(done)

    async def _cancel_pending_tasks(self, pending: set) -> None:
        for task in pending:
            task.cancel()

        if pending:
            try:
                async with asyncio.timeout(5.0):
                    await asyncio.gather(*pending, return_exceptions=True)
            except TimeoutError:
                production("Timeout cancelando tasks")

    def _log_completed_task_errors(self, done: set) -> None:
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, asyncio.CancelledError):
                error("Task erro", task=task.get_name(), error=str(exc))

    async def get_status(self) -> dict:
        try:
            trading_state = self._validate_trading_state()

            if not self.coordinator.risk_manager:
                return self._build_uninitialized_status(trading_state)

            account = await self._get_validated_account()
            all_positions = await self._get_validated_positions()
            risk_status = await self.coordinator.risk_manager.get_status()

            position_stats = await self._calculate_position_stats(all_positions)

            return self._build_full_status(
                trading_state, account, all_positions, risk_status, position_stats
            )

        except Exception as e:
            error("CRÍTICO: Erro ao obter status", error=str(e))
            return self._build_error_status(str(e))

    def _build_uninitialized_status(self, trading_state: dict) -> dict:
        return {
            "running": False,
            "last_update": datetime.now(),
            "error": "Sistema não inicializado",
            "trading_state": trading_state,
        }

    def _build_error_status(self, error_msg: str) -> dict:
        return {
            "running": False,
            "last_update": datetime.now(),
            "error": f"Status unavailable: {error_msg}",
            "health": {"system_error": True},
        }

    async def _get_validated_account(self) -> dict:
        account = await state.get_account()
        if not self.coordinator.validators.validate_financial_data(account, "conta"):
            warning("Dados de conta inválidos no status")
            return {"balance": 0, "equity": 0}
        return account

    async def _get_validated_positions(self) -> dict:
        all_positions = await state.get_all_positions()
        if not isinstance(all_positions, dict):
            warning("Dados de posições inválidos no status")
            return {}
        return all_positions

    async def _calculate_position_stats(self, positions: dict) -> dict:
        total_pnl = 0.0
        valid_count = 0
        details = []

        for symbol, position in positions.items():
            result = self._process_position_for_status(symbol, position)
            if result:
                total_pnl += result["pnl"]
                valid_count += 1
                details.append(result)

        if not is_numeric_valid(total_pnl):
            total_pnl = 0.0

        return {"pnl": total_pnl, "valid_count": valid_count, "details": details}

    def _process_position_for_status(self, symbol: str, position: dict) -> dict | None:
        if not self.coordinator.validators.validate_financial_data(
            position, f"posição {symbol}", ["entry_price", "quantity"]
        ):
            return None

        current_price = position.get("current_price", position.get("entry_price", 0))
        entry_price = position.get("entry_price", 0)
        quantity = position.get("quantity", 0)

        if not isinstance(current_price, (int, float, Decimal)) or current_price <= 0:
            current_price = entry_price

        if not all(is_numeric_valid(v) for v in [current_price, entry_price, quantity]):
            return None

        try:
            current_d = to_decimal(current_price)
            entry_d = to_decimal(entry_price)
            quantity_d = to_decimal(quantity)

            pnl = float(
                ((current_d - entry_d) * quantity_d).quantize(
                    Decimal("0.00000001"), rounding=ROUND_HALF_UP
                )
            )
            pnl_pct = PositionManager.calculate_pnl_percentage(entry_d, current_d)

            if not is_numeric_valid(pnl):
                return None

            return {
                "symbol": symbol,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "current_price": current_price,
                "entry_price": entry_price,
                "quantity": quantity,
            }

        except (InvalidOperation, ValueError):
            return None

    async def _build_full_status(
        self,
        trading_state: dict,
        account: dict,
        positions: dict,
        risk_status: dict,
        position_stats: dict,
    ) -> dict:
        try:
            summary = await metrics.get_summary()
        except Exception:
            summary = {}

        return {
            "running": self.coordinator.running,
            "last_update": datetime.now(),
            "start_time": self.coordinator.start_time,
            "uptime_hours": (
                datetime.now() - self.coordinator.start_time
            ).total_seconds()
            / 3600,
            "account_balance": {
                "USDT": account.get("balance", 0),
                "total_equity": account.get("equity", 0),
            },
            "positions": {
                "open": len(positions),
                "valid": position_stats["valid_count"],
                "total_pnl": position_stats["pnl"],
                "details": position_stats["details"][:10],
            },
            "daily_stats": {
                "pnl": await state.get_metric("daily_pnl") or 0,
                "pnl_pct": await state.get_metric("daily_pnl_pct") or 0,
                "trades": await state.get_metric("total_trades") or 0,
            },
            "risk_status": risk_status or {},
            "performance": summary or {},
            "trading_state": trading_state,
            "health": {
                "components_initialized": all(
                    [
                        self.coordinator.client,
                        self.coordinator.executor,
                        self.coordinator.risk_manager,
                        self.coordinator.position_manager,
                        self.coordinator.db,
                    ]
                ),
                "invalid_positions": len(positions) - position_stats["valid_count"],
            },
        }

    async def stop_trading_operations(self) -> None:
        self.coordinator.running = False
        self.coordinator.shutdown_event.set()

        if self.coordinator.tasks:
            production("Cancelando tasks ativas", count=len(self.coordinator.tasks))

            for i, task in enumerate(self.coordinator.tasks):
                if not task.done():
                    task.cancel()
                    debug(f"Task {i} cancelada")

            try:
                async with asyncio.timeout(10.0):
                    await asyncio.gather(
                        *self.coordinator.tasks, return_exceptions=True
                    )
                production(" Todas as tasks finalizadas")
            except TimeoutError:
                warning("Timeout ao aguardar tasks - forçando shutdown")

            self.coordinator.tasks.clear()
