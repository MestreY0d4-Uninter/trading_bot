import asyncio
import math
from collections import defaultdict
from datetime import datetime

from shared.observability.logger import debug, error, production
from shared.observability.metrics import metrics
from shared.types.state import GlobalState, state


class PerformanceMonitor:
    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator
        self.config = coordinator.config

        self.update_interval = 300
        self.analysis_window_days = 30

        self.performance_history = []
        self.hourly_performance = defaultdict(
            lambda: {"trades": 0, "wins": 0, "total_pnl": 0}
        )
        self.symbol_performance = defaultdict(
            lambda: {"trades": 0, "wins": 0, "total_pnl": 0}
        )

        self.risk_free_rate = 0.0

        production("Monitor de Performance inicializado", interval=self.update_interval)

    async def run(self):
        production("Monitor de performance iniciado")

        while self.coordinator.running and not self.coordinator.shutdown_event.is_set():
            try:
                await self._update_performance_metrics()

                await asyncio.sleep(self.update_interval)

            except Exception as e:
                error("Erro no monitor de performance", error=str(e))
                await asyncio.sleep(self.update_interval * 2)

    async def _update_performance_metrics(self):
        try:
            if hasattr(self.coordinator, "db"):
                trades = await self.coordinator.db.get_trades(limit=500)
                self._analyze_trades(trades)

            current_metrics = await self._calculate_current_metrics()
            self.performance_history.append(
                {"timestamp": datetime.now(), "metrics": current_metrics}
            )

            if len(self.performance_history) > 288:
                self.performance_history = self.performance_history[-288:]

            suggestions = await self._generate_suggestions(current_metrics)
            if suggestions:
                for suggestion in suggestions:
                    production("Sugestão de performance", suggestion=suggestion)

        except Exception as e:
            error("Erro ao atualizar métricas de performance", error=str(e))

    def _analyze_trades(self, trades: list[dict]):
        self.hourly_performance.clear()
        self.symbol_performance.clear()

        for trade in trades:
            if trade.get("status") != "CLOSED":
                continue

            timestamp = trade.get("exit_time")
            if not timestamp:
                continue

            if isinstance(timestamp, str):
                try:
                    timestamp = datetime.fromisoformat(timestamp)
                except (ValueError, TypeError):
                    continue

            hour = timestamp.hour
            symbol = trade.get("symbol", "UNKNOWN")
            pnl = trade.get("realized_pnl", 0)
            is_win = pnl > 0

            self.hourly_performance[hour]["trades"] += 1
            self.hourly_performance[hour]["total_pnl"] += pnl
            if is_win:
                self.hourly_performance[hour]["wins"] += 1

            self.symbol_performance[symbol]["trades"] += 1
            self.symbol_performance[symbol]["total_pnl"] += pnl
            if is_win:
                self.symbol_performance[symbol]["wins"] += 1

    async def _calculate_current_metrics(self) -> dict:
        summary = await metrics.get_summary()

        total_trades = summary["trades"]["total"]
        win_rate = summary["trades"]["win_rate"]

        daily_pnl = await state.get_metric("daily_pnl")
        daily_pnl_pct = await state.get_metric("daily_pnl_pct")

        returns = self._get_recent_returns()
        sharpe = self.calculate_sharpe_ratio(returns, self.risk_free_rate)

        best_hours = self.identify_best_trading_hours()
        best_symbols = self._identify_best_symbols()

        win_streaks = await self.calculate_win_streaks()

        return {
            "total_trades": total_trades,
            "win_rate": win_rate,
            "daily_pnl": daily_pnl,
            "daily_pnl_pct": daily_pnl_pct,
            "sharpe_ratio": sharpe,
            "best_hours": best_hours,
            "best_symbols": best_symbols,
            "current_win_streak": win_streaks["current_win_streak"],
            "current_loss_streak": win_streaks["current_loss_streak"],
            "max_win_streak": win_streaks["max_win_streak"],
            "max_loss_streak": win_streaks["max_loss_streak"],
        }

    def calculate_sharpe_ratio(
        self, returns: list[float], risk_free_rate: float = 0
    ) -> float:
        try:
            if not returns or len(returns) < 2:
                return 0.0

            avg_return = sum(returns) / len(returns)

            variance = sum((r - avg_return) ** 2 for r in returns) / (len(returns) - 1)
            std_dev = math.sqrt(variance)

            if std_dev == 0:
                return 0.0

            sharpe = (avg_return - risk_free_rate) / std_dev

            annualized_sharpe = sharpe * math.sqrt(252)

            return annualized_sharpe

        except Exception as e:
            debug("Erro ao calcular Sharpe", error=str(e))
            return 0.0

    def identify_best_trading_hours(self) -> dict[int, float]:
        best_hours = {}

        for hour, stats in self.hourly_performance.items():
            if stats["trades"] > 0:
                from decimal import Decimal

                win_rate = GlobalState.calculate_win_rate(
                    stats["wins"], stats["trades"]
                )
                avg_pnl = stats["total_pnl"] / stats["trades"]

                score = win_rate * Decimal("0.7") + (avg_pnl / Decimal("10")) * Decimal(
                    "0.3"
                )
                best_hours[hour] = score

        sorted_hours = dict(
            sorted(best_hours.items(), key=lambda x: x[1], reverse=True)[:5]
        )

        return sorted_hours

    def _identify_best_symbols(self) -> list[tuple[str, float]]:
        symbol_scores = []

        for symbol, stats in self.symbol_performance.items():
            if stats["trades"] >= 5:
                from decimal import Decimal

                win_rate = GlobalState.calculate_win_rate(
                    stats["wins"], stats["trades"]
                )
                avg_pnl = stats["total_pnl"] / stats["trades"]

                score = win_rate * Decimal("0.6") + (avg_pnl / Decimal("10")) * Decimal(
                    "0.4"
                )
                symbol_scores.append((symbol, score))

        symbol_scores.sort(key=lambda x: x[1], reverse=True)

        return symbol_scores[:5]

    async def calculate_win_streaks(self) -> dict[str, int]:
        current_win_streak = 0
        current_loss_streak = 0
        max_win_streak = 0
        max_loss_streak = 0

        consecutive_losses = await state.get_metric("consecutive_losses")
        current_loss_streak = consecutive_losses

        if hasattr(self.coordinator, "db"):
            trades = await self.coordinator.db.get_trades(limit=100)

            for trade in reversed(trades):
                if trade.get("status") != "CLOSED":
                    continue

                pnl = trade.get("realized_pnl", 0)

                if pnl > 0:
                    if current_loss_streak > 0:
                        max_loss_streak = max(max_loss_streak, current_loss_streak)
                        current_loss_streak = 0
                    current_win_streak += 1
                else:
                    if current_win_streak > 0:
                        max_win_streak = max(max_win_streak, current_win_streak)
                        current_win_streak = 0
                    current_loss_streak += 1

        max_win_streak = max(max_win_streak, current_win_streak)
        max_loss_streak = max(max_loss_streak, current_loss_streak)

        return {
            "current_win_streak": current_win_streak,
            "current_loss_streak": current_loss_streak,
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak,
        }

    async def suggest_position_size_adjustment(self) -> float:
        try:
            recent_performance = await self._get_recent_performance()

            base_adjustment = 1.0

            if recent_performance["win_rate"] > 65:
                base_adjustment *= 1.1
            elif recent_performance["win_rate"] < 40:
                base_adjustment *= 0.8

            if recent_performance["sharpe_ratio"] > 2:
                base_adjustment *= 1.1
            elif recent_performance["sharpe_ratio"] < 0.5:
                base_adjustment *= 0.9

            consecutive_losses = await state.get_metric("consecutive_losses")
            if consecutive_losses >= 3:
                base_adjustment *= 0.7
            elif consecutive_losses >= 2:
                base_adjustment *= 0.85

            daily_pnl_pct = await state.get_metric("daily_pnl_pct")
            if daily_pnl_pct < -5:
                base_adjustment *= 0.5
            elif daily_pnl_pct > 5:
                base_adjustment *= 1.05

            return max(0.5, min(1.5, base_adjustment))

        except Exception as e:
            debug("Erro ao sugerir ajuste de position size", error=str(e))
            return 1.0

    def _get_recent_returns(self) -> list[float]:
        returns = []

        for i in range(1, len(self.performance_history)):
            prev_equity = self.performance_history[i - 1]["metrics"].get(
                "total_equity", 0
            )
            curr_equity = self.performance_history[i]["metrics"].get(
                "total_equity", prev_equity
            )

            if prev_equity > 0:
                daily_return = (curr_equity - prev_equity) / prev_equity
                returns.append(daily_return)

        return returns

    async def _get_recent_performance(self) -> dict:
        total_trades = await state.get_metric("total_trades") or 0
        winning_trades = await state.get_metric("winning_trades") or 0

        win_rate = GlobalState.calculate_win_rate(winning_trades, total_trades)

        returns = self._get_recent_returns()
        sharpe = self.calculate_sharpe_ratio(returns)

        return {
            "win_rate": win_rate,
            "sharpe_ratio": sharpe,
            "total_trades": total_trades,
        }

    async def _generate_suggestions(self, current_metrics: dict) -> list[str]:
        suggestions = []

        if current_metrics["win_rate"] < 40:
            suggestions.append(
                "Win rate baixo - considere revisar critérios de entrada"
            )

        if current_metrics["sharpe_ratio"] < 0.5:
            suggestions.append("Sharpe ratio baixo - risco/retorno desfavorável")

        best_hours = current_metrics.get("best_hours", {})
        if best_hours:
            best_hour = list(best_hours.keys())[0]
            suggestions.append(
                f"Melhor horário para trading: {best_hour}:00-{best_hour+1}:00"
            )

        position_adjustment = await self.suggest_position_size_adjustment()
        if position_adjustment < 0.9:
            suggestions.append(
                f"Considere reduzir position size em {(1-position_adjustment)*100:.0f}%"
            )
        elif position_adjustment > 1.1:
            suggestions.append(
                f"Performance permite aumentar position size em {(position_adjustment-1)*100:.0f}%"
            )

        return suggestions

    async def get_performance_report(self) -> dict:
        current_metrics = await self._calculate_current_metrics()

        report = {
            "summary": {
                "total_trades": current_metrics["total_trades"],
                "win_rate": current_metrics["win_rate"],
                "sharpe_ratio": current_metrics["sharpe_ratio"],
                "daily_pnl": current_metrics["daily_pnl"],
                "daily_pnl_pct": current_metrics["daily_pnl_pct"],
            },
            "best_trading_hours": current_metrics["best_hours"],
            "best_symbols": current_metrics["best_symbols"],
            "streaks": {
                "current_win_streak": current_metrics["current_win_streak"],
                "current_loss_streak": current_metrics["current_loss_streak"],
                "max_win_streak": current_metrics["max_win_streak"],
                "max_loss_streak": current_metrics["max_loss_streak"],
            },
            "position_size_suggestion": await self.suggest_position_size_adjustment(),
            "suggestions": await self._generate_suggestions(current_metrics),
        }

        return report

    async def analyze_symbol_performance(self, symbol: str) -> dict:
        try:
            if symbol not in self.symbol_performance:
                return {
                    "symbol": symbol,
                    "trades": 0,
                    "message": "Sem dados de performance para este símbolo",
                }

            stats = self.symbol_performance[symbol]

            analysis = {
                "symbol": symbol,
                "trades": stats["trades"],
                "wins": stats["wins"],
                "win_rate": GlobalState.calculate_win_rate(
                    stats["wins"], stats["trades"]
                ),
                "total_pnl": stats["total_pnl"],
                "avg_pnl": (
                    stats["total_pnl"] / stats["trades"] if stats["trades"] > 0 else 0
                ),
            }

            return analysis

        except Exception as e:
            error(
                "Erro ao analisar performance do símbolo", symbol=symbol, error=str(e)
            )
            return {"symbol": symbol, "error": str(e)}
