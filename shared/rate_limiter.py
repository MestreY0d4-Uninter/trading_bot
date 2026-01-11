import asyncio
import heapq
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .observability.logger import debug, error, warning


class Priority(Enum):
    CRITICAL = 1
    HIGH = 2
    NORMAL = 3
    LOW = 4


@dataclass
class QueuedRequest:
    priority: Priority
    timestamp: float
    weight: int
    endpoint: str
    future: asyncio.Future
    adaptive_weight: int = field(init=False)

    def __post_init__(self):
        self.adaptive_weight = self.weight

    def __lt__(self, other):
        if self.priority.value != other.priority.value:
            return self.priority.value < other.priority.value
        return self.timestamp < other.timestamp


@dataclass
class EndpointConfig:
    base_weight: int
    max_requests_per_second: int = 10
    max_requests_per_minute: int = 100
    backoff_multiplier: float = 1.0
    last_429_time: float = 0.0
    consecutive_429s: int = 0


class RateLimiter:
    BINANCE_MAX_CALLS_PER_MINUTE = 1200
    BINANCE_MAX_WEIGHT_PER_MINUTE = 6000
    WINDOW_SECONDS = 60
    PRECISION_MS = 100

    ENDPOINT_CONFIGS = {
        "/api/v3/ticker/price": EndpointConfig(1, 40, 400),
        "/api/v3/ticker/24hr": EndpointConfig(1, 40, 400),
        "/api/v3/depth": EndpointConfig(1, 20, 200),
        "/api/v3/trades": EndpointConfig(1, 20, 200),
        "/api/v3/klines": EndpointConfig(1, 20, 200),
        "/api/v3/avgPrice": EndpointConfig(1, 30, 300),
        "/api/v3/account": EndpointConfig(10, 5, 60),
        "/api/v3/myTrades": EndpointConfig(10, 5, 60),
        "/api/v3/order": EndpointConfig(1, 10, 100),
        "/api/v3/order/test": EndpointConfig(1, 10, 100),
        "/api/v3/openOrders": EndpointConfig(3, 5, 60),
        "/api/v3/allOrders": EndpointConfig(10, 2, 20),
        "/fapi/v1/positionRisk": EndpointConfig(5, 5, 50),
        "/fapi/v1/balance": EndpointConfig(5, 5, 50),
        "/fapi/v1/order": EndpointConfig(1, 10, 100),
        "/fapi/v1/openOrders": EndpointConfig(1, 5, 50),
        "default": EndpointConfig(1, 10, 100),
    }

    CRITICAL_ENDPOINTS = {
        "/api/v3/order",
        "/fapi/v1/order",
        "/api/v3/account",
        "/fapi/v1/positionRisk",
        "/fapi/v1/balance",
    }

    def __init__(
        self,
        calls_per_minute: int | None = None,
        weight_per_minute: int | None = None,
    ):
        self._calls_limit = calls_per_minute or self.BINANCE_MAX_CALLS_PER_MINUTE
        self._weight_limit = weight_per_minute or self.BINANCE_MAX_WEIGHT_PER_MINUTE

        if self._calls_limit > self.BINANCE_MAX_CALLS_PER_MINUTE:
            raise ValueError(
                f"Calls limit {self._calls_limit} exceeds Binance maximum {self.BINANCE_MAX_CALLS_PER_MINUTE}"
            )
        if self._weight_limit > self.BINANCE_MAX_WEIGHT_PER_MINUTE:
            raise ValueError(
                f"Weight limit {self._weight_limit} exceeds Binance maximum {self.BINANCE_MAX_WEIGHT_PER_MINUTE}"
            )

        self._precision_interval = self.PRECISION_MS / 1000.0
        self._calls_window: deque[float] = deque()
        self._weight_window: deque[tuple[float, int]] = deque()

        self._endpoint_windows: dict[str, deque] = defaultdict(deque)
        self._endpoint_weight_windows: dict[str, deque] = defaultdict(deque)
        self._endpoint_configs = self.ENDPOINT_CONFIGS.copy()

        self._request_queue: list[QueuedRequest] = []
        self._processing_queue = False

        self._async_lock = asyncio.Lock()
        self._queue_condition = asyncio.Condition()

        self._adaptive_mode = True
        self._global_backoff_multiplier = 1.0
        self._last_api_error_time = 0.0
        self._consecutive_errors = 0

        self._total_requests = 0
        self._total_waits = 0
        self._total_wait_time = 0.0
        self._rate_limit_hits = 0
        self._adaptive_adjustments = 0

        self._running = True
        self._queue_processor_task: asyncio.Task | None = None

    async def start_queue_processor(self):
        if self._queue_processor_task is None or self._queue_processor_task.done():
            self._queue_processor_task = asyncio.create_task(self._process_queue())

    async def stop_queue_processor(self):
        self._running = False
        if self._queue_processor_task:
            self._queue_processor_task.cancel()
            try:
                await self._queue_processor_task
            except asyncio.CancelledError:
                pass

    def _get_endpoint_priority(self, endpoint: str) -> Priority:
        if endpoint in self.CRITICAL_ENDPOINTS:
            return Priority.CRITICAL
        if "account" in endpoint or "position" in endpoint or "balance" in endpoint:
            return Priority.HIGH
        if "order" in endpoint:
            return Priority.HIGH
        return Priority.NORMAL

    def _get_endpoint_config(self, endpoint: str) -> EndpointConfig:
        for pattern, config in self._endpoint_configs.items():
            if pattern in endpoint:
                return config
        return self._endpoint_configs["default"]

    async def acquire(
        self,
        weight: int = 1,
        endpoint: str | None = None,
        priority: Priority | None = None,
    ) -> float:
        if not isinstance(weight, int) or weight <= 0:
            error("Invalid weight in rate limiter", weight=weight, endpoint=endpoint)
            raise ValueError(f"Weight must be positive integer, got: {weight}")

        if weight > self._weight_limit:
            error(
                "Weight exceeds limit",
                weight=weight,
                limit=self._weight_limit,
                endpoint=endpoint,
            )
            raise ValueError(
                f"Weight {weight} exceeds maximum limit {self._weight_limit}"
            )

        endpoint = endpoint or "unknown"
        if priority is None:
            priority = self._get_endpoint_priority(endpoint)

        start_time = time.time()

        request_future: asyncio.Future[float] = asyncio.Future()
        queued_request = QueuedRequest(
            priority=priority,
            timestamp=start_time,
            weight=weight,
            endpoint=endpoint,
            future=request_future,
        )

        async with self._queue_condition:
            heapq.heappush(self._request_queue, queued_request)
            self._queue_condition.notify()

        await self.start_queue_processor()

        return await request_future

    async def _process_queue(self):
        while self._running:
            try:
                async with self._queue_condition:
                    if not self._request_queue:
                        await self._queue_condition.wait()
                        continue

                    request = heapq.heappop(self._request_queue)

                if request.future.cancelled():
                    continue

                try:
                    wait_time = await self._process_request(request)
                    if not request.future.done():
                        request.future.set_result(wait_time)
                except Exception as e:
                    if not request.future.done():
                        request.future.set_exception(e)

            except asyncio.CancelledError:
                break
            except Exception as e:
                error("Queue processor error", error=str(e))
                await asyncio.sleep(0.1)

        async with self._queue_condition:
            while self._request_queue:
                request = heapq.heappop(self._request_queue)
                if not request.future.done():
                    request.future.set_exception(asyncio.CancelledError())

    async def _process_request(self, request: QueuedRequest) -> float:
        async with self._async_lock:
            if self._adaptive_mode:
                request.adaptive_weight = self._calculate_adaptive_weight(request)

            wait_time = await self._wait_for_capacity(
                request.adaptive_weight, request.endpoint
            )

            now = time.time()
            self._record_request(now, request.adaptive_weight, request.endpoint)

            self._total_requests += 1
            if wait_time > 0:
                self._total_waits += 1
                self._total_wait_time += wait_time

            return wait_time

    def _calculate_adaptive_weight(self, request: QueuedRequest) -> int:
        config = self._get_endpoint_config(request.endpoint)
        base_weight = request.weight

        adaptive_multiplier = max(
            config.backoff_multiplier, self._global_backoff_multiplier
        )

        if config.consecutive_429s > 0:
            adaptive_multiplier *= 1.5 ** min(config.consecutive_429s, 5)

        if self._consecutive_errors > 0:
            adaptive_multiplier *= 1.2 ** min(self._consecutive_errors, 3)

        adaptive_weight = max(1, int(base_weight * adaptive_multiplier))

        if adaptive_weight != base_weight:
            self._adaptive_adjustments += 1
            debug(
                "Adaptive weight adjustment",
                endpoint=request.endpoint,
                original_weight=base_weight,
                adaptive_weight=adaptive_weight,
                multiplier=adaptive_multiplier,
            )

        return adaptive_weight

    def _record_request(self, timestamp: float, weight: int, endpoint: str):
        self._calls_window.append(timestamp)
        self._weight_window.append((timestamp, weight))

        self._endpoint_windows[endpoint].append(timestamp)
        self._endpoint_weight_windows[endpoint].append((timestamp, weight))

    async def _wait_for_capacity(self, weight: int, endpoint: str) -> float:
        total_wait_time = 0.0
        max_wait_time = 300.0
        config = self._get_endpoint_config(endpoint)

        while total_wait_time < max_wait_time:
            self._cleanup_expired_entries()
            self._cleanup_endpoint_entries(endpoint)

            if await self._check_capacity(weight, endpoint, config):
                break

            wait_time = self._calculate_optimal_wait_time(weight, endpoint, config)

            self._log_rate_limit_status(endpoint, wait_time)

            await asyncio.sleep(wait_time)
            total_wait_time += wait_time

        if total_wait_time >= max_wait_time:
            error("Rate limiter timeout", timeout=max_wait_time, endpoint=endpoint)
            raise RuntimeError(
                f"Rate limiter timeout - waited {max_wait_time}s for capacity"
            )

        return total_wait_time

    async def _check_capacity(
        self, weight: int, endpoint: str, config: EndpointConfig
    ) -> bool:
        current_calls = len(self._calls_window)
        current_weight = sum(w for _, w in self._weight_window)

        if current_calls >= self._calls_limit:
            return False
        if current_weight + weight > self._weight_limit:
            return False

        endpoint_calls = len(self._endpoint_windows[endpoint])
        endpoint_weight = sum(w for _, w in self._endpoint_weight_windows[endpoint])

        calls_per_second = sum(
            1 for t in self._endpoint_windows[endpoint] if time.time() - t <= 1.0
        )

        if calls_per_second >= config.max_requests_per_second:
            return False
        if endpoint_calls >= config.max_requests_per_minute:
            return False
        if (
            endpoint_weight + weight
            > config.max_requests_per_minute * config.base_weight
        ):
            return False

        return True

    def _calculate_optimal_wait_time(
        self, weight: int, endpoint: str, config: EndpointConfig
    ) -> float:
        wait_times = []
        now = time.time()

        self._add_calls_limit_wait(wait_times, now)
        self._add_weight_limit_wait(wait_times, weight, now)
        self._add_endpoint_limit_wait(wait_times, endpoint, config, now)
        self._add_rate_limit_wait(wait_times, endpoint, config, now)

        if wait_times:
            base_wait = max(0.1, min(wait_times))
            multiplier = max(config.backoff_multiplier, self._global_backoff_multiplier)
            return min(base_wait * multiplier, 30.0)

        return self._precision_interval

    def _add_calls_limit_wait(self, wait_times: list, now: float) -> None:
        current_calls = len(self._calls_window)
        if current_calls >= self._calls_limit and self._calls_window:
            oldest = self._calls_window[0]
            wait_times.append(
                oldest + self.WINDOW_SECONDS + self._precision_interval - now
            )

    def _add_weight_limit_wait(self, wait_times: list, weight: int, now: float) -> None:
        current_weight = sum(w for _, w in self._weight_window)
        if current_weight + weight <= self._weight_limit:
            return

        needed_reduction = (current_weight + weight) - self._weight_limit
        accumulated = 0

        for entry_time, entry_weight in self._weight_window:
            accumulated += entry_weight
            if accumulated >= needed_reduction:
                wait_times.append(
                    entry_time + self.WINDOW_SECONDS + self._precision_interval - now
                )
                break

    def _add_endpoint_limit_wait(
        self, wait_times: list, endpoint: str, config: EndpointConfig, now: float
    ) -> None:
        endpoint_calls = len(self._endpoint_windows[endpoint])
        if (
            endpoint_calls >= config.max_requests_per_minute
            and self._endpoint_windows[endpoint]
        ):
            oldest = self._endpoint_windows[endpoint][0]
            wait_times.append(
                oldest + self.WINDOW_SECONDS + self._precision_interval - now
            )

    def _add_rate_limit_wait(
        self, wait_times: list, endpoint: str, config: EndpointConfig, now: float
    ) -> None:
        recent_calls = [t for t in self._endpoint_windows[endpoint] if now - t <= 1.0]
        if len(recent_calls) >= config.max_requests_per_second and recent_calls:
            wait_times.append(1.1 - (now - max(recent_calls)))

    def _cleanup_expired_entries(self):
        cutoff_time = time.time() - self.WINDOW_SECONDS

        while self._calls_window and self._calls_window[0] <= cutoff_time:
            self._calls_window.popleft()

        while self._weight_window and self._weight_window[0][0] <= cutoff_time:
            self._weight_window.popleft()

    def _cleanup_endpoint_entries(self, endpoint: str):
        cutoff_time = time.time() - self.WINDOW_SECONDS

        endpoint_window = self._endpoint_windows[endpoint]
        while endpoint_window and endpoint_window[0] <= cutoff_time:
            endpoint_window.popleft()

        endpoint_weight_window = self._endpoint_weight_windows[endpoint]
        while endpoint_weight_window and endpoint_weight_window[0][0] <= cutoff_time:
            endpoint_weight_window.popleft()

    def _log_rate_limit_status(self, endpoint: str, wait_time: float):
        current_calls = len(self._calls_window)
        current_weight = sum(w for _, w in self._weight_window)

        calls_pct = (current_calls / self._calls_limit) * 100
        weight_pct = (current_weight / self._weight_limit) * 100

        if calls_pct >= 90 or weight_pct >= 90:
            warning(
                "Rate limit high usage",
                calls_pct=calls_pct,
                weight_pct=weight_pct,
                endpoint=endpoint,
                wait_time=wait_time,
            )

        debug(
            "Rate limit waiting",
            current_calls=current_calls,
            calls_limit=self._calls_limit,
            current_weight=current_weight,
            weight_limit=self._weight_limit,
            endpoint=endpoint,
            wait_time=wait_time,
        )

    async def handle_rate_limit_response(
        self, endpoint: str, retry_after: int | None = None
    ):
        self._rate_limit_hits += 1
        config = self._get_endpoint_config(endpoint)

        config.last_429_time = time.time()
        config.consecutive_429s += 1
        config.backoff_multiplier = min(config.backoff_multiplier * 1.5, 5.0)

        self._global_backoff_multiplier = min(
            self._global_backoff_multiplier * 1.2, 3.0
        )

        warning(
            "Rate limit hit - applying adaptive backoff",
            endpoint=endpoint,
            consecutive_429s=config.consecutive_429s,
            backoff_multiplier=config.backoff_multiplier,
            retry_after=retry_after,
        )

        if retry_after:
            await asyncio.sleep(min(retry_after, 60))

    async def handle_success_response(self, endpoint: str):
        config = self._get_endpoint_config(endpoint)

        if config.consecutive_429s > 0:
            config.consecutive_429s = max(0, config.consecutive_429s - 1)
            if config.consecutive_429s == 0:
                config.backoff_multiplier = max(1.0, config.backoff_multiplier * 0.9)

        if self._consecutive_errors > 0:
            self._consecutive_errors = max(0, self._consecutive_errors - 1)

        if self._global_backoff_multiplier > 1.0:
            self._global_backoff_multiplier = max(
                1.0, self._global_backoff_multiplier * 0.95
            )

    async def handle_error_response(self, endpoint: str, error_code: int | None = None):
        self._consecutive_errors += 1
        self._last_api_error_time = time.time()

        if error_code == 429:
            await self.handle_rate_limit_response(endpoint)
        elif error_code in [418, 451]:  # IP ban codes
            error("Potential IP ban detected", endpoint=endpoint, error_code=error_code)
            self._global_backoff_multiplier = 5.0
            await asyncio.sleep(60)

    def get_current_limits(self) -> dict[str, Any]:
        return {
            "calls_limit": self._calls_limit,
            "weight_limit": self._weight_limit,
            "window_seconds": self.WINDOW_SECONDS,
            "precision_ms": self.PRECISION_MS,
            "adaptive_mode": self._adaptive_mode,
            "global_backoff_multiplier": self._global_backoff_multiplier,
        }

    async def get_status(self) -> dict[str, Any]:
        async with self._async_lock:
            self._cleanup_expired_entries()

            current_calls = len(self._calls_window)
            current_weight = sum(w for _, w in self._weight_window)

            endpoint_status = {}
            for endpoint, window in self._endpoint_windows.items():
                if window:
                    endpoint_calls = len(window)
                    endpoint_weight = sum(
                        w for _, w in self._endpoint_weight_windows[endpoint]
                    )
                    config = self._get_endpoint_config(endpoint)

                    endpoint_status[endpoint] = {
                        "calls": endpoint_calls,
                        "weight": endpoint_weight,
                        "max_calls": config.max_requests_per_minute,
                        "consecutive_429s": config.consecutive_429s,
                        "backoff_multiplier": config.backoff_multiplier,
                    }

            return {
                "global": {
                    "calls": f"{current_calls}/{self._calls_limit}",
                    "weight": f"{current_weight}/{self._weight_limit}",
                    "calls_remaining": self._calls_limit - current_calls,
                    "weight_remaining": self._weight_limit - current_weight,
                    "utilization_pct": round(
                        (current_calls / self._calls_limit) * 100, 1
                    ),
                    "weight_utilization_pct": round(
                        (current_weight / self._weight_limit) * 100, 1
                    ),
                },
                "endpoints": endpoint_status,
                "queue": {
                    "pending_requests": len(self._request_queue),
                    "processing": self._processing_queue,
                },
                "adaptive": {
                    "global_backoff_multiplier": self._global_backoff_multiplier,
                    "consecutive_errors": self._consecutive_errors,
                    "adaptive_adjustments": self._adaptive_adjustments,
                },
                "statistics": {
                    "total_requests": self._total_requests,
                    "total_waits": self._total_waits,
                    "avg_wait_time": round(
                        self._total_wait_time / max(1, self._total_waits), 3
                    ),
                    "total_wait_time": round(self._total_wait_time, 3),
                    "rate_limit_hits": self._rate_limit_hits,
                },
            }


rate_limiter = RateLimiter()
