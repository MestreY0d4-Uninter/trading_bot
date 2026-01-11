import asyncio
from collections import defaultdict, deque
from datetime import datetime
from decimal import Decimal
from typing import Any

from utils.decimal_math import to_decimal

from .flow_tracker import track_component
from .logger import production, warning

# State will be imported when needed to avoid circular imports


class MetricsCollector:
    def __init__(self, db_handler=None) -> None:
        self._lock = asyncio.Lock()
        self._trades: deque[dict[str, Any]] = deque(maxlen=1000)
        self._api_calls: deque[dict[str, Any]] = deque(maxlen=10000)
        self._signals: deque[dict[str, Any]] = deque(maxlen=5000)
        self._errors: dict[str, int] = defaultdict(int)
        self._db_handler = db_handler
        self._db_synced = False

        production("MetricsCollector inicializado", memory_trades=len(self._trades))

    @track_component("metrics")
    async def record_trade(
        self, symbol: str, pnl: Decimal, duration_minutes: float, success: bool
    ):
        async with self._lock:
            self._trades.append(
                {
                    "timestamp": datetime.now(),
                    "symbol": symbol,
                    "pnl": pnl,
                    "duration": duration_minutes,
                    "success": success,
                }
            )

    @track_component("metrics")
    async def record_api_call(self, endpoint: str, latency_ms: float, success: bool):
        async with self._lock:
            self._api_calls.append(
                {
                    "timestamp": datetime.now(),
                    "endpoint": endpoint,
                    "latency": latency_ms,
                    "success": success,
                }
            )

    @track_component("metrics")
    async def record_signal(self, symbol: str, score: Decimal, accepted: bool):
        async with self._lock:
            self._signals.append(
                {
                    "timestamp": datetime.now(),
                    "symbol": symbol,
                    "score": score,
                    "accepted": accepted,
                }
            )

    async def record_error(self, error_type: str):
        async with self._lock:
            self._errors[error_type] += 1

    @track_component("metrics", slow_threshold=3)
    async def sync_with_database(self, db_handler):
        if not db_handler:
            warning("Database handler não fornecido para sincronização")
            return

        # Fetch from DB outside lock (I/O operation)
        db_trades: list = []

        # Process and prepare data outside lock
        processed_trades = []
        for trade in db_trades:
            if trade.get("status") == "CLOSED":
                pnl = trade.get("realized_pnl", 0)
                is_success = pnl > 0

                # Calcular duração
                duration = 30  # default
                try:
                    if trade.get("exit_time") and trade.get("entry_time"):
                        exit_time = datetime.fromisoformat(
                            str(trade["exit_time"]).replace("Z", "+00:00")
                        )
                        entry_time = datetime.fromisoformat(
                            str(trade["entry_time"]).replace("Z", "+00:00")
                        )
                        duration = (exit_time - entry_time).total_seconds() / 60
                except (ValueError, TypeError):
                    pass

                processed_trades.append(
                    {
                        "timestamp": datetime.now(),
                        "symbol": trade.get("symbol", "UNKNOWN"),
                        "pnl": pnl,
                        "duration": duration,
                        "success": is_success,
                    }
                )

        # Update memory state inside lock (fast)
        async with self._lock:
            try:
                old_count = len(self._trades)
                self._trades.clear()

                # Append processed trades (fast - no computation)
                for trade in processed_trades:
                    self._trades.append(trade)

                synced_count = len(processed_trades)

                self._db_handler = db_handler
                self._db_synced = True

                production(
                    "Métricas sincronizadas com banco",
                    old_memory_count=old_count,
                    db_trades_total=len(db_trades),
                    synced_closed_trades=synced_count,
                    new_memory_count=len(self._trades),
                )

            except Exception as e:
                warning("Erro ao sincronizar metrics com banco", error=str(e))
                self._db_synced = False

    @track_component("metrics", slow_threshold=2)
    async def get_summary(self) -> dict:
        # Get memory summary inside lock (fast)
        memory_summary = None
        has_db = False

        async with self._lock:
            memory_summary = self._get_memory_summary()
            has_db = self._db_handler is not None

        # Enrich with DB data outside lock (slow I/O)
        if has_db:
            try:
                db_summary = await self._get_database_summary_async()
                return db_summary  # DB data is more complete
            except Exception as e:
                warning("ERRO DB - fallback para memória", error=str(e))
                return memory_summary
        else:
            from shared.observability.logger import debug

            debug(
                "DB não disponível - usando memória (dados podem estar desatualizados)"
            )
            return memory_summary

    def _get_memory_summary(self) -> dict:
        total_trades = len(self._trades)
        winning_trades = sum(1 for t in self._trades if t["success"])
        total_signals = len(self._signals)
        accepted_signals = sum(1 for s in self._signals if s["accepted"])

        win_rate = (
            to_decimal(winning_trades) / to_decimal(total_trades) * Decimal("100")
            if total_trades > 0
            else Decimal("0")
        )
        avg_duration = (
            sum(to_decimal(t["duration"]) for t in self._trades)
            / to_decimal(total_trades)
            if total_trades > 0
            else Decimal("0")
        )
        acceptance_rate = (
            to_decimal(accepted_signals) / to_decimal(total_signals) * Decimal("100")
            if total_signals > 0
            else Decimal("0")
        )
        api_call_count = len(self._api_calls)
        avg_latency = (
            sum(to_decimal(c["latency"]) for c in self._api_calls)
            / to_decimal(api_call_count)
            if api_call_count > 0
            else Decimal("0")
        )

        return {
            "trades": {
                "total": total_trades,
                "winning": winning_trades,
                "win_rate": win_rate,
                "avg_duration": avg_duration,
            },
            "signals": {
                "total": total_signals,
                "accepted": accepted_signals,
                "acceptance_rate": acceptance_rate,
            },
            "api": {
                "total_calls": len(self._api_calls),
                "failed_calls": sum(1 for c in self._api_calls if not c["success"]),
                "avg_latency": avg_latency,
            },
            "errors": dict(self._errors),
            "source": "memory",
        }

    def _get_database_summary(self) -> dict:
        if not self._db_handler:
            return self._get_memory_summary()

        try:
            db_trades: list = []
            return self._build_summary_from_trades(db_trades, source="database")
        except Exception as e:
            warning("Erro ao calcular database summary", error=str(e))
            return self._get_memory_summary()

    def _build_summary_from_trades(self, db_trades: list, source: str) -> dict:
        from shared.types.state import _GlobalState

        closed_trades = [t for t in db_trades if t.get("status") == "CLOSED"]
        winning_trades = [t for t in closed_trades if t.get("realized_pnl", 0) > 0]

        total_trades = len(closed_trades)
        winning_count = len(winning_trades)
        win_rate = _GlobalState.calculate_win_rate(winning_count, total_trades)
        avg_duration = self._calculate_avg_duration(closed_trades)
        signals_stats = self._calculate_signals_stats()
        api_stats = self._calculate_api_stats()

        return {
            "trades": {
                "total": total_trades,
                "winning": winning_count,
                "win_rate": win_rate,
                "avg_duration": avg_duration,
            },
            "signals": signals_stats,
            "api": api_stats,
            "errors": dict(self._errors),
            "source": source,
        }

    def _calculate_avg_duration(self, closed_trades: list) -> Decimal:
        if not closed_trades:
            return Decimal("0")

        durations = []
        for trade in closed_trades:
            duration = self._extract_trade_duration(trade)
            if duration is not None:
                durations.append(duration)

        if not durations:
            return Decimal("0")

        return sum(to_decimal(d) for d in durations) / to_decimal(len(durations))

    def _extract_trade_duration(self, trade: dict) -> float | None:
        try:
            if not (trade.get("exit_time") and trade.get("entry_time")):
                return None

            exit_time = datetime.fromisoformat(
                str(trade["exit_time"]).replace("Z", "+00:00")
            )
            entry_time = datetime.fromisoformat(
                str(trade["entry_time"]).replace("Z", "+00:00")
            )
            return (exit_time - entry_time).total_seconds() / 60
        except (ValueError, TypeError):
            return None

    def _calculate_signals_stats(self) -> dict:
        total_signals = len(self._signals)
        accepted_signals = sum(1 for s in self._signals if s["accepted"])
        acceptance_rate = (
            to_decimal(accepted_signals) / to_decimal(total_signals) * Decimal("100")
            if total_signals > 0
            else Decimal("0")
        )
        return {
            "total": total_signals,
            "accepted": accepted_signals,
            "acceptance_rate": acceptance_rate,
        }

    def _calculate_api_stats(self) -> dict:
        api_call_count = len(self._api_calls)
        avg_latency = (
            sum(to_decimal(c["latency"]) for c in self._api_calls)
            / to_decimal(api_call_count)
            if api_call_count > 0
            else Decimal("0")
        )
        return {
            "total_calls": api_call_count,
            "failed_calls": sum(1 for c in self._api_calls if not c["success"]),
            "avg_latency": avg_latency,
        }

    async def _get_database_summary_async(self) -> dict:
        if not self._db_handler:
            return self._get_memory_summary()

        try:
            db_trades = await self._db_handler.get_trades(limit=200)
            return self._build_summary_from_trades(db_trades, source="database")
        except Exception as e:
            warning("Erro ao calcular database summary async", error=str(e))
            return self._get_memory_summary()

    async def get_account_metrics(self) -> dict:
        try:
            from shared.types.state import state

            async with state._lock:
                return {
                    "current_balance": state._metrics.get(
                        "current_balance", Decimal("0")
                    ),
                    "daily_start_balance": state._metrics.get(
                        "starting_balance", Decimal("0")
                    ),
                    "daily_pnl": state._metrics.get("daily_pnl", Decimal("0")),
                    "daily_pnl_pct": state._metrics.get("daily_pnl_pct", Decimal("0")),
                }
        except Exception as e:
            warning("Erro ao obter account metrics", error=str(e))
            return {
                "current_balance": Decimal("0"),
                "daily_start_balance": Decimal("0"),
                "daily_pnl": Decimal("0"),
                "daily_pnl_pct": Decimal("0"),
            }

    async def get_performance_metrics(self) -> dict:
        summary = await self.get_summary()
        trades_data = summary.get("trades", {})

        return {
            "win_rate": trades_data.get("win_rate", 0.0),
            "total_trades": trades_data.get("total", 0),
            "winning_trades": trades_data.get("winning", 0),
            "avg_duration": trades_data.get("avg_duration", 0.0),
        }


