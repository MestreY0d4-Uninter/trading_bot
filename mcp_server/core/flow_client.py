import json
import socket
from typing import Any


class FlowTrackerClient:
    def __init__(self, socket_path: str = "/tmp/flow_tracker.sock"):
        self.socket_path = socket_path
        self.timeout = 5.0

    def get_component_status(self) -> dict[str, Any]:
        try:
            client_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client_socket.settimeout(self.timeout)

            client_socket.connect(self.socket_path)

            request = json.dumps({"command": "status"})
            client_socket.sendall(request.encode("utf-8"))

            response_data = b""
            while True:
                chunk = client_socket.recv(4096)
                if not chunk:
                    break
                response_data += chunk

                try:
                    json.loads(response_data.decode("utf-8"))
                    break
                except json.JSONDecodeError:
                    continue

            client_socket.close()

            if response_data:
                return json.loads(response_data.decode("utf-8"))
            else:
                return self._get_offline_status()

        except (TimeoutError, FileNotFoundError, ConnectionRefusedError):
            return self._get_offline_status()
        except Exception as e:
            return {
                "overall_status": "ERROR",
                "error": str(e),
                "timestamp": "",
                "healthy": [],
                "stuck": [],
                "errors": [],
                "slow": [],
                "summary": {
                    "total_components": 0,
                    "healthy_count": 0,
                    "stuck_count": 0,
                    "error_count": 0,
                    "slow_count": 0,
                },
            }

    def _get_offline_status(self) -> dict[str, Any]:
        return {
            "overall_status": "OFFLINE",
            "message": "Flow Tracker not running or socket not available",
            "timestamp": "",
            "healthy": [],
            "stuck": [],
            "errors": [],
            "slow": [],
            "summary": {
                "total_components": 0,
                "healthy_count": 0,
                "stuck_count": 0,
                "error_count": 0,
                "slow_count": 0,
            },
        }

    def is_healthy(self) -> bool:
        status = self.get_component_status()
        return status.get("overall_status") == "HEALTHY"

    def get_stuck_components(self) -> list[dict[str, Any]]:
        status = self.get_component_status()
        return status.get("stuck", [])

    def get_component_errors(self) -> list[dict[str, Any]]:
        status = self.get_component_status()
        return status.get("errors", [])

    def get_slow_components(self) -> list[dict[str, Any]]:
        status = self.get_component_status()
        return status.get("slow", [])
