from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class CircuitBreakerState(BaseModel):
    timestamp: str
    is_open: bool
    reason: str | None = None
    opened_at: str | None = None
    cooldown_until: str | None = None


class CircuitBreakerValidation(BaseModel):
    timestamp: str
    valid: bool
    db_state: CircuitBreakerState | None
    log_events_count: int
    last_trigger: dict[str, Any] | None
    last_reset: dict[str, Any] | None
    issues: list[str]

    @classmethod
    def create(
        cls,
        db_state: dict[str, Any] | None,
        log_events: list[dict[str, Any]],
        issues: list[str],
    ) -> CircuitBreakerValidation:
        from datetime import datetime

        cb_state = None
        if db_state:
            cb_state = CircuitBreakerState(
                timestamp=db_state.get("updated_at", ""),
                is_open=bool(db_state.get("is_open", False)),
                reason=db_state.get("reason"),
                opened_at=db_state.get("opened_at"),
                cooldown_until=db_state.get("cooldown_until"),
            )

        trigger_events = [
            e for e in log_events if "trigger" in e.get("event", "").lower()
        ]
        reset_events = [e for e in log_events if "reset" in e.get("event", "").lower()]

        return cls(
            timestamp=datetime.now().isoformat(),
            valid=len(issues) == 0,
            db_state=cb_state,
            log_events_count=len(log_events),
            last_trigger=trigger_events[-1] if trigger_events else None,
            last_reset=reset_events[-1] if reset_events else None,
            issues=issues,
        )


class RiskLimitCheck(BaseModel):
    check_name: str
    within_limit: bool
    current_value: str
    limit_value: str
    utilization_pct: float


class RiskLimitValidation(BaseModel):
    timestamp: str
    within_limits: bool
    checks: list[RiskLimitCheck]
    total_exposure: str
    max_exposure: str
    open_positions: int
    max_positions: int
    issues: list[str]

    @classmethod
    def create(
        cls,
        open_positions: int,
        max_positions: int,
        total_exposure: float,
        max_exposure: float,
    ) -> RiskLimitValidation:
        from datetime import datetime

        checks = []
        issues = []

        position_utilization = (
            (open_positions / max_positions * 100) if max_positions > 0 else 0
        )
        checks.append(
            RiskLimitCheck(
                check_name="max_positions",
                within_limit=open_positions <= max_positions,
                current_value=str(open_positions),
                limit_value=str(max_positions),
                utilization_pct=round(position_utilization, 2),
            )
        )

        if open_positions > max_positions:
            issues.append(
                f"Position count exceeds limit: {open_positions} > {max_positions}"
            )

        exposure_utilization = (
            (total_exposure / max_exposure * 100) if max_exposure > 0 else 0
        )
        checks.append(
            RiskLimitCheck(
                check_name="max_exposure",
                within_limit=total_exposure <= max_exposure,
                current_value=f"{total_exposure:.2f}",
                limit_value=f"{max_exposure:.2f}",
                utilization_pct=round(exposure_utilization, 2),
            )
        )

        if total_exposure > max_exposure:
            issues.append(
                f"Exposure exceeds limit: {total_exposure:.2f} > {max_exposure:.2f}"
            )

        return cls(
            timestamp=datetime.now().isoformat(),
            within_limits=len(issues) == 0,
            checks=checks,
            total_exposure=f"{total_exposure:.2f}",
            max_exposure=f"{max_exposure:.2f}",
            open_positions=open_positions,
            max_positions=max_positions,
            issues=issues,
        )


class DataIntegrityIssue(BaseModel):
    issue_type: str
    severity: str
    description: str
    affected_records: int
    sample_ids: list[int]


