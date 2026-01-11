import asyncio
from datetime import datetime

from config.config_loader import load_config
from core.validators.trading_validator import TradingValidator
from shared.observability.flow_tracker import track_component
from shared.observability.logger import debug, error, production, warning


class BotLifecycleMixin:
    def __init__(self) -> None:
        self.coordinator = None
        self.health_monitor = None
        self.maintenance = None
        self.websocket_manager = None
        self.config = None
        self.data_fetcher = None

        try:
            self.config = load_config()
            production("Configuração carregada no BotOrchestrator")

            trading_pairs = self.config.get("trading_pairs", [])
            if trading_pairs:
                TradingValidator.set_symbol_whitelist(trading_pairs)
                production(
                    f"Symbol whitelist configurada: {len(trading_pairs)} símbolos"
                )
            else:
                warning(
                    "Nenhum trading_pairs configurado - validação de whitelist desabilitada"
                )
        except Exception as e:
            error("CRÍTICO: Falha ao carregar configuração", error=str(e))
            self.config = None

        self.shutdown_event = asyncio.Event()
        self.tasks = []

        self._global_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._task_lock = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()

        self._running = False
        self._shutdown_requested = False
        self._shutdown_in_progress = False
        self._initialized = False
        self._initializing = False
        self._startup_time = None
        self._shutdown_time = None
        self._shutdown_mode = None
        self._emergency_signals_count = 0

        self._initialization_order = []
        self._shutdown_timeouts = {
            "graceful": {"total": 30.0, "component": 8.0},
            "emergency": {"total": 15.0, "component": 3.0},
            "forced": {"total": 5.0, "component": 1.0},
        }

        production("BotOrchestrator inicializado com shutdown avançado")

    @property
    def running(self):
        return self._running

    @running.setter
    def running(self, value):
        if not isinstance(value, bool):
            error(
                "CRÍTICO: Running state deve ser boolean", value=value, type=type(value)
            )
            return

        old_value = self._running
        self._running = value

        if old_value != value:
            debug("Estado running alterado", from_value=old_value, to_value=value)
            if value:
                self._startup_time = datetime.now()
            else:
                self._shutdown_time = datetime.now()

    @property
    def shutdown_requested(self):
        return self._shutdown_requested

    @shutdown_requested.setter
    def shutdown_requested(self, value):
        if not isinstance(value, bool):
            error(
                "CRÍTICO: Shutdown requested deve ser boolean",
                value=value,
                type=type(value),
            )
            return

        old_value = self._shutdown_requested
        self._shutdown_requested = value

        if old_value != value and value:
            debug("Shutdown solicitado", timestamp=datetime.now())
            try:
                self.shutdown_event.set()
            except Exception as e:
                error("Erro ao sinalizar shutdown event", error=str(e))

    def _log_startup_info(self):
        production("═" * 60)
        production("🚀 INICIANDO TRADING BOT")
        production("═" * 60)

    def _finalize_initialization(self) -> None:
        self._initialized = True
        self._initializing = False
        production("✅ Inicialização completa")

    @track_component("bot_orchestrator", slow_threshold=30)
    async def initialize(self) -> bool:
        production("🔧 BotOrchestrator.initialize() INICIADO")
        production(
            f"🐛 DEBUG: _initialized={self._initialized}, _initializing={self._initializing}"
        )
        async with self._global_lock:
            if self._initialized:
                warning("Bot já está inicializado")
                return True

            if self._initializing:
                warning("Inicialização já em andamento")
                return False

            self._initializing = True
            production("🐛 DEBUG: Lock adquirido, _initializing=True")

        try:
            production("📦 Iniciando setup de dependências...")
            deps_result = await self._setup_dependencies()
            production(f"🐛 DEBUG: _setup_dependencies() retornou: {deps_result}")
            if not deps_result:
                raise RuntimeError("Falha no setup de dependências")

            production("🎮 Iniciando componentes de trading...")
            trading_result = await self._initialize_trading_components()
            production(
                f"🐛 DEBUG: _initialize_trading_components() retornou: {trading_result}"
            )
            if not trading_result:
                raise RuntimeError("Falha na inicialização de componentes de trading")
            production("✅ Componentes de trading inicializados com sucesso")

            production("📊 Configurando monitoramento...")
            production("🐛 DEBUG: Chamando _setup_monitoring()...")
            monitoring_result = await self._setup_monitoring()
            production(f"🐛 DEBUG: _setup_monitoring() retornou: {monitoring_result}")
            if not monitoring_result:
                warning("Monitoramento não pôde ser configurado - continuando sem ele")
            else:
                production("✅ Monitoramento configurado com sucesso")

            # Fase 2.1: Parallel Reconciliation (+700ms ganho)
            # Operações read-only independentes executadas em paralelo
            production("🔄 Reconciliando estado e ordens em paralelo...")
            results = await asyncio.gather(
                self._reconcile_state_on_startup(),
                self._reconcile_exchange_orders_on_startup(),
                return_exceptions=True,
            )

            state_ok, orders_ok = results

            # Handle exceptions
            if isinstance(state_ok, Exception):
                error(
                    "Reconciliação de estado falhou",
                    error=str(state_ok),
                )
                state_ok = False
            if isinstance(orders_ok, Exception):
                error(
                    "Reconciliação de ordens falhou",
                    error=str(orders_ok),
                )
                orders_ok = False

            # Warnings para operações que falharam
            if not state_ok:
                warning("Reconciliação de estado falhou - iniciando com state limpo")
            if not orders_ok:
                warning("Reconciliação de ordens falhou - ordens órfãs podem existir")

            production("💰 Verificando balance inicial...")
            balance = await self._check_initial_balance()
            if balance is None or balance <= 0:
                raise RuntimeError("Balance inicial inválido ou insuficiente")

            self._finalize_initialization()
            return True

        except Exception as e:
            error("CRÍTICO: Falha na inicialização", error=str(e))
            await self._rollback_initialization(self._initialization_order)
            self._initializing = False
            return False

    async def start(self):
        production("🚀 BotOrchestrator.start() INICIADO")
        if not self._initialized:
            production("Bot não inicializado, iniciando inicialização automática...")
            if not await self.initialize():
                error("Falha na inicialização automática do bot")
                return

        self.running = True
        production("🎯 Bot iniciado e operacional")

        try:
            self.tasks.append(asyncio.create_task(self._shutdown_monitor()))

            if self.coordinator:
                self.tasks.append(
                    asyncio.create_task(
                        self._run_with_error_handling(
                            self.coordinator.run(), "Coordenador"
                        )
                    )
                )

            if self.maintenance:
                self.tasks.append(
                    asyncio.create_task(
                        self._run_with_error_handling(
                            self.maintenance.run(), "Maintenance"
                        )
                    )
                )

            self.tasks.append(
                asyncio.create_task(
                    self._run_with_error_handling(self._sync_state_task(), "State Sync")
                )
            )

            if self.websocket_manager:
                self.tasks.append(
                    asyncio.create_task(
                        self._run_with_error_handling(
                            self.websocket_manager.start(), "WebSocket"
                        )
                    )
                )

            await self.shutdown_event.wait()

        except Exception as e:
            error("Erro durante execução do bot", error=str(e))
        finally:
            await self.shutdown()

    async def _run_with_error_handling(self, coro, task_name: str):
        try:
            await coro
        except Exception as e:
            error(f"Erro na task {task_name}", error=str(e))

    async def _sync_state_task(self):
        """
        SSOT: Sincroniza state (cache) com database (fonte de verdade) periodicamente.
        Previne dessincronia entre state e DB.
        """
        from shared.types.state import state

        while not self.shutdown_requested:
            try:
                await asyncio.sleep(30)

                if self.shutdown_requested:
                    break

                if hasattr(self, "db_handler") and self.db_handler:
                    synced = await state.sync_positions_from_db(self.db_handler)
                    if synced > 0:
                        debug(f"Sync state ← DB: {synced} alterações")

            except asyncio.CancelledError:
                debug("Sync task cancelada - shutdown iniciado")
                break
            except Exception as e:
                error("Erro no sync task", error=str(e))
                await asyncio.sleep(60)

        debug("Sync task finalizando graciosamente")

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            if self._shutdown_in_progress:
                debug("Shutdown já em progresso")
                return
            self._shutdown_in_progress = True

        production("🛑 Iniciando shutdown do bot")

        try:
            self.running = False

            await self._persist_state()
            await self._close_positions_safely()
            await self._cleanup_resources(self._initialization_order)

            for task in self.tasks:
                if not task.done():
                    task.cancel()

            production("✅ Shutdown completo")

        except Exception as e:
            error("Erro durante shutdown", error=str(e))
        finally:
            self._shutdown_in_progress = False
