from typing import Any

from shared.constants import MIN_ENTRY_SCORE
from shared.observability.logger import error, warning

VALIDATION_RULES = {
    "min_entry_score": {
        "type": "numeric_range",
        "min_value": 5,
        "max_value": 100,
        "auto_adjust": True,
        "warning_low": "min_entry_score muito baixo, ajustado para {min_value}",
        "warning_high": "min_entry_score muito alto, ajustado para {max_value}",
    },
    "cooldown_seconds": {
        "type": "numeric_minimum",
        "min_value": 60,
        "auto_adjust": True,
        "warning": "cooldown_seconds muito baixo, ajustado para {min_value}",
    },
    "position_size_pct": {
        "type": "risk_threshold",
        "thresholds": [
            {
                "value": 50,
                "level": "warning",
                "message": "⚠️ Position size > 50% é muito arriscado!",
            }
        ],
    },
    "daily_loss_limit_pct": {
        "type": "risk_threshold",
        "thresholds": [
            {
                "value": 20,
                "level": "warning",
                "message": "⚠️ Daily loss limit > 20% é muito alto!",
            }
        ],
    },
    "stop_loss_pct": {
        "type": "risk_threshold",
        "thresholds": [
            {
                "value": 5,
                "level": "warning",
                "message": "⚠️ Stop loss > 5% é muito permissivo!",
            },
            {
                "value": 0.5,
                "level": "warning",
                "message": "⚠️ Stop loss < 0.5% pode resultar em muitas paradas falsas",
                "direction": "below",
            },
        ],
    },
    "max_consecutive_losses": {
        "type": "risk_threshold",
        "thresholds": [
            {
                "value": 5,
                "level": "warning",
                "message": "⚠️ Max consecutive losses > 5 pode ser perigoso",
            }
        ],
    },
    "api_key": {
        "type": "api_credential",
        "min_length": 16,
        "invalid_values": ["YOUR_API_KEY", ""],
    },
    "api_secret": {
        "type": "api_credential",
        "min_length": 16,
        "invalid_values": ["YOUR_API_SECRET", ""],
    },
}


def validate_config_value(
    key: str, value: Any, config_context: dict[str, Any] | None = None
) -> Any:
    if key not in VALIDATION_RULES:
        return value

    rules = VALIDATION_RULES[key]
    validation_type = rules["type"]

    if validation_type == "numeric_range":
        return _validate_numeric_range(key, value, rules)
    elif validation_type == "numeric_minimum":
        return _validate_numeric_minimum(key, value, rules)
    elif validation_type == "risk_threshold":
        return _validate_risk_threshold(key, value, rules)
    elif validation_type == "api_credential":
        return _validate_api_credential(key, value, rules, config_context)

    return value


def _validate_numeric_range(
    key: str, value: int | float, rules: dict[str, Any]
) -> int | float:
    min_val = rules["min_value"]
    max_val = rules["max_value"]

    if value < min_val:
        if rules.get("auto_adjust", False):
            warning(rules["warning_low"].format(min_value=min_val), current=value)
            return min_val
    elif value > max_val:
        if rules.get("auto_adjust", False):
            warning(rules["warning_high"].format(max_value=max_val), current=value)
            return max_val

    return value


def _validate_numeric_minimum(
    key: str, value: int | float, rules: dict[str, Any]
) -> int | float:
    min_val = rules["min_value"]

    if value < min_val:
        if rules.get("auto_adjust", False):
            warning(rules["warning"].format(min_value=min_val), current=value)
            return min_val

    return value


def _validate_risk_threshold(
    key: str, value: int | float, rules: dict[str, Any]
) -> int | float:
    thresholds = rules["thresholds"]

    for threshold in thresholds:
        threshold_value = threshold["value"]
        direction = threshold.get("direction", "above")

        if direction == "above" and value > threshold_value:
            if threshold["level"] == "warning":
                warning(threshold["message"], **{key: value})
            elif threshold["level"] == "error":
                error(threshold["message"], **{key: value})
        elif direction == "below" and value < threshold_value:
            if threshold["level"] == "warning":
                warning(threshold["message"], **{key: value})
            elif threshold["level"] == "error":
                error(threshold["message"], **{key: value})

    return value


