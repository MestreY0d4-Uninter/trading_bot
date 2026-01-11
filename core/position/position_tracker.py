import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any

from shared.constants import MAX_POSITIONS
from shared.exceptions import PositionNotFoundError, ValidationError
from shared.observability.flow_tracker import track_component
from utils.decimal_math import ROUND_HALF_UP, to_decimal


class PositionTracker:
    """Position tracker with INTERNAL locking (Thread-Safe).

    Handles all position state management with atomic access.
    Can be safely shared between components (State, PositionManager).
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._positions: dict[str, dict[str, Any]] = {}
        self._position_history: list[dict[str, Any]] = []

    @track_component("position_tracker")
    async def add_position(self, symbol: str, position_data: dict[str, Any]) -> None:
        async with self._lock:
            if symbol in self._positions:
                raise ValidationError("symbol", symbol, "Position already exists")

            required_fields = ["entry_price", "quantity", "entry_time"]
            for field in required_fields:
                if field not in position_data:
                    raise ValidationError(
                        field, None, f"Missing required field {field}"
                    )

            self._positions[symbol] = {
                **position_data,
                "symbol": symbol,
                "created_at": datetime.now(),
                "updated_at": datetime.now(),
                "status": "OPEN",
            }

    @track_component("position_tracker")
    async def update_position(self, symbol: str, updates: dict[str, Any]) -> bool:
        async with self._lock:
            if symbol not in self._positions:
                raise PositionNotFoundError(symbol)

            position = self._positions[symbol]
            for key, value in updates.items():
                if key in [
                    "quantity",
                    "entry_price",
                    "current_price",
                    "pnl",
                    "pnl_pct",
                ]:
                    if value is not None and isinstance(value, (int, float)):
                        position[key] = to_decimal(value)
                else:
                    position[key] = value

            position["updated_at"] = datetime.now()
            return True

    @track_component("position_tracker")
    async def remove_position(self, symbol: str) -> dict[str, Any] | None:
        async with self._lock:
            if symbol not in self._positions:
                from shared.observability.logger import warning

                warning(
                    "⚠️ Tentativa de remover posição inexistente (idempotente)",
                    symbol=symbol,
                )
                return None

            position = self._positions.pop(symbol)
            position["closed_at"] = datetime.now()
            position["status"] = "CLOSED"
            self._position_history.append(position)

            if len(self._position_history) > 1000:
                self._position_history = self._position_history[-500:]

            return position

    @track_component("position_tracker", slow_threshold=1)
    async def get_position(self, symbol: str) -> dict[str, Any] | None:
        async with self._lock:
            if symbol not in self._positions:
                return None
            return self._positions[symbol].copy()

    @track_component("position_tracker", slow_threshold=1)
    async def get_position_by_client_id(
        self, client_order_id: str
    ) -> dict[str, Any] | None:
        async with self._lock:
            for _symbol, position in self._positions.items():
                if position.get("client_order_id") == client_order_id:
                    return position.copy()
            return None

    @track_component("position_tracker", slow_threshold=1)
    async def get_all_positions(self) -> dict[str, dict[str, Any]]:
        async with self._lock:
            return {symbol: pos.copy() for symbol, pos in self._positions.items()}

    async def has_position(self, symbol: str) -> bool:
        async with self._lock:
            return symbol in self._positions

    async def get_position_count(self) -> int:
        async with self._lock:
            return len(self._positions)

    async def can_open_position(self) -> bool:
        async with self._lock:
            return len(self._positions) < MAX_POSITIONS

    @track_component("position_tracker", slow_threshold=2)
    async def get_total_exposure(self) -> Decimal:
        from shared.observability.logger import warning

        async with self._lock:
            total = Decimal("0")
            for symbol, position in self._positions.items():
                quantity = position.get("quantity")
                current_price = position.get("current_price") or position.get(
                    "entry_price"
                )

                if not quantity or not current_price:
                    warning(
                        "Position missing critical fields, skipping from exposure",
                        symbol=symbol,
                        has_quantity=bool(quantity),
                        has_price=bool(current_price),
                    )
                    continue

                exposure = (to_decimal(quantity) * to_decimal(current_price)).quantize(
                    Decimal("0.00000001"), rounding=ROUND_HALF_UP
                )
                total += exposure
            return total.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)

    async def get_position_history(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self._lock:
            return self._position_history[-limit:]

    async def clear_all(self) -> None:
        async with self._lock:
            self._positions.clear()
            self._position_history.clear()
