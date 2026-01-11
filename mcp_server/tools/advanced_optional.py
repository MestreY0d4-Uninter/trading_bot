from typing import Any

from mcp_server.core.db_client import DatabaseClient
from mcp_server.core.log_parser import StructuredLogParser
from mcp_server.models.advanced import (
    DbWriteMetrics,
    MarketAnalysis,
    OrderLookupResult,
    PositionHistory,
    TestnetValidation,
    WebSocketDiagnostics,
)


def get_websocket_diagnostics() -> dict[str, Any]:
    parser = StructuredLogParser()

    log_entries = parser.parse_log_file(hours=24)

    response = WebSocketDiagnostics.create(log_entries=log_entries, hours=24)

    return response.model_dump()


def get_client_order_id_lookup(client_order_id: str) -> dict[str, Any]:
    db = DatabaseClient()
    parser = StructuredLogParser()

    db_trades = db.get_trades(limit=1000)

    log_entries = parser.parse_log_file(hours=72)

    response = OrderLookupResult.create(
        client_order_id=client_order_id,
        db_trades=db_trades,
        log_entries=log_entries,
    )

    return response.model_dump()


def get_position_history(symbol: str, days: int = 30) -> dict[str, Any]:
    db = DatabaseClient()

    all_trades = db.get_trades(symbol=symbol, limit=5000)

    response = PositionHistory.create(symbol=symbol, trades=all_trades, days=days)

    return response.model_dump()


def get_db_write_metrics(hours: int = 24) -> dict[str, Any]:
    parser = StructuredLogParser()

    log_entries = parser.parse_log_file(hours=hours)

    response = DbWriteMetrics.create(log_entries=log_entries, hours=hours)

    return response.model_dump()


def run_testnet_validation() -> dict[str, Any]:
    from pathlib import Path

    import yaml  # type: ignore

    config_path = Path("config/settings.yaml")

    if not config_path.exists():
        return {
            "testnet_active": False,
            "api_key_configured": False,
            "api_secret_configured": False,
            "base_url": None,
            "balance_check": "config_not_found",
            "warnings": ["Config file not found at config/settings.yaml"],
            "recommendations": [
                "Create config/settings.yaml from template or run bot once to generate default config"
            ],
        }

    try:
        with open(config_path) as f:
            config_data = yaml.safe_load(f)
    except Exception as e:
        return {
            "testnet_active": False,
            "api_key_configured": False,
            "api_secret_configured": False,
            "base_url": None,
            "balance_check": "config_error",
            "warnings": [f"Failed to load config: {str(e)}"],
            "recommendations": ["Check config/settings.yaml for YAML syntax errors"],
        }

    response = TestnetValidation.create(config_data=config_data)

    return response.model_dump()


def get_market_analysis() -> dict[str, Any]:
    db = DatabaseClient()

    trades = db.get_trades(limit=5000)

    response = MarketAnalysis.create(trades=trades, cooldown_hours=24)

    return response.model_dump()
