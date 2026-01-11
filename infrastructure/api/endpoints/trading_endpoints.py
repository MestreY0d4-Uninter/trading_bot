import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any

from infrastructure.api.api_client import ConnectionPoolStatus
from shared.observability.flow_tracker import track_component
from shared.observability.logger import debug, error, production, warning
from utils.decimal_math import safe_decimal_convert
from utils.validation_utils import is_numeric_valid


class TradingEndpointsMixin:
    def _parse_asset_balance(
        self, asset_data: dict
    ) -> tuple[str | None, Decimal | None, Decimal | None, str | None]:
        """Parse a single asset balance entry. Returns (asset, free, locked, error)."""
        if not isinstance(asset_data, dict):
            return None, None, None, f"Dado não é dict: {asset_data}"

        asset_name = asset_data.get("asset")
        if not asset_name or not isinstance(asset_name, str):
            return None, None, None, f"Asset name inválido: {asset_data}"

        try:
            free_amount = safe_decimal_convert(asset_data.get("free", "0"))
            locked_amount = safe_decimal_convert(asset_data.get("locked", "0"))
        except (ValueError, TypeError) as e:
            return (
                asset_name,
                None,
                None,
                f"{asset_name}: conversão Decimal falhou - {e}",
            )

        if free_amount is None or locked_amount is None:
            return asset_name, None, None, f"{asset_name}: Conversão retornou None"

        if free_amount < 0 or locked_amount < 0:
            return asset_name, None, None, f"{asset_name}: valores negativos"

        return asset_name, free_amount, locked_amount, None

    @track_component("binance_api", slow_threshold=1)
    async def get_balance(self) -> dict:
        """Get account balances from Binance API."""
        try:
            client = await self._get_healthy_client()
            account = await self._execute_request(client.get_account, "get_account")

            if not self._validate_api_response(account, "account"):
                raise ValueError("Resposta de account inválida da API")

            balances = {}
            invalid_assets = []

            for asset_data in account["balances"]:
                asset_name, free, locked, err = self._parse_asset_balance(asset_data)
                if err:
                    invalid_assets.append(err)
                    continue

                total = free + locked
                if total > Decimal("0"):
                    balances[asset_name] = {
                        "free": free,
                        "locked": locked,
                        "total": total,
                    }

            if invalid_assets:
                warning(
                    f"Assets inválidos ignorados durante get_balance: {len(invalid_assets)} assets",
                    samples=invalid_assets[:3],
                )

            debug(
                "Balance obtido com sucesso",
                assets_count=len(balances),
                invalid_count=len(invalid_assets),
            )
            return balances

        except Exception as e:
            error("CRÍTICO: Erro ao obter balances", error=str(e), exc_info=True)
            raise RuntimeError(f"Falha crítica no get_balance: {str(e)}") from e

    VALID_ORDER_TYPES = frozenset(
        [
            "MARKET",
            "LIMIT",
            "STOP_LOSS",
            "STOP_LOSS_LIMIT",
            "TAKE_PROFIT",
            "TAKE_PROFIT_LIMIT",
        ]
    )
    VALID_SIDES = frozenset(["BUY", "SELL"])
    NUMERIC_PARAMS = frozenset(["quantity", "price", "stopPrice", "quoteOrderQty"])

    def _validate_required_order_params(self, params: dict) -> tuple[str, str, str]:
        required = ["symbol", "side", "type"]
        for param in required:
            if param not in params:
                raise ValueError(f"Parâmetro obrigatório ausente: {param}")
            if not params[param] or not isinstance(params[param], str):
                raise ValueError(f"Parâmetro {param} deve ser uma string válida")

        symbol = params["symbol"]
        side = params["side"].upper()
        order_type = params["type"].upper()

        if side not in self.VALID_SIDES:
            raise ValueError(f"Side inválido: {side}. Deve ser BUY ou SELL")

        if order_type not in self.VALID_ORDER_TYPES:
            raise ValueError(f"Type inválido: {order_type}")

        return symbol, side, order_type

    def _validate_numeric_order_param(self, params: dict, key: str) -> None:
        if key not in params:
            return
        try:
            value = float(params[key])
            if not is_numeric_valid(value) or value <= 0:
                raise ValueError(f"{key} inválido: {value}")
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"{key} deve ser um número positivo válido: {params[key]}"
            ) from exc

    def _validate_order_type_rules(self, order_type: str, params: dict) -> dict:
        if order_type == "MARKET" and "price" in params:
            warning("Removendo price de ordem MARKET", symbol=params.get("symbol"))
            params = {k: v for k, v in params.items() if k != "price"}
        elif order_type == "LIMIT" and "price" not in params:
            raise ValueError("Ordens LIMIT devem ter price especificado")

        if (
            order_type in ["MARKET", "LIMIT"]
            and "quantity" not in params
            and "quoteOrderQty" not in params
        ):
            raise ValueError("Ordens devem ter quantity ou quoteOrderQty especificado")

        return params

    def _prepare_order_params_for_api(self, params: dict) -> dict:
        prepared = params.copy()
        for key in self.NUMERIC_PARAMS:
            if key in prepared:
                prepared[key] = str(prepared[key])
        return prepared

    async def _execute_order(
        self, client: Any, params: dict, symbol: str, side: str, order_type: str
    ) -> dict:
        debug(
            "Criando ordem validada",
            symbol=symbol,
            side=side,
            type=order_type,
            quantity=params.get("quantity"),
            price=params.get("price"),
        )

        prepared_params = self._prepare_order_params_for_api(params)

        if prepared_params.get("emergency_execution", False):
            prepared_params.pop("emergency_execution", None)
            return await self._execute_emergency_request(
                client.create_order, "emergency_stop_loss", **prepared_params
            )

        return await self._execute_request(
            client.create_order, "create_order", **prepared_params
        )

    @track_component("binance_api", slow_threshold=3)
    async def create_order(self, **params) -> dict:
        client = await self._get_healthy_client()

        symbol, side, order_type = self._validate_required_order_params(params)

        self._validate_numeric_order_param(params, "quantity")
        self._validate_numeric_order_param(params, "price")
        self._validate_numeric_order_param(params, "stopPrice")

        params = self._validate_order_type_rules(order_type, params)

        try:
            return await self._execute_order(client, params, symbol, side, order_type)
        except Exception as e:
            error(
                "CRÍTICO: Falha na criação de ordem",
                symbol=symbol,
                side=side,
                type=order_type,
                params=str(params),
                error=str(e),
            )
            raise

    async def cancel_order(self, symbol: str, order_id: int) -> dict:
        if not symbol or not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("Symbol deve ser uma string válida não vazia")
        if not isinstance(order_id, int) or order_id <= 0:
            raise ValueError("Order ID deve ser um inteiro positivo")

        client = await self._get_healthy_client()
        return await self._execute_request(
            client.cancel_order,
            "cancel_order",
            symbol=symbol.strip().upper(),
            orderId=order_id,
        )

    async def cancel_oco(self, symbol: str, order_list_id: int) -> dict:
        try:
            client = await self._get_healthy_client()
            return await self._execute_request(
                client.cancel_oco_order,
                "cancel_oco_order",
                symbol=symbol,
                orderListId=order_list_id,
            )
        except Exception as e:
            error(
                "CRÍTICO: Falha ao cancelar OCO",
                symbol=symbol,
                order_list_id=order_list_id,
                error=str(e),
            )
            raise

    async def get_open_orders(self, symbol: str | None = None) -> list:
        params = {}
        if symbol:
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError("Symbol deve ser uma string válida não vazia")
            params["symbol"] = symbol.strip().upper()

        client = await self._get_healthy_client()
        return await self._execute_request(
            client.get_open_orders, "get_open_orders", **params
        )

    async def create_oco_order(self, **params) -> dict:
        client = await self._get_healthy_client()
        return await self._execute_request(
            client.create_oco_order, "create_oco_order", **params
        )

    async def close(self):
        async with self._connection_lock:
            if self._cleanup_called:
                return
            self._cleanup_called = True

            try:
                debug("Iniciando fechamento do pool de conexões Binance")
                await self._cleanup_all_connections()
                production("✓ Pool de conexões Binance fechado com sucesso")
            except Exception as e:
                warning("Erro durante fechamento do pool", error=str(e))
            finally:
                self._pool_status = ConnectionPoolStatus.FAILED
                debug("Binance connection pool desconectado e limpo")

    async def _ensure_connection_health(self):
        try:
            client = await self._get_healthy_client()

            async with asyncio.timeout(5.0):
                await client.ping()

            if self._pool_status == ConnectionPoolStatus.FAILED:
                self._pool_status = ConnectionPoolStatus.DEGRADED
                production("Pool de conexões recuperado para estado degradado")

        except Exception as e:
            warning(
                "Pool de conexões não está saudável, tentando recuperar", error=str(e)
            )

            if self._pool_status == ConnectionPoolStatus.FAILED:
                try:
                    await self._cleanup_all_connections()
                    await self._initialize_connection_pool()
                    production("Pool de conexões reinicializado com sucesso")
                except Exception as reinit_error:
                    error("Falha na reinicialização do pool", error=str(reinit_error))
                    raise RuntimeError(
                        "Pool de conexões irrecuperável"
                    ) from reinit_error

    async def get_pool_stats(self) -> dict[str, Any]:
        async with self._pool_lock:
            stats: dict[str, Any] = {
                "pool_status": self._pool_status.value,
                "active_client": self._active_client_id,
                "total_connections": 1 + len(self._backup_clients),
                "healthy_connections": sum(
                    1
                    for client_stats in self._client_stats.values()
                    if client_stats.last_health_check
                    and (datetime.now() - client_stats.last_health_check).seconds
                    < self._health_check_interval
                ),
                "current_rate_limit": self._current_rate_limit,
                "failure_count": self._failure_count,
                "backoff_multiplier": self._backoff_multiplier,
                "client_stats": {},
            }

            for client_id, client_stats in self._client_stats.items():
                if "client_stats" not in stats:
                    stats["client_stats"] = {}
                stats["client_stats"][client_id] = {
                    "total_requests": client_stats.total_requests,
                    "success_rate": (
                        client_stats.successful_requests
                        / max(client_stats.total_requests, 1)
                    )
                    * 100,
                    "avg_latency_ms": round(client_stats.avg_latency_ms, 2),
                    "last_used": (
                        client_stats.last_used.isoformat()
                        if client_stats.last_used
                        else None
                    ),
                    "last_health_check": (
                        client_stats.last_health_check.isoformat()
                        if client_stats.last_health_check
                        else None
                    ),
                }

            return stats
