from decimal import Decimal

from shared.constants import BINANCE_VALID_INTERVALS
from shared.infra.cache import cache
from shared.observability.flow_tracker import track_component
from shared.observability.logger import debug, error


class SpotEndpointsMixin:
    @track_component("binance_api", slow_threshold=2)
    async def get_ticker(self, symbol: str) -> dict:
        if not symbol or not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("Symbol deve ser uma string válida não vazia")
        client = await self._get_healthy_client()
        return await self._execute_request(
            client.get_ticker, "get_ticker", symbol=symbol.strip().upper()
        )

    async def get_symbol_ticker(self, symbol: str) -> dict:
        if not symbol or not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("Symbol deve ser uma string válida não vazia")
        client = await self._get_healthy_client()
        return await self._execute_request(
            client.get_symbol_ticker, "get_symbol_ticker", symbol=symbol.strip().upper()
        )

    @track_component("binance_api", slow_threshold=2)
    async def get_orderbook(self, symbol: str, limit: int = 5) -> dict:
        if not symbol or not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("Symbol deve ser uma string válida não vazia")
        if not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise ValueError("Limit deve ser um inteiro entre 1 e 1000")
        client = await self._get_healthy_client()
        return await self._execute_request(
            client.get_order_book,
            "get_order_book",
            symbol=symbol.strip().upper(),
            limit=limit,
        )

    async def get_klines(self, symbol: str, interval: str, limit: int = 100) -> list:
        if not symbol or not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("Symbol deve ser uma string válida não vazia")
        if not interval or not isinstance(interval, str) or not interval.strip():
            raise ValueError("Interval deve ser uma string válida não vazia")
        if not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise ValueError("Limit deve ser um inteiro entre 1 e 1000")

        if interval not in BINANCE_VALID_INTERVALS:
            raise ValueError(
                f"Interval inválido. Deve ser um de: {sorted(BINANCE_VALID_INTERVALS)}"
            )

        client = await self._get_healthy_client()
        return await self._execute_request(
            client.get_klines,
            "get_klines",
            symbol=symbol.strip().upper(),
            interval=interval,
            limit=limit,
        )

    async def ping(self) -> dict:
        client = await self._get_healthy_client()
        return await self._execute_request(client.ping, "ping")

    async def get_symbol_info(self, symbol: str) -> dict | None:
        try:
            client = await self._get_healthy_client()
            exchange_info = await self._execute_request(
                client.get_exchange_info, "get_exchange_info"
            )
            if not self._validate_api_response(exchange_info, "exchange_info"):
                return None
            for s in exchange_info.get("symbols", []):
                if s["symbol"] == symbol:
                    return s
            return None
        except Exception as e:
            error("Error getting symbol info", symbol=symbol, error=str(e))
            return None

    async def get_all_tickers(self) -> list[dict]:
        client = await self._get_healthy_client()
        return await self._execute_request(client.get_all_tickers, "get_all_tickers")

    @track_component("binance_api", slow_threshold=3)
    async def get_exchange_info_cached(
        self, force_refresh: bool = False
    ) -> dict | None:
        cache_key = "binance:exchange_info"

        if not force_refresh:
            cached = cache.get(cache_key)
            if cached is not None:
                debug("Exchange info retrieved from cache")
                return cached

        try:
            client = await self._get_healthy_client()
            exchange_info = await self._execute_request(
                client.get_exchange_info, "get_exchange_info"
            )

            if not self._validate_api_response(exchange_info, "exchange_info"):
                error("Invalid exchange_info response from Binance API")
                return None

            cache.set(cache_key, exchange_info, ttl=3600)
            debug("Exchange info cached for 1 hour")
            return exchange_info

        except Exception as e:
            error("Error fetching exchange info", error=str(e))
            return None

    def get_symbol_filters(self, symbol_info: dict) -> dict[str, Decimal] | None:
        if not symbol_info or not isinstance(symbol_info, dict):
            return None

        try:
            filters = symbol_info.get("filters", [])
            result = {}

            for f in filters:
                filter_type = f.get("filterType")

                if filter_type == "PRICE_FILTER":
                    result["tick_size"] = Decimal(str(f.get("tickSize", "0.01")))
                    result["min_price"] = Decimal(str(f.get("minPrice", "0")))
                    result["max_price"] = Decimal(str(f.get("maxPrice", "0")))

                elif filter_type == "LOT_SIZE":
                    result["step_size"] = Decimal(str(f.get("stepSize", "0.00001")))
                    result["min_qty"] = Decimal(str(f.get("minQty", "0")))
                    result["max_qty"] = Decimal(str(f.get("maxQty", "0")))

                elif filter_type == "MIN_NOTIONAL" or filter_type == "NOTIONAL":
                    notional_value = f.get("minNotional") or f.get("notional", "10")
                    result["min_notional"] = Decimal(str(notional_value))

            if not result:
                return None

            return result

        except (KeyError, ValueError, TypeError) as e:
            error(
                "Error extracting symbol filters",
                symbol=symbol_info.get("symbol"),
                error=str(e),
            )
            return None

    async def get_symbol_filters_cached(self, symbol: str) -> dict[str, Decimal] | None:
        exchange_info = await self.get_exchange_info_cached()
        if not exchange_info:
            return None

        for s in exchange_info.get("symbols", []):
            if s.get("symbol") == symbol:
                return self.get_symbol_filters(s)

        error("Symbol not found in exchange info", symbol=symbol)
        return None
