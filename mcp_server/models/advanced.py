from __future__ import annotations

from datetime import UTC
from typing import Any

from pydantic import BaseModel


class WebSocketConnection(BaseModel):
    timestamp: str
    event: str
    status: str


class WebSocketDiagnostics(BaseModel):
    health: str
    total_connections: int
    total_reconnects: int
    total_disconnects: int
    data_gaps_detected: int
    last_connection: str | None
    last_disconnect: str | None
    recent_events: list[WebSocketConnection]
    analysis_hours: int

    @staticmethod
    def _filter_ws_events(log_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Filter log entries to WebSocket related events."""
        keywords = {"websocket", "user_stream", "connection"}
        return [
            e
            for e in log_entries
            if any(kw in e.get("event", "").lower() for kw in keywords)
        ]

    @staticmethod
    def _count_event_type(events: list[dict[str, Any]], keyword: str) -> int:
        """Count events containing a specific keyword."""
        return sum(1 for e in events if keyword in e.get("event", "").lower())

    @staticmethod
    def _get_last_event_timestamp(
        events: list[dict[str, Any]], keyword: str
    ) -> str | None:
        """Get timestamp of last event matching keyword."""
        matching = [e for e in events if keyword in e.get("event", "").lower()]
        return matching[-1].get("timestamp") if matching else None

    @staticmethod
    def _determine_health(
        reconnects: int, data_gaps: int, disconnects: int, connections: int
    ) -> str:
        """Determine WebSocket health status."""
        if disconnects > 0 and not connections:
            return "disconnected"
        if data_gaps > 0:
            return "degraded"
        if reconnects > 5:
            return "unstable"
        return "healthy"

    @classmethod
    def create(
        cls, log_entries: list[dict[str, Any]], hours: int = 24
    ) -> WebSocketDiagnostics:
        from datetime import datetime

        ws_events = cls._filter_ws_events(log_entries)

        connections = cls._count_event_type(ws_events, "connect")
        reconnects = cls._count_event_type(ws_events, "reconnect")
        disconnects = cls._count_event_type(ws_events, "disconnect")
        data_gaps = cls._count_event_type(ws_events, "gap")

        health = cls._determine_health(reconnects, data_gaps, disconnects, connections)

        recent_events = [
            WebSocketConnection(
                timestamp=e.get("timestamp", datetime.now(UTC).isoformat()),
                event=e.get("event", "unknown"),
                status=e.get("level", "info"),
            )
            for e in ws_events[-10:]
        ]

        return cls(
            health=health,
            total_connections=connections,
            total_reconnects=reconnects,
            total_disconnects=disconnects,
            data_gaps_detected=data_gaps,
            last_connection=cls._get_last_event_timestamp(ws_events, "connect"),
            last_disconnect=cls._get_last_event_timestamp(ws_events, "disconnect"),
            recent_events=recent_events,
            analysis_hours=hours,
        )


class OrderLifecycleEvent(BaseModel):
    timestamp: str
    event: str
    status: str


class OrderLookupResult(BaseModel):
    found: bool
    client_order_id: str
    in_position_tracker: bool
    in_database: bool
    order_status: str | None
    symbol: str | None
    side: str | None
    quantity: str | None
    price: str | None
    lifecycle_events: list[OrderLifecycleEvent]
    notes: list[str]

    @classmethod
    def create(
        cls,
        client_order_id: str,
        db_trades: list[dict[str, Any]],
        log_entries: list[dict[str, Any]],
    ) -> OrderLookupResult:
        from datetime import datetime

        db_match = next(
            (t for t in db_trades if t.get("client_order_id") == client_order_id), None
        )

        log_events = [
            e
            for e in log_entries
            if client_order_id in str(e.get("event", ""))
            or client_order_id in str(e.get("details", {}))
        ]

        found = db_match is not None or len(log_events) > 0
        in_database = db_match is not None
        in_position_tracker = False

        order_status = db_match.get("status") if db_match else None
        symbol = db_match.get("symbol") if db_match else None
        side = db_match.get("side") if db_match else None
        quantity = str(db_match.get("quantity")) if db_match else None
        price = str(db_match.get("entry_price")) if db_match else None

        lifecycle_events = [
            OrderLifecycleEvent(
                timestamp=e.get("timestamp", datetime.now(UTC).isoformat()),
                event=e.get("event", "unknown"),
                status=e.get("level", "info"),
            )
            for e in log_events
        ]

        notes = []
        if not found:
            notes.append(
                "Order not found in database or logs. May be very old or invalid client_order_id"
            )
        if in_database and not lifecycle_events:
            notes.append(
                "Order in database but no lifecycle events in logs (beyond retention)"
            )

        return cls(
            found=found,
            client_order_id=client_order_id,
            in_position_tracker=in_position_tracker,
            in_database=in_database,
            order_status=order_status,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            lifecycle_events=lifecycle_events,
            notes=notes,
        )


class PositionSummary(BaseModel):
    entry_time: str
    exit_time: str | None
    side: str
    entry_price: str
    exit_price: str | None
    quantity: str
    realized_pnl: str
    status: str


class PositionHistory(BaseModel):
    symbol: str
    total_positions: int
    winning_positions: int
    losing_positions: int
    total_pnl: str
    avg_pnl: str
    best_trade_pnl: str
    worst_trade_pnl: str
    positions: list[PositionSummary]
    days_analyzed: int

    @classmethod
    def create(
        cls, symbol: str, trades: list[dict[str, Any]], days: int = 30
    ) -> PositionHistory:
        from datetime import datetime, timedelta
        from decimal import Decimal

        cutoff_time = datetime.now(UTC) - timedelta(days=days)

        symbol_trades = []
        for trade in trades:
            if trade.get("symbol") != symbol:
                continue

            entry_time_str = trade.get("entry_time", "")
            if not entry_time_str:
                continue

            try:
                entry_time = datetime.fromisoformat(
                    entry_time_str.replace("Z", "+00:00")
                )
                if entry_time.tzinfo is None:
                    entry_time = entry_time.replace(tzinfo=UTC)

                if entry_time >= cutoff_time:
                    symbol_trades.append(trade)
            except (ValueError, AttributeError):
                continue

        total_positions = len(symbol_trades)

        pnls = []
        for trade in symbol_trades:
            try:
                pnl = Decimal(str(trade.get("realized_pnl", "0")))
                pnls.append(pnl)
            except (ValueError, TypeError):
                pnls.append(Decimal("0"))

        winning_positions = sum(1 for pnl in pnls if pnl > 0)
        losing_positions = sum(1 for pnl in pnls if pnl < 0)

        total_pnl = sum(pnls, Decimal("0"))
        avg_pnl = total_pnl / total_positions if total_positions > 0 else Decimal("0")
        best_trade_pnl = max(pnls) if pnls else Decimal("0")
        worst_trade_pnl = min(pnls) if pnls else Decimal("0")

        positions = [
            PositionSummary(
                entry_time=t.get("entry_time", "unknown"),
                exit_time=t.get("exit_time"),
                side=t.get("side", "unknown"),
                entry_price=str(t.get("entry_price", "0")),
                exit_price=str(t.get("exit_price")) if t.get("exit_price") else None,
                quantity=str(t.get("quantity", "0")),
                realized_pnl=str(t.get("realized_pnl", "0")),
                status=t.get("status", "unknown"),
            )
            for t in symbol_trades[:50]
        ]

        return cls(
            symbol=symbol,
            total_positions=total_positions,
            winning_positions=winning_positions,
            losing_positions=losing_positions,
            total_pnl=str(total_pnl),
            avg_pnl=str(avg_pnl),
            best_trade_pnl=str(best_trade_pnl),
            worst_trade_pnl=str(worst_trade_pnl),
            positions=positions,
            days_analyzed=days,
        )


class TableMetrics(BaseModel):
    table_name: str
    write_count: int
    error_count: int


class DbWriteMetrics(BaseModel):
    total_writes: int
    total_errors: int
    write_success_rate: float
    tables: list[TableMetrics]
    analysis_hours: int

    @classmethod
    def create(
        cls, log_entries: list[dict[str, Any]], hours: int = 24
    ) -> DbWriteMetrics:
        db_events = [
            e
            for e in log_entries
            if any(
                keyword in e.get("event", "").lower()
                for keyword in ["database", "db_write", "insert", "update"]
            )
        ]

        total_writes = len(db_events)
        error_events = [e for e in db_events if e.get("level") == "error"]
        total_errors = len(error_events)

        write_success_rate = (
            ((total_writes - total_errors) / total_writes * 100)
            if total_writes > 0
            else 100.0
        )

        table_stats: dict[str, dict[str, int]] = {}
        for event in db_events:
            event_text = event.get("event", "")
            is_error = event.get("level") == "error"

            table_name = "unknown"
            if "trades" in event_text.lower():
                table_name = "trades"
            elif "positions" in event_text.lower():
                table_name = "positions"
            elif "orders" in event_text.lower():
                table_name = "orders"

            if table_name not in table_stats:
                table_stats[table_name] = {"writes": 0, "errors": 0}

            table_stats[table_name]["writes"] += 1
            if is_error:
                table_stats[table_name]["errors"] += 1

        tables = [
            TableMetrics(
                table_name=table,
                write_count=stats["writes"],
                error_count=stats["errors"],
            )
            for table, stats in table_stats.items()
        ]

        return cls(
            total_writes=total_writes,
            total_errors=total_errors,
            write_success_rate=round(write_success_rate, 2),
            tables=tables,
            analysis_hours=hours,
        )


class TestnetValidation(BaseModel):
    testnet_active: bool
    api_key_configured: bool
    api_secret_configured: bool
    base_url: str | None
    balance_check: str
    warnings: list[str]
    recommendations: list[str]

    @classmethod
    def create(cls, config_data: dict[str, Any]) -> TestnetValidation:
        import os

        api_section = config_data.get("api", {})

        testnet_active = api_section.get("testnet", False)
        base_url = api_section.get("base_url")

        api_key = os.getenv("BINANCE_API_KEY", "")
        api_secret = os.getenv("BINANCE_API_SECRET", "")

        api_key_configured = len(api_key) > 0
        api_secret_configured = len(api_secret) > 0

        warnings = []
        recommendations = []

        if not testnet_active:
            warnings.append(
                "PRODUCTION mode active - real funds at risk. Use testnet for development."
            )
        else:
            recommendations.append("Testnet mode active - safe for development")

        if not api_key_configured:
            warnings.append("BINANCE_API_KEY not configured in environment")
        if not api_secret_configured:
            warnings.append("BINANCE_API_SECRET not configured in environment")

        if testnet_active and base_url and "testnet" not in base_url.lower():
            warnings.append(
                "Testnet mode enabled but base_url does not contain 'testnet'"
            )

        balance_check = "not_checked"
        if api_key_configured and api_secret_configured:
            balance_check = "api_configured"
            recommendations.append(
                "API credentials configured. Run bot to check account balance."
            )

        return cls(
            testnet_active=testnet_active,
            api_key_configured=api_key_configured,
            api_secret_configured=api_secret_configured,
            base_url=base_url,
            balance_check=balance_check,
            warnings=warnings,
            recommendations=recommendations,
        )


class SymbolInfo(BaseModel):
    symbol: str
    current_price: str | None
    spread: str | None
    in_cooldown: bool
    last_trade_time: str | None


class MarketAnalysis(BaseModel):
    total_symbols: int
    active_symbols: int
    symbols_in_cooldown: int
    available_for_trading: int
    symbols: list[SymbolInfo]
    market_data_timestamp: str

    @classmethod
    def create(
        cls, trades: list[dict[str, Any]], cooldown_hours: int = 24
    ) -> MarketAnalysis:
        from datetime import datetime, timedelta

        now = datetime.now(UTC)
        cooldown_cutoff = now - timedelta(hours=cooldown_hours)

        symbol_data: dict[str, dict[str, Any]] = {}

        for trade in trades:
            symbol = trade.get("symbol")
            if not symbol:
                continue

            if symbol not in symbol_data:
                symbol_data[symbol] = {
                    "last_trade_time": None,
                    "in_cooldown": False,
                    "current_price": None,
                    "spread": None,
                }

            exit_time_str = trade.get("exit_time")
            if exit_time_str:
                try:
                    exit_time = datetime.fromisoformat(
                        exit_time_str.replace("Z", "+00:00")
                    )
                    if exit_time.tzinfo is None:
                        exit_time = exit_time.replace(tzinfo=UTC)

                    if (
                        symbol_data[symbol]["last_trade_time"] is None
                        or exit_time > symbol_data[symbol]["last_trade_time"]
                    ):
                        symbol_data[symbol]["last_trade_time"] = exit_time
                        symbol_data[symbol]["in_cooldown"] = exit_time > cooldown_cutoff

                except (ValueError, AttributeError):
                    pass

        total_symbols = len(symbol_data)
        symbols_in_cooldown = sum(1 for s in symbol_data.values() if s["in_cooldown"])
        available_for_trading = total_symbols - symbols_in_cooldown
        active_symbols = total_symbols

        symbols = [
            SymbolInfo(
                symbol=sym,
                current_price=data["current_price"],
                spread=data["spread"],
                in_cooldown=data["in_cooldown"],
                last_trade_time=(
                    data["last_trade_time"].isoformat()
                    if data["last_trade_time"]
                    else None
                ),
            )
            for sym, data in symbol_data.items()
        ]

        return cls(
            total_symbols=total_symbols,
            active_symbols=active_symbols,
            symbols_in_cooldown=symbols_in_cooldown,
            available_for_trading=available_for_trading,
            symbols=symbols,
            market_data_timestamp=now.isoformat(),
        )
