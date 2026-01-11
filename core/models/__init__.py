"""
Pydantic models para validação de dados de trading.

Fase 4: Type-safe data validation com Pydantic.
"""

from core.models.market import MarketDataParams, OrderBookParams, TickerDataParams
from core.models.order import OCOOrderParams, OrderParams
from core.models.position import PositionData, PositionParams
from core.models.signal import (
    SignalParams,
    StopLossTakeProfitParams,
    TradingSignalParams,
)
from core.models.trade import TradeData, TradeResult

__all__ = [
    "OrderParams",
    "OCOOrderParams",
    "PositionParams",
    "PositionData",
    "TradeData",
    "TradeResult",
    "SignalParams",
    "TradingSignalParams",
    "StopLossTakeProfitParams",
    "MarketDataParams",
    "TickerDataParams",
    "OrderBookParams",
]
