from infrastructure.api.api_client import (
    BinanceClient as BinanceClientBase,
)
from infrastructure.api.endpoints.spot_endpoints import SpotEndpointsMixin
from infrastructure.api.endpoints.trading_endpoints import TradingEndpointsMixin


class BinanceClient(BinanceClientBase, SpotEndpointsMixin, TradingEndpointsMixin):
    pass
