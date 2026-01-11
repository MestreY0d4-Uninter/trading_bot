# Ranking de Complexidade Ciclomática

**Data:** 2025-12-02  
**Última Atualização:** 2025-12-18  
**Ferramenta:** Radon CC  
**Threshold:** CC ≥ 11 (C ou pior)

---

## Resumo Executivo

| Métrica | Antes | Depois | Melhoria |
|---------|-------|--------|----------|
| Métodos D/E | 18 | **0** | 100% ✅ |
| Métodos C | ~50 | **52** | - |
| Média CC | ~5.2 | **3.74 (A)** | 28% |
| Total blocos | ~1000 | **1083** | +8% (helpers) |
| Total refatorado | 0 | **42+ métodos** | - |

---

## Legenda de Ranks

| Rank | CC Range | Risco | Ação Recomendada |
|------|----------|-------|------------------|
| **A** | 1-5 | Baixo | ✅ Ideal |
| **B** | 6-10 | Baixo | ✅ Aceitável |
| **C** | 11-20 | Moderado | ⚠️ Considerar refatorar |
| **D** | 21-30 | Alto | 🔴 Refatorar |
| **E** | 31-40 | Muito Alto | 🔴 Refatorar urgente |
| **F** | 41+ | Crítico | 🚨 Refatorar imediatamente |

---

## Top 30 - Métodos Mais Complexos

| # | CC | Rank | Arquivo | Método | Status |
|---|----|----|---------|--------|--------|
| 1 | ~~37~~ **6** | ~~E~~ **B** | `core/engine/trading_loop_execution.py:303` | `run` | ✅ **REFATORADO** |
| 2 | ~~30~~ **9** | ~~D~~ **B** | `core/engine/trading_loop_execution.py:618` | `_execute_entry` | ✅ **REFATORADO** |
| 3 | ~~35~~ **7** | ~~E~~ **B** | `core/coordinator/trading_coordinator_balance.py:196` | `update_account_balance` | ✅ **REFATORADO** |
| 4 | ~~34~~ **2** | ~~E~~ **A** | `infrastructure/api/endpoints/trading_endpoints.py:186` | `create_order` | ✅ **REFATORADO** |
| 5 | ~~32~~ **6** | ~~E~~ **B** | `core/position/position_validator.py:183` | `validate_fresh_price` | ✅ **REFATORADO** |
| 6 | ~~30~~ **4** | ~~D~~ **A** | `core/market/market_updater.py:144` | `_update_symbol_data` | ✅ **REFATORADO** |
| 7 | ~~30~~ **7** | ~~D~~ **B** | `core/signals/signal_analyzer.py:95` | `analyze_symbol` | ✅ **REFATORADO** |
| 8 | ~~30~~ **3** | ~~D~~ **A** | `core/coordinator/trading_coordinator_lifecycle.py:234` | `_restore_and_validate_positions` | ✅ **REFATORADO** |
| 9 | ~~29~~ **4** | ~~D~~ **A** | `infrastructure/data_fetch/data_fetcher_base.py:334` | `_validate_candles_data` | ✅ **REFATORADO** |
| 10 | ~~28~~ **6** | ~~D~~ **B** | `core/execution/order_executor.py:622` | `get_average_fill_price` | ✅ **REFATORADO** |
| 11 | ~~26~~ **9** | ~~D~~ **B** | `infrastructure/data_fetch/ticker_fetcher.py:18` | `fetch_ticker` | ✅ **REFATORADO** |
| 12 | ~~24~~ **5** | ~~D~~ **A** | `core/coordinator/trading_coordinator_lifecycle.py:553` | `_handle_emergency_close` | ✅ **REFATORADO** |
| 13 | ~~24~~ **4** | ~~D~~ **A** | `infrastructure/data_fetch/data_fetcher_base.py:552` | `_validate_orderbook_data` | ✅ **REFATORADO** |
| 14 | ~~21~~ **3** | ~~D~~ **A** | `core/risk/emergency_manager.py:182` | `health_check` | ✅ **REFATORADO** |
| 15 | ~~21~~ **3** | ~~D~~ **A** | `core/coordinator/trading_coordinator_orchestration.py:151` | `get_status` | ✅ **REFATORADO** |
| 16 | ~~21~~ **6** | ~~D~~ **B** | `services/state_recovery.py:33` | `_reconcile_state_on_startup` | ✅ **REFATORADO** |
| 17 | ~~21~~ **3** | ~~D~~ **A** | `shared/observability/metrics.py:216` | `_get_database_summary` | ✅ **REFATORADO** |
| 18 | ~~21~~ **3** | ~~D~~ **A** | `shared/observability/metrics.py:311` | `_get_database_summary_async` | ✅ **REFATORADO** |
| 19 | ~~20~~ **6** | ~~C~~ **B** | `core/analysis/indicators/trend.py:187` | `calculate_support_resistance` | ✅ **REFATORADO** |
| 20 | ~~20~~ **3** | ~~C~~ **A** | `core/validators/trading_validator.py:433` | `validate_order_preflight_or_raise` | ✅ **REFATORADO** |
| 21 | **19** | C | `infrastructure/data_fetch/candle_fetcher.py:80` | `fetch_candles` | ⚠️ Pendente |
| 22 | ~~18~~ **4** | ~~C~~ **A** | `core/validators/trading_validator.py:121` | `validate_profit_target` | ✅ **REFATORADO** |
| 23 | **18** | C | `infrastructure/websocket/websocket_manager.py:275` | `_handle_message` | ⚠️ Pendente |
| 24 | ~~17~~ **7** | ~~C~~ **B** | `core/position/position_state.py:289` | `check_exit_conditions` | ✅ **REFATORADO** |
| 25 | **17** | C | `infrastructure/data_fetch/data_fetcher_base.py:451` | `_validate_ticker_data` | ⚠️ Pendente |
| 26 | **17** | C | `infrastructure/data_fetch/ticker_fetcher.py:249` | `fetch_orderbook` |
| 27 | **17** | C | `infrastructure/data_fetch/ticker_fetcher.py:467` | `fetch_24hr_ticker` |
| 28 | **17** | C | `monitoring/unified_monitor.py:131` | `_run_critical_monitoring` |
| 29 | **17** | C | `monitoring/unified_monitor.py:436` | `_check_position_timeout` |
| 30 | ~~15~~ **6** | ~~C~~ **B** | `core/risk/duration_monitor.py:47` | `check_position_duration_alerts` | ✅ **REFATORADO** |
| 31 | ~~15~~ **8** | ~~C~~ **B** | `core/validators/correlation_validator.py:40` | `can_open_position` | ✅ **REFATORADO** |
| 32 | ~~13~~ **2** | ~~C~~ **A** | `core/risk/risk_tracker.py:238` | `register_trade_result` | ✅ **REFATORADO** |
| 33 | ~~12~~ **3** | ~~C~~ **A** | `core/risk/risk_tracker.py:131` | `update_balance` | ✅ **REFATORADO** |

