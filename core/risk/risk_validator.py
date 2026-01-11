import threading
from decimal import Decimal

from shared.observability.flow_tracker import track_component
from shared.observability.logger import error, warning
from shared.types.state import state
from utils import decimal_math
from utils.validation_utils import is_numeric_valid

from .risk_calculator import RiskLevel


class RiskValidator:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.risk_config = config["risk"]

        self.daily_loss_limit_pct = self.risk_config.get("daily_loss_limit_pct", 8.0)
        self.max_consecutive_losses = self.risk_config.get("max_consecutive_losses", 5)

        is_testnet = self.config.get("mode", "").lower() == "testnet"
        if is_testnet:
            self.max_consecutive_losses = 999

        from decimal import Decimal

        max_spread_config = self.risk_config.get("max_spread_pct", "0.5")
        max_spread_float = (
            float(max_spread_config)
            if isinstance(max_spread_config, str)
            else max_spread_config
        )
        if max_spread_float <= 0 or max_spread_float > 10:
            max_spread_config = "0.5"
        self.max_spread_pct = Decimal(str(max_spread_config)) / Decimal("100")

        self.min_balance_threshold = self.config.get("sanity_checks", {}).get(
            "min_balance_required", 10.0
        )

        self._spread_lock = threading.Lock()

    @track_component("risk_validator", slow_threshold=50)
    async def check_position_allowed(
        self,
        emergency_shutdown_active: bool,
        emergency_shutdown_reason,
        circuit_breaker_triggered: bool,
        circuit_breaker_reason: str,
        current_balance: Decimal,
        risk_level: RiskLevel,
        position_count_func,
    ) -> tuple[bool, str]:
        if emergency_shutdown_active:
            reason_value = (
                emergency_shutdown_reason.value
                if emergency_shutdown_reason
                else "unknown"
            )
            return False, f"Emergency shutdown ativo: {reason_value}"

        if circuit_breaker_triggered:
            return False, circuit_breaker_reason

        metrics = await state.get_all_metrics()
        daily_pnl_pct = metrics.get("daily_pnl_pct", 0)
        consecutive_losses = metrics.get("consecutive_losses", 0)

        violations = self._check_risk_violations(
            daily_pnl_pct, consecutive_losses, risk_level
        )
        if violations:
            return False, f"Violações de risco: {', '.join(violations)}"

        if current_balance < self.min_balance_threshold:
            return False, f"Saldo insuficiente: ${current_balance:.2f}"

        max_positions = self.risk_config.get("max_positions", 2)
        open_positions = await position_count_func()
        if open_positions >= max_positions:
            return False, f"Máximo de posições: {open_positions}/{max_positions}"

        if current_balance > 0:
            from .risk_calculator import RiskCalculator

            calculator = RiskCalculator(self.config)
            position_pct = calculator._calculate_position_size_percent(
                risk_level, consecutive_losses, circuit_breaker_triggered
            )
            position_value = decimal_math.calculate_position_size(
                current_balance, position_pct
            )
            min_order_size = Decimal(str(self.risk_config.get("min_order_size", 10.0)))

            if position_value < min_order_size:
                return False, f"Position size muito pequena: ${position_value:.2f}"

        return True, "OK"

    def _check_risk_violations(
        self, daily_pnl_pct: float, consecutive_losses: int, risk_level: RiskLevel
    ) -> list[str]:
        violations = []

        if daily_pnl_pct <= -self.daily_loss_limit_pct:
            violations.append(f"Daily loss limit: {daily_pnl_pct:.1f}%")

        if consecutive_losses >= self.max_consecutive_losses:
            violations.append(f"Max consecutive losses: {consecutive_losses}")

        if risk_level == RiskLevel.CRITICAL:
            violations.append("Risk level critical")

        return violations

    @track_component("risk_validator", slow_threshold=50)
    async def validate_all_limits(
        self, current_balance: Decimal, position_count_func
    ) -> bool:
        metrics = await state.get_all_metrics()
        daily_pnl_pct = metrics.get("daily_pnl_pct", 0)
        consecutive_losses = metrics.get("consecutive_losses", 0)

        limit_checks = [
            (daily_pnl_pct > -self.daily_loss_limit_pct, "Daily loss limit"),
            (consecutive_losses < self.max_consecutive_losses, "Consecutive losses"),
            (current_balance >= self.min_balance_threshold, "Min balance"),
            (
                await position_count_func() <= self.risk_config.get("max_positions", 2),
                "Max positions",
            ),
        ]

        for check, name in limit_checks:
            if not check:
                error(f"Limite violado: {name}")
                return False

        return True

    @track_component("risk_validator", slow_threshold=10)
    def check_spread(self, spread_pct: Decimal, dynamic_spread_limit: Decimal) -> bool:
        with self._spread_lock:
            if not isinstance(spread_pct, (int, float, Decimal)):
                warning("Spread não é numérico", spread=spread_pct)
                return False

            if not is_numeric_valid(spread_pct):
                warning("Spread é NaN ou infinito", spread=spread_pct)
                return False

            if spread_pct < 0:
                warning("Spread negativo", spread=spread_pct)
                return False

            if spread_pct > 10:
                warning("Spread muito alto", spread_pct=spread_pct)
                return False

            is_valid = (spread_pct / 100) <= dynamic_spread_limit

            if not is_valid:
                warning(
                    "Spread rejeitado",
                    spread_pct=spread_pct,
                    limit_pct=dynamic_spread_limit * 100,
                )

            return is_valid

    @track_component("risk_validator", slow_threshold=20)
    def can_trade_safely(
        self,
        emergency_shutdown_active: bool,
        circuit_breaker_triggered: bool,
        circuit_breaker_in_grace: bool,
        risk_level: RiskLevel,
        validate_limits_func,
    ) -> bool:
        if emergency_shutdown_active:
            return False

        if circuit_breaker_triggered and not circuit_breaker_in_grace:
            return False

        if risk_level == RiskLevel.CRITICAL:
            return False

        if not validate_limits_func():
            return False

        return True

    @track_component("risk_validator", slow_threshold=10)
    def validate_balance(self, balance: float) -> bool:
        if not isinstance(balance, (int, float, Decimal)):
            error("Saldo tipo inválido", balance=balance, type=type(balance).__name__)
            return False

        if balance < 0:
            error("Saldo negativo", balance=balance)
            return False

        if isinstance(balance, (int, float)) and not is_numeric_valid(balance):
            error("Saldo é NaN ou infinito", balance=balance)
            return False

        return True

    @track_component("risk_validator", slow_threshold=50)
    def validate_risk_reward_parameters(self):
        stop_loss_pct = self.risk_config.get("stop_loss_pct", "3.5")
        take_profit_pct = self.risk_config.get("take_profit_pct", "8.0")
        position_size_pct = self.risk_config.get("position_size_pct", "6.0")

        risk_reward_ratio = take_profit_pct / stop_loss_pct if stop_loss_pct > 0 else 0
        if risk_reward_ratio < 2.0:
            warning(
                "Risk/Reward ratio baixo - pode causar baixo win rate",
                stop_loss=stop_loss_pct,
                take_profit=take_profit_pct,
                ratio=risk_reward_ratio,
                recomendado=">=2.0",
            )
        else:
            from shared.observability.logger import production

            production(
                "✅ Risk/Reward ratio otimizado",
                stop_loss=stop_loss_pct,
                take_profit=take_profit_pct,
                ratio=risk_reward_ratio,
            )

        if position_size_pct > 15:
            warning(
                "Position size muito alto - risco elevado",
                position_size=position_size_pct,
            )
        elif position_size_pct < 5:
            warning(
                "Position size muito baixo - ganhos limitados",
                position_size=position_size_pct,
            )

        from shared.observability.logger import production

        production(
            "Parâmetros risk/reward validados",
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            risk_reward_ratio=risk_reward_ratio,
            position_size_pct=position_size_pct,
            daily_loss_limit=self.daily_loss_limit_pct,
            consecutive_losses_limit=self.max_consecutive_losses,
        )
