"""
WebSocket Manager - Custom Implementation

Gerencia conexão WebSocket com Binance testnet/mainnet.

Responsabilidades:
- Conectar ao endpoint correto
- Subscrever streams configurados
- Receber e normalizar mensagens
- Reconexão automática com exponential backoff
- Integração FlowTracker
- Thread-safe message processing
- Decimal precision para preços

Nota: Implementação custom (não usa library externa).
"""

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import websockets
import websockets.client
from websockets.exceptions import ConnectionClosed, WebSocketException

from shared.observability.flow_tracker import flow_tracker, track_component
from shared.observability.logger import error, production, warning


@dataclass
class WebSocketConfig:
    """Configuração do WebSocket"""

    mode: str  # 'testnet' ou 'mainnet'
    symbols: list[str]  # ['btcusdt', 'ethusdt', ...]
    streams: list[str]  # ['kline_5m', 'miniTicker', ...]

    # Reconexão
    max_reconnect_attempts: int = 5
    initial_backoff: int = 1  # segundos
    max_backoff: int = 300  # 5 minutos
    backoff_multiplier: int = 2

    # Ping/Pong (por ambiente)
    ping_interval_testnet: int = 20
    ping_timeout_testnet: int = 60
    ping_interval_mainnet: int = 180
    ping_timeout_mainnet: int = 600

    # Performance
    message_queue_size: int = 1000
    max_lag_warning_ms: int = 500

    # Reconexão preventiva (antes de 24h Binance disconnect)
    preventive_reconnect_hours: int = 23


