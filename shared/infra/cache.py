import builtins
import gzip
import pickle
import sys
import threading
import time
from collections import OrderedDict
from collections import OrderedDict as OrderedDictType
from typing import Any

from shared.enums import CacheEvent, MarketVolatility
from shared.observability.flow_tracker import track_component


class UnifiedCache:
    def __init__(self, max_size: int = 1000, max_memory_mb: int = 100) -> None:
        self._cache: OrderedDictType[str, tuple[Any, float, int]] = OrderedDict()
        self._max_size = max(10, max_size)
        self._max_memory_bytes = max_memory_mb * 1024 * 1024
        self._current_memory_bytes = 0
        self._lock = threading.RLock()
        self._event_invalidations: dict[CacheEvent, set[str]] = {
            event: set() for event in CacheEvent
        }
        self._volatility_state = MarketVolatility.MEDIUM
        self._hit_count = 0
        self._miss_count = 0
        self._eviction_count = 0
        self._compression_threshold = 1024

    @track_component("cache", slow_threshold=1)
    def get(self, key: str) -> Any | None:
        if not key:
            return None

        with self._lock:
            if key in self._cache:
                value, expiry, size = self._cache[key]
                if expiry > time.time():
                    self._cache.move_to_end(key)
                    self._hit_count += 1

                    if isinstance(value, bytes) and value.startswith(b"\x1f\x8b"):
                        try:
                            return pickle.loads(gzip.decompress(value))
                        except (
                            gzip.BadGzipFile,
                            pickle.UnpicklingError,
                            pickle.PickleError,
                            EOFError,
                        ):
                            del self._cache[key]
                            self._current_memory_bytes -= size
                            return None
                    return value
                else:
                    self._current_memory_bytes -= size
                    del self._cache[key]

            self._miss_count += 1
            return None

    @track_component("cache", slow_threshold=1)
    def set(
        self,
        key: str,
        value: Any,
        ttl: int | None = None,
        auto_invalidate_on: builtins.set[CacheEvent] | None = None,
    ) -> bool:
        if not key:
            return False

        calculated_ttl = self._calculate_dynamic_ttl(key, ttl)
        if calculated_ttl <= 0:
            return False

        with self._lock:
            compressed_value, value_size = self._prepare_value_for_storage(value)

            if key in self._cache:
                _, _, old_size = self._cache[key]
                self._current_memory_bytes -= old_size

            new_memory_usage = self._current_memory_bytes + value_size
            if new_memory_usage > self._max_memory_bytes:
                self._evict_by_memory(new_memory_usage - self._max_memory_bytes)

            if len(self._cache) >= self._max_size:
                self._evict_lru_entries(max(1, int(self._max_size * 0.1)))

            expiry = time.time() + calculated_ttl
            self._cache[key] = (compressed_value, expiry, value_size)
            self._current_memory_bytes += value_size

            if auto_invalidate_on:
                for event in auto_invalidate_on:
                    self._event_invalidations[event].add(key)

            return True

    @track_component("cache")
    def invalidate(self, pattern: str = "") -> int:
        with self._lock:
            if pattern:
                keys_to_delete = [k for k in self._cache.keys() if pattern in k]
                for key in keys_to_delete:
                    _, _, size = self._cache[key]
                    self._current_memory_bytes -= size
                    del self._cache[key]
                    self._remove_from_event_invalidations(key)
                return len(keys_to_delete)
            else:
                count = len(self._cache)
                self._cache.clear()
                self._current_memory_bytes = 0
                for event_set in self._event_invalidations.values():
                    event_set.clear()
                return count

    def invalidate_by_event(self, event: CacheEvent) -> int:
        with self._lock:
            keys_to_invalidate = self._event_invalidations[event].copy()
            count = 0
            for key in keys_to_invalidate:
                if key in self._cache:
                    _, _, size = self._cache[key]
                    self._current_memory_bytes -= size
                    del self._cache[key]
                    count += 1

            self._event_invalidations[event].clear()
            return count

    def update_market_volatility(self, volatility: MarketVolatility):
        with self._lock:
            if self._volatility_state != volatility:
                self._volatility_state = volatility
                if volatility in [MarketVolatility.HIGH, MarketVolatility.EXTREME]:
                    self.invalidate_by_event(CacheEvent.MARKET_DATA_UPDATE)

    @track_component("cache", slow_threshold=2)
    def cleanup_expired(self) -> int:
        with self._lock:
            return self._cleanup_expired()

    def size(self) -> int:
        with self._lock:
            return len(self._cache)

    def get_all_keys(self, prefix: str | None = None) -> list[str]:
        """
        Retorna todas as chaves do cache, opcionalmente filtradas por prefixo.

        Args:
            prefix: Se fornecido, retorna apenas chaves que começam com este prefixo

        Returns:
            Lista de chaves do cache
        """
        with self._lock:
            if prefix:
                return [k for k in self._cache.keys() if k.startswith(prefix)]
            return list(self._cache.keys())

    def memory_usage_mb(self) -> float:
        with self._lock:
            return self._current_memory_bytes / (1024 * 1024)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total_requests = self._hit_count + self._miss_count
            hit_ratio = self._hit_count / total_requests if total_requests > 0 else 0

            return {
                "size": len(self._cache),
                "max_size": self._max_size,
                "memory_usage_mb": self.memory_usage_mb(),
                "max_memory_mb": self._max_memory_bytes / (1024 * 1024),
                "hit_count": self._hit_count,
                "miss_count": self._miss_count,
                "hit_ratio": round(hit_ratio, 3),
                "eviction_count": self._eviction_count,
                "volatility_state": self._volatility_state.value,
            }

    def _calculate_dynamic_ttl(self, key: str, base_ttl: int | None) -> int:
        if base_ttl is not None:
            return base_ttl

        volatility_multipliers = {
            MarketVolatility.LOW: 2.0,
            MarketVolatility.MEDIUM: 1.0,
            MarketVolatility.HIGH: 0.5,
            MarketVolatility.EXTREME: 0.2,
        }

        base_ttl_by_type = {
            "price": 30,
            "orderbook": 15,
            "ticker": 60,
            "klines": 300,
            "account": 120,
            "position": 60,
            "balance": 180,
            "analysis": 300,
        }

        key_lower = key.lower()
        determined_ttl = 300

        for data_type, ttl in base_ttl_by_type.items():
            if data_type in key_lower:
                determined_ttl = ttl
                break

        multiplier = volatility_multipliers[self._volatility_state]
        return int(determined_ttl * multiplier)

    def _prepare_value_for_storage(self, value: Any) -> tuple[Any, int]:
        try:
            serialized = pickle.dumps(value)
            size = len(serialized)

            if size > self._compression_threshold:
                compressed = gzip.compress(serialized)
                if len(compressed) < size * 0.8:
                    return compressed, len(compressed)

            return value, size  # FIX: Use deep size instead of shallow sys.getsizeof
        except (pickle.PickleError, TypeError, OverflowError, MemoryError):
            # Fallback: estimate size for non-picklable objects
            return value, sys.getsizeof(value)

    def _evict_by_memory(self, bytes_to_free: int):
        freed_bytes = 0
        keys_to_remove = []

        for key, (_, _, size) in self._cache.items():
            keys_to_remove.append((key, size))
            freed_bytes += size
            if freed_bytes >= bytes_to_free:
                break

        for key, size in keys_to_remove:
            del self._cache[key]
            self._current_memory_bytes -= size
            self._remove_from_event_invalidations(key)
            self._eviction_count += 1

    def _evict_lru_entries(self, count: int):
        keys_to_remove = list(self._cache.keys())[:count]
        for key in keys_to_remove:
            _, _, size = self._cache[key]
            self._current_memory_bytes -= size
            del self._cache[key]
            self._remove_from_event_invalidations(key)
            self._eviction_count += 1

    def _cleanup_expired(self) -> int:
        now = time.time()
        expired_keys = []

        for key, (_, exp, size) in self._cache.items():
            if exp < now:
                expired_keys.append((key, size))

        for key, size in expired_keys:
            del self._cache[key]
            self._current_memory_bytes -= size
            self._remove_from_event_invalidations(key)

        return len(expired_keys)

    def _remove_from_event_invalidations(self, key: str):
        for event_set in self._event_invalidations.values():
            event_set.discard(key)


cache = UnifiedCache(max_size=1000, max_memory_mb=100)
