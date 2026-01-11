import asyncio
from datetime import datetime, timedelta
from typing import Any

from shared.constants import (
    ACTIVITY_LOG_INTERVAL,
    CORRUPTION_THRESHOLD_HOURS,
    ERROR_RECOVERY_DELAY,
    INTER_SYMBOL_DELAY,
    LONG_COOLDOWN_THRESHOLD,
    MAX_CONCURRENT_ANALYSES,
    MAX_COOLDOWN_SLEEP,
    MAX_ENTRIES_PER_MINUTE,
    MIN_GLOBAL_ENTRY_INTERVAL,
    SYMBOL_COOLDOWN_MINUTES,
)
from shared.observability.logger import production
from shared.types.state import state


class TradingLoop:
    def __init__(self, coordinator: Any) -> None:
        if not coordinator:
            raise ValueError("Coordinator é obrigatório para TradingLoop")

        required_components = [
            "config",
            "signal_analyzer",
            "position_manager",
            "risk_manager",
            "data_manager",
        ]
        missing_components = [
            comp
            for comp in required_components
            if not hasattr(coordinator, comp) or getattr(coordinator, comp) is None
        ]

        if missing_components:
            raise AttributeError(
                f"Coordinator ausente componentes críticos: {missing_components}"
            )

        self.coordinator = coordinator
        self.config = coordinator.config
        self.signal_analyzer = coordinator.signal_analyzer
        self.position_manager = coordinator.position_manager
        self.risk_manager = coordinator.risk_manager

        self.check_interval = self.config.get("trading", {}).get("check_interval", 15)
        self.global_cooldown_seconds = self.config.get("strategy", {}).get(
            "cooldown_seconds", 100
        )
        self.symbol_cooldown_seconds = (
            self.config.get("strategy", {}).get(
                "trade_cooldown_minutes", SYMBOL_COOLDOWN_MINUTES
            )
            * 60
        )

        self.activity_log_interval = self.config.get("trading", {}).get(
            "activity_log_interval", ACTIVITY_LOG_INTERVAL
        )
        self.corruption_threshold_hours = self.config.get("trading", {}).get(
            "corruption_threshold_hours", CORRUPTION_THRESHOLD_HOURS
        )
        self.long_cooldown_threshold = self.config.get("trading", {}).get(
            "long_cooldown_threshold", LONG_COOLDOWN_THRESHOLD
        )
        self.max_cooldown_sleep = self.config.get("trading", {}).get(
            "max_cooldown_sleep", MAX_COOLDOWN_SLEEP
        )
        self.inter_symbol_delay = self.config.get("trading", {}).get(
            "inter_symbol_delay", INTER_SYMBOL_DELAY
        )
        self.error_recovery_delay = self.config.get("trading", {}).get(
            "error_recovery_delay", ERROR_RECOVERY_DELAY
        )

        self.symbol_cooldowns = {}
        self._cooldown_lock = asyncio.Lock()
        self.global_cooldown_until = datetime.now()

        self.analysis_semaphore = asyncio.Semaphore(MAX_CONCURRENT_ANALYSES)

        self._entry_locks: dict[str, asyncio.Lock] = {}
        self._lock_creation = asyncio.Lock()

        self.global_entry_semaphore = asyncio.Semaphore(1)
        self.global_entry_history = []

        self.min_global_entry_interval = self.config.get("strategy", {}).get(
            "min_global_entry_interval", MIN_GLOBAL_ENTRY_INTERVAL
        )
        self.max_entries_per_minute = self.config.get("strategy", {}).get(
            "max_entries_per_minute", MAX_ENTRIES_PER_MINUTE
        )
        self._entry_lock = asyncio.Lock()

        self._symbols_no_change = set()
        self._last_no_change_log = 0
        self._repeated_errors = {}

        production(
            "Trading Loop inicializado",
            check_interval=self.check_interval,
            global_cooldown=self.global_cooldown_seconds,
        )

    async def _get_entry_lock(self, symbol: str) -> asyncio.Lock:
        if symbol not in self._entry_locks:
            async with self._lock_creation:
                if symbol not in self._entry_locks:
                    self._entry_locks[symbol] = asyncio.Lock()
        return self._entry_locks[symbol]

    async def _remove_entry_lock(self, symbol: str) -> None:
        async with self._lock_creation:
            if symbol in self._entry_locks:
                lock = self._entry_locks[symbol]
                if not lock.locked():
                    del self._entry_locks[symbol]
                    production(
                        f"Entry lock removed for {symbol}",
                        locks_count=len(self._entry_locks),
                    )

    async def _cleanup_closed_position_locks(self) -> int:
        cleaned = 0
        async with self._lock_creation:
            all_positions = await state.get_all_positions()
            open_symbols = set(all_positions.keys())

            symbols_to_remove = []
            for symbol in list(self._entry_locks.keys()):
                if symbol not in open_symbols:
                    lock = self._entry_locks[symbol]
                    if not lock.locked():
                        symbols_to_remove.append(symbol)

            for symbol in symbols_to_remove:
                del self._entry_locks[symbol]
                cleaned += 1

            if cleaned > 0:
                production(
                    f"Cleaned {cleaned} unused entry locks",
                    remaining=len(self._entry_locks),
                )

        return cleaned

    async def _is_symbol_in_cooldown(self, symbol: str) -> bool:
        async with self._cooldown_lock:
            if symbol in self.symbol_cooldowns:
                if datetime.now() < self.symbol_cooldowns[symbol]:
                    return True

        if await state.is_in_cooldown(symbol):
            return True

        return False

    async def apply_symbol_cooldown(self, symbol: str, seconds: int) -> bool:
        """Public interface to apply cooldown to a symbol.

        This method should be used by external components (e.g., UnifiedMonitor)
        instead of directly accessing _cooldown_lock and symbol_cooldowns.
        """
        async with self._cooldown_lock:
            self.symbol_cooldowns[symbol] = datetime.now() + timedelta(seconds=seconds)
        return True
