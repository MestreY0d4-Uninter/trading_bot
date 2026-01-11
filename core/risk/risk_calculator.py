from decimal import Decimal
from enum import Enum

from shared.observability.logger import debug, error, production, warning
from utils import decimal_math
from utils.decimal_math import ROUND_HALF_UP, to_decimal
from utils.validation_utils import is_numeric_valid


class RiskLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RiskCalculator:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.risk_config = config["risk"]
        self.aggressive_mode = config.get("aggressive_mode", {}).get("enabled", False)

        self.daily_loss_limit_pct = to_decimal(
            self.risk_config.get("daily_loss_limit_pct", 8.0)
        )
        self.max_consecutive_losses = self.risk_config.get("max_consecutive_losses", 5)

        is_testnet = self.config.get("mode", "").lower() == "testnet"
        if is_testnet:
            self.max_consecutive_losses = 999

        max_spread_config = self.risk_config.get("max_spread_pct", "0.5")
        max_spread_float = (
            float(max_spread_config)
            if isinstance(max_spread_config, str)
            else max_spread_config
        )
        if max_spread_float <= 0 or max_spread_float > 10:
            warning("Max spread inválido", spread=max_spread_float, usando="0.5")
            max_spread_config = "0.5"
        self.max_spread_pct = to_decimal(max_spread_config) / Decimal("100")

        # Usar int para limites percentuais (não são valores financeiros diretos)
        self.max_position_size_pct = min(
            self.risk_config.get("position_size_max", 25), 30
        )
        self.min_position_size_pct = max(
            self.risk_config.get("position_size_min", 1), 1
        )

        self.dynamic_spread_limit = self.max_spread_pct

    def calculate_position_size(
        self,
        balance: Decimal,
        risk_level: RiskLevel,
        consecutive_losses: int,
        circuit_breaker_triggered: bool,
        emergency_shutdown_active: bool,
    ) -> Decimal:
        if emergency_shutdown_active:
            raise ValueError(
                "Emergency shutdown ativo - não é possível calcular position size"
            )

        # Garantir que balance é Decimal
        balance = to_decimal(balance)

        if balance <= 0:
            raise ValueError(f"Balance inválido: {balance}")

        position_pct = self._calculate_position_size_percent(
            risk_level, consecutive_losses, circuit_breaker_triggered
        )
        position_size = decimal_math.calculate_position_size(balance, position_pct)

        position_size = self._apply_position_size_limits(balance, position_size)

        if position_size > balance * Decimal("0.999"):
            position_size = balance * Decimal("0.95")
            warning(
                "Position size limitado a 95% do balance",
                size=float(position_size),
                balance=float(balance),
            )

        return position_size

    def _calculate_position_size_percent(
        self,
        risk_level: RiskLevel,
        consecutive_losses: int,
        circuit_breaker_triggered: bool,
    ) -> Decimal:
        # Converter base para Decimal
        base_position_pct_raw = self.risk_config.get("position_size_pct", 6.0)

        if base_position_pct_raw <= 0 or base_position_pct_raw > 50:
            warning(
                "Position size base inválido", valor=base_position_pct_raw, usando=5.0
            )
            base_position_pct_raw = 5.0

        base_position_pct = Decimal(str(base_position_pct_raw))
        adjusted_pct = base_position_pct

        # Multiplicadores como Decimal
        risk_multipliers = {
            RiskLevel.LOW: Decimal("1.0"),
            RiskLevel.MEDIUM: Decimal("0.8"),
            RiskLevel.HIGH: Decimal("0.6"),
            RiskLevel.CRITICAL: Decimal("0.3"),
        }

        adjusted_pct *= risk_multipliers.get(risk_level, Decimal("0.5"))

        # Ajustes por consecutive losses (Decimal)
        if consecutive_losses >= 3:
            adjusted_pct *= Decimal("0.6")
        elif consecutive_losses >= 2:
            adjusted_pct *= Decimal("0.7")
        elif consecutive_losses == 1:
            adjusted_pct *= Decimal("0.85")

        # Aggressive mode
        if self.aggressive_mode and risk_level == RiskLevel.LOW:
            adjusted_pct *= Decimal("1.2")

        # Circuit breaker penalty
        if circuit_breaker_triggered:
            adjusted_pct *= Decimal("0.2")

        # Limites (converter para Decimal)
        min_pct = Decimal(str(self.min_position_size_pct))
        max_pct = Decimal(str(self.max_position_size_pct))
        final_pct = max(min_pct, min(adjusted_pct, max_pct))

        # Log se mudança significativa
        if abs(final_pct - base_position_pct) > Decimal("1.0"):
            production(
                "Position size ajustado por risco",
                original=float(base_position_pct),
                final=float(final_pct),
                risk_level=risk_level.value,
                consecutive_losses=consecutive_losses,
            )

        return final_pct

    def _apply_position_size_limits(
        self, balance: Decimal, position_size: Decimal
    ) -> Decimal:
        min_position_size = to_decimal(self.risk_config.get("min_order_size", 10.0))
        max_position_size = decimal_math.calculate_position_size(
            balance, self.max_position_size_pct
        )

        if position_size < min_position_size:
            if balance * Decimal("0.99") >= min_position_size:
                position_size = min(min_position_size, balance * Decimal("0.95"))
                debug("Position size ajustado para mínimo", size=float(position_size))
            else:
                error(
                    "Saldo insuficiente para position mínima",
                    balance=float(balance),
                    min_size=float(min_position_size),
                )
                raise ValueError(f"Saldo insuficiente: ${float(balance):.2f}")

        if position_size > max_position_size:
            position_size = max_position_size
            warning(
                "Position size limitado ao máximo",
                size=float(position_size),
                max_pct=self.max_position_size_pct,
            )

        return position_size

    def calculate_stop_loss(
        self, entry_price: Decimal, risk_level: RiskLevel
    ) -> Decimal:
        try:
            if not is_numeric_valid(entry_price) or entry_price <= 0:
                error("Entry price inválido", price=float(entry_price))
                return Decimal("0.0")

            entry_decimal = entry_price

            if entry_decimal.is_nan() or entry_decimal.is_infinite():
                error("Entry price é NaN ou infinito", price=float(entry_price))
                return Decimal("0.0")

            stop_loss_pct_config = Decimal(
                str(self.risk_config.get("stop_loss_pct", "3.5"))
            )
            stop_pct = stop_loss_pct_config / Decimal("100")

            if stop_pct <= 0 or stop_pct > Decimal("0.1"):
                warning("Stop loss % inválido", pct=float(stop_pct * 100), usando="3.5")
                stop_pct = Decimal("0.035")

            if self.aggressive_mode and risk_level == RiskLevel.LOW:
                stop_pct *= Decimal("1.2")

            stop_price = (entry_decimal * (Decimal("1") - stop_pct)).quantize(
                Decimal("0.00000001"), rounding=ROUND_HALF_UP
            )

            if stop_price <= 0 or stop_price >= entry_price:
                error(
                    "Stop price inválido",
                    stop=float(stop_price),
                    entry=float(entry_price),
                )
                return (entry_decimal * Decimal("0.965")).quantize(
                    Decimal("0.00000001"), rounding=ROUND_HALF_UP
                )

            return stop_price

        except Exception as e:
            error("Erro ao calcular stop loss", entry=float(entry_price), error=str(e))
            return (entry_price * Decimal("0.965")).quantize(
                Decimal("0.00000001"), rounding=ROUND_HALF_UP
            )

    def calculate_take_profit(
        self, entry_price: Decimal, risk_level: RiskLevel
    ) -> Decimal:
        try:
            if not is_numeric_valid(entry_price) or entry_price <= 0:
                error("Entry price inválido", price=float(entry_price))
                return Decimal("0.0")

            entry_decimal = entry_price

            if entry_decimal.is_nan() or entry_decimal.is_infinite():
                error("Entry price é NaN ou infinito", price=float(entry_price))
                return Decimal("0.0")

            take_profit_pct_config = Decimal(
                str(self.risk_config.get("take_profit_pct", 8.0))
            )
            take_profit_pct = take_profit_pct_config / Decimal("100")

            if take_profit_pct <= 0 or take_profit_pct > Decimal("0.2"):
                warning(
                    "Take profit % inválido",
                    pct=float(take_profit_pct * 100),
                    usando=8.0,
                )
                take_profit_pct = Decimal("0.080")

            if self.aggressive_mode and risk_level == RiskLevel.LOW:
                take_profit_pct *= Decimal("0.9")

            tp_price = (entry_decimal * (Decimal("1") + take_profit_pct)).quantize(
                Decimal("0.00000001"), rounding=ROUND_HALF_UP
            )

            if tp_price <= entry_price:
                error(
                    "Take profit inválido", tp=float(tp_price), entry=float(entry_price)
                )
                return (entry_decimal * Decimal("1.080")).quantize(
                    Decimal("0.00000001"), rounding=ROUND_HALF_UP
                )

            return tp_price

        except Exception as e:
            error(
                "Erro ao calcular take profit", entry=float(entry_price), error=str(e)
            )
            return (entry_price * Decimal("1.080")).quantize(
                Decimal("0.00000001"), rounding=ROUND_HALF_UP
            )

    def assess_risk_level(
        self, daily_pnl_pct: float, consecutive_losses: int
    ) -> RiskLevel:
        # Thresholds de risco (percentuais, não valores monetários)
        critical_threshold = self.daily_loss_limit_pct * Decimal("0.9")
        high_threshold = self.daily_loss_limit_pct * Decimal("0.7")
        medium_threshold = self.daily_loss_limit_pct * Decimal("0.4")

        if (
            daily_pnl_pct <= -float(critical_threshold)
            or consecutive_losses >= self.max_consecutive_losses
        ):
            return RiskLevel.CRITICAL
        elif (
            daily_pnl_pct <= -float(high_threshold)
            or consecutive_losses >= self.max_consecutive_losses - 1
        ):
            return RiskLevel.HIGH
        elif daily_pnl_pct <= -float(medium_threshold) or consecutive_losses >= 2:
            return RiskLevel.MEDIUM
        else:
            return RiskLevel.LOW

    def get_dynamic_spread_limit(
        self, risk_level: RiskLevel, circuit_breaker_triggered: bool
    ) -> Decimal:
        base_limit = self.dynamic_spread_limit

        risk_multipliers = {
            RiskLevel.LOW: Decimal("1.0"),
            RiskLevel.MEDIUM: Decimal("0.9"),
            RiskLevel.HIGH: Decimal("0.7"),
            RiskLevel.CRITICAL: Decimal("0.6"),
        }

        adjusted_limit = base_limit * risk_multipliers.get(risk_level, Decimal("0.6"))

        if self.aggressive_mode and risk_level == RiskLevel.LOW:
            adjusted_limit *= Decimal("1.3")

        if circuit_breaker_triggered:
            adjusted_limit *= Decimal("0.7")

        return max(adjusted_limit, self.max_spread_pct * Decimal("0.2"))
