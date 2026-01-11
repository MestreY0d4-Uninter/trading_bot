#!/usr/bin/env python3
import asyncio
import signal
import sys
import threading
from pathlib import Path

sys.path.append(str(Path(__file__).parent))

from services.bot_orchestrator import BotOrchestrator
from shared.observability.logger import debug, error, production
from shared.timeouts import Timeouts

REQUIRED_MODULES = {
    "datetime": "datetime/timedelta",
    "pandas": "pandas",
    "numpy": "numpy",
    "talib": "TA-Lib",
    "binance": "python-binance",
    "yaml": "pyyaml",
    "sqlalchemy": "sqlalchemy",
}


def check_dependencies():
    missing = []

    for module_name, display_name in REQUIRED_MODULES.items():
        try:
            __import__(module_name)
        except ImportError:
            missing.append(display_name)

    if missing:
        error("Dependências críticas faltando", missing=missing)
        error("Execute: uv pip install --python venv/bin/python .")
        sys.exit(1)

    production("Todas as dependências verificadas")


class ShutdownManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._shutdown_requested = False
        self._orchestrator = None
        self._shutdown_count = 0
        self._force_shutdown = False

    @property
    def shutdown_requested(self):
        with self._lock:
            return self._shutdown_requested

    @property
    def force_shutdown(self):
        with self._lock:
            return self._force_shutdown

    def set_orchestrator(self, orchestrator):
        with self._lock:
            self._orchestrator = orchestrator

    def handle_signal(self, signum, frame):
        with self._lock:
            self._shutdown_count += 1

            if self._shutdown_count == 1:
                self._shutdown_requested = True
                production("Sinal de interrupção recebido, encerrando graciosamente...")
                if self._orchestrator:
                    self._orchestrator.shutdown_requested = True
                    self._orchestrator.shutdown_event.set()
            elif self._shutdown_count == 2:
                production("Segundo sinal recebido, forçando encerramento...")
                self._force_shutdown = True
                if self._orchestrator:
                    self._orchestrator.shutdown_requested = True
                    self._orchestrator.shutdown_event.set()
            else:
                production("Terceiro sinal recebido, saindo imediatamente!")
                sys.exit(1)


shutdown_manager = ShutdownManager()


async def shutdown_with_timeout(orchestrator):
    try:
        async with asyncio.timeout(Timeouts.STARTUP_CHECK):
            await orchestrator.shutdown()
    except TimeoutError:
        production("Timeout no shutdown gracioso, forçando encerramento")
    except Exception as e:
        error("Erro durante shutdown", error=str(e))


async def main():
    check_dependencies()

    signal.signal(signal.SIGINT, shutdown_manager.handle_signal)
    signal.signal(signal.SIGTERM, shutdown_manager.handle_signal)

    orchestrator = None
    start_task = None

    try:
        production("Iniciando Trading Bot V2 Refatorado")

        production("📋 Criando BotOrchestrator...")
        orchestrator = BotOrchestrator()
        shutdown_manager.set_orchestrator(orchestrator)

        production("⏳ Criando task para orchestrator.start()...")
        start_task = asyncio.create_task(orchestrator.start())
        production("✓ Task criada, aguardando execução...")

        while not shutdown_manager.shutdown_requested and not start_task.done():
            await asyncio.sleep(0.1)

        if shutdown_manager.shutdown_requested:
            production("Iniciando processo de shutdown...")

            if not start_task.done():
                start_task.cancel()
                try:
                    await start_task
                except asyncio.CancelledError:
                    debug("Start task cancelado durante shutdown")

            if orchestrator:
                await shutdown_with_timeout(orchestrator)
        else:
            try:
                await start_task
            except Exception as e:
                error("Erro durante execução do bot", error=str(e))
                raise

    except asyncio.CancelledError:
        production("Operação cancelada")
    except KeyboardInterrupt:
        production("Interrupção recebida durante startup")
    except Exception as e:
        error("Erro fatal", error=str(e))
        raise
    finally:
        if start_task and not start_task.done():
            start_task.cancel()
            try:
                await start_task
            except asyncio.CancelledError:
                debug("Start task cancelado no cleanup")
            except Exception as e:
                error("Erro ao cancelar start task", error=str(e))

        if orchestrator and not shutdown_manager.shutdown_requested:
            try:
                await shutdown_with_timeout(orchestrator)
            except Exception as e:
                error("Erro durante shutdown final", error=str(e))

        if shutdown_manager.force_shutdown:
            production("Encerramento forçado executado")

        production("Bot encerrado")


if __name__ == "__main__":
    try:
        asyncio.run(main())
        production("Bot encerrado com sucesso")
        sys.exit(0)
    except KeyboardInterrupt:
        production("Bot encerrado pelo usuário")
        sys.exit(0)
    except Exception as e:
        error("Erro fatal durante execução", error=str(e))
        import traceback

        traceback.print_exc()
        sys.exit(1)
