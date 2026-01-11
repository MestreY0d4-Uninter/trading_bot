import asyncio
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

try:
    import psutil

    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

from core.position.position_manager import PositionManager
from shared.observability.flow_tracker import track_component
from shared.observability.logger import debug, error, production, warning
from shared.observability.metrics import metrics
from shared.timeouts import Timeouts
from shared.types.state import state
from utils.decimal_math import to_decimal

ZERO = Decimal("0")


class UnifiedMonitor:
    def __init__(self, coordinator, orchestrator=None) -> None:
        self.coordinator = coordinator
        self.orchestrator = orchestrator
        self.config = coordinator.config

        # Position monitoring config (2s interval)
        self.monitor_interval = 2
        self.monitor_count = 0
        self.position_alerts = {}
        self.alert_cooldown = 300
        self.extreme_loss_threshold = -10
        self.critical_stop_threshold_pct = -5.0
        self.position_warning_hours = 24
        self.position_critical_hours = 48
        self.position_max_hours = 72
        self.old_position_hours = self.position_critical_hours

        # Health monitoring config (60s interval)
        self.health_check_interval = 60
        self.last_health_check = datetime.now()
        self.health_issues = []
        self.max_issues = 10
        self.memory_threshold_mb = 500
        self.min_active_tasks = 4
        self.max_time_without_update = 300
        self.health_metrics = {
            "checks_performed": 0,
            "issues_detected": 0,
            "memory_peaks": [],
            "last_issue_time": None,
        }

        # Intelligent monitoring config (no loop - on-demand)
        monitoring_config = self.config.get("monitoring", {}).get(
            "intelligent_monitoring", {}
        )
        self.daily_pair_limit = monitoring_config.get("daily_pair_limit", 3)
        self.max_simultaneous_losses = monitoring_config.get(
            "max_simultaneous_losses", 3
        )
        self.performance_review_days = monitoring_config.get(
            "performance_review_days", 7
        )
        self.daily_pair_counts: dict[str, int] = defaultdict(int)
        self.simultaneous_losses = 0
        self.last_reset_date = datetime.now().date()
        self.performance_cache: dict[str, Any] = {}
        self.cache_timestamp = None
        self.cache_ttl = timedelta(hours=1)

        # Database reference
        if hasattr(coordinator, "db"):
            self.db_handler = coordinator.db
        else:
            self.db_handler = None

        production(
            "Unified Monitor inicializado",
            critical_interval=self.monitor_interval,
            health_interval=self.health_check_interval,
        )
        if not PSUTIL_AVAILABLE:
            warning("psutil não disponível - monitoramento de memória limitado")

    async def _apply_symbol_cooldown(self, symbol: str, reason: str = "") -> bool:
        """Apply cooldown to symbol after critical stop loss."""
        cooldown_seconds = self.config.get("strategy", {}).get("cooldown_seconds", 90)

        if hasattr(self.coordinator, "trading_loop") and self.coordinator.trading_loop:
            try:
                await self.coordinator.trading_loop.apply_symbol_cooldown(
                    symbol, cooldown_seconds
                )
                production(
                    "Cooldown aplicado via trading_loop",
                    symbol=symbol,
                    seconds=cooldown_seconds,
                    reason=reason,
                )
                return True
            except Exception as e:
                warning("Falha no cooldown primário", symbol=symbol, error=str(e))

        cooldown_until = datetime.now() + timedelta(seconds=cooldown_seconds)
        await state.set_cooldown(symbol, cooldown_until)
        production(
            "Cooldown aplicado via state",
            symbol=symbol,
            seconds=cooldown_seconds,
            until=cooldown_until.strftime("%H:%M:%S"),
            reason=reason,
        )
        return True

    async def run(self):
        production("Unified Monitor iniciado - modo dual-interval")

        await asyncio.gather(
            self._run_critical_monitoring(),
            self._run_health_monitoring(),
            return_exceptions=True,
        )

    # ========== CRITICAL MONITORING (2s interval) ==========

    def _extract_price_from_data(
        self, price_data: dict | Decimal | int | float
    ) -> Decimal:
        """Extract bid price from price data (dict or scalar)."""
        if isinstance(price_data, dict):
            return to_decimal(price_data.get("bid", price_data.get("last", 0)))
        return to_decimal(price_data) if price_data > 0 else ZERO

    def _is_critical_stop_loss(
        self, entry_price: Decimal, current_price: Decimal
    ) -> tuple[bool, Decimal]:
        """Check if position has hit critical stop loss threshold."""
        if entry_price <= 0 or current_price <= 0:
            return False, ZERO
        pnl_pct = PositionManager.calculate_pnl_percentage(entry_price, current_price)
        return pnl_pct <= self.critical_stop_threshold_pct, pnl_pct

    async def _classify_positions(
        self, positions: dict, current_prices: dict
    ) -> tuple[list, list]:
        """Classify positions into emergency (critical stop) and normal."""
        emergency_tasks = []
        normal_positions = []

        for symbol, position in positions.items():
            if not self.coordinator.running:
                break

            if not position:
                continue

            current_price = self._extract_price_from_data(current_prices.get(symbol, 0))
            if current_price <= 0:
                debug("Sem preço atual", symbol=symbol)
                continue

            entry_price = to_decimal(position.get("entry_price", 0))
            is_critical, pnl_pct = self._is_critical_stop_loss(
                entry_price, current_price
            )

            if is_critical:
                production(
                    "STOP LOSS CRÍTICO DETECTADO - EXECUÇÃO IMEDIATA",
                    symbol=symbol,
                    pnl_pct=pnl_pct,
                    current_price=current_price,
                    entry_price=entry_price,
                )
                emergency_tasks.append(
                    asyncio.create_task(
                        self._critical_stop_loss(
                            symbol, position, current_price, pnl_pct
                        )
                    )
                )
            else:
                normal_positions.append((symbol, position, current_price))

        return emergency_tasks, normal_positions

    async def _process_emergency_stops(self, emergency_tasks: list) -> None:
        """Process all emergency stop loss tasks."""
        if not emergency_tasks:
            return
        try:
            await asyncio.gather(*emergency_tasks, return_exceptions=True)
            production(f"Processadas {len(emergency_tasks)} posições críticas")
        except Exception as e:
            error("Erro ao processar stop loss críticos", error=str(e))

    async def _process_normal_positions(self, normal_positions: list) -> None:
        """Process normal positions for exit conditions."""
        for symbol, position, current_price in normal_positions:
            if not self.coordinator.running:
                break
            await self._monitor_position(symbol, position, current_price)

    async def _run_critical_monitoring(self):
        """Critical position monitoring loop (2s interval)."""
        production("Critical monitoring iniciado (2s interval)")

        while self.coordinator.running and not self.coordinator.shutdown_event.is_set():
            try:
                self.monitor_count += 1
                await self.coordinator.update_account_balance()

                positions = await state.get_all_positions()
                if not positions:
                    debug("Monitor", count=self.monitor_count, positions=0)
                    await asyncio.sleep(5)
                    continue

                debug(
                    "Monitor verificando posições",
                    count=self.monitor_count,
                    total=len(positions),
                )

                current_prices = await self._get_current_prices(list(positions.keys()))
                emergency_tasks, normal_positions = await self._classify_positions(
                    positions, current_prices
                )

                await self._process_emergency_stops(emergency_tasks)
                await self._process_normal_positions(normal_positions)
                await asyncio.sleep(self.monitor_interval)

            except Exception as e:
                error(
                    "Erro no monitor de posições",
                    time=datetime.now().strftime("%H:%M:%S"),
                    positions=len(await state.get_all_positions()),
                    error=str(e),
                )
                await asyncio.sleep(10)

    async def _execute_emergency_sell(
        self, symbol: str, quantity: Decimal, position_manager
    ) -> dict | None:
        """Execute emergency market sell with timeout."""
        if quantity <= 0:
            error(
                "ERRO: Quantidade inválida para venda emergencial",
                symbol=symbol,
                quantity=quantity,
            )
            return None

        async with asyncio.timeout(Timeouts.MONITOR_CHECK):
            return await position_manager._execute_market_sell(symbol, quantity)

    async def _record_critical_exit(
        self, symbol: str, entry_price: Decimal, sell_price: Decimal, quantity: Decimal
    ) -> dict:
        """Record critical stop loss exit in database and return result."""
        pnl_pct = PositionManager.calculate_pnl_percentage(entry_price, sell_price)
        pnl_usd = PositionManager.calculate_pnl_usd(entry_price, sell_price, quantity)

        if self.db_handler:
            await self.db_handler.update_trade_exit(
                symbol=symbol,
                exit_price=sell_price,
                exit_time=datetime.now(),
                realized_pnl=pnl_usd,
                exit_reason="CRITICAL_STOP_LOSS",
            )

        return {
            "symbol": symbol,
            "pnl": pnl_usd,
            "pnl_pct": pnl_pct,
            "reason": "CRITICAL_STOP_LOSS",
            "entry_price": entry_price,
            "exit_price": sell_price,
            "quantity": quantity,
        }

    async def _cleanup_after_critical_exit(
        self,
        symbol: str,
        position_manager,
        result: dict,
        expected_pnl_pct: Decimal,
        start_time: datetime,
    ) -> None:
        """Cleanup state and apply cooldown after critical exit."""
        execution_time = (datetime.now() - start_time).total_seconds()

        production(
            "✅ STOP LOSS CRÍTICO EXECUTADO",
            symbol=symbol,
            execution_time_ms=execution_time * 1000,
            pnl_usd=result.get("pnl", 0),
            actual_pnl_pct=result.get("pnl_pct", 0),
            expected_pnl_pct=expected_pnl_pct,
        )

        await state.remove_position(symbol)
        if position_manager:
            await position_manager.critical_position_cleanup(symbol)
        await self._process_exit_result(symbol, result)
        await self._apply_symbol_cooldown(symbol, "CRITICAL_STOP_LOSS")

    async def _critical_stop_loss(
        self, symbol: str, position: dict, current_price: Decimal, pnl_pct: Decimal
    ) -> bool:
        """Execute critical stop loss immediately."""
        error(
            "CRÍTICO: Executando stop loss EXTREMO",
            symbol=symbol,
            pnl_pct=pnl_pct,
            current_price=current_price,
            entry_price=position.get("entry_price"),
            threshold=self.critical_stop_threshold_pct,
        )

        position_manager = getattr(self.coordinator, "position_manager", None)
        if not position_manager:
            error("CRÍTICO: PositionManager não disponível", symbol=symbol)
            return False

        original_timeout = getattr(self.coordinator.client, "_dynamic_timeout", None)
        if hasattr(self.coordinator.client, "_dynamic_timeout"):
            self.coordinator.client._dynamic_timeout = lambda x: 2.0

        start_time = datetime.now()
        try:
            sell_order = await self._execute_emergency_sell(
                symbol, to_decimal(position.get("quantity", 0)), position_manager
            )
            if not sell_order:
                error("CRÍTICO: Venda emergencial retornou None", symbol=symbol)
                return False

            sell_price = position_manager.executor.get_average_fill_price(sell_order)
            entry_price = to_decimal(position.get("entry_price", 0))

            result = await self._record_critical_exit(
                symbol, entry_price, sell_price, to_decimal(position.get("quantity", 0))
            )
            await self._cleanup_after_critical_exit(
                symbol, position_manager, result, pnl_pct, start_time
            )
            return True

        except TimeoutError:
            execution_time = (datetime.now() - start_time).total_seconds()
            error(
                "TIMEOUT CRÍTICO: Stop loss crítico não executou em 2s",
                symbol=symbol,
                pnl_pct=pnl_pct,
                current_price=current_price,
                execution_time_ms=execution_time * 1000,
            )
            return False

        except Exception as e:
            execution_time = (datetime.now() - start_time).total_seconds()
            error(
                "ERRO na execução da venda crítica",
                symbol=symbol,
                error=str(e),
                execution_time_ms=execution_time * 1000,
            )
            return False

        finally:
            if original_timeout and hasattr(
                self.coordinator.client, "_dynamic_timeout"
            ):
                self.coordinator.client._dynamic_timeout = original_timeout

    async def _monitor_position(
        self, symbol: str, position: dict, current_price: Decimal
    ):
        """Monitor single position for exit conditions"""
        try:
            entry_price = position["entry_price"]
            pnl_pct = PositionManager.calculate_pnl_percentage(
                to_decimal(entry_price), to_decimal(current_price)
            )

            production(
                "Monitorando posição",
                module="position_monitor",
                symbol=symbol,
                pnl_pct=pnl_pct,
                entry=entry_price,
                current=current_price,
            )

            await state.set_position(
                symbol, {**position, "current_price": current_price}
            )

            timeout_action = await self._check_position_timeout(
                symbol, position, current_price
            )
            if timeout_action:
                return

            await self._check_position_alerts(symbol, position, float(pnl_pct))

            position_manager = getattr(self.coordinator, "position_manager", None)
            if not position_manager:
                return

            should_exit, exit_reason = await position_manager.check_exit_conditions(
                symbol, position, current_price
            )

            if should_exit:
                production(
                    "Condição de saída detectada",
                    module="position_monitor",
                    symbol=symbol,
                    reason=exit_reason,
                )

                from decimal import Decimal

                price_data = (
                    current_price.get(symbol, {})
                    if isinstance(current_price, dict) and symbol not in current_price
                    else current_price
                )
                if isinstance(price_data, dict):
                    exit_price = price_data.get(
                        "bid", price_data.get("last", Decimal("0"))
                    )
                else:
                    exit_price = price_data

                result = await position_manager.close_position(
                    symbol, exit_reason, exit_price
                )

                if result:
                    await self._process_exit_result(symbol, result)
            else:
                await position_manager.update_trailing_stop(
                    symbol, position, current_price
                )

        except Exception as e:
            error("Erro ao monitorar posição", symbol=symbol, error=str(e))

    def _parse_entry_time(self, entry_time: str | datetime | None) -> datetime | None:
        """Parse entry_time from string or datetime to datetime."""
        if not entry_time:
            return None
        if isinstance(entry_time, datetime):
            return entry_time
        if isinstance(entry_time, str):
            try:
                return datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return None
        return None

    def _normalize_timezones(
        self, entry_time: datetime, current_time: datetime
    ) -> tuple[datetime, datetime]:
        """Normalize timezones between entry_time and current_time."""
        if entry_time.tzinfo is not None and current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=entry_time.tzinfo)
        elif entry_time.tzinfo is None and current_time.tzinfo is not None:
            entry_time = entry_time.replace(tzinfo=current_time.tzinfo)
        return entry_time, current_time

    def _should_alert(self, symbol: str, cooldown_seconds: int) -> bool:
        """Check if enough time has passed since last alert for symbol."""
        last_alert_time = self.position_alerts.get(symbol, {}).get("time", datetime.min)
        return (datetime.now() - last_alert_time).total_seconds() > cooldown_seconds

    async def _force_close_timeout_position(
        self, symbol: str, current_price: Decimal, hours_in_position: float
    ) -> bool:
        """Force close a position that exceeded max hours."""
        position_manager = getattr(self.coordinator, "position_manager", None)
        if not position_manager:
            return False

        result = await position_manager.close_position(
            symbol, "POSITION_TIMEOUT", current_price
        )

        if result:
            production(
                "Posição fechada por TIMEOUT",
                symbol=symbol,
                hours_in_position=hours_in_position,
                pnl_usd=result.get("pnl", 0),
                reason="POSITION_TIMEOUT",
            )
            await self._process_exit_result(symbol, result)
        else:
            error("CRÍTICO: Falha ao fechar posição por timeout", symbol=symbol)

        return True

    async def _check_position_timeout(
        self, symbol: str, position: dict, current_price: Decimal
    ) -> bool:
        """Check and force close old positions."""
        try:
            entry_time = self._parse_entry_time(position.get("entry_time"))
            if not entry_time:
                if position.get("entry_time"):
                    warning(f"Formato de entry_time inválido para {symbol}")
                return False

            current_time = datetime.now()
            entry_time, current_time = self._normalize_timezones(
                entry_time, current_time
            )
            hours_in_position = (current_time - entry_time).total_seconds() / 3600

            if hours_in_position >= self.position_max_hours:
                error(
                    "TIMEOUT CRÍTICO: Forçando fechamento de posição antiga",
                    symbol=symbol,
                    hours_in_position=hours_in_position,
                    max_hours=self.position_max_hours,
                    entry_time=entry_time,
                    current_price=current_price,
                )
                return await self._force_close_timeout_position(
                    symbol, current_price, hours_in_position
                )

            if hours_in_position >= self.position_critical_hours:
                if self._should_alert(symbol, 1800):
                    warning(
                        "ALERTA CRÍTICO: Posição muito antiga",
                        symbol=symbol,
                        hours_in_position=hours_in_position,
                        will_close_in_hours=self.position_max_hours - hours_in_position,
                    )
            elif hours_in_position >= self.position_warning_hours:
                if self._should_alert(symbol, 3600):
                    warning(
                        "ALERTA: Posição ficando antiga",
                        symbol=symbol,
                        hours_in_position=hours_in_position,
                    )

            return False

        except Exception as e:
            error("Erro ao verificar timeout de posição", symbol=symbol, error=str(e))
            return False

    async def _check_position_alerts(self, symbol: str, position: dict, pnl_pct: float):
        """Check and log position alerts"""
        current_time = datetime.now()

        last_alert = self.position_alerts.get(symbol, {})
        last_alert_time = last_alert.get("time")

        if last_alert_time:
            time_since_alert = (current_time - last_alert_time).total_seconds()
            if time_since_alert < self.alert_cooldown:
                return

        alerts = []

        if pnl_pct <= self.extreme_loss_threshold:
            alerts.append(f"Perda extrema: {pnl_pct:.1f}%")
        elif pnl_pct <= -5:
            alerts.append(f"Perda significativa: {pnl_pct:.1f}%")

        entry_time = position.get("entry_time")
        if isinstance(entry_time, datetime):
            hours_in_position = (current_time - entry_time).total_seconds() / 3600
            if hours_in_position > self.old_position_hours:
                alerts.append(f"Posição antiga: {hours_in_position:.0f}h")

        if pnl_pct >= 5:
            alerts.append(f"Lucro alto: {pnl_pct:.1f}%")

        if alerts:
            for alert in alerts:
                warning(f"{symbol}: {alert}")

            self.position_alerts[symbol] = {
                "time": current_time,
                "alerts": alerts,
                "pnl_pct": pnl_pct,
            }

    async def _process_exit_result(self, symbol: str, result: dict):
        """Process exit result and record trade"""
        try:
            pnl = result["pnl"]
            pnl_pct = result["pnl_pct"]
            reason = result["reason"]

            if pnl > 0:
                production(
                    "Trade lucrativo fechado",
                    symbol=symbol,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    reason=reason,
                )
                self.record_trade(symbol, "win")
            else:
                consecutive_losses = await state.get_metric("consecutive_losses") or 0
                warning(
                    "Trade com perda fechado",
                    symbol=symbol,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    reason=reason,
                    consecutive_losses=consecutive_losses + 1,
                )
                self.record_trade(symbol, "loss")

            if symbol in self.position_alerts:
                del self.position_alerts[symbol]

        except Exception as e:
            error("Erro ao processar resultado de saída", symbol=symbol, error=str(e))

    def _extract_ticker_prices(self, ticker: dict) -> dict[str, Decimal]:
        """Extract bid, ask, last prices from ticker dict."""
        price = ticker.get("price", ticker.get("lastPrice", 0))
        bid = to_decimal(ticker.get("bidPrice", price))
        ask = to_decimal(ticker.get("askPrice", price))
        last = to_decimal(price) if price else bid
        return {"bid": bid, "ask": ask, "last": last}

    def _has_data_manager(self) -> bool:
        """Check if coordinator has a valid data_manager."""
        return (
            hasattr(self.coordinator, "data_manager") and self.coordinator.data_manager
        )

    def _has_client(self) -> bool:
        """Check if coordinator has a valid client."""
        return hasattr(self.coordinator, "client") and self.coordinator.client

    async def _fetch_price_from_source(
        self, symbol: str, source: str
    ) -> dict[str, Decimal] | None:
        """Try to fetch price from a specific source."""
        ticker = None

        match source:
            case "data_manager_cache" if self._has_data_manager():
                ticker = await self.coordinator.data_manager.get_trading_price(
                    symbol, max_age_seconds=10
                )
            case "data_manager_fetch" if self._has_data_manager():
                ticker = await self.coordinator.data_manager.fetch_ticker(
                    symbol, max_age_seconds=3
                )
            case "client_direct" if self._has_client():
                ticker = await self.coordinator.client.get_ticker(symbol)
                if ticker:
                    debug("Usando preço direto da exchange (sem cache)", symbol=symbol)

        if ticker and "price" in ticker:
            return self._extract_ticker_prices(ticker)
        return None

    async def _get_current_prices(self, symbols: list[str]) -> dict[str, dict]:
        """Get current prices for symbols (returns Decimal values)."""
        current_prices = {}
        sources = ["data_manager_cache", "data_manager_fetch", "client_direct"]

        for symbol in symbols:
            try:
                for source in sources:
                    try:
                        prices = await self._fetch_price_from_source(symbol, source)
                        if prices:
                            current_prices[symbol] = prices
                            break
                    except Exception as e:
                        debug(f"Fonte {source} falhou", symbol=symbol, error=str(e))
                else:
                    debug("Sem preço disponível", symbol=symbol)
            except Exception as e:
                debug("Erro ao obter preço", symbol=symbol, error=str(e))

        return current_prices

    async def force_check_all_positions(self):
        """Force check all positions immediately"""
        production("Verificação forçada de todas as posições iniciada")

        positions = await state.get_all_positions()
        if not positions:
            production("Nenhuma posição para verificar")
            return

        current_prices = await self._get_current_prices(list(positions.keys()))

        for symbol, position in positions.items():
            price_data = current_prices.get(symbol, 0)
            if isinstance(price_data, dict):
                current_price = price_data.get("bid", 0)
            else:
                current_price = price_data if price_data > 0 else 0

            if current_price > 0:
                await self._monitor_position(symbol, position, current_price)

        production("Verificação forçada concluída", positions=len(positions))

    async def get_monitoring_stats(self) -> dict:
        """Get position monitoring statistics"""
        positions = await state.get_all_positions()

        stats = {
            "positions_monitored": len(positions),
            "monitor_runs": self.monitor_count,
            "active_alerts": len(self.position_alerts),
            "positions_with_alerts": list(self.position_alerts.keys()),
        }

        if positions:
            pnl_values = []
            for position in positions.values():
                if "current_price" in position and "entry_price" in position:
                    entry = position["entry_price"]
                    current = position["current_price"]
                    if entry > 0:
                        pnl_pct = PositionManager.calculate_pnl_percentage(
                            entry, current
                        )
                        pnl_values.append(pnl_pct)

            if pnl_values:
                stats["avg_pnl_pct"] = sum(pnl_values) / len(pnl_values)
                stats["best_pnl_pct"] = max(pnl_values)
                stats["worst_pnl_pct"] = min(pnl_values)

        return stats

    # ========== HEALTH MONITORING (60s interval) ==========

    async def _run_health_monitoring(self):
        """System health monitoring loop (60s interval)"""
        production("Health monitoring iniciado (60s interval)")

        while (
            self.orchestrator and self.orchestrator.running
            if self.orchestrator
            else self.coordinator.running
        ):
            try:
                await asyncio.sleep(self.health_check_interval)

                if self.orchestrator and not self.orchestrator.running:
                    break

                await self.perform_health_check()

            except asyncio.CancelledError:
                break
            except Exception as e:
                error("Erro no monitor de saúde", error=str(e))
                await asyncio.sleep(120)

    @track_component("health_monitor", slow_threshold=10)
    async def perform_health_check(self):
        """Perform comprehensive health check"""
        current_time = datetime.now()
        issues_found = []

        self.health_metrics["checks_performed"] += 1

        await self._check_binance_connection(issues_found)
        await self._check_coordinator_activity(current_time, issues_found)
        self._check_memory_usage(issues_found)
        self._check_active_tasks(issues_found)
        self._check_circuit_breaker(issues_found)
        await self._check_positions_health(issues_found)
        self._check_data_fetcher(issues_found)

        if issues_found:
            warning("Problemas de saúde detectados", issues="; ".join(issues_found))
            self.add_issues(issues_found)
            self.health_metrics["issues_detected"] += len(issues_found)
            self.health_metrics["last_issue_time"] = current_time
            await metrics.record_error("health_check_issues")
        else:
            if self.health_issues:
                production("Problemas de saúde resolvidos")
                self.health_issues.clear()

        self.last_health_check = current_time

    async def _check_binance_connection(self, issues: list[str]):
        """Check Binance connection"""
        if (
            self.coordinator
            and hasattr(self.coordinator, "client")
            and self.coordinator.client
        ):
            try:
                async with asyncio.timeout(Timeouts.MONITOR_EXTENDED):
                    ping_result = await self.coordinator.client.ping()
                if ping_result is None:
                    issues.append("Conexão Binance falhou")
            except TimeoutError:
                issues.append("Ping Binance timeout")
            except Exception as e:
                issues.append(f"Erro ping Binance: {str(e)[:50]}")

    async def _check_coordinator_activity(
        self, current_time: datetime, issues: list[str]
    ):
        """Check coordinator activity"""
        if self.coordinator and hasattr(self.coordinator, "get_status"):
            try:
                status = await self.coordinator.get_status()
                last_update = status.get("last_update")
                if isinstance(last_update, datetime):
                    time_since_update = (current_time - last_update).total_seconds()
                    if time_since_update > self.max_time_without_update:
                        issues.append(
                            f"Coordenador sem update há {time_since_update//60:.0f}min"
                        )
            except Exception as e:
                issues.append(f"Erro ao verificar status do coordenador: {str(e)[:50]}")

    def _check_memory_usage(self, issues: list[str]):
        """Check memory usage"""
        if not PSUTIL_AVAILABLE:
            return

        try:
            process = psutil.Process()
            memory_mb = process.memory_info().rss / 1024 / 1024

            if memory_mb > self.memory_threshold_mb:
                issues.append(f"Alto uso de memória: {memory_mb:.0f}MB")
                self.health_metrics["memory_peaks"].append(
                    {"timestamp": datetime.now(), "memory_mb": memory_mb}
                )

                if len(self.health_metrics["memory_peaks"]) > 10:
                    self.health_metrics["memory_peaks"] = self.health_metrics[
                        "memory_peaks"
                    ][-10:]

        except Exception as e:
            debug("Erro ao verificar memória", error=str(e))

    def _check_active_tasks(self, issues: list[str]):
        """Check active tasks count"""
        if self.orchestrator and hasattr(self.orchestrator, "tasks"):
            active_tasks = len([t for t in self.orchestrator.tasks if not t.done()])
            if active_tasks < self.min_active_tasks:
                issues.append(f"Poucas tasks ativas: {active_tasks}")

    def _check_circuit_breaker(self, issues: list[str]):
        """Check circuit breaker status"""
        if hasattr(self.coordinator, "risk_manager") and self.coordinator.risk_manager:
            if hasattr(self.coordinator.risk_manager, "circuit_breaker"):
                cb = self.coordinator.risk_manager.circuit_breaker
                if hasattr(cb, "is_triggered") and cb.is_triggered():
                    reason = (
                        cb.get_reason()
                        if hasattr(cb, "get_reason")
                        else "Não especificado"
                    )
                    issues.append(f"Circuit breaker ativo: {reason}")

    async def _check_positions_health(self, issues: list[str]):
        """Check positions health"""
        all_positions = await state.get_all_positions()

        for symbol, position in all_positions.items():
            try:
                if not isinstance(position, dict):
                    continue

                entry_price = position.get("entry_price", 0)
                current_price = position.get("current_price", entry_price)
                entry_time = position.get("entry_time")

                if entry_price > 0 and current_price > 0:
                    pnl_pct = PositionManager.calculate_pnl_percentage(
                        to_decimal(entry_price), to_decimal(current_price)
                    )

                    if pnl_pct < -10:
                        issues.append(f"{symbol}: Perda extrema ({pnl_pct:.1f}%)")

                    if isinstance(entry_time, datetime):
                        time_in_position = (
                            datetime.now() - entry_time
                        ).total_seconds() / 3600
                        if time_in_position > 48:
                            issues.append(
                                f"{symbol}: Posição muito antiga ({time_in_position:.0f}h)"
                            )

            except Exception as e:
                debug("Erro ao verificar saúde de posição", symbol=symbol, error=str(e))

    def _check_data_fetcher(self, issues: list[str]):
        """Check data fetcher health"""
        if (
            self.orchestrator
            and hasattr(self.orchestrator, "data_fetcher")
            and self.orchestrator.data_fetcher
        ):
            try:
                if hasattr(self.orchestrator.data_fetcher, "cache"):
                    from shared.infra.cache import cache

                    cache_stats = (
                        cache.get_status() if hasattr(cache, "get_status") else None
                    )
                    if cache_stats:
                        hit_rate = cache_stats.get("hit_rate", 100)
                        if hit_rate < 50:
                            issues.append(f"Cache hit rate baixo: {hit_rate:.1f}%")
            except Exception as e:
                debug("Erro ao verificar data fetcher", error=str(e))

    def add_issue(self, issue: str):
        """Add single health issue"""
        self.health_issues.append(issue)
        if len(self.health_issues) > self.max_issues:
            self.health_issues = self.health_issues[-self.max_issues :]

    def add_issues(self, issues: list[str]):
        """Add multiple health issues"""
        self.health_issues.extend(issues)
        if len(self.health_issues) > self.max_issues:
            self.health_issues = self.health_issues[-self.max_issues :]

    @track_component("health_monitor", slow_threshold=2)
    def get_health_status(self) -> dict:
        """Get health status summary"""
        status = "HEALTHY"
        if len(self.health_issues) > 5:
            status = "CRITICAL"
        elif len(self.health_issues) > 2:
            status = "WARNING"
        elif self.health_issues:
            status = "MINOR_ISSUES"

        return {
            "status": status,
            "issues_count": len(self.health_issues),
            "recent_issues": self.health_issues[-5:],
            "last_check": self.last_health_check,
            "metrics": self.health_metrics.copy(),
        }

    def get_system_info(self) -> dict:
        """Get system information"""
        info = {
            "python_version": sys.version.split()[0],
            "platform": sys.platform,
            "uptime_hours": 0,
            "psutil_available": PSUTIL_AVAILABLE,
        }

        if hasattr(self.coordinator, "start_time") and isinstance(
            self.coordinator.start_time, datetime
        ):
            uptime = datetime.now() - self.coordinator.start_time
            info["uptime_hours"] = uptime.total_seconds() / 3600

        if PSUTIL_AVAILABLE:
            try:
                process = psutil.Process()
                info["memory_mb"] = process.memory_info().rss / 1024 / 1024
                info["cpu_percent"] = process.cpu_percent(interval=0.1)
                info["threads"] = process.num_threads()
            except Exception as e:
                debug("Erro ao coletar info do sistema", error=str(e))

        return info

    @track_component("health_monitor", slow_threshold=5)
    async def emergency_check(self) -> dict:
        """Perform emergency check"""
        issues: list[str] = []
        await self._check_binance_connection(issues)
        self._check_circuit_breaker(issues)

        return {
            "timestamp": datetime.now(),
            "critical_issues": issues,
            "requires_restart": len(issues) > 0,
        }

    # ========== INTELLIGENT MONITORING (on-demand) ==========

    def can_trade_pair(self, symbol: str) -> tuple[bool, str]:
        """Check if pair can be traded (daily limit)"""
        self._reset_daily_counts_if_needed()

        current_count = self.daily_pair_counts.get(symbol, 0)
        if current_count >= self.daily_pair_limit:
            return (
                False,
                f"Daily limit reached for {symbol}: {current_count}/{self.daily_pair_limit}",
            )

        return True, "Pair trading allowed"

    def can_open_position(self) -> tuple[bool, str]:
        """Check if new position can be opened (simultaneous losses)"""
        if self.simultaneous_losses >= self.max_simultaneous_losses:
            return (
                False,
                f"Max simultaneous losses reached: {self.simultaneous_losses}/{self.max_simultaneous_losses}",
            )

        return True, "Position opening allowed"

    def record_trade(self, symbol: str, result: str):
        """Record trade result for intelligent monitoring"""
        self._reset_daily_counts_if_needed()

        self.daily_pair_counts[symbol] += 1

        if result == "loss":
            self.simultaneous_losses += 1
        elif result == "win":
            self.simultaneous_losses = max(0, self.simultaneous_losses - 1)

        debug(
            "Trade registrado",
            symbol=symbol,
            result=result,
            daily_count=self.daily_pair_counts[symbol],
            simultaneous_losses=self.simultaneous_losses,
        )

    def _reset_daily_counts_if_needed(self):
        """Reset daily counts at midnight"""
        current_date = datetime.now().date()
        if current_date != self.last_reset_date:
            debug(
                "Reset diário dos contadores",
                old_date=self.last_reset_date,
                new_date=current_date,
                previous_counts=dict(self.daily_pair_counts),
            )
            self.daily_pair_counts.clear()
            self.last_reset_date = current_date

    def _is_cache_valid(self) -> bool:
        """Check if performance cache is still valid."""
        if not self.cache_timestamp or not self.performance_cache:
            return False
        return datetime.now() - self.cache_timestamp < self.cache_ttl

    def _filter_trades_by_period(
        self, trades: list[dict], start_date: datetime, end_date: datetime
    ) -> list[dict]:
        """Filter trades within the specified date range."""
        filtered = []
        for trade in trades or []:
            exit_time = trade.get("exit_time")
            if not exit_time:
                continue
            try:
                exit_dt = datetime.fromisoformat(exit_time)
                if start_date <= exit_dt <= end_date:
                    filtered.append(trade)
            except (ValueError, TypeError):
                continue
        return filtered

    async def get_performance_summary(self, force_refresh: bool = False) -> dict:
        """Get performance summary with caching."""
        if not force_refresh and self._is_cache_valid():
            return self.performance_cache

        try:
            now = datetime.now()
            if self.db_handler:
                start_date = now - timedelta(days=self.performance_review_days)
                all_trades = await self.db_handler.get_trades(limit=1000)
                trades = self._filter_trades_by_period(all_trades, start_date, now)
                metrics = self._calculate_performance_metrics(trades)
            else:
                metrics = await self._get_basic_metrics()

            self.performance_cache = {
                "period_days": self.performance_review_days,
                "last_updated": now.isoformat(),
                "daily_limits": dict(self.daily_pair_counts),
                "simultaneous_losses": self.simultaneous_losses,
                "performance_metrics": metrics,
            }
            self.cache_timestamp = now
            return self.performance_cache

        except Exception as e:
            error("Erro ao gerar performance summary", error=str(e))
            return {
                "error": str(e),
                "daily_limits": dict(self.daily_pair_counts),
                "simultaneous_losses": self.simultaneous_losses,
            }

    def _calculate_trade_stats(
        self, trades: list[dict]
    ) -> tuple[int, int, float, float, float]:
        """Calculate wins, losses, total pnl, avg win, avg loss from trades."""
        wins = losses = 0
        total_pnl = win_sum = loss_sum = 0.0

        for trade in trades:
            pnl = float(trade.get("pnl", 0))
            total_pnl += pnl
            if pnl > 0:
                wins += 1
                win_sum += pnl
            else:
                losses += 1
                loss_sum += pnl

        avg_win = win_sum / wins if wins > 0 else 0.0
        avg_loss = loss_sum / losses if losses > 0 else 0.0
        return wins, losses, total_pnl, avg_win, avg_loss

    def _calculate_pair_performance(self, trades: list[dict]) -> dict[str, dict]:
        """Calculate performance per trading pair."""
        pair_performance: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"trades": 0, "pnl": 0.0}
        )
        for trade in trades:
            symbol = trade.get("symbol", "UNKNOWN")
            pair_performance[symbol]["trades"] += 1
            pair_performance[symbol]["pnl"] += float(trade.get("pnl", 0))
        return dict(pair_performance)

    def _calculate_performance_metrics(self, trades: list[dict]) -> dict:
        """Calculate performance metrics from trades."""
        if not trades:
            return {"total_trades": 0, "message": "No trades in period"}

        total_trades = len(trades)
        wins, losses, total_pnl, avg_win, avg_loss = self._calculate_trade_stats(trades)
        profit_factor = (
            abs(avg_win * wins / (avg_loss * losses))
            if losses > 0 and avg_loss != 0
            else float("inf")
        )

        return {
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / total_trades if total_trades > 0 else 0,
            "total_pnl": total_pnl,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": profit_factor,
            "pair_performance": self._calculate_pair_performance(trades),
        }

    async def _get_basic_metrics(self) -> dict:
        """Get basic metrics when DB not available"""
        try:
            positions = await state.get_active_positions()
            return {
                "active_positions": len(positions),
                "message": "Database not available - basic metrics only",
            }
        except Exception as e:
            return {"error": f"Could not get basic metrics: {str(e)}"}

    async def should_review_strategy(self) -> tuple[bool, str]:
        """Check if strategy should be reviewed"""
        summary = await self.get_performance_summary()
        metrics = summary.get("performance_metrics", {})

        if "error" in metrics:
            return False, "Cannot review - insufficient data"

        total_trades = metrics.get("total_trades", 0)
        if total_trades < 10:
            return False, f"Insufficient trades for review: {total_trades}"

        win_rate = metrics.get("win_rate", 0)
        total_pnl = metrics.get("total_pnl", 0)

        if win_rate < 0.4:
            return True, f"Low win rate: {win_rate:.1%}"

        if total_pnl < -100:
            return True, f"Significant losses: ${total_pnl:.2f}"

        pair_performance = metrics.get("pair_performance", {})
        problematic_pairs = [
            symbol
            for symbol, data in pair_performance.items()
            if data["trades"] >= 3 and data["pnl"] < -50
        ]

        if problematic_pairs:
            return True, f"Problematic pairs detected: {', '.join(problematic_pairs)}"

        return False, "Strategy performance acceptable"

    def get_monitoring_status(self) -> dict:
        """Get intelligent monitoring status"""
        return {
            "intelligent_monitoring": {
                "daily_pair_limits": dict(self.daily_pair_counts),
                "simultaneous_losses": self.simultaneous_losses,
                "max_simultaneous_losses": self.max_simultaneous_losses,
                "last_reset_date": self.last_reset_date.isoformat(),
                "cache_valid": (
                    (
                        self.cache_timestamp
                        and datetime.now() - self.cache_timestamp < self.cache_ttl
                    )
                    if self.cache_timestamp
                    else False
                ),
            }
        }
