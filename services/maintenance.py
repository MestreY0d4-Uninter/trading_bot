import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from shared.observability.flow_tracker import flow_tracker, track_component
from shared.observability.logger import debug, error, production, warning
from shared.observability.metrics import metrics, sync_database_to_state
from shared.timeouts import Timeouts
from shared.types.state import state


class MaintenanceService:
    def __init__(self, db: Any, cache_instance: Any) -> None:
        self.db = db
        self.cache = cache_instance

        self.running = False
        self.maintenance_interval = 3600
        self.last_vacuum = datetime.now()
        self.last_cleanup = datetime.now()
        self.last_backup = datetime.now()
        self.last_sync = datetime.now()

        self.logs_retention_days = 7
        self.data_retention_days = 30
        self.max_backups = 10

        production("Serviço de Manutenção inicializado")

    @track_component("maintenance", slow_threshold=1800)
    async def run(self):
        self.running = True
        production("Serviço de manutenção iniciado")

        while self.running:
            try:
                flow_tracker.force_component_update("maintenance")
                await asyncio.sleep(self.maintenance_interval)

                if not self.running:
                    break

                production("Executando manutenção periódica")

                await self._perform_maintenance()
                flow_tracker.force_component_update("maintenance")

            except asyncio.CancelledError:
                break
            except Exception as e:
                error("Erro no serviço de manutenção", error=str(e))
                await asyncio.sleep(300)

    async def _perform_maintenance(self):
        maintenance_tasks = []

        now = datetime.now()

        if (now - self.last_cleanup).total_seconds() > 3600:
            maintenance_tasks.append(self.cleanup_old_data(self.data_retention_days))
            maintenance_tasks.append(self._cleanup_old_logs())
            self.last_cleanup = now

        if (now - self.last_vacuum).days >= 1:
            maintenance_tasks.append(self.optimize_database())
            self.last_vacuum = now

        if now.hour == 0 and (now - self.last_backup).total_seconds() / 3600 >= 23:
            maintenance_tasks.append(self._create_backup())
            maintenance_tasks.append(self._cleanup_old_backups())
            self.last_backup = now

        maintenance_tasks.append(self._cleanup_cache())
        maintenance_tasks.append(self._reset_daily_metrics())

        # Sincronização frequente para dashboard (a cada 5 minutos)
        if (now - self.last_sync).total_seconds() > 300:
            maintenance_tasks.append(self._sync_database_to_state())
            self.last_sync = now

        results = await asyncio.gather(*maintenance_tasks, return_exceptions=True)

        errors = sum(1 for r in results if isinstance(r, Exception))
        if errors > 0:
            warning("Erros durante manutenção", errors=errors, total=len(results))
        else:
            production("Manutenção concluída com sucesso")

    async def cleanup_old_data(self, days: int = 30):
        try:
            if not self.db:
                return

            await self.db.cleanup_old_data(days)

            production("Dados antigos limpos", days=days)

        except Exception as e:
            error("Erro ao limpar dados antigos", error=str(e))

    async def optimize_database(self):
        try:
            if not self.db:
                return

            production("Iniciando otimização do banco de dados")

            async with asyncio.timeout(Timeouts.MAINTENANCE_FULL):
                await self.db.vacuum_database()

            production("Banco de dados otimizado (VACUUM)")

        except TimeoutError:
            warning("Timeout na otimização do banco")
        except Exception as e:
            error("Erro ao otimizar banco", error=str(e))

    async def _cleanup_old_logs(self):
        try:
            logs_dir = Path("logs")
            if not logs_dir.exists():
                return

            current_time = datetime.now()
            cutoff_time = current_time - timedelta(days=self.logs_retention_days)

            cleaned_count = 0

            for log_file in logs_dir.glob("*.log*"):
                try:
                    file_time = datetime.fromtimestamp(log_file.stat().st_mtime)
                    if file_time < cutoff_time:
                        log_file.unlink()
                        cleaned_count += 1
                        debug("Log antigo removido", file=log_file.name)
                except Exception as e:
                    debug("Erro ao remover log", file=log_file.name, error=str(e))

            if cleaned_count > 0:
                production("Logs antigos limpos", count=cleaned_count)

        except Exception as e:
            error("Erro na limpeza de logs", error=str(e))

    async def _create_backup(self):
        try:
            if not self.db:
                return

            backup_path = await self.db.create_backup()

            production("Backup criado", path=backup_path)

        except Exception as e:
            error("Erro ao criar backup", error=str(e))

    async def _cleanup_old_backups(self):
        try:
            if not self.db:
                return

            db_dir = Path(self.db.db_path).parent
            backups = sorted(
                db_dir.glob("*.backup_*"), key=lambda p: p.stat().st_mtime, reverse=True
            )

            if len(backups) > self.max_backups:
                for backup in backups[self.max_backups :]:
                    try:
                        backup.unlink()
                        debug("Backup antigo removido", file=backup.name)
                    except Exception as e:
                        debug("Erro ao remover backup", file=backup.name, error=str(e))

                production(
                    "Backups antigos limpos", removed=len(backups) - self.max_backups
                )

        except Exception as e:
            error("Erro ao limpar backups", error=str(e))

    async def _cleanup_cache(self):
        try:
            expired_count = self.cache.cleanup_expired()
            debug(f"Cache limpo: {expired_count} entradas expiradas removidas")
        except Exception as e:
            error("Erro ao limpar cache", error=str(e))

    async def _reset_daily_metrics(self):
        try:
            now = datetime.now()

            if now.hour == 0 and now.minute < 5:
                await state.update_metric("daily_pnl", 0.0)
                await state.update_metric("daily_pnl_pct", 0.0)
                await state.update_metric("consecutive_losses", 0)

                current_trades = await state.get_metric("total_trades")
                current_wins = await state.get_metric("winning_trades")

                metrics_summary = await metrics.get_summary()

                production(
                    "Métricas diárias resetadas",
                    trades_hoje=current_trades,
                    wins_hoje=current_wins,
                    win_rate=metrics_summary["trades"]["win_rate"],
                )

        except Exception as e:
            error("Erro ao resetar métricas diárias", error=str(e))

    async def run_daily_maintenance(self):
        production("Executando manutenção diária completa")

        try:
            await self.optimize_database()

            await self.cleanup_old_data(self.data_retention_days)

            await self._cleanup_old_logs()

            await self._create_backup()
            await self._cleanup_old_backups()

            await self._reset_daily_metrics()

            production("Manutenção diária concluída")

        except Exception as e:
            error("Erro na manutenção diária", error=str(e))

    async def emergency_cleanup(self):
        warning("Limpeza de emergência iniciada")

        try:
            self.cache.invalidate()

            await self.cleanup_old_data(7)

            await self.optimize_database()

            production("Limpeza de emergência concluída")

        except Exception as e:
            error("Erro na limpeza de emergência", error=str(e))

    async def _sync_database_to_state(self):
        try:
            if not self.db:
                return

            await sync_database_to_state(self.db)
            debug("Sincronização database->state executada com sucesso")

        except Exception as e:
            error("Erro na sincronização database->state", error=str(e))

    def get_maintenance_status(self) -> dict:
        return {
            "running": self.running,
            "last_vacuum": self.last_vacuum,
            "last_cleanup": self.last_cleanup,
            "last_backup": self.last_backup,
            "last_sync": self.last_sync,
            "next_maintenance": datetime.now()
            + timedelta(seconds=self.maintenance_interval),
            "config": {
                "logs_retention_days": self.logs_retention_days,
                "data_retention_days": self.data_retention_days,
                "max_backups": self.max_backups,
                "maintenance_interval": self.maintenance_interval,
            },
        }

    async def shutdown(self):
        self.running = False
        production("Serviço de manutenção encerrado")
