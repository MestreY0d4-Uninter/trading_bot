from datetime import datetime
from decimal import Decimal
from typing import Any

from shared.observability.logger import debug, error, production, warning
from shared.types.state import state


class DurationMonitor:
    def __init__(self, config: dict, db_handler: Any | None = None) -> None:
        self.config = config
        self.monitoring_config = config.get("monitoring", {})

        self.position_tracking_config = self.monitoring_config.get(
            "position_tracking", {}
        )
        self.position_tracking_enabled = self.position_tracking_config.get(
            "enabled", True
        )
        self.duration_alert_thresholds = self.position_tracking_config.get(
            "alert_thresholds", {"yellow": 12, "orange": 24, "red": 48}
        )

        self.temporal_metrics_config = self.monitoring_config.get(
            "temporal_metrics", {}
        )
        self.track_duration = self.temporal_metrics_config.get("track_duration", True)
        self.avg_duration_target = self.temporal_metrics_config.get(
            "avg_duration_target", 8
        )
        self.max_comfort_hours = self.temporal_metrics_config.get(
            "max_comfort_hours", 36
        )
        self.log_duration_stats = self.temporal_metrics_config.get(
            "log_duration_stats", True
        )

        self.last_duration_alert_time: dict[str, float] = {}
        self.duration_alerts_sent: dict[str, int] = {}

        if db_handler is None:
            raise ValueError(
                "DurationMonitor requires valid db_handler for metrics persistence"
            )
        self.db_handler = db_handler

    async def check_position_duration_alerts(self):
        if not self.position_tracking_enabled or not self.track_duration:
            return

        try:
            positions = await state.get_all_positions()
            if not positions:
                return

            now = datetime.now()

            for symbol in positions:
                await self._check_single_position_alert(symbol, now)

        except Exception as e:
            error("Erro ao verificar alertas de duração", error=str(e))

    async def _check_single_position_alert(self, symbol: str, now: datetime) -> None:
        duration_hours = await state.get_position_duration_hours(symbol)
        alert_level = await state.get_position_duration_alert_level(
            symbol, self.duration_alert_thresholds
        )

        alert_sent_level = self.duration_alerts_sent.get(symbol, "green")
        alert_result = self._evaluate_alert_level(
            symbol, alert_level, alert_sent_level, duration_hours
        )

        if alert_result and self._should_send_alert(symbol, now):
            self._send_duration_alert(
                symbol, alert_level, duration_hours, alert_result["message"]
            )
            self.last_duration_alert_time[symbol] = now

    def _evaluate_alert_level(
        self, symbol: str, alert_level: str, sent_level: str, duration: float
    ) -> dict | None:
        alert_config = {
            "red": {
                "skip_if": ["red"],
                "message": f"CRÍTICO: Posição {symbol} há {duration:.1f}h - Análise necessária",
            },
            "orange": {
                "skip_if": ["red", "orange"],
                "message": f"ATENÇÃO: Posição {symbol} há {duration:.1f}h - Revisão recomendada",
            },
            "yellow": {
                "skip_if": ["red", "orange", "yellow"],
                "message": f"MONITORANDO: Posição {symbol} há {duration:.1f}h - Alerta informativo",
            },
        }

        config = alert_config.get(alert_level)
        if not config or sent_level in config["skip_if"]:
            return None

        self.duration_alerts_sent[symbol] = alert_level
        return {"message": config["message"]}

    def _should_send_alert(self, symbol: str, now: datetime) -> bool:
        last_alert = self.last_duration_alert_time.get(symbol)
        return not last_alert or (now - last_alert).total_seconds() > 3600

    def _send_duration_alert(
        self, symbol: str, alert_level: str, duration_hours: float, message: str
    ):
        log_func = warning if alert_level in ["yellow", "orange"] else error

        log_func(
            "Alerta de duração de posição",
            symbol=symbol,
            duration_hours=duration_hours,
            alert_level=alert_level,
            threshold_yellow=self.duration_alert_thresholds["yellow"],
            threshold_orange=self.duration_alert_thresholds["orange"],
            threshold_red=self.duration_alert_thresholds["red"],
            message=message,
        )

    async def get_duration_analytics(self) -> dict:
        try:
            duration_metrics = await state.get_duration_metrics()
            positions_with_duration = await state.get_all_positions_with_duration()

            avg_duration_hours = Decimal(
                str(duration_metrics.get("avg_duration_minutes", 0))
            ) / Decimal("60")
            max_duration_hours = Decimal(
                str(duration_metrics.get("max_duration_minutes", 0))
            ) / Decimal("60")
            min_duration_hours = Decimal(
                str(duration_metrics.get("min_duration_minutes", 0))
            ) / Decimal("60")

            summary_with_hours = duration_metrics.copy()
            summary_with_hours.update(
                {
                    "avg_duration_hours": avg_duration_hours,
                    "max_duration_hours": max_duration_hours,
                    "min_duration_hours": min_duration_hours,
                    "oldest_position_hours": max_duration_hours,
                }
            )

            analytics: dict[str, Any] = {
                "summary": summary_with_hours,
                "positions": {},
                "alerts": {
                    "yellow_alerts": 0,
                    "orange_alerts": 0,
                    "red_alerts": 0,
                    "total_alerts": 0,
                },
                "recommendations": [],
                "performance_vs_duration": {
                    "avg_duration_target": self.avg_duration_target,
                    "max_comfort_hours": self.max_comfort_hours,
                    "current_avg": avg_duration_hours,
                    "target_met": avg_duration_hours <= self.avg_duration_target,
                },
            }

            for symbol, position in positions_with_duration.items():
                duration_hours = position["duration_hours"]
                alert_level = await state.get_position_duration_alert_level(
                    symbol, self.duration_alert_thresholds
                )

                analytics["positions"][symbol] = {
                    "duration_hours": duration_hours,
                    "alert_level": alert_level,
                    "entry_time": position.get("entry_time"),
                    "pnl_pct": position.get("pnl_pct", 0),
                    "recommendation": self._get_position_duration_recommendation(
                        symbol, duration_hours, alert_level
                    ),
                }

                if alert_level == "yellow":
                    analytics["alerts"]["yellow_alerts"] += 1
                elif alert_level == "orange":
                    analytics["alerts"]["orange_alerts"] += 1
                elif alert_level == "red":
                    analytics["alerts"]["red_alerts"] += 1

            analytics["alerts"]["total_alerts"] = sum(
                [
                    analytics["alerts"]["yellow_alerts"],
                    analytics["alerts"]["orange_alerts"],
                    analytics["alerts"]["red_alerts"],
                ]
            )

            analytics["recommendations"] = self._generate_duration_recommendations(
                analytics
            )

            return analytics

        except Exception as e:
            error("Erro ao gerar analytics de duração", error=str(e))
            return {}

    def _get_position_duration_recommendation(
        self, symbol: str, duration_hours: float, alert_level: str
    ) -> str:
        if alert_level == "red":
            return f"Posição há {duration_hours:.1f}h - Considere análise da estratégia para {symbol}"
        elif alert_level == "orange":
            return f"Posição há {duration_hours:.1f}h - Monitorar mais de perto"
        elif alert_level == "yellow":
            return f"Posição há {duration_hours:.1f}h - Dentro do esperado, monitorando"
        else:
            return "Duração normal"

    def _generate_duration_recommendations(self, analytics: dict) -> list[str]:
        recommendations: list[str] = []

        avg_duration = analytics["summary"]["avg_duration_hours"]
        oldest_duration = analytics["summary"]["oldest_position_hours"]
        total_positions = analytics["summary"]["total_positions"]

        if total_positions == 0:
            return recommendations

        if avg_duration > self.avg_duration_target * Decimal("1.5"):
            recommendations.append(
                f"Duração média ({avg_duration:.1f}h) muito acima da meta ({self.avg_duration_target}h)"
            )

        if oldest_duration > self.max_comfort_hours:
            recommendations.append(
                f"Posição mais antiga ({oldest_duration:.1f}h) fora da zona de conforto ({self.max_comfort_hours}h)"
            )

        red_alerts = analytics["alerts"]["red_alerts"]
        if red_alerts > 0:
            recommendations.append(
                f"{red_alerts} posição(ões) em nível crítico de duração"
            )

        orange_alerts = analytics["alerts"]["orange_alerts"]
        if orange_alerts > 1:
            recommendations.append(
                f"{orange_alerts} posições requerem atenção por duração"
            )

        if len(recommendations) == 0:
            recommendations.append(
                "Todas as posições dentro dos parâmetros temporais esperados"
            )

        return recommendations

    def cleanup_duration_alerts(self, symbol: str):
        self.last_duration_alert_time.pop(symbol, None)
        self.duration_alerts_sent.pop(symbol, None)

    async def save_duration_metrics_to_db(self, timestamp: datetime):
        if not self.db_handler:
            return

        try:
            duration_metrics = await state.get_duration_metrics()

            if duration_metrics["total_positions"] == 0:
                return

            avg_duration_hours = Decimal(
                str(duration_metrics.get("avg_duration_minutes", 0))
            ) / Decimal("60")
            max_duration_hours = Decimal(
                str(duration_metrics.get("max_duration_minutes", 0))
            ) / Decimal("60")
            min_duration_hours = Decimal(
                str(duration_metrics.get("min_duration_minutes", 0))
            ) / Decimal("60")

            duration_data = [
                ("avg_duration_hours", avg_duration_hours),
                ("max_duration_hours", max_duration_hours),
                ("min_duration_hours", min_duration_hours),
                ("total_positions_with_duration", duration_metrics["total_positions"]),
                (
                    "positions_over_target",
                    duration_metrics.get("positions_over_target", 0),
                ),
                (
                    "target_duration_minutes",
                    duration_metrics.get("target_duration_minutes", 480),
                ),
            ]

            analytics = await self.get_duration_analytics()
            alert_data = [
                ("duration_yellow_alerts", analytics["alerts"]["yellow_alerts"]),
                ("duration_orange_alerts", analytics["alerts"]["orange_alerts"]),
                ("duration_red_alerts", analytics["alerts"]["red_alerts"]),
                ("duration_total_alerts", analytics["alerts"]["total_alerts"]),
            ]

            all_duration_metrics = duration_data + alert_data

            for metric_name, metric_value in all_duration_metrics:
                if metric_value is not None:
                    metadata = {
                        "avg_duration_target": self.avg_duration_target,
                        "max_comfort_hours": self.max_comfort_hours,
                        "alert_thresholds": self.duration_alert_thresholds,
                        "tracking_enabled": self.position_tracking_enabled,
                        "timestamp": timestamp.isoformat(),
                    }

                    await self.db_handler.save_performance_metric(
                        metric_name=f"duration_{metric_name}",
                        metric_value=float(metric_value),
                        metadata=metadata,
                    )

            debug("Duration metrics salvos no banco", count=len(all_duration_metrics))

        except Exception as e:
            error("Erro ao salvar duration metrics", error=str(e))

    async def log_daily_duration_summary(self):
        if not self.log_duration_stats:
            return

        try:
            analytics = await self.get_duration_analytics()

            if analytics["summary"]["total_positions"] == 0:
                production("Resumo diário de duração: Sem posições abertas")
                return

            summary = analytics["summary"]
            alerts = analytics["alerts"]
            recommendations = analytics["recommendations"]

            production(
                "📊 Resumo diário de duração de posições",
                total_positions=summary["total_positions"],
                avg_duration_hours=summary["avg_duration_hours"],
                oldest_position_hours=summary["oldest_position_hours"],
                target_hours=self.avg_duration_target,
                comfort_zone_hours=self.max_comfort_hours,
                target_met=summary["avg_duration_hours"] <= self.avg_duration_target,
                alert_summary={
                    "yellow": alerts["yellow_alerts"],
                    "orange": alerts["orange_alerts"],
                    "red": alerts["red_alerts"],
                    "total": alerts["total_alerts"],
                },
                recommendations=recommendations,
            )

        except Exception as e:
            error("Erro ao gerar resumo diário de duração", error=str(e))
