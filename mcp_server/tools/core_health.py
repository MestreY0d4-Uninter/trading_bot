from typing import Any

from mcp_server.core.db_client import DatabaseClient
from mcp_server.core.flow_client import FlowTrackerClient
from mcp_server.core.log_parser import StructuredLogParser
from mcp_server.core.state_client import StateClient
from mcp_server.models.analysis import CompleteAnalysisResponse
from mcp_server.models.health import HealthCheckResponse
from mcp_server.models.market import LogSearchResponse
from mcp_server.models.position import (
    PositionTrackerResponse,
    ReconciliationStatus,
)
from mcp_server.models.trade import ProblemTradesResponse, TradeInvestigation


def get_complete_analysis() -> dict[str, Any]:
    db = DatabaseClient()
    parser = StructuredLogParser()
    flow_client = FlowTrackerClient()

    health = flow_client.get_component_status()

    trades = db.get_trades(limit=100)

    open_trades = db.get_open_trades()
    positions = [
        {
            "symbol": t["symbol"],
            "status": t["status"],
            "entry_price": t["entry_price"],
            "quantity": t["quantity"],
            "entry_time": t["entry_time"],
        }
        for t in open_trades
    ]

    market_data = db.get_market_data(limit=20)

    log_entries = parser.parse_log_file(hours=24)
    errors = parser.get_errors_only(log_entries)

    response = CompleteAnalysisResponse.create(
        health=health,
        trades=trades,
        positions=positions,
        market_data=market_data,
        errors=errors,
    )

    return response.model_dump()


def get_bot_health() -> dict[str, Any]:
    flow_client = FlowTrackerClient()
    status = flow_client.get_component_status()

    response = HealthCheckResponse.from_flow_tracker(status)

    return response.model_dump()


def get_position_tracker_state() -> dict[str, Any]:
    state_client = StateClient()

    if not state_client.available:
        response = PositionTrackerResponse.create_unavailable()
    else:
        positions = state_client.get_all_positions()
        exposure_data = state_client.get_total_exposure()
        response = PositionTrackerResponse.create_from_state(
            positions=positions, exposure=str(exposure_data.get("exposure", "0"))
        )

    return response.model_dump()


def get_user_stream_status() -> dict[str, Any]:
    parser = StructuredLogParser()

    log_entries = parser.parse_log_file(hours=24)

    websocket_logs = [
        entry
        for entry in log_entries
        if "websocket" in entry.get("event", "").lower()
        or "user_stream" in entry.get("event", "").lower()
        or "user_data" in entry.get("event", "").lower()
    ]

    connection_events = [
        entry for entry in websocket_logs if "connect" in entry.get("event", "").lower()
    ]
    reconnect_events = [
        entry
        for entry in websocket_logs
        if "reconnect" in entry.get("event", "").lower()
    ]
    error_events = [entry for entry in websocket_logs if entry.get("level") == "error"]

    last_message = websocket_logs[-1] if websocket_logs else None

    return {
        "status": "available",
        "connection_state": "connected" if connection_events else "unknown",
        "total_events": len(websocket_logs),
        "connection_events": len(connection_events),
        "reconnect_events": len(reconnect_events),
        "error_events": len(error_events),
        "last_message_time": last_message.get("timestamp") if last_message else None,
        "recent_events": websocket_logs[-10:],
    }


def investigate_trade(trade_id: int) -> dict[str, Any]:
    db = DatabaseClient()
    parser = StructuredLogParser()

    trade = db.get_trade_by_id(trade_id)

    if not trade:
        return {
            "error": "Trade not found",
            "trade_id": trade_id,
            "message": f"No trade found with ID {trade_id}",
        }

    symbol = trade["symbol"]

    log_entries = parser.parse_log_file(hours=24 * 7)
    trade_logs = [
        entry
        for entry in log_entries
        if str(trade_id) in str(entry)
        or symbol in entry.get("event", "")
        or symbol in entry.get("symbol", "")
    ]

    issues = [
        entry for entry in trade_logs if entry.get("level") in ["error", "warning"]
    ]

    response = TradeInvestigation.create(
        trade=trade, timeline_events=trade_logs, issues=issues, related_logs=trade_logs
    )

    return response.model_dump()


def find_problem_trades(hours: int = 24) -> dict[str, Any]:
    db = DatabaseClient()

    problem_trades = db.get_problem_trades(hours=hours)

    response = ProblemTradesResponse.create(trades=problem_trades, hours=hours)

    return response.model_dump()


def search_logs(text: str, level: str | None = None, hours: int = 24) -> dict[str, Any]:
    parser = StructuredLogParser()

    log_entries = parser.parse_log_file(hours=hours)

    filtered = parser.filter_by_text(log_entries, text)

    if level:
        filtered = parser.filter_by_level(filtered, level)

    response = LogSearchResponse.create(
        query=text, level=level, hours=hours, entries=filtered[:100]
    )

    return response.model_dump()


def get_reconciliation_status() -> dict[str, Any]:
    parser = StructuredLogParser()

    log_entries = parser.parse_log_file(hours=24 * 7)

    reconciliation_logs = [
        entry
        for entry in log_entries
        if "uncertain" in entry.get("event", "").lower()
        or "reconcil" in entry.get("event", "").lower()
        or "client_order_id" in entry
    ]

    response = ReconciliationStatus.from_logs(reconciliation_logs)

    return response.model_dump()
