import json
import logging
import threading
import time
from datetime import date, datetime
from enum import Enum
from typing import Any

from shared.log_sanitizers import LogSanitizer


class DebugLevel(Enum):
    BASIC = "basic"
    DETAILED = "detailed"
    VERBOSE = "verbose"


class CompactJSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.now().astimezone().isoformat(),
            "lvl": record.levelname[:3],
            "mod": record.module,
            "func": record.funcName,
            "line": record.lineno,
            "msg": LogSanitizer.sanitize_message(record.getMessage()),
        }

        if hasattr(record, "extra_data") and record.extra_data:
            sanitized_extra = self._serialize_data(record.extra_data)
            data.update(sanitized_extra)

        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)

        return json.dumps(data, ensure_ascii=False, default=self._json_serial)

    def _serialize_data(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {"data": LogSanitizer.sanitize_value("data", data)}

        serialized = {}
        for key, value in data.items():
            if isinstance(value, (datetime, date)):
                serialized[key] = value.isoformat()
            elif hasattr(value, "__dict__"):
                try:
                    serialized[key] = LogSanitizer.sanitize_value(key, str(value))
                except Exception:
                    try:
                        serialized[key] = LogSanitizer.sanitize_value(key, repr(value))
                    except Exception:
                        serialized[key] = (
                            f"<serialization_error: {type(value).__name__}>"
                        )
            else:
                serialized[key] = LogSanitizer.sanitize_value(key, value)
        return serialized

    def _json_serial(self, obj: Any) -> str:
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        elif hasattr(obj, "__dict__"):
            return f"<{type(obj).__name__}_object>"
        elif hasattr(obj, "__str__"):
            return str(obj)
        else:
            return f"<non_serializable_{type(obj).__name__}>"


class MessageAggregator:
    def __init__(
        self, window: int = 5, max_cache_size: int = 1000, ttl_minutes: int = 5
    ):
        self.window = window
        self.max_cache_size = max_cache_size
        self.ttl_seconds = ttl_minutes * 60
        self.cache: dict[str, tuple[float, int]] = {}
        self.lock = threading.RLock()
        self._last_cleanup = time.time()
        self._cleanup_interval = 60
        self._memory_usage = 0
        self._max_memory_mb = 50
        self._max_memory_bytes = self._max_memory_mb * 1024 * 1024

    def should_log(
        self, msg: str, level: str, context: dict[str, Any] | None = None
    ) -> tuple[bool, int]:
        with self.lock:
            now = time.time()
            self._maybe_cleanup(now)

            key = self._build_cache_key(msg, level, context)

            if key in self.cache:
                return self._handle_existing_key(key, now)

            self._ensure_cache_capacity(now)
            self._check_memory_usage(key)
            self.cache[key] = (now, 1)
            return True, 0

    def _maybe_cleanup(self, now: float) -> None:
        if now - self._last_cleanup > self._cleanup_interval:
            self._force_cleanup(now)
            self._last_cleanup = now

    def _build_cache_key(
        self, msg: str, level: str, context: dict[str, Any] | None
    ) -> str:
        if isinstance(msg, str) and len(msg) > 500:
            msg = msg[:500] + "...[TRUNCATED]"

        if context:
            context_keys = sorted(context.items())
            context_hash = hash(
                frozenset(
                    (k, str(v)[:50])
                    for k, v in context_keys
                    if k in {"symbol", "trade_id", "order_id"}
                )
            )
            key = f"{level}:{msg}:{context_hash}"
        else:
            key = f"{level}:{msg}"

        return key[:1000] if len(key) > 1000 else key

    def _handle_existing_key(self, key: str, now: float) -> tuple[bool, int]:
        last_time, count = self.cache[key]
        if now - last_time < self.window:
            if count < 10000:
                self.cache[key] = (last_time, count + 1)
            return False, 0
        else:
            old_count = count
            del self.cache[key]
            return True, old_count

    def _ensure_cache_capacity(self, now: float) -> None:
        if len(self.cache) >= self.max_cache_size:
            self._emergency_cleanup(now)

    def _force_cleanup(self, now: float) -> int:
        expired = []
        for k, (t, _) in self.cache.items():
            if now - t > max(self.window, self.ttl_seconds):
                expired.append(k)

        removed = 0
        for k in expired:
            if k in self.cache:
                del self.cache[k]
                removed += 1

        self._update_memory_usage()
        return removed

    def _emergency_cleanup(self, now: float) -> None:
        if not self.cache:
            return

        sorted_items = sorted(self.cache.items(), key=lambda x: x[1][0])
        remove_count = max(1, len(sorted_items) // 3)

        for key, _ in sorted_items[:remove_count]:
            self.cache.pop(key, None)

        self._update_memory_usage()

    def _check_memory_usage(self, new_key: str) -> None:
        estimated_size = len(new_key) * 2 + 64
        if self._memory_usage + estimated_size > self._max_memory_bytes:
            self._emergency_cleanup(time.time())

    def _update_memory_usage(self) -> None:
        total_size = 0
        for key in self.cache.keys():
            total_size += len(key) * 2 + 64
        self._memory_usage = total_size

    def get_cache_stats(self) -> dict[str, Any]:
        with self.lock:
            return {
                "cache_size": len(self.cache),
                "max_size": self.max_cache_size,
                "window_seconds": self.window,
                "ttl_seconds": self.ttl_seconds,
                "memory_usage_mb": round(self._memory_usage / (1024 * 1024), 2),
                "max_memory_mb": self._max_memory_mb,
                "last_cleanup": self._last_cleanup,
            }


class DebugFilter:
    def __init__(self, debug_level: DebugLevel = DebugLevel.BASIC) -> None:
        self.debug_level = debug_level
        self.module_filters: set[str] = set()
        self.message_rate_limiter: dict[str, float] = {}
        self.rate_limit_window = 10
        self.lock = threading.RLock()

    def should_log_debug(self, module: str, msg: str, diagnostic: bool = False) -> bool:
        with self.lock:
            if diagnostic:
                return True

            if self.debug_level == DebugLevel.BASIC:
                return "error" in msg.lower() or "critical" in msg.lower()
            elif self.debug_level == DebugLevel.DETAILED:
                if self.module_filters and module not in self.module_filters:
                    return False
                return not self._is_rate_limited(f"{module}:{msg[:50]}")
            else:
                return True

    def add_module_filter(self, module: str) -> None:
        with self.lock:
            self.module_filters.add(module)

    def remove_module_filter(self, module: str) -> None:
        with self.lock:
            self.module_filters.discard(module)

    def set_debug_level(self, level: DebugLevel) -> None:
        self.debug_level = level

    def _is_rate_limited(self, key: str) -> bool:
        now = time.time()
        if key in self.message_rate_limiter:
            if now - self.message_rate_limiter[key] < self.rate_limit_window:
                return True

        self.message_rate_limiter[key] = now

        if len(self.message_rate_limiter) > 1000:
            cutoff = now - self.rate_limit_window * 2
            self.message_rate_limiter = {
                k: v for k, v in self.message_rate_limiter.items() if v > cutoff
            }

        return False