class WebSocketManager:
    """
    Gerencia conexão WebSocket com Binance.

    Features:
    - Reconexão automática com exponential backoff
    - Conversão Decimal automática
    - Integração FlowTracker
    - Thread-safe
    - Normalização de mensagens
    - Cache de últimos dados por símbolo
    """

    # Endpoints Binance
    ENDPOINTS = {
        "testnet": "wss://stream.testnet.binance.vision/ws",
        "mainnet": "wss://stream.binance.com:9443/ws",
    }

    def __init__(
        self, config: WebSocketConfig, circuit_breaker=None, ws_circuit_breaker=None
    ):
        self.config = config
        self.circuit_breaker = circuit_breaker
        self.ws_circuit_breaker = ws_circuit_breaker

        # Determinar endpoint e ping config
        self.endpoint = self.ENDPOINTS[config.mode]

        if config.mode == "testnet":
            self.ping_interval = config.ping_interval_testnet
            self.ping_timeout = config.ping_timeout_testnet
        else:
            self.ping_interval = config.ping_interval_mainnet
            self.ping_timeout = config.ping_timeout_mainnet

        # Estado da conexão
        self.connection: Any | None = None
        self.is_running = False
        self.is_connected = False
        self.connection_timestamp: float | None = None

        # Métricas
        self.messages_received = 0
        self.messages_dropped = 0
        self.reconnect_count = 0
        self.error_count = 0
        self.last_message_timestamp: float | None = None

        # Cache de últimos dados por símbolo
        self.latest_data: dict[str, dict] = {}

        # Callbacks registrados
        self.on_message_callbacks: list[Callable] = []

        # Circuit breaker para callbacks (Issue #47)
        self._callback_failures: dict[Callable, int] = {}
        self.MAX_CALLBACK_FAILURES = 5

        # Task tracking para prevenir memory leaks
        self._active_tasks: set[asyncio.Task] = set()

        # Message queue (para consumo externo se necessário)
        self.message_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=config.message_queue_size
        )

        production(
            "WebSocketManager initialized",
            extra={
                "mode": config.mode,
                "endpoint": self.endpoint,
                "symbols": config.symbols,
                "streams": config.streams,
                "ping_interval": self.ping_interval,
            },
        )

    @track_component("websocket_manager")
    async def start(self):
        """Inicia o WebSocket manager"""
        production("Starting WebSocket manager...")

        self.is_running = True

        # Executar loops em paralelo
        await asyncio.gather(
            self._connection_loop(),
            self._preventive_reconnect_loop(),
            self._health_monitor_loop(),
            return_exceptions=True,
        )

    async def _should_pause_for_circuit_breaker(self) -> bool:
        """Check if we should pause due to circuit breaker being open."""
        if self.circuit_breaker and self.circuit_breaker.is_open():
            warning("Circuit breaker OPEN, pausing WebSocket reconnection")
            await asyncio.sleep(60)
            return True
        return False

    async def _handle_connection_success(self) -> None:
        """Handle successful connection."""
        if self.circuit_breaker:
            self.circuit_breaker.record_success()

    def _handle_connection_error(self, error_type: str, error_msg: str) -> None:
        """Handle connection error and update metrics."""
        if error_type == "closed":
            warning(error_msg)
        else:
            error(error_msg, exc_info=(error_type == "unexpected"))
        self.is_connected = False
        self.error_count += 1
        if self.circuit_breaker:
            self.circuit_breaker.record_failure()

    async def _handle_reconnect(self, attempt: int) -> int:
        """Handle reconnection logic. Returns updated attempt count."""
        wait_time = await self._calculate_backoff(attempt)

        if attempt < self.config.max_reconnect_attempts:
            production(
                f"Reconnecting in {wait_time}s (attempt {attempt + 1}/{self.config.max_reconnect_attempts})"
            )
            await asyncio.sleep(wait_time)
            return attempt + 1

        error(f"Max reconnect attempts reached ({self.config.max_reconnect_attempts})")
        await asyncio.sleep(self.config.max_backoff)
        return 0

    async def _connection_loop(self):
        """Loop principal de conexão com reconexão automática."""
        attempt = 0

        while self.is_running:
            try:
                if await self._should_pause_for_circuit_breaker():
                    continue

                flow_tracker.force_component_update("websocket_manager")
                await self._connect()
                await self._handle_connection_success()
                attempt = 0

                async for message in self.connection:
                    await self._handle_message(message)
                    if self.circuit_breaker:
                        self.circuit_breaker.record_success()
                    flow_tracker.force_component_update("websocket_manager")

            except ConnectionClosed as e:
                self._handle_connection_error(
                    "closed", f"WebSocket connection closed: {e.code} {e.reason}"
                )

            except WebSocketException as e:
                self._handle_connection_error("websocket", f"WebSocket exception: {e}")

            except Exception as e:
                self._handle_connection_error(
                    "unexpected", f"Unexpected error in connection loop: {e}"
                )

            finally:
                if self.is_running:
                    attempt = await self._handle_reconnect(attempt)

    async def _connect(self):
        """Estabelece conexão WebSocket e subscreve streams"""
        production(f"Connecting to {self.endpoint}")

        # Conectar com ping/pong automático
        self.connection = await websockets.connect(
            self.endpoint,
            ping_interval=self.ping_interval,
            ping_timeout=self.ping_timeout,
            close_timeout=10,
        )

        self.is_connected = True
        self.connection_timestamp = time.time()
        self.reconnect_count += 1

        production(
            "WebSocket connected", extra={"reconnect_count": self.reconnect_count}
        )

        # Subscrever streams
        await self._subscribe_streams()

    async def _subscribe_streams(self):
        """Subscreve aos streams configurados"""
        # Construir lista de streams completos (symbol@stream)
        # IMPORTANT: Binance requires lowercase symbols in stream names
        stream_params = []
        for symbol in self.config.symbols:
            for stream in self.config.streams:
                stream_params.append(f"{symbol.lower()}@{stream}")

        subscribe_message = {
            "method": "SUBSCRIBE",
            "params": stream_params,
            "id": int(time.time()),
        }

        await self.connection.send(json.dumps(subscribe_message))

        production("Subscribed to streams", extra={"streams": stream_params})

    async def _handle_message(self, raw_message: str):
        try:
            message = json.loads(raw_message)

            if "result" in message or "id" in message:
                return

            self._update_message_metrics(message)

            normalized = self._normalize_message(message)
            if not normalized:
                return

            self._update_cache(normalized)
            await self._queue_message(normalized)
            await self._dispatch_callbacks(normalized)

        except json.JSONDecodeError as e:
            error(f"Invalid JSON: {e}", extra={"raw_message": raw_message[:200]})
        except Exception as e:
            error(f"Error handling message: {e}", exc_info=True)

    def _update_message_metrics(self, message: dict) -> None:
        self.messages_received += 1
        self.last_message_timestamp = time.time()

        if "E" in message:
            server_time = message["E"]
            local_time = int(time.time() * 1000)
            lag_ms = local_time - server_time

            if lag_ms > self.config.max_lag_warning_ms:
                warning(f"High lag: {lag_ms}ms", extra={"symbol": message.get("s")})

    def _update_cache(self, normalized: dict) -> None:
        symbol = normalized.get("symbol")
        if symbol:
            self.latest_data[symbol.upper()] = normalized

    async def _queue_message(self, normalized: dict) -> None:
        if not self.message_queue.full():
            await self.message_queue.put(normalized)
        else:
            self.messages_dropped += 1
            if self.messages_dropped % 10 == 0:
                warning(f"WebSocket backpressure: {self.messages_dropped} dropped")

    async def _dispatch_callbacks(self, normalized: dict) -> None:
        for callback in self.on_message_callbacks[:]:
            if asyncio.iscoroutinefunction(callback):
                self._dispatch_async_callback(callback, normalized)
            else:
                self._dispatch_sync_callback(callback, normalized)

    def _dispatch_async_callback(self, callback, normalized: dict) -> None:
        task = asyncio.create_task(
            self._safe_callback_wrapper(callback, normalized),
            name=f"ws-callback-{callback.__name__}",
        )
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)

    def _dispatch_sync_callback(self, callback, normalized: dict) -> None:
        try:
            callback(normalized)
            self._callback_failures[callback] = 0
        except Exception as e:
            error(f"Sync callback error: {e}")
            self._handle_callback_failure(callback)

    def _handle_callback_failure(self, callback) -> None:
        self._callback_failures[callback] = self._callback_failures.get(callback, 0) + 1

        if self._callback_failures[callback] >= self.MAX_CALLBACK_FAILURES:
            warning(f"Removing failing callback '{callback.__name__}'")
            if callback in self.on_message_callbacks:
                self.on_message_callbacks.remove(callback)
            self._callback_failures.pop(callback, None)

    def _normalize_message(self, raw_message: dict) -> dict | None:
        """
        Normaliza mensagem do WebSocket para formato padronizado.

        CRITICAL: Converte strings de preço para Decimal.

        Returns:
            dict com estrutura normalizada ou None se mensagem não relevante
        """
        event_type = raw_message.get("e")

        if event_type == "kline":
            return self._normalize_kline(raw_message)
        elif event_type == "24hrMiniTicker":
            return self._normalize_mini_ticker(raw_message)
        else:
            # Tipo de evento não esperado
            return None

    def _normalize_kline(self, raw_message: dict) -> dict:
        """
        Normaliza mensagem de kline.

        Estrutura original Binance:
        {
          "e": "kline",
          "E": 1638747660000,  // Event time
          "s": "BTCUSDT",
          "k": {
            "t": 1638747600000,  // Kline start time
            "T": 1638747899999,  // Kline close time
            "s": "BTCUSDT",
            "i": "5m",
            "o": "67234.50",  // Open
            "c": "67245.30",  // Close
            "h": "67250.00",  // High
            "l": "67230.00",  // Low
            "v": "123.456",   // Volume
            "x": false        // Is closed
          }
        }
        """
        kline = raw_message["k"]

        return {
            "type": "kline",
            "symbol": kline["s"],
            "interval": kline["i"],
            "event_time": raw_message["E"],
            "kline_start_time": kline["t"],
            "kline_close_time": kline["T"],
            "is_closed": kline["x"],  # CRITICAL: só processar se True
            # Preços (Decimal para precisão financeira)
            "open": Decimal(kline["o"]),
            "high": Decimal(kline["h"]),
            "low": Decimal(kline["l"]),
            "close": Decimal(kline["c"]),
            # Volume
            "volume": Decimal(kline["v"]),
            "quote_volume": Decimal(kline["q"]),
            # Trades
            "number_of_trades": kline["n"],
            # Raw para debug se necessário
            "raw": raw_message,
        }

    def _normalize_mini_ticker(self, raw_message: dict) -> dict:
        """
        Normaliza mensagem de miniTicker.

        Estrutura original Binance:
        {
          "e": "24hrMiniTicker",
          "E": 1638747660000,
          "s": "BTCUSDT",
          "c": "67234.50",  // Close
          "o": "67100.00",  // Open
          "h": "67300.00",  // High
          "l": "67050.00",  // Low
          "v": "1234.567"   // Volume
        }
        """
        return {
            "type": "miniTicker",
            "symbol": raw_message["s"],
            "event_time": raw_message["E"],
            # Preços (Decimal)
            "close": Decimal(raw_message["c"]),
            "open": Decimal(raw_message["o"]),
            "high": Decimal(raw_message["h"]),
            "low": Decimal(raw_message["l"]),
            # Volume
            "volume": Decimal(raw_message["v"]),
            "quote_volume": Decimal(raw_message["q"]),
            "raw": raw_message,
        }

    async def _calculate_backoff(self, attempt: int) -> int:
        """Calcula tempo de backoff exponencial"""
        backoff = min(
            self.config.max_backoff,
            self.config.initial_backoff * (self.config.backoff_multiplier**attempt),
        )
        return backoff

    async def _preventive_reconnect_loop(self):
        """
        Reconecta preventivamente antes de 24h (limite Binance).

        Binance desconecta automaticamente conexões após 24h.
        Este loop reconecta em 23h para evitar surpresas.
        """
        while self.is_running:
            await asyncio.sleep(3600)  # Check a cada hora

            if not self.is_connected or not self.connection_timestamp:
                continue

            connection_age_hours = (time.time() - self.connection_timestamp) / 3600

            if connection_age_hours >= self.config.preventive_reconnect_hours:
                production(
                    f"Preventive reconnect (connection age: {connection_age_hours:.1f}h)"
                )

                try:
                    await self.connection.close()
                except Exception as e:
                    warning(f"Error during preventive close: {e}")

    async def _health_monitor_loop(self):
        """
        Monitora saúde da conexão.

        Detecta:
        - Falta de mensagens por período longo
        - Connection freeze
        """
        while self.is_running:
            await asyncio.sleep(60)  # Check a cada minuto

            if not self.is_connected:
                continue

            # Verificar se recebeu mensagens recentemente
            if self.last_message_timestamp:
                time_since_last = time.time() - self.last_message_timestamp

                if time_since_last > 120:  # 2 minutos sem mensagens
                    warning(
                        f"No messages for {time_since_last:.0f}s",
                        extra={"action": "Possible connection freeze"},
                    )

    async def _safe_callback_wrapper(self, callback: Callable, message: dict):
        """
        Wrapper para callback com circuit breaker e exception tracking.

        Executa callback em task separada, trata erros e atualiza circuit breaker.
        """
        callback_name = callback.__name__

        try:
            await callback(message)

            self._callback_failures[callback] = 0

        except Exception as e:
            error(
                f"Callback '{callback_name}' failed",
                symbol=message.get("symbol"),
                error=str(e),
                exc_info=True,
            )

            self._callback_failures[callback] = (
                self._callback_failures.get(callback, 0) + 1
            )

            if self._callback_failures[callback] >= self.MAX_CALLBACK_FAILURES:
                warning(
                    f"Removing callback '{callback_name}' after {self.MAX_CALLBACK_FAILURES} consecutive failures",
                    extra={
                        "callback": callback_name,
                        "failures": self._callback_failures[callback],
                    },
                )
                if callback in self.on_message_callbacks:
                    self.on_message_callbacks.remove(callback)
                if callback in self._callback_failures:
                    del self._callback_failures[callback]

    def register_callback(self, callback: Callable):
        """
        Registra callback para ser chamado em cada mensagem.

        Callback deve ser async: async def callback(normalized_message: dict)
        """
        self.on_message_callbacks.append(callback)
        production(f"Callback registered: {callback.__name__}")

    def unregister_callback(self, callback: Callable) -> bool:
        """
        Remove callback da lista (Issue #47).

        Args:
            callback: Callback a ser removido

        Returns:
            True se callback foi removido, False se não estava registrado
        """
        try:
            self.on_message_callbacks.remove(callback)
            if callback in self._callback_failures:
                del self._callback_failures[callback]
            production(f"Callback unregistered: {callback.__name__}")
            return True
        except ValueError:
            warning(f"Callback not found: {callback.__name__}")
            return False

    def get_latest_data(self, symbol: str) -> dict | None:
        """Retorna últimos dados do símbolo do cache"""
        return self.latest_data.get(symbol.upper())

    def get_metrics(self) -> dict:
        """Retorna métricas atuais para monitoramento"""
        uptime_seconds = None
        if self.connection_timestamp:
            uptime_seconds = time.time() - self.connection_timestamp

        return {
            "status": "connected" if self.is_connected else "disconnected",
            "is_running": self.is_running,
            "uptime_seconds": uptime_seconds,
            "messages_received": self.messages_received,
            "reconnect_count": self.reconnect_count,
            "error_count": self.error_count,
            "symbols_tracked": len(self.latest_data),
            "queue_size": self.message_queue.qsize(),
            "last_message_age_seconds": (
                time.time() - self.last_message_timestamp
                if self.last_message_timestamp
                else None
            ),
        }

    async def stop(self):
        """Para o WebSocket manager gracefully"""
        production("Stopping WebSocket manager...")

        self.is_running = False

        if self._active_tasks:
            production(f"Cancelling {len(self._active_tasks)} active callback tasks...")
            for task in list(self._active_tasks):
                task.cancel()

            await asyncio.wait(self._active_tasks, timeout=5.0)

        if self.connection and self.is_connected:
            try:
                await self.connection.close()
            except Exception as e:
                warning(f"Error closing connection: {e}")

        production("WebSocket manager stopped")
