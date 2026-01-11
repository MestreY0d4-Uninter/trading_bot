from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class PositionState(BaseModel):
    symbol: str
    status: str
    entry_price: str
    quantity: str
    entry_time: str
    current_price: str | None = None
    pnl: str | None = None
    pnl_pct: str | None = None
    stop_loss: str | None = None
    take_profit: str | None = None
    client_order_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PositionState:
        return cls(
            symbol=data.get("symbol", ""),
            status=data.get("status", "UNKNOWN"),
            entry_price=str(data.get("entry_price", "0")),
            quantity=str(data.get("quantity", "0")),
            entry_time=data.get("entry_time", ""),
            current_price=(
                str(data.get("current_price")) if data.get("current_price") else None
            ),
            pnl=str(data.get("pnl")) if data.get("pnl") else None,
            pnl_pct=str(data.get("pnl_pct")) if data.get("pnl_pct") else None,
            stop_loss=str(data.get("stop_loss")) if data.get("stop_loss") else None,
            take_profit=(
                str(data.get("take_profit")) if data.get("take_profit") else None
            ),
            client_order_id=data.get("client_order_id"),
            created_at=str(data.get("created_at")) if data.get("created_at") else None,
            updated_at=str(data.get("updated_at")) if data.get("updated_at") else None,
        )


class PositionTrackerResponse(BaseModel):
    available: bool
    positions: dict[str, PositionState]
    total_positions: int
    total_exposure: str
    symbols: list[str]
    message: str | None = None

    @classmethod
    def create_unavailable(cls) -> PositionTrackerResponse:
        return cls(
            available=False,
            positions={},
            total_positions=0,
            total_exposure="0",
            symbols=[],
            message="PositionTracker requires bot running in same process. Use DatabaseClient for historical data.",
        )

    @classmethod
    def create_from_state(
        cls, positions: dict[str, dict[str, Any]], exposure: str
    ) -> PositionTrackerResponse:
        position_states = {
            symbol: PositionState.from_dict(data) for symbol, data in positions.items()
        }

        return cls(
            available=True,
            positions=position_states,
            total_positions=len(position_states),
            total_exposure=exposure,
            symbols=list(position_states.keys()),
            message=None,
        )


class UncertainOrder(BaseModel):
    client_order_id: str
    symbol: str
    side: str
    timestamp: str
    status: str
    resolution: str | None = None


class ReconciliationStatus(BaseModel):
    uncertain_orders: list[UncertainOrder]
    total_uncertain: int
    resolved_count: int
    pending_count: int
    success_rate: float
    recent_reconciliation_events: list[dict[str, Any]]

    @classmethod
    def from_logs(cls, log_entries: list[dict[str, Any]]) -> ReconciliationStatus:
        uncertain_orders = [
            UncertainOrder(
                client_order_id=e.get("client_order_id", ""),
                symbol=e.get("symbol", ""),
                side=e.get("side", ""),
                timestamp=e.get("timestamp", ""),
                status="UNCERTAIN",
                resolution=e.get("resolution"),
            )
            for e in log_entries
            if "uncertain" in e.get("event", "").lower()
        ]

        resolved = sum(1 for o in uncertain_orders if o.resolution)
        pending = len(uncertain_orders) - resolved
        success_rate = (
            (resolved / len(uncertain_orders) * 100) if uncertain_orders else 100.0
        )

        return cls(
            uncertain_orders=uncertain_orders,
            total_uncertain=len(uncertain_orders),
            resolved_count=resolved,
            pending_count=pending,
            success_rate=round(success_rate, 2),
            recent_reconciliation_events=log_entries[:20],
        )
