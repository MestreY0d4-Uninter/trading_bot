import os
from typing import Any

import yaml
from dotenv import load_dotenv

from shared.constants import MIN_ENTRY_SCORE
from shared.observability.logger import debug, production

load_dotenv()


def create_default_config(config_path: str = "config/settings.yaml"):
    os.makedirs(os.path.dirname(config_path), exist_ok=True)

    default_config = {
        "mode": "testnet",
        "trading_pairs": [
            "BTCUSDT",  # Core holdings - Market leader
            "ETHUSDT",  # Core holdings - Smart contracts
            "BNBUSDT",  # Exchange token - Guaranteed liquidity
            "ADAUSDT",  # Proven performer - 2 profitable trades
            "DOTUSDT",  # Layer-1 - Active ecosystem
            "SOLUSDT",  # NEW - High performance L1
            "XRPUSDT",  # NEW - Exceptional liquidity
            "AVAXUSDT",  # NEW - DeFi ecosystem
        ],
        "strategy": {
            "rsi_oversold": 20,
            "rsi_overbought": 80,
            "bb_lower_threshold": 0.12,
            "min_volume_ratio": 0.05,
            "min_bb_width": 0.5,
            "stop_loss_pct": 2.5,
            "take_profit_pct": 2.0,
            "min_entry_score": MIN_ENTRY_SCORE,
            "cooldown_seconds": 100,
        },
        "risk": {
            "position_size_pct": 8.5,  # Optimized from 12% → aligned with standards
            "max_positions": 7,  # Optimized from 3 → +133% opportunities,
            "daily_loss_limit_pct": 11.0,
            "daily_target_pct": 5.0,
            "stop_loss_pct": 2.5,
            "take_profit_pct": 2.0,
            "max_consecutive_losses": 3,
            "max_spread_pct": 0.3,
            "min_order_size": 10.0,
            "trailing_trigger_multiplier": 1.012,
            "trailing_stop_multiplier": 0.992,
            "trailing_update_threshold": 1.002,
        },
        "aggressive_mode": {
            "enabled": False,
            "score_reduction": 10,
            "spread_multiplier": 1.3,
            "position_multiplier": 1.1,
        },
        "market_analysis": {
            "update_interval": 300,
            "trend_threshold": 0.005,
            "strong_trend_threshold": 0.015,
            "volatility_adjustment": True,
            "volume_weight": 0.3,
        },
        "data": {"download_days": 30, "candle_interval": "5m", "cache_ttl": 300},
        "cache": {
            "default_ttl": 300,
            "ticker_ttl": 10,
            "candles_ttl": 60,
            "orderbook_ttl": 5,
        },
        "trading": {
            "check_interval": 30,
            "max_slippage": 0.015,
            "emergency_close_on_shutdown": True,
        },
        "bot": {"name": "TradingBot V2", "version": "2.0.0", "check_interval": 30},
        "binance": {
            "testnet_api_key": os.getenv("BINANCE_TESTNET_API_KEY", ""),
            "testnet_api_secret": os.getenv("BINANCE_TESTNET_API_SECRET", ""),
            "real_api_key": os.getenv("BINANCE_API_KEY", ""),
            "real_api_secret": os.getenv("BINANCE_API_SECRET", ""),
            "testnet_url": "https://testnet.binance.vision",
            "real_url": "https://api.binance.com",
        },
        "database": {
            "path": "data/trading_bot.db",
            "backup_interval": 24,
            "max_backups": 7,
        },
        "oco_settings": {
            "enabled": False,
            "price_precision": 2,
            "quantity_precision": 4,
            "slippage_tolerance": 0.002,
        },
        "sanity_checks": {
            "max_spread_allowed": 0.003,
            "min_balance_required": 25,
            "max_single_loss": 18,
            "max_single_loss_pct": 8.0,
            "require_confirmations": False,
            "min_volume_usd": 40000,
            "max_slippage_pct": 1.5,
        },
        "websocket": {"enabled": False, "reconnect_interval": 5, "ping_interval": 30},
        "system": {
            "timezone": "America/Sao_Paulo",
            "update_check": True,
            "debug_mode": False,
        },
        "emergency_conditions": {
            "close_on_daily_drawdown": 15.0,
            "close_on_consecutive_losses": 4,
            "close_on_system_error": True,
        },
        "shutdown_safety": {
            "maintain_stops_active": True,
            "maintain_takes_active": True,
            "alert_major_moves": 5.0,
            "position_timeout_hours": 48,
        },
        "monitoring": {
            "save_performance_metrics": True,
            "metrics_interval": 30,
            "save_market_data": True,
            "daily_metrics_hour": 0,
            "position_tracking": {
                "enabled": True,
                "alert_thresholds": {
                    "yellow": 12,  # "Monitorando" - Alerta informativo (horas)
                    "orange": 24,  # "Atenção" - Revisão recomendada (horas)
                    "red": 48,  # "Crítico" - Análise necessária (horas)
                },
            },
            "temporal_metrics": {
                "track_duration": True,
                "avg_duration_target": 8,  # Meta ideal (horas)
                "max_comfort_hours": 36,  # Zona de conforto
                "log_duration_stats": True,
            },
        },
    }

    with open(config_path, "w", encoding="utf-8") as file:
        yaml.dump(
            default_config,
            file,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
            indent=2,
        )

    production("✅ Arquivo de configuração padrão V2 criado", path=config_path)


