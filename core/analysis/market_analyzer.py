from decimal import Decimal

import numpy as np
import pandas as pd

from shared.constants import (
    STRONG_TREND_MULTIPLIER,
    TREND_SLOPE_WEIGHT_MID,
    TREND_SLOPE_WEIGHT_SHORT,
    TREND_WEIGHT_MID,
    TREND_WEIGHT_PRICE,
    TREND_WEIGHT_SHORT,
    VOLATILITY_HIGH_THRESHOLD,
    VOLATILITY_LOW_THRESHOLD,
    VOLATILITY_NORMAL_THRESHOLD,
    VOLATILITY_VERY_LOW_THRESHOLD,
    WEAK_TREND_MULTIPLIER,
)
from shared.enums import MarketCondition
from shared.observability.flow_tracker import track_component
from shared.observability.logger import debug, error, production, warning
from shared.types.state import state
from utils.decimal_math import to_decimal

MARKET_CONDITIONS_MAP = {
    "strong_uptrend": lambda trend, threshold: trend
    > threshold * STRONG_TREND_MULTIPLIER,
    "uptrend": lambda trend, threshold: threshold
    < trend
    <= threshold * STRONG_TREND_MULTIPLIER,
    "sideways": lambda trend, threshold: -threshold <= trend <= threshold,
    "downtrend": lambda trend, threshold: -threshold * STRONG_TREND_MULTIPLIER
    <= trend
    < -threshold,
    "strong_downtrend": lambda trend, threshold: trend
    < -threshold * STRONG_TREND_MULTIPLIER,
}

VOLATILITY_ADJUSTMENTS = {
    "very_low": {
        "threshold": VOLATILITY_VERY_LOW_THRESHOLD,
        "multiplier": Decimal("0.8"),
    },
    "low": {"threshold": VOLATILITY_LOW_THRESHOLD, "multiplier": Decimal("0.9")},
    "normal": {"threshold": VOLATILITY_NORMAL_THRESHOLD, "multiplier": Decimal("1.0")},
    "high": {"threshold": VOLATILITY_HIGH_THRESHOLD, "multiplier": Decimal("1.1")},
    "very_high": {"threshold": Decimal("inf"), "multiplier": Decimal("1.2")},
}

TRENDING_CONDITIONS = frozenset(
    ["strong_uptrend", "uptrend", "downtrend", "strong_downtrend"]
)


