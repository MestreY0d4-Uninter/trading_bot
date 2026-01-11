from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ComponentStatus(BaseModel):
    component: str
    last_seen_minutes_ago: int
    timeout_minutes: int | None = None


class ComponentError(BaseModel):
    component: str
    error: str
    timestamp: float
    function: str


class ComponentSlow(BaseModel):
    component: str
    duration_seconds: float


class HealthSummary(BaseModel):
    total_components: int
    healthy_count: int
    stuck_count: int
    error_count: int
    slow_count: int


class HealthCheckResponse(BaseModel):
    timestamp: str
    overall_status: str
    healthy: list[ComponentStatus]
    stuck: list[ComponentStatus]
    errors: list[ComponentError]
    slow: list[ComponentSlow]
    summary: HealthSummary
    message: str | None = None

    @classmethod
    def from_flow_tracker(cls, flow_data: dict[str, Any]) -> HealthCheckResponse:
        return cls(
            timestamp=flow_data.get("timestamp", ""),
            overall_status=flow_data.get("overall_status", "UNKNOWN"),
            healthy=[ComponentStatus(**c) for c in flow_data.get("healthy", [])],
            stuck=[ComponentStatus(**c) for c in flow_data.get("stuck", [])],
            errors=[ComponentError(**e) for e in flow_data.get("errors", [])],
            slow=[ComponentSlow(**s) for s in flow_data.get("slow", [])],
            summary=HealthSummary(**flow_data.get("summary", {})),
            message=flow_data.get("message"),
        )


class FlowTrackerSummary(BaseModel):
    available: bool
    status: str
    components_monitored: int
    healthy_components: int
    stuck_components: int
    components_with_errors: int
    slow_components: int

    @classmethod
    def from_status(cls, status: dict[str, Any]) -> FlowTrackerSummary:
        summary = status.get("summary", {})
        return cls(
            available=status.get("overall_status") != "OFFLINE",
            status=status.get("overall_status", "UNKNOWN"),
            components_monitored=summary.get("total_components", 0),
            healthy_components=summary.get("healthy_count", 0),
            stuck_components=summary.get("stuck_count", 0),
            components_with_errors=summary.get("error_count", 0),
            slow_components=summary.get("slow_count", 0),
        )
