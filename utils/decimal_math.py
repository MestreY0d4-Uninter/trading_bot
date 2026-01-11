"""Decimal math operations for financial calculations.

All monetary calculations MUST use Decimal (NEVER float) for precision.
"""

from decimal import (
    ROUND_DOWN,
    ROUND_HALF_UP,
    ROUND_UP,
    Decimal,
    InvalidOperation,
    getcontext,
)
from typing import Any

getcontext().prec = 28


class DecimalConverter:
    """Conversor singleton com cache para Decimal (Fase 1.6 - +15% performance)"""

    _cache: dict = {}

    @classmethod
    def to_decimal(cls, value: int | float | str | Decimal) -> Decimal:
        """
        Converte para Decimal com cache para valores comuns.

        Cache aplicado para:
        - Valores int/float entre -1000 e 1000 (preços, percentagens comuns)
        - Melhoria de 15% em conversões repetidas

        Args:
            value: Valor a converter (int, float, str, ou Decimal)

        Returns:
            Decimal equivalente
        """
        if isinstance(value, Decimal):
            return value

        # Cache para valores comuns (-1000 a 1000)
        if isinstance(value, (int, float)) and -1000 <= value <= 1000:
            key = (type(value).__name__, value)
            if key not in cls._cache:
                cls._cache[key] = Decimal(str(value))
            return cls._cache[key]

        return Decimal(str(value))

    @classmethod
    def get_cache_stats(cls) -> dict:
        """Retorna estatísticas do cache para debugging"""
        return {"cache_size": len(cls._cache), "cached_values": list(cls._cache.keys())}

    @classmethod
    def clear_cache(cls):
        """Limpa o cache (útil para testes)"""
        cls._cache.clear()


# Atalho conveniente (mantém compatibilidade com código existente)
def to_decimal(value: int | float | str | Decimal) -> Decimal:
    """Convert value to Decimal (cached for common values)."""
    return DecimalConverter.to_decimal(value)


def calculate_pnl(
    entry_price: float | Decimal,
    exit_price: float | Decimal,
    quantity: float | Decimal,
) -> Decimal:
    """Calculate P&L from entry/exit prices and quantity."""
    entry = to_decimal(entry_price)
    exit_price_dec = to_decimal(exit_price)
    qty = to_decimal(quantity)

    pnl = (exit_price_dec - entry) * qty
    return pnl.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def calculate_pnl_percentage(
    entry_price: float | Decimal, exit_price: float | Decimal
) -> Decimal:
    """Calculate P&L percentage from entry/exit prices.

    Raises:
        ValueError: If entry_price is zero (invalid state)
    """
    entry = to_decimal(entry_price)
    exit_price_dec = to_decimal(exit_price)

    if entry == 0:
        raise ValueError("Entry price cannot be zero for P&L percentage calculation")

    pnl_pct = ((exit_price_dec - entry) / entry) * Decimal("100")
    return pnl_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_position_size(
    balance: float | Decimal, risk_percentage: float | Decimal
) -> Decimal:
    """Calculate position size from balance and risk percentage."""
    bal = to_decimal(balance)
    risk = to_decimal(risk_percentage)

    return bal * (risk / Decimal("100"))


def calculate_stop_loss(
    entry_price: float | Decimal, stop_loss_percentage: float | Decimal
) -> Decimal:
    """Calculate stop loss price from entry price and percentage."""
    entry = to_decimal(entry_price)
    sl_pct = to_decimal(stop_loss_percentage)

    return entry * (Decimal("1") - sl_pct / Decimal("100"))


def calculate_take_profit(
    entry_price: float | Decimal,
    take_profit_percentage: float | Decimal,
) -> Decimal:
    """Calculate take profit price from entry price and percentage."""
    entry = to_decimal(entry_price)
    tp_pct = to_decimal(take_profit_percentage)

    return entry * (Decimal("1") + tp_pct / Decimal("100"))


def round_down(value: float | Decimal, decimals: int) -> Decimal:
    """Round value down to specified decimals."""
    val = to_decimal(value)
    quantizer = Decimal(10) ** -decimals
    return val.quantize(quantizer, rounding=ROUND_DOWN)


def round_up(value: float | Decimal, decimals: int) -> Decimal:
    """Round value up to specified decimals."""
    val = to_decimal(value)
    quantizer = Decimal(10) ** -decimals
    return val.quantize(quantizer, rounding=ROUND_UP)


