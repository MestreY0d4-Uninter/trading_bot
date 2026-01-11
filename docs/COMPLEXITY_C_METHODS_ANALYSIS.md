# Análise dos Métodos de Complexidade C (11-20)

> **Gerado em:** 2025-12-18  
> **Total de métodos C:** 46  
> **Média de complexidade:** 13.5

## Padrões Identificados no Freqtrade (Referência)

Após análise do código do Freqtrade (bot de trading open-source mais popular), identifiquei os seguintes padrões que devemos seguir:

### 1. **Métodos Pequenos e Focados**
Cada método faz UMA coisa. Se um método precisa fazer múltiplas coisas, ele delega para métodos privados.

```python
# Freqtrade Pattern
def get_rate(self, pair, refresh, side, is_short, order_book=None, ticker=None):
    # Apenas orquestra - delega trabalho real para métodos específicos
    if conf_strategy.get("use_order_book", False):
        rate = self._get_rate_from_ob(...)
    else:
        rate = self._get_rate_from_ticker(...)
    return rate
```

### 2. **Early Returns para Validações**
Validações no início com retorno imediato, sem aninhamento.

```python
# ✅ Bom (Freqtrade)
def fetch_ticker(self, pair):
    if pair not in self.markets or not self.markets[pair].get("active"):
        raise ExchangeError(f"Pair {pair} not available")
    return self._api.fetch_ticker(pair)

# ❌ Ruim
def fetch_ticker(self, pair):
    if pair in self.markets:
        if self.markets[pair].get("active"):
            return self._api.fetch_ticker(pair)
    raise ExchangeError(...)
```

### 3. **Configuração como Dados (não como código)**
Em vez de múltiplos if/elif, usa dicionários de configuração.

```python
# ✅ Bom
price_map = {
    ("entry", "long", "same"): "bid",
    ("entry", "long", "other"): "ask",
    ("entry", "short", "same"): "ask",
    ...
}
price_side = price_map[(side, direction, price_side)]

# ❌ Ruim
if side == "entry" and not is_short and price_side == "same":
    return "bid"
elif side == "entry" and not is_short and price_side == "other":
    return "ask"
...
```

### 4. **Decorator @retrier para Operações de Rede**
Retry automático sem poluir a lógica do método.

```python
@retrier
def fetch_ticker(self, pair):
    # Lógica limpa, retry é tratado pelo decorator
    return self._api.fetch_ticker(pair)
```

### 5. **Separação de Responsabilidades**
- **Fetch**: Busca dados externos
- **Validate**: Valida dados
- **Process**: Processa/transforma dados
- **Persist**: Salva dados

---

## Análise dos 46 Métodos C do Trading Bot

### Categoria 1: Validadores (12 métodos) - Prioridade Alta

| CC | Método | Arquivo | Padrão Recomendado |
|----|--------|---------|-------------------|
| 20 | `validate_order_preflight_or_raise` | `core/validators/trading_validator.py` | Early returns + dataclass |
| 18 | `validate_profit_target` | `core/validators/trading_validator.py` | Config como dados |
| 17 | `_validate_ticker_data` | `infrastructure/data_fetch/data_fetcher_base.py` | Já refatorado? Verificar |
| 13 | `validate_order_params` | `core/execution/execution_validator.py` | Early returns + helper methods |
| 13 | `validate_oco_params` | `core/execution/execution_validator.py` | Early returns |
| 12 | `_validate_numeric_input` | `core/execution/execution_validator.py` | Simplificar condições |
| 12 | `_get_precision` | `core/execution/execution_validator.py` | Cache + fallback pattern |
| 14 | `validate_financial_data` | `core/coordinator/trading_coordinator_validators.py` | Early returns |
| 11 | `validate_position_inputs` | `core/validators/trading_validator.py` | Early returns |
| 11 | `validate_portfolio_correlation` | `core/validators/correlation_validator.py` | Já simplificado |

**Técnica:** Usar dataclasses para estruturar validações, early returns, e configuração como dados.

---

### Categoria 2: Fetchers de Dados (10 métodos) - Prioridade Média

| CC | Método | Arquivo | Padrão Recomendado |
|----|--------|---------|-------------------|
| 19 | `fetch_candles` | `infrastructure/data_fetch/candle_fetcher.py` | Separar fetch/validate/process |
| 17 | `fetch_orderbook` | `infrastructure/data_fetch/ticker_fetcher.py` | Early returns + helper |
| 17 | `fetch_24hr_ticker` | `infrastructure/data_fetch/ticker_fetcher.py` | Early returns + helper |
| 16 | `fetch_all_tickers` | `infrastructure/data_fetch/ticker_fetcher.py` | Batch processing pattern |
| 12 | `update_comprehensive_market_data` | `infrastructure/data_fetch/candle_fetcher.py` | Orchestrator pattern |
| 11 | `get_klines` | `infrastructure/api/spot_endpoints.py` | @retrier + early return |
| 11 | `get_symbol_filters` | `infrastructure/api/spot_endpoints.py` | Cache pattern |
| 15 | `get_balance` | `infrastructure/api/trading_endpoints.py` | @retrier + early return |

**Técnica:** Padrão Freqtrade - separar fetch, validate, process. Usar @retrier.

---

### Categoria 3: Calculadores (6 métodos) - Prioridade Baixa

