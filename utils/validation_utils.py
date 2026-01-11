import math
import re
from decimal import Decimal
from typing import Any

from shared.constants import FINANCIAL_VALIDATORS
from shared.observability.logger import error

VALID_SQL_IDENTIFIER_REGEX = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def validate_sql_identifier(identifier: str, whitelist: set[str] | None = None) -> bool:
    if not isinstance(identifier, str):
        return False

    if not identifier:
        return False

    if not VALID_SQL_IDENTIFIER_REGEX.match(identifier):
        return False

    if whitelist is not None:
        return identifier in whitelist

    return True


def validate_numeric(
    value: Any,
    min_val: float | Decimal | None = None,
    max_val: float | Decimal | None = None,
) -> bool:
    if not is_numeric_valid(value):
        return False

    if min_val is not None and value < min_val:
        return False

    if max_val is not None and value > max_val:
        return False

    return True


def validate_string(
    value: Any, min_length: int | None = None, max_length: int | None = None
) -> bool:
    if not isinstance(value, str):
        return False

    if min_length is not None and len(value) < min_length:
        return False

    if max_length is not None and len(value) > max_length:
        return False

    return True


def validate_symbol(symbol: Any) -> bool:
    return isinstance(symbol, str) and bool(symbol.strip())


def is_numeric_valid(value: Any) -> bool:
    if not isinstance(value, (int, float, Decimal)):
        return False

    if isinstance(value, Decimal):
        return not (value.is_nan() or value.is_infinite())

    return not (math.isnan(value) or math.isinf(value))


def _is_value_nan(value: int | float | Decimal) -> bool:
    """Check if value is NaN."""
    if isinstance(value, Decimal):
        return value.is_nan()
    return math.isnan(value)


def _is_value_inf(value: int | float | Decimal) -> bool:
    """Check if value is infinite."""
    if isinstance(value, Decimal):
        return value.is_infinite()
    return math.isinf(value)


def _check_nan_inf(
    value: int | float | Decimal, config: dict, validator_type: str, symbol: str
) -> bool:
    """Check NaN and Inf constraints. Returns False if validation fails."""
    if config.get("check_nan", False) and _is_value_nan(value):
        if symbol:
            error(f"Valor {validator_type} é NaN", symbol=symbol, value=value)
        return False

    if config.get("check_inf", False) and _is_value_inf(value):
        if symbol:
            error(f"Valor {validator_type} é infinito", symbol=symbol, value=value)
        return False

    return True


def _check_min_max(
    value: int | float | Decimal, config: dict, validator_type: str, symbol: str
) -> bool:
    """Check min/max constraints. Returns False if validation fails."""
    if "min" in config:
        min_val = config["min"]
        exclude_zero = config.get("exclude_zero", False)
        if (exclude_zero and value <= min_val) or (
            not exclude_zero and value < min_val
        ):
            if symbol:
                op = ">" if exclude_zero else ">="
                error(
                    f"Valor {validator_type} deve ser {op} {min_val}",
                    symbol=symbol,
                    value=value,
                )
            return False

    if "max" in config and value > config["max"]:
        if symbol:
            error(
                f"Valor {validator_type} deve ser <= {config['max']}",
                symbol=symbol,
                value=value,
            )
        return False

    return True


def validate_financial_values(
    value: Any, validator_type: str, symbol: str = ""
) -> bool:
    """Validate a financial value against configured constraints."""
    if validator_type not in FINANCIAL_VALIDATORS:
        return False

    config: dict[str, Any] = FINANCIAL_VALIDATORS[validator_type]

    if not isinstance(value, (int, float, Decimal)):
        if symbol:
            error(
                f"Valor {validator_type} deve ser numérico", symbol=symbol, value=value
            )
        return False

    if not _check_nan_inf(value, config, validator_type, symbol):
        return False

    if not _check_min_max(value, config, validator_type, symbol):
        return False

    return True


def validate_pnl_inputs_batch(
    entry_price: Decimal,
    current_price: Decimal,
    quantity: Decimal,
    symbol: str = "",
) -> bool:
    values_to_validate = [
        (entry_price, "price"),
        (current_price, "price"),
        (quantity, "quantity"),
    ]

    for value, validator_type in values_to_validate:
        if not validate_financial_values(value, validator_type, symbol):
            return False

    return True
