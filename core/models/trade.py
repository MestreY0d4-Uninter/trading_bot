"""
Pydantic models para validação de trades executados.

Representa resultado de execução de ordem na exchange.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

OrderStatus = Literal[
    "NEW",
    "PARTIALLY_FILLED",
    "FILLED",
    "CANCELED",
    "REJECTED",
    "EXPIRED",
]


class TradeData(BaseModel):
    """
    Dados de um trade executado (fill de ordem).

    Representa 1 execução individual na exchange.
    Baseado em Binance Trade response.
    """

    trade_id: int = Field(..., description="Binance trade ID")
    order_id: int = Field(..., description="Binance order ID")
    symbol: str = Field(..., description="Trading pair symbol")
    side: Literal["BUY", "SELL"] = Field(..., description="Trade side")
    price: Decimal = Field(..., gt=0, description="Execution price")
    quantity: Decimal = Field(..., gt=0, description="Executed quantity")
    commission: Decimal = Field(..., ge=0, description="Commission paid")
    commission_asset: str = Field(..., description="Commission asset (ex: BNB)")
    timestamp: datetime = Field(..., description="Trade execution timestamp")

    @field_validator("symbol", "commission_asset")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        """Valida formato do symbol/asset"""
        if not v or not isinstance(v, str):
            raise ValueError("Symbol/asset must be non-empty string")
        return v.upper()

    @field_validator("price", "quantity", "commission")
    @classmethod
    def validate_decimal_precision(cls, v: Decimal) -> Decimal:
        """Valida precisão decimal (max 8 casas decimais)"""
        exponent = v.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -8:
            raise ValueError("Decimal precision exceeds 8 decimals")
        return v

    def calculate_notional(self) -> Decimal:
        """Calcula valor notional do trade (price * quantity)"""
        return self.price * self.quantity

    model_config = ConfigDict(
        frozen=True,
        json_encoders={Decimal: str, datetime: lambda v: v.isoformat()},
    )


class TradeResult(BaseModel):
    """
    Resultado completo de execução de ordem.

    Agrupa múltiplos trades (fills) + metadata de execução.
    Baseado em Binance Order response.
    """

    order_id: int = Field(..., description="Binance order ID")
    client_order_id: str = Field(..., description="Client order ID (idempotency)")
    symbol: str = Field(..., description="Trading pair symbol")
    status: OrderStatus = Field(..., description="Order status")
    side: Literal["BUY", "SELL"] = Field(..., description="Order side")
    order_type: str = Field(..., description="Order type")
    price: Decimal | None = Field(None, gt=0, description="Order price (LIMIT)")
    quantity: Decimal = Field(..., gt=0, description="Original quantity")
    executed_quantity: Decimal = Field(..., ge=0, description="Executed quantity")
    cumulative_quote_qty: Decimal = Field(
        ..., ge=0, description="Cumulative quote quantity (USDT spent/received)"
    )
    avg_price: Decimal | None = Field(None, ge=0, description="Average execution price")
    timestamp: datetime = Field(..., description="Order timestamp")
    trades: list[TradeData] = Field(
        default_factory=list, description="Individual trades (fills)"
    )

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        """Valida formato do symbol"""
        if not v or not isinstance(v, str):
            raise ValueError("Symbol must be non-empty string")
        return v.upper()

    @field_validator("client_order_id")
    @classmethod
    def validate_client_order_id(cls, v: str) -> str:
        """Valida que client_order_id não é vazio (idempotency key)"""
        if not v or not isinstance(v, str):
            raise ValueError("Client order ID must be non-empty (idempotency)")
        return v

    def is_fully_filled(self) -> bool:
        """Verifica se ordem foi 100% executada"""
        return self.status == "FILLED"

    def is_rejected(self) -> bool:
        """Verifica se ordem foi rejeitada"""
        return self.status == "REJECTED"

    def calculate_total_commission(self) -> Decimal:
        """Calcula comissão total de todos os trades"""
        return sum((trade.commission for trade in self.trades), start=Decimal("0"))

    model_config = ConfigDict(
        json_encoders={Decimal: str, datetime: lambda v: v.isoformat()},
    )
