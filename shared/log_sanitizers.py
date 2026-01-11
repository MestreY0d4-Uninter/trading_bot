import re
from typing import Any


class LogSanitizer:
    _SENSITIVE_PATTERNS = frozenset(
        [
            re.compile(r"api[_\s]*key", re.IGNORECASE),
            re.compile(r"api[_\s]*secret", re.IGNORECASE),
            re.compile(r"password", re.IGNORECASE),
            re.compile(r"token", re.IGNORECASE),
            re.compile(r"secret", re.IGNORECASE),
            re.compile(r"auth", re.IGNORECASE),
            re.compile(r"private[_\s]*key", re.IGNORECASE),
            re.compile(r"access[_\s]*token", re.IGNORECASE),
            re.compile(r"refresh[_\s]*token", re.IGNORECASE),
            re.compile(r"bearer", re.IGNORECASE),
            re.compile(r"credential", re.IGNORECASE),
        ]
    )

    _CREDIT_CARD_PATTERN = re.compile(r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b")
    _API_KEY_PATTERN = re.compile(r"\b[A-Za-z0-9]{32,}\b")
    _HEX_PATTERN = re.compile(r"\b[a-fA-F0-9]{40,}\b")

    _SAFE_FIELDS = frozenset(
        [
            "symbol",
            "side",
            "quantity",
            "price",
            "status",
            "timestamp",
            "level",
            "module",
            "function",
            "line",
            "method",
            "url_path",
            "http_status",
            "latency",
            "count",
            "percentage",
            "profit_loss",
            "balance",
            "equity",
            "margin",
            "leverage",
            "spread",
            "volume",
        ]
    )

    @classmethod
    def sanitize_message(cls, msg: str) -> str:
        if not isinstance(msg, str):
            return str(msg)

        sanitized = msg

        sanitized = cls._CREDIT_CARD_PATTERN.sub("[CARD_****]", sanitized)
        sanitized = cls._API_KEY_PATTERN.sub(
            lambda m: cls._mask_key(m.group(0)), sanitized
        )
        sanitized = cls._HEX_PATTERN.sub(lambda m: cls._mask_key(m.group(0)), sanitized)

        sensitive_terms = [
            "api key",
            "api_key",
            "api secret",
            "api_secret",
            "password",
            "token",
            "secret",
            "auth",
            "private key",
            "access_token",
            "refresh_token",
            "bearer",
            "credential",
        ]

        for term in sensitive_terms:
            pattern = re.compile(rf"{re.escape(term)}\s*[:=]\s*[^\s\]]+", re.IGNORECASE)
            sanitized = pattern.sub(f"{term}=[REDACTED]", sanitized)

        return sanitized

    @classmethod
    def _mask_key(cls, key: str) -> str:
        if len(key) <= 8:
            return "[REDACTED]"
        return f"{key[:4]}***{key[-4:]}"

    @classmethod
    def sanitize_value(cls, key: str, value: Any) -> Any:
        if isinstance(key, str):
            key_lower = key.lower()

            if key_lower in cls._SAFE_FIELDS:
                return value

            if any(pattern.search(key) for pattern in cls._SENSITIVE_PATTERNS):
                if isinstance(value, str) and len(value) > 8:
                    return cls._mask_key(value)
                return "[REDACTED]"

        if isinstance(value, str):
            return cls.sanitize_message(value)

        return value
