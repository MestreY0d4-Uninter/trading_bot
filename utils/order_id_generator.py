"""
Order ID Generator

Generates unique client order IDs for Binance API.
Follows Binance requirements:
- Max 36 characters
- Alphanumeric + dash/underscore only
- Unique per order

Format: {side}-{symbol}-{timestamp_ms}-{random_suffix}
Example: buy-BTCUSDT-1700000000000-a1b2
"""

import time
import uuid
from typing import Literal


def generate_client_order_id(
    side: Literal["buy", "sell", "tp", "sl", "oco"],
    symbol: str,
) -> str:
    """
    Generate unique client order ID.

    Args:
        side: Order side ('buy', 'sell', 'tp', 'sl', 'oco')
        symbol: Trading pair symbol (e.g., 'BTCUSDT')

    Returns:
        Unique client order ID (max 36 chars)

    Example:
        >>> generate_client_order_id('buy', 'BTCUSDT')
        'buy-BTCUSDT-1700000000000-a1b2'
    """
    # Validate inputs
    if side not in ["buy", "sell", "tp", "sl", "oco"]:
        raise ValueError(
            f"Invalid side: {side}. Must be one of: buy, sell, tp, sl, oco"
        )

    if not symbol or not isinstance(symbol, str):
        raise ValueError(f"Invalid symbol: {symbol}")

    # Generate components
    timestamp_ms = int(time.time() * 1000)
    random_suffix = str(uuid.uuid4())[:8]  # 8 chars

    # Calculate max symbol length
    # Format: {side}-{symbol}-{timestamp}-{random}
    # Max 36: buy(3) + -(1) + symbol(?) + -(1) + timestamp(13) + -(1) + random(8) = 27 + symbol
    # So: symbol <= 9 chars to fit in 36 total
    max_symbol_len = 13
    symbol_part = symbol[:max_symbol_len]

    # Build ID
    client_order_id = f"{side}-{symbol_part}-{timestamp_ms}-{random_suffix}"

    # Safety check
    if len(client_order_id) > 36:
        # Truncate symbol more if needed
        excess = len(client_order_id) - 36
        symbol_part = symbol_part[: len(symbol_part) - excess]
        client_order_id = f"{side}-{symbol_part}-{timestamp_ms}-{random_suffix}"

    return client_order_id


def validate_client_order_id(client_order_id: str) -> bool:
    """
    Validate client order ID format.

    Args:
        client_order_id: ID to validate

    Returns:
        True if valid, False otherwise

    Rules:
        - Max 36 characters
        - Only alphanumeric, dash, underscore
        - Not empty
    """
    if not client_order_id:
        return False

    if len(client_order_id) > 36:
        return False

    # Check characters (alphanumeric + dash + underscore)
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    if not all(c in allowed for c in client_order_id):
        return False

    return True
