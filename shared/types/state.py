from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.position.position_tracker import PositionTracker

import asyncio
import time
import uuid

from shared.constants import API_RESET_INTERVAL
from shared.observability.flow_tracker import track_component
from shared.observability.logger import production
from utils.decimal_math import to_decimal


class _GlobalState:
    """Global state manager (module-level singleton).

    Private class - use the module-level `state` instance instead of instantiating directly.
    """

    def __init__(self, config: dict | None = None) -> None:
        self._lock = asyncio.Lock()
        self._config = config or {}
        self._instance_id = str(uuid.uuid4())[:8]

        # Position tracker will be injected via set_position_tracker()
        # to avoid circular dependency (shared -> core)
        self._position_tracker: PositionTracker | None = None

        self._metrics = {
            "daily_pnl": Decimal("0"),
            "daily_pnl_pct": Decimal("0"),
            "total_trades": 0,
            "winning_trades": 0,
            "consecutive_losses": 0,
            "last_trade_time": None,
            "api_calls_count": 0,
            "starting_balance": Decimal("0"),
            "current_balance": Decimal("0"),
            "total_equity": Decimal("0"),
            "win_rate": Decimal("0"),
        }

        self._market_data: dict[str, Any] = {}
        self._market_conditions: dict[str, Any] = {}
        self._cooldowns: dict[str, float] = {}
        self._last_api_reset = time.time()

        # Initialize account data for snapshots
        self._account: dict[str, Any] = {
            "balance": Decimal("0"),
            "equity": Decimal("0"),
            "last_update": None,
        }

        production(f" _GlobalState initialized [ID: {self._instance_id}]")

        # Position restoration handled by TradingCoordinator.initialize()
        # See: core/coordinator/trading_coordinator_lifecycle.py:207-336

    def set_position_tracker(self, tracker: PositionTracker) -> None:
        """Inject PositionTracker dependency (called by TradingCoordinatorLifecycle)"""
        if self._position_tracker is not None:
            from shared.observability.logger import warning

            warning(
                "PositionTracker already set - overwriting",
                instance_id=self._instance_id,
            )
        self._position_tracker = tracker
        production(f"PositionTracker injected into state [ID: {self._instance_id}]")

    def _get_position_tracker(self) -> PositionTracker:
        # Position tracker must be injected before use
        if self._position_tracker is None:
            raise RuntimeError(
                "PositionTracker not injected! Call state.set_position_tracker() first"
            )
        return self._position_tracker

    async def get_position(self, symbol: str) -> dict[str, Any] | None:
        return await self._get_position_tracker().get_position(symbol)

    @track_component("state")
    async def set_position(self, symbol: str, position_data: dict[str, Any]) -> None:
        tracker = self._get_position_tracker()
        if await tracker.has_position(symbol):
            await tracker.update_position(symbol, position_data)
        else:
            await tracker.add_position(symbol, position_data)

    @track_component("state")
    async def remove_position(self, symbol: str) -> dict[str, Any] | None:
        return await self._get_position_tracker().remove_position(symbol)

    @track_component("state", slow_threshold=1)
    async def get_all_positions(self) -> dict[str, dict[str, Any]]:
        return await self._get_position_tracker().get_all_positions()

    async def has_position(self, symbol: str) -> bool:
        return await self._get_position_tracker().has_position(symbol)

    async def get_position_count(self) -> int:
        return await self._get_position_tracker().get_position_count()

    async def clear_all_positions(self) -> None:
        positions = await self.get_all_positions()
        for symbol in list(positions.keys()):
            await self.remove_position(symbol)

    async def get_account(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "balance": self._metrics["current_balance"],
                "equity": self._metrics["total_equity"],
                "last_update": datetime.now(),
            }

    async def update_account(self, balance: Decimal, equity: Decimal) -> None:
        async with self._lock:
            balance_decimal = to_decimal(balance)
            equity_decimal = to_decimal(equity)

            self._account = {
                "balance": balance_decimal,
                "equity": equity_decimal,
                "last_update": datetime.now(),
            }

            self._metrics["current_balance"] = balance_decimal
            self._metrics["total_equity"] = equity_decimal

    @track_component("state")
    async def update_metric(self, key: str, value: Any) -> None:
        async with self._lock:
            if key in self._metrics:
                if isinstance(value, (int, float)) and key != "last_trade_time":
                    self._metrics[key] = (
                        to_decimal(value)
                        if "pnl" in key
                        or "balance" in key
                        or "equity" in key
                        or "rate" in key
                        else value
                    )
                else:
                    self._metrics[key] = value

    async def get_metric(self, key: str) -> Any:
        async with self._lock:
            return self._metrics.get(key)

    async def get_all_metrics(self) -> dict[str, Any]:
        async with self._lock:
            return dict(self._metrics)

    async def update_market_data(self, symbol: str, data: dict[str, Any]) -> None:
        async with self._lock:
            self._market_data[symbol] = {**data, "timestamp": datetime.now()}

    async def get_market_data(self, symbol: str) -> dict[str, Any] | None:
        async with self._lock:
            return self._market_data.get(symbol)

    async def set_market_condition(self, symbol: str, condition: str) -> None:
        async with self._lock:
            self._market_conditions[symbol] = {
                "condition": condition,
                "timestamp": datetime.now(),
            }

    async def get_market_condition(self, symbol: str) -> str | None:
        async with self._lock:
            data = self._market_conditions.get(symbol)
            if data:
                return data["condition"]
            return None

    async def set_cooldown(self, symbol: str, until: datetime) -> None:
        async with self._lock:
            self._cooldowns[symbol] = until

    async def is_in_cooldown(self, symbol: str) -> bool:
        async with self._lock:
            if symbol not in self._cooldowns:
                return False
            return datetime.now() < self._cooldowns[symbol]

    async def increment_api_calls(self) -> None:
        async with self._lock:
            current_time = time.time()
            if current_time - self._last_api_reset > API_RESET_INTERVAL:
                self._metrics["api_calls_count"] = 0
                self._last_api_reset = current_time
            else:
                self._metrics["api_calls_count"] += 1

    async def get_api_calls_count(self) -> int:
        async with self._lock:
            return self._metrics["api_calls_count"]

    async def clear_runtime_data(self) -> None:
        async with self._lock:
            self._market_data.clear()
            self._market_conditions.clear()
            self._cooldowns.clear()
            production("Runtime data cleared")

    async def sync_positions_from_db(self, db_handler) -> int:
        """
        SSOT: Sincroniza state (cache) com database (fonte de verdade).
        Chamado periodicamente para manter state atualizado.

        Returns:
            Número de posições sincronizadas
        """
        if not db_handler:
            return 0

        try:
            db_positions = await db_handler.get_open_trades()
            state_positions = await self.get_all_positions()

            synced = 0

            db_symbols = {p["symbol"] for p in db_positions}
            state_symbols = set(state_positions.keys())

            for db_pos in db_positions:
                symbol = db_pos["symbol"]
                if symbol not in state_symbols:
                    position_data = {**db_pos}
                    await self.set_position(symbol, position_data)
                    synced += 1
                    from shared.observability.logger import debug

                    debug(f"Posição adicionada ao state via sync: {symbol}")

            for symbol in state_symbols:
                if symbol not in db_symbols:
                    await self.remove_position(symbol)
                    synced += 1
                    from shared.observability.logger import debug

                    debug(f"Posição removida do state via sync: {symbol}")

            if synced > 0:
                from shared.observability.logger import production

                production(
                    f"State sincronizado com DB: {synced} mudanças",
                    db_count=len(db_positions),
                    state_count=len(await self.get_all_positions()),
                )

            return synced

        except Exception as e:
            from shared.observability.logger import error

            error("Erro ao sincronizar state com DB", error=str(e))
            return 0

    async def increment_metric(
        self, key: str, increment: Decimal = Decimal("1.0")
    ) -> None:
        """Incrementa valor de uma métrica"""
        async with self._lock:
            if key in self._metrics:
                current_value = self._metrics[key]
                if isinstance(current_value, (int, float)) and key != "last_trade_time":
                    if (
                        "pnl" in key
                        or "balance" in key
                        or "equity" in key
                        or "rate" in key
                    ):
                        self._metrics[key] = to_decimal(current_value) + to_decimal(
                            increment
                        )
                    else:
                        self._metrics[key] = current_value + increment
                else:
                    self._metrics[key] = increment
            else:
                if "pnl" in key or "balance" in key or "equity" in key or "rate" in key:
                    self._metrics[key] = to_decimal(increment)
                else:
                    self._metrics[key] = increment

    async def get_dashboard_data(self, symbol: str) -> dict[str, Any] | None:
        """Obtém dados de mercado formatados para dashboard"""
        async with self._lock:
            market_data = self._market_data.get(symbol)
            if not market_data:
                return None

            condition_data = self._market_conditions.get(symbol)
            condition = condition_data["condition"] if condition_data else "normal"

            volume_24h = market_data.get(
                "volume_24h_usd", market_data.get("volume_24h", 0)
            )

            return {
                "price": market_data.get("price", 0),
                "volume_24h": volume_24h,
                "spread_pct": market_data.get("spread_pct", 0),
                "condition": condition,
                "timestamp": market_data.get("timestamp"),
                "bid": market_data.get("bid", 0),
                "ask": market_data.get("ask", 0),
                "change_24h": market_data.get("change_24h", 0),
                "high_24h": market_data.get("high_24h", 0),
                "low_24h": market_data.get("low_24h", 0),
            }

    async def get_balance_copy(self) -> dict[str, Any]:
        """Retorna cópia dos dados de conta para snapshots"""
        async with self._lock:
            return {
                "balance": self._account["balance"],
                "equity": self._account["equity"],
                "last_update": self._account["last_update"],
                "snapshot_time": datetime.now(),
            }

    async def get_duration_metrics(self) -> dict[str, Any]:
        """Calcula métricas de duração das posições"""
        positions = await self._get_position_tracker().get_all_positions()

        if not positions:
            return {
                "total_positions": 0,
                "avg_duration_minutes": 0,
                "max_duration_minutes": 0,
                "min_duration_minutes": 0,
                "positions_over_target": 0,
                "target_duration_minutes": 480,
            }

        now = datetime.now()
        durations = []
        target_duration_minutes = 480

        for position in positions.values():
            entry_time = position.get("entry_time")
            if isinstance(entry_time, str):
                entry_time = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
            elif not isinstance(entry_time, datetime):
                continue

            duration = (now - entry_time).total_seconds() / 60
            durations.append(duration)

        positions_over_target = sum(1 for d in durations if d > target_duration_minutes)

        return {
            "total_positions": len(positions),
            "avg_duration_minutes": (
                sum(durations) / len(durations) if durations else 0
            ),
            "max_duration_minutes": max(durations) if durations else 0,
            "min_duration_minutes": min(durations) if durations else 0,
            "positions_over_target": positions_over_target,
            "target_duration_minutes": target_duration_minutes,
        }

    async def get_active_positions(self) -> dict[str, dict[str, Any]]:
        """Retorna apenas posições ativas (alias para get_all_positions)"""
        return await self.get_all_positions()

    async def get_all_positions_with_duration(self) -> dict[str, dict[str, Any]]:
        """Retorna todas as posições com informações de duração calculadas"""
        positions = await self._get_position_tracker().get_all_positions()
        positions_with_duration = {}

        now = datetime.now()

        for symbol, position in positions.items():
            position_copy = position.copy()

            entry_time = position.get("entry_time")
            if isinstance(entry_time, str):
                entry_time = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
            elif not isinstance(entry_time, datetime):
                entry_time = now

            duration_seconds = (now - entry_time).total_seconds()
            duration_minutes = duration_seconds / 60
            duration_hours = duration_minutes / 60

            position_copy.update(
                {
                    "duration_seconds": duration_seconds,
                    "duration_minutes": duration_minutes,
                    "duration_hours": duration_hours,
                    "is_over_target": duration_minutes > 480,
                    "is_stale": duration_hours > 72,
                }
            )

            positions_with_duration[symbol] = position_copy

        return positions_with_duration

    async def get_position_duration_hours(self, symbol: str) -> float:
        """Calcula a duração de uma posição em horas"""
        position = await self.get_position(symbol)
        if not position:
            return 0.0

        entry_time = position.get("entry_time")
        if not entry_time:
            return 0.0

        try:
            if isinstance(entry_time, str):
                entry_time = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
            elif not isinstance(entry_time, datetime):
                return 0.0

            duration = datetime.now() - entry_time
            return duration.total_seconds() / 3600  # Retorna em horas
        except Exception:
            return 0.0

    async def get_position_duration_alert_level(
        self, symbol: str, thresholds: dict[str, int]
    ) -> str:
        """Determina o nível de alerta baseado na duração da posição"""
        duration_hours = await self.get_position_duration_hours(symbol)

        if duration_hours >= thresholds.get("red", 48):
            return "red"
        elif duration_hours >= thresholds.get("orange", 24):
            return "orange"
        elif duration_hours >= thresholds.get("yellow", 12):
            return "yellow"
        else:
            return "green"

    @staticmethod
    def calculate_win_rate(winning_trades: int, total_trades: int) -> Decimal:
        if total_trades == 0:
            return Decimal("0")
        return (Decimal(str(winning_trades)) / Decimal(str(total_trades))) * Decimal(
            "100"
        )


# Module-level singleton (Pythonic pattern)
state = _GlobalState()

# Backward compatibility alias (for @staticmethod access)
GlobalState = _GlobalState
