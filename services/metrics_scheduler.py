import asyncio
from datetime import datetime, timedelta
from typing import Any

from shared.observability.flow_tracker import flow_tracker, track_component
from shared.observability.logger import error, production, warning


class MetricsScheduler:
    def __init__(self, config: dict, risk_manager: Any | None = None) -> None:
        self.config = config
        self.risk_manager = risk_manager

        self.monitoring_config = config.get("monitoring", {})
        self.daily_metrics_hour = self.monitoring_config.get("daily_metrics_hour", 0)
        self.metrics_interval = self.monitoring_config.get("metrics_interval", 300)

        self.last_daily_save = None
        self.scheduler_running = False
        self._scheduler_task: asyncio.Task | None = None

        production(
            "MetricsScheduler inicializado",
            daily_metrics_hour=self.daily_metrics_hour,
            metrics_interval_seconds=self.metrics_interval,
        )

    @track_component("metrics_scheduler")
    async def start(self):
        if self.scheduler_running:
            warning("MetricsScheduler já está em execução")
            return

        self.scheduler_running = True
        self._scheduler_task = asyncio.create_task(self._scheduler_worker())

        production("MetricsScheduler iniciado")

    @track_component("metrics_scheduler")
    async def stop(self):
        if not self.scheduler_running:
            return

        self.scheduler_running = False

        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await asyncio.wait_for(self._scheduler_task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                pass

        production("MetricsScheduler parado")

    @track_component("metrics_scheduler", slow_threshold=120)
    async def _scheduler_worker(self):
        while self.scheduler_running:
            try:
                flow_tracker.force_component_update("metrics_scheduler")
                await self._check_daily_metrics_schedule()
                flow_tracker.force_component_update("metrics_scheduler")

                await asyncio.sleep(60)

            except asyncio.CancelledError:
                break
            except Exception as e:
                error("Erro no scheduler de métricas", error=str(e))
                await asyncio.sleep(60)

    async def _check_daily_metrics_schedule(self):
        now = datetime.now()
        today = now.date()

        if (
            now.hour == self.daily_metrics_hour
            and now.minute < 5
            and (self.last_daily_save is None or self.last_daily_save != today)
        ):

            await self._save_daily_metrics()
            self.last_daily_save = today

    async def _save_daily_metrics(self):
        if not self.risk_manager:
            warning("RiskManager não disponível para salvar métricas diárias")
            return

        try:
            await self.risk_manager.tracker.save_daily_metrics(
                self.risk_manager.risk_level,
                self.risk_manager.emergency.circuit_breaker.is_triggered(),
            )
            production("Métricas diárias salvas pelo scheduler")
        except Exception as e:
            error("Erro ao salvar métricas diárias pelo scheduler", error=str(e))

    @track_component("metrics_scheduler", slow_threshold=5)
    async def force_daily_save(self):
        try:
            await self._save_daily_metrics()
            production("Salvamento forçado de métricas diárias executado")
            return True
        except Exception as e:
            error("Erro no salvamento forçado de métricas", error=str(e))
            return False

    @track_component("metrics_scheduler", slow_threshold=1)
    def get_status(self) -> dict[str, Any]:
        return {
            "scheduler_running": self.scheduler_running,
            "daily_metrics_hour": self.daily_metrics_hour,
            "metrics_interval": self.metrics_interval,
            "last_daily_save": (
                self.last_daily_save.isoformat() if self.last_daily_save else None
            ),
            "next_daily_save_estimate": self._estimate_next_daily_save(),
        }

    def _estimate_next_daily_save(self) -> str:
        now = datetime.now()
        today_target = now.replace(
            hour=self.daily_metrics_hour, minute=0, second=0, microsecond=0
        )

        if now >= today_target:
            next_save = today_target + timedelta(days=1)
        else:
            next_save = today_target

        return next_save.isoformat()