def add_v2_defaults(config: dict[str, Any]) -> dict[str, Any]:
    if "market_analysis" not in config:
        config["market_analysis"] = {
            "update_interval": 300,
            "trend_threshold": 0.005,
            "strong_trend_threshold": 0.015,
            "volatility_adjustment": True,
            "volume_weight": 0.3,
        }
        production("Adicionada configuração market_analysis padrão")

    if "cache" not in config:
        config["cache"] = {
            "default_ttl": 300,
            "ticker_ttl": 10,
            "candles_ttl": 60,
            "orderbook_ttl": 5,
        }
        production("Adicionada configuração cache padrão")

    if "trading" not in config:
        config["trading"] = {}

    if "check_interval" not in config["trading"]:
        if "bot" in config and "check_interval" in config["bot"]:
            config["trading"]["check_interval"] = config["bot"]["check_interval"]
        else:
            config["trading"]["check_interval"] = 30

    if "risk" not in config:
        config["risk"] = {}

    risk_defaults = {
        "position_size_pct": 8.5,  # Optimized from 12% → aligned with standards
        "max_positions": 7,  # Optimized from 3 → +133% opportunities,
        "daily_loss_limit_pct": 11.0,
        "daily_target_pct": 5.0,
        "stop_loss_pct": 2.5,
        "take_profit_pct": 2.5,
        "max_consecutive_losses": 3,
        "max_spread_pct": 0.3,
        "min_order_size": 10.0,
        "trailing_trigger_multiplier": 1.012,
        "trailing_stop_multiplier": 0.992,
        "trailing_update_threshold": 1.002,
    }

    for key, value in risk_defaults.items():
        if key not in config["risk"]:
            config["risk"][key] = value
            debug("Adicionado padrão", section="risk", key=key, value=value)

    if "strategy" in config:
        if "min_entry_score" not in config["strategy"]:
            config["strategy"]["min_entry_score"] = MIN_ENTRY_SCORE
            production(
                f"min_entry_score ajustado para {MIN_ENTRY_SCORE} (configuração otimizada)"
            )
        elif config["strategy"]["min_entry_score"] > 50:
            production(
                "min_entry_score muito alto - considere reduzir para 35-45",
                current=config["strategy"]["min_entry_score"],
            )

    if "optimization" not in config:
        config["optimization"] = {
            "enable_optimization": True,
            "cache_ttl": 300,
            "update_warmup": 60,
            "batch_size": 10,
            "use_parallel": True,
            "max_workers": 4,
        }
        production("Adicionada configuração optimization padrão")

    if "emergency_conditions" not in config:
        config["emergency_conditions"] = {
            "close_on_daily_drawdown": 15.0,
            "close_on_consecutive_losses": 4,
            "close_on_system_error": True,
        }
        production("Adicionada configuração emergency_conditions padrão")

    if "shutdown_safety" not in config:
        config["shutdown_safety"] = {
            "maintain_stops_active": True,
            "maintain_takes_active": True,
            "alert_major_moves": 5.0,
            "position_timeout_hours": 48,
        }
        production("Adicionada configuração shutdown_safety padrão")

    if "trading" not in config:
        config["trading"] = {}

    if "emergency_close_on_shutdown" not in config["trading"]:
        config["trading"]["emergency_close_on_shutdown"] = True
        production(
            "Configurado emergency_close_on_shutdown = True (fechamento automático habilitado)"
        )

    if "monitoring" not in config:
        config["monitoring"] = {
            "save_performance_metrics": True,
            "metrics_interval": 30,
            "save_market_data": True,
            "daily_metrics_hour": 0,
            "position_tracking": {
                "enabled": True,
                "alert_thresholds": {
                    "yellow": 12,  # "Monitorando" - Alerta informativo (horas)
                    "orange": 24,  # "Atenção" - Revisão recomendada (horas)
                    "red": 48,  # "Crítico" - Análise necessária (horas)
                },
            },
            "temporal_metrics": {
                "track_duration": True,
                "avg_duration_target": 8,  # Meta ideal (horas)
                "max_comfort_hours": 36,  # Zona de conforto
                "log_duration_stats": True,
            },
        }
        production("Adicionada configuração monitoring padrão")

    return config


def add_sanity_checks(config: dict[str, Any]) -> dict[str, Any]:
    if "sanity_checks" not in config:
        config["sanity_checks"] = {
            "max_spread_allowed": 0.003,
            "min_balance_required": 25,
            "max_single_loss": 18,
            "max_single_loss_pct": 8.0,
            "require_confirmations": False,
            "min_volume_usd": 40000,
            "max_slippage_pct": 1.5,
        }
        production("Adicionada configuração sanity_checks padrão")

    return config
