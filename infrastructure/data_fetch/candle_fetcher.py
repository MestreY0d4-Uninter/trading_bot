import asyncio
import time
from decimal import Decimal

import pandas as pd

from core.validators.exceptions import MarketValidationError
from core.validators.market_validator import MarketValidator
from infrastructure.data_fetch.data_fetcher_base import MarketDataType
from shared.constants import BINANCE_VALID_INTERVALS
from shared.infra.cache import cache
from shared.observability.flow_tracker import track_component
from shared.observability.logger import error, warning
from shared.types.state import state
from utils.decimal_math import to_decimal

ZERO = Decimal("0")


class CandleFetcherMixin:
    def _extract_24hr_stats(
        self, stats_24hr: dict | None, price: Decimal, volume: Decimal
    ) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
        """Extract 24hr stats from ticker response."""
        if not stats_24hr or isinstance(stats_24hr, Exception):
            return price, price, price, ZERO, volume

        high = to_decimal(stats_24hr.get("highPrice", price))
        low = to_decimal(stats_24hr.get("lowPrice", price))
        prev_close = to_decimal(stats_24hr.get("prevClosePrice", price))
        volume_24h = to_decimal(stats_24hr.get("quoteVolume", 0))
        avg_volume = volume_24h / Decimal("24") if volume_24h > 0 else volume
        return high, low, prev_close, volume_24h, avg_volume

    def _calculate_spread(self, orderbook: dict | None, price: Decimal) -> Decimal:
        """Calculate spread percentage from orderbook."""
        if not orderbook or isinstance(orderbook, Exception):
            return ZERO
        try:
            bid = to_decimal(orderbook["bids"][0][0])
            ask = to_decimal(orderbook["asks"][0][0])
            return ((ask - bid) / price) * Decimal("100") if price > 0 else ZERO
        except (IndexError, KeyError, ValueError, TypeError):
            return ZERO

    @track_component("data_fetcher")
    async def update_comprehensive_market_data(self, symbol: str) -> bool:
        """Fetch and update comprehensive market data for a symbol."""
        try:
            results = await asyncio.gather(
                self.fetch_ticker(symbol, max_age_seconds=15),
                self.fetch_24hr_ticker(symbol),
                self.fetch_orderbook(symbol, limit=5),
                return_exceptions=True,
            )
            ticker, stats_24hr, orderbook = results

            if not ticker or isinstance(ticker, Exception):
                return False

            price = to_decimal(ticker.get("price", ticker.get("lastPrice", 0)))
            volume = to_decimal(ticker.get("volume", 0))

            high, low, prev_close, volume_24h, avg_volume = self._extract_24hr_stats(
                stats_24hr, price, volume
            )
            spread_pct = self._calculate_spread(orderbook, price)

            comprehensive_data = {
                "price": price,
                "volume": volume,
                "spread_pct": spread_pct,
                "high": high,
                "low": low,
                "prev_close": prev_close,
                "avg_volume": avg_volume,
                "volume_24h": volume_24h,
                "timestamp": time.time(),
            }

            await self._safe_update_market_data(symbol, comprehensive_data)
            return True

        except Exception as e:
            error("Erro na coleta completa de dados", symbol=symbol, error=str(e))
            return False

    @track_component("data_fetcher", slow_threshold=5)
    async def fetch_candles(
        self, symbol: str, interval: str = "5m", limit: int = 100
    ) -> pd.DataFrame | None:
        validation_error = self._validate_candle_params(symbol, interval, limit)
        if validation_error:
            return None

        safe_cache_key = self._safe_cache_key(f"candles:{symbol}:{interval}:{limit}")
        cached_data = cache.get(safe_cache_key)
        if cached_data is not None:
            return cached_data

        if not self._ensure_client():
            return self._get_fallback_or_none(safe_cache_key, symbol, "candles")

        try:
            klines = await self._fetch_klines_with_retry(symbol, interval, limit)
            validation = self._validate_candles_data(klines, symbol)

            if not validation.is_valid:
                error("CRÍTICO: Dados de candles inválidos", symbol=symbol)
                return self._get_fallback_or_none(safe_cache_key, symbol, "validation")

            df = self._process_klines_to_dataframe(klines, symbol)
            if df is None:
                return None

            await self._cache_and_track_candles(safe_cache_key, df, symbol, validation)
            return df

        except Exception as e:
            error("CRÍTICO: Erro ao buscar candles", symbol=symbol, error=str(e))
            return self._get_fallback_or_none(safe_cache_key, symbol, "error")

    def _validate_candle_params(
        self, symbol: str, interval: str, limit: int
    ) -> str | None:
        if not isinstance(interval, str) or interval not in BINANCE_VALID_INTERVALS:
            error("CRÍTICO: Interval inválido", interval=interval)
            return "invalid_interval"

        if not isinstance(symbol, str) or not symbol.strip():
            error("CRÍTICO: Symbol inválido", symbol=symbol)
            return "invalid_symbol"

        if limit <= 0 or limit > 1000:
            error("CRÍTICO: Limit inválido para candles", limit=limit)
            return "invalid_limit"

        return None

    async def _fetch_klines_with_retry(
        self, symbol: str, interval: str, limit: int
    ) -> list:
        async def fetch_operation():
            async with asyncio.timeout(self.timeouts[MarketDataType.CANDLES]):
                return await self.client.get_klines(symbol, interval, limit)

        return await self._retry_with_backoff(fetch_operation)

    def _get_fallback_or_none(
        self, cache_key: str, symbol: str, context: str
    ) -> pd.DataFrame | None:
        fallback = self._get_fallback_data(cache_key)
        if fallback is not None:
            warning(f"Usando dados de fallback ({context})", symbol=symbol)
            return fallback
        return None

    def _process_klines_to_dataframe(
        self, klines: list, symbol: str
    ) -> pd.DataFrame | None:
        try:
            df = pd.DataFrame(
                klines,
                columns=[
                    "timestamp",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "close_time",
                    "quote_volume",
                    "trades",
                    "taker_buy_base",
                    "taker_buy_quote",
                    "ignore",
                ],
            )

            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            numeric_columns = ["open", "high", "low", "close", "volume"]

            for col in numeric_columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            if df[numeric_columns].isnull().any().any():
                error("CRÍTICO: Dados NaN após conversão", symbol=symbol)
                return None

            df.set_index("timestamp", inplace=True)

            try:
                MarketValidator.validate_candles_or_raise(df, min_count=10)
            except MarketValidationError as e:
                error("CRÍTICO: Candles falhou validação", symbol=symbol, error=str(e))
                return None

            return df

        except Exception as e:
            error("CRÍTICO: Erro ao processar DataFrame", symbol=symbol, error=str(e))
            return None

    async def _cache_and_track_candles(
        self, cache_key: str, df: pd.DataFrame, symbol: str, validation
    ) -> None:
        ttl = await self._get_dynamic_ttl(MarketDataType.CANDLES, symbol)
        cache.set(cache_key, df, ttl)
        self._store_fallback_data(cache_key, df)
        await state.increment_api_calls()
        await state.increment_metric("candles_fetched")

    get_candles = fetch_candles
