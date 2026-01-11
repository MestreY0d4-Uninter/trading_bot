import asyncio
import hashlib
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from shared.infra.cache import cache
from shared.observability.logger import debug, error, production, warning
from shared.types.state import state
from utils.validation_utils import is_numeric_valid


class MarketDataType(Enum):
    CANDLES = "candles"
    TICKER = "ticker"
    ORDERBOOK = "orderbook"
    TICKER_24HR = "ticker_24hr"
    ALL_TICKERS = "all_tickers"


@dataclass
class ValidationResult:
    is_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    data_quality_score: float = 1.0
    validation_timestamp: float = field(default_factory=time.time)
    data_size: int = 0
    processing_time_ms: float = 0.0

    def add_error(self, message: str):
        self.errors.append(message)
        self.is_valid = False

    def add_warning(self, message: str):
        self.warnings.append(message)
        self.data_quality_score *= 0.9

    def has_issues(self) -> bool:
        return len(self.errors) > 0 or len(self.warnings) > 0


@dataclass
class CacheConfig:
    base_ttl: int
    min_ttl: int = 5
    max_ttl: int = 3600
    volatility_multiplier: float = 1.0


class TickerValidationWarningThrottler:
    def __init__(
        self, window_seconds: int = 60, max_warnings_per_symbol: int = 1
    ) -> None:
        self.window_seconds = window_seconds
        self.max_warnings_per_symbol = max_warnings_per_symbol
        self._warnings_cache: dict[str, list[float]] = {}
        self._lock = threading.RLock()
        self._last_cleanup = time.time()
        self._cleanup_interval = 120

    def should_log_warning(self, symbol: str, warnings: list[str]) -> bool:
        with self._lock:
            now = time.time()

            if now - self._last_cleanup > self._cleanup_interval:
                self._cleanup_expired(now)
                self._last_cleanup = now

            warning_key = f"{symbol}:{hash(tuple(sorted(warnings)))}"

            if warning_key not in self._warnings_cache:
                self._warnings_cache[warning_key] = []

            warning_times = self._warnings_cache[warning_key]
            recent_warnings = [
                t for t in warning_times if now - t < self.window_seconds
            ]

            if len(recent_warnings) >= self.max_warnings_per_symbol:
                return False

            recent_warnings.append(now)
            self._warnings_cache[warning_key] = recent_warnings
            return True

    def _cleanup_expired(self, now: float):
        expired_keys = []
        for key, times in self._warnings_cache.items():
            recent_times = [t for t in times if now - t < self.window_seconds * 2]
            if not recent_times:
                expired_keys.append(key)
            else:
                self._warnings_cache[key] = recent_times

        for key in expired_keys:
            del self._warnings_cache[key]

    def get_stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active_warning_keys": len(self._warnings_cache),
                "window_seconds": self.window_seconds,
                "max_warnings_per_symbol": self.max_warnings_per_symbol,
                "last_cleanup": self._last_cleanup,
            }


