from collections.abc import Callable
from typing import Any

from .exceptions import (
    MarketValidationError,
    OrderValidationError,
    PositionValidationError,
    SymbolValidationError,
    TradingValidationError,
    ValidationError,
)
from .market_validator import MarketValidator
from .trading_validator import TradingValidator


def graceful_validate(
    validator_func: Callable[..., None],
    *args,
    default_value: Any = None,
    log_errors: bool = True,
    **kwargs,
) -> tuple[bool, Any | None, str | None]:
    """
    Wrapper para validações com degradação graceful.

    Converte validators *_or_raise() em padrão graceful que retorna tuple.
    Ideal para validações não-críticas onde queremos continuar operação.

    Args:
        validator_func: Função validator que pode lançar exceção
        *args: Argumentos para o validator
        default_value: Valor padrão se validação falhar
        log_errors: Se deve logar erros (default True)
        **kwargs: Kwargs para o validator

    Returns:
        Tuple[bool, Optional[Any], Optional[str]]:
            - bool: True se validação passou
            - Any: Valor validado ou default_value
            - str: Mensagem de erro se houver

    Example:
        >>> is_valid, data, error = graceful_validate(
        ...     MarketValidator.validate_ticker_data_or_raise,
        ...     ticker_data
        ... )
        >>> if not is_valid:
        ...     warning("Ticker validation failed gracefully", error=error)
        ...     # Continue with fallback data instead of crashing
    """
    try:
        validator_func(*args, **kwargs)
        return True, args[0] if args else None, None
    except ValidationError as e:
        error_msg = str(e)
        if log_errors:
            from shared.observability.logger import warning

            warning(
                "Graceful validation failed",
                validator=validator_func.__name__,
                error=error_msg,
            )
        return False, default_value, error_msg
    except Exception as e:
        error_msg = f"Unexpected validation error: {str(e)}"
        if log_errors:
            from shared.observability.logger import error as log_error

            log_error(
                "Critical validation error in graceful wrapper",
                validator=validator_func.__name__,
                error=str(e),
            )
        return False, default_value, error_msg


__all__ = [
    "TradingValidator",
    "MarketValidator",
    "ValidationError",
    "OrderValidationError",
    "MarketValidationError",
    "TradingValidationError",
    "PositionValidationError",
    "SymbolValidationError",
    "graceful_validate",
]
