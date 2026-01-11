import asyncio
import time
from datetime import datetime
from decimal import Decimal
from typing import Any

from shared.observability.flow_tracker import track_component
from shared.observability.logger import debug, error, production, warning
from shared.types.state import GlobalState, state
from utils.decimal_math import to_decimal
from utils.validation_utils import is_numeric_valid

from .risk_calculator import RiskLevel


class RiskTracker:
    def __init__(self, config: dict, db_handler: Any | None = None) -> None:
        self.config = config
        self.risk_config = config["risk"]

        self._balance_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()

        self.starting_balance = Decimal("0")
        self.current_balance = Decimal("0")
        self.total_equity = Decimal("0")
        self.last_reset = datetime.now().date()

        if db_handler is None:
            raise ValueError(
                "RiskTracker requires valid db_handler for daily_metrics persistence"
            )
        self.db_handler = db_handler
        self._cache_ttl = 1

        self.monitoring_config = config.get("monitoring", {})
        self.save_performance_metrics_enabled = self.monitoring_config.get(
            "save_performance_metrics", True
        )
        self.metrics_interval = self.monitoring_config.get("metrics_interval", 300)
        self.last_metrics_save = datetime.now()

    async def load_from_database(self):
        if not self.db_handler:
            return

        try:
            today_str = datetime.now().strftime("%Y-%m-%d")
            daily_metrics = await self.db_handler.get_daily_metrics(today_str)

            if daily_metrics and daily_metrics.get("starting_balance"):
                saved_balance = to_decimal(daily_metrics["starting_balance"])
                if saved_balance > 0:
                    self.starting_balance = saved_balance
                    production(
                        "Starting balance carregado do banco",
                        balance=float(saved_balance),
                    )
                    return

            latest_metrics = await self.db_handler.get_daily_metrics()
            if latest_metrics and latest_metrics.get("starting_balance"):
                saved_balance = to_decimal(latest_metrics["starting_balance"])
                if saved_balance > 0:
                    self.starting_balance = saved_balance
                    production(
                        "Starting balance carregado do último registro",
                        balance=saved_balance,
                    )

        except Exception as e:
            warning("Erro ao carregar starting_balance do banco", error=str(e))

    async def _save_starting_balance_to_db(self):
        if not self.db_handler or self.starting_balance <= 0:
            return

        try:
            metrics_data = {
                "date": datetime.now().date(),
                "starting_balance": float(self.starting_balance),
                "ending_balance": float(self.current_balance),
                "total_pnl": 0.0,
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "best_trade": 0.0,
                "worst_trade": 0.0,
                "data": {},
            }

            await self.db_handler.save_daily_metrics(metrics_data)
        except Exception as e:
            warning("Erro ao salvar starting_balance no banco", error=str(e))

    async def _get_metrics_cached(self, timestamp_bucket: int):
        return await state.get_all_metrics()

    async def _get_cached_metrics(self) -> dict:
        timestamp_bucket = int(time.time()) // self._cache_ttl
        all_metrics = await self._get_metrics_cached(timestamp_bucket)

        return {
            "daily_pnl_pct": all_metrics.get("daily_pnl_pct", 0),
            "consecutive_losses": all_metrics.get("consecutive_losses", 0),
            "total_trades": all_metrics.get("total_trades", 0),
            "winning_trades": all_metrics.get("winning_trades", 0),
            "daily_pnl": all_metrics.get("daily_pnl", 0),
            "daily_unrealized_pnl": all_metrics.get("daily_unrealized_pnl", 0),
            "daily_total_pnl": all_metrics.get("daily_total_pnl", 0),
            "last_trade_time": all_metrics.get("last_trade_time"),
        }

    @track_component("risk_manager")
    async def set_starting_balance(self, balance: Decimal, validate_func) -> bool:
        async with self._balance_lock:
            if not validate_func(balance):
                return False

            self.starting_balance = balance
            self.current_balance = balance
            self.total_equity = balance

            await state.update_metric("starting_balance", float(balance))
            await state.update_metric("current_balance", float(balance))
            await state.update_metric("total_equity", float(balance))
            await self._save_starting_balance_to_db()
            return True

    @track_component("risk_manager")
    async def update_balance(
        self,
        balance: Decimal,
        positions_market_value: Decimal,
        validate_func,
        check_circuit_breakers_func,
        assess_risk_func,
        save_metrics_func,
        check_duration_func,
    ):
        if not validate_func(balance):
            return

        old_balance, flags = await self._update_balance_in_lock(
            balance, positions_market_value
        )

        await self._handle_balance_flags(flags, balance)

        if self.starting_balance > 0:
            await self._process_daily_pnl(
                balance,
                positions_market_value,
                old_balance,
                check_circuit_breakers_func,
                assess_risk_func,
            )

        await state.update_metric("current_balance", balance)
        await state.update_metric("total_equity", self.total_equity)
        await save_metrics_func()
        await check_duration_func()

    async def _update_balance_in_lock(
        self, balance: Decimal, positions_market_value: Decimal
    ) -> tuple[Decimal, dict]:
        today = datetime.now().date()
        flags = {"save_starting": False, "reset_daily": False, "log_balance": None}

        async with self._state_lock:
            async with self._balance_lock:
                old_balance = self.current_balance
                self.current_balance = balance
                self.total_equity = balance + positions_market_value

                if self.starting_balance == 0 and balance > 0:
                    self.starting_balance = balance
                    flags["log_balance"] = balance
                    flags["save_starting"] = True

            if today != self.last_reset:
                mode = self.config.get("mode", "testnet")
                if mode != "testnet":
                    self.starting_balance = balance
                    flags["reset_daily"] = True
                self.last_reset = today

        return old_balance, flags

    async def _handle_balance_flags(self, flags: dict, balance: Decimal) -> None:
        if flags["save_starting"]:
            await self._save_starting_balance_to_db()

        if flags["reset_daily"]:
            await self._reset_daily_stats_internal()

        if flags["log_balance"] is not None:
            production(
                "Starting balance definido automaticamente",
                balance=float(flags["log_balance"]),
            )

    async def _process_daily_pnl(
        self,
        balance: Decimal,
        positions_market_value: Decimal,
        old_balance: Decimal,
        check_circuit_breakers_func,
        assess_risk_func,
    ) -> None:
        daily_realized_pnl = Decimal("0")
        if self.db_handler:
            pnl_from_db = await self.db_handler.get_daily_realized_pnl()
            daily_realized_pnl = to_decimal(pnl_from_db)

        current_equity = balance + positions_market_value
        daily_unrealized_pnl = (
            current_equity - self.starting_balance - daily_realized_pnl
        )

        daily_pnl_pct = (daily_realized_pnl / self.starting_balance) * Decimal("100")
        daily_total_pnl = daily_realized_pnl + daily_unrealized_pnl
        daily_total_pnl_pct = (daily_total_pnl / self.starting_balance) * Decimal("100")

        await state.update_metric("daily_pnl", daily_realized_pnl)
        await state.update_metric("daily_pnl_pct", daily_pnl_pct)
        await state.update_metric("daily_unrealized_pnl", daily_unrealized_pnl)
        await state.update_metric("daily_total_pnl", daily_total_pnl)
        await state.update_metric("daily_total_pnl_pct", daily_total_pnl_pct)

        metrics = await self._get_cached_metrics()
        await check_circuit_breakers_func(daily_pnl_pct, metrics["consecutive_losses"])
        risk_level = await assess_risk_func(
            daily_pnl_pct, metrics["consecutive_losses"]
        )

        if abs(self.total_equity - (old_balance + positions_market_value)) > Decimal(
            "0.01"
        ):
            production(
                "Patrimônio atualizado",
                equity=self.total_equity,
                balance=balance,
                daily_pnl_pct=daily_pnl_pct,
                risk_level=risk_level.value,
            )

    @track_component("risk_manager")
    async def register_trade_result(
        self,
        symbol: str,
        pnl: float,
        pnl_pct: float,
        success: bool,
        risk_level: RiskLevel,
        trigger_emergency_func,
        cleanup_duration_func,
        save_metrics_func,
    ):
        result = await self._process_trade_result_in_lock(
            symbol, pnl, pnl_pct, success, risk_level, cleanup_duration_func
        )

        if not result:
            return

        await self._handle_post_trade_actions(
            result, trigger_emergency_func, save_metrics_func
        )

    async def _process_trade_result_in_lock(
        self,
        symbol: str,
        pnl: float,
        pnl_pct: float,
        success: bool,
        risk_level: RiskLevel,
        cleanup_duration_func,
    ) -> dict | None:
        async with self._state_lock:
            if not is_numeric_valid(pnl) or not is_numeric_valid(pnl_pct):
                error("PnL inválido", pnl=pnl, pnl_pct=pnl_pct)
                return None

            metrics = await self._get_cached_metrics()
            result = self._calculate_trade_metrics(
                success, metrics, risk_level, symbol, pnl, pnl_pct
            )

            for key, value in result["updates"]:
                await state.update_metric(key, value)

            cleanup_duration_func(symbol)

        return result

    def _calculate_trade_metrics(
        self,
        success: bool,
        metrics: dict,
        risk_level: RiskLevel,
        symbol: str,
        pnl: float,
        pnl_pct: float,
    ) -> dict:
        updates = []
        consecutive_losses = 0
        should_trigger_emergency = False
        consecutive_losses_limit = 0

        if success:
            updates.append(("consecutive_losses", 0))
            updates.append(("winning_trades", metrics["winning_trades"] + 1))
        else:
            consecutive_losses = metrics["consecutive_losses"] + 1
            updates.append(("consecutive_losses", consecutive_losses))

            consecutive_losses_limit = self.config.get("emergency_conditions", {}).get(
                "close_on_consecutive_losses", 4
            )

            if consecutive_losses >= consecutive_losses_limit:
                should_trigger_emergency = True

        updates.append(("total_trades", metrics["total_trades"] + 1))
        updates.append(("last_trade_time", datetime.now()))

        return {
            "updates": updates,
            "success": success,
            "symbol": symbol,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "risk_level": risk_level,
            "consecutive_losses": consecutive_losses,
            "consecutive_losses_limit": consecutive_losses_limit,
            "should_trigger_emergency": should_trigger_emergency,
            "should_log_winner": success and risk_level != RiskLevel.LOW,
        }

    async def _handle_post_trade_actions(
        self, result: dict, trigger_emergency_func, save_metrics_func
    ) -> None:
        if result["should_trigger_emergency"]:
            await trigger_emergency_func("CONSECUTIVE_LOSSES")

        await save_metrics_func()

        if result["should_log_winner"]:
            production(
                "Trade vencedor - risco reduzido",
                symbol=result["symbol"],
                pnl_pct=result["pnl_pct"],
                risk_level=result["risk_level"].value,
            )

        if result["should_trigger_emergency"]:
            error(
                "🚨 TRIGGERING EMERGENCY SHUTDOWN por perdas consecutivas",
                consecutive_losses=result["consecutive_losses"],
                limit=result["consecutive_losses_limit"],
                symbol=result["symbol"],
            )

        emoji = "✅" if result["success"] else "❌"
        production(
            f"{emoji} Trade registrado",
            symbol=result["symbol"],
            pnl=result["pnl"],
            pnl_pct=result["pnl_pct"],
            consecutive_losses=result["consecutive_losses"],
            risk_level=result["risk_level"].value,
        )

    async def save_performance_metrics(
        self,
        risk_level: RiskLevel,
        circuit_breaker_triggered: bool,
        position_count_func,
    ):
        if not self.save_performance_metrics_enabled or not self.db_handler:
            return

        now = datetime.now()
        if (now - self.last_metrics_save).total_seconds() < self.metrics_interval:
            return

        try:
            metrics = await self._get_cached_metrics()

            performance_data = [
                ("daily_pnl", metrics["daily_pnl"]),
                ("daily_pnl_pct", metrics["daily_pnl_pct"]),
                ("consecutive_losses", metrics["consecutive_losses"]),
                ("total_trades", metrics["total_trades"]),
                ("winning_trades", metrics["winning_trades"]),
                ("daily_unrealized_pnl", metrics["daily_unrealized_pnl"]),
                ("daily_total_pnl", metrics["daily_total_pnl"]),
                (
                    "risk_level_numeric",
                    {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}[
                        risk_level.value.upper()
                    ],
                ),
                ("current_balance", self.current_balance),
                ("total_equity", self.total_equity),
                ("emergency_shutdown", 0),
                ("circuit_breaker_active", int(circuit_breaker_triggered)),
            ]

            for metric_name, metric_value in performance_data:
                if metric_value is not None and is_numeric_valid(metric_value):
                    metadata = {
                        "risk_level": risk_level.value,
                        "emergency_shutdown": False,
                        "circuit_breaker": circuit_breaker_triggered,
                        "open_positions": await position_count_func(),
                        "timestamp": now.isoformat(),
                    }

                    await self.db_handler.save_performance_metric(
                        metric_name=metric_name,
                        metric_value=float(metric_value),
                        metadata=metadata,
                    )

            self.last_metrics_save = now
            debug("Performance metrics salvos no banco", count=len(performance_data))

        except Exception as e:
            error("Erro ao salvar performance metrics", error=str(e))

    async def save_daily_metrics(
        self, risk_level: RiskLevel, circuit_breaker_triggered: bool
    ):
        if not self.db_handler:
            return

        try:
            today = datetime.now().date()
            metrics = await self._get_cached_metrics()

            daily_metrics_data = {
                "date": today,
                "starting_balance": self.starting_balance,
                "ending_balance": self.current_balance,
                "total_pnl": metrics["daily_pnl"],
                "total_trades": metrics["total_trades"],
                "winning_trades": metrics["winning_trades"],
                "losing_trades": metrics["total_trades"] - metrics["winning_trades"],
                "best_trade": 0.0,
                "worst_trade": 0.0,
                "data": {
                    "daily_pnl_pct": metrics["daily_pnl_pct"],
                    "consecutive_losses": metrics["consecutive_losses"],
                    "total_equity": self.total_equity,
                    "risk_level": risk_level.value,
                    "emergency_shutdown": False,
                    "circuit_breaker_active": circuit_breaker_triggered,
                    "win_rate": GlobalState.calculate_win_rate(
                        metrics["winning_trades"], metrics["total_trades"]
                    ),
                },
            }

            success = await self.db_handler.save_daily_metrics(daily_metrics_data)
            if success:
                production(
                    "Daily metrics salvos",
                    date=today.isoformat(),
                    trades=metrics["total_trades"],
                )
            else:
                error("Falha ao salvar daily metrics", date=today.isoformat())

        except Exception as e:
            error("Erro ao salvar daily metrics", error=str(e))

    async def _reset_daily_stats_internal(self):
        production("Resetando estatísticas diárias e proteções")

        stats = await self.get_daily_stats(RiskLevel.LOW)
        if stats["total_trades"] > 0:
            production(
                "Resumo do dia anterior",
                trades=stats["total_trades"],
                win_rate=stats["win_rate"],
                daily_pnl=stats["daily_pnl"],
                daily_pnl_pct=stats["daily_pnl_pct"],
            )

        await state.update_metric("daily_pnl", 0.0)
        await state.update_metric("daily_pnl_pct", 0.0)
        await state.update_metric("consecutive_losses", 0)

    async def daily_reset(self, risk_level: RiskLevel, daily_target_pct: float):
        async with self._state_lock:
            await self._reset_daily_stats_internal()

        metrics = await self._get_cached_metrics()
        total_trades = metrics["total_trades"]
        winning_trades = metrics["winning_trades"]

        return {
            "starting_balance": self.starting_balance,
            "current_balance": self.current_balance,
            "total_equity": self.total_equity,
            "daily_pnl": metrics["daily_pnl"],
            "daily_pnl_pct": metrics["daily_pnl_pct"],
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": total_trades - winning_trades,
            "consecutive_losses": metrics["consecutive_losses"],
            "win_rate": GlobalState.calculate_win_rate(winning_trades, total_trades),
            "progress_to_target": (
                (metrics["daily_pnl_pct"] / daily_target_pct * 100)
                if daily_target_pct > 0
                else 0
            ),
            "daily_target_pct": daily_target_pct,
            "risk_level": risk_level.value,
            "emergency_shutdown_active": False,
        }

    async def get_daily_stats(self, risk_level: RiskLevel) -> dict:
        async with self._state_lock:
            metrics = await self._get_cached_metrics()
            total_trades = metrics["total_trades"]
            winning_trades = metrics["winning_trades"]

            return {
                "total_trades": total_trades,
                "winning_trades": winning_trades,
                "losing_trades": total_trades - winning_trades,
                "win_rate": GlobalState.calculate_win_rate(
                    winning_trades, total_trades
                ),
                "daily_pnl": metrics["daily_pnl"],
                "daily_pnl_pct": metrics["daily_pnl_pct"],
                "consecutive_losses": metrics["consecutive_losses"],
                "starting_balance": self.starting_balance,
                "current_balance": self.current_balance,
                "total_equity": self.total_equity,
                "risk_level": risk_level.value,
            }

    async def get_actual_position_count(self) -> int:
        try:
            if hasattr(self, "db_handler") and self.db_handler:
                db_positions = await self.db_handler.get_open_trades()
                db_count = len(db_positions)

                state_count = len(await state.get_all_positions())
                if state_count != db_count:
                    warning(
                        "State dessincronizado - usando DB como SSOT",
                        state_count=state_count,
                        db_count=db_count,
                    )

                return db_count

            error("DB indisponível - fallback para state (race condition possível)")
            return len(await state.get_all_positions())

        except Exception as e:
            error("Erro ao contar posições", error=str(e))
            return len(await state.get_all_positions())
