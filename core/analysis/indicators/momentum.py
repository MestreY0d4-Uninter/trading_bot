from decimal import Decimal

import numpy as np
import pandas as pd
import talib

from shared.observability.flow_tracker import track_component
from shared.observability.logger import error
from utils.decimal_math import to_decimal


class MomentumIndicators:
    def __init__(self) -> None:
        self.default_params = {
            "rsi_period": 7,
            "macd_fast": 12,
            "macd_slow": 26,
            "macd_signal": 9,
            "stoch_k": 14,
            "stoch_d": 3,
            "stoch_smooth": 3,
            "adx_period": 14,
        }

    @track_component("indicators", slow_threshold=50)
    def calculate_rsi(
        self, close: pd.Series, period: int | None = None
    ) -> Decimal | None:
        try:
            period = period or self.default_params["rsi_period"]
            close_array = close.to_numpy()

            rsi_array = talib.RSI(close_array, timeperiod=period)

            if (
                rsi_array is not None
                and len(rsi_array) > 0
                and not np.isnan(rsi_array[-1])
            ):
                return to_decimal(float(rsi_array[-1]))

            return None

        except Exception as e:
            error("Erro ao calcular RSI", error=str(e))
            return None

    @track_component("indicators", slow_threshold=100)
    def calculate_macd(
        self,
        close: pd.Series,
        fast: int | None = None,
        slow: int | None = None,
        signal: int | None = None,
    ) -> dict[str, Decimal | None]:
        try:
            fast = fast or self.default_params["macd_fast"]
            slow = slow or self.default_params["macd_slow"]
            signal = signal or self.default_params["macd_signal"]

            close_array = close.to_numpy()
            macd, macd_signal, macd_hist = talib.MACD(
                close_array, fastperiod=fast, slowperiod=slow, signalperiod=signal
            )

            macd_value = to_decimal(float(macd[-1])) if not np.isnan(macd[-1]) else None
            signal_value = (
                to_decimal(float(macd_signal[-1]))
                if not np.isnan(macd_signal[-1])
                else None
            )
            hist_value = (
                to_decimal(float(macd_hist[-1]))
                if not np.isnan(macd_hist[-1])
                else None
            )

            return {
                "macd": macd_value,
                "macd_signal": signal_value,
                "macd_histogram": hist_value,
            }

        except Exception as e:
            error("Erro ao calcular MACD", error=str(e))
            return {
                "macd": None,
                "macd_signal": None,
                "macd_histogram": None,
            }

    @track_component("indicators", slow_threshold=100)
    def calculate_stochastic(
        self,
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        k_period: int | None = None,
        d_period: int | None = None,
        smooth: int | None = None,
    ) -> dict[str, Decimal | None]:
        try:
            k_period = k_period or self.default_params["stoch_k"]
            d_period = d_period or self.default_params["stoch_d"]
            smooth = smooth or self.default_params["stoch_smooth"]

            high_array = high.to_numpy()
            low_array = low.to_numpy()
            close_array = close.to_numpy()

            slowk, slowd = talib.STOCH(
                high_array,
                low_array,
                close_array,
                fastk_period=k_period,
                slowk_period=smooth,
                slowk_matype=0,
                slowd_period=d_period,
                slowd_matype=0,
            )

            k_value = to_decimal(float(slowk[-1])) if not np.isnan(slowk[-1]) else None
            d_value = to_decimal(float(slowd[-1])) if not np.isnan(slowd[-1]) else None

            return {"stoch_k": k_value, "stoch_d": d_value}

        except Exception as e:
            error("Erro ao calcular Stochastic", error=str(e))
            return {"stoch_k": None, "stoch_d": None}

    @track_component("indicators", slow_threshold=150)
    def calculate_adx(
        self,
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        period: int | None = None,
    ) -> Decimal:
        try:
            period = period or self.default_params["adx_period"]

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

            return Decimal("0.0")

        except Exception as e:
            error("Erro ao calcular ADX", error=str(e))
            return Decimal("0.0")

    @track_component("indicators", slow_threshold=50)
    def calculate_momentum(self, close: pd.Series, periods: int = 10) -> Decimal:
        try:
            if len(close) < periods + 1:
                return Decimal("0.0")

            current_price = to_decimal(float(close.iloc[-1]))
            past_price = to_decimal(float(close.iloc[-periods]))

            if past_price > 0:
                roc = (current_price - past_price) / past_price
                return roc

            return Decimal("0.0")

        except Exception as e:
            error("Erro ao calcular momentum", error=str(e))
            return Decimal("0.0")

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

        results["rsi"] = self.calculate_rsi(close)
        results.update(self.calculate_macd(close))
        results.update(self.calculate_stochastic(high, low, close))
        results["adx"] = self.calculate_adx(high, low, close)
        results["momentum"] = self.calculate_momentum(close)

        return results
