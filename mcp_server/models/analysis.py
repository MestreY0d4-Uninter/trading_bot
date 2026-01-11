from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class TradeSnapshot(BaseModel):
    id: int
    symbol: str
    status: str
    entry_price: str
    quantity: str
    entry_time: str
    realized_pnl: str | None = None
    exit_reason: str | None = None


class PositionSnapshot(BaseModel):
    symbol: str
    status: str
    entry_price: str
    quantity: str
    entry_time: str
    current_price: str | None = None
    pnl: str | None = None
    pnl_pct: str | None = None


class MarketDataSnapshot(BaseModel):
    symbol: str
    price: str
    timestamp: str
    volume: str | None = None
    spread: str | None = None


class PerformanceMetrics(BaseModel):
    total_trades: int
    open_trades: int
    closed_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: str
    avg_pnl: str
    best_trade: str
    worst_trade: str

    @classmethod
    def from_db_data(cls, trades: list[dict[str, Any]]) -> PerformanceMetrics:
        from decimal import Decimal

        total = len(trades)
        open_count = sum(1 for t in trades if t.get("status") == "OPEN")
        closed_count = sum(1 for t in trades if t.get("status") == "CLOSED")

        closed_trades = [t for t in trades if t.get("status") == "CLOSED"]
        pnls = [
            Decimal(str(t.get("realized_pnl", "0")))
            for t in closed_trades
            if t.get("realized_pnl")
        ]

        winning = sum(1 for pnl in pnls if pnl > 0)
        losing = sum(1 for pnl in pnls if pnl < 0)
        win_rate = (winning / len(pnls) * 100) if pnls else 0.0

        total_pnl = sum(pnls) if pnls else Decimal("0")
        avg_pnl = total_pnl / len(pnls) if pnls else Decimal("0")
        best = max(pnls) if pnls else Decimal("0")
        worst = min(pnls) if pnls else Decimal("0")

        return cls(
            total_trades=total,
            open_trades=open_count,
            closed_trades=closed_count,
            winning_trades=winning,
            losing_trades=losing,
            win_rate=round(win_rate, 2),
            total_pnl=str(total_pnl),
            avg_pnl=str(avg_pnl),
            best_trade=str(best),
            worst_trade=str(worst),
        )


class ErrorSnapshot(BaseModel):
    timestamp: str
    level: str
    event: str
    module: str | None = None
    error: str | None = None


class CompleteAnalysisResponse(BaseModel):
    timestamp: str
    bot_health: dict[str, Any]
    trades: list[TradeSnapshot]
    positions: list[PositionSnapshot]
    performance: PerformanceMetrics
    market_data: list[MarketDataSnapshot]
    recent_errors: list[ErrorSnapshot]
    summary: dict[str, Any]

    @classmethod
    def create(
        cls,
        health: dict[str, Any],
        trades: list[dict[str, Any]],
        positions: list[dict[str, Any]],
        market_data: list[dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> CompleteAnalysisResponse:
        from datetime import datetime

        performance = PerformanceMetrics.from_db_data(trades)

        return cls(
            timestamp=datetime.now().isoformat(),
            bot_health=health,
            trades=[TradeSnapshot(**t) for t in trades[:50]],
            positions=[PositionSnapshot(**p) for p in positions],
            performance=performance,
            market_data=[MarketDataSnapshot(**m) for m in market_data[:20]],
            recent_errors=[ErrorSnapshot(**e) for e in errors[:20]],
            summary={
                "total_trades": len(trades),
                "open_positions": len(positions),
                "recent_errors": len(errors),
                "health_status": health.get("overall_status", "UNKNOWN"),
            },
        )
