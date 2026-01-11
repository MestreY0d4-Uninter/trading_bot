import asyncio
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from shared.observability.flow_tracker import track_component
from shared.observability.logger import error, production, warning
from shared.types.state import state

from .circuit_breaker import CircuitBreaker
from .risk_calculator import RiskLevel


class EmergencyShutdownReason(Enum):
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    CONSECUTIVE_LOSSES = "consecutive_losses"
    BALANCE_CRITICAL = "balance_critical"
    POSITION_RISK_HIGH = "position_risk_high"
    MANUAL_TRIGGER = "manual_trigger"
    SYSTEM_ERROR = "system_error"


class EmergencyManager:
    def __init__(self, config: dict, db: Any | None = None) -> None:
        self.config = config
        self.risk_config = config["risk"]

        self._emergency_lock = asyncio.Lock()

        self.emergency_shutdown_active = False
        self.emergency_shutdown_reason: EmergencyShutdownReason | None = None
        self.emergency_shutdown_timestamp: datetime | None = None
        self.position_cleanup_in_progress = False

        self.circuit_breaker = CircuitBreaker(config, db)

        self.daily_loss_limit_pct = Decimal(
            str(self.risk_config.get("daily_loss_limit_pct", 8.0))
        )
        self.max_daily_drawdown = Decimal(
            str(self.risk_config.get("max_daily_drawdown", 15.0))
        )

        self.circuit_breaker_levels: dict[str, dict] = {}
        self._initialize_multi_level_circuit_breaker()

    async def init_state(self):
        """Initialize emergency manager state from database"""
        await self.circuit_breaker.init_state()

    def _initialize_multi_level_circuit_breaker(self):
        self.circuit_breaker_levels = {
            "position": {
                "triggered": False,
                "max_positions": self.risk_config.get("max_positions", 2),
                "cooldown_minutes": 5,
            },
            "daily": {
                "triggered": False,
                "loss_threshold": self.daily_loss_limit_pct * Decimal("0.8"),
                "cooldown_minutes": 15,
            },
            "total": {
                "triggered": False,
                "loss_threshold": self.daily_loss_limit_pct,
                "cooldown_minutes": 60,
            },
        }

    async def check_all_circuit_breakers(
        self, daily_pnl_pct: float, consecutive_losses: int, position_count_func
    ):
        await self.circuit_breaker.check_triggers(daily_pnl_pct, consecutive_losses)

        if daily_pnl_pct <= -self.circuit_breaker_levels["daily"]["loss_threshold"]:
            self.circuit_breaker_levels["daily"]["triggered"] = True
            warning("Circuit breaker diário ativado", pnl_pct=daily_pnl_pct)

        emergency_drawdown_limit = self.max_daily_drawdown

        if daily_pnl_pct <= -emergency_drawdown_limit:
            self.circuit_breaker_levels["total"]["triggered"] = True
            await self.trigger_emergency_shutdown(
                EmergencyShutdownReason.DAILY_LOSS_LIMIT
            )

        open_positions = await position_count_func()
        if open_positions >= self.circuit_breaker_levels["position"]["max_positions"]:
            self.circuit_breaker_levels["position"]["triggered"] = True

    @track_component("risk_manager")
    async def trigger_emergency_shutdown(self, reason: EmergencyShutdownReason):
        async with self._emergency_lock:
            if self.emergency_shutdown_active:
                return

            self.emergency_shutdown_active = True
            self.emergency_shutdown_reason = reason
            self.emergency_shutdown_timestamp = datetime.now()

            error(
                "🚨 EMERGENCY SHUTDOWN ATIVADO",
                reason=reason.value,
                timestamp=self.emergency_shutdown_timestamp,
            )

            # Aguardar cleanup completo antes de retornar
            await self._initiate_position_cleanup()

    async def _initiate_position_cleanup(self):
        if self.position_cleanup_in_progress:
            return

        self.position_cleanup_in_progress = True

        try:
            positions = await state.get_all_positions()
            if positions:
                warning(f"Iniciando cleanup de {len(positions)} posições")

                for symbol in list(positions.keys()):
                    try:
                        if await state.has_position(symbol):
                            await state.remove_position(symbol)
                        production(f"Posição removida do state: {symbol}")
                    except Exception as e:
                        error(f"Erro ao remover posição {symbol}", error=str(e))

                production("Position cleanup concluído")
            else:
                production("Nenhuma posição para cleanup")

        except Exception as e:
            error("Erro durante position cleanup", error=str(e))
        finally:
            self.position_cleanup_in_progress = False

    async def manual_emergency_shutdown(self, reason: str = "Manual trigger"):
        await self.trigger_emergency_shutdown(EmergencyShutdownReason.MANUAL_TRIGGER)
        production("Emergency shutdown manual ativado", reason=reason)

    async def check_system_error_emergency(self, error_context: str = "Unknown"):
        emergency_config = self.config.get("emergency_conditions", {})
        if emergency_config.get("close_on_system_error", True):
            production(
                "System error - ativando emergency shutdown",
                error_context=error_context,
            )
            await self.trigger_emergency_shutdown(EmergencyShutdownReason.SYSTEM_ERROR)
        else:
            warning(
                "System error detectado mas emergency shutdown desabilitado",
                error_context=error_context,
            )

    @track_component("risk_manager")
    async def reset_emergency_shutdown(self):
        async with self._emergency_lock:
            if not self.emergency_shutdown_active:
                warning("Emergency shutdown não estava ativo")
                return False

            self.emergency_shutdown_active = False
            self.emergency_shutdown_reason = None
            self.emergency_shutdown_timestamp = None
            self.position_cleanup_in_progress = False

            for level in self.circuit_breaker_levels.values():
                level["triggered"] = False

            self.circuit_breaker.reset()

            production("Emergency shutdown resetado - trading pode ser retomado")
            return True

    def reset_circuit_breakers(self):
        for level in self.circuit_breaker_levels.values():
            level["triggered"] = False
        self.circuit_breaker.reset()

    async def health_check(
        self,
        current_balance: Decimal,
        min_balance_threshold: float,
        risk_level: RiskLevel,
        metrics: dict,
    ) -> dict:
        try:
            status = "healthy"
            issues: list[str] = []
            warnings_list: list[str] = []

            status = self._check_emergency_status(status, issues)
            status = self._check_balance_status(
                status, issues, current_balance, min_balance_threshold
            )
            status = self._check_daily_loss_status(
                status, issues, warnings_list, metrics
            )
            status = self._check_consecutive_losses(
                status, issues, warnings_list, metrics
            )
            status = self._check_circuit_breaker_status(status, issues)
            status = self._check_risk_level_status(status, warnings_list, risk_level)
            status = self._check_circuit_breaker_levels(status, warnings_list)

            return {
                "status": status,
                "issues": issues,
                "warnings": warnings_list,
                "emergency_shutdown": self.emergency_shutdown_active,
                "position_cleanup_progress": self.position_cleanup_in_progress,
                "circuit_breaker": self.circuit_breaker.is_triggered(),
                "multi_level_status": {
                    level: data["triggered"]
                    for level, data in self.circuit_breaker_levels.items()
                },
            }

        except Exception as e:
            error("Emergency manager health check error", error=str(e))
            return {"status": "error", "error": str(e)}

    def _check_emergency_status(self, status: str, issues: list) -> str:
        if self.emergency_shutdown_active:
            reason_value = (
                self.emergency_shutdown_reason.value
                if self.emergency_shutdown_reason
                else "unknown"
            )
            issues.append(f"Emergency shutdown: {reason_value}")
            return "critical"
        return status

    def _check_balance_status(
        self, status: str, issues: list, balance: Decimal, threshold: float
    ) -> str:
        if balance < threshold:
            issues.append(f"Balance below threshold: ${balance:.2f}")
            return "warning" if status == "healthy" else status
        return status

    def _check_daily_loss_status(
        self, status: str, issues: list, warnings_list: list, metrics: dict
    ) -> str:
        daily_pnl_pct = metrics["daily_pnl_pct"]

        if daily_pnl_pct <= -self.daily_loss_limit_pct:
            issues.append(f"Daily loss limit reached: {daily_pnl_pct:.1f}%")
            return "critical"

        if daily_pnl_pct <= -self.daily_loss_limit_pct * Decimal("0.8"):
            warnings_list.append(f"Approaching daily loss limit: {daily_pnl_pct:.1f}%")
            return "warning" if status == "healthy" else status

        return status

    def _check_consecutive_losses(
        self, status: str, issues: list, warnings_list: list, metrics: dict
    ) -> str:
        max_losses = self.config["risk"].get("max_consecutive_losses", 5)
        consecutive = metrics["consecutive_losses"]

        if consecutive >= max_losses:
            issues.append(f"Max consecutive losses: {consecutive}")
            return "critical"

        if consecutive >= max_losses - 1:
            warnings_list.append(f"Near max consecutive losses: {consecutive}")
            return "warning" if status == "healthy" else status

        return status

    def _check_circuit_breaker_status(self, status: str, issues: list) -> str:
        if self.circuit_breaker.is_triggered():
            issues.append(f"Circuit breaker: {self.circuit_breaker.get_reason()}")
            return "critical" if status != "critical" else status
        return status

    def _check_risk_level_status(
        self, status: str, warnings_list: list, risk_level: RiskLevel
    ) -> str:
        if risk_level in [RiskLevel.HIGH, RiskLevel.CRITICAL]:
            warnings_list.append(f"Risk level: {risk_level.value}")
            return "warning" if status == "healthy" else status
        return status

    def _check_circuit_breaker_levels(self, status: str, warnings_list: list) -> str:
        triggered_count = sum(
            1 for data in self.circuit_breaker_levels.values() if data["triggered"]
        )
        if triggered_count > 0:
            warnings_list.append(f"{triggered_count} circuit breaker levels triggered")
            return "warning" if status == "healthy" else status
        return status
