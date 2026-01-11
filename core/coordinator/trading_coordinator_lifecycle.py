import asyncio
from decimal import Decimal
from typing import Any

from core.analysis.indicators import (
    MomentumIndicators,
    TrendIndicators,
    VolatilityIndicators,
    VolumeIndicators,
)
from core.analysis.market_analyzer import MarketAnalyzer
from core.analysis.scoring_system import ScoringSystem
from core.execution import OrderExecutor
from core.position.position_manager import PositionManager
from core.position.repository import PositionRepository
from core.risk.risk_manager import RiskManager
from core.signals.signal_analyzer import SignalAnalyzer
from core.validators.correlation_validator import CorrelationValidator
from database.db_handler import DatabaseHandler
from infrastructure.api.binance_api import BinanceClient
from monitoring.performance_monitor import PerformanceMonitor
from monitoring.unified_monitor import UnifiedMonitor
from services.metrics_scheduler import MetricsScheduler
from shared.enums import PositionState
from shared.observability.flow_tracker import track_component
from shared.observability.logger import error, production, warning
from shared.types.state import state
from utils.decimal_math import to_decimal


class TechnicalIndicators:
    def __init__(self) -> None:
        self.momentum = MomentumIndicators()
        self.volatility = VolatilityIndicators()
        self.volume = VolumeIndicators()
        self.trend = TrendIndicators()

    def calculate_all(self, candles):
        results = {}
        results.update(self.momentum.get_all_indicators(candles))
        results.update(self.volatility.get_all_indicators(candles))
        results.update(self.volume.get_all_indicators(candles))
        return results

    def calculate_volume_profile(self, candles):
        return self.volume.calculate_volume_profile(candles)


COMPONENT_INIT_ORDER = {
    "client": {"dependency": None, "validator": lambda c: hasattr(c, "ping")},
    "data_manager": {
        "dependency": "client",
        "validator": lambda dm: dm.client is not None,
    },
    "executor": {"dependency": "client", "validator": lambda e: e.client is not None},
    "risk_manager": {"dependency": None, "validator": lambda rm: rm.config is not None},
    "position_monitor": {
        "dependency": ["data_manager", "risk_manager"],
        "validator": None,
    },
}


