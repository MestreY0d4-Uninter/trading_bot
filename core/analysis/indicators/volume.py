from decimal import Decimal

import numpy as np
import pandas as pd
import talib

from shared.observability.flow_tracker import track_component
from shared.observability.logger import error
from utils.decimal_math import to_decimal


class VolumeIndicators:
    def __init__(self) -> None:
        self.volume_threshold_multiplier = Decimal("1.5")

    @track_component("indicators", slow_threshold=100)
    def calculate_volume_indicators(self, candles: pd.DataFrame) -> dict:
        try:
            close_array = candles["close"].to_numpy()
            volume_array = candles["volume"].to_numpy()

            volume_ma_array = talib.SMA(volume_array, timeperiod=20)
            current_volume = to_decimal(float(volume_array[-1]))
            avg_volume = (
                to_decimal(float(volume_ma_array[-1]))
                if not np.isnan(volume_ma_array[-1])
                else current_volume
            )

            volume_ratio = (
                (current_volume / avg_volume) if avg_volume > 0 else Decimal("1.0")
            )

            obv_array = talib.OBV(close_array, volume_array)
            obv_value = (
                to_decimal(float(obv_array[-1]))
                if not np.isnan(obv_array[-1])
                else Decimal("0.0")
            )

            return {
                "volume_ratio": volume_ratio,
                "volume_24h": (
                    to_decimal(float(volume_array[-288:].sum()))
                    if len(volume_array) >= 288
                    else to_decimal(float(volume_array.sum()))
                ),
                "obv": obv_value,
                "avg_volume": avg_volume,
                "current_volume": current_volume,
            }

        except Exception as e:
            error("Erro ao calcular indicadores de volume", error=str(e))
            return {
                "volume_ratio": Decimal("1.0"),
                "volume_24h": Decimal("0"),
                "obv": Decimal("0"),
                "avg_volume": Decimal("0"),
                "current_volume": Decimal("0"),
            }

    @track_component("indicators", slow_threshold=150)
    def calculate_volume_profile(self, candles: pd.DataFrame) -> dict:
        try:
            if "volume" not in candles or len(candles) < 20:
                return {
                    "volume_trend": "NEUTRAL",
                    "volume_ratio": Decimal("1.0"),
                    "volume_24h_usd": Decimal("0"),
                    "high_volume": False,
                }

            volume = candles["volume"]
            close = candles["close"]

            volume_array = volume.to_numpy()
            vol_ma_short = talib.SMA(volume_array, timeperiod=10)
            vol_ma_long = talib.SMA(volume_array, timeperiod=30)

            current_vol_short = (
                to_decimal(float(vol_ma_short[-1]))
                if not np.isnan(vol_ma_short[-1])
                else Decimal("0")
            )
            current_vol_long = (
                to_decimal(float(vol_ma_long[-1]))
                if not np.isnan(vol_ma_long[-1])
                else Decimal("0")
            )

            if current_vol_short > current_vol_long * Decimal("1.1"):
                volume_trend = "INCREASING"
            elif current_vol_short < current_vol_long * Decimal("0.9"):
                volume_trend = "DECREASING"
            else:
                volume_trend = "NEUTRAL"

            current_volume = to_decimal(float(volume.iloc[-1]))
            avg_volume = current_vol_long if current_vol_long > 0 else current_volume
            volume_ratio = (
                (current_volume / avg_volume) if avg_volume > 0 else Decimal("1.0")
            )

            if len(candles) >= 288:
                vol_24h = to_decimal(float(volume.tail(288).sum()))
                price_avg = to_decimal(float(close.tail(288).mean()))
                volume_24h_usd = vol_24h * price_avg
            else:
                vol_24h = to_decimal(float(volume.sum()))
                price_avg = to_decimal(float(close.mean()))
                volume_24h_usd = vol_24h * price_avg

            high_volume = volume_ratio > to_decimal(self.volume_threshold_multiplier)

            return {
                "volume_trend": volume_trend,
                "volume_ratio": volume_ratio,
                "volume_24h_usd": volume_24h_usd,
                "high_volume": high_volume,
            }

        except Exception as e:
            error("Erro ao calcular perfil de volume", error=str(e))
            return {
                "volume_trend": "NEUTRAL",
                "volume_ratio": Decimal("1.0"),
                "volume_24h_usd": Decimal("0"),
                "high_volume": False,
            }

    @track_component("indicators", slow_threshold=100)
    def calculate_vwap(self, candles: pd.DataFrame, period: int = 20) -> Decimal:
        try:
            if len(candles) < period:
                return to_decimal(float(candles["close"].iloc[-1]))

            recent = candles.tail(period)
            typical_price = (recent["high"] + recent["low"] + recent["close"]) / 3
            volume = recent["volume"]

            if volume.sum() > 0:
                vwap_value = (typical_price * volume).sum() / volume.sum()
                return to_decimal(float(vwap_value))

            return to_decimal(float(candles["close"].iloc[-1]))

        except Exception as e:
            error("Erro ao calcular VWAP", error=str(e))
            return (
                to_decimal(float(candles["close"].iloc[-1]))
                if len(candles) > 0
                else Decimal("0")
            )

    @track_component("indicators", slow_threshold=50)
    def calculate_money_flow_index(
        self, candles: pd.DataFrame, period: int = 14
    ) -> Decimal:
        try:
            if len(candles) < period + 1:
                return Decimal("50.0")

            high_array = candles["high"].to_numpy()
            low_array = candles["low"].to_numpy()
            close_array = candles["close"].to_numpy()
            volume_array = candles["volume"].to_numpy()

            mfi_array = talib.MFI(
                high_array, low_array, close_array, volume_array, timeperiod=period
            )

            if (
                mfi_array is not None
                and len(mfi_array) > 0
                and not np.isnan(mfi_array[-1])
            ):
                return to_decimal(float(mfi_array[-1]))

            return Decimal("50.0")

        except Exception as e:
            error("Erro ao calcular MFI", error=str(e))
            return Decimal("50.0")

    @track_component("indicators", slow_threshold=200)
    def get_all_indicators(self, candles: pd.DataFrame) -> dict:
        results = {}

        results.update(self.calculate_volume_indicators(candles))
        results["vwap"] = self.calculate_vwap(candles)
        results["mfi"] = self.calculate_money_flow_index(candles)

        profile = self.calculate_volume_profile(candles)
        results["volume_24h_usd"] = profile["volume_24h_usd"]
        results["high_volume"] = profile["high_volume"]

        return results
