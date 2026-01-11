from typing import Any


class StateClient:
    def __init__(self):
        self.available = False

    def get_all_positions(self) -> dict[str, Any]:
        return {
            "status": "not_available",
            "message": "Direct PositionTracker access requires bot running in same process",
            "positions": {},
            "note": "Use DatabaseClient.get_open_trades() for historical data",
        }

    def get_position_by_symbol(self, symbol: str) -> dict[str, Any] | None:
        return {
            "status": "not_available",
            "message": "Direct PositionTracker access requires bot running in same process",
            "symbol": symbol,
            "note": "Use DatabaseClient.get_trades(symbol=symbol, status='OPEN') for historical data",
        }

    def get_position_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        return {
            "status": "not_available",
            "message": "Direct PositionTracker access requires bot running in same process",
            "client_order_id": client_order_id,
            "note": "Use log parsing to track client_order_id lifecycle",
        }

    def get_position_count(self) -> dict[str, Any]:
        return {
            "status": "not_available",
            "message": "Direct PositionTracker access requires bot running in same process",
            "count": 0,
            "note": "Use DatabaseClient.get_trades_count_by_status() for historical counts",
        }

    def get_total_exposure(self) -> dict[str, Any]:
        return {
            "status": "not_available",
            "message": "Direct PositionTracker access requires bot running in same process",
            "exposure": "0",
            "note": "Use DatabaseClient + current prices for exposure calculation",
        }
