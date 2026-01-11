from typing import Any

from mcp_server.core.db_client import DatabaseClient
from mcp_server.core.log_parser import StructuredLogParser
from mcp_server.models.validator import (
    ApiRecoveryStatus,
    CircuitBreakerValidation,
    DataIntegrityReport,
    EmergencyStopSummary,
    IdempotencyStatus,
    RiskLimitValidation,
    TrackBeforeSendValidation,
)


def validate_circuit_breaker_persistence() -> dict[str, Any]:
    db = DatabaseClient()
    parser = StructuredLogParser()

    db_state = db.get_circuit_breaker_state()

    log_entries = parser.parse_log_file(hours=24)
    circuit_breaker_logs = [
        e
        for e in log_entries
        if "circuit" in e.get("event", "").lower()
        or "breaker" in e.get("event", "").lower()
    ]

    issues = []

    if not db_state and circuit_breaker_logs:
        issues.append("Circuit breaker events in logs but no DB state found")

    response = CircuitBreakerValidation.create(
        db_state=db_state, log_events=circuit_breaker_logs, issues=issues
    )

    return response.model_dump()


def validate_risk_limits() -> dict[str, Any]:
    db = DatabaseClient()

    open_trades = db.get_open_trades()
    open_positions = len(open_trades)

    max_positions = 5
    max_exposure = 10000.0

    total_exposure = 0.0
    for trade in open_trades:
        try:
            entry_price = float(trade.get("entry_price", 0))
            quantity = float(trade.get("quantity", 0))
            total_exposure += entry_price * quantity
        except (ValueError, TypeError):
            continue

    response = RiskLimitValidation.create(
        open_positions=open_positions,
        max_positions=max_positions,
        total_exposure=total_exposure,
        max_exposure=max_exposure,
    )

    return response.model_dump()


def validate_data_integrity(hours: int = 24) -> dict[str, Any]:
    db = DatabaseClient()

    trades = db.get_trades(limit=1000)

    response = DataIntegrityReport.create(trades=trades, hours=hours)

    return response.model_dump()


def validate_track_before_send() -> dict[str, Any]:
    parser = StructuredLogParser()

    log_entries = parser.parse_log_file(hours=24)

    response = TrackBeforeSendValidation.create(log_entries=log_entries)

    return response.model_dump()


def get_emergency_stops_summary(days: int = 7) -> dict[str, Any]:
    db = DatabaseClient()

    trades = db.get_trades(limit=1000)

    response = EmergencyStopSummary.create(trades=trades, days=days)

    return response.model_dump()


def get_api_recovery_status() -> dict[str, Any]:
    db = DatabaseClient()
    parser = StructuredLogParser()

    trades = db.get_trades(limit=100)

    log_entries = parser.parse_log_file(hours=24)

    response = ApiRecoveryStatus.create(db_trades=trades, log_entries=log_entries)

    return response.model_dump()


def get_idempotency_status(hours: int = 24) -> dict[str, Any]:
    parser = StructuredLogParser()

    log_entries = parser.parse_log_file(hours=hours)

    response = IdempotencyStatus.create(log_entries=log_entries, hours=hours)

    return response.model_dump()
