import json
import os
import socket
import threading
import time
from datetime import datetime
from functools import wraps
from typing import Any


class SimpleFlowTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.component_heartbeat: dict[str, float] = {}
        self.component_errors: dict[str, dict] = {}
        self.slow_components: dict[str, float] = {}

        self.socket_path = "/tmp/flow_tracker.sock"
        self.socket_server = None
        self.socket_thread = None
        self.socket_running = False

        self.component_timeouts = {
            # Componentes core que devem ser ativos frequentemente
            "data_fetcher": 180,  # 3 min (fetch de dados)
            "binance_api": 120,  # 2 min (API calls)
            "market_updater": 300,  # 5 min (ciclos ~33s, mas pode ter gaps)
            "trading_coordinator": 7200,  # 2h (on-demand, só ativo durante sinais de entrada)
            "position_manager": 7200,  # 2h (on-demand, só ativo durante trades)
            "position_tracker": 300,  # 5 min (tracking de posições)
            "signal_analyzer": 180,  # 3 min (análise contínua)
            # Componentes que podem ficar idle mais tempo
            "risk_manager": 7200,  # 2h (on-demand, validações esporádicas)
            "circuit_breaker": 1800,  # 30 min (só ativo em emergências)
            "health_monitor": 900,  # 15 min (check a cada 60s, pode ter gaps)
            "metrics_scheduler": 1200,  # 20 min (operação em background)
            # Infrastructure
            "db_handler": 300,  # 5 min (operações de dados)
            "cache": 600,  # 10 min (acesso esporádico)
            "state": 300,  # 5 min (updates constantes)
            "websocket_manager": 300,  # 5 min (conexão contínua com heartbeats)
            # Validators (chamados esporadicamente)
            "trading_validator": 1800,  # 30 min
            "market_validator": 900,  # 15 min
            "order_validator": 600,  # 10 min
            "correlation_validator": 1800,  # 30 min (usado durante trading ativo, pode ficar idle)
            # Services orquestradores
            "bot_orchestrator": 99999,  # Não monitorar (aguarda tasks forever, não retorna)
            "idempotency_handler": 600,  # 10 min (uso durante trades)
            "maintenance": 3600,  # 60 min (manutenção programada)
            # Core trading execution
            "order_executor": 3600,  # 60 min (on-demand, bot pode ficar idle sem trades)
            "trading_loop": 300,  # 5 min (loop principal de trading)
            # Analytics
            "market_analyzer": 600,  # 10 min (análise de mercado)
            "metrics": 300,  # 5 min (coleta contínua)
        }

        self._setup_socket_server()

    def track_component(self, component_name: str, slow_threshold: int = 10):
        def decorator(func):
            import inspect

            if inspect.iscoroutinefunction(func):

                @wraps(func)
                async def async_wrapper(*args, **kwargs):
                    start_time = time.time()

                    try:
                        result = await func(*args, **kwargs)

                        # Sucesso: atualizar heartbeat
                        with self._lock:
                            self.component_heartbeat[component_name] = time.time()

                            # Detectar funções lentas
                            duration = time.time() - start_time
                            if duration > slow_threshold:
                                self.slow_components[component_name] = duration
                            elif component_name in self.slow_components:
                                # Remover da lista de lentos se voltou ao normal
                                del self.slow_components[component_name]

                        return result

                    except Exception as e:
                        # Registrar erro mas não quebrar o bot
                        with self._lock:
                            self.component_errors[component_name] = {
                                "timestamp": time.time(),
                                "error": str(e)[:200],  # Primeiros 200 chars
                                "function": func.__name__,
                            }
                        raise

                return async_wrapper
            else:

                @wraps(func)
                def wrapper(*args, **kwargs):
                    start_time = time.time()

                    try:
                        result = func(*args, **kwargs)

                        # Sucesso: atualizar heartbeat
                        with self._lock:
                            self.component_heartbeat[component_name] = time.time()

                            # Detectar funções lentas
                            duration = time.time() - start_time
                            if duration > slow_threshold:
                                self.slow_components[component_name] = duration
                            elif component_name in self.slow_components:
                                # Remover da lista de lentos se voltou ao normal
                                del self.slow_components[component_name]

                        return result

                    except Exception as e:
                        # Registrar erro mas não quebrar o bot
                        with self._lock:
                            self.component_errors[component_name] = {
                                "timestamp": time.time(),
                                "error": str(e)[:200],  # Primeiros 200 chars
                                "function": func.__name__,
                            }
                        raise

                return wrapper

        return decorator

    def get_bot_status(self) -> dict[str, Any]:
        """Retorna status completo do bot em JSON"""
        now = time.time()

        with self._lock:
            status: dict[str, Any] = {
                "timestamp": datetime.now().isoformat(),
                "overall_status": "UNKNOWN",
                "healthy": [],
                "stuck": [],
                "errors": [],
                "slow": [],
                "summary": {
                    "total_components": 0,
                    "healthy_count": 0,
                    "stuck_count": 0,
                    "error_count": len(self.component_errors),
                    "slow_count": len(self.slow_components),
                },
            }

            monitored_components = 0
            for component, last_seen in self.component_heartbeat.items():
                timeout = self.component_timeouts.get(component, 300)
                if timeout >= 99999:
                    continue

                monitored_components += 1
                minutes_ago = max(0, int((now - last_seen) / 60))

                if (now - last_seen) > timeout:
                    status["stuck"].append(
                        {
                            "component": component,
                            "last_seen_minutes_ago": minutes_ago,
                            "timeout_minutes": int(timeout / 60),
                        }
                    )
                else:
                    status["healthy"].append(
                        {"component": component, "last_seen_minutes_ago": minutes_ago}
                    )

            status["summary"]["total_components"] = monitored_components

            # Adicionar erros recentes (últimas 24h)
            for component, error_info in self.component_errors.items():
                error_age_hours = (now - error_info["timestamp"]) / 3600
                if error_age_hours < 24:  # Últimas 24 horas
                    status["errors"].append(
                        {
                            "component": component,
                            "error": error_info["error"],
                            "function": error_info["function"],
                            "hours_ago": round(error_age_hours, 1),
                        }
                    )

            # Adicionar componentes lentos
            for component, duration in self.slow_components.items():
                status["slow"].append(
                    {"component": component, "duration_seconds": round(duration, 2)}
                )

            # Atualizar contadores do summary
            status["summary"]["healthy_count"] = len(status["healthy"])
            status["summary"]["stuck_count"] = len(status["stuck"])
            status["summary"]["slow_count"] = len(status["slow"])

            # Determinar status geral
            if status["summary"]["stuck_count"] > 0:
                status["overall_status"] = "CRITICAL"
            elif (
                status["summary"]["error_count"] > 5
                or status["summary"]["slow_count"] > 3
            ):
                status["overall_status"] = "WARNING"
            elif status["summary"]["healthy_count"] > 0:
                status["overall_status"] = "HEALTHY"
            else:
                status["overall_status"] = "NO_DATA"

            return status

    def is_bot_healthy(self) -> bool:
        """Verificação rápida se bot está saudável"""
        status = self.get_bot_status()
        return status["overall_status"] in ["HEALTHY", "WARNING"]

    def get_stuck_components(self) -> list[str]:
        """Retorna lista de componentes travados"""
        status = self.get_bot_status()
        return [item["component"] for item in status["stuck"]]

    def clear_old_errors(self, max_age_hours: int = 24) -> int:
        """Limpa erros antigos e retorna quantidade removida"""
        now = time.time()
        cutoff = now - (max_age_hours * 3600)

        with self._lock:
            old_count = len(self.component_errors)
            self.component_errors = {
                k: v
                for k, v in self.component_errors.items()
                if v["timestamp"] > cutoff
            }
            return old_count - len(self.component_errors)

    def force_component_update(self, component_name: str) -> bool:
        """Força update do heartbeat de um componente específico"""
        with self._lock:
            self.component_heartbeat[component_name] = time.time()
            return True

    def reset_flow_tracker(self) -> dict[str, Any]:
        """Reset completo do flow tracker"""
        with self._lock:
            errors_cleared = len(self.component_errors)
            slow_cleared = len(self.slow_components)

            self.component_errors.clear()
            self.slow_components.clear()

            # Não limpar heartbeats para manter dados reais
            return {
                "errors_cleared": errors_cleared,
                "slow_components_cleared": slow_cleared,
                "heartbeats_preserved": len(self.component_heartbeat),
                "timestamp": datetime.now().isoformat(),
            }

    def _setup_socket_server(self):
        """Setup Unix domain socket server for IPC"""
        try:
            if os.path.exists(self.socket_path):
                os.remove(self.socket_path)

            self.socket_server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.socket_server.bind(self.socket_path)
            self.socket_server.listen(5)
            self.socket_server.settimeout(1.0)

            self.socket_running = True
            self.socket_thread = threading.Thread(
                target=self._socket_listener, daemon=True, name="FlowTrackerSocket"
            )
            self.socket_thread.start()
        except Exception:
            pass

    def _socket_listener(self):
        """Listen for status requests via Unix socket"""
        while self.socket_running:
            try:
                conn, _ = self.socket_server.accept()
                status_data = self.get_bot_status()
                response = json.dumps(status_data).encode("utf-8")
                conn.sendall(response)
                conn.close()
            except TimeoutError:
                continue
            except Exception as e:
                if self.socket_running:
                    try:
                        import logging

                        logging.warning(f"Socket listener error: {e}")
                    except Exception:
                        pass
                    continue
                break

    def shutdown_socket(self):
        """Cleanup socket resources"""
        self.socket_running = False

        if self.socket_server:
            try:
                self.socket_server.close()
            except Exception:
                pass

        if self.socket_thread and self.socket_thread.is_alive():
            self.socket_thread.join(timeout=2.0)

        if os.path.exists(self.socket_path):
            try:
                os.remove(self.socket_path)
            except Exception:
                pass


# Instância global
flow_tracker = SimpleFlowTracker()


# Decorator para uso fácil
def track_component(component_name: str, slow_threshold: int = 10):
    return flow_tracker.track_component(component_name, slow_threshold)
