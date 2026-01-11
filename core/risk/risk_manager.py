import asyncio
from decimal import Decimal
from typing import Any

from shared.observability.flow_tracker import track_component
from shared.observability.logger import production

from .duration_monitor import DurationMonitor
from .emergency_manager import EmergencyManager, EmergencyShutdownReason
from .risk_calculator import RiskCalculator, RiskLevel
from .risk_tracker import RiskTracker
from .risk_validator import RiskValidator


class RiskManager:
    def __init__(self, config: dict, db_handler: Any | None = None) -> None:
        self.config = config
        self.risk_config = config["risk"]

        self.calculator = RiskCalculator(config)
        self.validator = RiskValidator(config)
        self.tracker = RiskTracker(config, db_handler)
        self.emergency = EmergencyManager(config, db_handler)
        self.duration = DurationMonitor(config, db_handler)

        self.aggressive_mode = config.get("aggressive_mode", {}).get("enabled", False)
        self.daily_loss_limit_pct = self.risk_config.get("daily_loss_limit_pct", 8.0)
        self.daily_target_pct = self.risk_config.get("daily_target_pct", 10.0)
        self.max_consecutive_losses = self.calculator.max_consecutive_losses

        self.max_daily_drawdown = self.risk_config.get("max_daily_drawdown", 15.0)
        self.emergency_shutdown_threshold = self.risk_config.get(
            "emergency_shutdown_threshold", 18.0
        )
        self.consecutive_losses_limit = self.max_consecutive_losses
        self.correlation_check = self.risk_config.get("correlation_check", False)

        self.min_balance_threshold = config.get("sanity_checks", {}).get(
            "min_balance_required", 10.0
        )

        self._state_lock = asyncio.Lock()

        self.risk_level = RiskLevel.LOW
        self.risk_violations: list[str] = []

        self.db_handler = db_handler

        self.save_performance_metrics = self.tracker.save_performance_metrics_enabled
        self.metrics_interval = self.tracker.metrics_interval
        self.save_market_data = self.tracker.monitoring_config.get(
            "save_market_data", True
        )

        self.position_tracking_enabled = self.duration.position_tracking_enabled
        self.duration_alert_thresholds = self.duration.duration_alert_thresholds
        self.track_duration = self.duration.track_duration
        self.avg_duration_target = self.duration.avg_duration_target
        self.max_comfort_hours = self.duration.max_comfort_hours
        self.log_duration_stats = self.duration.log_duration_stats

        self.validator.validate_risk_reward_parameters()

        emergency_config = config.get("emergency_conditions", {})
        shutdown_safety = config.get("shutdown_safety", {})
        trading_config = config.get("trading", {})

        production(
            "Risk Manager V7 (Modular) inicializado com parâmetros crypto-otimizados",
            daily_loss_limit=self.daily_loss_limit_pct,
            daily_target=self.daily_target_pct,
            max_spread=self.risk_config.get("max_spread_pct", "0.5"),
            min_balance=self.min_balance_threshold,
            emergency_shutdown_on_drawdown=emergency_config.get(
                "close_on_daily_drawdown", 15.0
            ),
            emergency_shutdown_on_losses=emergency_config.get(
                "close_on_consecutive_losses", 4
            ),
            emergency_close_on_shutdown=trading_config.get(
                "emergency_close_on_shutdown", True
            ),
            stop_loss_active_during_downtime=shutdown_safety.get(
                "maintain_stops_active", True
            ),
            max_daily_drawdown=self.max_daily_drawdown,
            emergency_shutdown_threshold=self.emergency_shutdown_threshold,
            consecutive_losses_limit=self.consecutive_losses_limit,
            correlation_check=self.correlation_check,
            save_performance_metrics=self.save_performance_metrics,
            metrics_interval_seconds=self.metrics_interval,
            save_market_data=self.save_market_data,
            modules_loaded=[
                "calculator",
                "validator",
                "tracker",
                "emergency",
                "duration",
            ],
        )

    @track_component("risk_manager")
    async def initialize(self):
        if self.db_handler:
            await self.emergency.init_state()
            production("Risk Manager state initialized from database")

    @property
    def starting_balance(self) -> Decimal:
        return self.tracker.starting_balance

    @starting_balance.setter
    def starting_balance(self, value: Decimal):
        self.tracker.starting_balance = value

    @property
    def current_balance(self) -> Decimal:
        return self.tracker.current_balance

    @current_balance.setter
    def current_balance(self, value: Decimal):
        self.tracker.current_balance = value

    @property
    def total_equity(self) -> Decimal:
        return self.tracker.total_equity

    @total_equity.setter
    def total_equity(self, value: Decimal):
        self.tracker.total_equity = value

    @property
    def emergency_shutdown_active(self) -> bool:
        return self.emergency.emergency_shutdown_active

    @property
    def emergency_shutdown_reason(self):
        return self.emergency.emergency_shutdown_reason

    @property
    def emergency_shutdown_timestamp(self):
        return self.emergency.emergency_shutdown_timestamp

    @property
    def position_cleanup_in_progress(self) -> bool:
        return self.emergency.position_cleanup_in_progress

    @property
    def circuit_breaker(self):
        return self.emergency.circuit_breaker

    @property
    def circuit_breaker_levels(self):
        return self.emergency.circuit_breaker_levels

    @track_component("risk_manager")
    async def set_starting_balance(self, balance: Decimal):
        await self.tracker.set_starting_balance(
            balance, self.validator.validate_balance
        )

    @track_component("risk_manager")
    async def update_balance(
        self, balance: Decimal, positions_market_value: Decimal = Decimal("0")
    ):
        async def assess_and_update_risk(
            daily_pnl_pct: float, consecutive_losses: int
        ) -> RiskLevel:
            new_level = self.calculator.assess_risk_level(
                daily_pnl_pct, consecutive_losses
            )
            async with self._state_lock:
                if new_level != self.risk_level:
                    production(
                        "Nível de risco alterado",
                        anterior=self.risk_level.value,
                        atual=new_level.value,
                        daily_pnl_pct=daily_pnl_pct,
                        consecutive_losses=consecutive_losses,
                    )
                self.risk_level = new_level
            return new_level

        await self.tracker.update_balance(
            balance,
            positions_market_value,
            self.validator.validate_balance,
            lambda dpnl, cl: self.emergency.check_all_circuit_breakers(
                dpnl, cl, self._get_actual_position_count
            ),
            assess_and_update_risk,
            self._save_performance_metrics,
            self._check_position_duration_alerts,
        )

    @track_component("risk_manager")
    async def check_position_allowed(self) -> tuple[bool, str]:
        async with self._state_lock:
            return await self.validator.check_position_allowed(
                self.emergency.emergency_shutdown_active,
                self.emergency.emergency_shutdown_reason,
                self.emergency.circuit_breaker.is_triggered(),
                self.emergency.circuit_breaker.get_reason(),
                self.tracker.current_balance,
                self.risk_level,
                self._get_actual_position_count,
            )

    @track_component("risk_manager", slow_threshold=3)
    async def calculate_position_size(self, balance: Decimal) -> Decimal:
        metrics = await self.tracker._get_cached_metrics()
        consecutive_losses = metrics["consecutive_losses"]

        return self.calculator.calculate_position_size(
            balance,
            self.risk_level,
            consecutive_losses,
            self.emergency.circuit_breaker.is_triggered(),
            self.emergency.emergency_shutdown_active,
        )

    @track_component("risk_manager", slow_threshold=2)
    def calculate_stop_loss(self, entry_price: Decimal) -> Decimal:
        return self.calculator.calculate_stop_loss(entry_price, self.risk_level)

    @track_component("risk_manager", slow_threshold=2)
    def calculate_take_profit(self, entry_price: Decimal) -> Decimal:
        return self.calculator.calculate_take_profit(entry_price, self.risk_level)

    @track_component("risk_manager", slow_threshold=1)
    def check_spread(self, spread_pct: Decimal) -> bool:
        dynamic_limit = self.calculator.get_dynamic_spread_limit(
            self.risk_level, self.emergency.circuit_breaker.is_triggered()
        )
        return self.validator.check_spread(spread_pct, dynamic_limit)

    @track_component("risk_manager")
    async def register_trade_result(
        self, symbol: str, pnl: Decimal, pnl_pct: Decimal, success: bool
    ):
        async def _trigger_emergency(reason: str):
            await self.emergency.trigger_emergency_shutdown(
                EmergencyShutdownReason[reason]
            )

        await self.tracker.register_trade_result(
            symbol,
            pnl,
            pnl_pct,
            success,
            self.risk_level,
            _trigger_emergency,
            self.duration.cleanup_duration_alerts,
            self._save_performance_metrics,
        )

    @track_component("risk_manager")
    async def manual_emergency_shutdown(self, reason: str = "Manual trigger"):
        await self.emergency.manual_emergency_shutdown(reason)

    @track_component("risk_manager")
    async def check_system_error_emergency(self, error_context: str = "Unknown"):
        await self.emergency.check_system_error_emergency(error_context)

    @track_component("risk_manager")
    async def reset_emergency_shutdown(self):
        result = await self.emergency.reset_emergency_shutdown()
        if result:
            self.risk_level = RiskLevel.LOW
            self.risk_violations = []
        return result

    @track_component("risk_manager", slow_threshold=20)
    async def get_status(self) -> dict:
        async with self._state_lock:
            metrics = await self.tracker._get_cached_metrics()
            emergency_config = self.config.get("emergency_conditions", {})

            limits_valid = await self.validator.validate_all_limits(
                self.tracker.current_balance, self._get_actual_position_count
            )

            return {
                "can_trade": self.validator.can_trade_safely(
                    self.emergency.emergency_shutdown_active,
                    self.emergency.circuit_breaker.is_triggered(),
                    self.emergency.circuit_breaker.is_in_grace_period(),
                    self.risk_level,
                    lambda: limits_valid,
                ),
                "emergency_shutdown": self.emergency.emergency_shutdown_active,
                "emergency_shutdown_reason": (
                    self.emergency.emergency_shutdown_reason.value
                    if self.emergency.emergency_shutdown_reason
                    else None
                ),
                "emergency_shutdown_time": self.emergency.emergency_shutdown_timestamp,
                "emergency_conditions": {
                    "drawdown_limit": emergency_config.get(
                        "close_on_daily_drawdown", 15.0
                    ),
                    "consecutive_losses_limit": emergency_config.get(
                        "close_on_consecutive_losses", 4
                    ),
                    "close_on_system_error": emergency_config.get(
                        "close_on_system_error", True
                    ),
                    "max_daily_drawdown": self.max_daily_drawdown,
                    "emergency_shutdown_threshold": self.emergency_shutdown_threshold,
                    "consecutive_losses_limit_active": self.consecutive_losses_limit,
                    "correlation_check": self.correlation_check,
                },
                "circuit_breaker": self.emergency.circuit_breaker.is_triggered(),
                "circuit_breaker_reason": self.emergency.circuit_breaker.get_reason(),
                "circuit_breaker_remaining": self.emergency.circuit_breaker.get_remaining_time(),
                "grace_period": self.emergency.circuit_breaker.is_in_grace_period(),
                "risk_level": self.risk_level.value,
                "daily_pnl": metrics["daily_pnl"],
                "daily_pnl_pct": metrics["daily_pnl_pct"],
                "daily_target_pct": self.daily_target_pct,
                "consecutive_losses": metrics["consecutive_losses"],
                "current_position_size_pct": self.calculator._calculate_position_size_percent(
                    self.risk_level,
                    metrics["consecutive_losses"],
                    self.emergency.circuit_breaker.is_triggered(),
                ),
                "spread_limit": self.calculator.get_dynamic_spread_limit(
                    self.risk_level, self.emergency.circuit_breaker.is_triggered()
                )
                * 100,
                "open_positions": await self._get_actual_position_count(),
                "aggressive_mode": self.aggressive_mode,
                "multi_level_breakers": self.emergency.circuit_breaker_levels,
                "duration_monitoring": {
                    "enabled": self.position_tracking_enabled,
                    "track_duration": self.track_duration,
                    "alert_thresholds": self.duration_alert_thresholds,
                    "avg_duration_target": self.avg_duration_target,
                    "max_comfort_hours": self.max_comfort_hours,
                    "duration_analytics": (
                        await self.duration.get_duration_analytics()
                        if self.track_duration
                        else {}
                    ),
                },
            }

    async def daily_reset(self):
        result = await self.tracker.daily_reset(self.risk_level, self.daily_target_pct)
        self.emergency.reset_circuit_breakers()
        self.risk_level = RiskLevel.LOW
        self.risk_violations = []
        return result

    async def get_daily_stats(self) -> dict:
        return await self.tracker.get_daily_stats(self.risk_level)

    async def get_risk_metrics(self) -> dict:
        async with self._state_lock:
            return {
                "daily_loss_limit_pct": self.daily_loss_limit_pct,
                "daily_target_pct": self.daily_target_pct,
                "max_consecutive_losses": self.max_consecutive_losses,
                "max_spread_pct": self.calculator.max_spread_pct * 100,
                "dynamic_spread_limit_pct": self.calculator.get_dynamic_spread_limit(
                    self.risk_level, self.emergency.circuit_breaker.is_triggered()
                )
                * 100,
                "min_balance_threshold": self.min_balance_threshold,
                "max_position_size_pct": self.calculator.max_position_size_pct,
                "min_position_size_pct": self.calculator.min_position_size_pct,
                "aggressive_mode": self.aggressive_mode,
                "risk_level": self.risk_level.value,
                "circuit_breaker_active": self.emergency.circuit_breaker.is_triggered(),
                "grace_period_active": self.emergency.circuit_breaker.is_in_grace_period(),
                "emergency_shutdown_active": self.emergency.emergency_shutdown_active,
                "multi_level_breakers": self.emergency.circuit_breaker_levels,
            }

    @track_component("risk_manager", slow_threshold=5)
    async def health_check(self) -> dict:
        metrics = await self.tracker._get_cached_metrics()
        emergency_health = await self.emergency.health_check(
            self.tracker.current_balance,
            self.min_balance_threshold,
            self.risk_level,
            metrics,
        )

        async def validate_all():
            return await self.validator.validate_all_limits(
                self.tracker.current_balance, self._get_actual_position_count
            )

        emergency_health["trading_allowed"] = self.validator.can_trade_safely(
            self.emergency.emergency_shutdown_active,
            self.emergency.circuit_breaker.is_triggered(),
            self.emergency.circuit_breaker.is_in_grace_period(),
            self.risk_level,
            validate_all,
        )
        emergency_health["balance"] = self.tracker.current_balance
        emergency_health["equity"] = self.tracker.total_equity
        emergency_health["daily_pnl_pct"] = metrics["daily_pnl_pct"]
        emergency_health["consecutive_losses"] = metrics["consecutive_losses"]
        emergency_health["risk_level"] = self.risk_level.value

        return emergency_health

    async def get_duration_analytics(self) -> dict:
        return await self.duration.get_duration_analytics()

    def cleanup_duration_alerts(self, symbol: str):
        self.duration.cleanup_duration_alerts(symbol)

    async def log_daily_duration_summary(self):
        await self.duration.log_daily_duration_summary()

    async def _save_performance_metrics(self):
        await self.tracker.save_performance_metrics(
            self.risk_level,
            self.emergency.circuit_breaker.is_triggered(),
            self._get_actual_position_count,
        )

        if self.log_duration_stats:
            from datetime import datetime

            await self.duration.save_duration_metrics_to_db(datetime.now())

    async def _check_position_duration_alerts(self):
        await self.duration.check_position_duration_alerts()

    async def _get_actual_position_count(self) -> int:
        return await self.tracker.get_actual_position_count()
