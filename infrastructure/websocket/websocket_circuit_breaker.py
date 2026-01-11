"""
Circuit Breaker para WebSocket

Detecta falhas contínuas e ativa fallback REST automaticamente.

Estados:
- CLOSED: WebSocket funcionando normalmente
- OPEN: WebSocket falhando, REST fallback ativo
- HALF_OPEN: Testando se WebSocket recuperou
"""

import asyncio
import time
from collections.abc import Callable
from enum import Enum

from shared.observability.logger import debug, error, production, warning


class CircuitState(Enum):
    """Estados do circuit breaker"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class WebSocketCircuitBreaker:
    """
    Circuit Breaker Pattern para WebSocket.

    Monitora falhas e ativa fallback REST quando threshold atingido.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        success_threshold: int = 3,
        reset_timeout: int = 300,
    ):
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.reset_timeout = reset_timeout

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: float | None = None
        self.opened_at: float | None = None

        self.on_open_callback: Callable | None = None
        self.on_close_callback: Callable | None = None

        production(
            "WebSocketCircuitBreaker initialized",
            failure_threshold=failure_threshold,
            success_threshold=success_threshold,
            reset_timeout=reset_timeout,
        )

    def record_success(self):
        """Registra operação bem-sucedida"""
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1

            debug(
                f"Circuit breaker success recorded ({self.success_count}/{self.success_threshold})"
            )

            if self.success_count >= self.success_threshold:
                self._close_circuit()

        elif self.state == CircuitState.CLOSED:
            if self.failure_count > 0:
                self.failure_count -= 1

    def record_failure(self):
        """Registra falha"""
        self.failure_count += 1
        self.last_failure_time = time.time()

        warning(
            "Circuit breaker failure recorded",
            failure_count=self.failure_count,
            threshold=self.failure_threshold,
            state=self.state.value,
        )

        if self.state == CircuitState.HALF_OPEN:
            warning("Failure during HALF_OPEN, returning to OPEN")
            self._open_circuit()

        elif self.failure_count >= self.failure_threshold:
            self._open_circuit()

    def _open_circuit(self):
        """Abre o circuito (ativa fallback)"""
        if self.state == CircuitState.OPEN:
            return

        previous_state = self.state
        self.state = CircuitState.OPEN
        self.opened_at = time.time()
        self.success_count = 0

        error(
            "CIRCUIT BREAKER OPENED",
            previous_state=previous_state.value,
            failures=self.failure_count,
            action="Activating REST fallback",
        )

        if self.on_open_callback:
            try:
                self.on_open_callback()
            except Exception as e:
                error(f"Error in on_open_callback: {e}", exc_info=True)

    def _close_circuit(self):
        """Fecha o circuito (volta ao normal)"""
        previous_state = self.state
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0

        production(
            "CIRCUIT BREAKER CLOSED",
            previous_state=previous_state.value,
            action="Returning to WebSocket",
        )

        if self.on_close_callback:
            try:
                self.on_close_callback()
            except Exception as e:
                error(f"Error in on_close_callback: {e}", exc_info=True)

    async def attempt_reset_loop(self):
        """Loop que tenta resetar circuit breaker periodicamente"""
        while True:
            try:
                await asyncio.sleep(60)

                if self.state == CircuitState.OPEN:
                    if self.opened_at:
                        elapsed = time.time() - self.opened_at

                        if elapsed >= self.reset_timeout:
                            debug(
                                f"Circuit breaker attempting reset after {elapsed:.0f}s"
                            )
                            self._move_to_half_open()
            except asyncio.CancelledError:
                debug("Circuit breaker reset loop cancelled - shutdown in progress")
                break
            except Exception as e:
                error(f"Error in circuit breaker reset loop: {e}")

    def _move_to_half_open(self):
        """Move para HALF_OPEN para testar recovery"""
        if self.state != CircuitState.OPEN:
            return

        self.state = CircuitState.HALF_OPEN
        self.success_count = 0

        production("Circuit breaker moved to HALF_OPEN - testing WebSocket recovery")

    def is_open(self) -> bool:
        """Circuit breaker está aberto?"""
        return self.state == CircuitState.OPEN

    def is_closed(self) -> bool:
        """Circuit breaker está fechado (normal)?"""
        return self.state == CircuitState.CLOSED

    def is_half_open(self) -> bool:
        """Circuit breaker está em teste?"""
        return self.state == CircuitState.HALF_OPEN

    def can_attempt(self) -> bool:
        """Pode tentar operação?"""
        return self.state in [CircuitState.CLOSED, CircuitState.HALF_OPEN]

    def get_state(self) -> dict:
        """Retorna estado atual"""
        return {
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "failure_threshold": self.failure_threshold,
            "success_threshold": self.success_threshold,
            "opened_at": self.opened_at,
            "last_failure": self.last_failure_time,
        }
