import math
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from core.models.order import OCOOrderParams, OrderParams
from core.models.signal import SignalParams, StopLossTakeProfitParams
from core.validators.exceptions import (
    OrderValidationError,
    SymbolValidationError,
    TradingValidationError,
    format_pydantic_errors,
)
from shared.constants import (
    MAX_POSITION_SIZE_PCT,
    MAX_SLIPPAGE,
    MAX_SPREAD_PCT,
    MIN_ENTRY_SCORE,
    MIN_POSITION_SIZE,
    MIN_PROFIT_TARGET,
    MIN_VOLATILITY_THRESHOLD,
    MIN_VOLUME_SPIKE,
    MIN_VOLUME_USD,
)
from shared.observability.flow_tracker import track_component
from shared.observability.logger import error
from utils.decimal_math import format_price as dm_format_price
from utils.decimal_math import format_quantity as dm_format_quantity
from utils.decimal_math import to_decimal
from utils.validation_utils import is_numeric_valid, validate_symbol


class TradingValidator:
    _symbol_whitelist: list[str] | None = None

    @classmethod
    def set_symbol_whitelist(cls, symbols: list[str]) -> None:
        cls._symbol_whitelist = [s.upper() for s in symbols] if symbols else None

    @classmethod
    def get_symbol_whitelist(cls) -> list[str] | None:
        return cls._symbol_whitelist

    @staticmethod
    @track_component("trading_validator", slow_threshold=1)
    def validate_spread(spread_pct: Decimal, max_spread: Decimal | None = None) -> bool:
        max_spread = max_spread or MAX_SPREAD_PCT
        return is_numeric_valid(spread_pct) and 0 <= spread_pct <= max_spread

    @staticmethod
    @track_component("trading_validator", slow_threshold=1)
    def validate_volume(volume_usd: Decimal, min_volume: int | None = None) -> bool:
        min_vol: Decimal = to_decimal(min_volume or MIN_VOLUME_USD)
        return is_numeric_valid(volume_usd) and volume_usd >= min_vol

    @staticmethod
    @track_component("trading_validator", slow_threshold=1)
    def validate_entry_score(score: Decimal, min_score: int | None = None) -> bool:
        min_score_decimal: Decimal = to_decimal(min_score or MIN_ENTRY_SCORE)
        return is_numeric_valid(score) and score >= min_score_decimal

    @staticmethod
    @track_component("trading_validator", slow_threshold=1)
    def validate_position_size(
        position_size: Decimal, balance: Decimal
    ) -> tuple[bool, str | None]:
        position_decimal = to_decimal(position_size)
        balance_decimal = to_decimal(balance)

        if position_decimal <= 0:
            return False, "Invalid position size"
        if balance_decimal <= 0:
            return False, "Invalid balance"
        if position_decimal < MIN_POSITION_SIZE:
            return False, f"Position size below minimum {MIN_POSITION_SIZE}"
        max_size = balance_decimal * MAX_POSITION_SIZE_PCT
        if position_decimal > max_size:
            return (
                False,
                f"Position size exceeds {MAX_POSITION_SIZE_PCT*100}% of balance",
            )
        return True, None

    @staticmethod
    @track_component("trading_validator", slow_threshold=1)
    def validate_price(price: Any) -> bool:
        return is_numeric_valid(price) and price > 0

    @staticmethod
    def validate_quantity(quantity: Any) -> bool:
        return is_numeric_valid(quantity) and quantity > 0

    @staticmethod
    def validate_signal(signal: dict[str, Any]) -> tuple[bool, str | None]:
        if not isinstance(signal, dict):
            return False, "Signal must be a dictionary"
        try:
            SignalParams(**signal)
            return True, None
        except ValidationError as e:
            return False, format_pydantic_errors(e)
        except Exception as e:
            return False, f"Signal validation failed: {str(e)}"

    @staticmethod
    def validate_stop_loss_take_profit(
        entry_price: Decimal, stop_loss: Decimal, take_profit: Decimal
    ) -> tuple[bool, str | None]:
        try:
            StopLossTakeProfitParams(
                entry_price=entry_price, stop_loss=stop_loss, take_profit=take_profit
            )
            return True, None
        except ValidationError as e:
            return False, format_pydantic_errors(e)
        except Exception as e:
            return False, f"SL/TP validation failed: {str(e)}"

    @staticmethod
    def validate_profit_target(
        current_price: Decimal,
        take_profit_price: Decimal,
        min_profit_pct: Decimal | None = None,
    ) -> bool:
        min_profit_pct = min_profit_pct or MIN_PROFIT_TARGET

        if not TradingValidator._is_valid_price(current_price):
            return False

        if not TradingValidator._is_valid_price(take_profit_price):
            return False

        expected_profit_pct = (
            (to_decimal(take_profit_price) - to_decimal(current_price))
            / to_decimal(current_price)
        ) * Decimal("100")

        return expected_profit_pct >= min_profit_pct

    @staticmethod
    def _is_valid_price(value: int | float | Decimal) -> bool:
        if not isinstance(value, (int, float, Decimal)) or value <= 0:
            return False

        if isinstance(value, Decimal) and (value.is_nan() or value.is_infinite()):
            return False

        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return False

        return True

    @staticmethod
    def validate_slippage(
        slippage_pct: Decimal, max_slippage_pct: Decimal | None = None
    ) -> bool:
        max_slippage_pct = max_slippage_pct or MAX_SLIPPAGE
        return is_numeric_valid(slippage_pct) and 0 <= slippage_pct <= max_slippage_pct

    @staticmethod
    def validate_position_inputs(
        symbol: str, signal: dict, usdt_balance: Decimal
    ) -> bool:
        if not validate_symbol(symbol):
            error(
                "CRÍTICO: Symbol validation failed",
                symbol=symbol,
                reason="Symbol must be a non-empty string",
            )
            return False
        if (
            TradingValidator._symbol_whitelist
            and symbol.upper() not in TradingValidator._symbol_whitelist
        ):
            error(
                "CRÍTICO: Symbol validation failed",
                symbol=symbol,
                reason=f"Symbol {symbol} not in whitelist",
            )
            return False
        if not signal or not isinstance(signal, dict):
            error("CRÍTICO: Signal deve ser um dict válido", signal=signal)
            return False
        required_fields = ["current_price", "entry_score"]
        missing = [f for f in required_fields if f not in signal]
        if missing:
            error("CRÍTICO: Signal ausente campos obrigatórios", missing=missing)
            return False
        if not is_numeric_valid(usdt_balance) or usdt_balance <= 0:
            error("CRÍTICO: USDT balance inválido", balance=usdt_balance)
            return False
        return True

    @staticmethod
    def validate_close_inputs(symbol: str, reason: str, current_price: Decimal) -> bool:
        if not validate_symbol(symbol):
            error(
                "CRÍTICO: Symbol validation failed",
                symbol=symbol,
                reason="Symbol must be a non-empty string",
            )
            return False
        if (
            TradingValidator._symbol_whitelist
            and symbol.upper() not in TradingValidator._symbol_whitelist
        ):
            error(
                "CRÍTICO: Symbol validation failed",
                symbol=symbol,
                reason=f"Symbol {symbol} not in whitelist",
            )
            return False
        if not reason or not isinstance(reason, str):
            error("CRÍTICO: Reason deve ser string válida", reason=reason)
            return False
        if not is_numeric_valid(current_price) or current_price <= 0:
            error("CRÍTICO: Current price inválido", symbol=symbol, price=current_price)
            return False
        return True

    @staticmethod
    def validate_volume_spike(
        current_volume: Decimal,
        avg_volume: Decimal,
        min_spike: Decimal | None = None,
    ) -> bool:
        min_spike = min_spike or MIN_VOLUME_SPIKE
        if (
            not isinstance(current_volume, (int, float, Decimal))
            or current_volume <= 0
            or not is_numeric_valid(current_volume)
        ):
            return False
        if (
            not isinstance(avg_volume, (int, float, Decimal))
            or avg_volume <= 0
            or not is_numeric_valid(avg_volume)
        ):
            return False
        return (current_volume / avg_volume) >= min_spike

    @staticmethod
    def validate_volatility_threshold(
        price_range_pct: Decimal, min_volatility: Decimal | None = None
    ) -> bool:
        min_volatility = min_volatility or MIN_VOLATILITY_THRESHOLD
        return is_numeric_valid(price_range_pct) and price_range_pct >= min_volatility

    @staticmethod
    def validate_momentum_direction(
        current_price: Decimal, prev_price: Decimal, signal_direction: str
    ) -> bool:
        current_decimal = to_decimal(current_price)
        prev_decimal = to_decimal(prev_price)

        if not is_numeric_valid(current_decimal) or current_decimal <= 0:
            return False
        if not is_numeric_valid(prev_decimal) or prev_decimal <= 0:
            return False
        if signal_direction not in ["BUY", "SELL"]:
            return False
        price_change = (current_decimal - prev_decimal) / prev_decimal
        if signal_direction == "BUY":
            return price_change > 0
        return price_change < 0

    @staticmethod
    def validate_symbol_or_raise(
        symbol: str, whitelist: list[str] | None = None
    ) -> None:
        if not validate_symbol(symbol):
            raise SymbolValidationError("Symbol must be a non-empty string")
        if whitelist is None:
            whitelist = TradingValidator._symbol_whitelist
        if whitelist and symbol.upper() not in whitelist:
            raise SymbolValidationError(f"Symbol {symbol} not in whitelist")

    @staticmethod
    @track_component("trading_validator", slow_threshold=1)
    def validate_position_size_or_raise(
        position_size: Decimal, balance: Decimal
    ) -> None:
        is_valid, error_msg = TradingValidator.validate_position_size(
            position_size, balance
        )
        if not is_valid:
            raise TradingValidationError(error_msg or "Position size validation failed")

    @staticmethod
    def validate_signal_or_raise(signal: dict[str, Any]) -> None:
        is_valid, error_msg = TradingValidator.validate_signal(signal)
        if not is_valid:
            raise TradingValidationError(error_msg or "Signal validation failed")

    @staticmethod
    def validate_stop_loss_take_profit_or_raise(
        entry_price: Decimal, stop_loss: Decimal, take_profit: Decimal
    ) -> None:
        is_valid, error_msg = TradingValidator.validate_stop_loss_take_profit(
            entry_price, stop_loss, take_profit
        )
        if not is_valid:
            raise TradingValidationError(error_msg or "SL/TP validation failed")

    @staticmethod
    @track_component("trading_validator", slow_threshold=1)
    def validate_order_params(
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None = None,
    ) -> tuple[bool, str | None]:
        try:
            OrderParams(
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=price,
            )
            return True, None
        except ValidationError as e:
            return False, format_pydantic_errors(e)

    @staticmethod
    @track_component("trading_validator", slow_threshold=1)
    def validate_oco_order(
        symbol: str,
        quantity: Decimal,
        price: Decimal,
        stop_price: Decimal,
        stop_limit_price: Decimal,
    ) -> tuple[bool, str | None]:
        try:
            OCOOrderParams(
                symbol=symbol,
                quantity=quantity,
                price=price,
                stop_price=stop_price,
                stop_limit_price=stop_limit_price,
            )
            return True, None
        except ValidationError as e:
            return False, format_pydantic_errors(e)

    @staticmethod
    @track_component("trading_validator", slow_threshold=1)
    def validate_order_response(response: dict[str, Any]) -> tuple[bool, str | None]:
        if not isinstance(response, dict):
            return False, "Response must be a dictionary"
        if "orderId" not in response:
            return False, "Missing orderId in response"
        if "status" not in response:
            return False, "Missing status in response"
        if response.get("status") == "REJECTED":
            return False, f"Order rejected: {response.get('rejectReason', 'Unknown')}"
        return True, None

    @staticmethod
    def calculate_order_value(
        quantity: float | Decimal, price: float | Decimal
    ) -> Decimal:
        return to_decimal(quantity) * to_decimal(price)

    @staticmethod
    def validate_min_notional(
        quantity: float | Decimal,
        price: float | Decimal,
        min_notional: Decimal = Decimal("10.0"),
    ) -> tuple[bool, str | None]:
        order_value = TradingValidator.calculate_order_value(quantity, price)
        min_notional_decimal = to_decimal(min_notional)
        if order_value < min_notional_decimal:
            return False, f"Order value {order_value} below minimum {min_notional}"
        return True, None

    @staticmethod
    @track_component("trading_validator", slow_threshold=1)
    def validate_order_params_or_raise(
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None = None,
    ) -> None:
        is_valid, error_msg = TradingValidator.validate_order_params(
            symbol, side, order_type, quantity, price
        )
        if not is_valid:
            raise OrderValidationError(error_msg or "Order params validation failed")

    @staticmethod
    @track_component("trading_validator", slow_threshold=1)
    def validate_oco_order_or_raise(
        symbol: str,
        quantity: Decimal,
        price: Decimal,
        stop_price: Decimal,
        stop_limit_price: Decimal,
    ) -> None:
        is_valid, error_msg = TradingValidator.validate_oco_order(
            symbol, quantity, price, stop_price, stop_limit_price
        )
        if not is_valid:
            raise OrderValidationError(error_msg or "OCO order validation failed")

    @staticmethod
    @track_component("trading_validator", slow_threshold=1)
    def validate_order_response_or_raise(response: dict[str, Any]) -> None:
        is_valid, error_msg = TradingValidator.validate_order_response(response)
        if not is_valid:
            raise OrderValidationError(error_msg or "Order response validation failed")

    @staticmethod
    def validate_min_notional_or_raise(
        quantity: Decimal, price: Decimal, min_notional: Decimal = Decimal("10.0")
    ) -> None:
        is_valid, error_msg = TradingValidator.validate_min_notional(
            quantity, price, min_notional
        )
        if not is_valid:
            raise OrderValidationError(error_msg or "Min notional validation failed")

    @staticmethod
    @track_component("trading_validator", slow_threshold=1)
    def validate_order_preflight_or_raise(
        symbol: str,
        quantity: float | Decimal,
        price: float | Decimal | None,
        exchange_filters: dict[str, Decimal] | None,
    ) -> None:
        if not exchange_filters:
            raise OrderValidationError(
                f"Exchange filters not available for {symbol} - cannot validate order"
            )

        qty_decimal = to_decimal(quantity)
        TradingValidator._validate_quantity(symbol, qty_decimal, exchange_filters)

        order_value = Decimal("0")
        if price is not None:
            price_decimal = to_decimal(price)
            TradingValidator._validate_price(symbol, price_decimal, exchange_filters)
            order_value = qty_decimal * price_decimal

        TradingValidator._validate_notional(
            symbol, price, order_value, exchange_filters
        )

    @staticmethod
    def _validate_quantity(
        symbol: str, qty: Decimal, filters: dict[str, Decimal]
    ) -> None:
        if qty <= 0:
            raise OrderValidationError(f"Quantity must be positive, got {qty}")

        min_qty = filters.get("min_qty", Decimal("0"))
        max_qty = filters.get("max_qty", Decimal("0"))
        step_size = filters.get("step_size", Decimal("0.00001"))

        if min_qty > 0 and qty < min_qty:
            raise OrderValidationError(
                f"Quantity {qty} below minimum {min_qty} for {symbol}"
            )

        if max_qty > 0 and qty > max_qty:
            raise OrderValidationError(
                f"Quantity {qty} exceeds maximum {max_qty} for {symbol}"
            )

        if step_size > 0 and qty % step_size != 0:
            raise OrderValidationError(
                f"Quantity {qty} not multiple of step_size {step_size} for {symbol}"
            )

    @staticmethod
    def _validate_price(
        symbol: str, price: Decimal, filters: dict[str, Decimal]
    ) -> None:
        if price <= 0:
            raise OrderValidationError(f"Price must be positive, got {price}")

        min_price = filters.get("min_price", Decimal("0"))
        max_price = filters.get("max_price", Decimal("0"))
        tick_size = filters.get("tick_size", Decimal("0.01"))

        if min_price > 0 and price < min_price:
            raise OrderValidationError(
                f"Price {price} below minimum {min_price} for {symbol}"
            )

        if max_price > 0 and price > max_price:
            raise OrderValidationError(
                f"Price {price} exceeds maximum {max_price} for {symbol}"
            )

        if tick_size > 0 and price % tick_size != 0:
            raise OrderValidationError(
                f"Price {price} not multiple of tick_size {tick_size} for {symbol}"
            )

    @staticmethod
    def _validate_notional(
        symbol: str,
        price: float | Decimal | None,
        order_value: Decimal,
        filters: dict[str, Decimal],
    ) -> None:
        min_notional = filters.get("min_notional", Decimal("10"))
        if price is not None and order_value > 0 and order_value < min_notional:
            raise OrderValidationError(
                f"Order value {order_value} USDT below minimum notional {min_notional} USDT for {symbol}"
            )

    @staticmethod
    def format_quantity(quantity: float, precision: int) -> str:
        """Format quantity with precision (delegates to decimal_math)."""
        return dm_format_quantity(quantity, precision)

    @staticmethod
    def format_price(price: float, precision: int) -> str:
        """Format price with precision (delegates to decimal_math)."""
        return dm_format_price(price, precision)
