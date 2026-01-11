from typing import Any


class WebSocketStatusClient:
    def __init__(self):
        self.available = False

    def get_user_stream_status(self) -> dict[str, Any]:
        return {
            "status": "not_available",
            "message": "Direct WebSocket access requires bot running in same process",
            "connection_state": "unknown",
            "note": "Use log parsing to analyze WebSocket events (reconnects, disconnects, errors)",
        }

    def get_connection_state(self) -> dict[str, Any]:
        return {
            "status": "not_available",
            "message": "Direct WebSocket access requires bot running in same process",
            "connected": False,
            "note": "Use log parsing with filter: 'websocket' or 'user_stream'",
        }

    def get_reconnect_count(self) -> dict[str, Any]:
        return {
            "status": "not_available",
            "message": "Direct WebSocket access requires bot running in same process",
            "reconnects": 0,
            "note": "Use log parsing to count reconnection events",
        }

    def get_last_message_time(self) -> dict[str, Any]:
        return {
            "status": "not_available",
            "message": "Direct WebSocket access requires bot running in same process",
            "last_message": None,
            "note": "Use log parsing to find last WebSocket message timestamp",
        }
