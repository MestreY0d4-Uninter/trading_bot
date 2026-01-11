from decimal import Decimal

from shared.constants import AGGRESSIVE_MODE_MIN_SCORE, MIN_ENTRY_SCORE
from shared.observability.logger import debug, error, production
from utils.decimal_math import to_decimal


class ScoringSystem:
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
        if rsi < 30:
            return Decimal("35")
        elif rsi < oversold:
            return min(Decimal("25"), (oversold - rsi) * Decimal("2"))
        elif rsi < 45:
            return min(Decimal("10"), (45 - rsi))
        elif rsi < 55:
            return Decimal("5")
        elif rsi < overbought:
            return Decimal("0")
        else:
            return Decimal("-10")

    def _calculate_bb_score(
        self, price: Decimal, bb_lower: Decimal, bb_upper: Decimal, bb_width: Decimal
    ) -> Decimal:
        if price < bb_lower:
            return Decimal("30")
        elif bb_lower <= price < bb_lower + bb_width * Decimal("0.1"):
            return Decimal("20")
        elif price > bb_upper:
            return Decimal("-30")
        elif bb_upper - bb_width * Decimal("0.1") < price <= bb_upper:
            return Decimal("-20")
        else:
            return Decimal("0")

    def _calculate_volume_score(
        self, volume_ratio: Decimal, min_ratio: Decimal
    ) -> Decimal:
        if volume_ratio >= Decimal("2.0"):
            return Decimal("15")
        elif volume_ratio >= Decimal("1.0"):
            return Decimal("8")
        elif volume_ratio >= Decimal("0.5"):
            return Decimal("5")
        elif volume_ratio >= min_ratio:
            return Decimal("0")
        else:
            return Decimal("-3")

    def _calculate_momentum_score(self, momentum: Decimal) -> Decimal:
        if momentum > Decimal("0.005"):
            return Decimal("8")
        elif momentum > Decimal("0.002"):
            return Decimal("5")
        elif momentum > 0:
            return Decimal("2")
        else:
            return Decimal("0")

    def _calculate_macd_score(self, macd_histogram: Decimal) -> Decimal:
        if macd_histogram > Decimal("0.01"):
            return Decimal("8")
        elif macd_histogram > Decimal("0.001"):
            return Decimal("5")
        elif macd_histogram > 0:
            return Decimal("2")
        elif macd_histogram > Decimal("-0.001"):
            return Decimal("0")
        else:
            return Decimal("-5")

    def _calculate_volatility_score(self, volatility: Decimal) -> Decimal:
        if volatility < Decimal("0.3"):
            return Decimal("-10")
        elif volatility < Decimal("2.0"):
            return Decimal("5")
        elif volatility < Decimal("15.0"):
            return Decimal("0")
        elif volatility < Decimal("30.0"):
            return Decimal("-5")
        else:
            return Decimal("-10")

    def _calculate_spread_penalty(self, spread_pct: Decimal) -> Decimal:
        if spread_pct > Decimal("0.5"):
            return min(Decimal("20"), spread_pct * Decimal("30"))
        elif spread_pct > Decimal("0.3"):
            return min(Decimal("10"), spread_pct * Decimal("20"))
        return Decimal("0")

    def _calculate_trend_score(self, market_condition: str) -> Decimal:
        market_condition_upper = str(market_condition).upper()

        if "STRONG_UPTREND" in market_condition_upper:
            return Decimal("15")
        elif "UPTREND" in market_condition_upper:
            return Decimal("10")
        elif "SIDEWAYS" in market_condition_upper:
            return Decimal("-10")
        elif "DOWNTREND" in market_condition_upper:
            return Decimal("-5") if self.aggressive_mode else Decimal("-15")
        elif "VOLATILE" in market_condition_upper:
            return Decimal("-5")
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

    def calculate_entry_score(
        self, indicators: dict, market_condition: str, spread_pct: Decimal
    ) -> tuple[Decimal, dict]:
        score, components = self.calculate_score(
            indicators, market_condition, spread_pct
        )
        return score, components

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
