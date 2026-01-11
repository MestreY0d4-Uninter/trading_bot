import asyncio

from core.coordinator.trading_coordinator import TradingCoordinator
from infrastructure.data_fetch.data_fetcher import DataFetcher
from infrastructure.websocket.websocket_manager import WebSocketConfig, WebSocketManager
from shared.infra.cache import cache
from shared.observability.flow_tracker import track_component
from shared.observability.logger import debug, error, production
from shared.timeouts import Timeouts

from .maintenance import MaintenanceService


class ComponentManagerMixin:
    @track_component("bot_orchestrator", slow_threshold=10)
    async def _setup_dependencies(self) -> bool:
        try:
            self._log_startup_info()

            production("Inicializando data fetcher")
            if not self.config:
                raise RuntimeError("Config não disponível para DataFetcher")

            self.data_fetcher = DataFetcher(self.config)

            if not self.data_fetcher:
                raise RuntimeError("DataFetcher não foi inicializado corretamente")

            self._initialization_order.append("data_fetcher")
            debug("DataFetcher inicializado com sucesso")
            return True

        except Exception as fetcher_error:
            error("CRÍTICO: Falha ao inicializar DataFetcher", error=str(fetcher_error))
            return False

    @track_component("bot_orchestrator", slow_threshold=15)
    async def _initialize_trading_components(self) -> bool:
        try:
            production("Inicializando coordenador de trading")
            self.coordinator = TradingCoordinator(self.config, self.data_fetcher)

            debug("Inicializando coordenador com timeout estendido")
            async with asyncio.timeout(Timeouts.COMPONENT_STARTUP):
                await self.coordinator.initialize()

            if not self.coordinator.running:
                raise RuntimeError(
                    "Coordenador não está em estado running após inicialização"
                )

            if not self.data_fetcher.client:
                error(
                    "CRÍTICO: DataFetcher client não foi inicializado pelo coordinator"
                )
                return False

            self._initialization_order.append("coordinator")
            production("✓ Coordenador de trading inicializado")
            return True

        except Exception as e:
            error("CRÍTICO: Falha ao inicializar TradingCoordinator", error=str(e))
            return False

    @track_component("bot_orchestrator", slow_threshold=10)
    async def _setup_monitoring(self) -> bool:
        try:
            production("Configurando monitoramento")

            symbols = self.config.get("trading_pairs", [])
            streams = [
                "kline_5m",
                "miniTicker",
            ]  # kline_5m for candles, miniTicker for ticker

            ws_config = WebSocketConfig(
                mode=self.config.get("mode", "testnet"),
                symbols=symbols,
                streams=streams,
            )

            self.websocket_manager = WebSocketManager(ws_config)
            # WebSocketManager não tem initialize(), apenas start() que é chamado depois
            # await self.websocket_manager.initialize()  # REMOVIDO - método não existe
            self._initialization_order.append("websocket_manager")

            self.coordinator.set_websocket_manager(self.websocket_manager)
            production("✓ WebSocket manager vinculado ao coordinator")

            if (
                hasattr(self.coordinator, "unified_monitor")
                and self.coordinator.unified_monitor
            ):
                self.coordinator.unified_monitor.orchestrator = self
                production("✓ Unified Monitor vinculado ao orchestrator")

            if hasattr(self.coordinator, "db") and self.coordinator.db:
                self.maintenance = MaintenanceService(self.coordinator.db, cache)
                self._initialization_order.append("maintenance")
            else:
                debug(
                    "MaintenanceService não inicializado: coordinator.db não disponível"
                )

            production("✓ Sistema de monitoramento ativo")
            return True

        except Exception as e:
            error("Erro ao configurar monitoramento", error=str(e))
            return False

    async def _rollback_initialization(self, initialized_components):
        production(
            f"Revertendo inicialização: {len(initialized_components)} componentes"
        )

        for component_name in reversed(initialized_components):
            try:
                await self._shutdown_component(component_name)
            except Exception as e:
                error(
                    f"Erro ao reverter componente {component_name}",
                    error=str(e),
                )

    async def _cleanup_resources(self, components: list[str]) -> None:
        production(f"Limpando recursos: {len(components)} componentes")

        for component_name in reversed(components):
            try:
                await self._shutdown_component(component_name)
            except Exception as e:
                error(
                    f"Erro ao limpar componente {component_name}",
                    error=str(e),
                )

    async def _shutdown_component(self, component_name: str) -> None:
        try:
            if component_name == "coordinator" and self.coordinator:
                await self.coordinator.shutdown()
                production("✓ Coordinator desligado")

            elif component_name == "websocket_manager" and self.websocket_manager:
                await self.websocket_manager.stop()
                production("✓ WebSocket manager desligado")

            elif component_name == "maintenance" and self.maintenance:
                await self.maintenance.shutdown()
                production("✓ Maintenance desligado")

        except Exception as e:
            error(f"Erro ao desligar {component_name}", error=str(e))
