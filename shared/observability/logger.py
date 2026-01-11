import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

from .formatters import (
    DebugFilter,
    DebugLevel,
    MessageAggregator,
)


class MessageRoutingProcessor:
    OPERATIONS_KEYWORDS = frozenset(
        [
            "position",
            "trade",
            "pnl",
            "order",
            "signal",
            "market",
            "risk",
            "entry",
            "exit",
            "profit",
            "loss",
            "stop_loss",
            "take_profit",
            "buy",
            "sell",
            "quantity",
            "price",
            "spread",
            "volume",
            "posição",
            "ordem",
            "monitor",
            "monitorando",
            "score",
            "sinal",
            "balance",
            "saldo",
        ]
    )

    OPERATIONS_MODULES = frozenset(
        [
            "position_manager",
            "trading_coordinator",
            "signal_analyzer",
            "market_updater",
            "risk_manager",
            "trading_loop",
            "executor",
        ]
    )

    def __call__(self, logger, method_name, event_dict):
        msg = event_dict.get("event", "").lower()
        module = event_dict.get("module", "")

        # Decide routing baseado em keywords/modules (mesma lógica do logger antigo)
        if (
            any(kw in msg for kw in self.OPERATIONS_KEYWORDS)
            or module in self.OPERATIONS_MODULES
            or (event_dict.get("diagnostic") and "trading" in module)
        ):
            route = "operations"
        else:
            route = "maintenance"

        event_dict["_route"] = route

        # CRITICAL: Also set as _record_custom_attrs for stdlib logging
        # This ensures the route is available in LogRecord
        event_dict.setdefault("_record", {})["route"] = route

        return event_dict


class MessageAggregationProcessor:
    def __init__(self) -> None:
        self._aggregator = MessageAggregator()

    def __call__(self, logger, method_name, event_dict):
        msg = event_dict.get("event", "")
        level = event_dict.get("level", "info").upper()

        # Extrai context para aggregation
        context = {
            k: v
            for k, v in event_dict.items()
            if k in {"symbol", "trade_id", "order_id"}
        }

        should_log, repeat_count = self._aggregator.should_log(msg, level, context)

        if not should_log:
            raise structlog.DropEvent  # Suprime log duplicado

        if repeat_count > 1:
            event_dict["event"] = f"[{repeat_count}x] {msg}"

        return event_dict


class DebugFilteringProcessor:
    """Custom processor: Filtra debug logs baseado em nível e módulos"""

    def __init__(self) -> None:
        self._filter = DebugFilter()

    def set_debug_level(self, level: DebugLevel):
        self._filter.set_debug_level(level)

    def add_module_filter(self, module: str):
        self._filter.add_module_filter(module)

    def remove_module_filter(self, module: str):
        self._filter.remove_module_filter(module)

    def __call__(self, logger, method_name, event_dict):
        """Filtra debug logs usando DebugFilter existente"""
        if event_dict.get("level") == "debug":
            module = event_dict.get("module", "unknown")
            msg = event_dict.get("event", "")
            diagnostic = event_dict.get("diagnostic", False)

            if not self._filter.should_log_debug(module, msg, diagnostic):
                raise structlog.DropEvent

        return event_dict


class RouteFilter:
    """Filter para stdlib logging handlers - roteia baseado em _route"""

    def __init__(self, route: str) -> None:
        self.route = route

    def filter(self, record):
        """Aceita apenas logs com _route correto"""
        import json

        # O ProcessorFormatter já processou o JSON na mensagem
        # Precisamos parsear a mensagem para extrair o _route
        try:
            if hasattr(record, "msg"):
                msg = record.msg
                # Se msg já é string JSON, parse
                if isinstance(msg, str) and msg.startswith("{"):
                    try:
                        data = json.loads(msg)
                        route = data.get("_route", "maintenance")
                        return route == self.route
                    except json.JSONDecodeError:
                        pass

                # Se msg é dict (antes do JSON rendering)
                if isinstance(msg, dict):
                    route = msg.get("_route", "maintenance")
                    return route == self.route

        except Exception:
            pass

        # Fallback: se não conseguiu determinar route, vai para maintenance
        return self.route == "maintenance"


