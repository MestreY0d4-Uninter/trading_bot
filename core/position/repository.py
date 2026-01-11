import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any

from shared.observability.flow_tracker import track_component
from shared.observability.logger import error, production

# Database timeout to prevent indefinite blocking (SPRINT 1 task #10)
DB_TIMEOUT_SECONDS = 5.0


class PositionRepository:
    def __init__(self, db: Any) -> None:
        if not db:
            raise ValueError("Database connection é obrigatório")
        self._db = db

    @track_component("position_repository")
    async def save_trade(
        self,
        symbol: str,
        side: str,
        entry_price: Decimal,
        quantity: Decimal,
        entry_time: datetime,
        strategy: str,
        take_profit_price: Decimal | None = None,
        stop_loss_price: Decimal | None = None,
        **kwargs,
    ) -> int:
        try:
            if take_profit_price is None or take_profit_price == 0:
                error(
                    "CRÍTICO: Take profit não fornecido ao salvar trade",
                    symbol=symbol,
                    take_profit=take_profit_price,
                )
                raise ValueError(f"Take profit é obrigatório para {symbol}")

            if stop_loss_price is None or stop_loss_price == 0:
                error(
                    "CRÍTICO: Stop loss não fornecido ao salvar trade",
                    symbol=symbol,
                    stop_loss=stop_loss_price,
                )
                raise ValueError(f"Stop loss é obrigatório para {symbol}")

            trade_data = {
                "symbol": symbol,
                "side": side,
                "entry_price": entry_price,
                "quantity": quantity,
                "entry_time": entry_time,
                "strategy": strategy,
                "take_profit": take_profit_price,
                "stop_loss": stop_loss_price,
                **kwargs,
            }

            # Database operation with timeout to prevent indefinite blocking
            try:
                async with asyncio.timeout(DB_TIMEOUT_SECONDS):
                    trade_id = await self._db.save_trade(trade_data)
                return trade_id
            except TimeoutError:
                error(
                    "CRÍTICO: Timeout ao salvar trade no DB",
                    symbol=symbol,
                    timeout=DB_TIMEOUT_SECONDS,
                )
                raise RuntimeError(
                    f"Database save_trade timeout após {DB_TIMEOUT_SECONDS}s"
                ) from None

        except Exception as e:
            error("Erro ao salvar trade no DB", symbol=symbol, error=str(e))
            raise

    @track_component("position_repository")
    async def update_trade_exit(
        self,
        trade_id: int,
        exit_price: Decimal,
        exit_time: datetime,
        exit_reason: str,
        pnl: Decimal,
        pnl_pct: Decimal,
        **kwargs,
    ) -> bool:
        try:
            try:
                async with asyncio.timeout(DB_TIMEOUT_SECONDS):
                    success = await self._db.update_trade_exit(
                        trade_id=trade_id,
                        exit_price=exit_price,
                        exit_time=exit_time,
                        exit_reason=exit_reason,
                        pnl=pnl,
                        pnl_pct=pnl_pct,
                        **kwargs,
                    )
                if success:
                    production("Trade atualizado no DB", trade_id=trade_id)
                return success
            except TimeoutError:
                error(
                    "CRÍTICO: Timeout ao atualizar trade no DB",
                    trade_id=trade_id,
                    timeout=DB_TIMEOUT_SECONDS,
                )
                raise RuntimeError(
                    f"Database update_trade_exit timeout após {DB_TIMEOUT_SECONDS}s"
                ) from None

        except Exception as e:
            error("Erro ao atualizar trade no DB", trade_id=trade_id, error=str(e))
            raise

    @track_component("position_repository")
    async def get_trade_by_id(self, trade_id: int) -> dict[str, Any] | None:
        try:
            try:
                async with asyncio.timeout(DB_TIMEOUT_SECONDS):
                    return await self._db.get_trade_by_id(trade_id)
            except TimeoutError:
                error(
                    "CRÍTICO: Timeout ao buscar trade no DB",
                    trade_id=trade_id,
                    timeout=DB_TIMEOUT_SECONDS,
                )
                return None

        except Exception as e:
            error("Erro ao buscar trade", trade_id=trade_id, error=str(e))
            return None

    @track_component("position_repository")
    async def update_trade_exit_by_symbol(
        self,
        symbol: str,
        exit_price: Decimal,
        exit_time: datetime,
        realized_pnl: Decimal,
        exit_reason: str,
    ) -> bool:
        try:
            try:
                async with asyncio.timeout(DB_TIMEOUT_SECONDS):
                    success = await self._db.update_trade_exit(
                        symbol=symbol,
                        exit_price=exit_price,
                        exit_time=exit_time,
                        realized_pnl=realized_pnl,
                        exit_reason=exit_reason,
                    )
                if success:
                    production("Trade atualizado no DB", symbol=symbol)
                return success
            except TimeoutError:
                error(
                    "CRÍTICO: Timeout ao atualizar trade no DB",
                    symbol=symbol,
                    timeout=DB_TIMEOUT_SECONDS,
                )
                raise RuntimeError(
                    f"Database update_trade_exit_by_symbol timeout após {DB_TIMEOUT_SECONDS}s"
                ) from None

        except Exception as e:
            error("Erro ao atualizar trade no DB", symbol=symbol, error=str(e))
            raise

    @track_component("position_repository")
    async def get_open_trades(self) -> list[dict[str, Any]]:
        try:
            try:
                async with asyncio.timeout(DB_TIMEOUT_SECONDS):
                    return await self._db.get_open_trades()
            except TimeoutError:
                error(
                    "CRÍTICO: Timeout ao buscar trades em aberto",
                    timeout=DB_TIMEOUT_SECONDS,
                )
                return []

        except Exception as e:
            error("Erro ao buscar trades em aberto", error=str(e))
            return []
