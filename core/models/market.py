from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MarketDataParams(BaseModel):
    """Parâmetros de dados de mercado com validação automática"""

    model_config = ConfigDict(frozen=True)

    price: Decimal = Field(gt=0, description="Current market price")
    volume: Decimal = Field(ge=0, description="Trading volume")

    @field_validator("price", "volume")
    @classmethod
    def validate_positive_decimal(cls, v: Decimal) -> Decimal:
        """Valida que valores são positivos"""
        if v <= 0 and cls.model_fields.get("price"):
            raise ValueError("Price must be positive")
        return v


class TickerDataParams(BaseModel):
    """Parâmetros de ticker com validação automática"""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    symbol: str = Field(min_length=1, description="Trading pair symbol")
    bid_price: Decimal = Field(gt=0, description="Best bid price", alias="bidPrice")
    ask_price: Decimal = Field(gt=0, description="Best ask price", alias="askPrice")
    volume: Decimal = Field(ge=0, description="24h trading volume")

    @field_validator("ask_price")
    @classmethod
    def validate_ask_price(cls, v: Decimal, info) -> Decimal:
        """Ask price deve ser maior que bid price"""
        if "bid_price" in info.data:
            bid_price = info.data["bid_price"]
            if v <= bid_price:
                raise ValueError("Ask price must be greater than bid price")
        return v


class OrderBookParams(BaseModel):
    """Parâmetros de order book com validação automática"""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    bids: list = Field(min_length=1, description="List of bid orders")
    asks: list = Field(min_length=1, description="List of ask orders")

    @field_validator("bids", "asks")
    @classmethod
    def validate_orders(cls, v: list) -> list:
        """Valida que há pelo menos uma ordem"""
        if not v or len(v) == 0:
            raise ValueError("Order list cannot be empty")
        return v

    def validate_spread(self) -> bool:
        """Valida que best bid < best ask"""
        try:
            best_bid = (
                float(self.bids[0][0])
                if isinstance(self.bids[0], (list, tuple))
                else float(self.bids[0])
            )
            best_ask = (
                float(self.asks[0][0])
                if isinstance(self.asks[0], (list, tuple))
                else float(self.asks[0])
            )
            return best_bid < best_ask
        except (IndexError, ValueError, TypeError):
            return False
