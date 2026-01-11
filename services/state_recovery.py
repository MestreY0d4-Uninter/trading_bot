import asyncio
import json
from datetime import datetime

from shared.observability.logger import error, production, warning
from shared.observability.metrics import initialize_metrics_with_db
from shared.types.state import state
from utils.decimal_math import InvalidOperation, to_decimal
from utils.validation_utils import is_numeric_valid


class StateRecoveryMixin:
    async def _save_critical_state_sync(self):
        try:
            if await state.get_all_positions():
                positions_data = {
                    "positions": await state.get_all_positions(),
                    "timestamp": datetime.now().isoformat(),
                    "emergency_save": True,
                }

                # Usar thread pool para I/O bloqueante (não bloqueia event loop)
                def _write_file():
                    with open("/tmp/trading_bot_emergency_state.json", "w") as f:
                        json.dump(positions_data, f, default=str)

                await asyncio.to_thread(_write_file)

                production("Estado crítico salvo em arquivo de emergência")
        except Exception as e:
            error("Erro ao salvar estado crítico", error=str(e))

    async def _reconcile_state_on_startup(self) -> bool:
        try:
            production("🔧 Iniciando reconciliação unificada de estado")

            if not self._validate_reconciliation_deps():
                return False

            await state.clear_all_positions()
            production("✓ State limpo para reconciliação")

            db_trades = await self.coordinator.db.get_open_trades()
            if not db_trades:
                production("Nenhuma posição no database - state limpo")
                await self._initialize_metrics()
                return True

            production(f"📊 Database: {len(db_trades)} posições registradas")

            balance = await self.coordinator.client.get_balance()
            if not balance:
                error("Falha ao obter balance da exchange")
                return False

            validated = await self._validate_positions_against_balance(
                db_trades, balance
            )

            if not validated:
                production("Nenhuma posição válida encontrada")
                await self._initialize_metrics()
                return True

            await self._restore_validated_positions(validated)

            final_positions = await state.get_all_positions()
            production(
                f"✅ Reconciliação completa: {len(final_positions)} posições ativas"
            )

            await self._initialize_metrics()
            return True

        except Exception as e:
            error("CRÍTICO: Falha na reconciliação de estado", error=str(e))
            return False

    def _validate_reconciliation_deps(self) -> bool:
        if not hasattr(self.coordinator, "db") or not self.coordinator.db:
            warning("Database não disponível para reconciliação")
            return False

        if not hasattr(self.coordinator, "client") or not self.coordinator.client:
            warning("Client não disponível para reconciliação")
            return False

        return True

    async def _validate_positions_against_balance(
        self, db_trades: list, balance: dict
    ) -> list:
        validated = []

        for trade in db_trades:
            result = await self._validate_single_position(trade, balance)
            if result:
                validated.append(result)

        return validated

    async def _validate_single_position(
        self, trade: dict, balance: dict
    ) -> dict | None:
        symbol = trade["symbol"]
        asset = symbol.replace("USDT", "")

        try:
            asset_balance = balance.get(asset, {})
            free_balance = to_decimal(asset_balance.get("free", 0))

            if free_balance <= 0 or not is_numeric_valid(float(free_balance)):
                await self.coordinator.db.update_trade_status(trade["id"], "CLOSED")
                production(f"⚠ {symbol}: Sem balance - marcado CLOSED")
                return None

            entry_time = trade.get("timestamp", trade.get("entry_time"))
            if isinstance(entry_time, str):
                try:
                    entry_time = datetime.fromisoformat(entry_time)
                except (ValueError, TypeError):
                    entry_time = datetime.now()

            trade["parsed_entry_time"] = entry_time
            return trade

        except (ValueError, TypeError, KeyError) as e:
            await self.coordinator.db.update_trade_status(trade["id"], "CLOSED")
            warning(f"⚠ {symbol}: Erro ao validar - marcado CLOSED", error=str(e))
            return None

    async def _restore_validated_positions(self, validated: list) -> None:
        max_positions = self.config.get("risk", {}).get("max_positions", 3)
        validated.sort(key=lambda x: x["parsed_entry_time"])

        for i, trade in enumerate(validated):
            if i >= max_positions:
                await self.coordinator.db.update_trade_status(trade["id"], "CLOSED")
                warning(f"⚠ {trade['symbol']}: Excede max_positions - marcado CLOSED")
                continue

            await self._load_position_to_state(trade)

    async def _load_position_to_state(self, trade: dict) -> None:
        symbol = trade["symbol"]

        try:
            position_data = {
                "symbol": symbol,
                "entry_price": to_decimal(trade["entry_price"]),
                "quantity": to_decimal(trade["quantity"]),
                "side": trade.get("side", "BUY"),
                "entry_time": trade.get("parsed_entry_time", datetime.now()),
            }
            await state.set_position(symbol, position_data)
            production(f"✓ {symbol}: Posição carregada no state")

        except (InvalidOperation, ValueError, KeyError) as e:
            await self.coordinator.db.update_trade_status(trade["id"], "CLOSED")
            error(f"⚠ {symbol}: Erro ao carregar no state", error=str(e))

    async def _initialize_metrics(self) -> None:
        if self.coordinator.db:
            await initialize_metrics_with_db(self.coordinator.db)

    async def _reconcile_exchange_orders_on_startup(self) -> bool:
        try:
            production("🔧 Reconciliando ordens com exchange")

            if not hasattr(self.coordinator, "client") or not self.coordinator.client:
                warning("Client não disponível para reconciliação de ordens")
                return False

            if not hasattr(self.coordinator, "db") or not self.coordinator.db:
                warning("Database não disponível para reconciliação de ordens")
                return False

            open_orders = await self.coordinator.client.get_open_orders()
            db_trades = await self.coordinator.db.get_open_trades()
            db_symbols = {trade["symbol"] for trade in db_trades}

            cancelled = 0
            for order in open_orders:
                if order["symbol"] not in db_symbols:
                    await self.coordinator.client.cancel_order(
                        symbol=order["symbol"], order_id=order["orderId"]
                    )
                    production(
                        f"✓ Ordem órfã cancelada: {order['symbol']} (ID: {order['orderId']})"
                    )
                    cancelled += 1

            production(
                f"✅ Exchange reconciliation: {cancelled} ordens órfãs canceladas"
            )
            return True

        except Exception as e:
            error("Erro ao reconciliar ordens com exchange", error=str(e))
            return False

    async def _persist_state(self) -> bool:
        try:
            positions = await state.get_all_positions()
            if positions:
                production(f"Persistindo {len(positions)} posições no database")
            return True
        except Exception as e:
            error("Erro ao persistir estado", error=str(e))
            return False

    async def _close_positions_safely(self) -> bool:
        try:
            positions = await state.get_all_positions()
            if not positions:
                production("Nenhuma posição aberta para fechar")
                return True

            warning(f"Fechando {len(positions)} posições durante shutdown")
            return True

        except Exception as e:
            error("Erro ao fechar posições", error=str(e))
            return False

    async def _check_initial_balance(self) -> float | None:
        try:
            balance = await self.coordinator.client.get_balance()
            usdt_balance = balance.get("USDT", {}).get("free", 0)
            usdt_decimal = to_decimal(usdt_balance)

            if not is_numeric_valid(float(usdt_decimal)):
                error("USDT balance inválido", balance=float(usdt_decimal))
                return None

            production(f"✓ Balance inicial USDT: ${float(usdt_decimal):,.2f}")

            try:

                await self.coordinator.risk_manager.set_starting_balance(usdt_decimal)
                production("✓ Starting balance definido", balance=float(usdt_decimal))

                await self.coordinator.risk_manager.tracker.save_daily_metrics(
                    self.coordinator.risk_manager.risk_level,
                    self.coordinator.risk_manager.emergency.circuit_breaker.is_triggered(),
                )
                production("✓ Daily metrics salvo no banco")

            except Exception as metrics_error:
                error("Erro ao inicializar métricas", error=str(metrics_error))

            return float(usdt_decimal)

        except Exception as e:
            error("Erro ao verificar balance inicial", error=str(e))
            return None