class DataIntegrityReport(BaseModel):
    timestamp: str
    hours_analyzed: int
    integrity_score: float
    total_trades: int
    total_issues: int
    issues: list[DataIntegrityIssue]
    summary: dict[str, int]

    @classmethod
    def create(cls, trades: list[dict[str, Any]], hours: int) -> DataIntegrityReport:
        from datetime import datetime
        from decimal import Decimal

        issues: list[DataIntegrityIssue] = []

        missing_pnl = [
            t
            for t in trades
            if t.get("status") == "CLOSED" and not t.get("realized_pnl")
        ]
        if missing_pnl:
            issues.append(
                DataIntegrityIssue(
                    issue_type="missing_pnl",
                    severity="high",
                    description="Closed trades without realized PnL",
                    affected_records=len(missing_pnl),
                    sample_ids=[t["id"] for t in missing_pnl[:5]],
                )
            )

        invalid_pnl = []
        for t in trades:
            if t.get("realized_pnl"):
                try:
                    pnl = Decimal(str(t["realized_pnl"]))
                    if str(pnl) == "NaN" or pnl > Decimal("1000000"):
                        invalid_pnl.append(t)
                except Exception:
                    invalid_pnl.append(t)

        if invalid_pnl:
            issues.append(
                DataIntegrityIssue(
                    issue_type="invalid_pnl",
                    severity="high",
                    description="Trades with invalid PnL values (NaN or extreme)",
                    affected_records=len(invalid_pnl),
                    sample_ids=[t["id"] for t in invalid_pnl[:5]],
                )
            )

        orphan_trades = [
            t for t in trades if t.get("status") == "OPEN" and not t.get("entry_time")
        ]
        if orphan_trades:
            issues.append(
                DataIntegrityIssue(
                    issue_type="orphan_records",
                    severity="medium",
                    description="Open trades without entry time",
                    affected_records=len(orphan_trades),
                    sample_ids=[t["id"] for t in orphan_trades[:5]],
                )
            )

        integrity_score = 100.0 - (len(issues) / len(trades) * 100) if trades else 100.0

        summary = {
            "missing_pnl": len(missing_pnl),
            "invalid_pnl": len(invalid_pnl),
            "orphan_records": len(orphan_trades),
        }

        return cls(
            timestamp=datetime.now().isoformat(),
            hours_analyzed=hours,
            integrity_score=round(integrity_score, 2),
            total_trades=len(trades),
            total_issues=len(issues),
            issues=issues,
            summary=summary,
        )


class TrackBeforeSendValidation(BaseModel):
    timestamp: str
    pattern_valid: bool
    total_orders: int
    orders_with_client_id: int
    ghost_orders: int
    orphan_orders: int
    validation_rate: float
    issues: list[str]

    @classmethod
    def create(cls, log_entries: list[dict[str, Any]]) -> TrackBeforeSendValidation:
        from datetime import datetime

        order_logs = [
            e
            for e in log_entries
            if "order" in e.get("event", "").lower() and "client_order_id" in e
        ]

        orders_with_id = [e for e in order_logs if e.get("client_order_id")]

        sent_orders = [e for e in order_logs if "sent" in e.get("event", "").lower()]
        tracked_orders = [
            e for e in order_logs if "track" in e.get("event", "").lower()
        ]

        ghost_count = max(0, len(sent_orders) - len(tracked_orders))
        orphan_count = max(0, len(tracked_orders) - len(sent_orders))

        validation_rate = (
            (len(orders_with_id) / len(order_logs) * 100) if order_logs else 100.0
        )

        issues = []
        if ghost_count > 0:
            issues.append(
                f"Found {ghost_count} potential ghost orders (sent but not tracked)"
            )
        if orphan_count > 0:
            issues.append(
                f"Found {orphan_count} potential orphan orders (tracked but not sent)"
            )

        return cls(
            timestamp=datetime.now().isoformat(),
            pattern_valid=len(issues) == 0,
            total_orders=len(order_logs),
            orders_with_client_id=len(orders_with_id),
            ghost_orders=ghost_count,
            orphan_orders=orphan_count,
            validation_rate=round(validation_rate, 2),
            issues=issues,
        )


