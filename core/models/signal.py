from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SignalParams(BaseModel):
    """Parâmetros de sinal de trading com validação automática"""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    symbol: str = Field(min_length=1, description="Trading pair symbol (e.g., BTCUSDT)")
    entry_score: float = Field(ge=0, le=100, description="Entry score (0-100)")
    current_price: Decimal = Field(gt=0, description="Current market price")
    market_condition: str = Field(
        min_length=1, description="Market condition (UPTREND, DOWNTREND, SIDEWAYS)"
    )

    # Campos opcionais
    entry_price: Decimal | None = Field(default=None, gt=0)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)
    components: dict[str, Any] | None = Field(default_factory=dict)
    spread_pct: float | None = Field(default=None, ge=0)
    volume_usd: float | None = Field(default=None, ge=0)

    @field_validator("symbol")
    @classmethod
    def validate_symbol_format(cls, v: str) -> str:
        """Valida e normaliza o símbolo"""
        v = v.upper().strip()
        if not v.endswith("USDT"):
            raise ValueError("Symbol must end with USDT")
        if len(v) < 5:  # Mínimo: XUSDT (5 chars)
            raise ValueError("Symbol too short")
        return v

    @field_validator("market_condition")
    @classmethod
    def validate_market_condition(cls, v: str) -> str:
        """Valida condição de mercado"""
        v = v.upper().strip()
        valid_conditions = ["UPTREND", "DOWNTREND", "SIDEWAYS"]
        if v not in valid_conditions:
            if v == "UNKNOWN":
                raise ValueError(
                    "Market condition cannot be unknown - need valid market analysis"
                )
            raise ValueError(
                f"Invalid market condition. Must be one of: {', '.join(valid_conditions)}"
            )
        return v

    @field_validator("current_price", "entry_price", "stop_loss", "take_profit")
    @classmethod
    def validate_positive_decimal(cls, v: Decimal | None) -> Decimal | None:
        """Valida que decimais são positivos"""
        if v is not None and v <= 0:
            raise ValueError("Price must be positive")
        return v


class TradingSignalParams(BaseModel):
    """Parâmetros completos para validação de entrada em posição"""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    signal: SignalParams
    usdt_balance: Decimal = Field(gt=0, description="Available USDT balance")

    @field_validator("symbol")
    @classmethod
    def validate_symbol_format(cls, v: str) -> str:
        """Valida e normaliza o símbolo"""
        return v.upper().strip()


class StopLossTakeProfitParams(BaseModel):
    """Valida relação entre entry price, stop loss e take profit"""

    model_config = ConfigDict(frozen=True)

    entry_price: Decimal = Field(gt=0)
    stop_loss: Decimal = Field(gt=0)
    take_profit: Decimal = Field(gt=0)

    @field_validator("stop_loss")
    @classmethod
    def validate_stop_loss(cls, v: Decimal, info) -> Decimal:
        """Stop loss deve estar abaixo do entry price"""
        if "entry_price" in info.data:
            entry_price = info.data["entry_price"]
            if v >= entry_price:
                raise ValueError("Stop loss must be below entry price")
        return v

    @field_validator("take_profit")
    @classmethod
    def validate_take_profit(cls, v: Decimal, info) -> Decimal:
        """Take profit deve estar acima do entry price"""
        if "entry_price" in info.data:
            entry_price = info.data["entry_price"]
            if v <= entry_price:
                raise ValueError("Take profit must be above entry price")
        return v
