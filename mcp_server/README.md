# Trading Bot MCP Server

MCP (Model Context Protocol) server para análise e monitoramento do Trading Bot.

## Configuração

### Método Recomendado (Claude CLI)

O MCP server já está configurado! Use os comandos abaixo para gerenciar:

```bash
# Listar servidores configurados
claude mcp list

# Ver detalhes do servidor
claude mcp get trading-bot-analysis

# Remover servidor (se necessário)
claude mcp remove trading-bot-analysis -s local
```

### Adicionar em Outro Projeto

Para adicionar este MCP server em outro projeto:

```bash
cd /path/to/outro/projeto
claude mcp add --transport stdio trading-bot-analysis -- \
  /home/mateus_ubuntu/trading_bot/venv/bin/python -m mcp_server.server
```

## Execução Manual

Para testar o MCP server manualmente:

```bash
source venv/bin/activate
python -m mcp_server.server
```

## Ferramentas Disponíveis (26 tools)

### Core & Health (8 tools)
- **get_complete_analysis**: Análise completa do bot (saúde, trades, posições, performance, mercado, erros)
- **get_bot_health**: Status de saúde via Flow Tracker (22 componentes monitorados)
- **get_position_tracker_state**: Estado do position tracker em memória
- **get_user_stream_status**: Status do WebSocket user stream
- **investigate_trade**: Investigação profunda de um trade específico
- **find_problem_trades**: Encontrar trades com problemas (PnL negativo, emergency stops)
- **search_logs**: Buscar nos logs por texto com filtro de nível
- **get_reconciliation_status**: Status de reconciliação de ordens

### Analytics & Errors (5 tools)
- **get_error_summary**: Resumo de erros por nível, módulo e tempo
- **analyze_error_patterns**: Análise de padrões de erro (Sentry-like)
- **get_error_timeline**: Timeline de erros com buckets horários
- **detect_error_spikes**: Detectar picos/anomalias de erros
- **get_signal_breakdown**: Breakdown de sinais de trading

### Performance (2 tools)
- **get_symbol_performance**: Performance por símbolo (win rate, PnL)
- **get_monthly_performance**: Projeção de performance mensal

### Validators & Safety (7 tools)
- **validate_circuit_breaker_persistence**: Validar persistência do circuit breaker
- **validate_risk_limits**: Validar compliance com limites de risco
- **validate_data_integrity**: Validar integridade dos dados
- **validate_track_before_send**: Validar padrão Track BEFORE Send
- **get_emergency_stops_summary**: Resumo de emergency stops
- **get_api_recovery_status**: Status de recovery da API
- **get_idempotency_status**: Status de idempotência

### Advanced (4 tools)
- **get_websocket_diagnostics**: Diagnóstico de WebSocket
- **get_client_order_id_lookup**: Lookup de ordem por client_order_id
- **get_position_history**: Histórico de posições para um símbolo
- **get_db_write_metrics**: Métricas de escrita no banco
- **run_testnet_validation**: Validar configuração de testnet
- **get_market_analysis**: Análise de mercado (cooldowns, símbolos disponíveis)

## Arquitetura

```
mcp_server/
├── server.py              # MCP server principal
├── core/                  # Clientes para acesso a dados
│   ├── db_client.py      # SQLite database
│   ├── flow_client.py    # Unix socket (Flow Tracker)
│   ├── log_parser.py     # Parser de logs JSON
│   ├── state_client.py   # State em memória
│   └── websocket_client.py
├── tools/                 # Implementação das tools
│   ├── core_health.py    # Tools principais
│   ├── analytics_errors.py
│   ├── validators_safety.py
│   └── advanced_optional.py
└── models/               # Modelos de dados
    ├── health.py
    ├── trade.py
    ├── position.py
    ├── error.py
    └── ...
```

## Uso no Claude Code

Após configurar, use o prefixo `mcp__` para acessar as tools:

```
mcp__trading-bot-analysis__get_complete_analysis
mcp__trading-bot-analysis__investigate_trade
mcp__trading-bot-analysis__get_bot_health
```

Ou use o comando `/mcp` para listar os servidores disponíveis.

## Requisitos

- Python 3.13
- MCP SDK instalado (`pip install mcp`)
- Bot em execução (para algumas ferramentas que dependem de Flow Tracker ou State)

## Notas

- **CRITICAL**: Use MCP server para bot analysis - NEVER access database/logs directly (conforme CLAUDE.md)
- Algumas ferramentas requerem que o bot esteja em execução
- Flow Tracker usa Unix Socket em `/tmp/flow_tracker.sock`
- Logs em formato JSON em `logs/`
- Database SQLite em `data/trading_bot.db`
