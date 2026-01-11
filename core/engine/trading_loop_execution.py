import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

from shared.constants import (
    CLEANUP_INTERVAL_ITERATIONS,
    DEFAULT_MIN_ENTRY_SCORE,
    MIN_ENTRY_SCORE,
    REQUIRED_SIGNAL_FIELDS,
)
from shared.observability.flow_tracker import flow_tracker, track_component
from shared.observability.logger import (
    debug,
    error,
    production,
    signal_diagnostic,
    task_diagnostic,
    trading_diagnostic,
    warning,
)
from shared.observability.metrics import metrics
from shared.types.state import state
from utils.decimal_math import to_decimal
from utils.validation_utils import is_numeric_valid, validate_symbol


class TradingLoopExecutionMixin:
    def _should_continue_running(self) -> bool:
        return self.coordinator.running and not self.coordinator.shutdown_event.is_set()

    async def _validate_global_cooldown(self) -> bool:
        """Validate and handle global cooldown. Returns True if should continue processing."""
        current_time = datetime.now()

        if self.global_cooldown_until > current_time + timedelta(
            hours=self.corruption_threshold_hours
        ):
            warning(
                "CRITICAL: Global cooldown corrompido - resetando",
                bad_value=self.global_cooldown_until,
                reset_to=current_time,
            )
            self.global_cooldown_until = current_time

        if current_time < self.global_cooldown_until:
            remaining = (self.global_cooldown_until - current_time).total_seconds()

            if remaining > self.long_cooldown_threshold:
                production(
                    "⚠️ Global cooldown muito longo - forçando reset",
                    remaining_seconds=remaining,
                    cooldown_until=self.global_cooldown_until,
                )
                self.global_cooldown_until = current_time
            else:
                debug("Global cooldown ativo", remaining_seconds=remaining)
                await asyncio.sleep(min(self.max_cooldown_sleep, remaining))
                return False

        return True

    def _get_validated_trading_pairs(self) -> list[str] | None:
        """Get and validate trading pairs from config. Returns None if invalid."""
        trading_pairs = self.config.get("trading_pairs", [])
        if not isinstance(trading_pairs, list) or not trading_pairs:
            error("CRÍTICO: trading_pairs inválido ou vazio na configuração")
            production("❌ ERRO: Configuração de trading_pairs inválida")
            return None
        return trading_pairs

    async def _should_skip_symbol(self, symbol: str) -> tuple[bool, str]:
        """Check if symbol should be skipped. Returns (skip, reason)."""
        if not validate_symbol(symbol):
            warning("Símbolo inválido ignorado", symbol=symbol)
            return True, "invalid_format"

        if await self._is_symbol_in_cooldown(symbol):
            async with self._cooldown_lock:
                if symbol in self.symbol_cooldowns:
                    remaining = (
                        self.symbol_cooldowns[symbol] - datetime.now()
                    ).total_seconds()
                    debug(
                        "Símbolo em cooldown local",
                        symbol=symbol,
                        remaining_seconds=remaining,
                    )
                else:
                    debug("Símbolo em cooldown global (fallback)", symbol=symbol)
            return True, "cooldown"

        return False, ""

    async def _analyze_symbol_signal(self, symbol: str) -> dict | None:
        """Analyze symbol and return signal if valid, None otherwise."""
        if not self.signal_analyzer or not hasattr(
            self.signal_analyzer, "analyze_symbol"
        ):
            error("CRÍTICO: SignalAnalyzer não disponível", symbol=symbol)
            return None

        signal_diagnostic("Executando analyze_symbol", symbol=symbol)
        signal = await self.signal_analyzer.analyze_symbol(
            symbol, self.coordinator.data_manager
        )
        signal_diagnostic("Análise concluída", symbol=symbol, signal_type=type(signal))

        return signal

    def _validate_signal(self, symbol: str, signal: dict | None) -> dict | None:
        """Validate signal data integrity. Returns validated signal or None."""
        if not signal or not isinstance(signal, dict):
            debug("Sinal inválido ou vazio", symbol=symbol, signal_type=type(signal))
            self._symbols_no_change.add(symbol)
            return None

        missing_fields = [f for f in REQUIRED_SIGNAL_FIELDS if f not in signal]
        if missing_fields:
            warning(
                "Sinal inválido - campos ausentes",
                symbol=symbol,
                missing=missing_fields,
            )
            return None

        entry_score = signal.get("entry_score", 0)
        if not is_numeric_valid(entry_score):
            warning("Entry score inválido", symbol=symbol, score=entry_score)
            return None

        current_price = signal.get("current_price", 0)
        if not is_numeric_valid(current_price) or current_price <= 0:
            warning(
                "Current price inválido no sinal", symbol=symbol, price=current_price
            )
            return None

        return signal

    def _get_min_entry_score(self) -> int | float | Decimal:
        """Get minimum entry score from config with validation."""
        min_score = self.config.get("strategy", {}).get(
            "min_entry_score", MIN_ENTRY_SCORE
        )
        if not isinstance(min_score, (int, float, Decimal)) or min_score < 0:
            warning("Min entry score inválido, usando default", current=min_score)
            return DEFAULT_MIN_ENTRY_SCORE
        return min_score

    async def _should_execute_entry(
        self, symbol: str, signal: dict, min_score: int | float | Decimal
    ) -> bool:
        """Decide if entry should be executed. Returns True if should execute."""
        entry_score = signal.get("entry_score", 0)

        production(
            "✅ SINAL VÁLIDO",
            symbol=symbol,
            score=round(entry_score, 2),
            min_score=min_score,
            price=signal.get("current_price", 0),
            spread=round(signal.get("spread_pct", 0) * 100, 3),
        )

        if entry_score < min_score:
            production(
                "❌ SINAL REJEITADO - Score insuficiente",
                symbol=symbol,
                score=round(entry_score, 2),
                min_required=min_score,
            )
            return False

        try:
            current_positions = await state.get_all_positions()
            if not isinstance(current_positions, dict):
                warning("Posições retornadas inválidas", type=type(current_positions))
                current_positions = {}
        except Exception as pe:
            warning("Erro ao obter posições atuais", error=str(pe))
            current_positions = {}

        if symbol in current_positions:
            production(
                "❌ SINAL REJEITADO - Posição já existe",
                symbol=symbol,
                score=round(entry_score, 2),
            )
            return False

        production("Sinal qualificado para entrada", symbol=symbol, score=entry_score)
        return True

    async def _process_symbol(self, symbol: str) -> dict:
        """Process a single trading symbol. Returns stats dict."""
        result = {"analyzed": False, "skipped": False, "entered": False, "error": False}

        symbol = symbol.strip().upper()
        debug("Processando símbolo", symbol=symbol)

        skip, reason = await self._should_skip_symbol(symbol)
        if skip:
            result["skipped"] = True
            return result

        try:
            signal_diagnostic("Aguardando semáforo para análise", symbol=symbol)
            async with self.analysis_semaphore:
                signal_diagnostic(
                    "Semáforo adquirido - iniciando análise", symbol=symbol
                )

                signal = await self._analyze_symbol_signal(symbol)
                validated_signal = self._validate_signal(symbol, signal)

                if validated_signal:
                    result["analyzed"] = True
                    min_score = self._get_min_entry_score()

                    if await self._should_execute_entry(
                        symbol, validated_signal, min_score
                    ):
                        await self._execute_entry(symbol, validated_signal)
                        result["entered"] = True

                production("🔓 Semáforo liberado", symbol=symbol)

        except Exception as analysis_error:
            error(
                "CRÍTICO: Erro na análise do símbolo",
                symbol=symbol,
                error=str(analysis_error),
            )
            result["error"] = True

        return result

    async def _process_all_symbols(self, trading_pairs: list[str]) -> dict:
        """Process all trading symbols. Returns aggregated stats."""
        stats = {
            "analyzed": 0,
            "skipped": 0,
            "entered": 0,
            "errors": 0,
            "total": len(trading_pairs),
        }

        for symbol in trading_pairs:
            if not self._should_continue_running():
                break

            result = await self._process_symbol(symbol)

            if result["analyzed"]:
                stats["analyzed"] += 1
            if result["skipped"]:
                stats["skipped"] += 1
            if result["entered"]:
                stats["entered"] += 1
            if result["error"]:
                stats["errors"] += 1

            await asyncio.sleep(self.inter_symbol_delay)

        return stats

    def _log_no_change_symbols(self) -> None:
        """Log symbols without signal changes periodically."""
        now = datetime.now().timestamp()
        if (
            self._symbols_no_change
            and now - self._last_no_change_log > self.activity_log_interval
        ):
            debug(
                "Símbolos sem sinal",
                count=len(self._symbols_no_change),
                symbols=sorted(self._symbols_no_change),
            )
            self._symbols_no_change.clear()
            self._last_no_change_log = now

    async def _periodic_cleanup(self, iteration: int) -> None:
        """Perform periodic cleanup tasks."""
        if iteration % CLEANUP_INTERVAL_ITERATIONS == 0:
            await self._cleanup_closed_position_locks()

    async def _handle_iteration_error(self, exc: Exception) -> None:
        """Handle errors in iteration with exponential backoff."""
        error_key = str(type(exc).__name__)
        error_count = self._repeated_errors.get(error_key, 0) + 1
        self._repeated_errors[error_key] = error_count

        if error_count == 1 or error_count % 10 == 0:
            error(
                "Erro no loop de trading",
                error_type=error_key,
                occurrences=error_count,
                error=str(exc),
            )

        await asyncio.sleep(self.error_recovery_delay)

    async def run(self):
        """Main trading loop - orchestrates iterations."""
        task_diagnostic("TradingLoop.run() iniciado")
        trading_diagnostic(
            "Trading loop entrando em execução principal",
            coordinator_running=self.coordinator.running,
        )
        production("Trading loop iniciado")

        iteration = 0
        last_activity_log = 0.0

        while self._should_continue_running():
            try:
                iteration += 1
                current_time = datetime.now()

                if (
                    current_time.timestamp() - last_activity_log
                    > self.activity_log_interval
                ):
                    task_diagnostic(
                        "TradingLoop ativo",
                        iteration=iteration,
                        cooldown_until=self.global_cooldown_until.strftime("%H:%M:%S"),
                        current_time=current_time.strftime("%H:%M:%S"),
                    )
                    last_activity_log = current_time.timestamp()

                debug("Trading loop iteração", count=iteration)

                if not await self._validate_global_cooldown():
                    continue

                trading_pairs = self._get_validated_trading_pairs()
                if not trading_pairs:
                    await asyncio.sleep(self.check_interval)
                    continue

                trading_diagnostic(
                    "Iniciando processamento de símbolos", iteration=iteration
                )
                production(
                    "📊 Processando símbolos",
                    count=len(trading_pairs),
                    symbols=trading_pairs[:3],
                )

                stats = await self._process_all_symbols(trading_pairs)

                self._log_no_change_symbols()

                production(
                    "📈 Ciclo completo",
                    iteration=iteration,
                    analyzed=stats["analyzed"],
                    skipped=stats["skipped"],
                    entered=stats["entered"],
                    errors=stats["errors"],
                    total_symbols=stats["total"],
                )

                await self._periodic_cleanup(iteration)

                production("⏸ Aguardando próximo ciclo", interval=self.check_interval)
                flow_tracker.force_component_update("trading_loop")
                await asyncio.sleep(self.check_interval)

            except Exception as e:
                await self._handle_iteration_error(e)

    async def _can_make_global_entry(self) -> tuple[bool, str]:
        async with self._entry_lock:
            now = datetime.now()

            # Clean old entries (older than 60 seconds)
            self.global_entry_history = [
                entry_time
                for entry_time in self.global_entry_history
                if (now - entry_time).total_seconds() < 60
            ]

            # Check minimum interval since last entry
            if self.global_entry_history:
                time_since_last = (now - max(self.global_entry_history)).total_seconds()
                if time_since_last < self.min_global_entry_interval:
                    return (
                        False,
                        f"Too soon since last entry ({time_since_last:.1f}s < {self.min_global_entry_interval}s)",
                    )

            # Check entries per minute limit
            if len(self.global_entry_history) >= self.max_entries_per_minute:
                return (
                    False,
                    f"Max entries per minute exceeded ({len(self.global_entry_history)}/{self.max_entries_per_minute})",
                )

            return True, "Entry allowed"

    async def _record_global_entry(self):
        async with self._entry_lock:
            self.global_entry_history.append(datetime.now())

    def _validate_entry_inputs(self, symbol: str, signal: dict) -> tuple[bool, str]:
        if not validate_symbol(symbol):
            return False, "invalid_symbol"
        if not signal or not isinstance(signal, dict):
            return False, "invalid_signal"
        return True, ""

    async def _check_entry_rate_limits(
        self, symbol: str, signal: dict
    ) -> tuple[bool, str]:
        can_enter, reason = await self._can_make_global_entry()
        if not can_enter:
            production(
                "Signal rejected - rate limit",
                symbol=symbol,
                reason=reason,
                score=signal.get("entry_score", 0),
            )
            return False, reason
        return True, ""

    async def _acquire_entry_and_check_position(
        self, symbol: str, signal: dict
    ) -> tuple[bool, str]:
        async with self.global_entry_semaphore:
            debug("Global entry semaphore adquirido", symbol=symbol)

            can_enter_final, reason_final = await self._can_make_global_entry()
            if not can_enter_final:
                debug(
                    "Entry bloqueada após semáforo", symbol=symbol, reason=reason_final
                )
                return False, reason_final

            current_positions = await state.get_all_positions()
            if symbol in current_positions:
                production(
                    "Signal rejected - position already exists",
                    symbol=symbol,
                    score=signal.get("entry_score", 0),
                )
                return False, "position_exists"

            debug("Executando entrada", symbol=symbol)
            return True, ""

    async def _check_risk_gates(self, symbol: str) -> tuple[bool, str]:
        position_allowed, reason = await self.risk_manager.check_position_allowed()
        if not position_allowed:
            production(
                "❌ ENTRADA BLOQUEADA - Risk Manager", symbol=symbol, reason=reason
            )
            return False, reason

        if (
            hasattr(self.coordinator, "correlation_validator")
            and self.coordinator.correlation_validator
        ):
            can_open_corr, corr_reason = (
                await self.coordinator.correlation_validator.can_open_position(symbol)
            )
            if not can_open_corr:
                production(
                    "❌ ENTRADA BLOQUEADA - Correlação",
                    symbol=symbol,
                    reason=corr_reason,
                )
                return False, corr_reason

        if (
            hasattr(self.coordinator, "unified_monitor")
            and self.coordinator.unified_monitor
        ):
            can_trade, trade_reason = self.coordinator.unified_monitor.can_trade_pair(
                symbol
            )
            if not can_trade:
                production(
                    "❌ ENTRADA BLOQUEADA - Limit por par",
                    symbol=symbol,
                    reason=trade_reason,
                )
                return False, trade_reason

            can_open_pos, pos_reason = (
                self.coordinator.unified_monitor.can_open_position()
            )
            if not can_open_pos:
                production(
                    "❌ ENTRADA BLOQUEADA - Perdas simultâneas",
                    symbol=symbol,
                    reason=pos_reason,
                )
                return False, pos_reason

        return True, ""

    async def _validate_account_balance(self, symbol: str) -> Decimal | None:
        try:
            account = await state.get_account()
            if not account or not isinstance(account, dict):
                error("CRÍTICO: Account data inválida", symbol=symbol, account=account)
                return None

            usdt_balance = account.get("balance", 0)

            if not isinstance(usdt_balance, (int, float, Decimal)):
                error(
                    "CRÍTICO: USDT balance não é numérico",
                    symbol=symbol,
                    balance=usdt_balance,
                    type=type(usdt_balance),
                )
                return None

            if not is_numeric_valid(usdt_balance):
                error(
                    "CRÍTICO: USDT balance é NaN ou infinito",
                    symbol=symbol,
                    balance=usdt_balance,
                )
                return None

            if usdt_balance <= 0:
                warning(
                    "Saldo insuficiente para entrada",
                    symbol=symbol,
                    balance=usdt_balance,
                )
                return None

            debug("Saldo USDT válido disponível", symbol=symbol, balance=usdt_balance)
            return (
                to_decimal(usdt_balance)
                if not isinstance(usdt_balance, Decimal)
                else usdt_balance
            )

        except Exception as balance_error:
            error(
                "CRÍTICO: Erro ao validar saldo",
                symbol=symbol,
                error=str(balance_error),
            )
            return None

    async def _record_successful_entry(
        self, symbol: str, signal: dict, usdt_balance: Decimal
    ) -> None:
        await self._record_global_entry()

        try:
            position = (
                self.position_manager.positions.get(symbol)
                if self.position_manager.positions
                else None
            )
            if position and isinstance(position, dict):
                await state.set_position(symbol, position)
                production(
                    "💰 POSIÇÃO ABERTA",
                    symbol=symbol,
                    entry_price=position.get("entry_price", 0),
                    quantity=position.get("quantity", 0),
                    stop_loss=position.get("stop_loss", 0),
                    take_profit=position.get("take_profit", 0),
                    score=round(signal.get("entry_score", 0), 2),
                    spread_pct=round(signal.get("spread_pct", 0), 3),
                    market_condition=signal.get("market_condition", "unknown"),
                )
            else:
                warning("Posição não encontrada após abertura", symbol=symbol)
        except Exception as pos_error:
            warning(
                "Erro ao registrar posição no state",
                symbol=symbol,
                error=str(pos_error),
            )

        async with self._cooldown_lock:
            self.symbol_cooldowns[symbol] = datetime.now() + timedelta(
                seconds=self.symbol_cooldown_seconds
            )
            cooldown_until = self.symbol_cooldowns[symbol].strftime("%H:%M:%S")

        production("Posição aberta com sucesso", symbol=symbol, balance=usdt_balance)
        debug("Cooldown aplicado", symbol=symbol, until=cooldown_until)

        try:
            await metrics.record_signal(symbol, signal.get("entry_score", 0), True)
        except Exception as metric_error:
            warning(
                "Erro ao registrar métrica de sinal",
                symbol=symbol,
                error=str(metric_error),
            )

    async def _record_failed_entry(
        self, symbol: str, signal: dict, usdt_balance: Decimal
    ) -> None:
        error("Falha ao abrir posição", symbol=symbol, balance=usdt_balance)
        try:
            await metrics.record_signal(symbol, signal.get("entry_score", 0), False)
        except Exception as metric_error:
            warning(
                "Erro ao registrar métrica de sinal falho",
                symbol=symbol,
                error=str(metric_error),
            )

    @track_component("trading_loop", slow_threshold=10)
    async def _execute_entry(self, symbol: str, signal: dict) -> bool | None:
        valid, reason = self._validate_entry_inputs(symbol, signal)
        if not valid:
            error(
                f"CRÍTICO: {reason} para execução",
                symbol=symbol,
                signal=signal if reason == "invalid_signal" else None,
            )
            return False

        rate_ok, _ = await self._check_entry_rate_limits(symbol, signal)
        if not rate_ok:
            return False

        usdt_balance = Decimal(0)

        try:
            entry_lock = await self._get_entry_lock(symbol)

            async with entry_lock:
                position_ok, _ = await self._acquire_entry_and_check_position(
                    symbol, signal
                )
                if not position_ok:
                    return False

                risk_ok, _ = await self._check_risk_gates(symbol)
                if not risk_ok:
                    return False

                balance = await self._validate_account_balance(symbol)
                if balance is None:
                    return False
                usdt_balance = balance

                success = await self.position_manager.open_position(
                    symbol=symbol, signal=signal, usdt_balance=usdt_balance
                )

                if success:
                    await self._record_successful_entry(symbol, signal, usdt_balance)
                    return True
                else:
                    await self._record_failed_entry(symbol, signal, usdt_balance)
                    return False

        except Exception as e:
            error(
                "CRÍTICO: Erro ao executar entrada",
                symbol=symbol,
                cooldown=self.symbol_cooldown_seconds,
                balance=usdt_balance,
                error=str(e),
            )
            return False
