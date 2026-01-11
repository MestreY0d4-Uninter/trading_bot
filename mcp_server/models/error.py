from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorSummary(BaseModel):
    timestamp: str
    total_errors: int
    total_warnings: int
    hours_analyzed: int
    by_level: dict[str, int]
    by_module: dict[str, int]
    by_hour: list[dict[str, Any]]

    @classmethod
    def create(cls, entries: list[dict[str, Any]], hours: int) -> ErrorSummary:
        from datetime import datetime

        errors = [e for e in entries if e.get("level") == "error"]
        warnings = [e for e in entries if e.get("level") == "warning"]

        by_level: dict[str, int] = {}
        for entry in entries:
            level = entry.get("level", "unknown")
            by_level[level] = by_level.get(level, 0) + 1

        by_module: dict[str, int] = {}
        for entry in entries:
            module = entry.get("module", "unknown")
            by_module[module] = by_module.get(module, 0) + 1

        by_hour: dict[str, dict[str, Any]] = {}
        for entry in entries:
            timestamp_str = entry.get("timestamp", "")
            if not timestamp_str:
                continue

            try:
                dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                hour_key = dt.strftime("%Y-%m-%d %H:00")

                if hour_key not in by_hour:
                    by_hour[hour_key] = {
                        "hour": hour_key,
                        "count": 0,
                        "errors": 0,
                        "warnings": 0,
                    }

                by_hour[hour_key]["count"] += 1
                if entry.get("level") == "error":
                    by_hour[hour_key]["errors"] += 1
                elif entry.get("level") == "warning":
                    by_hour[hour_key]["warnings"] += 1
            except (ValueError, AttributeError):
                continue

        return cls(
            timestamp=datetime.now().isoformat(),
            total_errors=len(errors),
            total_warnings=len(warnings),
            hours_analyzed=hours,
            by_level=by_level,
            by_module=by_module,
            by_hour=sorted(by_hour.values(), key=lambda x: x["hour"]),
        )


class ErrorPattern(BaseModel):
    pattern: str
    sample_event: str
    occurrences: int
    modules: list[str]
    first_seen: str
    last_seen: str
    severity: str = "error"

    @classmethod
    def from_group(cls, group: dict[str, Any]) -> ErrorPattern:
        return cls(
            pattern=group["pattern"],
            sample_event=group["sample_event"],
            occurrences=group["occurrences"],
            modules=group["modules"],
            first_seen=group["first_seen"],
            last_seen=group["last_seen"],
            severity=group.get("severity", "error"),
        )


class ErrorPatternsResponse(BaseModel):
    timestamp: str
    total_patterns: int
    min_occurrences: int
    hours_analyzed: int
    patterns: list[ErrorPattern]

    @classmethod
    def create(
        cls, patterns: list[dict[str, Any]], hours: int, min_occurrences: int
    ) -> ErrorPatternsResponse:
        from datetime import datetime

        return cls(
            timestamp=datetime.now().isoformat(),
            total_patterns=len(patterns),
            min_occurrences=min_occurrences,
            hours_analyzed=hours,
            patterns=[ErrorPattern.from_group(p) for p in patterns],
        )


class ErrorTimelineBucket(BaseModel):
    timestamp: str
    total: int
    by_level: dict[str, int]
    by_module: dict[str, int] = Field(default_factory=dict)


class ErrorTimeline(BaseModel):
    component: str | None
    hours_analyzed: int
    total_events: int
    timeline: list[ErrorTimelineBucket]

    @classmethod
    def create(
        cls,
        timeline_data: list[dict[str, Any]],
        component: str | None,
        hours: int,
        total: int,
    ) -> ErrorTimeline:
        buckets = [
            ErrorTimelineBucket(
                timestamp=bucket["timestamp"],
                total=bucket["total"],
                by_level=bucket["by_level"],
                by_module=bucket.get("by_module", {}),
            )
            for bucket in timeline_data
        ]

        return cls(
            component=component,
            hours_analyzed=hours,
            total_events=total,
            timeline=buckets,
        )


class ErrorSpike(BaseModel):
    timestamp: str
    window_start: str
    window_end: str
    error_count: int
    threshold: int
    modules_affected: list[str]
    sample_errors: list[dict[str, Any]]


