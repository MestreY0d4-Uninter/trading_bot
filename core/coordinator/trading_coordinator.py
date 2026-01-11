import asyncio
from datetime import datetime
from typing import Any

from core.coordinator.trading_coordinator_balance import TradingCoordinatorBalance
from core.coordinator.trading_coordinator_lifecycle import TradingCoordinatorLifecycle
from core.coordinator.trading_coordinator_orchestration import (
    TradingCoordinatorOrchestration,
)
from core.coordinator.trading_coordinator_validators import TradingCoordinatorValidators
from shared.observability.flow_tracker import track_component
from shared.observability.logger import production


class TradingCoordinator:
    def __init__(
        self, config: dict, data_fetcher: Any, websocket_manager: Any | None = None
    ) -> None:
        if not config or not isinstance(config, dict):
            raise ValueError("Config deve ser um dict válido")
        if not data_fetcher:
            raise ValueError("Data fetcher é obrigatório")

        self.config = config
        self.data_manager = data_fetcher
        self.websocket_manager = websocket_manager

        self.client = None
        self.executor = None
        self.risk_manager = None
        self.market_analyzer = None
        self.scoring_system = None
        self.indicators = None
        self.db = None
        self.position_manager = None
        self.signal_analyzer = None
        self.unified_monitor = None
        self.performance_monitor = None
        self.metrics_scheduler = None
        self.correlation_validator = None

        self.tasks: list[asyncio.Task] = []

        self.running = False
        self.shutdown_event = asyncio.Event()
        self.start_time = datetime.now()

        self.check_interval = self.config.get("trading", {}).get("check_interval", 15)

        self.global_entry_semaphore = asyncio.Semaphore(1)

        self.lifecycle = TradingCoordinatorLifecycle(self)
        self.balance = TradingCoordinatorBalance(self)
        self.orchestration = TradingCoordinatorOrchestration(self)
        self.validators = TradingCoordinatorValidators(self)

        production(
            "Trading Coordinator V2 inicializado", check_interval=self.check_interval
        )

    def set_websocket_manager(self, websocket_manager):
        self.websocket_manager = websocket_manager
        if websocket_manager:
            production("WebSocket manager set on TradingCoordinator")

    async def initialize(self):
        return await self.lifecycle.initialize()

    async def run(self):
        return await self.orchestration.run()

    @track_component("trading_coordinator", slow_threshold=100)
    async def update_account_balance(self):
        return await self.balance.update_account_balance()

    async def get_current_prices(self, symbols: list[str]) -> dict[str, float]:
        return await self.balance.get_current_prices(symbols)

    async def get_status(self) -> dict:
        return await self.orchestration.get_status()

    async def shutdown(self):
        return await self.lifecycle.shutdown()

    def request_shutdown(self):
        return self.lifecycle.request_shutdown()
