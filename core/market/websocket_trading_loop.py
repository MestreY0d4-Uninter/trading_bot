import asyncio
import time
from typing import Any

from shared.observability.flow_tracker import track_component
from shared.observability.logger import (
    debug,
    error,
    production,
    trading_diagnostic,
)
from shared.types.state import state
from utils.validation_utils import validate_symbol


class WebSocketTradingLoop:
    def __init__(self, coordinator: Any, websocket_manager: Any) -> None:
        if not coordinator:
            raise ValueError("Coordinator é obrigatório")

        if not websocket_manager:
            raise ValueError(
                "WebSocketManager é obrigatório para WebSocket trading loop"
            )

        required_components = [
            "config",
            "signal_analyzer",
            "position_manager",
            "risk_manager",
            "data_manager",
        ]

        missing = [
            c
            for c in required_components
            if not hasattr(coordinator, c) or getattr(coordinator, c) is None
        ]

        if missing:
            raise AttributeError(f"Coordinator missing components: {missing}")

        self.coordinator = coordinator
        self.websocket_manager = websocket_manager
        self.config = coordinator.config
        self.signal_analyzer = coordinator.signal_analyzer
        self.position_manager = coordinator.position_manager
        self.risk_manager = coordinator.risk_manager
        self.data_manager = coordinator.data_manager

        self.throttle_interval = self.config.get("websocket", {}).get(
            "throttle_interval_seconds", 5
        )
        self.symbol_cooldowns = self.config.get("strategy", {}).get(
            "cooldown_seconds", 100
        )

        self.last_analysis_time: dict[str, float] = {}
        self.klines_received = 0
        self.klines_analyzed = 0
        self.klines_throttled = 0
        self.analysis_semaphore = None

    def _should_throttle(self, symbol: str) -> bool:
        if symbol not in self.last_analysis_time:
            return False

        elapsed = time.time() - self.last_analysis_time[symbol]
        return elapsed < self.throttle_interval

    @track_component("websocket_trading_loop")
    async def _process_kline(self, kline_data: dict):
        debug(
            f"📥 _process_kline CALLED, type={kline_data.get('type')}, symbol={kline_data.get('symbol')}, is_closed={kline_data.get('is_closed')}, keys={list(kline_data.keys())[:10]}",
        )
        try:
            if kline_data.get("type") != "kline":
                debug(
                    f"⚠️ Skipping non-kline message: {kline_data.get('type')}",
                )
                return

            is_closed = kline_data.get("is_closed")
            debug(
                f"🔍 is_closed value: {is_closed}, type: {type(is_closed)}",
            )

            if not is_closed:
                debug(
                    f"⏭️ Skipping {kline_data.get('symbol')} - kline not closed (is_closed={is_closed})",
                )
                return

            symbol = kline_data.get("symbol", "UNKNOWN")
            self.klines_received += 1
            production(
                f"✅ Kline closed for {symbol}, klines_received={self.klines_received}",
            )

            if not validate_symbol(symbol):
                production(f" Invalid symbol: {symbol}")
                return

            if self._should_throttle(symbol):
                self.klines_throttled += 1
                production(
                    f"⏸️ Throttled {symbol}, throttled_count={self.klines_throttled}",
                )
                return

            if await state.is_in_cooldown(symbol):
                production(f" {symbol} in cooldown")
                return

            if await state.has_position(symbol):
                production(f" {symbol} already has position")
                return

            production(f" {symbol} passed all filters, analyzing...")

            debug(f"⏳ ANTES de semaphore.acquire() para {symbol}")
            async with self.analysis_semaphore:
                debug(f"✅ DENTRO do semaphore block para {symbol}")
                self.last_analysis_time[symbol] = time.time()
                self.klines_analyzed += 1

                trading_diagnostic(
                    "WebSocket kline analysis",
                    symbol=symbol,
                    close=str(kline_data.get("close")),
                    volume=str(kline_data.get("volume")),
                    received=self.klines_received,
                    analyzed=self.klines_analyzed,
                    throttled=self.klines_throttled,
                )

                debug(f"📊 ANTES de analyze_symbol() para {symbol}")
                signal = await self.signal_analyzer.analyze_symbol(
                    symbol, self.data_manager
                )

                if signal:
                    production(
                        "🔍 DEBUG - Signal retornado do analyzer",
                        symbol=symbol,
                        signal_type=signal.get("type"),
                        score=signal.get("entry_score", 0),
                    )

                if signal and signal.get("type") == "BUY":
                    production(
                        "🔍 DEBUG - WebSocket signal BUY detectado, chamando _execute_entry",
                        symbol=symbol,
                        score=signal.get("entry_score", 0),
                        price=str(signal.get("price", 0)),
                    )

                    await self._execute_entry(symbol, signal, kline_data)

        except Exception as e:
            error(f"Error processing WebSocket kline: {e}", exc_info=True)

    async def _execute_entry(self, symbol: str, signal, kline_data: dict):
        """Execute entry based on WebSocket signal"""
        production(
            "🔍 DEBUG - _execute_entry CHAMADO",
            symbol=symbol,
            score=signal.get("entry_score", 0),
        )
        try:
            async with self.coordinator.global_entry_semaphore:
                debug("Global entry semaphore acquired", symbol=symbol)

                allowed, reason = await self.risk_manager.check_position_allowed()
                if not allowed:
                    production(
                        "Signal rejected - position not allowed",
                        symbol=symbol,
                        reason=reason,
                        score=signal.get("entry_score", 0),
                    )
                    return

                usdt_balance = self.risk_manager.current_balance
                if usdt_balance <= 0:
                    production(
                        "Signal rejected - insufficient balance",
                        symbol=symbol,
                        balance=str(usdt_balance),
                        score=signal.get("entry_score", 0),
                    )
                    return

                success = await self.position_manager.open_position(
                    symbol=symbol, signal=signal, usdt_balance=usdt_balance
                )

                if success:
                    production(
                        "WebSocket entry executed",
                        symbol=symbol,
                        entry_price=str(signal.get("price")),
                        score=signal.get("entry_score"),
                    )

        except Exception as e:
            error(f"Error executing WebSocket entry: {e}", exc_info=True)

    @track_component("websocket_trading_loop", slow_threshold=30)
    async def run(self):
        """
        Main event-driven loop.

        Registers callback with WebSocket and processes klines as they arrive.
        """
        debug("✅ WebSocketTradingLoop.run() CALLED")

        if self.analysis_semaphore is None:
            self.analysis_semaphore = asyncio.Semaphore(4)
            debug("✅ Semaphore created")

        production("WebSocket Trading Loop started (event-driven mode)")
        debug("✅ After 'started' log")

        debug(
            f"✅ ANTES de registrar callback, callbacks atuais: {len(self.websocket_manager.on_message_callbacks)}",
        )
        self.websocket_manager.on_message_callbacks.append(self._process_kline)
        debug(
            f"✅ DEPOIS de registrar callback, callbacks totais: {len(self.websocket_manager.on_message_callbacks)}",
        )

        production(
            "Registered WebSocket callback",
            symbols=len(self.websocket_manager.config.symbols),
        )
        debug(
            f"✅ Callback registered for {len(self.websocket_manager.config.symbols)} symbols",
        )

        log_interval = 300
        last_log = 0

        try:
            while (
                self.coordinator.running
                and not self.coordinator.shutdown_event.is_set()
            ):
                await asyncio.sleep(1)

                if time.time() - last_log > log_interval:
                    production(
                        "WebSocket Trading Loop stats",
                        klines_received=self.klines_received,
                        klines_analyzed=self.klines_analyzed,
                        klines_throttled=self.klines_throttled,
                        analysis_rate=f"{(self.klines_analyzed / max(1, self.klines_received)) * 100:.1f}%",
                    )
                    last_log = time.time()

        except asyncio.CancelledError:
            production("WebSocket Trading Loop cancelled")
        except Exception as e:
            error(f"WebSocket Trading Loop error: {e}", exc_info=True)
        finally:
            if self._process_kline in self.websocket_manager.on_message_callbacks:
                self.websocket_manager.on_message_callbacks.remove(self._process_kline)
            production("WebSocket Trading Loop stopped")
