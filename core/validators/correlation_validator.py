from shared.observability.flow_tracker import track_component
from shared.observability.logger import debug, error, warning
from shared.types.state import state


class CorrelationValidator:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.correlation_config = config.get("correlation_management", {})
        self.enabled = self.correlation_config.get("enabled", False)

        if not self.enabled:
            return

        self.max_btc_pairs = self.correlation_config.get("max_btc_pairs", 2)
        self.max_eth_ecosystem = self.correlation_config.get("max_eth_ecosystem", 2)
        self.max_defi_pairs = self.correlation_config.get("max_defi_pairs", 2)
        self.correlation_threshold = self.correlation_config.get(
            "correlation_threshold", 0.7
        )

        ecosystem_pairs = self.correlation_config.get("ecosystem_pairs", {})
        self.btc_related = set(ecosystem_pairs.get("btc_related", ["BTCUSDT"]))
        self.eth_ecosystem = set(
            ecosystem_pairs.get("eth_ecosystem", ["ETHUSDT", "MATICUSDT", "LINKUSDT"])
        )
        self.defi_pairs = set(
            ecosystem_pairs.get("defi_pairs", ["UNIUSDT", "LINKUSDT", "ATOMUSDT"])
        )

        debug(
            "CorrelationValidator inicializado",
            enabled=self.enabled,
            max_btc=self.max_btc_pairs,
            max_eth=self.max_eth_ecosystem,
            max_defi=self.max_defi_pairs,
        )

    @track_component("correlation_validator", slow_threshold=2)
    async def can_open_position(self, symbol: str) -> tuple[bool, str]:
        if not self.enabled:
            return True, "Correlation management disabled"

        try:
            current_positions = await self._get_current_positions()

            ecosystem_checks = [
                (self.btc_related, self.max_btc_pairs, "BTC ecosystem"),
                (self.eth_ecosystem, self.max_eth_ecosystem, "ETH ecosystem"),
                (self.defi_pairs, self.max_defi_pairs, "DeFi pairs"),
            ]

            for ecosystem, max_count, name in ecosystem_checks:
                if symbol in ecosystem:
                    count = sum(1 for pos in current_positions if pos in ecosystem)
                    if count >= max_count:
                        return False, f"{name} limit reached ({count}/{max_count})"

            return True, "Correlation checks passed"

        except Exception as e:
            error("CRITICAL: Validator failed", symbol=symbol, error=str(e))
            return False, f"Blocked for safety: {str(e)}"

    async def _get_current_positions(self) -> set[str]:
        try:
            # CORREÇÃO: Usar método async nativo (Python 3.14 best practice)
            positions = await state.get_active_positions()

            return {
                pos["symbol"]
                for pos in positions.values()
                if pos.get("status") == "active"
            }
        except Exception as e:
            warning("Erro ao obter posições ativas para correlação", error=str(e))
            return set()

    @track_component("correlation_validator", slow_threshold=1)
    async def get_correlation_summary(self) -> dict:
        if not self.enabled:
            return {"enabled": False}

        current_positions = await self._get_current_positions()

        btc_count = sum(1 for pos in current_positions if pos in self.btc_related)
        eth_count = sum(1 for pos in current_positions if pos in self.eth_ecosystem)
        defi_count = sum(1 for pos in current_positions if pos in self.defi_pairs)

        return {
            "enabled": True,
            "current_exposure": {
                "btc_ecosystem": f"{btc_count}/{self.max_btc_pairs}",
                "eth_ecosystem": f"{eth_count}/{self.max_eth_ecosystem}",
                "defi_pairs": f"{defi_count}/{self.max_defi_pairs}",
            },
            "active_positions": list(current_positions),
        }

    @track_component("correlation_validator", slow_threshold=3)
    async def validate_portfolio_correlation(self) -> tuple[bool, list[str]]:
        if not self.enabled:
            return True, []

        issues = []
        current_positions = await self._get_current_positions()

        # Check all limits
        btc_count = sum(1 for pos in current_positions if pos in self.btc_related)
        if btc_count > self.max_btc_pairs:
            issues.append(
                f"BTC ecosystem overexposed: {btc_count}/{self.max_btc_pairs}"
            )

        eth_count = sum(1 for pos in current_positions if pos in self.eth_ecosystem)
        if eth_count > self.max_eth_ecosystem:
            issues.append(
                f"ETH ecosystem overexposed: {eth_count}/{self.max_eth_ecosystem}"
            )

        defi_count = sum(1 for pos in current_positions if pos in self.defi_pairs)
        if defi_count > self.max_defi_pairs:
            issues.append(f"DeFi pairs overexposed: {defi_count}/{self.max_defi_pairs}")

        return len(issues) == 0, issues
