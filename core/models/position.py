"""
Pydantic models para validação de posições de trading.

Constraints baseados em core/validators/trading_validator.py + core/position/.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.constants import (
    MAX_POSITION_SIZE_PCT,
    MIN_POSITION_SIZE,
    MIN_PROFIT_TARGET,
)

PositionStatus = Literal["OPEN", "CLOSED", "PENDING"]


class PositionParams(BaseModel):
    """
    Parâmetros validados para abertura de posição.

    Constraints:
    - entry_price > 0
    - stop_loss < entry_price (proteção downside)
    - take_profit > entry_price (target upside)
    - position_size >= MIN_POSITION_SIZE (10 USDT)
    - position_size <= MAX_POSITION_SIZE_PCT * balance (8.5% do balance)
    - profit_target >= MIN_PROFIT_TARGET (0.8%)
    """

    symbol: str = Field(..., min_length=1, description="Trading pair symbol")
    entry_price: Decimal = Field(..., gt=0, description="Entry price")
    quantity: Decimal = Field(..., gt=0, description="Position quantity")
    position_size: Decimal = Field(..., gt=0, description="Position size in USDT")
    stop_loss: Decimal = Field(..., gt=0, description="Stop loss price")
    take_profit: Decimal = Field(..., gt=0, description="Take profit price")
    balance: Decimal = Field(..., gt=0, description="Available USDT balance")

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        """Valida formato do symbol"""
        if not v or not isinstance(v, str):
            raise ValueError("Symbol must be non-empty string")
        return v.upper()

    @model_validator(mode="after")
    def validate_stop_loss_take_profit(self):
        """
        Valida stop loss e take profit:
        - Stop loss < entry price (proteção downside)
        - Take profit > entry price (target upside)
        """
        if self.stop_loss >= self.entry_price:
            raise ValueError(
                f"Stop loss {self.stop_loss} must be below entry price {self.entry_price}"
            )

        if self.take_profit <= self.entry_price:
            raise ValueError(
                f"Take profit {self.take_profit} must be above entry price {self.entry_price}"
            )

        return self

    @model_validator(mode="after")
    def validate_position_size(self):
        """
        Valida position size:
        - >= MIN_POSITION_SIZE (default 10 USDT)
        - <= MAX_POSITION_SIZE_PCT * balance (default 25% do balance)
        """
        min_size = Decimal(str(MIN_POSITION_SIZE))
        if self.position_size < min_size:
            raise ValueError(
                f"Position size {self.position_size} below minimum {min_size} USDT"
            )

        max_size = self.balance * Decimal(str(MAX_POSITION_SIZE_PCT))
        if self.position_size > max_size:
            raise ValueError(
                f"Position size {self.position_size} exceeds {float(MAX_POSITION_SIZE_PCT)*100}% of balance ({max_size} USDT)"
            )

        return self

    @model_validator(mode="after")
    def validate_profit_target(self):
        """
        Valida profit target mínimo:
        - (take_profit - entry_price) / entry_price >= MIN_PROFIT_TARGET (0.8%)
        """
        min_profit_pct = Decimal(
            str(MIN_PROFIT_TARGET)
        )  # Já é fração decimal (0.008 = 0.8%)
        expected_profit_pct = (self.take_profit - self.entry_price) / self.entry_price

        if expected_profit_pct < min_profit_pct:
            raise ValueError(
                f"Profit target {float(expected_profit_pct)*100:.2f}% below minimum {float(min_profit_pct)*100:.2f}%"
            )

        return self

    model_config = ConfigDict(
        frozen=True,
        json_encoders={Decimal: str},
    )


class PositionData(BaseModel):
    """
    Dados completos de uma posição (open ou closed).

    Usado para representar estado de posição no database/state.
    """

    symbol: str = Field(..., description="Trading pair symbol")
    status: PositionStatus = Field(..., description="Position status")
    entry_price: Decimal = Field(..., gt=0, description="Entry price")
    current_price: Decimal | None = Field(None, gt=0, description="Current price")
    quantity: Decimal = Field(..., gt=0, description="Position quantity")
    position_size: Decimal = Field(..., gt=0, description="Position size in USDT")
    stop_loss: Decimal = Field(..., gt=0, description="Stop loss price")
    take_profit: Decimal = Field(..., gt=0, description="Take profit price")
    pnl_usd: Decimal | None = Field(None, description="P&L in USDT")
    pnl_pct: Decimal | None = Field(None, description="P&L in percentage")
    entry_time: datetime = Field(..., description="Position entry timestamp")
    exit_time: datetime | None = Field(None, description="Position exit timestamp")
    exit_reason: str | None = Field(None, description="Exit reason if closed")

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        """Valida formato do symbol"""
        if not v or not isinstance(v, str):
            raise ValueError("Symbol must be non-empty string")
        return v.upper()

    @model_validator(mode="after")
    def validate_closed_position(self):
        """
        Valida que posições CLOSED têm campos obrigatórios:
        - exit_time
        - exit_reason
        - pnl_usd
        - pnl_pct
        """
        if self.status == "CLOSED":
            if self.exit_time is None:
                raise ValueError("Closed position must have exit_time")
            if self.exit_reason is None:
                raise ValueError("Closed position must have exit_reason")
            if self.pnl_usd is None:
                raise ValueError("Closed position must have pnl_usd")
            if self.pnl_pct is None:
                raise ValueError("Closed position must have pnl_pct")

        return self

    model_config = ConfigDict(
        json_encoders={Decimal: str, datetime: lambda v: v.isoformat()},
    )
