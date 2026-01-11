import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from shared.observability.flow_tracker import flow_tracker, track_component
from shared.observability.logger import debug, error, production, warning
from shared.types.state import state
from utils.decimal_math import to_decimal
from utils.validation_utils import is_numeric_valid


@dataclass
class TickerData:
    price: Decimal
    volume: Decimal
    volume_24h_usd: Decimal


@dataclass
class OrderbookData:
    bid: Decimal
    ask: Decimal
    spread_pct: Decimal


class MarketUpdater:
    def __init__(self, coordinator: Any) -> None:
        if not coordinator:
            raise ValueError("Coordinator é obrigatório para MarketUpdater")

        self.coordinator = coordinator
        self.config = coordinator.config
        self.data_manager = coordinator.data_manager

        # Configuration with safe defaults
        market_config = self.config.get("market", {})
        self.update_interval = market_config.get("update_interval", 30)
        self.update_on_startup = market_config.get("update_on_startup", True)

        if self.update_interval < 5:
            self.update_interval = 5
        elif self.update_interval > 300:
            self.update_interval = 300

        production(
            "Market Updater inicializado",
            update_interval=self.update_interval,
            startup_update=self.update_on_startup,
        )

    @track_component("market_updater")
    async def run(self):
        production("Market Updater iniciado")

        trading_pairs = self.config.get("trading_pairs", [])
        if not trading_pairs:
            error("CRÍTICO: trading_pairs não configurado")
            return

        if self.update_on_startup:
            production("Executando atualização inicial")
            await self._update_all_symbols(trading_pairs)

        cycle_count = 0
        while self.coordinator.running and not self.coordinator.shutdown_event.is_set():
            try:
                cycle_count += 1
                # Track component heartbeat no início de cada ciclo
                flow_tracker.force_component_update("market_updater")

                production(
                    f"🔄 Iniciando ciclo #{cycle_count} de market update",
                    symbols_count=len(trading_pairs),
                )

                successful_updates = 0
                for i, symbol in enumerate(trading_pairs, 1):
                    if not self.coordinator.running:
                        production("⏹ Shutdown solicitado, interrompendo market update")
                        break

                    debug(f"Processando symbol {i}/{len(trading_pairs)}: {symbol}")
                    result = await self._update_symbol_data(symbol)
                    if result:
                        successful_updates += 1

                production(
                    f"✅ Ciclo #{cycle_count} concluído",
                    successful=successful_updates,
                    total=len(trading_pairs),
                    next_update_in=f"{self.update_interval}s",
                )

                flow_tracker.force_component_update("market_updater")

                await asyncio.sleep(self.update_interval)

            except Exception as e:
                error(
                    f"Erro crítico no market updater (ciclo #{cycle_count})",
                    error=str(e),
                )
                production(
                    f"⏳ Aguardando {self.update_interval * 2}s antes do próximo ciclo devido ao erro"
                )
                await asyncio.sleep(self.update_interval * 2)

        production("Market Updater encerrado")

    async def _update_all_symbols(self, symbols: list[str]):
        """Update all symbols during startup."""
        production(
            "🚀 Iniciando atualização inicial de mercado", symbols_count=len(symbols)
        )

        successful = 0
        failed = 0

        for i, symbol in enumerate(symbols, 1):
            try:
                production(f" Atualizando {i}/{len(symbols)}: {symbol}")
                result = await self._update_symbol_data(symbol)
                if result:
                    successful += 1
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                error(
                    "Erro crítico ao atualizar símbolo na inicialização",
                    symbol=symbol,
                    error=str(e),
                )

        production(
            "✅ Atualização inicial concluída",
            successful=successful,
            failed=failed,
            total=len(symbols),
        )

    async def _update_symbol_data(self, symbol: str) -> bool:
        """Update market data for a single symbol."""
        if not self._validate_data_manager():
            warning("Client não configurado no data_manager", symbol=symbol)
            return False

        ticker_data = await self._fetch_ticker_data(symbol)
        if not ticker_data:
            return False

        await self._save_ticker_to_state(symbol, ticker_data)

        orderbook_data = await self._fetch_orderbook_data(symbol)
        if orderbook_data:
            await self._save_orderbook_to_state(symbol, orderbook_data)

        return True

    def _validate_data_manager(self) -> bool:
        return (
            hasattr(self.data_manager, "client")
            and self.data_manager.client is not None
        )

    async def _fetch_ticker_data(self, symbol: str) -> TickerData | None:
        timeout_val = self.config.get("data", {}).get("ticker_24hr_timeout", 5)
        try:
            async with asyncio.timeout(timeout_val):
                ticker = await self.data_manager.fetch_24hr_ticker(symbol)
        except TimeoutError:
            error(f"Timeout ao buscar ticker para {symbol}", timeout=timeout_val)
            return None
        except Exception as e:
            error(f"Erro ao buscar ticker para {symbol}", error=str(e))
            return None

        return self._parse_ticker(symbol, ticker)

    def _parse_ticker(self, symbol: str, ticker: dict | None) -> TickerData | None:
        if not ticker:
            warning("Ticker vazio", symbol=symbol)
            return None

        if "price" not in ticker and "lastPrice" not in ticker:
            warning("Ticker sem preço", symbol=symbol)
            return None

        price = to_decimal(ticker.get("price", ticker.get("lastPrice", 0)))
        if not is_numeric_valid(price) or price <= 0:
            warning("Preço inválido", symbol=symbol, price=price)
            return None

        volume = to_decimal(ticker.get("volume", 0))
        volume_24h_usd = to_decimal(ticker.get("quoteVolume", 0))

        if not is_numeric_valid(volume):
            volume = Decimal("0")
        if not is_numeric_valid(volume_24h_usd):
            volume_24h_usd = Decimal("0")

        return TickerData(price=price, volume=volume, volume_24h_usd=volume_24h_usd)

    async def _save_ticker_to_state(self, symbol: str, data: TickerData) -> None:
        market_data = await state.get_market_data(symbol) or {}
        market_data.update(
            {
                "price": data.price,
                "volume": data.volume,
                "volume_24h_usd": data.volume_24h_usd,
                "last_update": datetime.now(),
            }
        )
        await state.update_market_data(symbol, market_data)
        debug("Ticker salvo", symbol=symbol, price=data.price)

    async def _fetch_orderbook_data(self, symbol: str) -> OrderbookData | None:
        try:
            orderbook = await self.data_manager.fetch_orderbook(symbol)
        except TimeoutError:
            timeout_val = self.config.get("data", {}).get("orderbook_timeout", 2)
            warning(f"Timeout ao buscar orderbook para {symbol}", timeout=timeout_val)
            return None
        except Exception as e:
            warning(f"Erro ao buscar orderbook para {symbol}", error=str(e))
            return None

        return self._parse_orderbook(symbol, orderbook)

    def _parse_orderbook(
        self, symbol: str, orderbook: dict | None
    ) -> OrderbookData | None:
        if not orderbook:
            warning("Orderbook vazio", symbol=symbol)
            return None

        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])

        if not bids or not asks:
            warning("Orderbook sem bids/asks", symbol=symbol)
            return None

        bid = to_decimal(bids[0][0])
        ask = to_decimal(asks[0][0])

        if not self._validate_bid_ask(bid, ask):
            warning("Bid/ask inválidos", symbol=symbol, bid=bid, ask=ask)
            return None

        spread_pct = ((ask - bid) / bid) * 100 if bid > 0 else Decimal("0")
        return OrderbookData(bid=bid, ask=ask, spread_pct=spread_pct)

    def _validate_bid_ask(self, bid: Decimal, ask: Decimal) -> bool:
        return (
            is_numeric_valid(bid)
            and bid > 0
            and is_numeric_valid(ask)
            and ask > 0
            and ask >= bid
        )

    async def _save_orderbook_to_state(self, symbol: str, data: OrderbookData) -> None:
        market_data = await state.get_market_data(symbol) or {}
        market_data.update(
            {"bid": data.bid, "ask": data.ask, "spread_pct": data.spread_pct}
        )
        await state.update_market_data(symbol, market_data)
        production(
            f"📊 {symbol} atualizado",
            price=market_data.get("price", 0),
            bid=data.bid,
            ask=data.ask,
            spread=f"{data.spread_pct:.4f}%",
        )

    @track_component("market_updater", slow_threshold=10)
    async def force_update(self, symbols: list[str] | None = None) -> dict:
        """Force immediate update of market data."""
        if symbols is None:
            symbols = self.config.get("trading_pairs", [])

        if not symbols:
            return {"successful": 0, "failed": 0, "total": 0}

        production("Executando force update", symbols_count=len(symbols))

        successful = 0
        failed = 0

        for symbol in symbols:
            try:
                result = await self._update_symbol_data(symbol)
                if result:
                    successful += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

        production(
            "Force update concluído",
            successful=successful,
            failed=failed,
            total=len(symbols),
        )

        return {"successful": successful, "failed": failed, "total": len(symbols)}