class ErrorSpikesResponse(BaseModel):
    timestamp: str
    threshold: int
    window_minutes: int
    total_spikes: int
    spikes: list[ErrorSpike]

    @classmethod
    def create(
        cls, spikes: list[dict[str, Any]], threshold: int, window_minutes: int
    ) -> ErrorSpikesResponse:
        from datetime import datetime

        spike_list = [ErrorSpike(**s) for s in spikes]

        return cls(
            timestamp=datetime.now().isoformat(),
            threshold=threshold,
            window_minutes=window_minutes,
            total_spikes=len(spike_list),
            spikes=spike_list,
        )


class SymbolPerformance(BaseModel):
    symbol: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_pnl: float
    total_pnl: float
    best_trade: float
    worst_trade: float

    @classmethod
    def from_db_row(cls, symbol: str, row: dict[str, Any]) -> SymbolPerformance:
        return cls(
            symbol=symbol,
            total_trades=row.get("total_trades", 0),
            winning_trades=row.get("winning_trades", 0),
            losing_trades=row.get("losing_trades", 0),
            win_rate=row.get("win_rate", 0.0),
            avg_pnl=float(row.get("avg_pnl", 0.0)),
            total_pnl=float(row.get("total_pnl", 0.0)),
            best_trade=float(row.get("best_trade", 0.0)),
            worst_trade=float(row.get("worst_trade", 0.0)),
        )


class SymbolPerformanceResponse(BaseModel):
    timestamp: str
    days_analyzed: int
    total_symbols: int
    performances: dict[str, SymbolPerformance]

    @classmethod
    def create(
        cls, performance_data: dict[str, dict[str, Any]], days: int
    ) -> SymbolPerformanceResponse:
        from datetime import datetime

        performances = {
            symbol: SymbolPerformance.from_db_row(symbol, data)
            for symbol, data in performance_data.items()
        }

        return cls(
            timestamp=datetime.now().isoformat(),
            days_analyzed=days,
            total_symbols=len(performances),
            performances=performances,
        )


class MonthlyProjection(BaseModel):
    timestamp: str
    current_trades: int
    projected_monthly_trades: int
    current_pnl: str
    projected_monthly_pnl: str
    avg_daily_trades: float
    avg_daily_pnl: str
    days_in_month: int
    days_elapsed: int

    @classmethod
    def create(
        cls, trades: list[dict[str, Any]], current_trades: int | None
    ) -> MonthlyProjection:
        from datetime import datetime
        from decimal import Decimal

        now = datetime.now()
        days_in_month = 30
        days_elapsed = now.day

        closed_trades = [t for t in trades if t.get("status") == "CLOSED"]

        pnls = [
            Decimal(str(t.get("realized_pnl", "0")))
            for t in closed_trades
            if t.get("realized_pnl")
        ]

        current_pnl = sum(pnls) if pnls else Decimal("0")
        avg_daily_trades = len(closed_trades) / days_elapsed if days_elapsed > 0 else 0
        avg_daily_pnl = current_pnl / days_elapsed if days_elapsed > 0 else Decimal("0")

        projected_trades = int(avg_daily_trades * days_in_month)
        projected_pnl = avg_daily_pnl * days_in_month

        return cls(
            timestamp=now.isoformat(),
            current_trades=current_trades or len(closed_trades),
            projected_monthly_trades=projected_trades,
            current_pnl=str(current_pnl),
            projected_monthly_pnl=str(projected_pnl),
            avg_daily_trades=round(avg_daily_trades, 2),
            avg_daily_pnl=str(avg_daily_pnl),
            days_in_month=days_in_month,
            days_elapsed=days_elapsed,
        )


class SignalBreakdown(BaseModel):
    timestamp: str
    hours_analyzed: int
    total_signals: int
    signals_executed: int
    signals_rejected: int
    rejection_reasons: dict[str, int]
    execution_rate: float

    @classmethod
    def create(cls, log_entries: list[dict[str, Any]], hours: int) -> SignalBreakdown:
        from datetime import datetime

        signal_logs = [e for e in log_entries if "signal" in e.get("event", "").lower()]

        executed = [e for e in signal_logs if "execut" in e.get("event", "").lower()]
        rejected = [e for e in signal_logs if "reject" in e.get("event", "").lower()]

        rejection_reasons: dict[str, int] = {}
        for entry in rejected:
            reason = entry.get("reason", "unknown")
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

        execution_rate = (
            (len(executed) / len(signal_logs) * 100) if signal_logs else 0.0
        )

        return cls(
            timestamp=datetime.now().isoformat(),
            hours_analyzed=hours,
            total_signals=len(signal_logs),
            signals_executed=len(executed),
            signals_rejected=len(rejected),
            rejection_reasons=rejection_reasons,
            execution_rate=round(execution_rate, 2),
        )
