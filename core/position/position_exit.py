from datetime import datetime, timedelta
from decimal import Decimal

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.validators.trading_validator import TradingValidator
from shared.constants import EMERGENCY_EXIT_REASONS
from shared.enums import PositionState, PriceValidationStatus
from shared.observability.flow_tracker import track_component
from shared.observability.logger import (
    debug,
    error,
    position_closed,
    production,
    warning,
)
from shared.observability.metrics import metrics
from shared.types.position_types import CloseResult
from shared.types.state import state
from utils.order_id_generator import generate_client_order_id


class PositionExitMixin:
    async def _should_defer_to_oco(
        self, symbol: str, position: dict, reason: str
    ) -> bool:
        if not position.get("has_oco", False) or reason in EMERGENCY_EXIT_REASONS:
            return False

        production(
            "OCO ativa detectada - aguardando execução automática",
            symbol=symbol,
            reason=reason,
        )
        async with self._state_lock:
            self.position_states[symbol] = PositionState.OPEN
        return True

    async def _cancel_existing_orders(self, symbol: str, position: dict) -> bool:
        if not position.get("has_oco") or not position.get("oco_id"):
            return False

        try:
            if self.executor:
                cancel_operation_id = f"cancel_oco_{symbol}_{position['oco_id']}"
                await self.executor.idempotency.execute_with_idempotency(
                    operation_id=cancel_operation_id,
                    operation=lambda: self.executor.cancel_oco_order(
                        symbol, position["oco_id"]
                    ),
                    symbol=symbol,
                    operation_type="CANCEL_OCO",
                )
                debug("OCO cancelada antes do fechamento", symbol=symbol)
                return True
        except Exception as e:
            warning(
                "Erro ao cancelar OCO",
                symbol=symbol,
                oco_id=position["oco_id"],
                error=str(e),
            )

        return False

    async def _execute_market_sell(
        self, symbol: str, quantity: Decimal, client_order_id: str | None = None
    ) -> dict | None:
        if client_order_id is None:
            client_order_id = generate_client_order_id("sell", symbol)

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=3),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        )
        async def _sell_with_retry():
            sell_order = await self.executor.idempotency.execute_with_idempotency(
                operation_id=client_order_id,
                operation=lambda: self.executor.market_sell(
                    symbol, quantity, client_order_id=client_order_id
                ),
                symbol=symbol,
                operation_type="SELL",
            )
            if not sell_order:
                raise ValueError("market_sell returned None")
            return sell_order

        try:
            return await _sell_with_retry()
        except Exception as e:
            error(
                "CRÍTICO: Falha ao vender após todas as tentativas",
                symbol=symbol,
                qty=quantity,
                error=str(e),
            )
            return None

    def _validate_sell_price(
        self, symbol: str, sell_order: dict, position: dict
    ) -> Decimal | None:
        return self.validator.validate_sell_price(
            symbol, sell_order, position, self.executor
        )

    async def _validate_fresh_price(
        self, symbol: str, decision_price: Decimal, reason: str
    ) -> dict:
        client = self.client if hasattr(self, "client") else None
        data_manager = self.data_manager if hasattr(self, "data_manager") else None
        return await self.validator.validate_fresh_price(
            symbol, decision_price, reason, client, data_manager, self.executor
        )

    def _calculate_position_pnl(
        self, symbol: str, sell_price: Decimal, entry_price: Decimal, quantity: Decimal
    ) -> dict | None:
        return self.calculator.calculate_position_pnl(
            symbol, sell_price, entry_price, quantity
        )

    async def _update_position_database(
        self,
        symbol: str,
        sell_price: Decimal,
        pnl_usd: Decimal,
        pnl_pct: Decimal,
        reason: str,
    ) -> bool:
        success = await self.repository.update_trade_exit_by_symbol(
            symbol=symbol,
            exit_price=sell_price,
            exit_time=datetime.now(),
            realized_pnl=pnl_usd,
            exit_reason=reason,
        )

        if not success:
            error("Failed to update trade in database", symbol=symbol)
            return False

        await self.risk_manager.register_trade_result(
            symbol, pnl_usd, pnl_pct, pnl_usd > 0
        )

        entry_time = datetime.now() - timedelta(minutes=30)
        try:
            position = await state.get_position(symbol)
            if position:
                pos_entry_time = position.get("entry_time")
                if isinstance(pos_entry_time, str):
                    entry_time = datetime.fromisoformat(pos_entry_time)
                elif isinstance(pos_entry_time, datetime):
                    entry_time = pos_entry_time
        except (ValueError, TypeError, AttributeError):
            pass

        duration = (datetime.now() - entry_time).total_seconds() / 60
        await metrics.record_trade(symbol, pnl_usd, duration, pnl_usd > 0)
        return True

    async def _handle_position_close_error(
        self,
        symbol: str,
        exception: Exception,
        reason: str,
        snapshot_id: str | None = None,
        position_data: dict | None = None,
    ) -> None:
        error(
            "CRÍTICO: Erro ao fechar posição",
            symbol=symbol,
            reason=reason,
            error=str(exception),
        )

        if snapshot_id:
            await self._restore_from_snapshot(
                snapshot_id, f"Erro no fechamento: {str(exception)}"
            )
        else:
            async with self._state_lock:
                if symbol in self.position_states:
                    self.position_states[symbol] = PositionState.OPEN

        if position_data:
            warning(
                "Tentando restaurar proteção após falha no fechamento (Naked Exposure Fix)",
                symbol=symbol,
            )
            try:
                quantity = position_data.get("quantity")
                stop_loss = position_data.get("stop_loss")

                if quantity and stop_loss:
                    from utils.decimal_math import to_decimal

                    restore_id = generate_client_order_id("sl", symbol)
                    await self.executor.idempotency.execute_with_idempotency(
                        operation_id=restore_id,
                        operation=lambda: self.executor.create_stop_loss_order(
                            symbol,
                            to_decimal(quantity),
                            to_decimal(stop_loss),
                            client_order_id=restore_id,
                        ),
                        symbol=symbol,
                        operation_type="RESTORE_STOP_LOSS",
                    )
                    production(
                        "Proteção restaurada com sucesso (Stop Loss)",
                        symbol=symbol,
                        stop_loss=stop_loss,
                    )
            except Exception as restore_error:
                error(
                    "FALHA CRÍTICA AO RESTAURAR PROTEÇÃO",
                    symbol=symbol,
                    error=str(restore_error),
                )

    async def _validate_prices(
        self,
        symbol: str,
        current_price: Decimal,
        reason: str,
        snapshot_id: str | None,
        position: dict,
    ) -> tuple[bool, Decimal]:
        """Validate price freshness (initial + final). Returns (should_continue, validated_price)."""
        should_continue, price = await self._handle_price_validation(
            symbol, current_price, reason, snapshot_id, position
        )
        if not should_continue:
            return False, price

        return await self._handle_final_price_check(
            symbol, price, reason, snapshot_id, position
        )

    async def _handle_price_validation(
        self,
        symbol: str,
        current_price: Decimal,
        reason: str,
        snapshot_id: str | None,
        position: dict,
    ) -> tuple[bool, Decimal]:
        """Validate price freshness. Returns (should_continue, validated_price)."""
        price_validation = await self._validate_fresh_price(
            symbol, current_price, reason
        )

        match price_validation["status"]:
            case PriceValidationStatus.FRESH:
                debug(
                    "Price validation successful",
                    symbol=symbol,
                    fresh_price=price_validation.get("fresh_price"),
                )
                return True, current_price

            case PriceValidationStatus.STALE:
                if reason == "TAKE_PROFIT":
                    error("TAKE_PROFIT cancelled due to stale price", symbol=symbol)
                    await self._handle_position_close_error(
                        symbol,
                        Exception(
                            f"TAKE_PROFIT cancelled: price deviation {price_validation.get('deviation_pct', 0):.2f}%"
                        ),
                        reason,
                        snapshot_id,
                        position_data=position,
                    )
                    return False, current_price
                warning(
                    "Stale price detected but continuing (STOP_LOSS or emergency)",
                    symbol=symbol,
                    reason=reason,
                )
                return True, current_price

            case (
                PriceValidationStatus.VALIDATION_FAILED
                | PriceValidationStatus.VALIDATION_TIMEOUT
            ):
                if reason == "TAKE_PROFIT":
                    error(
                        "TAKE_PROFIT cancelled due to price validation failure",
                        symbol=symbol,
                    )
                    await self._handle_position_close_error(
                        symbol,
                        Exception(
                            f"TAKE_PROFIT cancelled: {price_validation['status'].value}"
                        ),
                        reason,
                        snapshot_id,
                        position_data=position,
                    )
                    return False, current_price
                if reason in EMERGENCY_EXIT_REASONS:
                    warning(
                        "Price validation failed but continuing (emergency closure)",
                        symbol=symbol,
                        reason=reason,
                    )
                    return True, current_price
                error(
                    "Position closure cancelled due to validation failure",
                    symbol=symbol,
                    reason=reason,
                )
                await self._handle_position_close_error(
                    symbol,
                    Exception(f"Validation failed: {price_validation['status'].value}"),
                    reason,
                    snapshot_id,
                    position_data=position,
                )
                return False, current_price

        return True, current_price

    async def _handle_final_price_check(
        self,
        symbol: str,
        current_price: Decimal,
        reason: str,
        snapshot_id: str | None,
        position: dict,
    ) -> tuple[bool, Decimal]:
        """Final price revalidation before sell. Returns (should_continue, final_price)."""
        final_check = await self._validate_fresh_price(symbol, current_price, reason)

        if final_check["status"] != PriceValidationStatus.STALE:
            return True, current_price

        fresh_price = final_check.get("fresh_price", current_price)
        deviation_pct = final_check.get("deviation_pct", 0)

        if reason == "TAKE_PROFIT" and abs(deviation_pct) > 2.0:
            error(
                "CRITICAL: TAKE_PROFIT cancelled - price moved >2% during execution",
                symbol=symbol,
                deviation_pct=deviation_pct,
            )
            await self._handle_position_close_error(
                symbol,
                Exception(f"TP cancelled: price deviation {deviation_pct:.2f}% > 2%"),
                reason,
                snapshot_id,
                position_data=position,
            )
            return False, current_price

        if reason == "STOP_LOSS":
            warning("Stop loss price updated to fresh market price", symbol=symbol)
            return True, fresh_price

        return True, current_price

    async def _execute_and_validate_sell(
        self,
        symbol: str,
        quantity: Decimal,
        position: dict,
        reason: str,
        snapshot_id: str | None,
        oco_cancelled: bool,
        close_start_time: datetime,
    ) -> tuple[Decimal | None, datetime]:
        """Execute market sell and validate. Returns (sell_price, execution_time)."""
        sell_order = await self._execute_market_sell(symbol, quantity)
        execution_time = datetime.now()
        time_to_execute = (execution_time - close_start_time).total_seconds()

        if not sell_order:
            error("CRIT: Falha na execução da venda", symbol=symbol, reason=reason)
            if oco_cancelled:
                warning(
                    "ALERTA: OCO foi cancelada mas venda falhou - posição sem proteção",
                    symbol=symbol,
                )
            await self._handle_position_close_error(
                symbol,
                Exception("Falha na venda"),
                reason,
                snapshot_id,
                position_data=position,
            )
            return None, execution_time

        if time_to_execute > 5.0:
            warning(
                "DELAY CRÍTICO na execução",
                symbol=symbol,
                time_to_execute=time_to_execute,
            )

        sell_price = self._validate_sell_price(symbol, sell_order, position)
        if sell_price is None:
            await self._handle_position_close_error(
                symbol, Exception("Preço de venda inválido"), reason, snapshot_id
            )
            return None, execution_time

        return sell_price, execution_time

    def _check_price_deviation(
        self, symbol: str, sell_price: Decimal, current_price: Decimal
    ) -> None:
        """Log warnings for price deviations."""
        price_deviation_pct = abs(sell_price - current_price) / current_price * 100
        if price_deviation_pct > 1.0:
            warning(
                "ALERTA: Grande desvio entre preço de decisão e execução",
                symbol=symbol,
                deviation_pct=price_deviation_pct,
            )
        if price_deviation_pct > 10.0:
            error(
                "RACE CONDITION CRÍTICO: Preço de execução muito diferente da decisão",
                symbol=symbol,
                deviation_pct=price_deviation_pct,
            )

    async def _finalize_close(
        self,
        symbol: str,
        position: dict,
        sell_price: Decimal,
        pnl_usd: Decimal,
        pnl_pct: Decimal,
        reason: str,
        entry_price: Decimal,
        quantity: Decimal,
        snapshot_id: str | None,
    ) -> CloseResult | None:
        """Finalize position close: update DB, state, and return result."""
        entry_time = position["entry_time"]
        if isinstance(entry_time, str):
            try:
                entry_time = datetime.fromisoformat(entry_time)
            except (ValueError, TypeError):
                entry_time = datetime.now() - timedelta(minutes=30)

        duration = (datetime.now() - entry_time).total_seconds() / 60

        position["exit_price"] = sell_price
        position["exit_time"] = datetime.now()
        position["realized_pnl"] = pnl_usd
        position["exit_reason"] = reason
        position["status"] = "CLOSED"

        if not await self._update_position_database(
            symbol, sell_price, pnl_usd, pnl_pct, reason
        ):
            await self._handle_position_close_error(
                symbol, Exception("Database update failed"), reason, snapshot_id
            )
            return None

        position_closed(
            symbol,
            reason,
            float(pnl_usd),
            float(pnl_pct),
            float(entry_price),
            float(sell_price),
            duration,
        )

        async with self._state_lock:
            self.position_states[symbol] = PositionState.CLOSED
            if await state.has_position(symbol):
                await state.remove_position(symbol)

        if not await self._atomic_state_check_and_set(
            symbol, PositionState.CLOSING, PositionState.CLOSED
        ):
            warning(
                "Posição fechada mas falha na transição final de estado", symbol=symbol
            )

        if not await self.validate_consistency():
            error("Consistency issues after closing position", symbol=symbol)

        return CloseResult(
            symbol=symbol,
            pnl=pnl_usd,
            pnl_pct=pnl_pct,
            reason=reason,
            entry_price=entry_price,
            exit_price=sell_price,
            quantity=quantity,
            duration_minutes=duration,
        )

    async def _prepare_close_operation(
        self, symbol: str, reason: str, current_price: Decimal
    ) -> tuple[dict, str, Decimal] | None:
        """Prepare close operation: validate, set state, create snapshot."""
        position = await state.get_position(symbol)
        if not position or not await self._validate_global_consistency(
            symbol, "close_position"
        ):
            return None

        self._record_trade_monitor(symbol, position, current_price)

        production(
            "Iniciando fechamento de posição",
            symbol=symbol,
            reason=reason,
            decision_price=current_price,
            entry_price=position.get("entry_price"),
            expected_pnl_pct=self.calculate_pnl_percentage(
                position.get("entry_price", 0), current_price
            ),
        )

        if not await self._atomic_state_check_and_set(
            symbol, PositionState.OPEN, PositionState.CLOSING
        ):
            error("CRÍTICO: Falha ao definir estado CLOSING", symbol=symbol)
            return None

        snapshot_id = await self._create_operation_snapshot(symbol, "close_position")
        quantity = Decimal(str(position["quantity"]))

        return position, snapshot_id, quantity

    async def _process_pnl_and_finalize(
        self,
        symbol: str,
        position: dict,
        sell_price: Decimal,
        current_price: Decimal,
        reason: str,
        snapshot_id: str,
    ) -> dict | None:
        """Calculate PnL, validate, and finalize the close operation."""
        self._check_price_deviation(symbol, sell_price, current_price)

        entry_price = Decimal(str(position["entry_price"]))
        quantity = Decimal(str(position["quantity"]))
        pnl_result = self._calculate_position_pnl(
            symbol, sell_price, entry_price, quantity
        )
        if pnl_result is None:
            await self._handle_position_close_error(
                symbol, Exception("Falha no cálculo de PnL"), reason, snapshot_id
            )
            return None

        pnl_usd, pnl_pct = pnl_result["pnl_usd"], pnl_result["pnl_pct"]

        if reason == "TAKE_PROFIT" and pnl_usd <= 0:
            error(
                "BUG CRÍTICO: TAKE_PROFIT resultou em PERDA!",
                symbol=symbol,
                pnl_usd=pnl_usd,
            )

        production(
            "Posição executada",
            symbol=symbol,
            entry_price=entry_price,
            exit_price=sell_price,
            pnl_usd=pnl_usd,
        )

        result = await self._finalize_close(
            symbol,
            position,
            sell_price,
            pnl_usd,
            pnl_pct,
            reason,
            entry_price,
            quantity,
            snapshot_id,
        )
        return result.to_dict() if result else None

    @track_component("position_manager")
    async def close_position(
        self, symbol: str, reason: str, current_price: Decimal
    ) -> dict | None:
        """Close position with atomic state transitions and OCO conflict resolution."""
        if not TradingValidator.validate_close_inputs(symbol, reason, current_price):
            return None

        if not await self.validate_consistency():
            warning(
                "Consistency issues detected before closing position", symbol=symbol
            )

        position_lock = await self._get_position_lock(symbol)
        close_start_time = datetime.now()

        async with self._acquire_lock_with_timeout(
            position_lock, symbol, "close_position"
        ):
            snapshot_id = None
            position = None
            try:
                prep_result = await self._prepare_close_operation(
                    symbol, reason, current_price
                )
                if prep_result is None:
                    return None

                position, snapshot_id, quantity = prep_result

                if await self._should_defer_to_oco(symbol, position, reason):
                    return None

                oco_cancelled = await self._cancel_existing_orders(symbol, position)

                should_continue, current_price = await self._validate_prices(
                    symbol, current_price, reason, snapshot_id, position
                )
                if not should_continue:
                    return None

                sell_price, _ = await self._execute_and_validate_sell(
                    symbol,
                    quantity,
                    position,
                    reason,
                    snapshot_id,
                    oco_cancelled,
                    close_start_time,
                )
                if sell_price is None:
                    return None

                return await self._process_pnl_and_finalize(
                    symbol, position, sell_price, current_price, reason, snapshot_id
                )

            except Exception as e:
                await self._handle_position_close_error(
                    symbol, e, reason, snapshot_id, position_data=position
                )
                return None
            finally:
                if snapshot_id and snapshot_id in self._operation_snapshots:
                    del self._operation_snapshots[snapshot_id]
                    await self._cleanup_old_snapshots()

    def _record_trade_monitor(
        self, symbol: str, position: dict, current_price: Decimal
    ) -> None:
        """Record trade in unified monitor if available."""
        if hasattr(self, "coordinator") and self.coordinator.unified_monitor:
            try:
                entry_price = position.get("entry_price", 0)
                pnl_pct = self.calculate_pnl_percentage(entry_price, current_price)
                result = "win" if pnl_pct > 0 else "loss"
                self.coordinator.unified_monitor.record_trade(symbol, result)
            except Exception:
                pass
