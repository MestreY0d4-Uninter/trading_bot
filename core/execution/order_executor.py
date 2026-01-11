from decimal import ROUND_DOWN, Decimal, InvalidOperation, getcontext
from typing import Any

from core.execution.execution_validator import (
    DECIMAL_HUNDRED,
    DECIMAL_ONE,
    DECIMAL_ZERO,
    ORDER_TYPE_CONFIGS,
    ExecutionValidator,
)
from core.validators.exceptions import OrderValidationError
from core.validators.trading_validator import TradingValidator
from infrastructure.api.idempotency_handler import IdempotencyHandler
from shared.observability.flow_tracker import track_component
from shared.observability.logger import debug, error, production, warning
from utils.decimal_math import (
    format_price,
    format_quantity,
    safe_decimal_convert,
    safe_float_convert,
)
from utils.validation_utils import validate_symbol

getcontext().prec = 28
getcontext().rounding = ROUND_DOWN

EXECUTION_ERROR_TYPES = {
    "ValidationError": {"severity": "high", "retry": False, "action": "abort"},
    "InsufficientBalance": {"severity": "high", "retry": False, "action": "rebalance"},
    "NetworkError": {"severity": "medium", "retry": True, "action": "retry"},
    "RateLimitError": {"severity": "low", "retry": True, "action": "wait"},
}


