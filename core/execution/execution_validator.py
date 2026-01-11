import math
from decimal import Decimal
from typing import Any

from shared.observability.logger import debug, error, warning
from utils.decimal_math import (
    ROUND_DOWN,
    format_price,
    format_quantity,
    getcontext,
    safe_decimal_convert,
    safe_float_convert,
)
from utils.validation_utils import is_numeric_valid, validate_symbol

getcontext().prec = 28
getcontext().rounding = ROUND_DOWN

DECIMAL_ZERO = Decimal("0")
DECIMAL_ONE = Decimal("1")
DECIMAL_HUNDRED = Decimal("100")

ORDER_TYPE_CONFIGS = {
    "MARKET": {
        "requires_price": False,
        "supports_tif": False,
        "supports_iceberg": False,
    },
    "LIMIT": {"requires_price": True, "supports_tif": True, "supports_iceberg": True},
    "STOP_LOSS": {
        "requires_price": True,
        "requires_stop_price": True,
        "supports_tif": False,
    },
    "STOP_LOSS_LIMIT": {
        "requires_price": True,
        "requires_stop_price": True,
        "supports_tif": True,
    },
    "TAKE_PROFIT": {
        "requires_price": True,
        "requires_stop_price": True,
        "supports_tif": False,
    },
    "TAKE_PROFIT_LIMIT": {
        "requires_price": True,
        "requires_stop_price": True,
        "supports_tif": True,
    },
}