| CC | Método | Arquivo | Padrão Recomendado |
|----|--------|---------|-------------------|
| 13 | `calculate_ema_crossover` | `core/analysis/indicators/trend.py` | Helper methods |
| 12 | `calculate_stop_loss` | `core/risk/risk_calculator.py` | Config como dados |
| 12 | `calculate_take_profit` | `core/risk/risk_calculator.py` | Config como dados |
| 11 | `calculate_volume_profile` | `core/analysis/indicators/volume.py` | Helper methods |
| 12 | `calculate_position_parameters` | `core/execution/position_calculator.py` | Dataclass para resultado |

**Técnica:** Extrair cálculos parciais para métodos helper, usar dataclasses.

---

### Categoria 4: Handlers de Estado (8 métodos) - Prioridade Média

| CC | Método | Arquivo | Padrão Recomendado |
|----|--------|---------|-------------------|
| 18 | `_handle_message` | `infrastructure/websocket/websocket_manager.py` | Message router pattern |
| 17 | `check_exit_conditions` | `core/position/position_state.py` | Strategy pattern |
| 15 | `_connection_loop` | `infrastructure/websocket/websocket_manager.py` | State machine pattern |
| 12 | `__init__` (PositionStateManager) | `core/position/position_state.py` | Builder pattern |
| 11 | `_process_kline` | `core/market/websocket_trading_loop.py` | Pipeline pattern |

**Técnica:** Message router, state machines, strategy pattern.

---

### Categoria 5: Orquestração (6 métodos) - Prioridade Alta

| CC | Método | Arquivo | Padrão Recomendado |
|----|--------|---------|-------------------|
| 16 | `run` | `core/coordinator/trading_coordinator_orchestration.py` | State machine |
| 13 | `initialize` | `services/bot_lifecycle.py` | Step-by-step initialization |
| 11 | `_rollback_initialization` | `core/coordinator/trading_coordinator_lifecycle.py` | Cleanup stack pattern |
| 12 | `create_oco_exit` | `core/execution/order_executor.py` | Builder pattern |
| 11 | `_process_single_fill` | `core/execution/order_executor.py` | Pipeline pattern |

**Técnica:** State machines, cleanup stacks, builder pattern.

---

### Categoria 6: Métricas e Monitoramento (4 métodos) - Prioridade Baixa

| CC | Método | Arquivo | Padrão Recomendado |
|----|--------|---------|-------------------|
| 15 | `sync_database_to_state` | `shared/observability/metrics.py` | Batch updates |
| 13 | `_get_memory_summary` | `shared/observability/metrics.py` | Já estruturado |
| 12 | `should_log` | `shared/observability/formatters.py` | Config como dados |
| 11 | `get_bot_status` | `shared/observability/flow_tracker.py` | Aggregate pattern |

**Técnica:** Batch updates, aggregate patterns.

---

## Priorização de Refatoração

### 🔴 Alta Prioridade (Impacto Crítico)
1. `validate_order_preflight_or_raise` (CC 20) - Validação de ordens
2. `fetch_candles` (CC 19) - Core do bot
3. `_handle_message` (CC 18) - WebSocket crítico
4. `validate_profit_target` (CC 18) - Risco financeiro
5. `check_exit_conditions` (CC 17) - Saída de posições

### 🟡 Média Prioridade (Manutenibilidade)
6-15. Fetchers e validadores restantes

### 🟢 Baixa Prioridade (Nice to have)
16-46. Calculadores, métricas, orquestração menor

---

## Técnicas de Refatoração Recomendadas

### 1. Early Returns Pattern
```python
# Antes (CC alto)
def validate(self, data):
    if data:
        if data.price > 0:
            if data.quantity > 0:
                return True
    return False

# Depois (CC baixo)
def validate(self, data):
    if not data:
        return False
    if data.price <= 0:
        return False
    if data.quantity <= 0:
        return False
    return True
```

### 2. Configuration as Data
```python
# Antes (CC alto por múltiplos if/elif)
def get_threshold(self, level):
    if level == "low":
        return 0.5
    elif level == "medium":
        return 1.0
    elif level == "high":
        return 2.0

# Depois (CC = 1)
THRESHOLDS = {"low": 0.5, "medium": 1.0, "high": 2.0}

def get_threshold(self, level):
    return THRESHOLDS.get(level, 1.0)
```

### 3. Extract Method
```python
# Antes (método longo com múltiplas responsabilidades)
def process_order(self, order):
    # 50 linhas de código misturado

# Depois (orquestrador + métodos focados)
def process_order(self, order):
    validated = self._validate_order(order)
    prepared = self._prepare_order(validated)
    return self._execute_order(prepared)
```

### 4. Dataclass para Resultados Complexos
```python
# Antes (retorna dict genérico)
def calculate(self):
    return {"value": 1, "valid": True, "error": None}

# Depois (tipagem forte)
@dataclass
class CalculationResult:
    value: Decimal
    valid: bool
    error: str | None = None

def calculate(self) -> CalculationResult:
    return CalculationResult(value=Decimal("1"), valid=True)
```

---

## Métricas Alvo

| Métrica | Atual | Alvo |
|---------|-------|------|
| Métodos D+ | 0 | 0 ✅ |
| Métodos C | 46 | < 20 |
| Média CC | 3.93 (A) | < 4 (A) ✅ |
| Maior CC | 20 | < 15 |

---

## Próximos Passos

1. ✅ Documentar métodos C (este documento)
2. 🔄 Refatorar 5 métodos de alta prioridade
3. 🔄 Testar refatorações
4. 🔄 Atualizar COMPLEXITY_RANKING.md