---

## Análise por Domínio

### Core (Trading Logic) - 12 métodos complexos

| CC | Método | Arquivo |
|----|--------|---------|
| 37 | `run` | `core/engine/trading_loop_execution.py` |
| 35 | `update_account_balance` | `core/coordinator/trading_coordinator_balance.py` |
| 32 | `validate_fresh_price` | `core/position/position_validator.py` |
| 30 | `_execute_entry` | `core/engine/trading_loop_execution.py` |
| 30 | `_update_symbol_data` | `core/market/market_updater.py` |
| 30 | `analyze_symbol` | `core/signals/signal_analyzer.py` |
| 30 | `_restore_and_validate_positions` | `core/coordinator/trading_coordinator_lifecycle.py` |
| 28 | `get_average_fill_price` | `core/execution/order_executor.py` |
| 24 | `_handle_emergency_close` | `core/coordinator/trading_coordinator_lifecycle.py` |
| 21 | `health_check` | `core/risk/emergency_manager.py` |
| 21 | `get_status` | `core/coordinator/trading_coordinator_orchestration.py` |
| 20 | `calculate_support_resistance` | `core/analysis/indicators/trend.py` |

### Infrastructure - 9 métodos complexos

| CC | Método | Arquivo |
|----|--------|---------|
| 34 | `create_order` | `infrastructure/api/endpoints/trading_endpoints.py` |
| 29 | `_validate_candles_data` | `infrastructure/data_fetch/data_fetcher_base.py` |
| 26 | `fetch_ticker` | `infrastructure/data_fetch/ticker_fetcher.py` |
| 24 | `_validate_orderbook_data` | `infrastructure/data_fetch/data_fetcher_base.py` |
| 19 | `fetch_candles` | `infrastructure/data_fetch/candle_fetcher.py` |
| 18 | `_handle_message` | `infrastructure/websocket/websocket_manager.py` |
| 17 | `_validate_ticker_data` | `infrastructure/data_fetch/data_fetcher_base.py` |
| 17 | `fetch_orderbook` | `infrastructure/data_fetch/ticker_fetcher.py` |
| 17 | `fetch_24hr_ticker` | `infrastructure/data_fetch/ticker_fetcher.py` |