metrics = MetricsCollector()


async def sync_database_to_state(db_handler):
    if not db_handler:
        warning("Database handler não fornecido para sincronização com state")
        return

    try:
        from shared.types.state import state

        # Obter stats reais do banco (async)
        db_trades = await db_handler.get_trades(limit=200)
        closed_trades = [t for t in db_trades if t.get("status") == "CLOSED"]
        winning_trades = [t for t in closed_trades if t.get("realized_pnl", 0) > 0]

        total_trades = len(closed_trades)
        winning_count = len(winning_trades)

        # Atualizar state com dados reais do database
        await state.update_metric("total_trades", total_trades)
        await state.update_metric("winning_trades", winning_count)

        # Calcular e atualizar P&L total
        total_pnl = sum(t.get("realized_pnl", 0) for t in closed_trades)
        await state.update_metric("total_pnl", total_pnl)

        # Calcular win rate
        win_rate = (
            to_decimal(winning_count) / to_decimal(total_trades) * Decimal("100")
            if total_trades > 0
            else Decimal("0")
        )
        await state.update_metric("win_rate", win_rate)

        # Calcular best/worst trades
        if closed_trades:
            pnls = [t.get("realized_pnl", 0) for t in closed_trades]
            await state.update_metric("best_trade", max(pnls))
            await state.update_metric("worst_trade", min(pnls))

        # Calcular consecutive losses
        recent_trades = sorted(
            closed_trades, key=lambda x: x.get("created_at", ""), reverse=True
        )
        consecutive_losses = 0
        for trade in recent_trades:
            if trade.get("realized_pnl", 0) < 0:
                consecutive_losses += 1
            else:
                break
        await state.update_metric("consecutive_losses", consecutive_losses)

        # Calcular balance total (starting balance + accumulated PnL)
        starting_balance = 10000
        total_balance = starting_balance + total_pnl
        await state.update_metric("total_balance", total_balance)

        # Calcular P&L diário (apenas trades de hoje)
        from datetime import date

        today = date.today().isoformat()
        daily_pnl = sum(
            t.get("realized_pnl", 0)
            for t in closed_trades
            if t.get("created_at", "").startswith(today)
        )
        await state.update_metric("daily_pnl", daily_pnl)

        production(
            "State sincronizado com database",
            total_trades=total_trades,
            winning_trades=winning_count,
            total_pnl=total_pnl,
            win_rate=win_rate,
            total_balance=total_balance,
            consecutive_losses=consecutive_losses,
        )

    except Exception as e:
        warning("Erro ao sincronizar database com state", error=str(e))


async def initialize_metrics_with_db(db_handler):
    global metrics
    await metrics.sync_with_database(db_handler)
    await sync_database_to_state(db_handler)
    production("Metrics inicializadas com sincronização de banco e state")
