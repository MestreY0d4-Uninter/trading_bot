import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any

from core.position.position_calculator import PositionCalculator
from core.position.position_validator import PositionValidator
from shared.enums import PositionState
from shared.observability.logger import debug, error, production, warning
from shared.types.state import state
from utils import decimal_math
from utils.decimal_math import to_decimal
from utils.validation_utils import validate_financial_values


class PositionStateManager:
    @staticmethod
    def calculate_pnl_percentage(
        entry_price: Decimal, current_price: Decimal
    ) -> Decimal:
        return decimal_math.calculate_pnl_percentage(entry_price, current_price)

    @staticmethod
    def calculate_pnl_usd(
        entry_price: Decimal, current_price: Decimal, quantity: Decimal
    ) -> Decimal:
        return decimal_math.calculate_pnl(entry_price, current_price, quantity)

    def _calculate_pnl_core(
        self,
        entry_price: Decimal,
        current_price: Decimal,
        quantity: Decimal,
        symbol: str = "",
    ):
        return self.calculator._calculate_pnl_core(
            entry_price, current_price, quantity, symbol
        )

    def __init__(
        self,
        config: dict,
        executor,
        risk_manager,
        repository,
        position_tracker=None,
        client=None,
    ):
        if not config or not isinstance(config, dict):
            raise ValueError("Config deve ser um dict válido")
        if not executor:
            raise ValueError("Executor é obrigatório")
        if not risk_manager:
            raise ValueError("Risk manager é obrigatório")
        if not repository:
            raise ValueError("Repository é obrigatório")

        required_executor_methods = [
            "create_market_buy_order",
            "market_sell",
            "get_average_fill_price",
            "create_oco_exit",
        ]
        missing_methods = [
            method
            for method in required_executor_methods
            if not hasattr(executor, method)
        ]
        if missing_methods:
            raise AttributeError(
                f"Executor ausente métodos críticos: {missing_methods}"
            )

        self.config = config
        self.executor = executor
        self.risk_manager = risk_manager
        self.repository = repository

        if position_tracker is None:
            from shared.types.state import state

            position_tracker = state._get_position_tracker()
        self.position_tracker = position_tracker

        self.client = client

        self.calculator = PositionCalculator()
        self.validator = PositionValidator(self.calculator)

        self.position_states: dict[str, PositionState] = {}
        self.position_locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._operation_snapshots: dict[str, dict[str, Any]] = {}
        self._lock_timeout = 30.0

        self.max_slippage_pct = self.config.get("trading", {}).get(
            "max_slippage_pct", 2.0
        )
        if (
            not isinstance(self.max_slippage_pct, (int, float))
            or self.max_slippage_pct <= 0
        ):
            warning("Max slippage inválido", value=self.max_slippage_pct, usando=2.0)
            self.max_slippage_pct = 2.0

        risk_config = self.config.get("risk", {})
        self.default_stop_loss_multiplier = risk_config.get(
            "default_stop_loss_multiplier", Decimal("0.97")
        )
        self.default_take_profit_multiplier = risk_config.get(
            "default_take_profit_multiplier", Decimal("1.03")
        )
        self.trailing_stop_multiplier = Decimal(
            str(risk_config.get("trailing_stop_multiplier", "0.993"))
        )
        self.trailing_trigger_multiplier = Decimal(
            str(risk_config.get("trailing_trigger_multiplier", "1.01"))
        )
        self.trailing_update_threshold = Decimal(
            str(risk_config.get("trailing_update_threshold", "1.001"))
        )

        self._last_summary_log: float = 0.0
        self._summary_log_interval = 300

        production("Position Manager inicializado com locks atômicos")

    async def _set_position_in_tracker(
        self, symbol: str, position_data: dict[str, Any]
    ) -> None:
        if await self.position_tracker.has_position(symbol):
            await self.position_tracker.update_position(symbol, position_data)
        else:
            await self.position_tracker.add_position(symbol, position_data)

    @asynccontextmanager
    async def _acquire_lock_with_timeout(
        self, lock: asyncio.Lock, symbol: str = "", operation: str = ""
    ):
        try:
            async with asyncio.timeout(self._lock_timeout):
                await lock.acquire()
        except TimeoutError as te:
            error(
                "CRÍTICO: Timeout ao adquirir lock",
                symbol=symbol,
                operation=operation,
                timeout=self._lock_timeout,
            )
            raise RuntimeError(
                f"Timeout ao adquirir lock para {operation} em {symbol}"
            ) from te

        try:
            yield
        finally:
            lock.release()

    async def _get_position_lock(self, symbol: str) -> asyncio.Lock:
        if symbol not in self.position_locks:
            async with self._state_lock:
                if symbol not in self.position_locks:
                    self.position_locks[symbol] = asyncio.Lock()
        return self.position_locks[symbol]

    async def _create_operation_snapshot(self, symbol: str, operation: str) -> str:
        snapshot_id = f"{symbol}_{operation}_{time.time()}"
        async with self._state_lock:
            current_state = self.position_states.get(symbol, PositionState.CLOSED)
            position = await self.position_tracker.get_position(symbol)
            position_copy = position.copy() if position else {}

            self._operation_snapshots[snapshot_id] = {
                "symbol": symbol,
                "operation": operation,
                "timestamp": time.time(),
                "initial_state": current_state,
                "initial_position": position_copy,
                "balance_snapshot": await state.get_balance_copy(),
            }

        debug(
            "Snapshot criado para operação atômica",
            symbol=symbol,
            operation=operation,
            snapshot_id=snapshot_id,
        )
        return snapshot_id

    async def _restore_from_snapshot(self, snapshot_id: str, reason: str) -> bool:
        if snapshot_id not in self._operation_snapshots:
            error(
                "CRÍTICO: Snapshot não encontrado para rollback",
                snapshot_id=snapshot_id,
                reason=reason,
            )
            return False

        symbol_for_log = None
        async with self._state_lock:
            snapshot = self._operation_snapshots[snapshot_id]
            symbol = snapshot["symbol"]
            symbol_for_log = symbol

            self.position_states[symbol] = snapshot["initial_state"]

            if snapshot["initial_position"]:
                await self._set_position_in_tracker(
                    symbol, snapshot["initial_position"]
                )
            else:
                await self.position_tracker.remove_position(symbol)

            del self._operation_snapshots[snapshot_id]

        warning(
            "Rollback executado",
            symbol=symbol_for_log,
            reason=reason,
            snapshot_id=snapshot_id,
        )
        return True

    async def _cleanup_old_snapshots(self) -> None:
        current_time = time.time()
        expired_count = 0
        async with self._state_lock:
            expired_snapshots = [
                snapshot_id
                for snapshot_id, snapshot in self._operation_snapshots.items()
                if current_time - snapshot["timestamp"] > 300
            ]

            for snapshot_id in expired_snapshots:
                del self._operation_snapshots[snapshot_id]

            expired_count = len(expired_snapshots)

        if expired_count > 0:
            debug("Snapshots expirados removidos", count=expired_count)

    async def _atomic_state_check_and_set(
        self, symbol: str, expected_state: PositionState, new_state: PositionState
    ) -> bool:
        return await self.validator.atomic_state_check_and_set(
            symbol, expected_state, new_state, self.position_states, self._state_lock
        )

    async def _validate_global_consistency(self, symbol: str, operation: str) -> bool:
        return await self.validator.validate_global_consistency(
            symbol, operation, self.position_states, self._state_lock
        )

    async def critical_position_cleanup(self, symbol: str) -> bool:
        try:
            position_lock = await self._get_position_lock(symbol)

            async with self._acquire_lock_with_timeout(
                position_lock, symbol, "critical_cleanup"
            ):
                had_position = False
                async with self._state_lock:
                    if await self.position_tracker.has_position(symbol):
                        await self.position_tracker.remove_position(symbol)
                        had_position = True

                    self.position_states[symbol] = PositionState.CLOSED

                if had_position:
                    debug(
                        "Critical cleanup: Posição removida do state",
                        symbol=symbol,
                    )

                debug(
                    "Critical cleanup: Estado atualizado para CLOSED",
                    symbol=symbol,
                )

                production(" Critical cleanup executado", symbol=symbol)

            return True

        except Exception as e:
            error("ERRO na limpeza crítica", symbol=symbol, error=str(e))
            return False

    async def check_exit_conditions(
        self, symbol: str, position: dict, current_price: float
    ) -> tuple[bool, str]:
        validation_result = self._validate_exit_inputs(symbol, position, current_price)
        if validation_result:
            return validation_result

        entry_price = position.get("entry_price", 0)
        if not validate_financial_values(entry_price, "price", symbol):
            return False, ""

        pnl_pct = self.calculate_pnl_percentage(
            to_decimal(entry_price), to_decimal(current_price)
        )

        async with self._state_lock:
            position["current_price"] = current_price
            position["price_check_timestamp"] = datetime.now().isoformat()

        exit_price = self._get_exit_price(current_price)
        stop_loss = position.get("stop_loss")
        take_profit = position.get("take_profit")

        if stop_loss and exit_price <= stop_loss:
            production("Exit: STOP_LOSS", symbol=symbol, pnl_pct=pnl_pct)
            return True, "STOP_LOSS"

        if take_profit and exit_price >= take_profit:
            production("Exit: TAKE_PROFIT", symbol=symbol, pnl_pct=pnl_pct)
            return True, "TAKE_PROFIT"

        return self._check_oco_exit_conditions(position, pnl_pct)

    def _validate_exit_inputs(
        self, symbol: str, position: dict, current_price: float
    ) -> tuple[bool, str] | None:
        if not symbol or not isinstance(symbol, str):
            error("CRÍTICO: Symbol inválido para exit conditions", symbol=symbol)
            return False, "INVALID_SYMBOL"

        if not position or not isinstance(position, dict):
            error("CRÍTICO: Position deve ser dict válido", symbol=symbol)
            return False, "INVALID_POSITION"

        if not validate_financial_values(current_price, "price", symbol):
            return False, "INVALID_PRICE"

        return None

    def _get_exit_price(self, current_price: float | dict) -> float:
        if isinstance(current_price, dict):
            return current_price.get("bid", current_price.get("last", 0))
        return current_price

    def _check_oco_exit_conditions(
        self, position: dict, pnl_pct: Decimal
    ) -> tuple[bool, str]:
        if not position.get("has_oco"):
            return False, ""

        entry_time = position.get("entry_time", datetime.now())
        if isinstance(entry_time, str):
            try:
                entry_time = datetime.fromisoformat(entry_time)
            except (ValueError, TypeError):
                entry_time = datetime.now()

        time_in_position = (datetime.now() - entry_time).total_seconds() / 3600

        if time_in_position > 24:
            return True, "MAX_TIME"

        if pnl_pct < -5:
            return True, "EXTREME_LOSS"

        return False, ""

    async def update_trailing_stop(
        self, symbol: str, position: dict, current_price: float
    ):
        from shared.observability.flow_tracker import track_component

        @track_component("position_manager", slow_threshold=5)
        async def _update_trailing_stop_impl():
            position_lock = await self._get_position_lock(symbol)

            async with self._acquire_lock_with_timeout(
                position_lock, symbol, "update_trailing_stop"
            ):
                try:
                    if not position.get("has_oco"):
                        return

                    if not await self._validate_global_consistency(
                        symbol, "update_trailing_stop"
                    ):
                        return

                    entry_price = position["entry_price"]
                    current_stop = position["stop_loss"]

                    entry_price_decimal = to_decimal(entry_price)
                    current_price_decimal = to_decimal(current_price)

                    if (
                        current_price_decimal
                        > entry_price_decimal * self.trailing_trigger_multiplier
                    ):
                        profit_from_entry = current_price_decimal - entry_price_decimal
                        trailing_protection_factor = Decimal("0.5")
                        new_stop = entry_price_decimal + (
                            profit_from_entry * trailing_protection_factor
                        )
                        current_stop_decimal = to_decimal(current_stop)

                        if (
                            new_stop
                            > current_stop_decimal * self.trailing_update_threshold
                        ):
                            quantity = position["quantity"]
                            take_profit = position["take_profit"]
                            old_oco_id = position.get("oco_id")

                            trailing_oco_failures = position.get(
                                "trailing_oco_failures", 0
                            )
                            max_failures = 3

                            if trailing_oco_failures >= max_failures:
                                warning(
                                    "Trailing stop desabilitado - múltiplas falhas na criação de OCO",
                                    symbol=symbol,
                                    failures=trailing_oco_failures,
                                    max_failures=max_failures,
                                )
                                return

                            oco_result = await self.executor.create_oco_exit(
                                symbol=symbol,
                                quantity=quantity,
                                take_profit_price=take_profit,
                                stop_loss_price=new_stop,
                            )

                            if oco_result:
                                new_oco_id = oco_result.get("orderListId")

                                if old_oco_id and self.client:
                                    try:
                                        await self.client.cancel_oco(symbol, old_oco_id)
                                        debug(
                                            "OCO antiga cancelada após criação da nova",
                                            symbol=symbol,
                                            old_oco_id=old_oco_id,
                                        )
                                    except Exception as e:
                                        warning(
                                            "Erro ao cancelar OCO antiga (nova já ativa)",
                                            symbol=symbol,
                                            old_oco_id=old_oco_id,
                                            error=str(e),
                                        )

                                async with self._state_lock:
                                    position["stop_loss"] = new_stop
                                    position["oco_id"] = new_oco_id
                                    position["trailing_oco_failures"] = 0
                                    await self._set_position_in_tracker(
                                        symbol, position
                                    )

                                debug(
                                    "Trailing stop atualizado com nova OCO",
                                    symbol=symbol,
                                    new_stop=new_stop,
                                    new_oco_id=new_oco_id,
                                )
                            else:
                                async with self._state_lock:
                                    position["trailing_oco_failures"] = (
                                        trailing_oco_failures + 1
                                    )
                                    await self._set_position_in_tracker(
                                        symbol, position
                                    )

                                warning(
                                    "Mantendo OCO antiga - falha ao criar nova OCO",
                                    symbol=symbol,
                                    new_stop=new_stop,
                                    keeping_old_oco=old_oco_id,
                                    failures=position["trailing_oco_failures"],
                                )

                except Exception as e:
                    error(
                        "CRÍTICO: Erro ao atualizar trailing stop",
                        symbol=symbol,
                        current_price=current_price,
                        error=str(e),
                    )

        await _update_trailing_stop_impl()

    async def get_position_summary(self) -> dict:
        async with self._acquire_lock_with_timeout(
            self._global_lock, operation="get_position_summary"
        ):
            all_positions = await self.position_tracker.get_all_positions()
            summary: dict[str, Any] = {
                "total_positions": len(all_positions),
                "total_value": 0,
                "total_pnl": 0,
                "positions": [],
                "healthy_positions": 0,
                "risky_positions": 0,
            }

            for symbol, position in all_positions.items():
                current_price = position.get(
                    "current_price", position.get("entry_price", 0)
                )
                entry_price = position.get("entry_price", 0)
                quantity = position.get("quantity", 0)

                if (
                    not validate_financial_values(current_price, "price")
                    or not validate_financial_values(entry_price, "price")
                    or not validate_financial_values(quantity, "quantity")
                ):
                    warning(
                        "Position data com valores inválidos",
                        symbol=symbol,
                        current_price=current_price,
                        entry_price=entry_price,
                        quantity=quantity,
                    )
                    continue

                calc_result = self.calculator._safe_decimal_operation(
                    "position_value",
                    current_price=current_price,
                    entry_price=entry_price,
                    quantity=quantity,
                )

                if calc_result is None:
                    warning("Resultado de cálculo é None", symbol=symbol)
                    continue

                if "error" in calc_result:
                    warning(calc_result["error"], symbol=symbol)
                    continue

                position_value = calc_result["result"]["position_value"]
                pnl = calc_result["result"]["pnl"]
                pnl_pct = self.calculate_pnl_percentage(entry_price, current_price)

                summary["total_value"] += position_value
                summary["total_pnl"] += pnl

                if pnl_pct < -10:
                    summary["risky_positions"] += 1
                else:
                    summary["healthy_positions"] += 1

                summary["positions"].append(
                    {
                        "symbol": symbol,
                        "entry_price": entry_price,
                        "current_price": current_price,
                        "quantity": quantity,
                        "value": position_value,
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                        "has_oco": position.get("has_oco", False),
                    }
                )

            if (
                time.time() - self._last_summary_log > self._summary_log_interval
                and summary["total_positions"] > 0
            ):
                production(
                    "Resumo de posições",
                    total=summary["total_positions"],
                    value=summary["total_value"],
                    pnl=summary["total_pnl"],
                )
                self._last_summary_log = time.time()

            return summary

    async def get_all_positions(self) -> dict:
        async with self._acquire_lock_with_timeout(
            self._global_lock, operation="get_all_positions"
        ):
            return await self.position_tracker.get_all_positions()

    async def health_check(self) -> dict:
        return await self.validator.health_check(
            self.executor,
            self.risk_manager,
            self.repository,
            self.position_states,
            self._operation_snapshots,
            self.position_locks,
            self._global_lock,
        )

    async def validate_consistency(self) -> bool:
        return await self.validator.validate_consistency(self.repository)