### Services & Monitoring - 5 métodos complexos

| CC | Método | Arquivo |
|----|--------|---------|
| 21 | `_reconcile_state_on_startup` | `services/state_recovery.py` |
| 17 | `_run_critical_monitoring` | `monitoring/unified_monitor.py` |
| 17 | `_check_position_timeout` | `monitoring/unified_monitor.py` |
| 15 | `_get_current_prices` | `monitoring/unified_monitor.py` |
| 12 | `_critical_stop_loss` | `monitoring/unified_monitor.py` |

### Shared - 2 métodos complexos

| CC | Método | Arquivo |
|----|--------|---------|
| 21 | `_get_database_summary` | `shared/observability/metrics.py` |
| 21 | `_get_database_summary_async` | `shared/observability/metrics.py` |

---

## Priorização para Refatoração

### 🔴 Prioridade Alta (CC ≥ 30) - 7 métodos

Estes métodos são críticos e devem ser refatorados:

1. ~~**`run`** (CC: 37) - Loop principal de trading~~ ✅ **CONCLUÍDO**
2. **`update_account_balance`** (CC: 35) - Gestão de balance
3. **`create_order`** (CC: 34) - Criação de ordens
4. **`validate_fresh_price`** (CC: 32) - Validação de preços
5. **`_execute_entry`** (CC: 30) - Execução de entrada
6. **`_update_symbol_data`** (CC: 30) - Atualização de mercado
7. **`analyze_symbol`** (CC: 30) - Análise de sinais
8. **`_restore_and_validate_positions`** (CC: 30) - Recuperação de posições

### 🟠 Prioridade Média (CC 21-29) - 10 métodos

Estes métodos devem ser considerados para refatoração:

1. **`_validate_candles_data`** (CC: 29)
2. **`get_average_fill_price`** (CC: 28)
3. **`fetch_ticker`** (CC: 26)
4. **`_handle_emergency_close`** (CC: 24)
5. **`_validate_orderbook_data`** (CC: 24)
6. **`health_check`** (CC: 21)
7. **`get_status`** (CC: 21)
8. **`_reconcile_state_on_startup`** (CC: 21)
9. **`_get_database_summary`** (CC: 21)
10. **`_get_database_summary_async`** (CC: 21)

### ⚠️ Prioridade Baixa (CC 11-20) - 12 métodos

Monitorar e refatorar quando conveniente.

---

## Meta de Qualidade

| Métrica | Inicial | Atual | Meta |
|---------|---------|-------|------|
| Métodos CC ≥ 30 | 8 | **3** | 0 |
| Métodos CC 21-29 | 10 | 10 | ≤ 5 |
| Métodos CC 11-20 | 12 | 12 | ≤ 10 |
| CC máximo | 37 | **30** | ≤ 15 |

---

## Changelog

### 2025-12-02 (Atualização 5)
- ✅ **REFATORADO:** `validate_fresh_price()` em `position_validator.py`
  - CC reduzido de **32 (E)** para **6 (B)** (-81%)
  - Extraídos 6 métodos auxiliares + 3 constantes de classe:
    - `STALE_THRESHOLDS` (dict - thresholds por reason)
    - `DEFAULT_STALE_THRESHOLD` (Decimal)
    - `CRITICAL_REASONS` (frozenset)
    - `_fetch_price_from_source()` (CC: 10)
    - `_fetch_price_with_fallback()` (CC: 4)
    - `_get_stale_threshold()` (CC: 1)
    - `_build_validation_result()` (CC: 1)
    - `_build_fallback_result()` (CC: 6)
  - Média de complexidade do arquivo: ~32 → **4.73 (A)**
- Status: 14 testes passando (3 falhas pré-existentes), sem regressões
- **3 Bots Comerciais Analisados:** Freqtrade (45k⭐), Hummingbot (15k⭐), Jesse (7k⭐)

