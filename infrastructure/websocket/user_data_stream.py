"""
User Data Stream Manager

Manages Binance User Data Stream WebSocket connection.

Responsibilities:
- Connect to User Data Stream via ThreadedWebsocketManager
- Process executionReport events (order updates)
- Process outboundAccountPosition events (balance updates)
- Update PositionTracker in real-time
- Handle reconnections automatically
- Signal order reconciliation events

Integrates with existing WebSocket infrastructure.
"""

import asyncio
from datetime import datetime
from decimal import Decimal

from binance import ThreadedWebsocketManager

from shared.observability.flow_tracker import track_component
from shared.observability.logger import debug, error, production, warning


class UserDataStreamManager:
    """
    Manages Binance User Data Stream WebSocket.

    Features:
    - Real-time order updates (executionReport)
    - Account position updates (outboundAccountPosition)
    - Balance updates (balanceUpdate)
    - Automatic reconnection via ThreadedWebsocketManager
    - Integration with PositionTracker
    - Reconciliation event signaling
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        position_tracker,
        db_repository=None,
        testnet=False,
    ):
        """
        Initialize User Data Stream Manager.

        Args:
            api_key: Binance API key
            api_secret: Binance API secret
            position_tracker: PositionTracker instance
            db_repository: Optional repository for logging
            testnet: Use testnet endpoints (default: False)
        """
        self.position_tracker = position_tracker
        self.db_repository = db_repository
        self.testnet = testnet

        # Create ThreadedWebsocketManager
        self.twm = ThreadedWebsocketManager(
            api_key=api_key, api_secret=api_secret, testnet=testnet
        )

        # State
        self.is_running = False
        self._stream_name: str | None = None
        self._main_loop = None  # Store reference to main event loop

        # Reconciliation manager (set by bot_orchestrator)
        self.reconciliation_manager = None

    async def start(self):
        """Start user data stream"""
        if self.is_running:
            warning("User data stream already running")
            return

        try:
            # Store reference to main event loop for thread-safe callback
            self._main_loop = asyncio.get_running_loop()

            # Start ThreadedWebsocketManager
            self.twm.start()
            production("ThreadedWebsocketManager started")

            # Start user socket (auto-manages listen key)
            self._stream_name = self.twm.start_user_socket(
                callback=self._socket_callback
            )

            self.is_running = True
            production(
                "User data stream started successfully", stream=self._stream_name
            )

        except Exception as e:
            error("Failed to start user data stream", error=str(e))
            await self.stop()
            raise

    async def stop(self):
        """Stop user data stream"""
        if not self.is_running:
            return

        self.is_running = False

        try:
            if self.twm:
                self.twm.stop()
                production("User data stream stopped")
        except Exception as e:
            warning("Error stopping user data stream", error=str(e))

    def _socket_callback(self, msg: dict):
        """
        WebSocket callback (runs in thread).

        Note: This runs in ThreadedWebsocketManager thread,
        so we need to schedule async handler in main event loop.

        Args:
            msg: WebSocket message dict
        """
        try:
            # Use stored main loop reference (thread-safe)
            if self._main_loop:
                asyncio.run_coroutine_threadsafe(
                    self.handle_user_data_event(msg), self._main_loop
                )
            else:
                error("Main event loop not initialized")
        except Exception as e:
            error("Error in socket callback", error=str(e), message=msg)

    @track_component("user_data_stream")
    async def handle_user_data_event(self, event: dict):
        """
        Handle user data stream event.

        Args:
            event: WebSocket event dict

        Event types:
            - executionReport: Order update
            - outboundAccountPosition: Balance update
            - balanceUpdate: Balance change
        """
        event_type = event.get("e")

        if event_type == "executionReport":
            await self._handle_execution_report(event)
        elif event_type == "outboundAccountPosition":
            await self._handle_account_position(event)
        elif event_type == "balanceUpdate":
            await self._handle_balance_update(event)
        else:
            debug("Unknown user data event", event_type=event_type)

    async def _handle_execution_report(self, event: dict):
        """
        Handle executionReport event.

        Event structure:
        {
            'e': 'executionReport',
            'E': 1234567890,           # Event time
            's': 'BTCUSDT',            # Symbol
            'c': 'buy-BTCUSDT-123-a1', # clientOrderId
            'i': 12345,                # orderId
            'X': 'FILLED',             # Order status
            'x': 'TRADE',              # Execution type
            'z': '0.001',              # Cumulative filled quantity
            'Z': '50.0',               # Cumulative quote asset transacted
            'L': '50000.0',            # Last executed price
            ...
        }
        """
        try:
            client_order_id = event.get("c")
            symbol = event.get("s")
            order_status = event.get("X")
            execution_type = event.get("x")

            production(
                "Execution report received",
                symbol=symbol,
                client_id=client_order_id,
                status=order_status,
                exec_type=execution_type,
            )

            # Get position by client_order_id
            position = await self.position_tracker.get_position_by_client_id(
                client_order_id
            )

            if not position:
                debug(
                    "Order not tracked (may be manual or external)",
                    client_id=client_order_id,
                )
                return

            # Update position with execution data
            updates = {
                "exchange_order_id": event.get("i"),
                "status": order_status,
                "filled_quantity": Decimal(str(event.get("z", 0))),
                "last_price": Decimal(str(event.get("L", 0))),
                "cumulative_quote_qty": Decimal(str(event.get("Z", 0))),
                "execution_type": execution_type,
                "updated_at": datetime.now(),
                "reconciled_via": "WebSocket",
            }

            await self.position_tracker.update_position(symbol, updates)

            # Signal reconciliation if applicable
            if hasattr(self, "reconciliation_manager") and self.reconciliation_manager:
                await self.reconciliation_manager.signal_order_update(client_order_id)

            # Log to database if FILLED
            if order_status == "FILLED" and self.db_repository:
                await self._log_filled_order(position, event)

            production(
                "Position updated from WebSocket",
                symbol=symbol,
                status=order_status,
            )

        except Exception as e:
            error("Error handling execution report", error=str(e), event=event)

    async def _handle_account_position(self, event: dict):
        """
        Handle outboundAccountPosition event.

        Event structure:
        {
            'e': 'outboundAccountPosition',
            'E': 1234567890,
            'u': 1234567890,
            'B': [
                {
                    'a': 'USDT',
                    'f': '1000.00',
                    'l': '0.00'
                }
            ]
        }
        """
        debug("Account position update", event=event)
        # Placeholder - can be used for balance tracking

    async def _handle_balance_update(self, event: dict):
        """
        Handle balanceUpdate event.

        Event structure:
        {
            'e': 'balanceUpdate',
            'E': 1234567890,
            'a': 'USDT',
            'd': '100.00'
        }
        """
        debug("Balance update", event=event)
        # Placeholder - can be used for balance tracking

    async def _log_filled_order(self, position: dict, event: dict):
        """
        Log filled order to database (analytics only).

        Args:
            position: Position dict from tracker
            event: Execution report event
        """
        try:
            # Extract data
            symbol = position.get("symbol")
            side = position.get("side")
            filled_qty = Decimal(str(event.get("z", 0)))
            avg_price = (
                Decimal(str(event.get("Z", 0))) / filled_qty
                if filled_qty > 0
                else Decimal(0)
            )

            debug(
                "Logging filled order to database",
                symbol=symbol,
                side=side,
                qty=filled_qty,
                price=avg_price,
            )

            # Save to DB (if repository available)
            # This is analytics only - not used for trading decisions
            # Implementation depends on repository structure

        except Exception as e:
            error("Error logging filled order", error=str(e))
