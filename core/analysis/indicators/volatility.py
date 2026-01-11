from decimal import Decimal

import numpy as np
import pandas as pd
import talib

from shared.observability.flow_tracker import track_component
from shared.observability.logger import error
from utils.decimal_math import to_decimal


class VolatilityIndicators:
    def __init__(self) -> None:
        self.default_params = {"bb_period": 20, "bb_std": 2, "atr_period": 14}

    @track_component("indicators", slow_threshold=100)
    def calculate_bollinger_bands(
        self,
        close: pd.Series,
        period: int | None = None,
        std_dev: int | None = None,
    ) -> dict[str, Decimal]:
        try:
            period = period or self.default_params["bb_period"]
            std_dev = std_dev or self.default_params["bb_std"]

            close_array = close.to_numpy()
            upper, middle, lower = talib.BBANDS(
                close_array, timeperiod=period, nbdevup=std_dev, nbdevdn=std_dev
            )

            current_price = to_decimal(float(close.iloc[-1]))
            bb_upper = (
                to_decimal(float(upper[-1]))
                if not np.isnan(upper[-1])
                else current_price * Decimal("1.02")
            )
            bb_middle = (
                to_decimal(float(middle[-1]))
                if not np.isnan(middle[-1])
                else current_price
            )
            bb_lower = (
                to_decimal(float(lower[-1]))
                if not np.isnan(lower[-1])
                else current_price * Decimal("0.98")
            )

            bb_width = bb_upper - bb_lower
            if bb_width > 0:
                bb_position = (current_price - bb_lower) / bb_width
            else:
                bb_position = Decimal("0.5")

            bb_width_pct = (
                (bb_width / bb_middle * Decimal("100"))
                if bb_middle > 0
                else Decimal("0")
            )

            return {
                "bb_upper": bb_upper,
                "bb_middle": bb_middle,
                "bb_lower": bb_lower,
                "bb_position": max(Decimal("0"), min(Decimal("1"), bb_position)),
                "bb_width": bb_width_pct,
                "current_price": current_price,
            }

        except Exception as e:
            error("Erro ao calcular Bollinger Bands", error=str(e))
            current = (
                to_decimal(float(close.iloc[-1])) if len(close) > 0 else Decimal("0")
            )
            return {
                "bb_upper": current * Decimal("1.02"),
                "bb_middle": current,
                "bb_lower": current * Decimal("0.98"),
                "bb_position": Decimal("0.5"),
                "bb_width": Decimal("0"),
                "current_price": current,
            }

    @track_component("indicators", slow_threshold=50)
    def calculate_atr(
        self,
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        period: int | None = None,
    ) -> Decimal:
        try:
            period = period or self.default_params["atr_period"]

            high_array = high.to_numpy()
            low_array = low.to_numpy()
            close_array = close.to_numpy()

            atr_array = talib.ATR(high_array, low_array, close_array, timeperiod=period)

            if (
                atr_array is not None
                and len(atr_array) > 0
                and not np.isnan(atr_array[-1])
            ):
                return to_decimal(float(atr_array[-1]))

            return Decimal("0.0")

        except Exception as e:
            error("Erro ao calcular ATR", error=str(e))
            return Decimal("0.0")

    @track_component("indicators", slow_threshold=100)
    def calculate_volatility_percent(
        self, high: pd.Series, low: pd.Series, close: pd.Series, lookback: int = 20
    ) -> Decimal:
        try:
            if len(close) < lookback:
                return Decimal("0.0")

            # Usar ATR direto do TA-Lib
            high_array = high.to_numpy()
            low_array = low.to_numpy()
            close_array = close.to_numpy()

            atr_array = talib.ATR(
                high_array, low_array, close_array, timeperiod=lookback
            )

            if atr_array is not None and not np.isnan(atr_array[-1]):
                atr = to_decimal(float(atr_array[-1]))
                current_price = to_decimal(float(close.iloc[-1]))

                if current_price > 0:
                    volatility_pct = (atr / current_price) * Decimal("100")
                    return volatility_pct

            return Decimal("0.0")

        except Exception as e:
            error("Erro ao calcular volatilidade", error=str(e))
            return Decimal("0.0")

    @track_component("indicators", slow_threshold=150)
    def calculate_keltner_channels(
        self,
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        period: int = 20,
        multiplier: float = 2.0,
    ) -> dict[str, Decimal]:
        try:
            close_array = close.to_numpy()
            high_array = high.to_numpy()
            low_array = low.to_numpy()

            ema_array = talib.EMA(close_array, timeperiod=period)
            atr_array = talib.ATR(high_array, low_array, close_array, timeperiod=period)

            if ema_array is not None and atr_array is not None:
                current_price = to_decimal(float(close.iloc[-1]))
                middle = (
                    to_decimal(float(ema_array[-1]))
                    if not np.isnan(ema_array[-1])
                    else current_price
                )
                atr_value = (
                    to_decimal(float(atr_array[-1]))
                    if not np.isnan(atr_array[-1])
                    else Decimal("0")
                )

                upper = middle + (atr_value * to_decimal(multiplier))
                lower = middle - (atr_value * to_decimal(multiplier))
            else:
                middle = to_decimal(float(close.iloc[-1]))
                upper = middle * Decimal("1.02")
                lower = middle * Decimal("0.98")

            return {"kc_upper": upper, "kc_middle": middle, "kc_lower": lower}

        except Exception as e:
            error("Erro ao calcular Keltner Channels", error=str(e))
            current = (
                to_decimal(float(close.iloc[-1])) if len(close) > 0 else Decimal("0")
            )
            return {
                "kc_upper": current * Decimal("1.02"),
                "kc_middle": current,
                "kc_lower": current * Decimal("0.98"),
            }

    @track_component("indicators", slow_threshold=200)
    def get_all_indicators(
        self, candles: pd.DataFrame, params: dict | None = None
    ) -> dict:
        if params:
            self.default_params.update(params)

        high = candles["high"]
        low = candles["low"]
        close = candles["close"]

        results = {}

        results.update(self.calculate_bollinger_bands(close))
        results["atr"] = self.calculate_atr(high, low, close)
        results["volatility"] = self.calculate_volatility_percent(high, low, close)

        return results
