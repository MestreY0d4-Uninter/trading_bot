import os
import re
import threading
from pathlib import Path
from typing import Any

import yaml

from shared.constants import MIN_ENTRY_SCORE
from shared.observability.logger import debug, error, production, warning

_expansion_lock = threading.RLock()
_compiled_pattern = re.compile(r"\$\{([^}]+)\}")


def expand_env_vars(
    value: Any, _visited: set[str] | None = None, _max_iterations: int = 10
) -> Any:
    with _expansion_lock:
        if _visited is None:
            _visited = set()

        if isinstance(value, str):
            if len(_visited) > 50:
                raise ValueError(
                    "Detectada referência circular ou recursão excessiva em variáveis de ambiente"
                )

            original_value = value
            if original_value in _visited:
                raise ValueError(
                    f"Referência circular detectada na variável: {original_value}"
                )

            result = value
            iterations = 0

            while iterations < _max_iterations:
                previous_result = result
                _visited.add(result)

                def replacer(match):
                    var_default = match.group(1)
                    if ":-" in var_default:
                        var_name, default_value = var_default.split(":-", 1)
                        var_name = var_name.strip()
                        env_value = os.environ.get(var_name)

                        if env_value is None or env_value.strip() == "":
                            debug(
                                "Usando valor padrão para variável de ambiente",
                                var=var_name,
                                default=default_value[:50],
                            )
                            return default_value
                        else:
                            return env_value
                    else:
                        var_name = var_default.strip()
                        env_value = os.environ.get(var_name)

                        if env_value is None:
                            warning(
                                "Variável de ambiente não encontrada, mantendo placeholder",
                                var=var_name,
                            )
                            return match.group(0)
                        elif env_value.strip() == "":
                            warning("Variável de ambiente vazia", var=var_name)
                            return match.group(0)
                        else:
                            if not _is_valid_env_value(env_value, var_name):
                                warning(
                                    "Valor de variável ambiente potencialmente inválido",
                                    var=var_name,
                                )
                            return env_value

                result = _compiled_pattern.sub(replacer, result)
                iterations += 1

                if result == previous_result:
                    break

                if result in _visited and result != previous_result:
                    raise ValueError(
                        f"Referência circular detectada durante expansão: {result}"
                    )

            if iterations >= _max_iterations:
                raise ValueError(
                    f"Máximo de iterações ({_max_iterations}) atingido na expansão de: {original_value}"
                )

            return result

        elif isinstance(value, dict):
            return {
                k: expand_env_vars(
                    v, _visited.copy() if _visited else None, _max_iterations
                )
                for k, v in value.items()
            }
        elif isinstance(value, list):
            return [
                expand_env_vars(
                    item, _visited.copy() if _visited else None, _max_iterations
                )
                for item in value
            ]
        else:
            return value


def _is_valid_env_value(value: str, var_name: str) -> bool:
    if not value or not isinstance(value, str):
        return False

    stripped_value = value.strip()

    if "API_KEY" in var_name.upper():
        if len(stripped_value) != 64 or not stripped_value.isalnum():
            warning(
                "API Key Binance deve ter 64 caracteres alfanuméricos", var=var_name
            )
            return False
        if stripped_value.upper() in ["YOUR_API_KEY", "PLACEHOLDER", "CHANGE_ME"]:
            warning("API Key contém placeholder inválido", var=var_name)
            return False

    elif "API_SECRET" in var_name.upper() or "SECRET" in var_name.upper():
        if len(stripped_value) != 64 or not stripped_value.isalnum():
            warning(
                "API Secret Binance deve ter 64 caracteres alfanuméricos", var=var_name
            )
            return False
        if stripped_value.upper() in ["YOUR_API_SECRET", "PLACEHOLDER", "CHANGE_ME"]:
            warning("API Secret contém placeholder inválido", var=var_name)
            return False

    elif any(
        keyword in var_name.upper()
        for keyword in ["KEY", "SECRET", "TOKEN", "PASSWORD"]
    ):
        if len(stripped_value) < 16:
            warning("Credencial muito curta (< 16 chars)", var=var_name)
            return False
        if any(
            placeholder in stripped_value.upper()
            for placeholder in ["PLACEHOLDER", "CHANGE_ME", "YOUR_"]
        ):
            warning("Credencial contém placeholder", var=var_name)
            return False

    return True


