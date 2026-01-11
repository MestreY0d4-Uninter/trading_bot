import asyncio
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from binance import AsyncClient
from binance.exceptions import BinanceAPIException

from core.risk.circuit_breaker import CircuitBreaker
from shared.constants import (
    BINANCE_API_RESPONSE_VALIDATORS,
    BINANCE_ERROR_RECOVERY_STRATEGIES,
)
from shared.observability.flow_tracker import track_component
from shared.observability.logger import debug, error, production, warning
from shared.observability.metrics import metrics
from shared.rate_limiter import rate_limiter
from shared.timeouts import Timeouts


class ConnectionPoolStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    RECONNECTING = "reconnecting"


@dataclass
class ConnectionStats:
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    avg_latency_ms: float = 0.0
    last_used: datetime | None = None
    last_health_check: datetime | None = None


class BinanceClient:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.mode = config["mode"]

        # Connection pool management
        self._primary_client = None
        self._backup_clients: list[AsyncClient] = []
        self._client_stats: dict[str, ConnectionStats] = {}
        self._active_client_id = "primary"
        self._connection_pool_size = config.get("connection_pool", {}).get("size", 3)
        self._pool_status = ConnectionPoolStatus.HEALTHY

        # Circuit breaker for API resilience (infrastructure failures)
        # Note: Intentionally NOT persisted to database
        #
        # Rationale:
        # - Manages transient infrastructure failures (Binance API downtime, timeouts)
        # - Auto-resets after cooldown period (typically 60-300s via can_execute())
        # - Persisting would cause false negatives: circuit stays open after
        #   bot restart even if Binance API has recovered
        # - Different from EmergencyManager's circuit breaker which IS persisted
        #   for financial risk management (daily losses, drawdown limits)
        #
        # Design Decision: In-memory circuit breaker with auto-reset is correct
        # for API protection layer.
        self.circuit_breaker = CircuitBreaker(config)  # ✅ Intentionally no db
        self._failure_count = 0
        self._last_failure_time = None
        self._backoff_multiplier = 1.0

        # Async-safe locks
        self._connection_lock = asyncio.Lock()
        self._pool_lock = asyncio.Lock()
        self._cleanup_called = False

        # Enhanced rate limiting
        self.api_calls: deque = deque(maxlen=1200)
        self._last_rate_warning = 0
        self._adaptive_rate_limit = True
        self._current_rate_limit = 1200  # Start conservative

        # Health monitoring
        self._health_check_interval = 60  # seconds
        self._last_health_check = None
        self._consecutive_health_failures = 0

        self.endpoint_weights = {
            "get_account": 10,
            "get_symbol_ticker": 1,
            "get_order_book": 1,
            "get_klines": 1,
            "create_order": 1,
            "cancel_order": 1,
            "cancel_oco_order": 1,
            "get_open_orders": 40,
            "ping": 1,
            "get_symbol_info": 10,
            "get_exchange_info": 10,
            "create_oco_order": 2,
            "get_all_tickers": 40,
            "get_ticker": 1,
        }

    def get_endpoint_weight(self, endpoint: str) -> int:
        return self.endpoint_weights.get(endpoint, 1)

    def _validate_api_response(self, response: Any, response_type: str) -> bool:
        if not response:
            return False

        if not isinstance(response, (dict, list)):
            return False

        if response_type not in BINANCE_API_RESPONSE_VALIDATORS:
            return True

        required_fields = BINANCE_API_RESPONSE_VALIDATORS[response_type]

        if isinstance(response, dict):
            return all(field in response for field in required_fields)
        elif isinstance(response, list) and required_fields:
            return (
                len(response) > 0
                and all(
                    isinstance(item, dict)
                    and all(field in item for field in required_fields)
                    for item in response[:3]
                )
                if response
                else False
            )

        return True

    def _get_error_strategy(self, error_code: int) -> dict:
        return BINANCE_ERROR_RECOVERY_STRATEGIES.get(
            error_code,
            {"recoverable": False, "description": f"Unknown error code: {error_code}"},
        )

    def _should_retry_error(self, error_code: int, attempt: int) -> bool:
        strategy = self._get_error_strategy(error_code)
        if not strategy.get("recoverable", False):
            return False

        max_retries = strategy.get("max_retries", 2)
        return attempt < max_retries

    def _calculate_wait_time(self, error_code: int, attempt: int) -> float:
        strategy = self._get_error_strategy(error_code)
        wait_multiplier = strategy.get("wait_multiplier", 1)

        if error_code == -1003:
            return min(wait_multiplier * (2**attempt), 10)

        return min(wait_multiplier * (1.5**attempt), 5)

    async def __aenter__(self):
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _create_client_connection(self, client_id: str) -> AsyncClient:
        """Create a single client connection with proper validation"""
        try:
            production(f"      → [{client_id}] Obtendo credenciais...")
            binance_config = self.config.get("binance", {})

            if self.mode == "testnet":
                api_key = binance_config.get("testnet_api_key")
                api_secret = binance_config.get("testnet_api_secret")
            else:
                api_key = binance_config.get("real_api_key")
                api_secret = binance_config.get("real_api_secret")

            # Validate credentials
            production(f"      → [{client_id}] Validando credenciais...")
            if not all([api_key, api_secret]):
                raise ValueError(f"Credenciais ausentes para {self.mode}")

            api_key, api_secret = api_key.strip(), api_secret.strip()

            if len(api_key) != 64 or not api_key.isalnum():
                raise ValueError(
                    "API key inválida: deve ter 64 caracteres alfanuméricos"
                )

            if len(api_secret) != 64 or not api_secret.isalnum():
                raise ValueError(
                    "API secret inválida: deve ter 64 caracteres alfanuméricos"
                )

            production(f"      ✓ [{client_id}] Credenciais válidas")

            # Create client with timeout
            production(f"      → [{client_id}] Criando AsyncClient (pode levar 20s)...")
            if self.mode == "testnet":
                async with asyncio.timeout(Timeouts.CLIENT_CREATE):
                    client = await AsyncClient.create(
                        api_key=api_key, api_secret=api_secret, testnet=True
                    )
                production(f"      ✓ [{client_id}] AsyncClient criado para testnet")
            else:
                async with asyncio.timeout(Timeouts.CLIENT_CREATE):
                    client = await AsyncClient.create(
                        api_key=api_key, api_secret=api_secret
                    )
                production(f"      ✓ [{client_id}] AsyncClient criado para real")

            # Initialize connection stats
            production(f"      → [{client_id}] Inicializando connection stats...")
            self._client_stats[client_id] = ConnectionStats(
                last_used=datetime.now(), last_health_check=datetime.now()
            )
            production(f"      ✓ [{client_id}] Stats inicializados")

            # Validate connection with ping
            production(f"      → [{client_id}] Testando conexão (ping)...")
            async with asyncio.timeout(Timeouts.CLIENT_PING):
                await client.ping()
            production(f"      ✓ [{client_id}] Ping bem-sucedido")

            production(f"      ✅ [{client_id}] Cliente criado e validado com sucesso")
            return client

        except Exception as e:
            error(f"Falha ao criar cliente {client_id}", error=str(e))
            raise RuntimeError(
                f"Falha na criação do cliente {client_id}: {str(e)}"
            ) from e

    async def _initialize_connection_pool(self):
        """Initialize connection pool with primary + backup clients"""
        try:
            # Create primary client
            production("   → Criando cliente primário...")
            try:
                async with asyncio.timeout(Timeouts.POOL_INIT_CLIENT):
                    self._primary_client = await self._create_client_connection(
                        "primary"
                    )
                production("   ✓ Cliente primário criado")
            except TimeoutError:
                error("   ✗ TIMEOUT ao criar cliente primário após 25s")
                raise RuntimeError("Primary client creation timeout") from None

            # Testnet limits to 2 simultaneous connections per API key
            max_pool_size = 2 if self.mode == "testnet" else self._connection_pool_size
            backup_count = min(max_pool_size - 1, 2)

            if backup_count > 0:
                debug(
                    f"Criando {backup_count} clientes de backup (mode={self.mode}, max_pool={max_pool_size})..."
                )
                for i in range(backup_count):
                    try:
                        backup_client = await self._create_client_connection(
                            f"backup_{i}"
                        )
                        self._backup_clients.append(backup_client)
                    except Exception as e:
                        warning(f"Falha ao criar cliente backup {i}", error=str(e))
                        # Continue anyway - backup is optional

            self._pool_status = ConnectionPoolStatus.HEALTHY
            production(
                "Pool de conexões inicializado",
                primary_ready=self._primary_client is not None,
                backup_count=len(self._backup_clients),
                total_connections=1 + len(self._backup_clients),
            )

        except Exception as e:
            self._pool_status = ConnectionPoolStatus.FAILED
            error("Falha crítica na inicialização do pool de conexões", error=str(e))
            raise

    @track_component("binance_api")
    async def initialize(self):
        try:
            production("🔧 [1/5] Validando configuração do BinanceClient...")
            if not isinstance(self.mode, str) or self.mode not in ["testnet", "real"]:
                raise ValueError(
                    f"Modo inválido: {self.mode}. Deve ser 'testnet' ou 'real'"
                )

            binance_config = self.config.get("binance", {})
            if not isinstance(binance_config, dict):
                raise ValueError("Configuração 'binance' não encontrada ou inválida")

            production(f"✓ Configuração validada - Mode: {self.mode}")

            # Initialize connection pool instead of single client
            production("🔧 [2/5] Inicializando connection pool...")
            try:
                async with asyncio.timeout(Timeouts.POOL_INIT_FULL):
                    await self._initialize_connection_pool()
                production("✓ Connection pool inicializado")
            except TimeoutError:
                error("TIMEOUT ao inicializar connection pool após 30s")
                raise RuntimeError("Connection pool initialization timeout") from None

            # Perform comprehensive validation
            production("🔧 [3/5] Validando todas as conexões...")
            try:
                async with asyncio.timeout(Timeouts.POOL_VALIDATION):
                    await self._validate_all_connections()
                production("✓ Conexões validadas")
            except TimeoutError:
                error("TIMEOUT ao validar conexões após 20s")
                raise RuntimeError("Connection validation timeout") from None

            # Setup adaptive rate limiting
            production("🔧 [4/5] Configurando rate limiting adaptativo...")
            self._setup_adaptive_rate_limiting()
            production("✓ Rate limiting configurado")

            production(
                "✅ [5/5] Binance client inicializado com sucesso",
                mode=self.mode,
                pool_status=self._pool_status.value,
                connections=1 + len(self._backup_clients),
            )

        except Exception as e:
            error(
                "CRÍTICO: Falha ao inicializar Binance client",
                mode=self.mode,
                error=str(e),
            )
            await self._cleanup_all_connections()
            raise

    async def _validate_all_connections(self):
        """Validate all connections in the pool"""
        validation_tasks = []

        # Validate primary client
        if self._primary_client:
            validation_tasks.append(
                self._validate_single_connection("primary", self._primary_client)
            )

        # Validate backup clients
        for i, backup_client in enumerate(self._backup_clients):
            validation_tasks.append(
                self._validate_single_connection(f"backup_{i}", backup_client)
            )

        results = await asyncio.gather(*validation_tasks, return_exceptions=True)

        successful_validations = sum(1 for result in results if result is True)
        total_validations = len(results)

        if successful_validations == 0:
            raise RuntimeError("Todas as validações de conexão falharam")

        if successful_validations < total_validations:
            warning(
                "Algumas validações falharam",
                successful=successful_validations,
                total=total_validations,
            )
            self._pool_status = ConnectionPoolStatus.DEGRADED

        production(
            "Validação do pool concluída",
            successful=successful_validations,
            total=total_validations,
        )

    async def _validate_single_connection(
        self, client_id: str, client: AsyncClient
    ) -> bool:
        """Validate a single connection"""
        try:
            # Test connectivity
            async with asyncio.timeout(Timeouts.CLIENT_RECONNECT):
                await client.ping()

            # Test permissions
            async with asyncio.timeout(Timeouts.REQUEST_ACCOUNT):
                account_info = await client.get_account()
            if not self._validate_api_response(account_info, "account"):
                raise RuntimeError("Resposta de account inválida")

            # Update stats
            if client_id in self._client_stats:
                self._client_stats[client_id].last_health_check = datetime.now()

            debug(f"Cliente {client_id} validado com sucesso")
            return True

        except Exception as e:
            error(f"Falha na validação do cliente {client_id}", error=str(e))
            return False

    def _setup_adaptive_rate_limiting(self):
        """Setup adaptive rate limiting based on connection pool health"""
        if self._pool_status == ConnectionPoolStatus.HEALTHY:
            self._current_rate_limit = 1200  # Full rate
        elif self._pool_status == ConnectionPoolStatus.DEGRADED:
            self._current_rate_limit = 800  # Reduced rate
        else:
            self._current_rate_limit = 400  # Conservative rate

        debug(
            f"Rate limit ajustado para {self._current_rate_limit} requests/minute",
            pool_status=self._pool_status.value,
        )

    async def _get_healthy_client(self) -> AsyncClient:
        """Get a healthy client from the pool with failover"""
        async with self._pool_lock:
            # Try primary client first
            if self._primary_client and self._active_client_id == "primary":
                if await self._check_client_health("primary", self._primary_client):
                    return self._primary_client
                else:
                    warning("Cliente primário não está saudável, tentando backup")

            # Try backup clients
            for i, backup_client in enumerate(self._backup_clients):
                client_id = f"backup_{i}"
                if await self._check_client_health(client_id, backup_client):
                    self._active_client_id = client_id
                    debug(f"Usando cliente backup {i}")
                    return backup_client

            # If all clients failed, try to reconnect primary
            if self._primary_client:
                try:
                    await self._reconnect_client("primary")
                    if self._primary_client:
                        self._active_client_id = "primary"
                        return self._primary_client
                except Exception as e:
                    error("Falha na reconexão do cliente primário", error=str(e))

            self._pool_status = ConnectionPoolStatus.FAILED
            raise RuntimeError("Nenhum cliente saudável disponível no pool")

    async def _check_client_health(self, client_id: str, client: AsyncClient) -> bool:
        """Quick health check for a client"""
        try:
            if not client:
                return False

            # Check if health check is recent enough
            stats = self._client_stats.get(client_id)
            if stats and stats.last_health_check:
                time_since_check = datetime.now() - stats.last_health_check
                if time_since_check.seconds < self._health_check_interval:
                    return True  # Assume healthy if recently checked

            # Perform quick ping
            async with asyncio.timeout(Timeouts.CLIENT_HEALTH_CHECK):
                await client.ping()

            # Update health check time
            if client_id in self._client_stats:
                self._client_stats[client_id].last_health_check = datetime.now()

            return True

        except Exception:
            return False

    async def _reconnect_client(self, client_id: str):
        """Reconnect a specific client"""
        try:
            debug(f"Tentando reconectar cliente {client_id}")

            if client_id == "primary":
                if self._primary_client:
                    await self._primary_client.close_connection()
                self._primary_client = await self._create_client_connection("primary")
            else:
                # Handle backup client reconnection
                backup_index = int(client_id.split("_")[1])
                if (
                    backup_index < len(self._backup_clients)
                    and self._backup_clients[backup_index]
                ):
                    await self._backup_clients[backup_index].close_connection()
                    self._backup_clients[backup_index] = (
                        await self._create_client_connection(client_id)
                    )

            production(f"Cliente {client_id} reconectado com sucesso")

        except Exception as e:
            error(f"Falha na reconexão do cliente {client_id}", error=str(e))
            raise

    async def _check_circuit_breaker_with_backoff(self) -> None:
        if not self.circuit_breaker or not await self.circuit_breaker.can_execute():
            if self._last_failure_time:
                time_since_failure = time.time() - self._last_failure_time
                required_backoff = self._backoff_multiplier * (
                    2 ** min(self._failure_count, 6)
                )
                if time_since_failure < required_backoff:
                    raise RuntimeError(
                        f"Circuit breaker is open - aguarde {required_backoff - time_since_failure:.1f}s"
                    )
            else:
                raise RuntimeError(
                    "Circuit breaker is open - sistema em modo de proteção"
                )

    async def _apply_adaptive_rate_limiting(self, endpoint_name: str) -> None:
        weight = self.get_endpoint_weight(endpoint_name)

        if self._pool_status == ConnectionPoolStatus.DEGRADED:
            weight = int(weight * 1.5)
        elif self._pool_status == ConnectionPoolStatus.FAILED:
            weight = int(weight * 2.0)

        await rate_limiter.acquire(weight, endpoint_name)

    def _calculate_max_retries(self) -> int:
        return 3 if self._pool_status == ConnectionPoolStatus.HEALTHY else 2

    async def _execute_with_timeout_and_record_success(
        self, func, endpoint_name: str, start_time: float, *args, **kwargs
    ):
        timeout = self._get_dynamic_timeout(endpoint_name)
        async with asyncio.timeout(timeout):
            result = await func(*args, **kwargs)

        self.circuit_breaker.record_success()
        self._failure_count = 0
        self._last_failure_time = None
        self._backoff_multiplier = 1.0

        latency_ms = (time.time() - start_time) * 1000
        self._update_client_stats(self._active_client_id, latency_ms, True)
        await metrics.record_api_call(endpoint_name, latency_ms, True)

        if self._adaptive_rate_limit and latency_ms < 100:
            self._current_rate_limit = min(self._current_rate_limit + 10, 1200)

        return result

    async def _handle_timeout_error(
        self, endpoint_name: str, attempt: int, max_retries: int, start_time: float
    ) -> None:
        self._handle_request_failure(endpoint_name, "timeout", attempt, start_time)
        if attempt < max_retries:
            await asyncio.sleep(min(2**attempt, 5))
            return
        self._record_critical_failure()
        raise RuntimeError(f"Timeout persistente para {endpoint_name}")

    async def _handle_binance_api_error(
        self,
        e: BinanceAPIException,
        func,
        endpoint_name: str,
        attempt: int,
        max_retries: int,
        start_time: float,
    ):
        latency_ms = (time.time() - start_time) * 1000
        strategy = self._get_error_strategy(e.code)
        is_recoverable = strategy.get("recoverable", False)

        if e.code == -1003:
            self._handle_rate_limit_error(e, attempt)
            if attempt < max_retries:
                wait_time = self._calculate_wait_time(e.code, attempt)
                await asyncio.sleep(wait_time)
                return await self._get_healthy_client()

        if is_recoverable and self._should_retry_error(e.code, attempt):
            wait_time = self._calculate_wait_time(e.code, attempt)
            warning(
                f"Erro recuperável na tentativa {attempt + 1} para {endpoint_name}",
                code=e.code,
                message=e.message,
                description=strategy.get("description"),
            )

            if attempt > 0:
                try:
                    client = await self._get_healthy_client()
                    func = getattr(client, func.__name__)
                except Exception:
                    pass

            await asyncio.sleep(wait_time)
            return await self._get_healthy_client()

        self._update_client_stats(self._active_client_id, latency_ms, False)

        should_circuit_break = not is_recoverable or e.code in [-2010, -2011]

        if should_circuit_break:
            self._record_critical_failure()
            error(
                "Binance API error crítico (circuit breaker ativado)",
                code=e.code,
                message=e.message,
                endpoint=endpoint_name,
                description=strategy.get("description"),
            )
        else:
            warning(
                "Binance API error após todas as tentativas",
                code=e.code,
                message=e.message,
                endpoint=endpoint_name,
                description=strategy.get("description"),
            )

        await metrics.record_api_call(endpoint_name, latency_ms, False)
        raise

    async def _handle_generic_error(
        self,
        e: Exception,
        func,
        endpoint_name: str,
        attempt: int,
        max_retries: int,
        start_time: float,
    ):
        self._handle_request_failure(endpoint_name, "unexpected", attempt, start_time)

        if attempt < max_retries:
            try:
                client = await self._get_healthy_client()
                func = getattr(client, func.__name__)
            except Exception:
                pass

            await asyncio.sleep(min(2**attempt, 5))
            return await self._get_healthy_client()

        self._record_critical_failure()
        error(
            "Request error crítico após todas as tentativas",
            func=func.__name__ if hasattr(func, "__name__") else str(func),
            endpoint=endpoint_name,
            error=str(e),
        )

        latency_ms = (time.time() - start_time) * 1000
        self._update_client_stats(self._active_client_id, latency_ms, False)
        await metrics.record_api_call(endpoint_name, latency_ms, False)
        raise

    async def _execute_request(self, func, endpoint_name: str, *args, **kwargs):
        await self._check_circuit_breaker_with_backoff()

        client = await self._get_healthy_client()
        await self._apply_adaptive_rate_limiting(endpoint_name)

        start_time = time.time()
        max_retries = self._calculate_max_retries()

        for attempt in range(max_retries + 1):
            try:
                if attempt > 0:
                    debug(
                        f"Tentativa {attempt + 1}/{max_retries + 1} para {endpoint_name}"
                    )
                    await asyncio.sleep(min(2**attempt, 5))

                result = await self._execute_with_timeout_and_record_success(
                    func, endpoint_name, start_time, *args, **kwargs
                )

                if attempt > 0:
                    production(
                        f"✓ Sucesso na tentativa {attempt + 1} para {endpoint_name}"
                    )

                return result

            except TimeoutError:
                await self._handle_timeout_error(
                    endpoint_name, attempt, max_retries, start_time
                )
                continue

            except BinanceAPIException as e:
                new_client = await self._handle_binance_api_error(
                    e, func, endpoint_name, attempt, max_retries, start_time
                )
                if new_client:
                    client = new_client
                    func = getattr(client, func.__name__)
                continue

            except Exception as e:
                new_client = await self._handle_generic_error(
                    e, func, endpoint_name, attempt, max_retries, start_time
                )
                if new_client:
                    client = new_client
                    func = getattr(client, func.__name__)
                continue

    async def _execute_emergency_request(
        self, func, endpoint_name: str, *args, **kwargs
    ):
        """CORREÇÃO CRÍTICA: Execução emergencial bypass de rate limiting e retries"""
        # BYPASS: Ignorar circuit breaker para emergências
        production(
            f"🚨 EXECUÇÃO EMERGENCIAL INICIADA: {endpoint_name}",
            func=func.__name__ if hasattr(func, "__name__") else str(func),
        )

        # BYPASS TOTAL: Usar client direto sem rate limiting
        client = None
        client_id_used = "emergency"

        if self._backup_clients and len(self._backup_clients) > 0:
            client = self._backup_clients[0]  # Usar primeiro client disponível
            func = getattr(client, func.__name__)
            client_id_used = "backup_0"
        elif self._primary_client:
            client = self._primary_client  # Fallback para client primário
            client_id_used = "primary"
        else:
            raise RuntimeError("EMERGÊNCIA: Nenhum client disponível para bypass")

        if hasattr(func, "__name__") and client:
            bound_func = getattr(client, func.__name__, None)
            if bound_func:
                func = bound_func

        production(
            f"🚀 BYPASS: Usando client {client_id_used} direto {type(client).__name__}"
        )

        start_time = time.time()

        # BYPASS COMPLETO: Execução direta sem rate limiting ou validações
        try:
            async with asyncio.timeout(Timeouts.EMERGENCY_CLOSE):
                result = await func(*args, **kwargs)

            # Record success
            latency_ms = (time.time() - start_time) * 1000
            self._update_client_stats(client_id_used, latency_ms, True)
            await metrics.record_api_call(
                f"{endpoint_name}_emergency", latency_ms, True
            )

            production(
                f"✅ EMERGÊNCIA EXECUTADA EM {latency_ms:.0f}ms: {endpoint_name}",
                latency_ms=latency_ms,
            )

            return result

        except TimeoutError:
            latency_ms = (time.time() - start_time) * 1000
            error(
                f"⏰ TIMEOUT EMERGENCIAL: {endpoint_name}",
                timeout_ms=1500,
                actual_ms=latency_ms,
            )
            await metrics.record_api_call(
                f"{endpoint_name}_emergency", latency_ms, False
            )
            raise RuntimeError(f"Timeout emergencial para {endpoint_name}") from None

        except BinanceAPIException as e:
            latency_ms = (time.time() - start_time) * 1000
            error(
                f"🚨 API ERROR EMERGENCIAL: {endpoint_name}",
                code=e.code,
                message=e.message,
                latency_ms=latency_ms,
            )
            await metrics.record_api_call(
                f"{endpoint_name}_emergency", latency_ms, False
            )
            raise

        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            error(
                f"🚨 ERROR CRÍTICO EMERGENCIAL: {endpoint_name}",
                error=str(e),
                latency_ms=latency_ms,
            )
            await metrics.record_api_call(
                f"{endpoint_name}_emergency", latency_ms, False
            )
            raise

    def _get_dynamic_timeout(self, endpoint_name: str) -> float:
        """Get dynamic timeout based on endpoint and pool health"""
        base_timeouts = {
            "ping": Timeouts.REQUEST_PING,
            "get_ticker": Timeouts.REQUEST_TICKER,
            "get_orderbook": Timeouts.REQUEST_ORDERBOOK,
            "get_klines": Timeouts.REQUEST_KLINES,
            "create_order": Timeouts.ORDER_CREATE,
            "cancel_order": Timeouts.ORDER_CANCEL,
            "get_account": Timeouts.REQUEST_ACCOUNT,
            "get_all_tickers": Timeouts.REQUEST_ALL_TICKERS,
        }

        base_timeout = base_timeouts.get(endpoint_name, Timeouts.REQUEST_DEFAULT)

        # Adjust timeout based on pool health
        if self._pool_status == ConnectionPoolStatus.DEGRADED:
            return base_timeout * 1.5
        elif self._pool_status == ConnectionPoolStatus.FAILED:
            return base_timeout * 2.0

        return base_timeout

    def _handle_request_failure(
        self, endpoint_name: str, failure_type: str, attempt: int, start_time: float
    ):
        """Handle request failure with proper logging and stats"""
        latency_ms = (time.time() - start_time) * 1000

        warning(
            f"{failure_type.title()} error na tentativa {attempt + 1} para {endpoint_name}",
            latency_ms=latency_ms,
        )

        self._update_client_stats(self._active_client_id, latency_ms, False)

        # Adaptive rate limiting on failures
        if self._adaptive_rate_limit:
            self._current_rate_limit = max(self._current_rate_limit - 50, 200)

    def _handle_rate_limit_error(self, exception: BinanceAPIException, attempt: int):
        """Handle rate limiting errors with adaptive response"""
        warning(
            "Rate limit atingido - ajustando estratégia",
            code=exception.code,
            attempt=attempt,
            current_limit=self._current_rate_limit,
        )

        # Drastically reduce rate limit
        self._current_rate_limit = max(self._current_rate_limit // 2, 100)

        # Set pool to degraded if not already
        if self._pool_status == ConnectionPoolStatus.HEALTHY:
            self._pool_status = ConnectionPoolStatus.DEGRADED

    def _record_critical_failure(self):
        """Record critical failure and update circuit breaker"""
        self.circuit_breaker.record_failure()
        self._failure_count += 1
        self._last_failure_time = time.time()

        # Increase backoff multiplier
        self._backoff_multiplier = min(self._backoff_multiplier * 1.5, 10.0)

        # Update pool status
        if self._failure_count >= 3:
            self._pool_status = ConnectionPoolStatus.FAILED

    def _update_client_stats(self, client_id: str, latency_ms: float, success: bool):
        """Update statistics for a specific client"""
        if client_id not in self._client_stats:
            self._client_stats[client_id] = ConnectionStats()

        stats = self._client_stats[client_id]
        stats.total_requests += 1
        stats.last_used = datetime.now()

        if success:
            stats.successful_requests += 1
        else:
            stats.failed_requests += 1

        # Update rolling average latency
        if stats.total_requests == 1:
            stats.avg_latency_ms = latency_ms
        else:
            stats.avg_latency_ms = (stats.avg_latency_ms * 0.9) + (latency_ms * 0.1)

    async def _cleanup_all_connections(self):
        """Clean up all connections in the pool"""
        cleanup_tasks = []

        # Cleanup primary client
        if self._primary_client:
            cleanup_tasks.append(
                self._cleanup_single_client("primary", self._primary_client)
            )

        # Cleanup backup clients
        for i, backup_client in enumerate(self._backup_clients):
            if backup_client:
                cleanup_tasks.append(
                    self._cleanup_single_client(f"backup_{i}", backup_client)
                )

        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

        # Clear references
        self._primary_client = None
        self._backup_clients.clear()
        self._client_stats.clear()

    async def _cleanup_single_client(self, client_id: str, client: AsyncClient):
        """Clean up a single client connection"""
        try:
            async with asyncio.timeout(Timeouts.CLEANUP_TIMEOUT):
                await client.close_connection()
            debug(f"Cliente {client_id} fechado com sucesso")
        except TimeoutError:
            warning(f"Timeout ao fechar cliente {client_id}")
        except Exception as e:
            warning(f"Erro ao fechar cliente {client_id}", error=str(e))
