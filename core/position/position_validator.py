import asyncio
from decimal import Decimal
from typing import Any

from shared.enums import PositionState, PriceValidationStatus
from shared.observability.logger import debug, error, warning
from shared.types.state import state
from utils.validation_utils import validate_financial_values


class PositionValidator:
    STALE_THRESHOLDS: dict[str, Decimal] = {
        "TAKE_PROFIT": Decimal("1.5"),
        "STOP_LOSS": Decimal("3.0"),
        "MAX_TIME": Decimal("5.0"),
        "EXTREME_LOSS": Decimal("5.0"),
    }
    DEFAULT_STALE_THRESHOLD = Decimal("5.0")
    CRITICAL_REASONS = frozenset(
        ["TAKE_PROFIT", "STOP_LOSS", "MAX_TIME", "EXTREME_LOSS"]
    )

    def __init__(self, position_calculator: Any) -> None:
        self.calculator = position_calculator

    @staticmethod
    def _calculate_price_deviation_pct(
        observed_price: Decimal, reference_price: Decimal
    ) -> Decimal:
        if reference_price == Decimal("0"):
            raise ZeroDivisionError("reference price is zero")
        return abs(observed_price - reference_price) / reference_price * Decimal("100")

    def validate_fill_price(
        self, symbol: str, buy_order: dict, expected_price: Decimal, executor
    ) -> dict:
        avg_price = Decimal(str(executor.get_average_fill_price(buy_order)))

        if not validate_financial_values(avg_price, "price"):
            return {"error": f"Preço médio inválido: {avg_price}"}

        try:
            price_deviation_pct = (
                abs(avg_price - expected_price) / expected_price * Decimal("100")
            )
            if price_deviation_pct > Decimal("50"):
                return {"error": f"Avg price muito diferente: {price_deviation_pct}%"}
        except ZeroDivisionError:
            return {"error": "Current price é zero para validação"}

        slippage_result = self.calculator._safe_decimal_operation(
            "slippage", current_price=expected_price, avg_price=avg_price
        )

        if slippage_result is None:
            return {"error": "Resultado de slippage é None"}

        if "error" in slippage_result:
            return {"error": slippage_result["error"]}

        return {"avg_price": avg_price, "slippage_pct": slippage_result["result"]}

    def validate_sell_price(
        self, symbol: str, sell_order: dict, position: dict, executor
    ) -> Decimal | None:
        sell_price = executor.get_average_fill_price(sell_order)
        entry_price = position.get("entry_price", 0)

        if not validate_financial_values(sell_price, "price", symbol):
            return None

        if not validate_financial_values(entry_price, "price", symbol):
            return None

        return Decimal(str(sell_price))

    async def _fetch_price_from_source(
        self, source: Any, symbol: str, timeout: float, source_name: str
    ) -> tuple[Decimal | None, str | None]:
        if source is None:
            return None, None

        try:
            async with asyncio.timeout(timeout):
                if source_name == "data_manager" and hasattr(source, "fetch_ticker"):
                    ticker = await source.fetch_ticker(symbol, max_age_seconds=5)
                elif source_name == "executor" and hasattr(source, "client"):
                    ticker = await source.client.get_ticker(symbol)
                else:
                    ticker = await source.get_ticker(symbol)

                if ticker and "price" in ticker:
                    return Decimal(str(ticker["price"])), source_name
        except TimeoutError:
            debug(f"{source_name} timeout", symbol=symbol, timeout=f"{timeout}s")
            return None, "timeout"
        except Exception as e:
            debug(f"{source_name} failed", symbol=symbol, error=str(e))

        return None, None

    async def _fetch_price_with_fallback(
        self, symbol: str, client: Any, data_manager: Any, executor: Any
    ) -> tuple[Decimal | None, str, bool]:
        sources = [
            (client, 3.0, "direct_client"),
            (data_manager, 2.0, "data_manager"),
            (executor, 2.0, "executor"),
        ]
        timeout_occurred = False

        for source, timeout, source_name in sources:
            price, result_type = await self._fetch_price_from_source(
                source, symbol, timeout, source_name
            )
            if result_type == "timeout":
                timeout_occurred = True
            if price is not None:
                return price, source_name, timeout_occurred

        return None, "none", timeout_occurred

    def _get_stale_threshold(self, reason: str) -> Decimal:
        return self.STALE_THRESHOLDS.get(reason, self.DEFAULT_STALE_THRESHOLD)

    def _build_validation_result(
        self,
        status: PriceValidationStatus,
        fresh_price: Decimal | None,
        decision_price: Decimal,
        deviation_pct: Decimal,
        fetch_method: str,
        action: str,
        error_msg: str | None = None,
        warning_msg: str | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "fresh_price": fresh_price,
            "decision_price": decision_price,
            "deviation_pct": deviation_pct,
            "fetch_method": fetch_method,
            "action": action,
            "error": error_msg,
            "warning": warning_msg,
        }

    def _build_fallback_result(
        self, decision_price: Decimal, reason: str, timeout_occurred: bool
    ) -> dict[str, Any]:
        validation_status = (
            PriceValidationStatus.VALIDATION_TIMEOUT
            if timeout_occurred
            else PriceValidationStatus.VALIDATION_FAILED
        )
        is_critical = reason in self.CRITICAL_REASONS

        warning_message = (
            "Unable to fetch fresh price - using decision price as fallback"
            if is_critical
            else "Unable to fetch fresh price"
        )
        warning(
            warning_message,
            symbol="unknown",
            reason=reason,
            decision_price=decision_price,
            status=validation_status.value,
            action="REJECT",
        )

        return self._build_validation_result(
            status=PriceValidationStatus.STALE,
            fresh_price=decision_price,
            decision_price=decision_price,
            deviation_pct=Decimal("0"),
            fetch_method="fallback_decision_price" if is_critical else "none",
            action="REJECT",
            error_msg=None if is_critical else "Unable to fetch fresh price",
            warning_msg="Using decision_price as fallback" if is_critical else None,
        )

    async def validate_fresh_price(
        self,
        symbol: str,
        decision_price: Decimal,
        reason: str,
        client,
        data_manager,
        executor,
    ) -> dict[str, Any]:
        try:
            fresh_price, fetch_method, timeout_occurred = (
                await self._fetch_price_with_fallback(
                    symbol, client, data_manager, executor
                )
            )

            if fresh_price is None:
                return self._build_fallback_result(
                    decision_price, reason, timeout_occurred
                )

            deviation_pct = self._calculate_price_deviation_pct(
                fresh_price, decision_price
            )
            stale_threshold = self._get_stale_threshold(reason)

            is_stale = deviation_pct > stale_threshold
            status = (
                PriceValidationStatus.STALE if is_stale else PriceValidationStatus.FRESH
            )
            action = "ABORT" if is_stale else "PROCEED"

            result = self._build_validation_result(
                status=status,
                fresh_price=fresh_price,
                decision_price=decision_price,
                deviation_pct=deviation_pct,
                fetch_method=fetch_method,
                action=action,
            )

            if deviation_pct > Decimal("0.5"):
                debug("Price validation", symbol=symbol, **result)

            return result

        except Exception as e:
            error(
                "Critical error validating fresh price",
                symbol=symbol,
                reason=reason,
                decision_price=decision_price,
                error=str(e),
            )
            return self._build_validation_result(
                status=PriceValidationStatus.VALIDATION_FAILED,
                fresh_price=None,
                decision_price=decision_price,
                deviation_pct=Decimal("0"),
                fetch_method="error",
                action="ABORT",
                error_msg=str(e),
            )

    async def atomic_state_check_and_set(
        self,
        symbol: str,
        expected_state: PositionState,
        new_state: PositionState,
        position_states: dict,
        state_lock,
    ) -> bool:
        success_log: dict[str, Any] = {}
        failure_log: dict[str, Any] = {}
        should_log_success = False
        should_log_failure = False

        async with state_lock:
            current_state = position_states.get(symbol, PositionState.CLOSED)
            if current_state == expected_state:
                position_states[symbol] = new_state
                should_log_success = True
                success_log = {
                    "symbol": symbol,
                    "from_state": expected_state,
                    "to_state": new_state,
                }
            else:
                should_log_failure = True
                failure_log = {
                    "symbol": symbol,
                    "expected": expected_state,
                    "current": current_state,
                    "attempted_new": new_state,
                }

        if should_log_success:
            debug("Estado alterado atomicamente", **success_log)
            return True

        if should_log_failure:
            warning("Estado não confere para operação atômica", **failure_log)

        return False

    async def validate_global_consistency(
        self, symbol: str, operation: str, position_states: dict, state_lock
    ) -> bool:
        try:
            should_log_inconsistency = False
            error_payload: dict[str, Any] = {}
            is_valid = True

            async with state_lock:
                position_exists = await state.has_position(symbol)
                position_state = position_states.get(symbol, PositionState.CLOSED)

                if position_exists and position_state == PositionState.CLOSED:
                    should_log_inconsistency = True
                    error_payload = {
                        "symbol": symbol,
                        "operation": operation,
                        "state": position_state,
                    }
                    is_valid = False

            if should_log_inconsistency:
                error(
                    "CRÍTICO: Posição existe mas estado é CLOSED",
                    **error_payload,
                )
            return is_valid

        except Exception as e:
            error(
                "CRÍTICO: Erro na validação de consistência",
                symbol=symbol,
                operation=operation,
                error=str(e),
            )
            return False

    async def validate_consistency(self, repository) -> bool:
        try:
            state_count = await state.get_position_count()
            db_trades = await repository.get_open_trades()
            db_count = len(db_trades)

            if state_count != db_count:
                warning(
                    f"Inconsistency detected: State positions={state_count}, DB positions={db_count}"
                )
                return False

            return True
        except Exception as e:
            error("Failed to validate consistency", error=str(e))
            return False

    async def health_check(
        self,
        executor,
        risk_manager,
        repository,
        position_states: dict,
        operation_snapshots: dict,
        position_locks: dict,
        global_lock,
    ) -> dict:
        try:
            import asyncio
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def _acquire_lock_with_timeout(
                lock: asyncio.Lock, timeout: float = 30.0
            ):
                try:
                    async with asyncio.timeout(timeout):
                        await lock.acquire()
                except TimeoutError as te:
                    error("CRÍTICO: Timeout ao adquirir lock", timeout=timeout)
                    raise RuntimeError("Timeout ao adquirir lock") from te

                try:
                    yield
                finally:
                    lock.release()

            async with _acquire_lock_with_timeout(global_lock):
                status, issues = self._check_dependencies(
                    executor, risk_manager, repository
                )
                all_positions = await state.get_all_positions()

                inconsistent = self._count_inconsistent_states(
                    all_positions, position_states
                )
                if inconsistent > 0:
                    status = "warning" if status == "healthy" else status
                    issues.append(f"{inconsistent} posições inconsistentes")

                invalid = self._count_invalid_positions(all_positions)
                if invalid > 0:
                    status = "critical"
                    issues.append(f"{invalid} posições com campos ausentes")

                return {
                    "status": status,
                    "issues": issues,
                    "total_positions": len(all_positions),
                    "position_states": dict(position_states),
                    "inconsistent_states": inconsistent,
                    "invalid_positions": invalid,
                    "executor_available": bool(executor),
                    "risk_manager_available": bool(risk_manager),
                    "repository_available": bool(repository),
                    "active_snapshots": len(operation_snapshots),
                    "position_locks_count": len(position_locks),
                }

        except Exception as e:
            error("PositionManager health check error", error=str(e))
            return {"status": "error", "error": str(e)}

    def _check_dependencies(
        self, executor, risk_manager, repository
    ) -> tuple[str, list]:
        status = "healthy"
        issues = []

        if not executor:
            status = "critical"
            issues.append("Executor não disponível")

        if not risk_manager:
            status = "critical"
            issues.append("Risk manager não disponível")

        if not repository:
            status = "critical"
            issues.append("Repository não disponível")

        return status, issues

    def _count_inconsistent_states(
        self, all_positions: dict, position_states: dict
    ) -> int:
        return sum(1 for symbol in all_positions if symbol not in position_states)

    def _count_invalid_positions(self, all_positions: dict) -> int:
        required_fields = ["entry_price", "quantity", "entry_time"]
        count = 0
        for position in all_positions.values():
            if any(f not in position for f in required_fields):
                count += 1
        return count