def load_config(config_path: str = "config/settings.yaml") -> dict[str, Any]:
    config_path_obj = Path(config_path)
    config = None
    config_created = False

    try:
        if not config_path_obj.exists():
            warning("Arquivo de configuração não encontrado", path=str(config_path_obj))
            production("Criando arquivo de configuração padrão")

            try:
                config_path_obj.parent.mkdir(parents=True, exist_ok=True)

                temp_config_path = config_path_obj.with_suffix(".tmp")
                from .config_defaults import create_default_config

                create_default_config(str(temp_config_path))

                temp_config_path.rename(config_path_obj)
                config_created = True
                production("✓ Arquivo de configuração padrão criado com sucesso")
            except ImportError as e:
                error("CRÍTICO: Módulo config_defaults não encontrado", error=str(e))
                raise RuntimeError(
                    f"Dependência crítica não encontrada: {str(e)}"
                ) from e
            except Exception as e:
                error("CRÍTICO: Falha ao criar configuração padrão", error=str(e))
                _safe_cleanup(config_path_obj, config_created)
                raise RuntimeError(
                    f"Não foi possível criar arquivo de configuração: {str(e)}"
                ) from e

        try:
            with open(config_path_obj, encoding="utf-8") as file:
                raw_content = file.read(1024 * 1024)  # 1MB limit
                if len(raw_content) >= 1024 * 1024:
                    raise ValueError("Arquivo de configuração muito grande (>1MB)")

            config = yaml.safe_load(raw_content)

            if not config or not isinstance(config, dict):
                raise ValueError("Arquivo de configuração vazio ou formato inválido")

            if len(config) == 0:
                raise ValueError("Arquivo de configuração está vazio")

        except (yaml.YAMLError, UnicodeDecodeError) as e:
            error(
                "CRÍTICO: Erro ao carregar YAML",
                path=str(config_path_obj),
                error=str(e),
            )

            _safe_cleanup(config_path_obj, config_created)
            raise RuntimeError(f"Arquivo YAML corrompido ou ilegível: {str(e)}") from e
        except PermissionError as e:
            error(
                "CRÍTICO: Permissão negada ao ler configuração",
                path=str(config_path_obj),
            )
            raise RuntimeError(
                f"Sem permissão para ler arquivo de configuração: {str(e)}"
            ) from e

        try:
            config = expand_env_vars(config)
        except ValueError as e:
            error("CRÍTICO: Erro na expansão de variáveis de ambiente", error=str(e))
            _safe_cleanup(config_path_obj, config_created)
            raise RuntimeError(f"Configuração inválida: {str(e)}") from e

        try:
            from .config_defaults import add_sanity_checks, add_v2_defaults

            config = add_v2_defaults(config)
            config = add_sanity_checks(config)
        except ImportError as e:
            error("CRÍTICO: Módulos de configuração não encontrados", error=str(e))
            _safe_cleanup(config_path_obj, config_created)
            raise RuntimeError(
                f"Dependências de configuração não encontradas: {str(e)}"
            ) from e
        except Exception as e:
            error("CRÍTICO: Erro ao adicionar defaults", error=str(e))
            _safe_cleanup(config_path_obj, config_created)
            raise RuntimeError(f"Falha ao processar configuração: {str(e)}") from e

        try:
            from .config_validator import validate_config

            validate_config(config)
        except ImportError as e:
            error("CRÍTICO: Validador de configuração não encontrado", error=str(e))
            _safe_cleanup(config_path_obj, config_created)
            raise RuntimeError(
                f"Validador de configuração não encontrado: {str(e)}"
            ) from e
        except ValueError as e:
            error("CRÍTICO: Configuração inválida", error=str(e))
            _safe_cleanup(config_path_obj, config_created)
            raise RuntimeError(f"Configuração não passou na validação: {str(e)}") from e
        except Exception as e:
            error("CRÍTICO: Erro na validação", error=str(e))
            _safe_cleanup(config_path_obj, config_created)
            raise RuntimeError(f"Erro durante validação: {str(e)}") from e

        try:
            log_config_summary(config)
        except Exception as e:
            warning("Erro ao gerar log de configuração", error=str(e))

        production("✅ Configuração carregada e validada com sucesso")
        return config

    except RuntimeError:
        raise
    except Exception as e:
        error(
            "CRÍTICO: Erro inesperado no carregamento de configuração",
            error=str(e),
            type=type(e).__name__,
        )

        _safe_cleanup(config_path_obj, config_created)
        raise RuntimeError(f"Erro crítico no sistema de configuração: {str(e)}") from e


def _safe_cleanup(config_path: Path, was_created: bool):
    if was_created and config_path.exists():
        try:
            config_path.unlink()
            debug("Arquivo de configuração corrompido removido", path=str(config_path))
        except PermissionError:
            warning(
                "Não foi possível remover arquivo corrompido - permissão negada",
                path=str(config_path),
            )
        except Exception as e:
            warning(
                "Falha ao limpar arquivo corrompido",
                path=str(config_path),
                error=str(e),
            )


def log_config_summary(config: dict[str, Any]):
    if not isinstance(config, dict):
        warning("Config inválido para log summary")
        return

    try:
        production("📋 Resumo da Configuração")

        mode = config.get("mode", "DESCONHECIDO")
        production("Modo configurado", mode=mode)

        trading_pairs = config.get("trading_pairs", [])
        if isinstance(trading_pairs, list):
            production("Pares de trading", count=len(trading_pairs))
        else:
            warning("Trading pairs não é uma lista válida")

        strategy = config.get("strategy", {})
        if isinstance(strategy, dict):
            production(
                "Score mínimo", score=strategy.get("min_entry_score", MIN_ENTRY_SCORE)
            )
            production("Cooldown", seconds=strategy.get("cooldown_seconds", 100))

        aggressive_mode = config.get("aggressive_mode", {})
        if isinstance(aggressive_mode, dict):
            production("Modo agressivo", enabled=aggressive_mode.get("enabled", False))

        websocket = config.get("websocket", {})
        if isinstance(websocket, dict):
            production("WebSocket", enabled=websocket.get("enabled", False))

        oco_settings = config.get("oco_settings", {})
        if isinstance(oco_settings, dict):
            production("OCO", enabled=oco_settings.get("enabled", False))

        production("✅ Summary de configuração concluído")

    except Exception as e:
        warning("Erro ao gerar summary detalhado de configuração", error=str(e))
