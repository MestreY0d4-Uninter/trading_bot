import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.validators.trading_validator import TradingValidator
from shared.constants import (
    MIN_ENTRY_SCORE,
    MIN_PROFIT_TARGET,
    MIN_VOLATILITY_THRESHOLD,
    MIN_VOLUME_SPIKE,
)
from shared.observability.flow_tracker import track_component
from shared.observability.logger import debug, error, production, warning
from shared.types.state import state
from utils.decimal_math import calculate_pnl_percentage, to_decimal
from utils.validation_utils import is_numeric_valid, validate_symbol


@dataclass
class AnalysisContext:
    symbol: str
    price: Decimal
    volume: Decimal
    volume_usd: Decimal
    spread_pct: Decimal
    market_data: dict
    candles: object
    indicators: dict
    market_condition: object
    entry_score: Decimal


class SignalAnalyzer:
    def __init__(
        self, config: dict, indicators, market_analyzer, scoring_system, risk_manager
    ):
        if not all([config, indicators, market_analyzer, scoring_system, risk_manager]):
            raise ValueError(
                "Todos os componentes são obrigatórios para SignalAnalyzer"
            )

        self.config = config
        self.indicators = indicators
        self.market_analyzer = market_analyzer
        self.scoring_system = scoring_system
        self.risk_manager = risk_manager

        trading_config = self.config.get("trading", {})
        sanity_config = self.config.get("sanity_checks", {})
        risk_config = self.config.get("risk", {})

        self.analysis_interval = trading_config.get("check_interval", 15)
        self.analysis_timeframe = trading_config.get("analysis_timeframe", "5m")
        self.min_volume = sanity_config.get("min_volume_usd", 40000)
        self.max_spread_pct = risk_config.get("max_spread_pct", 0.3)
        self.min_score_threshold = self.config.get("strategy", {}).get(
            "min_entry_score", MIN_ENTRY_SCORE
        )
        self.min_profit_target = self.config.get("strategy", {}).get(
            "min_profit_target", float(MIN_PROFIT_TARGET)
        )

        # Advanced Filters
        advanced_config = self.config.get("advanced_filters", {})
        self.min_volume_spike = Decimal(
            str(advanced_config.get("min_volume_spike", float(MIN_VOLUME_SPIKE)))
        )
        self.momentum_filter = advanced_config.get("momentum_filter", True)
        self.volatility_threshold = Decimal(
            str(
                advanced_config.get(
                    "volatility_threshold", float(MIN_VOLATILITY_THRESHOLD)
                )
            )
        )

        self.is_testnet = self.config.get("mode", "").lower() == "testnet"

        production(
            "Signal Analyzer inicializado",
            analysis_interval=self.analysis_interval,
            analysis_timeframe=self.analysis_timeframe,
            min_volume=self.min_volume,
            min_score_threshold=self.min_score_threshold,
            min_profit_target=self.min_profit_target,
            advanced_filters={
                "min_volume_spike": self.min_volume_spike,
                "momentum_filter": self.momentum_filter,
                "volatility_threshold": self.volatility_threshold,
            },
        )

    @track_component("signal_analyzer", slow_threshold=3)
    async def analyze_symbol(self, symbol: str, data_manager) -> dict | None:
        if not validate_symbol(symbol):
            return None

        try:
            context = await self._build_analysis_context(symbol, data_manager)
            if not context:
                return None

            if not self._validate_spread(context):
                return None

            signal_type = await self._determine_signal_type(
                context.entry_score, symbol, context.price, context.market_data
            )

            if signal_type == "HOLD":
                return None

            return self._build_signal(context, signal_type)

        except TimeoutError:
            warning("Timeout na análise", symbol=symbol)
            return None
        except Exception as e:
            error("Erro na análise de sinal", symbol=symbol, error=str(e))
            return None

    async def _build_analysis_context(
        self, symbol: str, data_manager
    ) -> AnalysisContext | None:
        if not await data_manager.update_comprehensive_market_data(symbol):
            warning("Falha na coleta de dados", symbol=symbol)
            return None

        await asyncio.sleep(0.01)

        market_data = await self._get_market_data_with_retry(symbol)
        if not market_data:
            return None

        price = to_decimal(market_data.get("price"))
        volume = to_decimal(market_data.get("volume", 0))

        if not self._validate_price_volume(symbol, price, volume):
            return None

        candles = await self._fetch_candles(symbol, data_manager)
        if candles is None:
            return None

        indicators = self.indicators.calculate_all(candles)
        if not indicators:
            warning("Falha ao calcular indicadores", symbol=symbol)
            return None

        market_condition = await self.market_analyzer.analyze_market(
            candles, symbol, self.analysis_timeframe
        )

        entry_score = self._calculate_entry_score(
            indicators, market_condition, market_data
        )
        if entry_score is None:
            return None

        volume_usd = price * volume if volume else Decimal("0")
        spread_pct = to_decimal(market_data.get("spread_pct", 0))

        production(
            "📊 Score calculado",
            symbol=symbol,
            entry_score=round(float(entry_score), 2),
            market_condition=self._get_market_condition_value(market_condition),
            spread_pct=round(float(spread_pct), 3),
        )

        return AnalysisContext(
            symbol=symbol,
            price=price,
            volume=volume,
            volume_usd=volume_usd,
            spread_pct=spread_pct,
            market_data=market_data,
            candles=candles,
            indicators=indicators,
            market_condition=market_condition,
            entry_score=entry_score,
        )

    async def _get_market_data_with_retry(self, symbol: str) -> dict | None:
        market_data = await state.get_market_data(symbol)
        if not market_data:
            await asyncio.sleep(0.05)
            market_data = await state.get_market_data(symbol)
        if not market_data:
            warning("Sem dados de mercado", symbol=symbol)
        return market_data

    def _validate_price_volume(
        self, symbol: str, price: Decimal, volume: Decimal
    ) -> bool:
        if not price or price <= 0:
            debug("Preço inválido", symbol=symbol, price=price)
            return False

        volume_usd = price * volume if volume else Decimal("0")

        if not self.is_testnet and volume_usd < self.min_volume:
            debug("Volume insuficiente", symbol=symbol, volume_usd=volume_usd)
            return False

        return True

    async def _fetch_candles(self, symbol: str, data_manager) -> object | None:
        try:
            async with asyncio.timeout(10.0):
                candles = await data_manager.get_candles(
                    symbol, self.analysis_timeframe, limit=100
                )
        except TimeoutError:
            warning("Timeout ao buscar candles", symbol=symbol)
            return None

        if candles is None or candles.empty or len(candles) < 20:
            debug("Dados de velas insuficientes", symbol=symbol)
            return None

        return candles

    def _calculate_entry_score(
        self, indicators: dict, market_condition: object, market_data: dict
    ) -> Decimal | None:
        entry_score_result = self.scoring_system.calculate_score(
            indicators,
            self._get_market_condition_value(market_condition),
            market_data.get("spread_pct", 0),
        )

        entry_score = (
            entry_score_result[0]
            if isinstance(entry_score_result, tuple)
            else entry_score_result
        )

        if entry_score is None or not is_numeric_valid(entry_score):
            warning("Score inválido", score=entry_score)
            return None

        return to_decimal(entry_score)

    def _get_market_condition_value(self, market_condition: object) -> str:
        return (
            market_condition.value
            if hasattr(market_condition, "value")
            else str(market_condition)
        )

    def _validate_spread(self, context: AnalysisContext) -> bool:
        if context.spread_pct > self.max_spread_pct:
            production(
                "⚠️ Spread muito alto - sinal rejeitado",
                symbol=context.symbol,
                spread_pct=round(float(context.spread_pct), 2),
                max_allowed=round(self.max_spread_pct, 2),
            )
            return False
        return True

    def _build_signal(self, context: AnalysisContext, signal_type: str) -> dict:
        signal = {
            "symbol": context.symbol,
            "type": signal_type,
            "entry_score": context.entry_score,
            "score": context.entry_score,
            "current_price": context.price,
            "price": context.price,
            "volume_usd": context.volume_usd,
            "spread_pct": context.spread_pct,
            "timestamp": datetime.now(),
            "market_condition": self._get_market_condition_value(
                context.market_condition
            ),
            "indicators": context.indicators,
        }

        production(
            "🎯 SINAL GERADO",
            symbol=context.symbol,
            type=signal_type,
            score=round(float(context.entry_score), 2),
            price=context.price,
            spread_pct=round(float(context.spread_pct), 3),
            volume_usd=round(float(context.volume_usd), 0),
            market_condition=signal["market_condition"],
        )

        return signal

    async def _determine_signal_type(
        self,
        score: Decimal,
        symbol: str,
        current_price: Decimal,
        market_data: dict | None = None,
    ) -> str:
        position = await state.get_position(symbol)
        if position:
            entry_price = position.get("entry_price", 0)
            if entry_price > 0:
                entry_decimal = (
                    to_decimal(entry_price)
                    if not isinstance(entry_price, Decimal)
                    else entry_price
                )
                current_decimal = (
                    to_decimal(current_price)
                    if not isinstance(current_price, Decimal)
                    else current_price
                )

                profit_pct = calculate_pnl_percentage(entry_decimal, current_decimal)

                take_profit_threshold = to_decimal(
                    self.config.get("strategy", {}).get("take_profit_pct", 2.0)
                )

                if profit_pct >= take_profit_threshold:
                    return "SELL"

        if score >= self.min_score_threshold:
            debug(
                f"✅ {symbol} score {score} >= threshold {self.min_score_threshold}",
            )

            take_profit_pct = self.config.get("strategy", {}).get(
                "take_profit_pct", 1.5
            )
            expected_profit = to_decimal(take_profit_pct) / Decimal("100")

            if expected_profit >= to_decimal(self.min_profit_target):
                debug(
                    f"✅ {symbol} profit target OK ({expected_profit*Decimal('100'):.2f}% >= {to_decimal(self.min_profit_target)*Decimal('100'):.2f}%)",
                )
                debug(
                    f"🔍 ANTES de _apply_advanced_filters para {symbol}",
                )

                if not self._apply_advanced_filters(
                    symbol, current_price, market_data, "BUY"
                ):
                    debug(
                        f"❌ {symbol} REJEITADO por filtros avançados",
                    )
                    return "HOLD"

                debug(
                    f"✅ {symbol} APROVADO por filtros avançados - retornando BUY",
                )
                return "BUY"
            else:
                debug(
                    f"❌ {symbol} profit target insuficiente ({expected_profit*Decimal('100'):.2f}% < {to_decimal(self.min_profit_target)*Decimal('100'):.2f}%)",
                )
                debug(
                    "Profit target insuficiente",
                    symbol=symbol,
                    expected_profit_pct=take_profit_pct,
                    min_required_pct=to_decimal(self.min_profit_target)
                    * Decimal("100"),
                    score=score,
                )
                return "HOLD"
        else:
            debug(
                f"❌ {symbol} score {score} < threshold {self.min_score_threshold}",
            )
            return "HOLD"

    @track_component("signal_analyzer", slow_threshold=10)
    async def bulk_analyze(self, symbols: list, data_manager) -> dict[str, dict | None]:
        if not symbols:
            return {}

        debug("Iniciando análise bulk", symbols_count=len(symbols))

        tasks = [self.analyze_symbol(symbol, data_manager) for symbol in symbols]

        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)

            signals: dict[str, dict | None] = {}
            for i, result in enumerate(results):
                symbol = symbols[i]
                if isinstance(result, Exception):
                    warning("Erro na análise bulk", symbol=symbol, error=str(result))
                    signals[symbol] = None
                else:
                    signals[symbol] = result

            successful = sum(1 for signal in signals.values() if signal is not None)
            debug(
                "Análise bulk concluída",
                total=len(symbols),
                successful=successful,
                failed=len(symbols) - successful,
            )

            return signals

        except Exception as e:
            error("Erro na análise bulk", error=str(e))
            return dict.fromkeys(symbols)

    @track_component("signal_analyzer", slow_threshold=1)
    def validate_signal(self, signal: dict) -> bool:
        if not isinstance(signal, dict):
            return False

        required_fields = ["symbol", "type", "score", "price", "timestamp"]
        if not all(field in signal for field in required_fields):
            return False

        if signal["type"] not in ["BUY", "SELL"]:
            return False

        if not is_numeric_valid(signal["score"]):
            return False

        if not isinstance(signal["price"], (int, float)) or signal["price"] <= 0:
            return False

        return True

    def _apply_advanced_filters(
        self,
        symbol: str,
        current_price: Decimal,
        market_data: dict,
        signal_direction: str,
    ) -> bool:
        if not market_data:
            return True

        if not self._check_volume_filter(symbol, market_data, signal_direction):
            return False

        if not self._check_volatility_filter(
            symbol, current_price, market_data, signal_direction
        ):
            return False

        if not self._check_momentum_filter(
            symbol, current_price, market_data, signal_direction
        ):
            return False

        return True

    def _check_volume_filter(
        self, symbol: str, market_data: dict, direction: str
    ) -> bool:
        current_volume = to_decimal(market_data.get("volume", 0))
        avg_volume = to_decimal(market_data.get("avg_volume", current_volume))

        if current_volume <= 0 or avg_volume <= 0:
            return True

        if not TradingValidator.validate_volume_spike(
            current_volume, avg_volume, self.min_volume_spike
        ):
            spike_ratio = current_volume / avg_volume
            production(
                "🔍 Signal rejected - volume spike insufficient",
                symbol=symbol,
                spike_ratio=f"{float(spike_ratio):.2f}",
                required=f"{float(self.min_volume_spike):.2f}",
            )
            return False
        return True

    def _check_volatility_filter(
        self, symbol: str, current_price: Decimal, market_data: dict, direction: str
    ) -> bool:
        high = to_decimal(market_data.get("high", current_price))
        low = to_decimal(market_data.get("low", current_price))

        if high <= 0 or low <= 0 or high == low:
            return True

        price_range_pct = (high - low) / to_decimal(current_price)
        if not TradingValidator.validate_volatility_threshold(
            price_range_pct, self.volatility_threshold
        ):
            production(
                "🔍 Signal rejected - volatility too low",
                symbol=symbol,
                range_pct=f"{float(price_range_pct * 100):.2f}%",
            )
            return False
        return True

    def _check_momentum_filter(
        self, symbol: str, current_price: Decimal, market_data: dict, direction: str
    ) -> bool:
        if not self.momentum_filter:
            return True

        prev_price = to_decimal(market_data.get("prev_close", current_price))
        current = to_decimal(current_price)

        if prev_price <= 0 or prev_price == current:
            return True

        if not TradingValidator.validate_momentum_direction(
            current, prev_price, direction
        ):
            production(
                "🔍 Signal rejected - momentum mismatch",
                symbol=symbol,
                direction=direction,
            )
            return False
        return True

    @track_component("signal_analyzer")
    def get_status(self) -> dict:
        return {
            "analysis_interval": self.analysis_interval,
            "min_volume": self.min_volume,
            "min_score_threshold": self.min_score_threshold,
            "status": "active",
        }