def _validate_api_credential(
    key: str, value: str, rules: dict[str, Any], config_context: dict[str, Any]
) -> str:
    if not config_context:
        return value

    min_length = rules.get("min_length", 0)
    invalid_values = rules.get("invalid_values", [])

    if not value or value in invalid_values:
        mode = config_context.get("mode", "unknown")
        warning(f"⚠️ {key} não configurada", mode=mode)
        raise ValueError(
            f"{key} para {mode} não configurada! Configure em config/settings.yaml ou use variáveis de ambiente"
        )

    if len(value) < min_length and value not in invalid_values:
        warning(f"⚠️ {key} muito curta (mínimo {min_length} caracteres)")

    return value


def validate_config(config: dict[str, Any]):
    required_sections = [
        "mode",
        "trading_pairs",
        "strategy",
        "risk",
        "data",
        "binance",
        "database",
    ]

    for section in required_sections:
        if section not in config:
            raise ValueError(f"Seção obrigatória ausente: {section}")

    if config["mode"] not in ["testnet", "real"]:
        raise ValueError("Modo deve ser 'testnet' ou 'real'")

    if not config["trading_pairs"] or not isinstance(config["trading_pairs"], list):
        raise ValueError("trading_pairs deve ser uma lista não vazia")

    validate_strategy_params(config["strategy"])
    validate_risk_params(config["risk"])
    validate_aggressive_mode(config)
    validate_api_keys(config)
    validate_websocket_config(config)
    validate_sanity_checks(config)


def validate_strategy_params(strategy: dict[str, Any]):
    if "min_entry_score" in strategy:
        strategy["min_entry_score"] = validate_config_value(
            "min_entry_score", strategy["min_entry_score"]
        )

    if "cooldown_seconds" in strategy:
        strategy["cooldown_seconds"] = validate_config_value(
            "cooldown_seconds", strategy.get("cooldown_seconds", 0)
        )


def validate_risk_params(risk: dict[str, Any]):
    position_size = risk.get("position_size_pct", 0)
    max_positions = risk.get("max_positions", 1)

    validate_config_value("position_size_pct", position_size)

    total_risk_exposure = position_size * max_positions
    if total_risk_exposure > 60:
        error(
            "🚨 RISCO CRÍTICO: Exposição total pode exceder 60% do capital!",
            position_size=position_size,
            max_positions=max_positions,
            total_exposure=total_risk_exposure,
        )
        warning(
            f"Com posições simultâneas, você pode ter {total_risk_exposure}% do capital em risco"
        )
    elif total_risk_exposure > 40:
        warning(
            "⚠️ RISCO ALTO: Exposição total pode exceder 40% do capital",
            position_size=position_size,
            max_positions=max_positions,
            total_exposure=total_risk_exposure,
        )

    validate_config_value("daily_loss_limit_pct", risk.get("daily_loss_limit_pct", 100))
    validate_config_value("stop_loss_pct", risk.get("stop_loss_pct", 5))
    validate_config_value(
        "max_consecutive_losses", risk.get("max_consecutive_losses", 10)
    )


def validate_aggressive_mode(config: dict[str, Any]):
    if "aggressive_mode" in config:
        aggressive = config["aggressive_mode"]
        if aggressive.get("enabled", False):
            warning("⚠️ Modo agressivo está ATIVADO - maior risco, mais trades")
            if aggressive.get("score_reduction", 0) > 20:
                warning("score_reduction muito alto, ajustado para 20")
                aggressive["score_reduction"] = 20


def validate_api_keys(config: dict[str, Any]):
    mode = config["mode"]
    api_key = config["binance"].get(f"{mode}_api_key", "")
    api_secret = config["binance"].get(f"{mode}_api_secret", "")

    validate_config_value("api_key", api_key, config)
    validate_config_value("api_secret", api_secret, config)

    if api_key and api_key != "YOUR_API_KEY" and not api_key.startswith("${"):
        _validate_api_key_cross_mode_conflicts(config, mode, api_key, api_secret)


