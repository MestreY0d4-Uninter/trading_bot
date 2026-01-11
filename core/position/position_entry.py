from datetime import datetime
from decimal import Decimal

from core.validators.trading_validator import TradingValidator
from shared.enums import PositionState
from shared.observability.flow_tracker import track_component
from shared.observability.logger import error, position_opened, production, warning
from utils.order_id_generator import generate_client_order_id


class PositionEntryMixin:
    async def _calculate_position_parameters(
        self, signal: dict, usdt_balance: Decimal
    ) -> dict:
        return await self.calculator.calculate_position_parameters(
            signal, usdt_balance, self.risk_manager, self.executor
        )

    def _validate_fill_price(
        self, symbol: str, buy_order: dict, expected_price: Decimal
    ) -> dict:
        return self.validator.validate_fill_price(
            symbol, buy_order, expected_price, self.executor
        )

    async def _setup_stop_orders(
        self, symbol: str, quantity: Decimal, avg_price: Decimal
    ) -> dict:
        stop_loss_price = self.risk_manager.calculate_stop_loss(avg_price)
        take_profit_price = self.risk_manager.calculate_take_profit(avg_price)

        if self.config.get("oco_settings", {}).get("enabled", True):
            try:
                oco_client_id = generate_client_order_id("oco", symbol)
                oco_result = await self.executor.idempotency.execute_with_idempotency(
                    operation_id=oco_client_id,
                    operation=lambda: self.executor.create_oco_exit(
                        symbol=symbol,
                        quantity=quantity,
                        take_profit_price=take_profit_price,
                        stop_loss_price=stop_loss_price,
                        list_client_order_id=oco_client_id,
                    ),
                    symbol=symbol,
                    operation_type="OCO",
                )

                production(
                    "OCO creation attempt result",
                    symbol=symbol,
                    oco_result=oco_result,
                    result_type=type(oco_result).__name__,
                )

                if oco_result and oco_result.get("orderListId"):
                    return {
                        "protection_level": "FULL",
                        "has_oco": True,
                        "oco_id": oco_result.get("orderListId"),
                        "has_emergency_stop": False,
                        "emergency_stop_id": None,
                        "stop_loss_price": stop_loss_price,
                        "take_profit_price": take_profit_price,
                    }
                elif oco_result:
                    warning(
                        "OCO criada mas sem orderListId - usando fallback",
                        symbol=symbol,
                        oco_result=oco_result,
                    )

            except Exception as e:
                error(
                    "OCO creation failed - falling back to separate orders",
                    symbol=symbol,
                    error=str(e),
                    exc=str(e.__class__.__name__),
                )

        # Testnet fallback: create TP and SL as separate orders
        production("Creating separate TP and SL orders", symbol=symbol)
        try:
            # IMPORTANT: Create TP first (LIMIT order), then SL (STOP_LOSS_LIMIT)
            # STOP_LOSS_LIMIT blocks assets, so TP must be created first
            tp_client_id = generate_client_order_id("tp", symbol)
            tp_order = await self.executor.idempotency.execute_with_idempotency(
                operation_id=tp_client_id,
                operation=lambda: self.executor.create_take_profit_order(
                    symbol, quantity, take_profit_price, client_order_id=tp_client_id
                ),
                symbol=symbol,
                operation_type="TAKE_PROFIT",
            )
            sl_client_id = generate_client_order_id("sl", symbol)
            sl_order = await self.executor.idempotency.execute_with_idempotency(
                operation_id=sl_client_id,
                operation=lambda: self.executor.create_stop_loss_order(
                    symbol, quantity, stop_loss_price, client_order_id=sl_client_id
                ),
                symbol=symbol,
                operation_type="STOP_LOSS",
            )

            if sl_order and tp_order:
                production(
                    "Position fully protected with separate TP/SL",
                    symbol=symbol,
                    sl_id=sl_order.get("orderId"),
                    tp_id=tp_order.get("orderId"),
                )
                return {
                    "protection_level": "FULL",
                    "has_oco": False,
                    "oco_id": None,
                    "has_emergency_stop": True,
                    "emergency_stop_id": sl_order.get("orderId"),
                    "take_profit_id": tp_order.get("orderId"),
                    "stop_loss_price": stop_loss_price,
                    "take_profit_price": take_profit_price,
                }
            elif sl_order:
                warning(
                    "Position partially protected (SL only, TP creation failed)",
                    symbol=symbol,
                )
                return {
                    "protection_level": "PARTIAL",
                    "has_oco": False,
                    "oco_id": None,
                    "has_emergency_stop": True,
                    "emergency_stop_id": sl_order.get("orderId"),
                    "stop_loss_price": stop_loss_price,
                    "take_profit_price": None,
                }

        except Exception as e:
            error("Failed to create protection orders", symbol=symbol, error=str(e))

        return {
            "protection_level": "NONE",
            "has_oco": False,
            "oco_id": None,
            "has_emergency_stop": False,
            "emergency_stop_id": None,
            "stop_loss_price": None,
            "take_profit_price": None,
        }

    async def _save_position_data(
        self, symbol: str, position_data: dict, signal: dict
    ) -> int | None:
        # Database I/O OUTSIDE lock to prevent blocking (LOCK_HIERARCHY.md Rule 4)
        trade_id = await self.repository.save_trade(
            symbol=position_data["symbol"],
            side=position_data["side"],
            entry_price=position_data["entry_price"],
            quantity=position_data["quantity"],
            entry_time=position_data["entry_time"],
            strategy=position_data.get("strategy", "unknown"),
            take_profit_price=position_data.get("take_profit"),
            stop_loss_price=position_data.get("stop_loss"),
            timestamp=datetime.now(),
            market_condition=signal.get("market_condition", "unknown"),
            entry_score=position_data.get("entry_score", 0),
        )

        if trade_id is None:
            error("Failed to save trade to database", symbol=symbol)
            return None

        # Only hold lock for in-memory state update
        async with self._state_lock:
            await self._set_position_in_tracker(symbol, position_data)

        return trade_id

    async def _handle_position_open_error(
        self,
        symbol: str,
        exception: Exception,
        usdt_balance: Decimal,
        snapshot_id: str | None = None,
        quantity_to_reverse: Decimal | None = None,
    ) -> bool:
        error(
            "CRÍTICO: Erro ao abrir posição",
            symbol=symbol,
            balance=usdt_balance,
            error=str(exception),
        )

        if quantity_to_reverse and quantity_to_reverse > 0:
            warning(
                "Iniciando reversão de posição órfã (Orphaned Position Revert)",
                symbol=symbol,
                quantity=quantity_to_reverse,
            )
            try:
                revert_id = generate_client_order_id("sell", symbol)
                await self.executor.idempotency.execute_with_idempotency(
                    operation_id=revert_id,
                    operation=lambda: self.executor.market_sell(
                        symbol,
                        quantity_to_reverse,
                        emergency_execution=True,
                        client_order_id=revert_id,
                    ),
                    symbol=symbol,
                    operation_type="EMERGENCY_SELL",
                )
                production(
                    "Posição órfã revertida com sucesso",
                    symbol=symbol,
                    quantity=quantity_to_reverse,
                )
            except Exception as revert_error:
                error(
                    "FALHA CRÍTICA NA REVERSÃO DE POSIÇÃO ÓRFÃ",
                    symbol=symbol,
                    error=str(revert_error),
                )

        should_restore = False
        async with self._state_lock:
            self.position_states[symbol] = PositionState.CLOSED
            await self.position_tracker.remove_position(symbol)
            should_restore = snapshot_id and snapshot_id in self._operation_snapshots

        if should_restore:
            await self._restore_from_snapshot(
                snapshot_id, f"Erro na abertura: {str(exception)}"
            )
        else:
            warning(
                "Rollback sem snapshot - estado resetado para CLOSED",
                symbol=symbol,
                reason=str(exception),
            )

        return False

    @track_component("position_manager")
    async def _validate_state_and_create_snapshot(self, symbol: str) -> str:
        """Validate state CLOSED→OPENING transition and create operation snapshot.

        Returns snapshot_id on success, raises Exception on failure.
        """
        consistency_valid = await self._validate_global_consistency(
            symbol, "open_position"
        )

        if not consistency_valid:
            error("Global consistency check failed", symbol=symbol)
            raise RuntimeError("Global consistency check failed")

        if await self.position_tracker.has_position(symbol):
            warning("Posição já existe", symbol=symbol)
            raise RuntimeError("Position already exists")

        if not await self._atomic_state_check_and_set(
            symbol, PositionState.CLOSED, PositionState.OPENING
        ):
            error(
                "CRÍTICO: Falha ao definir estado OPENING",
                symbol=symbol,
                current_state=self.position_states.get(symbol),
            )
            async with self._state_lock:
                self.position_states[symbol] = PositionState.CLOSED
            raise RuntimeError("Failed to transition to OPENING state")

        snapshot_id = await self._create_operation_snapshot(symbol, "open_position")
        return snapshot_id

    async def _execute_buy_order_with_full_validation(
        self, symbol: str, params: dict
    ) -> dict:
        """Execute market buy and perform complete API response validation (SPRINT 1)."""
        client_order_id = generate_client_order_id("buy", symbol)

        buy_order = await self.executor.idempotency.execute_with_idempotency(
            operation_id=client_order_id,
            operation=lambda: self.executor.create_market_buy_order(
                symbol, params["quantity"], client_order_id=client_order_id
            ),
            symbol=symbol,
            operation_type="BUY",
        )

        if not buy_order:
            raise RuntimeError("Falha ao criar ordem")

        if not isinstance(buy_order, dict):
            raise RuntimeError(
                f"Resposta de API inválida: esperado dict, recebido {type(buy_order).__name__}"
            )

        required_fields = ["orderId", "status", "symbol"]
        missing_fields = [field for field in required_fields if field not in buy_order]
        if missing_fields:
            raise RuntimeError(
                f"Resposta de API incompleta: campos ausentes {missing_fields}"
            )

        buy_order["client_order_id"] = client_order_id
        return buy_order

    async def _validate_fill_price_and_check_slippage(
        self, symbol: str, buy_order: dict, expected_price: Decimal, quantity: Decimal
    ) -> tuple[Decimal, Decimal]:
        """Validate fill price and handle slippage with emergency liquidation if needed.

        Returns (avg_price, slippage_pct) on success, raises Exception on critical slippage.
        """
        fill_validation = self._validate_fill_price(symbol, buy_order, expected_price)
        if "error" in fill_validation:
            raise RuntimeError(fill_validation["error"])

        if not isinstance(fill_validation, dict):
            raise RuntimeError(
                f"Validação de fill inválida: esperado dict, recebido {type(fill_validation).__name__}"
            )

        required_fill_fields = ["avg_price", "slippage_pct"]
        missing_fill_fields = [
            field for field in required_fill_fields if field not in fill_validation
        ]
        if missing_fill_fields:
            raise RuntimeError(
                f"Validação de fill incompleta: campos ausentes {missing_fill_fields}"
            )

        avg_price = fill_validation["avg_price"]
        slippage_pct = fill_validation["slippage_pct"]

        max_slippage = Decimal(str(self.max_slippage_pct))
        if slippage_pct > max_slippage:
            error(
                "CRÍTICO: Slippage excessivo - cancelando posição",
                symbol=symbol,
                expected=expected_price,
                executed=avg_price,
                slippage_pct=slippage_pct,
                max_allowed=max_slippage,
            )

            try:
                emergency_sell_id = generate_client_order_id("sell", symbol)
                await self.executor.idempotency.execute_with_idempotency(
                    operation_id=emergency_sell_id,
                    operation=lambda: self.executor.market_sell(
                        symbol, quantity, client_order_id=emergency_sell_id
                    ),
                    symbol=symbol,
                    operation_type="EMERGENCY_SELL",
                )
                production(
                    "Posição imediatamente liquidada por slippage excessivo",
                    symbol=symbol,
                    slippage=slippage_pct,
                )
            except Exception as e:
                error(
                    "Falha ao liquidar posição com slippage excessivo",
                    symbol=symbol,
                    error=str(e),
                )

            raise RuntimeError(f"Slippage excessivo: {slippage_pct}%")

        elif slippage_pct > Decimal("0.5"):
            warning(
                "Slippage alto mas aceitável",
                symbol=symbol,
                expected=expected_price,
                executed=avg_price,
                slippage_pct=slippage_pct,
            )

        return avg_price, slippage_pct

    async def _setup_protection_or_emergency_liquidate(
        self, symbol: str, quantity: Decimal, avg_price: Decimal
    ) -> dict:
        """Setup stop orders and emergency liquidate if protection is incomplete.

        Returns protection info on success, raises Exception on incomplete protection.
        """
        stop_orders = await self._setup_stop_orders(symbol, quantity, avg_price)
        protection_level = stop_orders["protection_level"]

        if protection_level == "FULL":
            production("Position protected with OCO", symbol=symbol)
            return stop_orders

        error(
            "CRITICAL: Incomplete protection - liquidating position",
            symbol=symbol,
            protection_level=protection_level,
        )

        try:
            protection_sell_id = generate_client_order_id("sell", symbol)
            close_order = await self.executor.idempotency.execute_with_idempotency(
                operation_id=protection_sell_id,
                operation=lambda: self.executor.market_sell(
                    symbol,
                    quantity,
                    emergency_execution=True,
                    client_order_id=protection_sell_id,
                ),
                symbol=symbol,
                operation_type="EMERGENCY_SELL",
            )

            if close_order:
                from utils.decimal_math import calculate_pnl, safe_decimal_convert

                avg_sell_price = safe_decimal_convert(
                    close_order.get("avgPrice", str(avg_price))
                )
                pnl = calculate_pnl(avg_price, avg_sell_price, quantity)
                production(
                    "Position liquidated (incomplete protection)",
                    symbol=symbol,
                    protection_level=protection_level,
                    pnl=pnl,
                )
            else:
                error(
                    "CRITICAL: Failed to liquidate unprotected position",
                    symbol=symbol,
                )

        except Exception as sell_error:
            error(
                "CRITICAL: Exception during emergency liquidation",
                symbol=symbol,
                error=str(sell_error),
            )

        raise RuntimeError(f"Incomplete protection: {protection_level}")

    async def _save_position_and_transition_to_open(
        self,
        symbol: str,
        signal: dict,
        params: dict,
        avg_price: Decimal,
        buy_order: dict,
        stop_orders: dict,
    ) -> None:
        """Save position to database and transition state OPENING→OPEN atomically.

        CRITICAL: Database save MUST happen before state transition to OPEN.
        """
        position_data = {
            "symbol": symbol,
            "side": "BUY",
            "entry_price": avg_price,
            "quantity": params["quantity"],
            "entry_time": datetime.now(),
            "stop_loss": stop_orders["stop_loss_price"],
            "take_profit": stop_orders["take_profit_price"],
            "entry_score": signal["entry_score"],
            "score": signal["entry_score"],
            "score_components": signal.get("components", {}),
            "has_oco": stop_orders["has_oco"],
            "oco_id": stop_orders["oco_id"],
            "protection_level": stop_orders["protection_level"],
            "has_emergency_stop": stop_orders["has_emergency_stop"],
            "emergency_stop_id": stop_orders["emergency_stop_id"],
            "realized_pnl": 0,
            "status": "OPEN",
            "current_price": avg_price,
            "client_order_id": buy_order["client_order_id"],
            "exchange_order_id": buy_order.get("orderId"),
        }

        trade_id = await self._save_position_data(symbol, position_data, signal)
        if trade_id is None:
            raise RuntimeError("Failed to save position data")

        production(
            "Trade saved atomically",
            symbol=symbol,
            trade_id=trade_id,
            entry_price=avg_price,
        )

        if not await self._atomic_state_check_and_set(
            symbol, PositionState.OPENING, PositionState.OPEN
        ):
            error(
                "❌ [DEBUG] CRÍTICO: Falha ao definir estado OPEN após criação",
                symbol=symbol,
                current_state=self.position_states.get(symbol),
                expected_state=PositionState.OPENING,
                target_state=PositionState.OPEN,
            )
            raise RuntimeError("Falha na transição de estado")

    async def _finalize_position_success(
        self,
        symbol: str,
        params: dict,
        avg_price: Decimal,
        stop_orders: dict,
        signal: dict,
        snapshot_id: str,
    ) -> None:
        """Finalize successful position opening with metrics and cleanup."""
        position_opened(
            symbol,
            avg_price,
            params["quantity"],
            params["position_size_usd"],
            stop_orders["stop_loss_price"],
            stop_orders["take_profit_price"],
            stop_orders["has_oco"],
        )

        signal["has_oco"] = stop_orders["has_oco"]
        signal["protection_level"] = stop_orders["protection_level"]
        signal["has_emergency_stop"] = stop_orders["has_emergency_stop"]

        if snapshot_id in self._operation_snapshots:
            del self._operation_snapshots[snapshot_id]

        await self._cleanup_old_snapshots()

        if not await self.validate_consistency():
            error("Consistency issues after opening position", symbol=symbol)

    @track_component("position_manager")
    async def open_position(
        self, symbol: str, signal: dict, usdt_balance: Decimal
    ) -> bool:
        validation_result = TradingValidator.validate_position_inputs(
            symbol, signal, usdt_balance
        )
        if not validation_result:
            error(f"Position validation failed for {symbol}")
            return False

        consistency_result = await self.validate_consistency()
        if not consistency_result:
            warning(
                "Consistency issues detected before opening position", symbol=symbol
            )

        position_lock = await self._get_position_lock(symbol)

        async with self._acquire_lock_with_timeout(
            position_lock, symbol, "open_position"
        ):
            snapshot_id = None
            executed_quantity = None

            try:
                snapshot_id = await self._validate_state_and_create_snapshot(symbol)

                params = await self._calculate_position_parameters(signal, usdt_balance)
                if "error" in params:
                    error("CRÍTICO: " + params["error"], symbol=symbol)
                    return await self._handle_position_open_error(
                        symbol, Exception(params["error"]), usdt_balance, snapshot_id
                    )

                buy_order = await self._execute_buy_order_with_full_validation(
                    symbol, params
                )

                if buy_order:
                    from utils.decimal_math import to_decimal

                    executed_quantity = to_decimal(
                        buy_order.get(
                            "executedQty",
                            buy_order.get("origQty", params.get("quantity", 0)),
                        )
                    )

                avg_price, slippage_pct = (
                    await self._validate_fill_price_and_check_slippage(
                        symbol, buy_order, params["current_price"], params["quantity"]
                    )
                )

                stop_orders = await self._setup_protection_or_emergency_liquidate(
                    symbol, params["quantity"], avg_price
                )

                await self._save_position_and_transition_to_open(
                    symbol, signal, params, avg_price, buy_order, stop_orders
                )

                await self._finalize_position_success(
                    symbol, params, avg_price, stop_orders, signal, snapshot_id
                )

                return True

            except Exception as e:
                return await self._handle_position_open_error(
                    symbol,
                    e,
                    usdt_balance,
                    snapshot_id,
                    quantity_to_reverse=executed_quantity,
                )
