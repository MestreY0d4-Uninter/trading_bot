import asyncio
import time
from decimal import Decimal
from typing import Any

from shared.observability.flow_tracker import track_component
from shared.observability.logger import error, production, warning
from shared.types.state import state
from utils.decimal_math import InvalidOperation, to_decimal
from utils.validation_utils import is_numeric_valid


class TradingCoordinatorBalance:
    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator
        self._balance_lock = asyncio.Lock()
        self._last_balance_update = 0.0
        self._balance_cache_ttl = 30

    def _should_skip_update(self, current_time: float) -> bool:
        return (current_time - self._last_balance_update) < self._balance_cache_ttl

    def _validate_components(self) -> bool:
        if not self.coordinator.client or not self.coordinator.risk_manager:
            error("Componentes críticos não inicializados para atualização de saldo")
            return False
        return True

    async def _fetch_usdt_balance(self) -> tuple[Decimal, Decimal] | None:
        balance = await self.coordinator.client.get_balance()
        if not self.coordinator.validators.validate_financial_data(
            balance, "saldo da API"
        ):
            error("Resposta de saldo inválida da API")
            return None

        usdt_data = balance.get("USDT", {})
        if not self.coordinator.validators.validate_financial_data(
            usdt_data, "dados USDT"
        ):
            error("Dados USDT inválidos na resposta de saldo")
            return None

        try:
            usdt_balance = Decimal(str(usdt_data.get("free", "0")))
            usdt_locked = Decimal(str(usdt_data.get("locked", "0")))
        except (InvalidOperation, ValueError) as e:
            error("CRÍTICO: Erro na conversão precisa de USDT balance", error=str(e))
            return None

        if not is_numeric_valid(float(usdt_balance)) or usdt_balance < 0:
            error("Saldo USDT inválido", balance=usdt_balance)
            return None

        if not is_numeric_valid(float(usdt_locked)) or usdt_locked < 0:
            warning("USDT locked inválido, assumindo 0", locked=usdt_locked)
            usdt_locked = Decimal("0")

        return usdt_balance, usdt_locked

    async def _sync_positions_if_needed(self, current_positions: dict | None) -> dict:
        if current_positions:
            return current_positions

        if not self.coordinator.db:
            return {}

        try:
            restored = await state.sync_positions_from_db(self.coordinator.db)
            if restored > 0:
                production(
                    f"Positions sincronizadas do banco durante update_balance: {restored}"
                )
                return await state.get_all_positions()
        except Exception as restore_error:
            warning(
                "Erro ao tentar recarregar positions do banco", error=str(restore_error)
            )

        return {}

    def _validate_price(
        self, current_price: Any, entry_price: Any, symbol: str
    ) -> Decimal | None:
        if (
            not isinstance(current_price, (int, float, Decimal))
            or (
                isinstance(current_price, (int, float))
                and not is_numeric_valid(current_price)
            )
            or current_price <= 0
        ):
            warning(
                "Preço atual inválido, usando entry_price",
                symbol=symbol,
                current_price=current_price,
                entry_price=entry_price,
            )
            current_price = entry_price

        try:
            return to_decimal(current_price)
        except (InvalidOperation, ValueError):
            return None

    def _validate_quantity(self, quantity: Any, symbol: str) -> Decimal | None:
        if (
            not isinstance(quantity, (int, float, Decimal))
            or (isinstance(quantity, (int, float)) and not is_numeric_valid(quantity))
            or quantity <= 0
        ):
            warning("Quantidade inválida na posição", symbol=symbol, quantity=quantity)
            return None

        try:
            return to_decimal(quantity)
        except (InvalidOperation, ValueError):
            return None

    def _calculate_position_value(
        self, symbol: str, position: dict, current_price: Any
    ) -> Decimal | None:
        if not self.coordinator.validators.validate_financial_data(
            position, f"posição {symbol}", ["entry_price", "quantity"]
        ):
            warning("Posição inválida ignorada", symbol=symbol)
            return None

        entry_price = position.get("entry_price", 0)
        quantity = position.get("quantity", 0)

        price_decimal = self._validate_price(current_price, entry_price, symbol)
        if price_decimal is None:
            return None

        quantity_decimal = self._validate_quantity(quantity, symbol)
        if quantity_decimal is None:
            return None

        try:
            position_value = price_decimal * quantity_decimal
            if not is_numeric_valid(float(position_value)):
                warning("Valor de posição calculado é inválido", symbol=symbol)
                return None
            return position_value
        except (InvalidOperation, ValueError) as e:
            warning(
                "Erro no cálculo preciso do valor da posição",
                symbol=symbol,
                error=str(e),
            )
            return None

    async def _calculate_positions_total_value(self, positions: dict) -> Decimal:
        if not positions or not isinstance(positions, dict):
            return Decimal("0")

        current_prices = await self.get_current_prices(list(positions.keys()))
        total_value = Decimal("0")

        for symbol, position in positions.items():
            current_price = current_prices.get(symbol, position.get("entry_price", 0))
            position_value = self._calculate_position_value(
                symbol, position, current_price
            )
            if position_value is not None:
                total_value += position_value

        if not is_numeric_valid(float(total_value)):
            error("Valor total de posições é inválido", positions_value=total_value)
            return Decimal("0")

        return total_value

    async def _update_risk_and_state(
        self, total_balance: Decimal, positions_value: Decimal, total_equity: Decimal
    ) -> None:
        await self.coordinator.risk_manager.update_balance(
            total_balance, positions_value
        )

        if not self.coordinator.risk_manager.starting_balance:
            await self.coordinator.risk_manager.set_starting_balance(total_balance)

        await state.update_metric("current_balance", total_balance)
        await state.update_metric("total_equity", total_equity)

        if self.coordinator.risk_manager.starting_balance:
            await state.update_metric(
                "starting_balance", self.coordinator.risk_manager.starting_balance
            )

        await state.update_account(total_balance, total_equity)

    @track_component("trading_coordinator")
    async def update_account_balance(self) -> None:
        current_time = time.time()
        if self._should_skip_update(current_time):
            return

        async with self._balance_lock:
            try:
                if not self._validate_components():
                    return

                usdt_result = await self._fetch_usdt_balance()
                if usdt_result is None:
                    return
                usdt_balance, usdt_locked = usdt_result
                total_balance = usdt_balance + usdt_locked

                current_positions = await state.get_all_positions()
                current_positions = await self._sync_positions_if_needed(
                    current_positions
                )

                positions_value = await self._calculate_positions_total_value(
                    current_positions
                )
                total_equity = total_balance + positions_value

                if not is_numeric_valid(float(total_equity)):
                    error(
                        "Equity total calculado é inválido", total_equity=total_equity
                    )
                    return

                await self._update_risk_and_state(
                    total_balance, positions_value, total_equity
                )

                self._last_balance_update = current_time

                production(
                    "Saldo atualizado",
                    balance=total_balance,
                    positions_value=positions_value,
                    equity=total_equity,
                    positions_count=len(current_positions) if current_positions else 0,
                )

            except Exception as e:
                error("CRÍTICO: Erro ao atualizar saldo", error=str(e))
                self._last_balance_update = current_time - (
                    self._balance_cache_ttl * 0.5
                )

    @track_component("trading_coordinator")
    async def get_current_prices(self, symbols: list[str]) -> dict[str, float]:
        async def fetch_price(symbol: str):
            try:
                ticker = await self.coordinator.data_manager.fetch_ticker(symbol)
                if ticker:
                    return symbol, float(
                        ticker.get("price", ticker.get("lastPrice", 0))
                    )
                return symbol, None
            except Exception:
                return symbol, None

        tasks = [fetch_price(symbol) for symbol in symbols]
        results = await asyncio.gather(*tasks)

        current_prices = {}
        for symbol, price in results:
            if price is not None:
                current_prices[symbol] = price

        return current_prices
