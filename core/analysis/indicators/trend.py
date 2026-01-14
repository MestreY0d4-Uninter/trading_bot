from decimal import Decimal

import pandas as pd

from shared.observability.flow_tracker import track_component
from shared.observability.logger import error, production
from utils.decimal_math import to_decimal


class TrendIndicators:
    def __init__(self) -> None:
        self.default_params = {"ema_short": 9, "ema_long": 21, "ema_trend": 50}

        production("TrendIndicators inicializado")

    @track_component("indicators", slow_threshold=150)
    def calculate_trend_strength(self, candles: pd.DataFrame) -> tuple[str, Decimal]:
        try:
            if len(candles) < 50:
                return "NEUTRAL", Decimal("0")

            close = candles["close"]

            ema_short = pd.Series.ewm(
                close, span=self.default_params["ema_short"], adjust=False
            ).mean()
            ema_long = pd.Series.ewm(
                close, span=self.default_params["ema_long"], adjust=False
            ).mean()

            high = candles["high"]
            low = candles["low"]
            adx = self._calculate_adx(high, low, close)

            current_ema_short = (
                to_decimal(ema_short.iloc[-1])
                if not ema_short.empty and not pd.isna(ema_short.iloc[-1])
                else Decimal("0")
            )
            current_ema_long = (
                to_decimal(ema_long.iloc[-1])
                if not ema_long.empty and not pd.isna(ema_long.iloc[-1])
                else Decimal("0")
            )

            if current_ema_short > current_ema_long:
                direction = "UP"
            elif current_ema_short < current_ema_long:
                direction = "DOWN"
            else:
                direction = "NEUTRAL"

            strength = min(Decimal("100"), adx)

            return direction, strength

        except Exception as e:
            error("Erro ao calcular força da tendência", error=str(e))
            return "NEUTRAL", Decimal("0")

    def _calculate_adx(
        self, high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
    ) -> Decimal:
        try:
            import numpy as np
            import talib

            high_array = high.to_numpy()
            low_array = low.to_numpy()
            close_array = close.to_numpy()

            adx_array = talib.ADX(high_array, low_array, close_array, timeperiod=period)

            if (
                adx_array is not None
                and len(adx_array) > 0
                and not np.isnan(adx_array[-1])
            ):
                return to_decimal(float(adx_array[-1]))

            return Decimal("0")

        except Exception as e:
            error("Erro ao calcular ADX", error=str(e))
            return Decimal("0")

    @track_component("indicators", slow_threshold=100)
    def calculate_ema_crossover(self, candles: pd.DataFrame) -> dict[str, any]:
        try:
            import numpy as np
            import talib

            close_series = candles["close"].astype(float)
            close_array = close_series.to_numpy()

            ema_short = talib.EMA(
                close_array, timeperiod=self.default_params["ema_short"]
            )
            ema_long = talib.EMA(
                close_array, timeperiod=self.default_params["ema_long"]
            )

            current_short = (
                to_decimal(float(ema_short[-1]))
                if ema_short.size > 0 and not np.isnan(ema_short[-1])
                else Decimal("0")
            )
            current_long = (
                to_decimal(float(ema_long[-1]))
                if ema_long.size > 0 and not np.isnan(ema_long[-1])
                else Decimal("0")
            )
            prev_short = (
                to_decimal(float(ema_short[-2]))
                if ema_short.size > 1 and not np.isnan(ema_short[-2])
                else Decimal("0")
            )
            prev_long = (
                to_decimal(float(ema_long[-2]))
                if ema_long.size > 1 and not np.isnan(ema_long[-2])
                else Decimal("0")
            )

            bullish_cross = prev_short <= prev_long and current_short > current_long
            bearish_cross = prev_short >= prev_long and current_short < current_long

            distance = current_short - current_long
            distance_pct = (
                (distance / current_long) * Decimal("100")
                if current_long > Decimal("0")
                else Decimal("0")
            )

            return {
                "ema_short": current_short,
                "ema_long": current_long,
                "bullish_crossover": bullish_cross,
                "bearish_crossover": bearish_cross,
                "distance": distance,
                "distance_pct": distance_pct,
            }

        except Exception as e:
            error("Erro ao calcular EMA crossover", error=str(e))
            return {
                "ema_short": Decimal("0"),
                "ema_long": Decimal("0"),
                "bullish_crossover": False,
                "bearish_crossover": False,
                "distance": Decimal("0"),
                "distance_pct": Decimal("0"),
            }

    @track_component("indicators", slow_threshold=200)
    def calculate_support_resistance(self, candles: pd.DataFrame) -> dict:
        try:
            recent = candles.tail(100).dropna(subset=["high", "low", "close"])
            if recent.empty:
                raise ValueError("Insufficient candle data for support/resistance")

            high_values = [to_decimal(v) for v in recent["high"].values]
            low_values = [to_decimal(v) for v in recent["low"].values]
            close_values = [to_decimal(v) for v in recent["close"].values]

            current_price = close_values[-1]
            pivot = (high_values[-1] + low_values[-1] + close_values[-1]) / Decimal("3")

            levels = self._find_local_levels(high_values, low_values, current_price)
            pivot_levels = self._calculate_pivot_levels(
                pivot, high_values[-1], low_values[-1]
            )

            resistance = self._determine_resistance(
                levels["valid_resistance"], pivot_levels["r1"], current_price
            )
            support = self._determine_support(
                levels["valid_support"], pivot_levels["s1"], current_price
            )

            support, resistance = self._ensure_valid_levels(
                support, resistance, current_price
            )

            return {
                "support": support,
                "resistance": resistance,
                "pivot": pivot,
                "support_strength": len(levels["valid_support"]),
                "resistance_strength": len(levels["valid_resistance"]),
            }

        except Exception as e:
            error("Erro ao calcular suporte/resistência", error=str(e))
            return self._get_fallback_levels(candles)

    def _find_local_levels(
        self, high_values: list, low_values: list, current_price: Decimal
    ) -> dict:
        resistance_levels = []
        support_levels = []

        for i in range(10, len(high_values) - 10):
            if high_values[i] == max(high_values[i - 10 : i + 10]):
                resistance_levels.append(high_values[i])
            if low_values[i] == min(low_values[i - 10 : i + 10]):
                support_levels.append(low_values[i])

        return {
            "valid_resistance": [r for r in resistance_levels if r > current_price],
            "valid_support": [s for s in support_levels if s < current_price],
        }

    def _calculate_pivot_levels(
        self, pivot: Decimal, high: Decimal, low: Decimal
    ) -> dict:
        return {
            "r1": Decimal("2") * pivot - low,
            "s1": Decimal("2") * pivot - high,
        }

    def _determine_resistance(
        self, valid_resistance: list, r1: Decimal, current_price: Decimal
    ) -> Decimal:
        if valid_resistance:
            return min(valid_resistance)
        return max(r1, current_price * Decimal("1.02"))

    def _determine_support(
        self, valid_support: list, s1: Decimal, current_price: Decimal
    ) -> Decimal:
        if valid_support:
            return max(valid_support)
        return min(s1, current_price * Decimal("0.98"))

    def _ensure_valid_levels(
        self, support: Decimal, resistance: Decimal, current_price: Decimal
    ) -> tuple[Decimal, Decimal]:
        if support >= resistance:
            mid = (support + resistance) / Decimal("2")
            support = mid * Decimal("0.995")
            resistance = mid * Decimal("1.005")

        if current_price <= support:
            support = current_price * Decimal("0.98")
        if current_price >= resistance:
            resistance = current_price * Decimal("1.02")

        if support >= resistance:
            support = current_price * Decimal("0.98")
            resistance = current_price * Decimal("1.02")

        return support, resistance

    def _get_fallback_levels(self, candles: pd.DataFrame) -> dict:
        fallback_price = (
            to_decimal(candles["close"].iloc[-1])
            if len(candles) > 0
            else Decimal("100")
        )
        return {
            "support": fallback_price * Decimal("0.98"),
            "resistance": fallback_price * Decimal("1.02"),
            "pivot": fallback_price,
            "support_strength": 0,
            "resistance_strength": 0,
        }
