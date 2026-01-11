import asyncio
import time
from decimal import Decimal

from core.validators.exceptions import MarketValidationError
from core.validators.market_validator import MarketValidator
from infrastructure.data_fetch.data_fetcher_base import MarketDataType
from shared.infra.cache import cache
from shared.observability.flow_tracker import track_component
from shared.observability.logger import debug, error, warning
from shared.types.state import state
from utils.decimal_math import to_decimal
from utils.validation_utils import is_numeric_valid


class TickerFetcherMixin:
    @track_component("data_fetcher", slow_threshold=2)
    async def fetch_ticker(self, symbol: str, max_age_seconds: int = 30) -> dict | None:
        if not self._validate_symbol_input(symbol):
            return None

        safe_cache_key = self._safe_cache_key(f"ticker:{symbol}")

        cached = self._get_valid_cached_ticker(safe_cache_key, max_age_seconds)
        if cached is not None:
            return cached

        if not self._ensure_client():
            return self._get_fallback_or_none(safe_cache_key, symbol)

        try:
            ticker, validation = await self._fetch_and_validate_ticker(symbol)

            if not ticker or not validation:
                return self._get_fallback_ticker_or_none(
                    safe_cache_key, symbol, max_age_seconds
                )

            if not self._validate_ticker_centralized(ticker, symbol):
                return None

            return await self._process_and_cache_ticker(
                ticker, validation, symbol, safe_cache_key
            )

        except Exception as e:
            error("CRÍTICO: Erro ao buscar ticker", symbol=symbol, error=str(e))
            fallback_data = self._get_fallback_data(safe_cache_key)
            if fallback_data is not None:
                warning("Usando dados de fallback após erro", symbol=symbol)
                return fallback_data
            return None

    def _validate_symbol_input(self, symbol: str) -> bool:
        if not isinstance(symbol, str) or not symbol.strip():
            error("CRÍTICO: Symbol inválido", symbol=symbol)
            return False
        return True

    def _get_valid_cached_ticker(
        self, cache_key: str, max_age_seconds: int
    ) -> dict | None:
        cached_data = cache.get(cache_key)
        if cached_data is None:
            return None

        cache_timestamp = cached_data.get("_fetch_timestamp", 0)
        cache_age = time.time() - cache_timestamp

        if cache_age <= max_age_seconds:
            debug("Cache hit válido", key=cache_key, age_seconds=cache_age)
            return cached_data

        debug("Cache expirado", key=cache_key, age_seconds=cache_age)
        cache.invalidate(cache_key)
        return None

    def _get_fallback_or_none(self, cache_key: str, symbol: str) -> dict | None:
        fallback_data = self._get_fallback_data(cache_key)
        if fallback_data is not None:
            warning("Usando dados de fallback para ticker", symbol=symbol)
            return fallback_data
        return None

    async def _fetch_and_validate_ticker(
        self, symbol: str
    ) -> tuple[dict | None, object | None]:
        ticker_timestamp = time.time()

        async def fetch_operation():
            async with asyncio.timeout(self.timeouts[MarketDataType.TICKER]):
                return await self.client.get_ticker(symbol)

        for retry_attempt in range(max(1, self._max_retries)):
            ticker = await self._retry_with_backoff(fetch_operation)

            if not ticker:
                if retry_attempt == 0:
                    warning("Ticker vazio retornado da API", symbol=symbol)
                continue

            validation = self._validate_ticker_data(ticker, symbol)

            if not validation.is_valid:
                if retry_attempt == 0:
                    debug("Dados de ticker inválidos", symbol=symbol)
                continue

            if validation.data_quality_score >= self._quality_threshold:
                ticker["_fetch_timestamp"] = ticker_timestamp
                ticker["_retry_attempt"] = retry_attempt
                return ticker, validation

            if retry_attempt < self._max_retries - 1:
                await asyncio.sleep(0.1 * (retry_attempt + 1))

        return None, None

    def _get_fallback_ticker_or_none(
        self, cache_key: str, symbol: str, max_age_seconds: int
    ) -> dict | None:
        error("CRÍTICO: Não foi possível obter ticker válido", symbol=symbol)
        fallback_data = self._get_fallback_data(
            cache_key, max_age_seconds=max_age_seconds
        )

        if fallback_data is None:
            return None

        fallback_age = time.time() - fallback_data.get("_fetch_timestamp", 0)

        if fallback_age <= max_age_seconds:
            warning("Usando dados de fallback recentes", symbol=symbol)
            return fallback_data

        error("Fallback muito antigo para trading", symbol=symbol, age=fallback_age)
        return None

    def _validate_ticker_centralized(self, ticker: dict, symbol: str) -> bool:
        try:
            MarketValidator.validate_ticker_data_or_raise(ticker)
            return True
        except MarketValidationError as e:
            error("CRÍTICO: Ticker falhou validação", symbol=symbol, error=str(e))
            return False

    async def _process_and_cache_ticker(
        self, ticker: dict, validation: object, symbol: str, cache_key: str
    ) -> dict | None:
        if (
            validation.warnings
            and validation.data_quality_score < self._quality_threshold
        ):
            if self._warning_throttler.should_log_warning(symbol, validation.warnings):
                warning(
                    "Avisos na validação de ticker",
                    symbol=symbol,
                    warnings=validation.warnings,
                )

        try:
            price = to_decimal(ticker.get("price", ticker.get("lastPrice", 0)))
            volume = to_decimal(ticker.get("volume", ticker.get("quoteVolume", 0)))
            fetch_timestamp = ticker.get("_fetch_timestamp", time.time())

            market_update = {
                "price": price,
                "volume": volume,
                "ticker": ticker,
                "last_updated": time.time(),
                "fetch_timestamp": fetch_timestamp,
                "data_age_ms": (time.time() - fetch_timestamp) * 1000,
            }

            await self._safe_update_market_data(symbol, market_update)
            await self._save_market_data_to_db(symbol, price, volume)

            ticker["_fetch_timestamp"] = fetch_timestamp

            ttl = await self._get_dynamic_ttl(MarketDataType.TICKER, symbol)
            cache.set(cache_key, ticker, ttl)
            self._store_fallback_data(cache_key, ticker)

            await state.increment_api_calls()
            await state.increment_metric("tickers_fetched")

            debug(
                "Ticker fetched successfully",
                symbol=symbol,
                price=price,
                quality_score=validation.data_quality_score,
            )
            return ticker

        except (ValueError, TypeError) as conversion_error:
            error(
                "CRÍTICO: Erro na conversão de dados do ticker",
                symbol=symbol,
                error=str(conversion_error),
            )
            return None

    async def _fetch_orderbook_with_progressive_timeout(
        self, symbol: str, limit: int
    ) -> dict | None:
        if not self.client:
            return None

        timeout_sequence = self.config.get("data", {}).get(
            "orderbook_timeout_sequence", [2, 3, 5]
        )
        max_retries = len(timeout_sequence)
        last_exception: Exception | None = None

        for attempt in range(max_retries):
            timeout = timeout_sequence[attempt]
            try:
                async with asyncio.timeout(timeout):
                    orderbook = await self.client.get_orderbook(symbol, limit)
                if attempt > 0:
                    debug(
                        f"Orderbook obtido na tentativa {attempt + 1}",
                        symbol=symbol,
                        timeout=timeout,
                        attempt=attempt + 1,
                    )
                return orderbook

            except TimeoutError as e:
                last_exception = e
                if attempt < max_retries - 1:
                    debug(
                        f"Timeout na tentativa {attempt + 1}/{max_retries}",
                        symbol=symbol,
                        timeout=timeout,
                        next_timeout=timeout_sequence[attempt + 1],
                    )
                    await asyncio.sleep(0.3)
                else:
                    warning(
                        f"Orderbook timeout após {max_retries} tentativas",
                        symbol=symbol,
                        timeouts=timeout_sequence,
                    )

        if last_exception:
            raise last_exception
        return None

    @track_component("data_fetcher", slow_threshold=2)
    async def fetch_orderbook(self, symbol: str, limit: int = 5) -> dict | None:
        if not self._validate_orderbook_params(symbol, limit):
            return None

        safe_cache_key = self._safe_cache_key(f"orderbook:{symbol}:{limit}")
        cached_data = cache.get(safe_cache_key)
        if cached_data is not None:
            return cached_data

        if not self._ensure_client():
            return self._get_orderbook_fallback(safe_cache_key, symbol)

        try:
            orderbook = await self._fetch_orderbook_with_progressive_timeout(
                symbol, limit
            )
            if not orderbook:
                warning("Orderbook vazio", symbol=symbol)
                return None

            validation = self._validate_orderbook_data(orderbook, symbol)
            if not validation.is_valid:
                error("CRÍTICO: Orderbook inválido", symbol=symbol)
                return self._get_orderbook_fallback(safe_cache_key, symbol)

            try:
                MarketValidator.validate_order_book_or_raise(orderbook)
            except MarketValidationError as e:
                error(
                    "CRÍTICO: Orderbook falhou validação", symbol=symbol, error=str(e)
                )
                return None

            await self._process_and_cache_orderbook(safe_cache_key, symbol, orderbook)
            return orderbook

        except (ValueError, TypeError, IndexError) as e:
            error("CRÍTICO: Erro processando orderbook", symbol=symbol, error=str(e))
            return None
        except Exception as e:
            error("CRÍTICO: Erro ao buscar orderbook", symbol=symbol, error=str(e))
            return self._get_orderbook_fallback(safe_cache_key, symbol)

    def _validate_orderbook_params(self, symbol: str, limit: int) -> bool:
        if not isinstance(symbol, str) or not symbol.strip():
            error("CRÍTICO: Symbol inválido", symbol=symbol)
            return False

        if limit <= 0 or limit > 1000:
            error("CRÍTICO: Limit inválido para orderbook", limit=limit)
            return False

        return True

    def _get_orderbook_fallback(self, cache_key: str, symbol: str) -> dict | None:
        fallback = self._get_fallback_data(cache_key, max_age_seconds=30)
        if fallback:
            warning("Usando fallback orderbook", symbol=symbol)
            return fallback
        return None

    async def _process_and_cache_orderbook(
        self, cache_key: str, symbol: str, orderbook: dict
    ) -> None:
        bids = orderbook["bids"]
        asks = orderbook["asks"]

        bid = to_decimal(bids[0][0])
        ask = to_decimal(asks[0][0])
        spread_pct = ((ask - bid) / bid * Decimal("100")) if bid > 0 else Decimal("0")

        market_update = {
            "spread_pct": spread_pct,
            "bid": bid,
            "ask": ask,
            "bid_size": to_decimal(bids[0][1]),
            "ask_size": to_decimal(asks[0][1]),
            "last_updated": time.time(),
        }

        await self._safe_update_market_data(symbol, market_update)
        ttl = await self._get_dynamic_ttl(MarketDataType.ORDERBOOK, symbol)
        cache.set(cache_key, orderbook, ttl)
        self._store_fallback_data(cache_key, orderbook)
        await state.increment_api_calls()
        await state.increment_metric("orderbooks_fetched")

    @track_component("data_fetcher", slow_threshold=10)
    async def fetch_all_tickers(self) -> dict[str, dict]:
        cache_key = "all_tickers"
        cached_data = cache.get(cache_key)
        if cached_data is not None:
            return cached_data

        if not self._ensure_client():
            return self._get_all_tickers_fallback(cache_key)

        try:
            all_tickers = await self._fetch_all_tickers_with_retry()

            if not isinstance(all_tickers, list):
                error("CRÍTICO: Formato inválido all_tickers", type=type(all_tickers))
                return {}

            result, invalid_count = self._process_all_tickers(all_tickers)

            if invalid_count > 0:
                warning(
                    "Tickers inválidos", invalid=invalid_count, total=len(all_tickers)
                )

            if not result:
                error("CRÍTICO: Nenhum ticker válido")
                return {}

            await self._cache_all_tickers(cache_key, result)
            return result

        except Exception as e:
            error("CRÍTICO: Erro ao buscar all tickers", error=str(e))
            return self._get_all_tickers_fallback(cache_key)

    async def _fetch_all_tickers_with_retry(self) -> list:
        async def fetch_operation():
            async with asyncio.timeout(self.timeouts[MarketDataType.ALL_TICKERS]):
                return await self.client.get_all_tickers()

        return await self._retry_with_backoff(fetch_operation)

    def _get_all_tickers_fallback(self, cache_key: str) -> dict:
        fallback = self._get_fallback_data(cache_key)
        if fallback:
            warning("Usando fallback all tickers")
            return fallback
        return {}

    def _process_all_tickers(self, all_tickers: list) -> tuple[dict, int]:
        result = {}
        invalid_count = 0

        for ticker in all_tickers:
            parsed = self._parse_single_ticker(ticker)
            if parsed:
                result[parsed["symbol"]] = parsed["data"]
            else:
                invalid_count += 1

        return result, invalid_count

    def _parse_single_ticker(self, ticker: dict) -> dict | None:
        try:
            if not isinstance(ticker, dict):
                return None

            symbol = ticker.get("symbol")
            price = ticker.get("price")

            if not symbol or not price:
                return None

            price_decimal = to_decimal(price)
            if price_decimal <= 0 or not is_numeric_valid(price_decimal):
                return None

            return {
                "symbol": symbol,
                "data": {"price": price_decimal, "ticker": ticker},
            }
        except (ValueError, TypeError):
            return None

    async def _cache_all_tickers(self, cache_key: str, result: dict) -> None:
        ttl = await self._get_dynamic_ttl(MarketDataType.ALL_TICKERS)
        cache.set(cache_key, result, ttl)
        self._store_fallback_data(cache_key, result)
        await state.increment_api_calls()
        await state.increment_metric("all_tickers_fetched")

    @track_component("data_fetcher", slow_threshold=3)
    async def fetch_24hr_ticker(self, symbol: str) -> dict | None:
        if not isinstance(symbol, str) or not symbol.strip():
            error("CRÍTICO: Symbol inválido", symbol=symbol)
            return None

        safe_cache_key = self._safe_cache_key(f"ticker24hr:{symbol}")
        cached_data = cache.get(safe_cache_key)
        if cached_data is not None:
            return cached_data

        if not self._ensure_client():
            return self._get_ticker_fallback(safe_cache_key, symbol)

        try:
            ticker = await self._fetch_ticker_24hr_with_retry(symbol)
            if not self._validate_ticker_24hr(ticker, symbol):
                return None

            market_update = self._build_24hr_market_update(ticker)
            await self._save_ticker_24hr(safe_cache_key, symbol, ticker, market_update)
            return ticker

        except (ValueError, TypeError) as e:
            error("CRÍTICO: Erro na conversão ticker 24hr", symbol=symbol, error=str(e))
            return None
        except Exception as e:
            error("CRÍTICO: Erro ao buscar ticker 24hr", symbol=symbol, error=str(e))
            return self._get_ticker_fallback(safe_cache_key, symbol)

    async def _fetch_ticker_24hr_with_retry(self, symbol: str) -> dict | None:
        async def fetch_operation():
            async with asyncio.timeout(self.timeouts[MarketDataType.TICKER_24HR]):
                return await self.client.get_ticker(symbol)

        return await self._retry_with_backoff(fetch_operation)

    def _validate_ticker_24hr(self, ticker: dict | None, symbol: str) -> bool:
        if not ticker:
            warning("Ticker 24hr vazio", symbol=symbol)
            return False

        if not isinstance(ticker, dict):
            error("CRÍTICO: Ticker 24hr não é dict", symbol=symbol)
            return False

        return True

    def _get_ticker_fallback(self, cache_key: str, symbol: str) -> dict | None:
        fallback = self._get_fallback_data(cache_key)
        if fallback:
            warning("Usando fallback ticker 24hr", symbol=symbol)
            return fallback
        return None

    def _build_24hr_market_update(self, ticker: dict) -> dict:
        volume_24h_usd = to_decimal(ticker.get("quoteVolume", 0))
        price_change_pct = to_decimal(ticker.get("priceChangePercent", 0))

        if volume_24h_usd < 0 or not is_numeric_valid(volume_24h_usd):
            volume_24h_usd = Decimal("0")

        if not is_numeric_valid(price_change_pct):
            price_change_pct = Decimal("0")

        avg_volume = (
            volume_24h_usd / Decimal("24") if volume_24h_usd > 0 else Decimal("0")
        )

        return {
            "volume_24h_usd": volume_24h_usd,
            "price_change_24h_pct": price_change_pct,
            "high": to_decimal(ticker.get("highPrice", 0)),
            "low": to_decimal(ticker.get("lowPrice", 0)),
            "prev_close": to_decimal(ticker.get("prevClosePrice", 0)),
            "avg_volume": avg_volume,
            "volume_24h": volume_24h_usd,
            "last_updated": time.time(),
        }

    async def _save_ticker_24hr(
        self, cache_key: str, symbol: str, ticker: dict, market_update: dict
    ) -> None:
        await self._safe_update_market_data(symbol, market_update)
        ttl = await self._get_dynamic_ttl(MarketDataType.TICKER_24HR, symbol)
        cache.set(cache_key, ticker, ttl)
        self._store_fallback_data(cache_key, ticker)
        await state.increment_api_calls()
        await state.increment_metric("ticker_24hr_fetched")

    @track_component("data_fetcher", slow_threshold=1)
    async def get_trading_price(
        self, symbol: str, max_age_seconds: int = 15
    ) -> dict | None:
        ticker = await self.fetch_ticker(symbol, max_age_seconds=max_age_seconds)
        if not ticker:
            return None

        fetch_timestamp = ticker.get("_fetch_timestamp", 0)
        current_time = time.time()
        data_age = current_time - fetch_timestamp

        if data_age > max_age_seconds:
            error(
                "CRÍTICO: Dados muito antigos para trading",
                symbol=symbol,
                data_age_seconds=data_age,
                max_age=max_age_seconds,
            )
            return None

        try:
            price = to_decimal(ticker.get("price", ticker.get("lastPrice", 0)))
            if price <= 0 or not is_numeric_valid(price):
                error(
                    "CRÍTICO: Preço inválido para trading", symbol=symbol, price=price
                )
                return None

            return {
                "symbol": symbol,
                "price": price,
                "fetch_timestamp": fetch_timestamp,
                "data_age_ms": data_age * 1000,
                "is_fresh": data_age <= max_age_seconds / 2,
                "ticker": ticker,
            }

        except (ValueError, TypeError) as e:
            error(
                "CRÍTICO: Erro ao processar preço para trading",
                symbol=symbol,
                error=str(e),
            )
            return None

    def validate_price_deviation(
        self,
        symbol: str,
        decision_price: float,
        execution_price: float,
        max_deviation_pct: float = 5.0,
    ) -> bool:
        if decision_price <= 0 or execution_price <= 0:
            error(
                "CRÍTICO: Preços inválidos para validação",
                symbol=symbol,
                decision_price=decision_price,
                execution_price=execution_price,
            )
            return False

        deviation_pct = (
            abs(execution_price - decision_price) / decision_price * Decimal("100")
        )

        if deviation_pct > max_deviation_pct:
            error(
                "CRÍTICO: DESVIO DE PREÇO DETECTADO",
                symbol=symbol,
                decision_price=decision_price,
                execution_price=execution_price,
                deviation_pct=deviation_pct,
                max_allowed_pct=max_deviation_pct,
            )
            return False

        if deviation_pct > max_deviation_pct / 2:
            warning(
                "ALERTA: Desvio de preço significativo",
                symbol=symbol,
                decision_price=decision_price,
                execution_price=execution_price,
                deviation_pct=deviation_pct,
            )

        return True

    async def get_fresh_price_with_validation(
        self,
        symbol: str,
        decision_price: float | None = None,
        max_age_seconds: int = 15,
        max_deviation_pct: float = 5.0,
    ) -> dict | None:
        price_data = await self.get_trading_price(symbol, max_age_seconds)
        if not price_data:
            return None

        execution_price = price_data["price"]

        if decision_price is not None:
            if not self.validate_price_deviation(
                symbol, decision_price, execution_price, max_deviation_pct
            ):
                error(
                    "CRÍTICO: Validação de desvio falhou - dados rejeitados",
                    symbol=symbol,
                    decision_price=decision_price,
                    execution_price=execution_price,
                )
                return None

            price_data["price_deviation_pct"] = (
                abs(execution_price - decision_price) / decision_price * Decimal("100")
            )
            price_data["decision_price"] = decision_price

        return price_data
