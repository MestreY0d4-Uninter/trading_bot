from datetime import datetime
from typing import Any

import pandas as pd
from pydantic import ValidationError

from core.models.market import MarketDataParams, OrderBookParams, TickerDataParams
from core.validators.exceptions import MarketValidationError, format_pydantic_errors
from shared.observability.flow_tracker import track_component


class MarketValidator:
    """Validator slim usando Pydantic para validação type-safe"""

    @staticmethod
    @track_component("market_validator", slow_threshold=1)
    def validate_market_data(data: dict[str, Any]) -> tuple[bool, str | None]:
        if not isinstance(data, dict):
            return False, "Market data must be a dictionary"
        try:
            MarketDataParams(**data)
            return True, None
        except ValidationError as e:
            return False, format_pydantic_errors(e)
        except Exception as e:
            return False, f"Market data validation failed: {str(e)}"

    @staticmethod
    @track_component("market_validator", slow_threshold=2)
    def validate_candles(
        candles: pd.DataFrame, min_count: int = 20
    ) -> tuple[bool, str | None]:
        if candles is None:
            return False, "Candles data is None"
        if not isinstance(candles, pd.DataFrame):
            return False, "Candles must be a DataFrame"
        if candles.empty:
            return False, "Candles DataFrame is empty"
        if len(candles) < min_count:
            return False, f"Insufficient candles: {len(candles)} < {min_count}"
        required_columns = ["close", "high", "low", "volume"]
        missing = [col for col in required_columns if col not in candles.columns]
        if missing:
            return False, f"Missing columns: {missing}"
        if candles["close"].isna().any():
            return False, "Candles contain NaN values in close prices"
        if (candles["close"] <= 0).any():
            return False, "Candles contain non-positive close prices"
        return True, None

    @staticmethod
    @track_component("market_validator", slow_threshold=1)
    def validate_ticker_data(ticker: dict[str, Any]) -> tuple[bool, str | None]:
        if not isinstance(ticker, dict):
            return False, "Ticker must be a dictionary"
        try:
            TickerDataParams(**ticker)
            return True, None
        except ValidationError as e:
            return False, format_pydantic_errors(e)
        except Exception as e:
            return False, f"Ticker validation failed: {str(e)}"

    @staticmethod
    @track_component("market_validator", slow_threshold=1)
    def validate_order_book(order_book: dict[str, Any]) -> tuple[bool, str | None]:
        if not isinstance(order_book, dict):
            return False, "Order book must be a dictionary"
        if "bids" not in order_book or "asks" not in order_book:
            return False, "Missing bids or asks in order book"
        try:
            book_params = OrderBookParams(**order_book)
            if not book_params.validate_spread():
                return False, "Best bid >= best ask"
            return True, None
        except ValidationError as e:
            return False, format_pydantic_errors(e)
        except Exception as e:
            return False, f"Order book validation failed: {str(e)}"

    @staticmethod
    def is_data_stale(timestamp: datetime, max_age_seconds: int = 60) -> bool:
        if not isinstance(timestamp, datetime):
            return True
        age = (datetime.now() - timestamp).total_seconds()
        return age > max_age_seconds

    @staticmethod
    @track_component("market_validator", slow_threshold=1)
    def validate_market_data_or_raise(data: dict[str, Any]) -> None:
        is_valid, error_msg = MarketValidator.validate_market_data(data)
        if not is_valid:
            raise MarketValidationError(error_msg or "Market data validation failed")

    @staticmethod
    @track_component("market_validator", slow_threshold=2)
    def validate_candles_or_raise(candles: pd.DataFrame, min_count: int = 20) -> None:
        is_valid, error_msg = MarketValidator.validate_candles(candles, min_count)
        if not is_valid:
            raise MarketValidationError(error_msg or "Candles validation failed")

    @staticmethod
    @track_component("market_validator", slow_threshold=1)
    def validate_ticker_data_or_raise(ticker: dict[str, Any]) -> None:
        is_valid, error_msg = MarketValidator.validate_ticker_data(ticker)
        if not is_valid:
            raise MarketValidationError(error_msg or "Ticker data validation failed")

    @staticmethod
    @track_component("market_validator", slow_threshold=1)
    def validate_order_book_or_raise(order_book: dict[str, Any]) -> None:
        is_valid, error_msg = MarketValidator.validate_order_book(order_book)
        if not is_valid:
            raise MarketValidationError(error_msg or "Order book validation failed")