class TradingCoordinatorLifecycle:
    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator

    async def _initialize_component(
        self, component_name: str, component_factory, *args, **kwargs
    ):
        try:
            component = component_factory(*args, **kwargs)
            if hasattr(component, "initialize"):
                await component.initialize()

            if component_name in COMPONENT_INIT_ORDER:
                validator = COMPONENT_INIT_ORDER[component_name]["validator"]
                if validator and not validator(component):
                    raise RuntimeError(f"{component_name} falhou na validação")

            if not component:
                raise RuntimeError(f"{component_name} falhou na inicialização")

            production(f" {component_name} inicializado")
            return component

        except Exception as e:
            error(f"Erro na inicialização do {component_name}", error=str(e))
            raise RuntimeError(
                f"{component_name} falhou na inicialização: {str(e)}"
            ) from e

    async def _initialize_core_components(
        self, initialized_components: list[str]
    ) -> None:
        production(" [COORD 1/10] Inicializando BinanceClient...")
        self.coordinator.client = await self._initialize_component(
            "client", BinanceClient, self.coordinator.config
        )
        production(" [COORD 1/10] BinanceClient inicializado!")
        initialized_components.append("client")

        self.coordinator.data_manager.set_client(self.coordinator.client)
        state.client = self.coordinator.client
        production(" DataManager configurado IMEDIATAMENTE")

        production(" [COORD 2/10] Inicializando Database + Executor...")
        self.coordinator.db = DatabaseHandler(
            self.coordinator.config["database"]["path"]
        )

        # Inicializar DB primeiro para passar ao OrderExecutor
        await self.coordinator.db.initialize()

        if not self.coordinator.db:
            raise RuntimeError("DatabaseHandler falhou na inicialização")

        # Criar OrderExecutor com db_handler para idempotency persistence
        self.coordinator.executor = await self._initialize_component(
            "executor",
            OrderExecutor,
            self.coordinator.client,
            self.coordinator.config,
            self.coordinator.db,  # Passar db_handler
        )

        # Restaurar cache de idempotência após restart
        if hasattr(self.coordinator.executor, "idempotency"):
            await self.coordinator.executor.idempotency.load_cache_from_db()

        initialized_components.extend(["executor", "db"])
        production(
            "✅ [COORD 2/10] DatabaseHandler + Executor inicializados (idempotency cache restored)"
        )

    async def _initialize_analysis_components(
        self, initialized_components: list[str]
    ) -> None:
        from shared.observability.metrics import initialize_metrics_with_db

        production(" [COORD 3/10] Paralelizando Metrics + RiskManager...")
        _, self.coordinator.risk_manager = await asyncio.gather(
            initialize_metrics_with_db(self.coordinator.db),
            self._initialize_component(
                "risk_manager",
                RiskManager,
                self.coordinator.config,
                self.coordinator.db,
            ),
        )

        self.coordinator.data_manager.set_db_handler(self.coordinator.db)
        initialized_components.append("risk_manager")
        production(" [COORD 3/10] Metrics + RiskManager inicializados em paralelo")

        self.coordinator.market_analyzer = MarketAnalyzer(self.coordinator.config)
        self.coordinator.scoring_system = ScoringSystem(self.coordinator.config)
        self.coordinator.indicators = TechnicalIndicators()

        if not all(
            [
                self.coordinator.market_analyzer,
                self.coordinator.scoring_system,
                self.coordinator.indicators,
            ]
        ):
            raise RuntimeError("Componentes de análise falharam na inicialização")
        initialized_components.extend(
            ["market_analyzer", "scoring_system", "indicators"]
        )
        production(" Componentes de análise inicializados")

    async def _initialize_position_management(
        self, initialized_components: list[str]
    ) -> None:
        # Create PositionTracker and inject into state (DI to break circular dependency)
        from core.position.position_tracker import PositionTracker

        position_tracker = PositionTracker()
        state.set_position_tracker(position_tracker)

        position_repository = PositionRepository(self.coordinator.db)
        self.coordinator.position_manager = PositionManager(
            self.coordinator.config,
            self.coordinator.executor,
            self.coordinator.risk_manager,
            repository=position_repository,
            position_tracker=position_tracker,
            client=self.coordinator.client,
        )
        if not self.coordinator.position_manager:
            raise RuntimeError("PositionManager falhou na inicialização")
        initialized_components.append("position_manager")
        production(" PositionManager inicializado")

        self.coordinator.correlation_validator = CorrelationValidator(
            self.coordinator.config
        )
        initialized_components.append("correlation_validator")
        production(" Correlation Validator inicializado")

        self.coordinator.signal_analyzer = SignalAnalyzer(
            self.coordinator.config,
            self.coordinator.indicators,
            self.coordinator.market_analyzer,
            self.coordinator.scoring_system,
            self.coordinator.risk_manager,
        )
        if not self.coordinator.signal_analyzer:
            raise RuntimeError("SignalAnalyzer falhou na inicialização")
        initialized_components.append("signal_analyzer")
        production(" SignalAnalyzer inicializado")

    async def _initialize_monitors(self, initialized_components: list[str]) -> None:
        self.coordinator.unified_monitor = UnifiedMonitor(self.coordinator)
        self.coordinator.performance_monitor = PerformanceMonitor(self.coordinator)
        self.coordinator.metrics_scheduler = MetricsScheduler(
            self.coordinator.config, self.coordinator.risk_manager
        )

        if not all(
            [
                self.coordinator.unified_monitor,
                self.coordinator.performance_monitor,
                self.coordinator.metrics_scheduler,
            ]
        ):
            raise RuntimeError("Monitores e scheduler falharam na inicialização")
        initialized_components.extend(
            ["unified_monitor", "performance_monitor", "metrics_scheduler"]
        )
        production(" Unified Monitor e MetricsScheduler inicializados")

    async def _restore_and_validate_positions(
        self, initialized_components: list[str]
    ) -> None:
        try:
            open_trades, balance = await asyncio.gather(
                self.coordinator.db.get_open_trades(),
                self.coordinator.client.get_balance(),
            )

            if not balance:
                raise RuntimeError(
                    "Não foi possível obter saldo para validação de consistência"
                )

            positions = self._build_positions_from_trades(open_trades)
            validation_result = self._validate_positions_against_balance(
                positions, balance
            )

            await self._handle_inconsistencies(
                positions, balance, validation_result["inconsistent"]
            )

            await self._sync_positions_to_state(positions)
            self._detect_zombie_assets(positions, balance)

            initialized_components.append("positions_synced")
            production(
                "✓ Posições sincronizadas e validadas",
                total=len(positions),
                valid=validation_result["valid_count"],
                inconsistencies=len(validation_result["inconsistent"]),
            )

        except Exception as e:
            error("Falha na sincronização e validação de posições", error=str(e))
            raise RuntimeError(f"Sincronização crítica falhou: {str(e)}") from e

    def _build_positions_from_trades(self, open_trades: list) -> dict:
        positions = {}
        for trade in open_trades:
            symbol = trade.get("symbol")
            if symbol:
                positions[symbol] = {
                    "symbol": symbol,
                    "entry_price": trade.get("entry_price"),
                    "quantity": trade.get("quantity"),
                    "entry_time": trade.get("entry_time"),
                    "side": trade.get("side", "BUY"),
                    "trade_id": trade.get("id"),
                    "stop_loss": trade.get("stop_loss"),
                    "take_profit": trade.get("take_profit"),
                    "has_oco": bool(trade.get("has_oco", 0)),
                    "oco_id": trade.get("oco_id"),
                }
        return positions

    def _validate_positions_against_balance(
        self, positions: dict, balance: dict
    ) -> dict:
        valid_count = 0
        inconsistent = []

        for symbol, position in positions.items():
            if not isinstance(position, dict):
                inconsistent.append(f"{symbol}: dados inválidos")
                continue

            asset = symbol.replace("USDT", "")
            asset_balance = balance.get(asset, {})

            if not isinstance(asset_balance, dict):
                inconsistent.append(f"{symbol}: saldo do asset não encontrado")
                continue

            total_balance = to_decimal(asset_balance.get("free", 0)) + to_decimal(
                asset_balance.get("locked", 0)
            )
            position_qty = to_decimal(position.get("quantity", 0))

            if total_balance == 0 and position_qty > 0:
                inconsistent.append(f"{symbol}: posição registrada mas sem saldo")
                continue

            if abs(total_balance - position_qty) > (position_qty * Decimal("0.001")):
                warning(
                    f"Pequena diferença detectada {symbol}: position={position_qty}, balance={total_balance}"
                )

            valid_count += 1

        return {"valid_count": valid_count, "inconsistent": inconsistent}

    async def _handle_inconsistencies(
        self, positions: dict, balance: dict, inconsistent: list
    ) -> None:
        if not inconsistent:
            return

        for inconsistency in inconsistent:
            warning("Inconsistência detectada", inconsistency=inconsistency)

        if len(inconsistent) <= len(positions) * 0.5:
            return

        warning(
            f"Detectadas {len(inconsistent)}/{len(positions)} posições inconsistentes - limpando"
        )

        ghost_symbols = self._find_ghost_positions(positions, balance)
        await self._clean_ghost_positions(positions, ghost_symbols)

        remaining = len(inconsistent) - len(ghost_symbols)
        if len(positions) > 0 and remaining > len(positions) * 0.5:
            raise RuntimeError(
                f"Muitas inconsistências após limpeza: {remaining}/{len(positions)}"
            )

        if len(positions) == 0:
            production("✓ Posições inconsistentes limpas - banco sincronizado")

    def _find_ghost_positions(self, positions: dict, balance: dict) -> list:
        ghost_symbols = []
        for symbol in positions.keys():
            asset = symbol.replace("USDT", "")
            asset_balance = balance.get(asset, {})
            total = to_decimal(asset_balance.get("free", 0)) + to_decimal(
                asset_balance.get("locked", 0)
            )
            if total < Decimal("0.001"):
                ghost_symbols.append(symbol)
        return ghost_symbols

    async def _clean_ghost_positions(
        self, positions: dict, ghost_symbols: list
    ) -> None:
        if not ghost_symbols:
            return

        production(f"Limpando {len(ghost_symbols)} posições fantasma: {ghost_symbols}")

        for symbol in ghost_symbols:
            open_trades = await self.coordinator.db.get_open_trades()
            for trade in open_trades:
                if trade["symbol"] == symbol:
                    await self.coordinator.db.update_trade_status(trade["id"], "CLOSED")
                    production(f"✓ Posição {symbol} marcada como CLOSED")
                    break
            positions.pop(symbol, None)

    async def _sync_positions_to_state(self, positions: dict) -> None:
        for symbol, position in positions.items():
            if not isinstance(position, dict):
                continue

            await state.set_position(symbol, position)

            if self.coordinator.position_manager:
                async with self.coordinator.position_manager._state_lock:
                    self.coordinator.position_manager.position_states[symbol] = (
                        PositionState.OPEN
                    )
                production(f"✓ position_states sincronizado para OPEN: {symbol}")

    def _detect_zombie_assets(self, positions: dict, balance: dict) -> None:
        for asset, asset_data in balance.items():
            if asset == "USDT":
                continue

            total_balance = to_decimal(asset_data.get("free", 0)) + to_decimal(
                asset_data.get("locked", 0)
            )

            if total_balance > Decimal("0.001"):
                symbol = f"{asset}USDT"
                if symbol not in positions:
                    error(
                        "CRITICAL: ZOMBIE ASSET DETECTED",
                        symbol=symbol,
                        balance=total_balance,
                    )

    async def _update_balance_and_metrics(
        self, initialized_components: list[str]
    ) -> None:
        try:
            production(" Forçando atualização de balance...")
            self.coordinator.balance._last_balance_update = 0
            await self.coordinator.balance.update_account_balance()
            balance = await state.get_account()
            if not balance or balance.get("balance", 0) <= 0:
                raise RuntimeError("Falha na atualização de saldo ou saldo inválido")

            total_balance = balance.get("balance", 0)
            total_equity = balance.get("equity", total_balance)

            await self.coordinator.risk_manager.set_starting_balance(total_balance)
            production(" Starting balance definido", balance=total_balance)

            await state.update_metric("current_balance", total_balance)
            await state.update_metric("total_equity", total_equity)
            await state.update_metric("starting_balance", total_balance)
            production(" Metrics sincronizadas")

            initialized_components.append("balance_updated")
            production(" Saldo atualizado", balance=total_balance)

            try:
                production(" Inicializando daily_metrics para hoje...")
                await self.coordinator.risk_manager.tracker.save_daily_metrics(
                    self.coordinator.risk_manager.risk_level,
                    self.coordinator.risk_manager.emergency.circuit_breaker.is_triggered(),
                )
                production(" Daily metrics inicializado no banco")
            except Exception as metrics_error:
                error(
                    "Erro ao inicializar daily_metrics (não crítico)",
                    error=str(metrics_error),
                )
        except Exception as balance_error:
            error(
                "CRÍTICO: Erro ao atualizar balance durante inicialização",
                error=str(balance_error),
            )
            raise

    @track_component("trading_coordinator")
    async def initialize(self):
        production(" INICIANDO TradingCoordinator.initialize()...")
        initialized_components = []

        try:
            await self._initialize_core_components(initialized_components)
            await self._initialize_analysis_components(initialized_components)
            await self._initialize_position_management(initialized_components)
            await self._initialize_monitors(initialized_components)
            await self._restore_and_validate_positions(initialized_components)
            await self._update_balance_and_metrics(initialized_components)

            initialized_components.append("data_manager")
            production(" DataManager já configurado anteriormente")

            self.coordinator.running = True
            production(
                "✅ Trading Coordinator inicializado com sucesso",
                components=len(initialized_components),
            )

        except Exception as e:
            error(
                "CRÍTICO: Falha na inicialização - iniciando rollback",
                error=str(e),
                initialized_components=initialized_components,
            )

            await self._rollback_initialization(initialized_components)

            self.coordinator.running = False
            error("Sistema não está operacional - inicialização falhou")
            raise RuntimeError(f"Inicialização crítica falhou: {str(e)}") from e

    async def _rollback_initialization(self, initialized_components: list[str]):
        try:
            production("Iniciando rollback de componentes inicializados...")

            if (
                "data_manager" in initialized_components
                and self.coordinator.data_manager
            ):
                try:
                    self.coordinator.data_manager.set_client(None)
                    production(" DataManager limpo")
                except Exception as e:
                    warning("Erro ao limpar DataManager", error=str(e))

            if "db" in initialized_components and self.coordinator.db:
                try:
                    await self.coordinator.db.close()
                    self.coordinator.db = None
                    production(" Database fechado")
                except Exception as e:
                    warning("Erro ao fechar database", error=str(e))

            if "client" in initialized_components and self.coordinator.client:
                try:
                    await self.coordinator.client.close()
                    self.coordinator.client = None
                    production(" Cliente Binance fechado")
                except Exception as e:
                    warning("Erro ao fechar cliente", error=str(e))

            self.coordinator.executor = None
            self.coordinator.risk_manager = None
            self.coordinator.market_analyzer = None
            self.coordinator.scoring_system = None
            self.coordinator.indicators = None
            self.coordinator.position_manager = None
            self.coordinator.signal_analyzer = None
            self.coordinator.unified_monitor = None
            self.coordinator.performance_monitor = None

            production("Rollback completado - todos os componentes limpos")

        except Exception as e:
            error("Erro durante rollback", error=str(e))

    @track_component("trading_coordinator")
    async def shutdown(self):
        try:
            production("Iniciando shutdown do coordenador...")

            await self.coordinator.orchestration.stop_trading_operations()
            await self._handle_emergency_close()
            await self._cleanup_resources()
            self._log_shutdown_summary()

        except Exception as e:
            error("CRÍTICO: Erro no shutdown", error=str(e))

    async def _handle_emergency_close(self) -> None:

        if not (self.coordinator.position_manager and self.coordinator.client):
            return

        positions = await self.coordinator.position_manager.get_all_positions()
        if not positions:
            production(" Nenhuma posição aberta no shutdown")
            return

        production(
            "ALERTA: Posições abertas detectadas no shutdown", count=len(positions)
        )

        emergency_close = self.coordinator.config.get("trading", {}).get(
            "emergency_close_on_shutdown", True
        )

        if emergency_close:
            await self._execute_emergency_closes(positions)
        else:
            self._log_open_positions_warning(positions)

    async def _execute_emergency_closes(self, positions: dict) -> None:

        if not self.coordinator.position_manager or not hasattr(
            self.coordinator.position_manager, "close_position"
        ):
            error("CRÍTICO: PositionManager não disponível para emergency close")
            return

        production("Fechando posições de emergência...", count=len(positions))

        successful_closes = 0
        failed_closes = 0

        for symbol, position in positions.items():
            result = await self._close_single_position_emergency(symbol, position)
            if result:
                successful_closes += 1
            else:
                failed_closes += 1

        production(
            f"Emergency close: {successful_closes} sucessos, {failed_closes} falhas"
        )

        await self._verify_positions_closed(successful_closes, failed_closes)

    async def _close_single_position_emergency(
        self, symbol: str, position: dict
    ) -> bool:

        try:
            if not self.coordinator.validators.validate_financial_data(
                position, f"posição {symbol}", ["quantity", "entry_price"]
            ):
                error(f"Posição {symbol} tem dados inválidos - pulando")
                return False

            current_price = self._get_valid_price_for_close(symbol, position)
            if current_price <= 0:
                error(f"Preço inválido para {symbol} - pulando")
                return False

            production(f"Fechando posição de emergência: {symbol}")

            try:
                async with asyncio.timeout(30.0):
                    result = await self.coordinator.position_manager.close_position(
                        symbol, "EMERGENCY_SHUTDOWN", current_price
                    )

                if result:
                    production(
                        f"✓ Posição {symbol} fechada com PnL: {result.get('pnl', 0):.4f}"
                    )
                    return True

                error(f"✗ Falha ao fechar posição {symbol}")
                return False

            except TimeoutError:
                error(f"CRÍTICO: Timeout ao fechar posição {symbol}")
                return False

        except Exception as e:
            error("CRÍTICO: Exceção ao fechar posição", symbol=symbol, error=str(e))
            return False

    def _get_valid_price_for_close(self, symbol: str, position: dict) -> float:
        from decimal import Decimal

        current_price = position.get("current_price", position.get("entry_price", 0))

        if not isinstance(current_price, (int, float, Decimal)) or current_price <= 0:
            current_price = position.get("entry_price", 0)

        return float(current_price) if current_price > 0 else 0

    async def _verify_positions_closed(self, successful: int, failed: int) -> None:
        remaining = await self.coordinator.position_manager.get_all_positions()

        if not remaining:
            production(" Todas as posições foram fechadas com sucesso")
            return

        error(
            "CRÍTICO: Posições ainda abertas após emergency close",
            count=len(remaining),
            successful=successful,
            failed=failed,
        )
        for symbol, position in remaining.items():
            error(
                "Posição não fechada",
                symbol=symbol,
                quantity=position["quantity"],
                entry=position["entry_price"],
            )

    def _log_open_positions_warning(self, positions: dict) -> None:

        warning("AVISO: Posições ficaram abertas (emergency_close = False)")

        for symbol, position in positions.items():
            pnl_unrealized = self._calculate_unrealized_pnl(position)
            warning(
                "Posição aberta no shutdown",
                symbol=symbol,
                quantity=position["quantity"],
                entry=position["entry_price"],
                current_price=position.get("current_price", "N/A"),
                unrealized_pnl=pnl_unrealized,
            )

        self._log_shutdown_protections()

    def _calculate_unrealized_pnl(self, position: dict) -> float:
        from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

        if "current_price" not in position or "entry_price" not in position:
            return 0

        try:
            current = to_decimal(position["current_price"])
            entry = to_decimal(position["entry_price"])
            quantity = to_decimal(position["quantity"])
            pnl = ((current - entry) * quantity).quantize(
                Decimal("0.00000001"), rounding=ROUND_HALF_UP
            )
            return float(pnl)
        except (InvalidOperation, ValueError):
            return 0

    def _log_shutdown_protections(self) -> None:
        shutdown_safety = self.coordinator.config.get("shutdown_safety", {})
        maintain_stops = shutdown_safety.get("maintain_stops_active", True)
        maintain_takes = shutdown_safety.get("maintain_takes_active", True)

        production(
            "PROTEÇÕES ATIVAS DURANTE DOWNTIME",
            stop_loss_active=maintain_stops,
            take_profit_active=maintain_takes,
        )

        if maintain_stops and maintain_takes:
            production("✓ Stop-loss e take-profit permanecem funcionais")
        else:
            warning("⚠️ Algumas proteções podem estar desabilitadas")

    async def _cleanup_resources(self) -> None:
        if self.coordinator.metrics_scheduler:
            try:
                await self.coordinator.metrics_scheduler.stop()
                production(" MetricsScheduler parado")
            except Exception as e:
                error("Erro ao parar MetricsScheduler", error=str(e))

        if self.coordinator.db:
            try:
                await self.coordinator.db.close()
                production(" Database fechado")
            except Exception as e:
                error("Erro ao fechar database", error=str(e))

        if self.coordinator.client:
            try:
                await self.coordinator.client.close()
                production(" Cliente Binance fechado")
            except Exception as e:
                error("Erro ao fechar cliente", error=str(e))

    def _log_shutdown_summary(self) -> None:
        production(" Coordenador encerrado")

    def request_shutdown(self):
        production("Shutdown solicitado - iniciando processo graceful")
        self.coordinator.shutdown_event.set()