class OrderExecutor:
    def __init__(self, client: Any, config: dict, db_handler=None) -> None:
        if not client or not config:
            raise ValueError("Client e config são obrigatórios para OrderExecutor")

        if not hasattr(client, "create_order") or not hasattr(
            client, "_execute_request"
        ):
            raise TypeError(
                "Client deve ser uma instância válida do BinanceClient com métodos obrigatórios"
            )

        required_client_methods = [
            "create_order",
            "cancel_order",
            "get_symbol_info",
            "_execute_request",
        ]
        missing_methods = [
            method for method in required_client_methods if not hasattr(client, method)
        ]
        if missing_methods:
            raise AttributeError(f"Client ausente métodos críticos: {missing_methods}")

        self.client = client
        self.config = config
        self.idempotency = IdempotencyHandler(db_handler=db_handler)
        self.idempotency.start_cleanup_task()
        self.validator = ExecutionValidator(client, config)

        debug("OrderExecutor inicializado", has_db_handler=db_handler is not None)

    def _safe_decimal_from_float(
        self, value: float, context: str = ""
    ) -> Decimal | None:
        if value is None:
            return None
        result = safe_decimal_convert(value)
        return result if result != Decimal("0") or value == 0 else None

    def _safe_float_from_decimal(
        self, value: Decimal, context: str = ""
    ) -> float | None:
        if value is None:
            return None
        return safe_float_convert(value)

    async def _validate_basic_inputs(
        self, symbol: str, side: str, quantity: float, price: float | None = None
    ) -> dict:
        return await self.validator.validate_basic_inputs(symbol, side, quantity, price)

    async def _validate_oco_params(
        self,
        symbol: str,
        quantity: float,
        take_profit_price: float,
        stop_loss_price: float,
    ) -> dict:
        return await self.validator.validate_oco_params(
            symbol, quantity, take_profit_price, stop_loss_price
        )

    async def _verify_oco_created(self, symbol: str, order_list_id: int) -> bool:
        try:
            oco_status = await self.client.ws_get_oco_order(orderListId=order_list_id)

            if not oco_status or not isinstance(oco_status, dict):
                error(
                    "Falha ao verificar OCO - resposta inválida",
                    symbol=symbol,
                    order_list_id=order_list_id,
                )
                return False

            list_order_status = oco_status.get("listOrderStatus")

            if list_order_status != "EXECUTING":
                error(
                    "OCO criada mas não está EXECUTING",
                    symbol=symbol,
                    order_list_id=order_list_id,
                    status=list_order_status,
                )
                return False

            debug(
                "OCO verificada com sucesso",
                symbol=symbol,
                order_list_id=order_list_id,
                status=list_order_status,
            )
            return True

        except Exception as e:
            error(
                "Erro ao verificar OCO",
                symbol=symbol,
                order_list_id=order_list_id,
                error=str(e),
            )
            return False

    async def validate_order_params(
        self, symbol: str, side: str, quantity: float, price: float | None = None
    ) -> dict:
        return await self.validator.validate_order_params(symbol, side, quantity, price)

    def get_cache_stats(self) -> dict:
        return self.validator.get_cache_stats()

    def clear_cache(self) -> int:
        return self.validator.clear_cache()

    def health_check(self) -> dict:
        return self.validator.health_check()

    async def _prepare_order_data(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: float | None = None,
        stop_price: float | None = None,
        **kwargs,
    ) -> dict:
        validation_result = await self._validate_basic_inputs(
            symbol, side, quantity, price
        )
        if not validation_result["valid"]:
            return validation_result

        try:
            from decimal import Decimal

            exchange_filters = await self.client.get_symbol_filters_cached(symbol)

            formatted_qty = Decimal(validation_result["formatted_quantity"])

            TradingValidator.validate_order_preflight_or_raise(
                symbol, formatted_qty, price, exchange_filters
            )
        except OrderValidationError as e:
            error(
                "Pre-flight order validation failed",
                symbol=symbol,
                quantity=quantity,
                price=price,
                error=str(e),
            )
            return {"valid": False, "error": str(e)}
        except Exception as e:
            error(
                "Unexpected error during pre-flight validation",
                symbol=symbol,
                error=str(e),
            )
            return {"valid": False, "error": f"Pre-flight validation error: {str(e)}"}

        order_data = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": validation_result["formatted_quantity"],
        }

        config = ORDER_TYPE_CONFIGS.get(order_type.upper(), {})

        if config.get("requires_price") and price is not None:
            order_data["price"] = validation_result["formatted_price"]

        if config.get("requires_stop_price") and stop_price is not None:
            order_data["stopPrice"] = format_price(
                stop_price, validation_result["price_precision"]
            )

        if kwargs.get("time_in_force") and config.get("supports_tif"):
            order_data["timeInForce"] = kwargs["time_in_force"]

        debug(
            "Order data prepared",
            symbol=symbol,
            side=side,
            type=order_type,
            data=order_data,
        )
        return {"valid": True, "order_data": order_data}

    def _handle_execution_error(
        self,
        error_type: str,
        symbol: str,
        operation: str,
        error_details: Exception,
        **context,
    ) -> dict | None:
        error_config = EXECUTION_ERROR_TYPES.get(
            error_type, {"severity": "high", "retry": False, "action": "abort"}
        )

        error_msg = f"Failed to {operation}"

        if error_config["severity"] == "high":
            error(error_msg, symbol=symbol, error=str(error_details), **context)
        elif error_config["severity"] == "medium":
            warning(error_msg, symbol=symbol, error=str(error_details), **context)
        else:
            debug(error_msg, symbol=symbol, error=str(error_details), **context)

        if error_config["retry"]:
            debug(
                f"Error type {error_type} is retryable", action=error_config["action"]
            )

        return None

    @track_component("order_executor", slow_threshold=5)
    async def create_market_buy_order(
        self, symbol: str, quantity: float, client_order_id: str | None = None
    ) -> dict | None:
        try:
            if client_order_id is None:
                client_order_id = self.idempotency.generate_order_id("buy", symbol)

            prep_result = await self._prepare_order_data(
                symbol, "BUY", "MARKET", quantity
            )
            if not prep_result["valid"]:
                return self._handle_execution_error(
                    "ValidationError",
                    symbol,
                    "create market buy",
                    ValueError(prep_result["error"]),
                    qty=quantity,
                )

            order_data = {
                **prep_result["order_data"],
                "newClientOrderId": client_order_id,
            }

            order = await self.idempotency.execute_with_idempotency(
                operation_id=client_order_id,
                operation=lambda: self.client.create_order(**order_data),
                symbol=symbol,
                operation_type="market_buy",
            )

            production(
                "Market buy order created",
                symbol=symbol,
                qty=prep_result["order_data"]["quantity"],
                client_order_id=client_order_id,
            )
            return order

        except Exception as e:
            return self._handle_execution_error(
                "NetworkError", symbol, "create market buy", e, qty=quantity
            )

    @track_component("order_executor", slow_threshold=5)
    async def market_sell(
        self,
        symbol: str,
        quantity: float,
        emergency_execution: bool = False,
        client_order_id: str | None = None,
    ) -> dict | None:
        try:
            if client_order_id is None:
                client_order_id = self.idempotency.generate_order_id("sell", symbol)

            prep_result = await self._prepare_order_data(
                symbol, "SELL", "MARKET", quantity
            )
            if not prep_result["valid"]:
                return self._handle_execution_error(
                    "ValidationError",
                    symbol,
                    "create market sell",
                    ValueError(prep_result["error"]),
                    qty=quantity,
                )

            order_data = {
                **prep_result["order_data"],
                "newClientOrderId": client_order_id,
            }

            if emergency_execution:
                order_data["emergency_execution"] = True
                production(
                    "🚨 EMERGENCY Market sell order iniciada",
                    symbol=symbol,
                    qty=prep_result["order_data"]["quantity"],
                    client_order_id=client_order_id,
                )

            order = await self.idempotency.execute_with_idempotency(
                operation_id=client_order_id,
                operation=lambda: self.client.create_order(**order_data),
                symbol=symbol,
                operation_type=(
                    "market_sell_emergency" if emergency_execution else "market_sell"
                ),
            )

            if emergency_execution:
                production(
                    "✅ EMERGENCY Market sell order executada",
                    symbol=symbol,
                    qty=prep_result["order_data"]["quantity"],
                    client_order_id=client_order_id,
                )
            else:
                production(
                    "Market sell order created",
                    symbol=symbol,
                    qty=prep_result["order_data"]["quantity"],
                    client_order_id=client_order_id,
                )
            return order

        except Exception as e:
            return self._handle_execution_error(
                "NetworkError", symbol, "create market sell", e, qty=quantity
            )

    @track_component("order_executor", slow_threshold=10)
    async def create_oco_exit(
        self,
        symbol: str,
        quantity: float,
        take_profit_price: float,
        stop_loss_price: float,
        list_client_order_id: str | None = None,
    ) -> dict | None:
        try:
            if list_client_order_id is None:
                list_client_order_id = self.idempotency.generate_oco_id(symbol)

            oco_validation = await self._validate_oco_params(
                symbol, quantity, take_profit_price, stop_loss_price
            )
            if not oco_validation["valid"]:
                return self._handle_execution_error(
                    "ValidationError",
                    symbol,
                    "create OCO",
                    ValueError(oco_validation["error"]),
                )

            order_params = await self._build_oco_params(
                symbol,
                quantity,
                take_profit_price,
                stop_loss_price,
                list_client_order_id,
            )

            order = await self._execute_oco_order(
                symbol, order_params, list_client_order_id
            )
            if not order:
                return None

            return order

        except Exception as e:
            return self._handle_execution_error(
                "NetworkError",
                symbol,
                "create OCO",
                e,
            )

    async def _build_oco_params(
        self,
        symbol: str,
        quantity: float,
        tp_price: float,
        sl_price: float,
        order_id: str,
    ) -> dict:
        price_precision, quantity_precision, _, step_size = (
            await self.validator._get_precision(symbol)
        )

        return {
            "symbol": symbol,
            "side": "SELL",
            "quantity": format_quantity(quantity, quantity_precision, step_size),
            "aboveType": "TAKE_PROFIT_LIMIT",
            "aboveStopPrice": format_price(tp_price, price_precision),
            "abovePrice": format_price(tp_price, price_precision),
            "aboveTimeInForce": "GTC",
            "belowType": "STOP_LOSS",
            "belowStopPrice": format_price(sl_price, price_precision),
            "listClientOrderId": order_id,
        }

    async def _execute_oco_order(
        self, symbol: str, order_params: dict, order_id: str
    ) -> dict | None:
        try:
            order = await self.idempotency.execute_with_idempotency(
                operation_id=order_id,
                operation=lambda: self.client.create_oco_order(**order_params),
                symbol=symbol,
                operation_type="oco_exit",
            )

            if not self._validate_oco_response(order, symbol):
                return None

            production(
                "OCO criada", symbol=symbol, order_list_id=order.get("orderListId")
            )

            order_list_id = order.get("orderListId")
            if order_list_id:
                await self._verify_oco_created(symbol, order_list_id)

            return order

        except AttributeError as ae:
            return self._handle_execution_error(
                "ValidationError", symbol, "create OCO", ae
            )
        except Exception as oe:
            error("CRÍTICO: Falha OCO", symbol=symbol, error=str(oe))
            raise RuntimeError(f"Falha crítica OCO: {str(oe)}") from oe

    def _validate_oco_response(self, order: dict | None, symbol: str) -> bool:
        if not order or not isinstance(order, dict):
            self._handle_execution_error(
                "ValidationError", symbol, "create OCO", ValueError("Resposta inválida")
            )
            return False

        if "orderListId" not in order or "orders" not in order:
            self._handle_execution_error(
                "ValidationError", symbol, "create OCO", ValueError("Campos ausentes")
            )
            return False

        return True

    @track_component("order_executor", slow_threshold=5)
    async def create_stop_loss_order(
        self,
        symbol: str,
        quantity: float,
        stop_price: float,
        client_order_id: str | None = None,
    ) -> dict | None:
        try:
            if client_order_id is None:
                client_order_id = self.idempotency.generate_order_id("sle", symbol)

            price_precision, qty_precision, _, step_size = (
                await self.validator._get_precision(symbol)
            )

            stop_limit_offset = self.config.get("oco_settings", {}).get(
                "stop_limit_offset", Decimal("0.1")
            )

            sl_decimal = self._safe_decimal_from_float(stop_price, "Stop loss")
            offset_decimal = self._safe_decimal_from_float(
                stop_limit_offset, "Stop limit offset"
            )

            if not sl_decimal or not offset_decimal:
                error("CRÍTICO: Conversão Decimal falhou para cálculo stop loss")
                return None

            offset_factor = offset_decimal / DECIMAL_HUNDRED
            stop_limit_decimal = sl_decimal * (DECIMAL_ONE - offset_factor)

            stop_limit_price = self._safe_float_from_decimal(
                stop_limit_decimal, "Stop limit price"
            )

            if not stop_limit_price:
                error("CRÍTICO: Conversão float falhou para stop limit price")
                return None

            formatted_qty = format_quantity(quantity, qty_precision, step_size)
            formatted_stop_price = format_price(stop_price, price_precision)
            formatted_stop_limit_price = format_price(stop_limit_price, price_precision)

            order_params = {
                "symbol": symbol,
                "side": "SELL",
                "type": "STOP_LOSS_LIMIT",
                "quantity": formatted_qty,
                "stopPrice": formatted_stop_price,
                "price": formatted_stop_limit_price,
                "timeInForce": "GTC",
                "newClientOrderId": client_order_id,
            }

            order = await self.idempotency.execute_with_idempotency(
                operation_id=client_order_id,
                operation=lambda: self.client.create_order(**order_params),
                symbol=symbol,
                operation_type="stop_loss_emergency",
            )

            production(
                "Emergency stop-loss created",
                symbol=symbol,
                stop_price=formatted_stop_price,
                stop_limit_price=formatted_stop_limit_price,
                order_id=order.get("orderId"),
                client_order_id=client_order_id,
            )
            return order

        except Exception as e:
            error(
                "Failed to create emergency stop-loss",
                symbol=symbol,
                stop_price=stop_price,
                error=str(e),
            )
            return None

    @track_component("order_executor", slow_threshold=200)
    async def create_take_profit_order(
        self,
        symbol: str,
        quantity: float,
        take_profit_price: float,
        client_order_id: str | None = None,
    ) -> dict | None:
        """Create a take profit LIMIT order"""
        try:
            if client_order_id is None:
                client_order_id = self.idempotency.generate_order_id("tp", symbol)

            price_precision, qty_precision, _, step_size = (
                await self.validator._get_precision(symbol)
            )

            formatted_qty = format_quantity(quantity, qty_precision, step_size)
            formatted_tp_price = format_price(take_profit_price, price_precision)

            order_params = {
                "symbol": symbol,
                "side": "SELL",
                "type": "LIMIT",
                "quantity": formatted_qty,
                "price": formatted_tp_price,
                "timeInForce": "GTC",
                "newClientOrderId": client_order_id,
            }

            order = await self.idempotency.execute_with_idempotency(
                operation_id=client_order_id,
                operation=lambda: self.client.create_order(**order_params),
                symbol=symbol,
                operation_type="take_profit",
            )

            production(
                "Take profit order created",
                symbol=symbol,
                tp_price=formatted_tp_price,
                order_id=order.get("orderId"),
                client_order_id=client_order_id,
            )
            return order

        except Exception as e:
            error(
                "Failed to create take profit order",
                symbol=symbol,
                tp_price=take_profit_price,
                error=str(e),
            )
            return None

    def get_average_fill_price(self, order: dict) -> float:
        if not order or not isinstance(order, dict):
            return 0.0

        if order.get("status") == "FILLED":
            fills = order.get("fills", [])
            if fills and isinstance(fills, list):
                return self._calculate_fills_average(fills, order)
            return self._get_order_price_fallback(order)

        return self._get_order_price_fallback(order)

    def _calculate_fills_average(self, fills: list, order: dict) -> float:
        try:
            total_qty = DECIMAL_ZERO
            total_value = DECIMAL_ZERO
            valid_count = 0

            for fill in fills:
                result = self._process_single_fill(fill)
                if result:
                    total_qty += result["qty"]
                    total_value += result["value"]
                    valid_count += 1

            if total_qty > DECIMAL_ZERO and valid_count > 0:
                avg_price = total_value / total_qty
                return (
                    self._safe_float_from_decimal(avg_price, "average fill price")
                    or 0.0
                )

            warning(
                "Nenhum fill válido encontrado",
                fills_count=len(fills),
                valid_count=valid_count,
            )
            return 0.0

        except Exception as e:
            error(
                "CRÍTICO: Erro ao calcular preço médio",
                order_id=order.get("orderId"),
                error=str(e),
            )
            return 0.0

    def _process_single_fill(self, fill: Any) -> dict | None:
        if not isinstance(fill, dict):
            warning("Fill inválido - não é dict", fill=fill)
            return None

        if "qty" not in fill or "price" not in fill:
            warning("Fill ausente campos obrigatórios", fill=fill)
            return None

        qty_value = fill.get("qty")
        price_value = fill.get("price")

        if qty_value is None or price_value is None:
            return None

        try:
            qty_float = float(qty_value)
            price_float = float(price_value)

            if not self.validator._validate_numeric_input(
                qty_float, "fill_qty"
            ) or not self.validator._validate_numeric_input(price_float, "fill_price"):
                return None

            qty_decimal = self._safe_decimal_from_float(qty_float, "fill qty")
            price_decimal = self._safe_decimal_from_float(price_float, "fill price")

            if not qty_decimal or not price_decimal:
                return None

            return {"qty": qty_decimal, "value": price_decimal * qty_decimal}

        except (ValueError, TypeError, InvalidOperation) as e:
            error("Erro processando fill", fill=fill, error=str(e))
            return None

    def _get_order_price_fallback(self, order: dict) -> float:
        try:
            price_value = order.get("price")
            if price_value is None:
                return 0.0

            price_float = float(price_value)
            if self.validator._validate_numeric_input(
                price_float, "order_price", allow_zero=True
            ):
                return price_float
            return 0.0
        except (ValueError, TypeError):
            return 0.0

    async def cancel_order(self, symbol: str, order_id: int) -> dict | None:
        try:
            if not validate_symbol(symbol):
                return self._handle_execution_error(
                    "ValidationError",
                    symbol,
                    "cancel order",
                    ValueError("Symbol inválido"),
                    order_id=order_id,
                )

            if not isinstance(order_id, int) or order_id <= 0:
                return self._handle_execution_error(
                    "ValidationError",
                    symbol,
                    "cancel order",
                    ValueError("Order ID inválido"),
                    order_id=order_id,
                )

            result = await self.client.cancel_order(symbol.upper(), order_id)
            production("Order cancelada com sucesso", symbol=symbol, order_id=order_id)
            return result

        except Exception as e:
            return self._handle_execution_error(
                "NetworkError", symbol, "cancel order", e, order_id=order_id
            )

    async def cancel_oco_order(self, symbol: str, order_list_id: int) -> dict | None:
        try:
            if not validate_symbol(symbol):
                return self._handle_execution_error(
                    "ValidationError",
                    symbol,
                    "cancel OCO order",
                    ValueError("Symbol inválido"),
                    order_list_id=order_list_id,
                )

            if not isinstance(order_list_id, int) or order_list_id <= 0:
                return self._handle_execution_error(
                    "ValidationError",
                    symbol,
                    "cancel OCO order",
                    ValueError("Order list ID inválido"),
                    order_list_id=order_list_id,
                )

            result = await self.client.cancel_oco(symbol.upper(), order_list_id)
            production(
                "OCO order cancelada com sucesso",
                symbol=symbol,
                order_list_id=order_list_id,
            )
            return result

        except Exception as e:
            return self._handle_execution_error(
                "NetworkError",
                symbol,
                "cancel OCO order",
                e,
                order_list_id=order_list_id,
            )

    async def get_order_status(self, symbol: str, order_id: int) -> dict | None:
        try:
            if not validate_symbol(symbol):
                return self._handle_execution_error(
                    "ValidationError",
                    symbol,
                    "get order status",
                    ValueError("Symbol inválido"),
                    order_id=order_id,
                )

            if not isinstance(order_id, int) or order_id <= 0:
                return self._handle_execution_error(
                    "ValidationError",
                    symbol,
                    "get order status",
                    ValueError("Order ID inválido"),
                    order_id=order_id,
                )

            if hasattr(self.client, "get_order"):
                return await self.client.get_order(
                    symbol=symbol.upper(), orderId=order_id
                )
            else:
                warning("get_order method não disponível no client")
                return None

        except Exception as e:
            return self._handle_execution_error(
                "NetworkError", symbol, "get order status", e, order_id=order_id
            )
