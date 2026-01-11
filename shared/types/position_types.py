"""
Dataclasses para operações de posição.

Tipagem forte para evitar bugs de runtime e melhorar legibilidade.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from shared.enums import PositionState, PriceValidationStatus


@dataclass(frozen=True, slots=True)
class CloseRequest:
    """Dados de entrada para fechamento de posição."""

    symbol: str
    reason: str
    current_price: Decimal

    def __post_init__(self):
        if not self.symbol:
            raise ValueError("symbol é obrigatório")
        if not self.reason:
            raise ValueError("reason é obrigatório")
        if self.current_price <= 0:
            raise ValueError("current_price deve ser positivo")


@dataclass(frozen=True, slots=True)
class CloseResult:
    """Resultado do fechamento de posição."""

    symbol: str
    pnl: Decimal
    pnl_pct: Decimal
    reason: str
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    duration_minutes: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "pnl": self.pnl,
            "pnl_pct": self.pnl_pct,
            "reason": self.reason,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "quantity": self.quantity,
            "duration_minutes": self.duration_minutes,
        }


@dataclass(slots=True)
class PositionContext:
    """Contexto durante operação de fechamento."""

    position: dict[str, Any]
    snapshot_id: str | None = None
    oco_cancelled: bool = False
    state: PositionState = PositionState.OPEN
    close_start_time: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True, slots=True)
class PriceValidation:
    """Resultado de validação de preço."""

    status: PriceValidationStatus
    fresh_price: Decimal | None = None
    deviation_pct: float = 0.0
    error_message: str = ""