def quantize_quantity(
    quantity: float | Decimal,
    step_size: float | Decimal = Decimal("0.00000001"),
) -> Decimal:
    """
    Quantize quantity to match Binance LOT_SIZE step_size filter.

    Uses floor division to ensure quantity never exceeds available balance.
    Binance requires: (quantity - minQty) % stepSize == 0

    Args:
        quantity: Raw quantity to quantize (float or Decimal)
        step_size: LOT_SIZE stepSize from exchange info (default: 8 decimals)

    Returns:
        Quantized quantity as Decimal (preserves exact precision)

    Example:
        >>> quantize_quantity(1731.9622846531477, Decimal("0.01"))
        Decimal('1731.96')
        >>> quantize_quantity(4784.594389204488, Decimal("1.0"))
        Decimal('4784')
    """
    try:
        qty_decimal = to_decimal(quantity)
        step_decimal = to_decimal(step_size)

        if qty_decimal <= 0 or step_decimal <= 0:
            from shared.observability.logger import error

            error(
                "Invalid quantity or step_size for quantize",
                qty=quantity,
                step=step_size,
            )
            return Decimal("0")

        # Floor division ensures we never exceed available balance
        quantized = (qty_decimal // step_decimal) * step_decimal

        return quantized

    except (InvalidOperation, ValueError, ZeroDivisionError) as e:
        from shared.observability.logger import error

        error("Error quantizing quantity", qty=quantity, step=step_size, error=str(e))
        return Decimal("0")


def calculate_spread(bid_price: float | Decimal, ask_price: float | Decimal) -> Decimal:
    """Calculate spread percentage from bid/ask prices.

    Raises:
        ValueError: If ask_price is zero (invalid state)
    """
    bid = to_decimal(bid_price)
    ask = to_decimal(ask_price)

    if ask == 0:
        raise ValueError("Ask price cannot be zero for spread calculation")

    return ((ask - bid) / ask) * Decimal("100")


def calculate_average_price(prices: list, quantities: list | None = None) -> Decimal:
    """Calculate average price, optionally weighted by quantities.

    Raises:
        ValueError: If prices is empty or total_quantity is zero (invalid state)
    """
    if not prices:
        raise ValueError("Cannot calculate average price with empty prices list")

    prices_decimal = [to_decimal(p) for p in prices]

    if quantities:
        quantities_decimal = [to_decimal(q) for q in quantities]
        total_value = sum(
            p * q for p, q in zip(prices_decimal, quantities_decimal, strict=True)
        )
        total_quantity = sum(quantities_decimal)

        if total_quantity == 0:
            raise ValueError(
                "Cannot calculate weighted average with zero total quantity"
            )

        return Decimal(str(total_value / total_quantity))
    else:
        return sum(prices_decimal) / Decimal(str(len(prices_decimal)))


def safe_float_convert(value: float | Decimal | int | str) -> float:
    """Safely convert value to float."""
    try:
        if isinstance(value, Decimal):
            return float(value)
        elif isinstance(value, (int, float)):
            return float(value)
        else:
            return float(to_decimal(value))
    except (ValueError, TypeError, InvalidOperation) as e:
        from shared.observability.logger import error

        error(
            "❌ Conversão para float falhou",
            value=value,
            type=type(value).__name__,
            error=str(e),
        )
        return 0.0


def safe_decimal_convert(value: Any) -> Decimal:
    """Safely convert value to Decimal."""
    try:
        return to_decimal(value)
    except (ValueError, TypeError, InvalidOperation) as e:
        from shared.observability.logger import error

        error(
            "❌ Conversão para Decimal falhou",
            value=value,
            type=type(value).__name__,
            error=str(e),
        )
        return Decimal("0")


def format_quantity(
    quantity: float | Decimal | int,
    precision: int = 6,
    step_size: float | Decimal | None = None,
) -> str:
    """Format quantity to string with specified precision and optional step_size.

    Args:
        quantity: Quantity value to format
        precision: Decimal precision for formatting (default: 6)
        step_size: Optional exchange step_size for LOT_SIZE compliance

    Returns:
        Formatted quantity string with trailing zeros stripped

    Example:
        >>> format_quantity(1.500, 3)
        '1.5'
        >>> format_quantity(1.0, 2)
        '1'
        >>> format_quantity(0.12345678, 4)
        '0.1234'
    """
    try:
        qty_decimal = to_decimal(quantity)

        if qty_decimal <= 0:
            return "0"

        if step_size is not None:
            step_decimal = to_decimal(step_size)
            if step_decimal <= 0:
                from shared.observability.logger import error

                error("Invalid step_size", step_size=step_size)
                step_decimal = Decimal("0.00000001")

            if qty_decimal < step_decimal:
                return "0"

            qty_decimal = (qty_decimal // step_decimal) * step_decimal

            if qty_decimal <= 0:
                return "0"

        quantizer = Decimal(10) ** -precision
        formatted = qty_decimal.quantize(quantizer, rounding=ROUND_DOWN)

        formatted_str = str(formatted)
        if "." in formatted_str:
            formatted_str = formatted_str.rstrip("0").rstrip(".")

        return formatted_str if formatted_str else "0"

    except Exception as e:
        from shared.observability.logger import error

        error(
            "format_quantity failed",
            quantity=quantity,
            precision=precision,
            step_size=step_size,
            error=str(e),
        )
        return "0"


def format_price(price: float | Decimal | int, precision: int = 8) -> str:
    """Format price to string with specified precision.

    Args:
        price: Price value to format
        precision: Decimal precision for formatting (default: 8)

    Returns:
        Formatted price string with trailing zeros stripped

    Example:
        >>> format_price(50000.12345678, 8)
        '50000.12345678'
        >>> format_price(50000.0, 2)
        '50000'
        >>> format_price(50000.12345678, 2)
        '50000.12'
    """
    try:
        price_decimal = to_decimal(price)

        if price_decimal < 0:
            from shared.observability.logger import error

            error("Invalid price (negative)", price=price)
            return "0"

        if precision == 0:
            return str(int(price_decimal))

        quantizer = Decimal(10) ** -precision
        formatted = price_decimal.quantize(quantizer, rounding=ROUND_DOWN)

        formatted_str = str(formatted)
        if "." in formatted_str:
            formatted_str = formatted_str.rstrip("0").rstrip(".")

        return formatted_str if formatted_str else "0"

    except Exception as e:
        from shared.observability.logger import error

        error(
            "format_price failed",
            price=price,
            precision=precision,
            error=str(e),
        )
        return "0"


def safe_decimal_operation(operation: str, **kwargs) -> dict | None:
    """Perform safe decimal operations with error handling."""
    try:
        if operation == "slippage":
            current_price = to_decimal(kwargs["current_price"])
            avg_price = to_decimal(kwargs["avg_price"])

            if current_price == 0:
                raise ValueError(
                    "Current price cannot be zero for slippage calculation"
                )

            slippage = calculate_spread(avg_price, current_price)
            slippage_float = safe_float_convert(slippage)

            return {"result": slippage_float}

        elif operation == "pnl":
            sell_price = to_decimal(kwargs["sell_price"])
            entry_price = to_decimal(kwargs["entry_price"])
            quantity = to_decimal(kwargs["quantity"])

            pnl_usd = calculate_pnl(entry_price, sell_price, quantity)
            pnl_pct = calculate_pnl_percentage(entry_price, sell_price)

            return {
                "result": {
                    "pnl_usd": safe_float_convert(pnl_usd),
                    "pnl_pct": safe_float_convert(pnl_pct),
                }
            }

        elif operation == "position_value":
            current_price = to_decimal(kwargs["current_price"])
            entry_price = to_decimal(kwargs["entry_price"])
            quantity = to_decimal(kwargs["quantity"])

            position_value = current_price * quantity
            pnl = calculate_pnl(entry_price, current_price, quantity)

            return {
                "result": {
                    "position_value": safe_float_convert(position_value),
                    "pnl": safe_float_convert(pnl),
                }
            }

        return None

    except Exception as e:
        from shared.observability.logger import error

        error(
            "❌ safe_decimal_operation falhou",
            operation=operation,
            kwargs=kwargs,
            error=str(e),
        )
        return {"error": f"Operação {operation} falhou: {str(e)}"}


def decimal_to_json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    return value


def dict_decimals_to_str(data: dict) -> dict:
    return {k: decimal_to_json_safe(v) for k, v in data.items()}


def to_float(value: int | float | str | Decimal) -> float:
    return safe_float_convert(value)