class TradingLogger:
    """
    Wrapper structlog mantendo API compatível com código existente.

    API pública idêntica ao shared/logger.py:
    - production(msg, **kwargs)
    - debug(msg, **kwargs)
    - error(msg, **kwargs)
    - warning(msg, **kwargs)
    - trading_diagnostic(msg, **kwargs)
    - position_opened(...)
    - position_closed(...)
    - etc.
    """

    def __init__(self) -> None:
        self._setup_complete = False
        self._is_debug = False
        self._debug_level = DebugLevel.BASIC
        self._dashboard_mode = False

        # Custom processors
        self._routing_processor = MessageRoutingProcessor()
        self._aggregation_processor = MessageAggregationProcessor()
        self._debug_processor = DebugFilteringProcessor()

        self._setup()

    def _setup(self) -> None:
        """Configura structlog com processors customizados e file handlers"""
        if self._setup_complete:
            return

        import os

        # Detecta debug level do ambiente
        log_level = os.environ.get("LOG_LEVEL", "").upper()
        self._is_debug = log_level in ["DEBUG", "DEBUG_DETAILED", "DEBUG_VERBOSE"]

        if log_level == "DEBUG_DETAILED":
            self._debug_level = DebugLevel.DETAILED
        elif log_level == "DEBUG_VERBOSE":
            self._debug_level = DebugLevel.VERBOSE
        elif log_level == "DEBUG":
            self._debug_level = DebugLevel.BASIC

        self._debug_processor.set_debug_level(self._debug_level)

        # Configure DEBUG_MODULES filter
        debug_modules = os.environ.get("DEBUG_MODULES", "")
        if debug_modules:
            for module in debug_modules.split(","):
                self._debug_processor.add_module_filter(module.strip())

        # Setup stdlib logging handlers (para rotear logs para arquivos)
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True, parents=True)

        # Formatter que renderiza JSON
        formatter = structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
        )

        # Handler 1: Operations log
        operations_handler = RotatingFileHandler(
            str(log_dir / "trading_operations.log"),
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=3,
            encoding="utf-8",
        )
        operations_handler.setLevel(logging.DEBUG if self._is_debug else logging.INFO)
        operations_handler.setFormatter(formatter)
        operations_handler.addFilter(RouteFilter("operations"))

        # Handler 2: Maintenance log
        maintenance_handler = RotatingFileHandler(
            str(log_dir / "system_maintenance.log"),
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=3,
            encoding="utf-8",
        )
        maintenance_handler.setLevel(logging.DEBUG if self._is_debug else logging.INFO)
        maintenance_handler.setFormatter(formatter)
        maintenance_handler.addFilter(RouteFilter("maintenance"))

        # Handler 3: Console (DESABILITADO - logs apenas em arquivos)
        # Para habilitar console, descomente as linhas abaixo
        console_handler = None
        # if sys.stdout.isatty() and not self._dashboard_mode:
        #     console_handler = logging.StreamHandler(sys.stdout)
        #     console_handler.setLevel(logging.DEBUG if self._is_debug else logging.INFO)
        #     console_handler.setFormatter(formatter)

        # Cria stdlib logger e adiciona handlers
        stdlib_logger = logging.getLogger("trading_bot")
        stdlib_logger.setLevel(logging.DEBUG if self._is_debug else logging.INFO)
        stdlib_logger.handlers.clear()

        stdlib_logger.addHandler(operations_handler)
        stdlib_logger.addHandler(maintenance_handler)
        if console_handler:
            stdlib_logger.addHandler(console_handler)

        # Configura structlog para usar stdlib logging factory
        structlog.configure(
            processors=[
                # 1. Merge contextvars (útil para trace_id futuros)
                structlog.contextvars.merge_contextvars,
                # 2. Add log level
                structlog.processors.add_log_level,
                # 3. Add timestamp
                structlog.processors.TimeStamper(fmt="iso", utc=False),
                # 4. CUSTOM: Message routing (operations vs maintenance)
                self._routing_processor,
                # 5. CUSTOM: Message aggregation (anti-spam)
                self._aggregation_processor,
                # 6. CUSTOM: Debug filtering
                self._debug_processor,
                # 7. Stack info renderer (para exceptions)
                structlog.processors.StackInfoRenderer(),
                # 8. Exception formatter
                structlog.processors.format_exc_info,
                # 9. Prepare for stdlib logging
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

        # Get logger instance (agora via stdlib factory)
        self._logger = structlog.get_logger("trading_bot")

        self._setup_complete = True

    def _log(self, level: str, msg: str, **kwargs) -> None:
        """Internal log method - routed to structlog"""
        try:
            if not self._setup_complete:
                self._setup()

            # Extrai module do kwargs (compatibilidade)
            module = kwargs.pop("module", "unknown")
            diagnostic = kwargs.pop("diagnostic", False)
            exc_info = kwargs.pop("exc_info", False)

            # Prepara event_dict para structlog
            event_dict = {
                "event": msg,
                "module": module,
                "diagnostic": diagnostic,
                **kwargs,
            }

            # Log usando structlog (agora vai para stdlib logger)
            if level == "ERROR":
                self._logger.error(**event_dict, exc_info=exc_info)
            elif level == "WARNING":
                self._logger.warning(**event_dict)
            elif level == "INFO":
                self._logger.info(**event_dict)
            elif level == "DEBUG":
                if self._is_debug:
                    self._logger.debug(**event_dict)
            else:
                self._logger.info(**event_dict)

        except structlog.DropEvent:
            # Mensagem foi suprimida por processor (aggregation ou debug filter)
            pass
        except Exception as e:
            # Emergency fallback
            from datetime import datetime

            timestamp = datetime.now().astimezone().isoformat()
            emergency_msg = f"[{timestamp}] [LOGGING_ERROR] Original: [{level}] {msg} | LogError: {e}"
            if sys.stdout.isatty():
                print(emergency_msg, file=sys.stderr)

    # ============================================================================
    # API PÚBLICA - COMPATÍVEL COM shared/logger.py
    # ============================================================================

    def production(self, msg: str, **kwargs) -> None:
        """Log production message (INFO level)"""
        self._log("INFO", msg, **kwargs)

    def debug(self, msg: str, **kwargs) -> None:
        """Log debug message (DEBUG level)"""
        self._log("DEBUG", msg, **kwargs)

    def error(self, msg: str, **kwargs) -> None:
        """Log error message (ERROR level)"""
        self._log("ERROR", msg, **kwargs)

    def warning(self, msg: str, **kwargs) -> None:
        """Log warning message (WARNING level)"""
        self._log("WARNING", msg, **kwargs)

    def trading_diagnostic(self, msg: str, **kwargs) -> None:
        """Trading diagnostic log (DEBUG level, always logged)"""
        kwargs["diagnostic"] = True
        kwargs["module"] = "trading"
        self._log("DEBUG", f"[TRADING_DIAG] {msg}", **kwargs)

    def task_diagnostic(self, msg: str, **kwargs) -> None:
        """Task diagnostic log"""
        kwargs["diagnostic"] = True
        kwargs["module"] = "task"
        self._log("DEBUG", f"[TASK_DIAG] {msg}", **kwargs)

    def signal_diagnostic(self, msg: str, **kwargs) -> None:
        """Signal diagnostic log"""
        kwargs["diagnostic"] = True
        kwargs["module"] = "signal"
        self._log("DEBUG", f"[SIGNAL_DIAG] {msg}", **kwargs)

    def execution_diagnostic(self, msg: str, **kwargs) -> None:
        """Execution diagnostic log"""
        kwargs["diagnostic"] = True
        kwargs["module"] = "execution"
        self._log("DEBUG", f"[EXEC_DIAG] {msg}", **kwargs)

    def critical_diagnostic(self, msg: str, **kwargs) -> None:
        """Critical diagnostic log (ERROR level)"""
        kwargs["diagnostic"] = True
        kwargs["critical"] = True
        kwargs["module"] = "critical"
        self._log("ERROR", f"[CRITICAL_DIAG] {msg}", **kwargs)

    def position_opened(
        self,
        symbol: str,
        avg_price: float,
        quantity: float,
        position_size_usd: float,
        stop_loss: float,
        take_profit: float,
        has_oco: bool,
    ) -> None:
        """Log position opened (compatibilidade com API antiga)"""
        self.production(
            "POSIÇÃO ABERTA",
            symbol=symbol,
            price=avg_price,
            quantity=quantity,
            size=position_size_usd,
        )
        self.debug(
            "Detalhes da posição",
            stop=stop_loss,
            take_profit=take_profit,
            has_oco=has_oco,
        )

    def position_closed(
        self,
        symbol: str,
        reason: str,
        pnl_usd: float,
        pnl_pct: float,
        entry_price: float,
        sell_price: float,
        duration: float,
    ) -> None:
        """Log position closed (compatibilidade com API antiga)"""
        emoji = "✅" if pnl_usd > 0 else "❌"
        self.production(
            f"{emoji} POSIÇÃO FECHADA",
            symbol=symbol,
            reason=reason,
            duration_min=duration,
        )
        self.production(
            "Resultado",
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            entry=entry_price,
            exit=sell_price,
        )

    def get_logger_stats(self) -> dict:
        """Retorna estatísticas do logger (compatibilidade)"""
        stats = {
            "setup_complete": self._setup_complete,
            "debug_mode": self._is_debug,
            "debug_level": self._debug_level.value if self._debug_level else None,
            "has_logger": self._logger is not None,
            "structlog_version": structlog.__version__,
        }

        # Aggregator stats
        if hasattr(self._aggregation_processor, "_aggregator"):
            stats["aggregator"] = (
                self._aggregation_processor._aggregator.get_cache_stats()
            )

        return stats

    def set_debug_level(self, level: DebugLevel) -> None:
        """Define debug level (compatibilidade)"""
        self._debug_level = level
        self._debug_processor.set_debug_level(level)

    def add_debug_module(self, module: str) -> None:
        """Adiciona módulo ao filtro de debug (compatibilidade)"""
        self._debug_processor.add_module_filter(module)

    def remove_debug_module(self, module: str) -> None:
        """Remove módulo do filtro de debug (compatibilidade)"""
        self._debug_processor.remove_module_filter(module)

    def set_dashboard_mode(self, active: bool) -> None:
        """Define dashboard mode (compatibilidade)"""
        self._dashboard_mode = active


# ============================================================================
# SINGLETON EXPORTS - API PÚBLICA IDÊNTICA
# ============================================================================

_instance = TradingLogger()

production = _instance.production
debug = _instance.debug
error = _instance.error
warning = _instance.warning
trading_diagnostic = _instance.trading_diagnostic
task_diagnostic = _instance.task_diagnostic
signal_diagnostic = _instance.signal_diagnostic
execution_diagnostic = _instance.execution_diagnostic
critical_diagnostic = _instance.critical_diagnostic
position_opened = _instance.position_opened
position_closed = _instance.position_closed
set_dashboard_mode = _instance.set_dashboard_mode
