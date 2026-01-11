from decimal import Decimal
from typing import Any

from shared.observability.flow_tracker import track_component
from shared.observability.logger import error
from utils import decimal_math
from utils.decimal_math import (
    InvalidOperation,
    quantize_quantity,
    to_decimal,
)
from utils.validation_utils import (
    is_numeric_valid,
    validate_financial_values,
    validate_pnl_inputs_batch,
)


class PositionCalculator:
    def _safe_decimal_operation(
        self, operation: str, **kwargs
    ) -> dict[str, Any] | None:
        """Wrapper for decimal operations - delegates to decimal_math for core calculations."""
        try:
            if operation == "slippage":
                current_price = to_decimal(kwargs["current_price"])
                avg_price = to_decimal(kwargs["avg_price"])

                if current_price == 0:
                    raise ValueError(
                        "Current price cannot be zero for slippage calculation"
                    )

                slippage_pct = decimal_math.calculate_spread(avg_price, current_price)

                if not is_numeric_valid(float(slippage_pct)):
                    return {"error": "Slippage calculation resultou em NaN/Inf"}

                return {"result": slippage_pct}

            elif operation == "pnl":
                sell_price = kwargs["sell_price"]
                entry_price = kwargs["entry_price"]
                quantity = kwargs["quantity"]

                pnl_usd = decimal_math.calculate_pnl(entry_price, sell_price, quantity)
                pnl_pct = decimal_math.calculate_pnl_percentage(entry_price, sell_price)

                if not is_numeric_valid(float(pnl_usd)):
                    return {"error": "PnL calculation resultou em NaN/Inf"}

                return {"result": {"pnl_usd": pnl_usd, "pnl_pct": pnl_pct}}

            elif operation == "position_value":
                current_price = to_decimal(kwargs["current_price"])
                entry_price = to_decimal(kwargs["entry_price"])
                quantity = to_decimal(kwargs["quantity"])

                position_value = (current_price * quantity).quantize(
                    Decimal("0.00000001")
                )
                pnl = decimal_math.calculate_pnl(entry_price, current_price, quantity)

                if not is_numeric_valid(float(pnl)) or not is_numeric_valid(
                    float(position_value)
                ):
                    return {"error": "Position value calculation resultou em NaN/Inf"}

                return {"result": {"position_value": position_value, "pnl": pnl}}

        except (InvalidOperation, ValueError) as e:
            return {"error": f"Erro na operação Decimal {operation}: {str(e)}"}

        return {"error": f"Operação {operation} não suportada"}

    def _calculate_pnl_core(
        self,
        entry_price: Decimal,
        current_price: Decimal,
        quantity: Decimal,
        symbol: str = "",
    ) -> dict[str, Decimal] | None:
        if not validate_pnl_inputs_batch(entry_price, current_price, quantity, symbol):
            return None

        pnl_result = self._safe_decimal_operation(
            "pnl", sell_price=current_price, entry_price=entry_price, quantity=quantity
        )

        if pnl_result is None:
            if symbol:
                error("CRÍTICO: Resultado de PnL é None", symbol=symbol)
            return None

        if "error" in pnl_result:
            if symbol:
                error(f"CRÍTICO: {pnl_result['error']}", symbol=symbol)
            return None

        return pnl_result["result"]

    @track_component("position_calculator", slow_threshold=100)
    async def calculate_position_parameters(
        self, signal: dict, usdt_balance: Decimal, risk_manager, executor=None
    ) -> dict:
        current_price = signal["current_price"]

        if not validate_financial_values(current_price, "price"):
            return {"error": f"Preço atual inválido: {current_price}"}

        if current_price == 0:
            return {"error": "Preço atual não pode ser zero"}

        if current_price < 0.000001:
            return {"error": f"Preço atual muito pequeno: {current_price}"}

        position_size_usd = await risk_manager.calculate_position_size(usdt_balance)

        if position_size_usd < 10:
            return {"error": f"Posição muito pequena: {position_size_usd}"}

        # Calculate raw quantity (current_price validated > 0 above)
        raw_quantity = position_size_usd / current_price

        # Get symbol-specific step_size from exchange info
        step_size = Decimal("0.00000001")  # Default fallback
        symbol = signal.get("symbol", "")

        if executor and symbol:
            try:
                filters = await executor.client.get_symbol_filters_cached(symbol)
                if filters and "step_size" in filters:
                    step_size = filters["step_size"]

                    # Validate max_qty from LOT_SIZE filter
                    max_qty = filters.get("max_qty")
                    if max_qty and raw_quantity > max_qty:
                        return {
                            "error": f"Quantidade calculada ({raw_quantity}) excede max_qty do símbolo ({max_qty})"
                        }
            except Exception as e:
                error(
                    "Failed to get step_size, using default",
                    symbol=symbol,
                    error=str(e),
                )

        # Quantize to Binance LOT_SIZE precision using symbol-specific step_size
        quantity = quantize_quantity(raw_quantity, step_size)

        return {
            "current_price": current_price,
            "position_size_usd": position_size_usd,
            "quantity": quantity,  # Decimal from quantize_quantity()
        }

    @track_component("position_calculator", slow_threshold=50)
    def calculate_position_pnl(
        self, symbol: str, sell_price: Decimal, entry_price: Decimal, quantity: Decimal
    ) -> dict | None:
        return self._calculate_pnl_core(entry_price, sell_price, quantity, symbol)
