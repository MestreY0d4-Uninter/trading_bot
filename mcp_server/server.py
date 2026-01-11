from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from mcp_server.tools.advanced_optional import (
    get_client_order_id_lookup,
    get_db_write_metrics,
    get_market_analysis,
    get_position_history,
    get_websocket_diagnostics,
    run_testnet_validation,
)
from mcp_server.tools.analytics_errors import (
    analyze_error_patterns,
    detect_error_spikes,
    get_error_summary,
    get_error_timeline,
    get_monthly_performance,
    get_signal_breakdown,
    get_symbol_performance,
)
from mcp_server.tools.core_health import (
    find_problem_trades,
    get_bot_health,
    get_complete_analysis,
    get_position_tracker_state,
    get_reconciliation_status,
    get_user_stream_status,
    investigate_trade,
    search_logs,
)
from mcp_server.tools.validators_safety import (
    get_api_recovery_status,
    get_emergency_stops_summary,
    get_idempotency_status,
    validate_circuit_breaker_persistence,
    validate_data_integrity,
    validate_risk_limits,
    validate_track_before_send,
)

app = Server("trading-bot-analysis")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_complete_analysis",
            description="Get complete bot analysis including health, trades, positions, performance metrics, market data, and recent errors",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_bot_health",
            description="Get bot health status from Flow Tracker (22 components monitored via Unix Socket)",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_position_tracker_state",
            description="Get in-memory position tracker state (requires bot running in same process, otherwise shows unavailable)",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_user_stream_status",
            description="Get WebSocket user stream status by analyzing logs (connection state, reconnects, errors)",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="investigate_trade",
            description="Deep dive investigation of a specific trade (timeline, issues, related logs)",
            inputSchema={
                "type": "object",
                "properties": {
                    "trade_id": {
                        "type": "integer",
                        "description": "Trade ID to investigate",
                    }
                },
                "required": ["trade_id"],
            },
        ),
        Tool(
            name="find_problem_trades",
            description="Find trades with problems (negative PnL, emergency stops, still open)",
            inputSchema={
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Hours to look back (default: 24)",
                        "default": 24,
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="search_logs",
            description="Search logs by text with optional level filter",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to search for in logs",
                    },
                    "level": {
                        "type": "string",
                        "description": "Log level filter (info, error, warning) - optional",
                    },
                    "hours": {
                        "type": "integer",
                        "description": "Hours to look back (default: 24)",
                        "default": 24,
                    },
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="get_reconciliation_status",
            description="Get order reconciliation status (UNCERTAIN orders, resolution rate, recent events)",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="get_error_summary",
            description="Get error summary by level, module, and time (hour buckets)",
            inputSchema={
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Hours to look back (default: 24)",
                        "default": 24,
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="analyze_error_patterns",
            description="Analyze error patterns with grouping/aggregation (Sentry-like functionality)",
            inputSchema={
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Hours to look back (default: 24)",
                        "default": 24,
                    },
                    "min_occurrences": {
                        "type": "integer",
                        "description": "Minimum occurrences to include pattern (default: 3)",
                        "default": 3,
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="get_error_timeline",
            description="Get error timeline with hourly buckets, optionally filtered by component",
            inputSchema={
                "type": "object",
                "properties": {
                    "component": {
                        "type": "string",
                        "description": "Component/module to filter by (optional)",
                    },
                    "hours": {
                        "type": "integer",
                        "description": "Hours to look back (default: 24)",
                        "default": 24,
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="detect_error_spikes",
            description="Detect error spikes/anomalies using sliding window analysis",
            inputSchema={
                "type": "object",
                "properties": {
                    "threshold": {
                        "type": "integer",
                        "description": "Error count threshold to consider spike (default: 10)",
                        "default": 10,
                    },
                    "window_minutes": {
                        "type": "integer",
                        "description": "Window size in minutes (default: 60)",
                        "default": 60,
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="get_symbol_performance",
            description="Get trading performance by symbol (win rate, PnL, trade counts)",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Days to look back (default: 7)",
                        "default": 7,
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="get_monthly_performance",
            description="Get monthly performance projection based on current data",
            inputSchema={
                "type": "object",
                "properties": {
                    "current_trades": {
                        "type": "integer",
                        "description": "Current trade count (optional, will calculate if not provided)",
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="get_signal_breakdown",
            description="Get trading signal breakdown (executed, rejected, rejection reasons)",
            inputSchema={
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Hours to look back (default: 24)",
                        "default": 24,
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="validate_circuit_breaker_persistence",
            description="Validate circuit breaker state persistence between DB and logs",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="validate_risk_limits",
            description="Validate risk limits compliance (max positions, max exposure)",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="validate_data_integrity",
            description="Validate data integrity across trades (missing fields, invalid states, orphaned records)",
            inputSchema={
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Hours to look back (default: 24)",
                        "default": 24,
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="validate_track_before_send",
            description="Validate Track BEFORE Send pattern compliance (all orders tracked before API call)",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_emergency_stops_summary",
            description="Get emergency stop summary (frequency, reasons, symbols affected)",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Days to look back (default: 7)",
                        "default": 7,
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="get_api_recovery_status",
            description="Get API recovery status (startup recovery events, failed recoveries)",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_idempotency_status",
            description="Get idempotency status (duplicate prevention, retried orders)",
            inputSchema={
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Hours to look back (default: 24)",
                        "default": 24,
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="get_websocket_diagnostics",
            description="Get WebSocket diagnostics (connection health, reconnects, data gaps)",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_client_order_id_lookup",
            description="Lookup order by client_order_id (position state, order status, lifecycle events)",
            inputSchema={
                "type": "object",
                "properties": {
                    "client_order_id": {
                        "type": "string",
                        "description": "Client order ID to lookup",
                    }
                },
                "required": ["client_order_id"],
            },
        ),
        Tool(
            name="get_position_history",
            description="Get position history for a symbol (PnL distribution, win rate, all historical positions)",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Symbol to analyze (e.g., BTCUSDT)",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Days to look back (default: 30)",
                        "default": 30,
                    },
                },
                "required": ["symbol"],
            },
        ),
        Tool(
            name="get_db_write_metrics",
            description="Get database write metrics (write frequency, errors, per-table statistics)",
            inputSchema={
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Hours to look back (default: 24)",
                        "default": 24,
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="run_testnet_validation",
            description="Validate testnet configuration (API keys, testnet mode, balance check)",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_market_analysis",
            description="Get market analysis (symbol cooldowns, available symbols, last trade times)",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


def _handle_investigate_trade(arguments: dict) -> dict:
    trade_id = arguments.get("trade_id")
    if not trade_id:
        return {"error": "trade_id is required"}
    return investigate_trade(trade_id)


def _handle_search_logs(arguments: dict) -> dict:
    text = arguments.get("text")
    if not text:
        return {"error": "text is required"}
    return search_logs(text, arguments.get("level"), arguments.get("hours", 24))


def _handle_client_order_id_lookup(arguments: dict) -> dict:
    client_order_id = arguments.get("client_order_id")
    if not client_order_id:
        return {"error": "client_order_id is required"}
    return get_client_order_id_lookup(client_order_id)


def _handle_position_history(arguments: dict) -> dict:
    symbol = arguments.get("symbol")
    if not symbol:
        return {"error": "symbol is required"}
    return get_position_history(symbol, arguments.get("days", 30))


TOOL_HANDLERS = {
    # Core health tools (no arguments)
    "get_complete_analysis": lambda _: get_complete_analysis(),
    "get_bot_health": lambda _: get_bot_health(),
    "get_position_tracker_state": lambda _: get_position_tracker_state(),
    "get_user_stream_status": lambda _: get_user_stream_status(),
    "get_reconciliation_status": lambda _: get_reconciliation_status(),
    # Core health tools (with arguments)
    "investigate_trade": _handle_investigate_trade,
    "find_problem_trades": lambda args: find_problem_trades(args.get("hours", 24)),
    "search_logs": _handle_search_logs,
    # Error analytics
    "get_error_summary": lambda args: get_error_summary(args.get("hours", 24)),
    "analyze_error_patterns": lambda args: analyze_error_patterns(
        args.get("hours", 24), args.get("min_occurrences", 3)
    ),
    "get_error_timeline": lambda args: get_error_timeline(
        args.get("component"), args.get("hours", 24)
    ),
    "detect_error_spikes": lambda args: detect_error_spikes(
        args.get("threshold", 10), args.get("window_minutes", 60)
    ),
    # Performance analytics
    "get_symbol_performance": lambda args: get_symbol_performance(args.get("days", 7)),
    "get_monthly_performance": lambda args: get_monthly_performance(
        args.get("current_trades")
    ),
    "get_signal_breakdown": lambda args: get_signal_breakdown(args.get("hours", 24)),
    # Validators and safety
    "validate_circuit_breaker_persistence": lambda _: validate_circuit_breaker_persistence(),
    "validate_risk_limits": lambda _: validate_risk_limits(),
    "validate_data_integrity": lambda args: validate_data_integrity(
        args.get("hours", 24)
    ),
    "validate_track_before_send": lambda _: validate_track_before_send(),
    "get_emergency_stops_summary": lambda args: get_emergency_stops_summary(
        args.get("days", 7)
    ),
    "get_api_recovery_status": lambda _: get_api_recovery_status(),
    "get_idempotency_status": lambda args: get_idempotency_status(
        args.get("hours", 24)
    ),
    # Advanced optional
    "get_websocket_diagnostics": lambda _: get_websocket_diagnostics(),
    "get_client_order_id_lookup": _handle_client_order_id_lookup,
    "get_position_history": _handle_position_history,
    "get_db_write_metrics": lambda args: get_db_write_metrics(args.get("hours", 24)),
    "run_testnet_validation": lambda _: run_testnet_validation(),
    "get_market_analysis": lambda _: get_market_analysis(),
}


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    import json

    handler = TOOL_HANDLERS.get(name)
    if handler:
        result = handler(arguments)
    else:
        result = {"error": f"Unknown tool: {name}"}

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