def _validate_api_key_cross_mode_conflicts(
    config: dict[str, Any], mode: str, api_key: str, api_secret: str
):
    opposite_mode = "real" if mode == "testnet" else "testnet"
    opposite_key = config["binance"].get(f"{opposite_mode}_api_key", "")
    opposite_secret = config["binance"].get(f"{opposite_mode}_api_secret", "")

    if (
        opposite_key
        and not opposite_key.startswith("${")
        and opposite_secret
        and not opposite_secret.startswith("${")
    ):

        if api_key == opposite_key or api_secret == opposite_secret:
            error(
                "🚨 PERIGO: Mesmas API keys configuradas para testnet e real!",
                current_mode=mode,
            )
            raise ValueError(
                "API keys idênticas para testnet/real - risco de execução no ambiente errado!"
            )

    if mode == "real":
        testnet_key = config["binance"].get("testnet_api_key", "")
        if testnet_key and not testnet_key.startswith("${") and api_key == testnet_key:
            error(
                "🚨 PERIGO: Usando API key testnet no modo REAL!",
                mode=mode,
                key_preview=api_key[:8] + "...",
            )
            raise ValueError(
                "API key testnet sendo usada em modo REAL - cancelando para evitar problemas"
            )

    elif mode == "testnet":
        real_key = config["binance"].get("real_api_key", "")
        if (
            real_key
            and not real_key.startswith("${")
            and "YOUR_API_KEY" not in real_key
            and api_key == real_key
        ):
            error(
                "🚨 CRÍTICO: Usando API key REAL no modo testnet!",
                mode=mode,
                key_preview=api_key[:8] + "...",
            )
            warning("Isso pode resultar em TRADES REAIS acidentais!")
            raise ValueError(
                "API key REAL sendo usada em modo testnet - RISCO DE TRADES REAIS!"
            )


def validate_websocket_config(config: dict[str, Any]):
    mode = config["mode"]
    if mode == "testnet" and config.get("use_ws_testnet", False):
        warning("⚠️ WebSocket testnet habilitado - pode ser instável!")


def validate_sanity_checks(config: dict[str, Any]):
    sanity = config.get("sanity_checks", {})
    if sanity.get("max_spread_allowed", 0) > 0.02:
        warning("⚠️ max_spread_allowed > 2% é muito alto!")


def validate_config_file(
    config_path: str = "config/settings.yaml",
) -> tuple[bool, list[str]]:
    issues = []

    try:
        from .config_loader import load_config

        config = load_config(config_path)

        validations = [
            (
                "trading_pairs não pode estar vazio",
                lambda c: len(c.get("trading_pairs", [])) > 0,
            ),
            (
                "min_entry_score deve estar entre 5-100",
                lambda c: 5
                <= c.get("strategy", {}).get("min_entry_score", MIN_ENTRY_SCORE)
                <= 100,
            ),
            (
                "position_size_pct deve estar entre 1-50",
                lambda c: 1 <= c.get("risk", {}).get("position_size_pct", 12) <= 50,
            ),
            (
                "max_positions deve ser >= 1",
                lambda c: c.get("risk", {}).get("max_positions", 1) >= 1,
            ),
            (
                "API keys configuradas para modo atual",
                lambda c: _validate_api_keys_check(c),
            ),
        ]

        for description, validator in validations:
            try:
                if not validator(config):
                    issues.append(description)
            except Exception as e:
                issues.append(f"{description}: Erro na validação - {e}")

        return len(issues) == 0, issues

    except Exception as e:
        issues.append(f"Erro ao carregar configuração: {e}")
        return False, issues


def _validate_api_keys_check(config: dict[str, Any]) -> bool:
    mode = config.get("mode", "testnet")

    api_key = config.get("binance", {}).get(f"{mode}_api_key", "")
    api_secret = config.get("binance", {}).get(f"{mode}_api_secret", "")

    return (
        api_key and "YOUR_" not in api_key and api_secret and "YOUR_" not in api_secret
    )