### 2025-12-02 (Atualização 4)
- ✅ **REFATORADO:** `create_order()` em `trading_endpoints.py`
  - CC reduzido de **34 (E)** para **2 (A)** (-94%)
  - Extraídos 5 métodos auxiliares + 3 constantes de classe:
    - `VALID_ORDER_TYPES` (frozenset)
    - `VALID_SIDES` (frozenset)
    - `NUMERIC_PARAMS` (frozenset)
    - `_validate_required_order_params()` (CC: 7)
    - `_validate_numeric_order_param()` (CC: 5)
    - `_validate_order_type_rules()` (CC: 10)
    - `_prepare_order_params_for_api()` (CC: 3)
    - `_execute_order()` (CC: 2)
  - Média de complexidade do arquivo: ~34 → **5.27 (B)**
- Status: Código testado manualmente, sem regressões
- **3 Bots Comerciais Analisados:** Freqtrade (45k⭐), Hummingbot (15k⭐), OctoBot (5k⭐)

### 2025-12-02 (Atualização 3)
- ✅ **REFATORADO:** `update_account_balance()` em `trading_coordinator_balance.py`
  - CC reduzido de **35 (E)** para **7 (B)** (-80%)
  - Extraídos 9 métodos auxiliares:
    - `_should_skip_update()` (CC: 1)
    - `_validate_components()` (CC: 3)
    - `_fetch_usdt_balance()` (CC: 8)
    - `_sync_positions_if_needed()` (CC: 5)
    - `_validate_price()` (CC: 6)
    - `_validate_quantity()` (CC: 6)
    - `_calculate_position_value()` (CC: 6)
    - `_calculate_positions_total_value()` (CC: 6)
    - `_update_risk_and_state()` (CC: 3)
  - Média de complexidade do arquivo: ~35 → **4.69 (A)**
- Status: 18 testes passando, sem regressões

### 2025-12-02 (Atualização 2)
- ✅ **REFATORADO:** `_execute_entry()` em `trading_loop_execution.py`
  - CC reduzido de **30 (D)** para **9 (B)** (-70%)
  - Extraídos 7 métodos auxiliares:
    - `_validate_entry_inputs()` (CC: 4)
    - `_check_entry_rate_limits()` (CC: 2)
    - `_acquire_entry_and_check_position()` (CC: 3)
    - `_check_risk_gates()` (CC: 9)
    - `_validate_account_balance()` (CC: 8)
    - `_record_successful_entry()` (CC: 6)
    - `_record_failed_entry()` (CC: 2)
  - Média de complexidade do arquivo: 5.67 → **4.56 (A)**
- Status: 33 testes passando, sem regressões

### 2025-12-02 (Atualização 1)
- ✅ **REFATORADO:** `run()` em `trading_loop_execution.py`
  - CC reduzido de **37 (E)** para **6 (B)** (-83.8%)
  - Extraídos 12 métodos auxiliares
  - Bug corrigido em `unified_monitor.py` (asyncio.Lock com `with` síncrono)
  - Média de complexidade do arquivo: 18.6 → 5.67
- Corrigido acoplamento: `unified_monitor.py` agora usa interface pública `apply_symbol_cooldown()`
- Adicionados 33 testes para o trading loop
- Corrigidos 10 testes em `test_symbol_validation.py` (API desatualizada)
- Status de testes: **366 passando**, 47 falhando (pré-existentes)

### 2025-12-02
- Documento criado
- 30 métodos identificados com CC ≥ 11
- 8 métodos com prioridade alta (CC ≥ 30)
- 10 métodos com prioridade média (CC 21-29)

---

## Próximos Passos Recomendados

1. ~~**Refatorar `_execute_entry()`** (CC: 30)~~ ✅ CONCLUÍDO
2. ~~**Refatorar `update_account_balance()`** (CC: 35)~~ ✅ CONCLUÍDO
3. ~~**Refatorar `create_order()`** (CC: 34)~~ ✅ CONCLUÍDO
4. ~~**Refatorar `validate_fresh_price()`** (CC: 32)~~ ✅ CONCLUÍDO
5. **Refatorar `_update_symbol_data()`** (CC: 30) - Próximo na lista
6. **Corrigir testes pré-existentes** - 47 testes com problemas de:
   - Mocks desatualizados
   - APIs alteradas (validators)
   - Injeção de dependências faltando (PositionTracker)
7. **Continuar ranking** - `analyze_symbol()` (CC: 30)
