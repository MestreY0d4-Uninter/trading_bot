from datetime import UTC
from typing import Any

from mcp_server.core.db_client import DatabaseClient
from mcp_server.core.log_parser import StructuredLogParser
from mcp_server.models.error import (
    ErrorPatternsResponse,
    ErrorSpikesResponse,
    ErrorSummary,
    ErrorTimeline,
    MonthlyProjection,
    SignalBreakdown,
    SymbolPerformanceResponse,
)


def get_error_summary(hours: int = 24) -> dict[str, Any]:
    parser = StructuredLogParser()

    log_entries = parser.parse_log_file(hours=hours)

    errors_and_warnings = [
        e for e in log_entries if e.get("level") in ["error", "warning"]
    ]

    response = ErrorSummary.create(entries=errors_and_warnings, hours=hours)

    return response.model_dump()


def analyze_error_patterns(hours: int = 24, min_occurrences: int = 3) -> dict[str, Any]:
    parser = StructuredLogParser()

    log_entries = parser.parse_log_file(hours=hours)

    errors = parser.get_errors_only(log_entries)

    patterns = parser.group_by_event_similarity(errors, min_occurrences=min_occurrences)

    response = ErrorPatternsResponse.create(
        patterns=patterns, hours=hours, min_occurrences=min_occurrences
    )

    return response.model_dump()


def get_error_timeline(component: str | None = None, hours: int = 24) -> dict[str, Any]:
    parser = StructuredLogParser()

    log_entries = parser.parse_log_file(hours=hours)

    errors_and_warnings = [
        e for e in log_entries if e.get("level") in ["error", "warning"]
    ]

    if component:
        errors_and_warnings = parser.filter_by_module(errors_and_warnings, component)

    timeline_data = parser.get_timeline(errors_and_warnings, bucket_minutes=60)

    response = ErrorTimeline.create(
        timeline_data=timeline_data,
        component=component,
        hours=hours,
        total=len(errors_and_warnings),
    )

    return response.model_dump()


def detect_error_spikes(
    threshold: int = 10, window_minutes: int = 60
) -> dict[str, Any]:
    parser = StructuredLogParser()

    log_entries = parser.parse_log_file(hours=24)

    errors = parser.get_errors_only(log_entries)

    from datetime import datetime, timedelta

    spikes: list[dict[str, Any]] = []

    if not errors:
        return ErrorSpikesResponse.create(
            spikes=spikes, threshold=threshold, window_minutes=window_minutes
        ).model_dump()

    errors_by_time: list[tuple[datetime, dict[str, Any]]] = []
    for error in errors:
        timestamp_str = error.get("timestamp", "")
        if not timestamp_str:
            continue

        try:
            dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            errors_by_time.append((dt, error))
        except (ValueError, AttributeError):
            continue

    errors_by_time.sort(key=lambda x: x[0])

    window_delta = timedelta(minutes=window_minutes)

    for i, (current_time, _) in enumerate(errors_by_time):
        window_start = current_time
        window_end = current_time + window_delta

        errors_in_window = [
            error for dt, error in errors_by_time[i:] if window_start <= dt < window_end
        ]

        if len(errors_in_window) >= threshold:
            modules = set()
            for error in errors_in_window:
                module = error.get("module", "unknown")
                modules.add(module)

            spikes.append(
                {
                    "timestamp": current_time.isoformat(),
                    "window_start": window_start.isoformat(),
                    "window_end": window_end.isoformat(),
                    "error_count": len(errors_in_window),
                    "threshold": threshold,
                    "modules_affected": list(modules),
                    "sample_errors": errors_in_window[:5],
                }
            )

    unique_spikes = []
    seen_windows = set()
    for spike in spikes:
        window_key = spike["window_start"]
        if window_key not in seen_windows:
            unique_spikes.append(spike)
            seen_windows.add(window_key)

    response = ErrorSpikesResponse.create(
        spikes=unique_spikes, threshold=threshold, window_minutes=window_minutes
    )

    return response.model_dump()


def get_symbol_performance(days: int = 7) -> dict[str, Any]:
    db = DatabaseClient()

    performance_data = db.get_symbol_performance(days=days)

    response = SymbolPerformanceResponse.create(
        performance_data=performance_data, days=days
    )

    return response.model_dump()


def get_monthly_performance(current_trades: int | None = None) -> dict[str, Any]:
    db = DatabaseClient()

    trades = db.get_trades(limit=1000)

    response = MonthlyProjection.create(trades=trades, current_trades=current_trades)

    return response.model_dump()


def get_signal_breakdown(hours: int = 24) -> dict[str, Any]:
    parser = StructuredLogParser()

    log_entries = parser.parse_log_file(hours=hours)

    response = SignalBreakdown.create(log_entries=log_entries, hours=hours)

    return response.model_dump()