class EmergencyStopSummary(BaseModel):
    timestamp: str
    days_analyzed: int
    total_emergency_stops: int
    by_protection_level: dict[str, int]
    by_symbol: dict[str, int]
    avg_loss_per_stop: str
    total_loss: str
    trades: list[dict[str, Any]]

    @classmethod
    def create(cls, trades: list[dict[str, Any]], days: int) -> EmergencyStopSummary:
        from datetime import datetime
        from decimal import Decimal

        emergency_trades = [
            t
            for t in trades
            if t.get("exit_reason") and "emergency" in t.get("exit_reason", "").lower()
        ]

        by_protection: dict[str, int] = {}
        by_symbol: dict[str, int] = {}

        for trade in emergency_trades:
            level = trade.get("protection_level", "unknown")
            by_protection[level] = by_protection.get(level, 0) + 1

            symbol = trade.get("symbol", "unknown")
            by_symbol[symbol] = by_symbol.get(symbol, 0) + 1

        losses = [
            Decimal(str(t.get("realized_pnl", "0")))
            for t in emergency_trades
            if t.get("realized_pnl")
        ]

        total_loss = sum(losses) if losses else Decimal("0")
        avg_loss = total_loss / len(losses) if losses else Decimal("0")

        return cls(
            timestamp=datetime.now().isoformat(),
            days_analyzed=days,
            total_emergency_stops=len(emergency_trades),
            by_protection_level=by_protection,
            by_symbol=by_symbol,
            avg_loss_per_stop=str(avg_loss),
            total_loss=str(total_loss),
            trades=[
                {
                    "id": t["id"],
                    "symbol": t["symbol"],
                    "realized_pnl": t.get("realized_pnl"),
                    "exit_reason": t.get("exit_reason"),
                    "protection_level": t.get("protection_level"),
                }
                for t in emergency_trades[:20]
            ],
        )


class ApiRecoveryStatus(BaseModel):
    timestamp: str
    in_sync: bool
    last_recovery_time: str | None
    db_trades_count: int
    db_open_positions: int
    sync_issues: list[str]
    recovery_events: list[dict[str, Any]]

    @classmethod
    def create(
        cls,
        db_trades: list[dict[str, Any]],
        log_entries: list[dict[str, Any]],
    ) -> ApiRecoveryStatus:
        from datetime import datetime

        recovery_logs = [
            e for e in log_entries if "recovery" in e.get("event", "").lower()
        ]

        open_positions = [t for t in db_trades if t.get("status") == "OPEN"]

        last_recovery = recovery_logs[-1] if recovery_logs else None

        sync_issues = []

        if len(open_positions) > 10:
            sync_issues.append(
                f"Unusually high number of open positions: {len(open_positions)}"
            )

        return cls(
            timestamp=datetime.now().isoformat(),
            in_sync=len(sync_issues) == 0,
            last_recovery_time=(
                last_recovery.get("timestamp") if last_recovery else None
            ),
            db_trades_count=len(db_trades),
            db_open_positions=len(open_positions),
            sync_issues=sync_issues,
            recovery_events=recovery_logs[-5:],
        )


class IdempotencyStatus(BaseModel):
    timestamp: str
    hours_analyzed: int
    duplicates_prevented: int
    idempotency_keys_used: int
    duplicate_events: list[dict[str, Any]]
    effectiveness_rate: float

    @classmethod
    def create(cls, log_entries: list[dict[str, Any]], hours: int) -> IdempotencyStatus:
        from datetime import datetime

        idempotency_logs = [
            e
            for e in log_entries
            if "idempotency" in e.get("event", "").lower()
            or "duplicate" in e.get("event", "").lower()
        ]

        duplicate_events = [
            e for e in idempotency_logs if "duplicate" in e.get("event", "").lower()
        ]

        keys_used = [e for e in idempotency_logs if "key" in e.get("event", "").lower()]

        effectiveness = (
            (len(duplicate_events) / len(keys_used) * 100) if keys_used else 0.0
        )

        return cls(
            timestamp=datetime.now().isoformat(),
            hours_analyzed=hours,
            duplicates_prevented=len(duplicate_events),
            idempotency_keys_used=len(keys_used),
            duplicate_events=duplicate_events[:10],
            effectiveness_rate=round(effectiveness, 2),
        )
