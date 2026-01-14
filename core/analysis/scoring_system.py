from decimal import Decimal

from shared.constants import AGGRESSIVE_MODE_MIN_SCORE, MIN_ENTRY_SCORE
from shared.observability.logger import debug, error, production, warning
from utils.decimal_math import to_decimal


class ScoringSystem:
    # RSI Weights
    RSI_VERY_OVERSOLD_LIMIT = Decimal("30")
    RSI_WEIGHT_VERY_OVERSOLD = Decimal("35")
    RSI_WEIGHT_OVERSOLD_MAX = Decimal("25")
    RSI_NEUTRAL_LOW_LIMIT = Decimal("45")
    RSI_WEIGHT_NEUTRAL_LOW_MAX = Decimal("10")
    RSI_NEUTRAL_HIGH_LIMIT = Decimal("55")
    RSI_WEIGHT_NEUTRAL_HIGH = Decimal("5")
    RSI_WEIGHT_OVERBOUGHT = Decimal("-10")

    # Bollinger Bands Weights
    BB_NEAR_BAND_THRESHOLD = Decimal("0.1")
    BB_WEIGHT_BELOW_LOWER = Decimal("30")
    BB_WEIGHT_NEAR_LOWER = Decimal("20")
    BB_WEIGHT_ABOVE_UPPER = Decimal("-30")
    BB_WEIGHT_NEAR_UPPER = Decimal("-20")

    # Volume Weights
    VOLUME_RATIO_HUGE = Decimal("2.0")
    VOLUME_WEIGHT_HUGE = Decimal("15")
    VOLUME_RATIO_HIGH = Decimal("1.0")
    VOLUME_WEIGHT_HIGH = Decimal("8")
    VOLUME_RATIO_NORMAL = Decimal("0.5")
    VOLUME_WEIGHT_NORMAL = Decimal("5")
    VOLUME_WEIGHT_LOW = Decimal("-3")

    # Momentum Weights
    MOMENTUM_STRONG_THRESHOLD = Decimal("0.005")
    MOMENTUM_WEIGHT_STRONG = Decimal("8")
    MOMENTUM_NORMAL_THRESHOLD = Decimal("0.002")
    MOMENTUM_WEIGHT_NORMAL = Decimal("5")
    MOMENTUM_WEIGHT_WEAK = Decimal("2")

    # MACD Weights
    MACD_STRONG_THRESHOLD = Decimal("0.01")
    MACD_WEIGHT_STRONG = Decimal("8")
    MACD_NORMAL_THRESHOLD = Decimal("0.001")
    MACD_WEIGHT_NORMAL = Decimal("5")
    MACD_WEIGHT_WEAK = Decimal("2")
    MACD_NEGATIVE_THRESHOLD = Decimal("-0.001")
    MACD_WEIGHT_NEGATIVE = Decimal("-5")

    # Volatility Weights
    VOLATILITY_LOW_LIMIT = Decimal("0.3")
    VOLATILITY_PENALTY_LOW = Decimal("-10")
    VOLATILITY_NORMAL_LIMIT = Decimal("2.0")
    VOLATILITY_BONUS_NORMAL = Decimal("5")
    VOLATILITY_HIGH_LIMIT = Decimal("15.0")
    VOLATILITY_PENALTY_HIGH = Decimal("-5")
    VOLATILITY_EXTREME_LIMIT = Decimal("30.0")
    VOLATILITY_PENALTY_EXTREME = Decimal("-10")

    # Spread Penalty
    SPREAD_HIGH_THRESHOLD = Decimal("0.5")
    SPREAD_NORMAL_THRESHOLD = Decimal("0.3")

    # Trend Weights
    TREND_WEIGHT_STRONG_UP = Decimal("15")
    TREND_WEIGHT_UP = Decimal("10")
    TREND_WEIGHT_SIDEWAYS = Decimal("-10")
    TREND_WEIGHT_DOWN_AGGRESSIVE = Decimal("-5")
    TREND_WEIGHT_DOWN = Decimal("-15")
    TREND_WEIGHT_VOLATILE = Decimal("-5")

    def __init__(self, config: dict) -> None:
        self.config = config
        self.strategy_config = config.get("strategy", {})
        self.aggressive_mode = config.get("aggressive_mode", {}).get("enabled", False)
        self.score_reduction = config.get("aggressive_mode", {}).get(
            "score_reduction", 10
        )

        self.min_score = self.strategy_config.get("min_entry_score", MIN_ENTRY_SCORE)
        self.rsi_oversold = self.strategy_config.get("rsi_oversold", 30)
        self.rsi_overbought = self.strategy_config.get("rsi_overbought", 70)
        self.bb_lower_threshold = self.strategy_config.get("bb_lower_threshold", 0.12)
        self.min_volume_ratio = self.strategy_config.get("min_volume_ratio", 0.4)

        production(
            "Sistema de Scoring inicializado",
            min_score=self.min_score,
            rsi_oversold=self.rsi_oversold,
            rsi_overbought=self.rsi_overbought,
            bb_threshold=self.bb_lower_threshold,
            min_volume_ratio=self.min_volume_ratio,
            aggressive_mode=self.aggressive_mode,
        )

    def _calculate_rsi_score(
        self, rsi: Decimal, oversold: Decimal, overbought: Decimal
    ) -> Decimal:
        if rsi < self.RSI_VERY_OVERSOLD_LIMIT:
            return self.RSI_WEIGHT_VERY_OVERSOLD
        elif rsi < oversold:
            return min(self.RSI_WEIGHT_OVERSOLD_MAX, (oversold - rsi) * Decimal("2"))
        elif rsi < self.RSI_NEUTRAL_LOW_LIMIT:
            return min(
                self.RSI_WEIGHT_NEUTRAL_LOW_MAX, (self.RSI_NEUTRAL_LOW_LIMIT - rsi)
            )
        elif rsi < self.RSI_NEUTRAL_HIGH_LIMIT:
            return self.RSI_WEIGHT_NEUTRAL_HIGH
        elif rsi < overbought:
            return Decimal("0")
        else:
            return self.RSI_WEIGHT_OVERBOUGHT

    def _calculate_bb_score(
        self, price: Decimal, bb_lower: Decimal, bb_upper: Decimal, bb_width: Decimal
    ) -> Decimal:
        if price < bb_lower:
            return self.BB_WEIGHT_BELOW_LOWER
        elif bb_lower <= price < bb_lower + bb_width * self.BB_NEAR_BAND_THRESHOLD:
            return self.BB_WEIGHT_NEAR_LOWER
        elif price > bb_upper:
            return self.BB_WEIGHT_ABOVE_UPPER
        elif bb_upper - bb_width * self.BB_NEAR_BAND_THRESHOLD < price <= bb_upper:
            return self.BB_WEIGHT_NEAR_UPPER
        else:
            return Decimal("0")

    def _calculate_volume_score(
        self, volume_ratio: Decimal, min_ratio: Decimal
    ) -> Decimal:
        if volume_ratio >= self.VOLUME_RATIO_HUGE:
            return self.VOLUME_WEIGHT_HUGE
        elif volume_ratio >= self.VOLUME_RATIO_HIGH:
            return self.VOLUME_WEIGHT_HIGH
        elif volume_ratio >= self.VOLUME_RATIO_NORMAL:
            return self.VOLUME_WEIGHT_NORMAL
        elif volume_ratio >= min_ratio:
            return Decimal("0")
        else:
            return self.VOLUME_WEIGHT_LOW

    def _calculate_momentum_score(self, momentum: Decimal) -> Decimal:
        if momentum > self.MOMENTUM_STRONG_THRESHOLD:
            return self.MOMENTUM_WEIGHT_STRONG
        elif momentum > self.MOMENTUM_NORMAL_THRESHOLD:
            return self.MOMENTUM_WEIGHT_NORMAL
        elif momentum > 0:
            return self.MOMENTUM_WEIGHT_WEAK
        else:
            return Decimal("0")

    def _calculate_macd_score(self, macd_histogram: Decimal) -> Decimal:
        if macd_histogram > self.MACD_STRONG_THRESHOLD:
            return self.MACD_WEIGHT_STRONG
        elif macd_histogram > self.MACD_NORMAL_THRESHOLD:
            return self.MACD_WEIGHT_NORMAL
        elif macd_histogram > 0:
            return self.MACD_WEIGHT_WEAK
        elif macd_histogram > self.MACD_NEGATIVE_THRESHOLD:
            return Decimal("0")
        else:
            return self.MACD_WEIGHT_NEGATIVE

    def _calculate_volatility_score(self, volatility: Decimal) -> Decimal:
        if volatility < self.VOLATILITY_LOW_LIMIT:
            return self.VOLATILITY_PENALTY_LOW
        elif volatility < self.VOLATILITY_NORMAL_LIMIT:
            return self.VOLATILITY_BONUS_NORMAL
        elif volatility < self.VOLATILITY_HIGH_LIMIT:
            return Decimal("0")
        elif volatility < self.VOLATILITY_EXTREME_LIMIT:
            return self.VOLATILITY_PENALTY_HIGH
        else:
            return self.VOLATILITY_PENALTY_EXTREME

    def _calculate_spread_penalty(self, spread_pct: Decimal) -> Decimal:
        if spread_pct > self.SPREAD_HIGH_THRESHOLD:
            return min(Decimal("20"), spread_pct * Decimal("30"))
        elif spread_pct > self.SPREAD_NORMAL_THRESHOLD:
            return min(Decimal("10"), spread_pct * Decimal("20"))
        return Decimal("0")

    def _calculate_trend_score(self, market_condition: str) -> Decimal:
        market_condition_upper = str(market_condition).upper()

        if "STRONG_UPTREND" in market_condition_upper:
            return self.TREND_WEIGHT_STRONG_UP
        elif "UPTREND" in market_condition_upper:
            return self.TREND_WEIGHT_UP
        elif "SIDEWAYS" in market_condition_upper:
            return self.TREND_WEIGHT_SIDEWAYS
        elif "DOWNTREND" in market_condition_upper:
            return (
                self.TREND_WEIGHT_DOWN_AGGRESSIVE
                if self.aggressive_mode
                else self.TREND_WEIGHT_DOWN
            )
        elif "VOLATILE" in market_condition_upper:
            return self.TREND_WEIGHT_VOLATILE
        else:
            return Decimal("0")

    def calculate_score(
        self, indicators: dict, market_condition: str, spread_pct: Decimal
    ) -> tuple[Decimal, dict]:
        try:
            debug(
                "Iniciando cálculo de score",
                market_condition=market_condition,
                spread_pct=spread_pct,
            )

            # Verificação de indicadores críticos
            critical_indicators = ["rsi", "bb_lower", "bb_upper", "volume_ratio"]
            for ind in critical_indicators:
                if indicators.get(ind) is None:
                    warning(
                        "Indicador crítico ausente. Signal invalidado.", indicator=ind
                    )
                    return Decimal("0"), {"error": f"Missing {ind}"}

            score = Decimal("0")
            components = {}

            rsi = to_decimal(indicators.get("rsi", 50))
            debug("RSI", value=rsi)
            rsi_score = self._calculate_rsi_score(
                rsi, to_decimal(self.rsi_oversold), to_decimal(self.rsi_overbought)
            )
            score += rsi_score
            components["rsi"] = rsi_score

            current_price = to_decimal(indicators.get("price", 0))
            bb_lower = to_decimal(indicators.get("bb_lower", 0))
            bb_upper = to_decimal(indicators.get("bb_upper", 0))
            bb_width = bb_upper - bb_lower if bb_upper > bb_lower else Decimal("0")

            debug(
                "BB Values",
                price=current_price,
                lower=bb_lower,
                upper=bb_upper,
                width=bb_width,
            )

            bb_score = self._calculate_bb_score(
                current_price, bb_lower, bb_upper, bb_width
            )
            score += bb_score
            components["bb_position"] = bb_score

            volume_ratio = to_decimal(indicators.get("volume_ratio", 0))
            debug("Volume Ratio", value=volume_ratio)
            volume_score = self._calculate_volume_score(
                volume_ratio, to_decimal(self.min_volume_ratio)
            )
            score += volume_score
            components["volume"] = volume_score

            debug("Market Condition", value=market_condition)
            trend_score = self._calculate_trend_score(market_condition)
            score += trend_score
            components["trend"] = trend_score

            momentum = to_decimal(indicators.get("momentum", 0))
            debug("Momentum", value=momentum)
            momentum_score = self._calculate_momentum_score(momentum)
            score += momentum_score
            components["momentum"] = momentum_score

            debug("Spread", value=spread_pct)
            spread_penalty = self._calculate_spread_penalty(spread_pct)
            score -= spread_penalty
            components["spread"] = -spread_penalty

            macd_histogram = to_decimal(indicators.get("macd_histogram", 0))
            debug("MACD Histogram", value=macd_histogram)
            macd_score = self._calculate_macd_score(macd_histogram)
            score += macd_score
            components["macd"] = macd_score

            volatility = to_decimal(indicators.get("volatility", 1.5))
            debug("Volatility", value=volatility)
            volatility_score = self._calculate_volatility_score(volatility)
            score += volatility_score
            components["volatility"] = volatility_score

            debug("Score parcial", score=score)

            if self.aggressive_mode and score >= AGGRESSIVE_MODE_MIN_SCORE:
                original_score = score
                score = max(Decimal("30"), score - to_decimal(self.score_reduction))
                components["aggressive_adjustment"] = score - original_score
                debug("Ajuste modo agressivo", original=original_score, adjusted=score)

            score = max(Decimal("-100"), min(Decimal("100"), score))

            # BUG FIX #26: Filtro de momentum para sideways
            # TESTNET MODE: Filtro DESABILITADO para permitir validação de código
            # PRODUÇÃO: Habilitar filtro score < 75 em sideways para bloquear entradas
            # market_condition_upper = str(market_condition).upper()
            # if "SIDEWAYS" in market_condition_upper:
            #     if score < 75:
            #         production(" ENTRADA BLOQUEADA: Mercado sideways", ...)
            #         return 0, components

            production("Score final calculado", score=score)
            debug("Componentes", components=components)

            return score, components

        except Exception as e:
            error("Erro ao calcular score", error=str(e))
            return Decimal("0"), {}

    def validate_exit_conditions(
        self, position_data: dict, current_price: float
    ) -> str:
        try:
            position_data.get("entry_price", 0)
            stop_loss = position_data.get("stop_loss", 0)
            take_profit = position_data.get("take_profit", 0)

            if current_price <= stop_loss:
                return "STOP_LOSS"
            elif current_price >= take_profit:
                return "TAKE_PROFIT"

            return "HOLD"

        except Exception as e:
            error("Erro ao validar condições de saída", error=str(e))
            return "HOLD"
