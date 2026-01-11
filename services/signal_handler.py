import asyncio
import sys

from shared.enums import ShutdownMode
from shared.observability.logger import debug, error, production, warning


class SignalHandlerMixin:
    async def _force_immediate_exit(self):
        error("SAÍDA FORÇADA - tentando salvar estado crítico")
        try:
            await self._save_critical_state_sync()
        except Exception as e:
            error("Erro ao salvar estado crítico na saída forçada", error=str(e))
        finally:
            production("Saída imediata executada")
            sys.exit(1)

    async def _request_shutdown(self, mode: ShutdownMode):
        async with self._shutdown_lock:
            if self._shutdown_in_progress:
                debug(f"Shutdown já em progresso, modo atual: {self._shutdown_mode}")
                return

            self._shutdown_mode = mode
            self._shutdown_in_progress = True

        production(f"Iniciando shutdown no modo: {mode.value}")
        self.shutdown_requested = True
        self.shutdown_event.set()

    async def _shutdown_monitor(self):
        try:
            while not self.shutdown_requested and self._running:
                await asyncio.sleep(1)

            if self._shutdown_mode == ShutdownMode.EMERGENCY:
                warning(
                    "Shutdown de emergência detectado - iniciando sequência acelerada"
                )
            elif self._shutdown_mode == ShutdownMode.FORCED:
                error("Shutdown forçado - execução imediata")
                await self._force_immediate_exit()

        except Exception as e:
            error("Erro no monitor de shutdown", error=str(e))
