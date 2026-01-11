from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SymbolData(BaseModel):
    symbol: str
    price: str
    timestamp: str
    volume: str | None = None
    spread: str | None = None
    last_update: str | None = None


class CooldownInfo(BaseModel):
    symbol: str
    active: bool
    remaining_seconds: int | None = None
    reason: str | None = None


class MarketSnapshot(BaseModel):
    timestamp: str
    total_symbols: int
    active_symbols: list[str]
    symbols_data: list[SymbolData]
    cooldowns: list[CooldownInfo]
    summary: dict[str, Any]

    @classmethod
    def create(
        cls,
        symbols_data: list[dict[str, Any]],
        cooldowns: list[dict[str, Any]],
        active_symbols: list[str],
    ) -> MarketSnapshot:
        from datetime import datetime

        symbol_list = [SymbolData(**s) for s in symbols_data]
        cooldown_list = [CooldownInfo(**c) for c in cooldowns]

        return cls(
            timestamp=datetime.now().isoformat(),
            total_symbols=len(symbol_list),
            active_symbols=active_symbols,
            symbols_data=symbol_list,
            cooldowns=cooldown_list,
            summary={
                "total_symbols": len(symbol_list),
                "symbols_with_cooldown": len(cooldown_list),
                "available_for_trading": len(active_symbols),
            },
        )


class LogSearchResult(BaseModel):
    timestamp: str
    level: str
    event: str
    module: str | None = None
    additional_fields: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_log_entry(cls, entry: dict[str, Any]) -> LogSearchResult:
        base_fields = {"timestamp", "level", "event", "module"}
        additional = {k: v for k, v in entry.items() if k not in base_fields}

        return cls(
            timestamp=entry.get("timestamp", ""),
            level=entry.get("level", ""),
            event=entry.get("event", ""),
            module=entry.get("module"),
            additional_fields=additional,
        )


class LogSearchResponse(BaseModel):
    query: str
    level_filter: str | None = None
    hours_searched: int
    total_results: int
    results: list[LogSearchResult]

    @classmethod
    def create(
        cls, query: str, level: str | None, hours: int, entries: list[dict[str, Any]]
    ) -> LogSearchResponse:
        results = [LogSearchResult.from_log_entry(e) for e in entries]

        return cls(
            query=query,
            level_filter=level,
            hours_searched=hours,
            total_results=len(results),
            results=results,
        )
