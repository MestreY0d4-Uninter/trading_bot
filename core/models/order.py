from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

OrderType = Literal[
    "MARKET",
    "LIMIT",
    "STOP_LOSS",
    "STOP_LOSS_LIMIT",
    "TAKE_PROFIT",
    "TAKE_PROFIT_LIMIT",
]

Side = Literal["BUY", "SELL"]

TimeInForce = Literal["GTC", "IOC", "FOK"]


class OrderParams(BaseModel):
    symbol: str = Field(
        ..., min_length=1, description="Trading pair symbol (ex: BTCUSDT)"
    )
    side: Side = Field(..., description="Order side: BUY or SELL")
    order_type: OrderType = Field(..., description="Order type")
    quantity: Decimal = Field(..., gt=0, description="Order quantity (must be > 0)")
    price: Decimal | None = Field(
        None, gt=0, description="Order price (required for LIMIT orders)"
    )
    time_in_force: TimeInForce = Field(
        "GTC", description="Time in force: GTC, IOC, FOK"
    )

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        if not v or not isinstance(v, str):
            raise ValueError("Symbol must be non-empty string")
        return v.upper()

    @field_validator("quantity", "price")
    @classmethod
    def validate_decimal_precision(cls, v: Decimal | None) -> Decimal | None:
        if v is None:
            return v

        exponent = v.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -8:
            raise ValueError("Decimal precision exceeds 8 decimals")

        return v

    @model_validator(mode="after")
    def validate_price_for_limit_orders(self):
        if self.order_type in ["LIMIT", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"]:
            if self.price is None or self.price <= 0:
                raise ValueError(f"Price is required for {self.order_type} orders")
        return self

    @model_validator(mode="after")
    def validate_min_notional(self):
        min_notional = Decimal("10.0")

        if self.price is not None:
            notional = self.quantity * self.price
            if notional < min_notional:
                raise ValueError(
                    f"Order notional {notional} USDT below minimum {min_notional} USDT"
                )

        return self

    model_config = ConfigDict(
        frozen=True,  # Immutable após criação
        json_encoders={Decimal: str},
    )


class OCOOrderParams(BaseModel):
    symbol: str = Field(..., min_length=1, description="Trading pair symbol")
    quantity: Decimal = Field(..., gt=0, description="Order quantity")
    price: Decimal = Field(..., gt=0, description="Limit order price (take profit)")
    stop_price: Decimal = Field(..., gt=0, description="Stop loss trigger price")
    stop_limit_price: Decimal = Field(..., gt=0, description="Stop loss limit price")
    time_in_force: TimeInForce = Field("GTC", description="Time in force")

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        if not v or not isinstance(v, str):
            raise ValueError("Symbol must be non-empty string")
        return v.upper()

    @field_validator("quantity", "price", "stop_price", "stop_limit_price")
    @classmethod
    def validate_decimal_precision(cls, v: Decimal) -> Decimal:
        exponent = v.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -8:
            raise ValueError("Decimal precision exceeds 8 decimals")
        return v

    @model_validator(mode="after")
    def validate_oco_constraints(self):
        if self.stop_price >= self.price:
            raise ValueError(
                f"Stop price {self.stop_price} must be below limit price {self.price}"
            )

        if self.stop_limit_price > self.stop_price:
            raise ValueError(
                f"Stop limit price {self.stop_limit_price} must be <= stop price {self.stop_price}"
            )

        return self

    @model_validator(mode="after")
    def validate_min_notional(self):
        min_notional = Decimal("10.0")

        notional = self.quantity * self.price
        if notional < min_notional:
            raise ValueError(
                f"Order notional {notional} USDT below minimum {min_notional} USDT"
            )

        return self

    model_config = ConfigDict(
        frozen=True,
        json_encoders={Decimal: str},
    )
