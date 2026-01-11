from datetime import datetime
from typing import Any

from shared.observability.flow_tracker import track_component
from shared.observability.logger import debug, error, production


class CircuitBreaker:
    def __init__(self, config: dict, db: Any | None = None) -> None:
        risk_config = config.get("risk", {})
        cb_config = config.get("circuit_breaker", {})

        self.daily_loss_limit = risk_config.get("daily_loss_limit_pct", 8.0)

        is_testnet = config.get("mode", "").lower() == "testnet"
        self.max_consecutive_losses = (
            999 if is_testnet else risk_config.get("max_consecutive_losses", 5)
        )

        self.max_drawdown_pct = risk_config.get("max_drawdown_pct", 15.0)
        self.cooldown_minutes = cb_config.get("cooldown_minutes", 30)

        self.is_open = False
        self.triggered_at = None
        self.trigger_reason = ""
        self.db = db

        # Lock para proteger state contra race conditions
        import asyncio

        self._state_lock = asyncio.Lock()

        production(
            "Circuit Breaker inicializado",
            mode="TESTNET" if is_testnet else "PRODUCTION",
            daily_loss_limit=self.daily_loss_limit,
            max_consecutive_losses=self.max_consecutive_losses,
            cooldown_minutes=self.cooldown_minutes,
            persistence_enabled=db is not None,
        )

    async def init_state(self):
        await self._load_state()

    async def _save_state(self):
        if not self.db:
            return

        try:
            async with self.db.pool.acquire() as conn:
                triggered_at_str = (
                    self.triggered_at.isoformat() if self.triggered_at else None
                )
                updated_at_str = datetime.now().isoformat()

                await conn.execute(
                    """INSERT OR REPLACE INTO circuit_breaker_state
                       (id, is_open, triggered_at, trigger_reason, updated_at)
                       VALUES (1, ?, ?, ?, ?)""",
                    (
                        1 if self.is_open else 0,
                        triggered_at_str,
                        self.trigger_reason,
                        updated_at_str,
                    ),
                )
                await conn.commit()
                debug(
                    "Circuit breaker state saved",
                    is_open=self.is_open,
                    reason=self.trigger_reason,
                )
        except Exception as e:
            error("Failed to save circuit breaker state", exception=str(e))

    async def _load_state(self):
        if not self.db:
            return

        try:
            async with self.db.pool.acquire() as conn:
                cursor = await conn.execute(
                    "SELECT is_open, triggered_at, trigger_reason FROM circuit_breaker_state WHERE id = 1"
                )
                row = await cursor.fetchone()

                if row:
                    self.is_open = bool(row[0])
                    self.triggered_at = (
                        datetime.fromisoformat(row[1]) if row[1] else None
                    )
                    self.trigger_reason = row[2] if row[2] else ""

                    if self.is_open:
                        production(
                            "Circuit breaker state restored from database",
                            is_open=self.is_open,
                            triggered_at=self.triggered_at,
                            reason=self.trigger_reason,
                        )
        except Exception as e:
            debug(
                "No previous circuit breaker state found or failed to load",
                exception=str(e),
            )

    @track_component("circuit_breaker")
    async def check_triggers(
        self, daily_pnl_pct: float, consecutive_losses: int, current_drawdown: float = 0
    ) -> bool:
        if self.is_open:
            return True

        # Check daily loss limit
        if daily_pnl_pct <= -self.daily_loss_limit:
            await self.trigger(f"Daily loss limit exceeded: {daily_pnl_pct:.2f}%")
            return True

        # Check consecutive losses
        if consecutive_losses >= self.max_consecutive_losses:
            await self.trigger(f"Max consecutive losses: {consecutive_losses}")
            return True

        # Check maximum drawdown
        if current_drawdown >= self.max_drawdown_pct:
            await self.trigger(f"Max drawdown exceeded: {current_drawdown:.2f}%")
            return True

        return False

    @track_component("circuit_breaker")
    async def trigger(self, reason: str):
        async with self._state_lock:
            if not self.is_open:
                self.is_open = True
                self.triggered_at = datetime.now()
                self.trigger_reason = reason
                error("🚨 CIRCUIT BREAKER TRIGGERED", reason=reason)

                if self.db:
                    try:
                        await self._save_state()
                    except Exception as e:
                        error(
                            "Failed to save circuit breaker state after trigger",
                            exception=str(e),
                        )

    @track_component("circuit_breaker", slow_threshold=1)
    async def can_execute(self) -> bool:
        async with self._state_lock:
            if not self.is_open:
                return True

            # Check if cooldown period has passed
            if self.triggered_at:
                elapsed_minutes = (
                    datetime.now() - self.triggered_at
                ).total_seconds() / 60
                if elapsed_minutes >= self.cooldown_minutes:
                    # Reset internamente (já estamos dentro do lock)
                    await self._reset_internal()
                    return True

            return False

    async def _reset_internal(self):
        """Reset interno (sem lock - assume que chamador já tem lock)"""
        if self.is_open:
            production("Circuit Breaker reset - resuming trading")
        self.is_open = False
        self.triggered_at = None
        self.trigger_reason = ""

        if self.db:
            try:
                await self._save_state()
            except Exception as e:
                error(
                    "Failed to save circuit breaker state after reset", exception=str(e)
                )

    @track_component("circuit_breaker")
    async def reset(self):
        """Reset público (com lock)"""
        async with self._state_lock:
            await self._reset_internal()

    def record_success(self):
        pass

    @track_component("circuit_breaker")
    def record_failure(self):
        pass

    @track_component("circuit_breaker", slow_threshold=1)
    def is_triggered(self) -> bool:
        return self.is_open

    def is_in_grace_period(self) -> bool:
        return False

    def get_reason(self) -> str:
        return self.trigger_reason if self.trigger_reason else ""

    def get_remaining_time(self) -> float:
        if not self.is_open or not self.triggered_at:
            return 0.0

        elapsed_minutes = (datetime.now() - self.triggered_at).total_seconds() / 60
        return max(0.0, self.cooldown_minutes - elapsed_minutes)

    def get_status(self) -> dict:
        remaining_time = 0
        if self.is_open and self.triggered_at:
            elapsed_minutes = (datetime.now() - self.triggered_at).total_seconds() / 60
            remaining_time = max(0, self.cooldown_minutes - elapsed_minutes)

        return {
            "is_triggered": self.is_open,
            "can_execute": self.can_execute(),
            "trigger_reason": self.trigger_reason,
            "remaining_time_minutes": remaining_time,
        }
