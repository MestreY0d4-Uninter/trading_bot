from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TradeDetail(BaseModel):
    id: int
    symbol: str
    status: str
    entry_price: str
    exit_price: str | None = None
    quantity: str
    entry_time: str
    exit_time: str | None = None
    realized_pnl: str
    score: str | None = None
    market_condition: str | None = None
    exit_reason: str | None = None
    stop_loss: str | None = None
    take_profit: str | None = None
    timestamp: str
    created_at: str
    updated_at: str

    @classmethod
    def from_db(cls, trade_dict: dict[str, Any]) -> TradeDetail:
        return cls(**trade_dict)


class TradeTimelineEvent(BaseModel):
    timestamp: str
    level: str
    event: str
    module: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class TradeTimeline(BaseModel):
    trade_id: int
    symbol: str
    events: list[TradeTimelineEvent]
    total_events: int
    errors_count: int
    warnings_count: int

    @classmethod
    def create(
        cls, trade_id: int, symbol: str, log_entries: list[dict[str, Any]]
    ) -> TradeTimeline:
        events = [
            TradeTimelineEvent(
                timestamp=e.get("timestamp", ""),
                level=e.get("level", ""),
                event=e.get("event", ""),
                module=e.get("module"),
                details={
                    k: v
                    for k, v in e.items()
                    if k not in ["timestamp", "level", "event", "module"]
                },
            )
            for e in log_entries
        ]

        errors = sum(1 for e in log_entries if e.get("level") == "error")
        warnings = sum(1 for e in log_entries if e.get("level") == "warning")

        return cls(
            trade_id=trade_id,
            symbol=symbol,
            events=events,
            total_events=len(events),
            errors_count=errors,
            warnings_count=warnings,
        )


class TradeIssue(BaseModel):
    timestamp: str
    level: str
    event: str
    module: str | None = None
    error: str | None = None


class TradeInvestigation(BaseModel):
    trade: TradeDetail
    timeline: TradeTimeline
    issues: list[TradeIssue]
    related_logs: list[dict[str, Any]]
    summary: dict[str, Any]

    @classmethod
    def create(
        cls,
        trade: dict[str, Any],
        timeline_events: list[dict[str, Any]],
        issues: list[dict[str, Any]],
        related_logs: list[dict[str, Any]],
    ) -> TradeInvestigation:
        trade_detail = TradeDetail.from_db(trade)
        timeline = TradeTimeline.create(
            trade_id=trade["id"], symbol=trade["symbol"], log_entries=timeline_events
        )

        issue_list = [
            TradeIssue(
                timestamp=i.get("timestamp", ""),
                level=i.get("level", ""),
                event=i.get("event", ""),
                module=i.get("module"),
                error=i.get("error"),
            )
            for i in issues
        ]

        return cls(
            trade=trade_detail,
            timeline=timeline,
            issues=issue_list,
            related_logs=related_logs[:50],
            summary={
                "trade_id": trade["id"],
                "symbol": trade["symbol"],
                "status": trade["status"],
                "total_events": timeline.total_events,
                "issues_found": len(issue_list),
                "errors": timeline.errors_count,
                "warnings": timeline.warnings_count,
            },
        )


class ProblemTradesResponse(BaseModel):
    trades: list[TradeDetail]
    total_problem_trades: int
    breakdown: dict[str, int]
    hours_analyzed: int

    @classmethod
    def create(cls, trades: list[dict[str, Any]], hours: int) -> ProblemTradesResponse:
        trade_details = [TradeDetail.from_db(t) for t in trades]

        breakdown = {
            "negative_pnl": sum(
                1 for t in trades if float(t.get("realized_pnl", 0)) < 0
            ),
            "emergency_stops": sum(
                1
                for t in trades
                if t.get("exit_reason")
                and "emergency" in t.get("exit_reason", "").lower()
            ),
            "manual_stops": sum(
                1
                for t in trades
                if t.get("exit_reason") and "stop" in t.get("exit_reason", "").lower()
            ),
            "still_open": sum(1 for t in trades if t.get("status") != "CLOSED"),
        }

        return cls(
            trades=trade_details,
            total_problem_trades=len(trade_details),
            breakdown=breakdown,
            hours_analyzed=hours,
        )