class DataFetcherBase:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.client = None
        self._sync_lock = threading.Lock()
        self._async_lock = asyncio.Lock()

        cache_config = config.get("cache", {})
        self.cache_ttl = cache_config.get("default_ttl", 300)

        self.cache_configs = {
            MarketDataType.TICKER: CacheConfig(
                base_ttl=cache_config.get("ticker_ttl", 10), min_ttl=2, max_ttl=60
            ),
            MarketDataType.CANDLES: CacheConfig(
                base_ttl=cache_config.get("candles_ttl", 60), min_ttl=30, max_ttl=300
            ),
            MarketDataType.ORDERBOOK: CacheConfig(
                base_ttl=cache_config.get("orderbook_ttl", 5), min_ttl=1, max_ttl=30
            ),
            MarketDataType.TICKER_24HR: CacheConfig(
                base_ttl=cache_config.get("ticker_24hr_ttl", 120),
                min_ttl=60,
                max_ttl=600,
            ),
            MarketDataType.ALL_TICKERS: CacheConfig(
                base_ttl=cache_config.get("all_tickers_ttl", 10), min_ttl=5, max_ttl=60
            ),
        }

        timeout_config = config.get("data", {})
        self.timeouts = {
            MarketDataType.CANDLES: timeout_config.get("candles_timeout", 15),
            MarketDataType.TICKER: timeout_config.get("ticker_timeout", 8),
            MarketDataType.ORDERBOOK: timeout_config.get("orderbook_timeout", 5),
            MarketDataType.TICKER_24HR: timeout_config.get("ticker_24hr_timeout", 10),
            MarketDataType.ALL_TICKERS: timeout_config.get("all_tickers_timeout", 20),
        }

        self._api_timeout = timeout_config.get("api_timeout", 10)
        self._max_retries = cache_config.get("retry_attempts", 3)
        self._base_delay = cache_config.get("retry_backoff", 2.0)
        self._quality_threshold = cache_config.get("quality_threshold", 0.8)

        self._warning_throttler = TickerValidationWarningThrottler(
            window_seconds=60, max_warnings_per_symbol=1
        )

        self.monitoring_config = config.get("monitoring", {})
        self.save_market_data = self.monitoring_config.get("save_market_data", True)
        self.db_handler = None

        self._max_delay = config.get("data", {}).get("retry_max_delay", 30.0)

        self._fallback_enabled = config.get("data", {}).get("fallback_enabled", True)
        self._fallback_max_age = config.get("data", {}).get(
            "fallback_max_age_seconds", 300
        )

        self._validation_counters: dict[str, int] = {
            "total_validations": 0,
            "validation_failures": 0,
            "validation_warnings": 0,
        }
        self._validation_lists: dict[str, list[float]] = {
            "data_quality_scores": [],
            "validation_times_ms": [],
        }

        production(
            "DataFetcher initialized",
            cache_configs={k.value: v.base_ttl for k, v in self.cache_configs.items()},
            timeouts={k.value: v for k, v in self.timeouts.items()},
            max_retries=self._max_retries,
            fallback_enabled=self._fallback_enabled,
            save_market_data=self.save_market_data,
        )

    def set_db_handler(self, db_handler):
        self.db_handler = db_handler
        production(
            "Database handler configurado no DataFetcher",
            save_market_data=self.save_market_data,
        )

    async def _save_market_data_to_db(
        self, symbol: str, price: float, volume: float | None = None
    ):
        if not self.save_market_data or not self.db_handler:
            return

        try:
            success = await self.db_handler.save_market_data(symbol, price, volume)
            if not success:
                warning("Falha ao salvar market data", symbol=symbol)
        except Exception as e:
            error("Erro ao salvar market data no banco", symbol=symbol, error=str(e))

    def set_client(self, client):
        self.client = client
        production(
            "DataFetcher client configurado", client_available=client is not None
        )

    def _ensure_client(self):
        if not self.client:
            try:
                from shared.types.state import state

                if hasattr(state, "client") and state.client:
                    self.client = state.client
                    debug("Client obtido do state global como fallback")
                    return True
            except Exception as e:
                debug("Erro ao obter client do state", error=str(e))

            error("CRÍTICO: Client não configurado no DataFetcher")
            return False
        return True

    async def _get_dynamic_ttl(
        self, data_type: MarketDataType, symbol: str | None = None
    ) -> int:
        try:
            config = self.cache_configs[data_type]
            base_ttl = config.base_ttl

            if symbol:
                market_data = await state.get_market_data(symbol)
                if market_data and isinstance(market_data, dict):
                    price_change_24h = abs(market_data.get("price_change_24h_pct", 0))
                    volume_24h = market_data.get("volume_24h_usd", 0)

                    volatility_factor = 1.0
                    if price_change_24h > 10:
                        volatility_factor = 0.5
                    elif price_change_24h > 5:
                        volatility_factor = 0.7
                    elif price_change_24h < 1:
                        volatility_factor = 1.5

                    volume_factor = 1.0
                    if volume_24h > 1000000:
                        volume_factor = 0.8
                    elif volume_24h < 100000:
                        volume_factor = 1.3

                    adjusted_ttl = int(base_ttl * volatility_factor * volume_factor)
                    return max(config.min_ttl, min(config.max_ttl, adjusted_ttl))

            return base_ttl

        except Exception as e:
            debug(
                "Erro ao calcular TTL dinâmico", error=str(e), data_type=data_type.value
            )
            return self.cache_configs[data_type].base_ttl

    async def _retry_with_backoff(self, operation, *args, **kwargs):
        """
        Retry com exponential backoff usando tenacity.
        Simplificado de 37 LOC para ~15 LOC.
        """

        @retry(
            stop=stop_after_attempt(self._max_retries + 1),
            wait=wait_exponential(
                multiplier=self._base_delay, min=self._base_delay, max=self._max_delay
            ),
            retry=retry_if_exception_type((TimeoutError, Exception)),
            reraise=True,
        )
        async def _execute_with_retry():
            return await operation(*args, **kwargs)

        return await _execute_with_retry()

    def _get_fallback_data(
        self, cache_key: str, max_age_seconds: int | None = None
    ) -> Any | None:
        if not self._fallback_enabled:
            return None

        try:
            max_age = max_age_seconds or self._fallback_max_age
            fallback_key = f"fallback_{cache_key}"
            fallback_data = cache.get(fallback_key)

            if fallback_data and isinstance(fallback_data, dict):
                stored_time = fallback_data.get("timestamp", 0)
                if time.time() - stored_time <= max_age:
                    debug(
                        "Usando dados de fallback",
                        cache_key=cache_key,
                        age_seconds=int(time.time() - stored_time),
                    )
                    return fallback_data.get("data")

            return None
        except Exception as e:
            debug("Erro ao obter dados de fallback", cache_key=cache_key, error=str(e))
            return None

    def _store_fallback_data(self, cache_key: str, data: Any):
        if not self._fallback_enabled:
            return

        try:
            fallback_key = f"fallback_{cache_key}"
            fallback_data = {"data": data, "timestamp": time.time()}
            cache.set(fallback_key, fallback_data, self._fallback_max_age * 2)
        except Exception as e:
            debug(
                "Erro ao armazenar dados de fallback", cache_key=cache_key, error=str(e)
            )

    def _validate_candles_data(
        self, klines: list, symbol: str = ""
    ) -> ValidationResult:
        start_time = time.time()
        result = ValidationResult(is_valid=True, data_size=len(klines) if klines else 0)

        try:
            if not self._validate_candles_basic(klines, result):
                return result

            avg_interval_ms = self._calculate_avg_interval(klines)
            stats = self._validate_candles_content(klines, avg_interval_ms, result)

            self._evaluate_validation_stats(klines, stats, result)

        except Exception as e:
            result.add_error(f"Erro na validação: {str(e)}")
        finally:
            result.processing_time_ms = (time.time() - start_time) * 1000
            self._update_validation_metrics(result)

        return result

    def _validate_candles_basic(self, klines: list, result: ValidationResult) -> bool:
        if not klines or not isinstance(klines, list):
            result.add_error("Dados de candles ausentes ou formato inválido")
            return False

        if len(klines) == 0:
            result.add_error("Lista de candles vazia")
            return False

        if len(klines) < 10:
            result.add_warning(f"Poucos candles retornados: {len(klines)}")

        return True

    def _calculate_avg_interval(self, klines: list) -> float:
        if len(klines) <= 1:
            return 0.0

        intervals = []
        for i in range(1, min(len(klines), 10)):
            try:
                time_diff = int(klines[i][0]) - int(klines[i - 1][0])
                intervals.append(time_diff)
            except (ValueError, IndexError, TypeError):
                pass

        return sum(intervals) / len(intervals) if intervals else 0.0

    def _validate_candles_content(
        self, klines: list, avg_interval_ms: float, result: ValidationResult
    ) -> dict:
        stats = {"invalid": 0, "price_gaps": 0, "time_gaps": 0}

        for i, candle in enumerate(klines):
            if not self._is_valid_candle_structure(candle):
                stats["invalid"] += 1
                continue

            try:
                candle_data = self._extract_candle_values(candle)
                if not candle_data:
                    stats["invalid"] += 1
                    continue

                if candle_data["volume"] < 0:
                    result.add_warning(f"Volume negativo no candle {i}")

                if i > 0:
                    self._check_candle_gaps(
                        i, klines, candle_data, avg_interval_ms, stats, result
                    )

            except (ValueError, IndexError, TypeError):
                stats["invalid"] += 1

        return stats

    def _is_valid_candle_structure(self, candle: Any) -> bool:
        return isinstance(candle, list) and len(candle) >= 12

    def _extract_candle_values(self, candle: list) -> dict | None:
        timestamp = int(candle[0])
        open_price = float(candle[1])
        high_price = float(candle[2])
        low_price = float(candle[3])
        close_price = float(candle[4])
        volume = float(candle[5])

        if timestamp <= 0:
            return None

        prices = [open_price, high_price, low_price, close_price]
        if any(p <= 0 for p in prices):
            return None

        if not (
            low_price <= open_price <= high_price
            and low_price <= close_price <= high_price
        ):
            return None

        return {
            "timestamp": timestamp,
            "open": open_price,
            "close": close_price,
            "volume": volume,
        }

    def _check_candle_gaps(
        self,
        i: int,
        klines: list,
        candle_data: dict,
        avg_interval_ms: float,
        stats: dict,
        result: ValidationResult,
    ) -> None:
        prev_close = float(klines[i - 1][4])
        gap_pct = abs(candle_data["open"] - prev_close) / prev_close * 100
        if gap_pct > 10:
            stats["price_gaps"] += 1

        if avg_interval_ms > 0:
            prev_timestamp = int(klines[i - 1][0])
            time_diff_ms = candle_data["timestamp"] - prev_timestamp
            time_gap_ratio = time_diff_ms / avg_interval_ms
            if time_gap_ratio > 1.5:
                stats["time_gaps"] += 1
                result.add_warning(
                    f"Time gap at candle {i}: {time_gap_ratio:.1f}x expected"
                )

    def _evaluate_validation_stats(
        self, klines: list, stats: dict, result: ValidationResult
    ) -> None:
        total = len(klines)
        invalid_pct = (stats["invalid"] / total) * 100

        if invalid_pct > 10:
            result.add_error(f"Muitos candles inválidos: {invalid_pct:.1f}%")
        elif invalid_pct > 5:
            result.add_warning(f"Candles inválidos: {invalid_pct:.1f}%")

        if stats["price_gaps"] > total * 0.1:
            result.add_warning(f"Muitos gaps de preço: {stats['price_gaps']}")

        if stats["time_gaps"] > total * 0.1:
            result.add_error(
                f"CRITICAL: Muitos time gaps ({stats['time_gaps']}/{total})"
            )

        result.data_quality_score = max(
            0.0,
            1.0
            - (invalid_pct / 100)
            - (stats["price_gaps"] / total)
            - (stats["time_gaps"] / total),
        )

    def _validate_ticker_data(self, ticker: dict, symbol: str = "") -> ValidationResult:
        start_time = time.time()
        result = ValidationResult(is_valid=True)

        try:
            if not self._validate_ticker_structure(ticker, result):
                return result

            result.data_size = len(str(ticker))

            price_field = ticker.get("price") or ticker.get("lastPrice")
            if not self._validate_ticker_price(price_field, result):
                return result

            self._validate_ticker_volume(ticker, result)
            self._check_ticker_fields(ticker, result)

        except Exception as e:
            result.add_error(f"Erro na validação: {str(e)}")
        finally:
            result.processing_time_ms = (time.time() - start_time) * 1000
            self._update_validation_metrics(result)

        return result

    def _validate_ticker_structure(
        self, ticker: dict, result: ValidationResult
    ) -> bool:
        if not isinstance(ticker, dict):
            result.add_error("Ticker não é um dicionário")
            return False

        if not ticker:
            result.add_error("Ticker vazio")
            return False

        return True

    def _validate_ticker_price(self, price_field, result: ValidationResult) -> bool:
        if price_field is None:
            result.add_error("Campo de preço ausente")
            return False

        try:
            price = float(price_field)
            if price <= 0:
                result.add_error(f"Preço inválido: {price}")
                return False
            if not is_numeric_valid(price):
                result.add_error(f"Preço NaN/Inf: {price}")
                return False
        except (ValueError, TypeError):
            result.add_error(f"Preço não numérico: {price_field}")
            return False

        return True

    def _validate_ticker_volume(self, ticker: dict, result: ValidationResult) -> None:
        volume = ticker.get("volume", ticker.get("quoteVolume"))
        if volume is None:
            return

        try:
            vol_float = float(volume)
            if vol_float < 0:
                result.add_warning("Volume negativo")
            elif vol_float == 0:
                result.add_warning("Volume zero")
        except (ValueError, TypeError):
            result.add_warning("Volume inválido")

    def _check_ticker_fields(self, ticker: dict, result: ValidationResult) -> None:
        required_fields = ["symbol", "price"]
        missing = [f for f in required_fields if f not in ticker]
        if missing:
            result.add_warning(f"Campos ausentes: {missing}")

        if len(ticker) < 3:
            result.add_warning("Poucos campos no ticker")

    def _validate_orderbook_data(
        self, orderbook: dict, symbol: str = ""
    ) -> ValidationResult:
        start_time = time.time()
        result = ValidationResult(is_valid=True)

        try:
            if not self._validate_orderbook_structure(orderbook, result):
                return result

            bids = orderbook["bids"]
            asks = orderbook["asks"]
            result.data_size = len(str(orderbook))

            if not self._validate_orderbook_prices(bids, asks, result):
                return result

            self._check_orderbook_spread(bids, asks, result)
            result.data_quality_score = self._calculate_depth_quality(bids, asks)

        except Exception as e:
            result.add_error(f"Erro na validação: {str(e)}")
        finally:
            result.processing_time_ms = (time.time() - start_time) * 1000
            self._update_validation_metrics(result)

        return result

    def _validate_orderbook_structure(
        self, orderbook: Any, result: ValidationResult
    ) -> bool:
        if not isinstance(orderbook, dict):
            result.add_error("Orderbook não é um dicionário")
            return False

        if not orderbook:
            result.add_error("Orderbook vazio")
            return False

        if "bids" not in orderbook or "asks" not in orderbook:
            result.add_error("Bids/asks ausentes")
            return False

        bids = orderbook["bids"]
        asks = orderbook["asks"]

        if not isinstance(bids, list) or not isinstance(asks, list):
            result.add_error("Bids/asks não são listas")
            return False

        if len(bids) == 0 or len(asks) == 0:
            result.add_error(f"Orderbook vazio (bids={len(bids)}, asks={len(asks)})")
            return False

        return True

    def _validate_orderbook_prices(
        self, bids: list, asks: list, result: ValidationResult
    ) -> bool:
        try:
            bid_price = float(bids[0][0])
            ask_price = float(asks[0][0])
            bid_size = float(bids[0][1])
            ask_size = float(asks[0][1])

            if bid_price <= 0 or ask_price <= 0:
                result.add_error(f"Preços inválidos: bid={bid_price}, ask={ask_price}")
                return False

            if ask_price <= bid_price:
                result.add_error(f"Ask <= Bid: ask={ask_price}, bid={bid_price}")
                return False

            if bid_size <= 0 or ask_size <= 0:
                result.add_warning(
                    f"Tamanhos inválidos: bid={bid_size}, ask={ask_size}"
                )

            return True

        except (ValueError, IndexError, TypeError) as e:
            result.add_error(f"Erro ao processar preços: {str(e)}")
            return False

    def _check_orderbook_spread(
        self, bids: list, asks: list, result: ValidationResult
    ) -> None:
        try:
            bid_price = float(bids[0][0])
            ask_price = float(asks[0][0])
            spread_pct = ((ask_price - bid_price) / bid_price) * 100

            if spread_pct > 10:
                result.add_warning(f"Spread muito alto: {spread_pct:.2f}%")
            elif spread_pct > 5:
                result.add_warning(f"Spread alto: {spread_pct:.2f}%")
        except (ValueError, IndexError):
            pass

    def _calculate_depth_quality(self, bids: list, asks: list) -> float:
        depth_quality = 0.0
        depth_count = min(10, len(bids), len(asks))

        for bid, ask in zip(bids[:depth_count], asks[:depth_count], strict=True):
            try:
                bid_p, bid_s = float(bid[0]), float(bid[1])
                ask_p, ask_s = float(ask[0]), float(ask[1])
                if bid_p > 0 and ask_p > 0 and bid_s > 0 and ask_s > 0:
                    depth_quality += 1
            except (ValueError, IndexError):
                pass

        return depth_quality / depth_count if depth_count > 0 else 0.0

    def _update_validation_metrics(self, validation_result: ValidationResult):
        try:
            with self._sync_lock:
                self._validation_counters["total_validations"] += 1

                if not validation_result.is_valid:
                    self._validation_counters["validation_failures"] += 1

                if validation_result.warnings:
                    self._validation_counters["validation_warnings"] += 1

                self._validation_lists["data_quality_scores"].append(
                    validation_result.data_quality_score
                )
                self._validation_lists["validation_times_ms"].append(
                    validation_result.processing_time_ms
                )

                if len(self._validation_lists["data_quality_scores"]) > 1000:
                    self._validation_lists["data_quality_scores"] = (
                        self._validation_lists["data_quality_scores"][-500:]
                    )
                if len(self._validation_lists["validation_times_ms"]) > 1000:
                    self._validation_lists["validation_times_ms"] = (
                        self._validation_lists["validation_times_ms"][-500:]
                    )

        except Exception as e:
            debug("Erro ao atualizar métricas de validação", error=str(e))

    def _safe_cache_key(self, base_key: str) -> str:
        if not isinstance(base_key, str):
            base_key = str(base_key)
        if len(base_key) > 200:
            return f"df_{hashlib.md5(base_key.encode()).hexdigest()}"
        return base_key

    async def _safe_update_market_data(self, symbol: str, data: dict):
        try:
            if not isinstance(symbol, str) or not symbol.strip():
                return

            if not isinstance(data, dict):
                return

            async with self._async_lock:
                current_data = await state.get_market_data(symbol)
                if not isinstance(current_data, dict):
                    current_data = {}
                updated_data = {**current_data, **data}
                await state.update_market_data(symbol, updated_data)
        except Exception as e:
            debug("Erro ao atualizar market data", symbol=symbol, error=str(e))

    async def cleanup_old_data(self, days: int = 30):
        try:
            if not isinstance(days, int) or days <= 0:
                error("CRÍTICO: Parâmetro days inválido", days=days)
                return

            production("Iniciando limpeza de dados antigos", days=days)

            cache_cleaned = 0
            market_data_cleaned = 0
            fallback_cleaned = 0

            try:
                cache.set("cleanup_marker", True, 1)
                cache_cleaned = cache.invalidate()
                production("Cache limpo", entries_removed=cache_cleaned)
            except Exception as cache_error:
                error("Erro na limpeza do cache", error=str(cache_error))

            try:
                market_data_cleaned = state.cleanup_expired_market_data(
                    days * 24 * 3600
                )
                production("Market data limpo", entries_removed=market_data_cleaned)
            except Exception as state_error:
                error("Erro na limpeza do market data", error=str(state_error))

            try:
                fallback_keys = cache.get_all_keys(prefix="fallback_")
                for key in fallback_keys:
                    cache.invalidate(key)
                    fallback_cleaned += 1
                production("Dados de fallback limpos", entries_removed=fallback_cleaned)
            except Exception as fallback_error:
                error(
                    "Erro na limpeza dos dados de fallback", error=str(fallback_error)
                )

            production(
                "Limpeza de dados concluída",
                cache_cleaned=cache_cleaned,
                market_data_cleaned=market_data_cleaned,
                fallback_cleaned=fallback_cleaned,
                days=days,
            )

        except Exception as e:
            error("CRÍTICO: Erro na limpeza de dados antigos", error=str(e))

    def clear_cache(self):
        try:
            with self._sync_lock:
                entries_count = cache.size() if hasattr(cache, "size") else "unknown"
                cache.invalidate()
                production("Todos os caches limpos", previous_count=entries_count)
        except Exception as e:
            error("CRÍTICO: Erro ao limpar caches", error=str(e))

    def get_stats(self) -> dict:
        try:
            with self._sync_lock:
                avg_quality_score = 0.0
                avg_validation_time = 0.0

                data_quality_scores = self._validation_lists["data_quality_scores"]
                validation_times_ms = self._validation_lists["validation_times_ms"]

                if data_quality_scores:
                    avg_quality_score = sum(data_quality_scores) / len(
                        data_quality_scores
                    )

                if validation_times_ms:
                    avg_validation_time = sum(validation_times_ms) / len(
                        validation_times_ms
                    )

                total_validations = self._validation_counters["total_validations"]
                validation_failures = self._validation_counters["validation_failures"]
                validation_warnings = self._validation_counters["validation_warnings"]

                return {
                    "client_configured": self.client is not None,
                    "cache_ttl_settings": {
                        k.value: v.base_ttl for k, v in self.cache_configs.items()
                    },
                    "timeouts": {k.value: v for k, v in self.timeouts.items()},
                    "retry_config": {
                        "max_retries": self._max_retries,
                        "base_delay": self._base_delay,
                        "max_delay": self._max_delay,
                    },
                    "fallback_config": {
                        "enabled": self._fallback_enabled,
                        "max_age_seconds": self._fallback_max_age,
                    },
                    "validation_metrics": {
                        "total_validations": total_validations,
                        "validation_failures": validation_failures,
                        "validation_warnings": validation_warnings,
                        "failure_rate": validation_failures / max(1, total_validations),
                        "warning_rate": validation_warnings / max(1, total_validations),
                        "avg_quality_score": avg_quality_score,
                        "avg_validation_time_ms": avg_validation_time,
                    },
                    "cache_stats": cache.stats() if hasattr(cache, "stats") else {},
                }
        except Exception as e:
            error("Erro ao obter estatísticas do DataFetcher", error=str(e))
            return {}

    async def health_check(self) -> dict:
        health: dict[str, Any] = {
            "status": "healthy",
            "checks": {},
            "timestamp": time.time(),
            "validation_metrics": {},
        }

        try:
            async with self._async_lock:
                if not self.client:
                    health["status"] = "unhealthy"
                    health["checks"]["client"] = "Client não configurado"
                else:
                    health["checks"]["client"] = "OK"

            try:
                cache.set("health_check_test", True, 1)
                health["checks"]["cache"] = "OK"
            except Exception as cache_error:
                health["status"] = "degraded"
                health["checks"]["cache"] = f"Cache error: {str(cache_error)}"

            try:
                await state.get_metric("api_calls_count")
                health["checks"]["state"] = "OK"
            except Exception as state_error:
                health["status"] = "degraded"
                health["checks"]["state"] = f"State error: {str(state_error)}"

            try:
                total_validations = self._validation_counters["total_validations"]
                if total_validations > 0:
                    validation_failures = self._validation_counters[
                        "validation_failures"
                    ]
                    validation_warnings = self._validation_counters[
                        "validation_warnings"
                    ]

                    failure_rate = validation_failures / total_validations
                    if failure_rate > 0.1:
                        health["status"] = "degraded"
                        health["checks"][
                            "validation"
                        ] = f"High validation failure rate: {failure_rate:.2%}"
                    else:
                        health["checks"]["validation"] = "OK"

                    health["validation_metrics"] = {
                        "total_validations": total_validations,
                        "failure_rate": failure_rate,
                        "warning_rate": validation_warnings / total_validations,
                    }
                else:
                    health["checks"]["validation"] = "No validations performed yet"
            except Exception as validation_error:
                health["checks"][
                    "validation"
                ] = f"Validation metrics error: {str(validation_error)}"

        except Exception as e:
            health["status"] = "unhealthy"
            health["error"] = str(e)

        return health