class ExecutionValidator:
    def __init__(self, client: Any, config: dict) -> None:
        if not client or not config:
            raise ValueError("Client e config são obrigatórios para ExecutionValidator")

        if not hasattr(client, "get_symbol_info"):
            raise TypeError(
                "Client deve ter método get_symbol_info para ExecutionValidator"
            )

        self.client = client
        self.config = config

        debug("ExecutionValidator inicializado")

    def _validate_numeric_input(
        self, value, name: str, allow_zero: bool = False, allow_negative: bool = False
    ) -> bool:
        if value is None:
            error(f"{name} é None")
            return False

        if not isinstance(value, (int, float, Decimal)):
            error(f"{name} deve ser numérico", type=type(value), value=value)
            return False

        if isinstance(value, float) and not is_numeric_valid(value):
            error(f"{name} é NaN ou infinito", value=value)
            return False

        if isinstance(value, Decimal) and (value.is_nan() or value.is_infinite()):
            error(f"{name} é NaN ou infinito (Decimal)", value=value)
            return False

        if not allow_zero and value == 0:
            error(f"{name} não pode ser zero", value=value)
            return False

        if not allow_negative and value < 0:
            error(f"{name} não pode ser negativo", value=value)
            return False

        return True

    def _safe_decimal_from_float(
        self, value: float, context: str = ""
    ) -> Decimal | None:
        if value is None or not is_numeric_valid(value):
            if context:
                warning(
                    f"Valor inválido para conversão Decimal: {value}", context=context
                )
            return None
        result = safe_decimal_convert(value)
        return result if result != Decimal("0") or value == 0 else None

    def _safe_float_from_decimal(
        self, value: Decimal, context: str = ""
    ) -> float | None:
        if value is None:
            return None
        result = safe_float_convert(value)
        if not is_numeric_valid(result):
            if context:
                warning(
                    f"Conversão Decimal->float resultou em valor inválido: {result}",
                    context=context,
                )
            return None
        return result

    async def _get_precision(self, symbol: str) -> tuple[int, int, float, float]:
        if not validate_symbol(symbol):
            error("Symbol inválido para _get_precision", symbol=symbol)
            return 8, 8, 10.0, 0.00001

        try:
            info = await self.client.get_symbol_info(symbol)

            if not info:
                error("Symbol info not found", symbol=symbol)
                return 8, 8, 10.0, 0.00001

            price_precision = 8
            quantity_precision = 8
            min_notional = 10.0
            step_size = 0.00001

            for f in info.get("filters", []):
                try:
                    if f["filterType"] == "PRICE_FILTER":
                        tick_size = float(f["tickSize"])
                        if self._validate_numeric_input(tick_size, "tick_size"):
                            price_precision = max(0, -int(math.log10(tick_size)))
                    elif f["filterType"] == "LOT_SIZE":
                        step_size_val = float(f["stepSize"])
                        if self._validate_numeric_input(step_size_val, "step_size"):
                            step_size = step_size_val
                            quantity_precision = max(0, -int(math.log10(step_size_val)))
                    elif f["filterType"] == "MIN_NOTIONAL":
                        min_notional_val = float(f["minNotional"])
                        if self._validate_numeric_input(
                            min_notional_val, "min_notional"
                        ):
                            min_notional = min_notional_val
                except (ValueError, KeyError, TypeError) as fe:
                    warning(
                        "Erro ao processar filter",
                        symbol=symbol,
                        filter_data=f,
                        error=str(fe),
                    )
                    continue

            debug(
                "Symbol precision obtained",
                symbol=symbol,
                price_precision=price_precision,
                quantity_precision=quantity_precision,
                min_notional=min_notional,
                step_size=step_size,
            )

            return price_precision, quantity_precision, min_notional, step_size

        except Exception as e:
            error("Error getting symbol precision", symbol=symbol, error=str(e))
            return 8, 8, 10.0, 0.00001

    async def validate_basic_inputs(
        self, symbol: str, side: str, quantity: float, price: float | None = None
    ) -> dict:
        if not self._validate_numeric_input(quantity, "quantity"):
            return {"valid": False, "error": "Quantidade inválida"}

        if not validate_symbol(symbol):
            return {"valid": False, "error": "Symbol inválido"}

        if not side or side.upper() not in ["BUY", "SELL"]:
            return {"valid": False, "error": "Side deve ser BUY ou SELL"}

        price_precision, quantity_precision, min_notional, step_size = (
            await self._get_precision(symbol)
        )
        formatted_qty = format_quantity(quantity, quantity_precision, step_size)

        if formatted_qty == "0":
            return {
                "valid": False,
                "error": "Quantidade muito pequena para este símbolo",
            }

        result = {
            "valid": True,
            "formatted_quantity": formatted_qty,
            "price_precision": price_precision,
            "quantity_precision": quantity_precision,
            "min_notional": min_notional,
            "step_size": step_size,
        }

        if price is not None:
            if not self._validate_numeric_input(price, "price"):
                return {"valid": False, "error": "Price inválido"}
            formatted_price = format_price(price, price_precision)
            if formatted_price == "0":
                return {
                    "valid": False,
                    "error": "Price muito pequeno para este símbolo",
                }
            result["formatted_price"] = formatted_price

        return result

    async def validate_oco_params(
        self,
        symbol: str,
        quantity: float,
        take_profit_price: float,
        stop_loss_price: float,
    ) -> dict:
        if not validate_symbol(symbol):
            return {"valid": False, "error": "Symbol inválido para OCO"}

        params = [
            ("quantity", quantity),
            ("take_profit_price", take_profit_price),
            ("stop_loss_price", stop_loss_price),
        ]

        for name, value in params:
            if not self._validate_numeric_input(value, name):
                return {"valid": False, "error": f"Parâmetro OCO {name} inválido"}

        tp_decimal = self._safe_decimal_from_float(
            take_profit_price, "OCO TP validation"
        )
        sl_decimal = self._safe_decimal_from_float(stop_loss_price, "OCO SL validation")

        if not tp_decimal or not sl_decimal:
            return {"valid": False, "error": "Conversão Decimal falhou para preços OCO"}

        if tp_decimal <= sl_decimal:
            return {"valid": False, "error": "Take profit deve ser maior que stop loss"}

        try:
            ticker = await self.client.get_ticker(symbol)
            current_price_float = float(ticker.get("lastPrice", 0))

            if not self._validate_numeric_input(current_price_float, "current_price"):
                error(
                    "CRÍTICO: OCO validation failed - current price invalid",
                    symbol=symbol,
                    ticker_response=ticker,
                    current_price=current_price_float,
                    tp_price=take_profit_price,
                    sl_price=stop_loss_price,
                )
                return {"valid": False, "error": "Preço atual inválido"}

            current_decimal = self._safe_decimal_from_float(
                current_price_float, "OCO current price"
            )
            if not current_decimal:
                error(
                    "CRÍTICO: OCO validation failed - Decimal conversion",
                    symbol=symbol,
                    current_price_float=current_price_float,
                )
                return {
                    "valid": False,
                    "error": "Conversão Decimal falhou para preço atual",
                }

            if not (sl_decimal < current_decimal < tp_decimal):
                error(
                    "CRÍTICO: OCO validation failed - price relationship invalid",
                    symbol=symbol,
                    current_price=float(current_decimal),
                    sl_price=float(sl_decimal),
                    tp_price=float(tp_decimal),
                    expected="sl < current < tp",
                )
                return {"valid": False, "error": "Relação de preços OCO inválida"}

        except Exception as e:
            error(
                "CRÍTICO: OCO validation failed - exception",
                symbol=symbol,
                error=str(e),
                exc_type=type(e).__name__,
            )
            return {"valid": False, "error": f"Falha ao obter preço atual: {str(e)}"}

        return {"valid": True}

    async def validate_order_params(
        self, symbol: str, side: str, quantity: float, price: float | None = None
    ) -> dict:
        try:
            validation_result = await self.validate_basic_inputs(
                symbol, side, quantity, price
            )
            if not validation_result["valid"]:
                return validation_result

            notional_error = self._check_notional(price, quantity, validation_result)
            if notional_error:
                return notional_error

            if validation_result["formatted_quantity"] == "0":
                return {
                    "valid": False,
                    "error": "Quantity muito pequena após formatação",
                }

            result = self._build_order_result(symbol, side, validation_result)
            self._add_price_info(result, price, validation_result)

            return result

        except Exception as e:
            return {"valid": False, "error": f"Erro na validação: {str(e)}"}

    def _check_notional(
        self, price: float | None, quantity: float, validation_result: dict
    ) -> dict | None:
        if price is None:
            return None

        price_decimal = self._safe_decimal_from_float(price, "price")
        qty_decimal = self._safe_decimal_from_float(quantity, "quantity")
        min_notional = self._safe_decimal_from_float(
            validation_result["min_notional"], "min_notional"
        )

        if price_decimal and qty_decimal and min_notional:
            notional = price_decimal * qty_decimal
            if notional < min_notional:
                return {
                    "valid": False,
                    "error": f"Notional {float(notional):.2f} < min {float(min_notional):.2f}",
                }
        return None

    def _build_order_result(
        self, symbol: str, side: str, validation_result: dict
    ) -> dict:
        return {
            "valid": True,
            "symbol": symbol.upper(),
            "side": side.upper(),
            "quantity": validation_result["formatted_quantity"],
            "price_precision": validation_result["price_precision"],
            "quantity_precision": validation_result["quantity_precision"],
            "min_notional": validation_result["min_notional"],
            "step_size": validation_result["step_size"],
        }

    def _add_price_info(
        self, result: dict, price: float | None, validation_result: dict
    ) -> None:
        if price is None or "formatted_price" not in validation_result:
            return

        if validation_result["formatted_price"] == "0":
            result["valid"] = False
            result["error"] = "Price inválido após formatação"
            return

        result["formatted_price"] = validation_result["formatted_price"]
        try:
            price_val = float(validation_result["formatted_price"])
            qty_val = float(validation_result["formatted_quantity"])
            result["notional_value"] = price_val * qty_val
        except (ValueError, TypeError):
            result["notional_value"] = 0.0

    def get_cache_stats(self) -> dict:
        return {
            "hits": 0,
            "misses": 0,
            "maxsize": 0,
            "currsize": 0,
            "hit_rate": 0.0,
        }

    def clear_cache(self) -> int:
        return 0

    def health_check(self) -> dict:
        try:
            status = "healthy"
            issues = []

            if not self.client:
                status = "critical"
                issues.append("Client não disponível")

            if not self.config:
                status = "critical"
                issues.append("Config não disponível")

            cache_stats = self.get_cache_stats()
            if (
                cache_stats["hit_rate"] < Decimal("0.5")
                and cache_stats["hits"] + cache_stats["misses"] > 100
            ):
                status = "warning" if status == "healthy" else status
                issues.append(f"Cache hit rate baixa: {cache_stats['hit_rate']:.2f}")

            required_methods = ["get_symbol_info"]
            missing_methods = []
            for method in required_methods:
                if not hasattr(self.client, method):
                    missing_methods.append(method)

            if missing_methods:
                status = "critical"
                issues.append(f'Métodos client ausentes: {", ".join(missing_methods)}')

            return {
                "status": status,
                "issues": issues,
                "cache_stats": cache_stats,
                "client_available": bool(self.client),
                "config_available": bool(self.config),
                "missing_methods": missing_methods,
            }

        except Exception as e:
            error("ExecutionValidator health check error", error=str(e))
            return {"status": "error", "error": str(e)}
