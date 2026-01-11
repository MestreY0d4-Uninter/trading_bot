from decimal import Decimal, InvalidOperation
from typing import Any

from shared.observability.logger import warning
from utils.validation_utils import is_numeric_valid


class TradingCoordinatorValidators:
    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator

    def _validate_field_value(
        self, value: Any, field: str, data_name: str, data: dict
    ) -> bool:
        try:
            if isinstance(value, str):
                try:
                    converted = Decimal(value)
                    data[field] = converted
                    value = converted
                except (InvalidOperation, ValueError, TypeError):
                    warning(
                        f"Não foi possível converter {field} para Decimal em {data_name}",
                        value=value,
                        type=type(value).__name__,
                    )
                    return False

            if not isinstance(value, (int, float, Decimal)):
                warning(
                    f"Tipo inválido para {field} em {data_name}",
                    value=value,
                    type=type(value).__name__,
                )
                return False

            if isinstance(value, Decimal):
                if value.is_nan() or value.is_infinite():
                    warning(
                        f"Valor NaN ou infinito para {field} em {data_name}",
                        value=value,
                    )
                    return False
            elif not is_numeric_valid(value):
                warning(
                    f"Valor NaN ou infinito para {field} em {data_name}", value=value
                )
                return False

            return True

        except Exception as e:
            warning(
                f"Erro na validação de {field} em {data_name}",
                value=value,
                error=str(e),
            )
            return False

    def validate_financial_data(
        self, data, data_name: str, required_fields: list[str] | None = None
    ):
        if not data or not isinstance(data, dict):
            warning(f"Dados de {data_name} inválidos")
            return False

        if required_fields:
            for field in required_fields:
                if field not in data:
                    warning(f"Campo obrigatório {field} ausente em {data_name}")
                    return False

                value = data[field]

                if not self._validate_field_value(value, field, data_name, data):
                    return False

        return True
