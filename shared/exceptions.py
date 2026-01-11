from decimal import Decimal
from typing import Any


class TradingError(Exception):
    def __init__(
        self,
        message: str,
        symbol: str | None = None,
        details: dict | None = None,
    ):
        self.symbol = symbol
        self.details = details or {}
        super().__init__(message)


class InsufficientBalanceError(TradingError):
    def __init__(
        self, required: Decimal, available: Decimal, symbol: str | None = None
    ):
        message = f"Insufficient balance: required {required}, available {available}"
        super().__init__(
            message, symbol, {"required": required, "available": available}
        )


class PositionNotFoundError(TradingError):
    def __init__(self, symbol: str) -> None:
        message = f"Position not found for symbol {symbol}"
        super().__init__(message, symbol)


class OrderExecutionError(TradingError):
    def __init__(
        self,
        message: str,
        symbol: str | None = None,
        order_type: str | None = None,
    ):
        super().__init__(message, symbol, {"order_type": order_type})


class ValidationError(TradingError):
    def __init__(self, field: str, value: Any, reason: str) -> None:
        message = f"Validation failed for {field}: {reason}"
        super().__init__(
            message, details={"field": field, "value": value, "reason": reason}
        )


class RateLimitError(TradingError):
    def __init__(self, endpoint: str, retry_after: int | None = None) -> None:
        message = f"Rate limit exceeded for endpoint {endpoint}"
        super().__init__(
            message, details={"endpoint": endpoint, "retry_after": retry_after}
        )


class CircuitBreakerError(TradingError):
    def __init__(self, component: str, reason: str) -> None:
        message = f"Circuit breaker triggered for {component}: {reason}"
        super().__init__(message, details={"component": component, "reason": reason})