class MarketAnalyzer:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.market_config = config.get("market_analysis", {})
        self.trend_threshold = self.market_config.get("trend_threshold", 0.015)
        self.strong_trend_threshold = self.market_config.get(
            "strong_trend_threshold", 0.025
        )
        self.volatility_adjustment = self.market_config.get(
            "volatility_adjustment", True
        )
        self.current_conditions: dict[str, str] = {}
        self.is_testnet = config.get("mode", "").lower() == "testnet"

        production(
            "Market Analyzer inicializado",
            trend_threshold=self.trend_threshold,
            strong_trend_threshold=self.strong_trend_threshold,
            volatility_adjustment=self.volatility_adjustment,
            mode="testnet" if self.is_testnet else "mainnet",
        )

    def _get_min_candles_required(self, interval: str) -> int:
        base_requirements = {
            "1m": 30,
            "3m": 30,
            "5m": 30,
            "15m": 40,
            "30m": 40,
            "1h": 50,
            "2h": 50,
            "4h": 60,
            "6h": 60,
            "8h": 60,
            "12h": 60,
            "1d": 60,
            "3d": 60,
            "1w": 60,
            "1M": 60,
        }

        base_min = base_requirements.get(interval, 50)

        if self.is_testnet:
            return max(20, int(base_min * 0.6))
        return base_min

    @track_component("market_analyzer", slow_threshold=5)
    async def analyze_market(
        self, candles: pd.DataFrame, symbol: str, interval: str = "5m"
    ) -> MarketCondition:
        try:
            debug("Analisando mercado", symbol=symbol, interval=interval)

            min_required = self._get_min_candles_required(interval)

            if len(candles) < min_required:
                warning(
                    "Dados insuficientes para análise",
                    symbol=symbol,
                    candles=len(candles),
                    min_required=min_required,
                    interval=interval,
                    mode="testnet" if self.is_testnet else "mainnet",
                )
                return MarketCondition.UNKNOWN

            close = candles["close"]
            high = candles["high"]
            low = candles["low"]

            ema_9 = close.ewm(span=9, adjust=False).mean()
            ema_21 = close.ewm(span=21, adjust=False).mean()
            ema_50 = close.ewm(span=50, adjust=False).mean()

            current_price = to_decimal(close.iloc[-1])
            short_ema = to_decimal(ema_9.iloc[-1])
            mid_ema = to_decimal(ema_21.iloc[-1])
            long_ema = to_decimal(ema_50.iloc[-1])

            debug(
                "EMAs calculadas",
                symbol=symbol,
                price=current_price,
                ema9=short_ema,
                ema21=mid_ema,
                ema50=long_ema,
            )

            trend_short = (
                (short_ema - mid_ema) / mid_ema if mid_ema > 0 else Decimal("0")
            )
            trend_mid = (
                (mid_ema - long_ema) / long_ema if long_ema > 0 else Decimal("0")
            )
            price_position = (
                (current_price - mid_ema) / mid_ema if mid_ema > 0 else Decimal("0")
            )

            trend_strength = (
                (trend_short * TREND_WEIGHT_SHORT)
                + (trend_mid * TREND_WEIGHT_MID)
                + (price_position * TREND_WEIGHT_PRICE)
            )

            debug("Força da tendência", symbol=symbol, strength=trend_strength)

            slope_9 = to_decimal(self._calculate_slope(ema_9.tail(10)))
            slope_21 = to_decimal(self._calculate_slope(ema_21.tail(20)))

            debug("Slopes calculados", symbol=symbol, slope9=slope_9, slope21=slope_21)

            volatility = to_decimal(self._calculate_volatility(high, low, close))
            debug("Volatilidade", symbol=symbol, volatility=volatility)

            adjusted_threshold = to_decimal(self.trend_threshold)
            adjusted_strong_threshold = to_decimal(self.strong_trend_threshold)

            if self.volatility_adjustment and volatility > Decimal("2.0"):
                adjustment_factor = min(
                    Decimal("0.5"), (volatility - Decimal("2.0")) * Decimal("0.1")
                )
                adjusted_threshold *= Decimal("1") - adjustment_factor
                adjusted_strong_threshold *= Decimal("1") - adjustment_factor
                debug(
                    "Thresholds ajustados por volatilidade",
                    symbol=symbol,
                    threshold=adjusted_threshold,
                    strong_threshold=adjusted_strong_threshold,
                )

            combined_trend = (
                (trend_strength * WEAK_TREND_MULTIPLIER)
                + (slope_9 * TREND_SLOPE_WEIGHT_SHORT)
                + (slope_21 * TREND_SLOPE_WEIGHT_MID)
            )

            debug("Tendência combinada", symbol=symbol, combined_trend=combined_trend)

            condition = self._determine_market_condition(
                combined_trend,
                adjusted_threshold,
                adjusted_strong_threshold,
                volatility,
                high.tail(20),
                low.tail(20),
                symbol,
            )

            self.current_conditions[symbol] = condition.value
            await state.set_market_condition(symbol, condition.value)

            return condition

        except Exception as e:
            error("Erro ao analisar mercado", symbol=symbol, error=str(e))
            return MarketCondition.UNKNOWN

    def _determine_market_condition(
        self,
        combined_trend: Decimal,
        threshold: Decimal,
        strong_threshold: Decimal,
        volatility: Decimal,
        high: pd.Series,
        low: pd.Series,
        symbol: str,
    ) -> MarketCondition:

        condition_mapping = {
            "strong_uptrend": (MarketCondition.STRONG_UPTREND, "STRONG_UPTREND"),
            "uptrend": (MarketCondition.UPTREND, "UPTREND"),
            "strong_downtrend": (MarketCondition.STRONG_DOWNTREND, "STRONG_DOWNTREND"),
            "downtrend": (MarketCondition.DOWNTREND, "DOWNTREND"),
            "sideways": (MarketCondition.SIDEWAYS, "SIDEWAYS"),
        }

        for condition_key, checker in MARKET_CONDITIONS_MAP.items():
            if checker(
                combined_trend,
                (
                    threshold
                    if condition_key not in ["strong_uptrend", "strong_downtrend"]
                    else strong_threshold
                ),
            ):
                market_condition, condition_name = condition_mapping[condition_key]
                production(
                    "📊 MARKET CONDITION",
                    symbol=symbol,
                    condition=condition_name,
                    trend=round(combined_trend, 4),
                    threshold=round(threshold, 4),
                    volatility=round(volatility, 2),
                )
                return market_condition

        if volatility > Decimal("3.0"):
            production(
                "📊 MARKET CONDITION",
                symbol=symbol,
                condition="VOLATILE",
                volatility=round(volatility, 2),
            )
            return MarketCondition.VOLATILE

        recent_range = to_decimal(self._calculate_recent_range(high, low))
        if recent_range < Decimal("0.005"):
            production(
                "📊 MARKET CONDITION",
                symbol=symbol,
                condition="SIDEWAYS",
                range=round(recent_range, 5),
            )
            return MarketCondition.SIDEWAYS

        if abs(combined_trend) > to_decimal(threshold) * Decimal("0.5"):
            condition = (
                MarketCondition.UPTREND
                if combined_trend > 0
                else MarketCondition.DOWNTREND
            )
            production(
                "📊 MARKET CONDITION",
                symbol=symbol,
                condition=condition.value,
                trend=round(combined_trend, 4),
                strength="weak",
            )
            return condition

        debug("Sideways por tendência insuficiente", symbol=symbol)
        return MarketCondition.SIDEWAYS

    def _calculate_volatility(
        self, high: pd.Series, low: pd.Series, close: pd.Series
    ) -> Decimal:
        try:
            lookback = min(20, len(close) - 1)
            if lookback < 5:
                debug("Lookback muito curto", lookback=lookback)
                return Decimal("0.0")

            recent_high = high.iloc[-lookback:]
            recent_low = low.iloc[-lookback:]
            recent_close = close.iloc[-lookback:]

            tr1 = recent_high - recent_low
            tr2 = abs(recent_high - recent_close.shift())
            tr3 = abs(recent_low - recent_close.shift())

            true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = to_decimal(float(true_range.mean()))

            current_price = to_decimal(close.iloc[-1])
            volatility_pct = (
                (atr / current_price) * Decimal("100")
                if current_price > 0
                else Decimal("0")
            )

            return volatility_pct

        except Exception as e:
            error("Erro ao calcular volatilidade", error=str(e))
            return Decimal("0.0")

    def _calculate_slope(self, series: pd.Series) -> Decimal:
        try:
            if len(series) < 2:
                return Decimal("0.0")

            y = series.values
            x = np.arange(len(y))

            # Polyfit de grau 1 retorna [slope, intercept]
            slope, _ = np.polyfit(x, y, 1)

            avg_price = np.mean(y)
            normalized_slope = slope / avg_price if avg_price > 0 else 0

            return to_decimal(float(normalized_slope))

        except Exception as e:
            error("Erro ao calcular slope", error=str(e))
            return Decimal("0.0")

    def _calculate_recent_range(self, high: pd.Series, low: pd.Series) -> Decimal:
        try:
            highest = high.max()
            lowest = low.min()

            if lowest > 0:
                return to_decimal((highest - lowest) / lowest)

            return Decimal("0.0")

        except Exception as e:
            error("Erro ao calcular range", error=str(e))
            return Decimal("0.0")

    @track_component("market_analyzer", slow_threshold=2)
    def get_market_summary(self) -> dict:
        try:
            debug("Preparando resumo de mercado")

            if not self.current_conditions:
                return {
                    "total_symbols": 0,
                    "conditions": {},
                    "dominant_condition": "UNKNOWN",
                }

            condition_counts: dict[str, int] = {}
            for condition in self.current_conditions.values():
                condition_counts[condition] = condition_counts.get(condition, 0) + 1

            dominant_condition = max(condition_counts, key=condition_counts.get)

            summary = {
                "total_symbols": len(self.current_conditions),
                "conditions": condition_counts,
                "dominant_condition": dominant_condition,
                "last_update": pd.Timestamp.now(),
            }

            debug(
                "Resumo do mercado",
                total=summary["total_symbols"],
                conditions=summary["conditions"],
                dominant=summary["dominant_condition"],
            )

            return summary

        except Exception as e:
            error("Erro ao gerar resumo de mercado", error=str(e))
            return {
                "total_symbols": 0,
                "conditions": {},
                "dominant_condition": "UNKNOWN",
            }
