from pydantic import ValidationError as PydanticValidationError


class ValidationError(Exception):
    """Base exception for all validation errors"""

    def __init__(self, message: str, field: str | None = None):
        self.message = message
        self.field = field
        super().__init__(self.message)

    def __str__(self) -> str:
        if self.field:
            return f"{self.field}: {self.message}"
        return self.message


class OrderValidationError(ValidationError):
    """Raised when order validation fails critically"""

    pass


class MarketValidationError(ValidationError):
    """Raised when market data validation fails"""

    pass


class TradingValidationError(ValidationError):
    """Raised when trading parameter validation fails"""

    pass


class PositionValidationError(ValidationError):
    """Raised when position validation fails"""

    pass


class SymbolValidationError(ValidationError):
    """Raised when symbol validation fails"""

    pass


def format_pydantic_errors(e: PydanticValidationError) -> str:
    """Format Pydantic validation errors into readable string."""
    errors = []
    for err in e.errors():
        field = err["loc"][0] if err["loc"] else "unknown"
        errors.append(f"{field}: {err['msg']}")
    return "; ".join(errors)


__all__ = [
    "ValidationError",
    "OrderValidationError",
    "MarketValidationError",
    "TradingValidationError",
    "PositionValidationError",
    "SymbolValidationError",
    "format_pydantic_errors",
]
