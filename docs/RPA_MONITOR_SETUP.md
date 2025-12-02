# Configuração do RPA Monitor Client

Este documento explica como configurar o monitoramento remoto do RPA usando o `rpa-monitor-client`.

## O que é o RPA Monitor?

O RPA Monitor é um sistema de monitoramento remoto que permite:
- **Heartbeat automático**: Envia sinais periódicos indicando que o RPA está ativo
- **Logs centralizados**: Todos os logs do RPA são enviados para servidor remoto
- **Screenshots**: Capturas de tela são enviadas automaticamente para análise
- **Rastreamento de erros**: Erros e exceções são registrados com stack trace completo

## Como funciona a integração?

A integração foi feita de forma **não-invasiva** e **opcional**:
- ✅ **Não quebra nada**: Se desabilitado, sistema funciona normalmente
- ✅ **Compatível**: Todos os logs locais continuam funcionando
- ✅ **Automático**: Uma vez configurado, funciona automaticamente
- ✅ **Sem código adicional**: Integração transparente via wrapper da função `log()`

## Configuração

### 1. Variáveis de Ambiente

Adicione as seguintes variáveis ao arquivo `.env`:

```bash
# Habilitar monitoramento (true/false)
RPA_MONITOR_ENABLED=true

# Identificador único do RPA
RPA_MONITOR_ID=RPA-FGbularmaci-5

# Host do servidor de monitoramento (URL WebSocket completa: wss://...)
RPA_MONITOR_HOST=wss://7e7d7d39-e41c-4573-8d51-d1d8b03590af-00-sr2xlgswtkl0.worf.replit.dev/ws

# Porta (ignorado para WebSocket - deixe vazio ou 0)
RPA_MONITOR_PORT=0

# Região/Sistema identificador
RPA_MONITOR_REGION=Teste RPA Juridico
```

### 2. Verificar Instalação

O pacote `rpa-monitor-client` já está instalado em modo editável:

```bash
pip list | grep rpa-monitor
# Deve mostrar: rpa-monitor-client 0.1.0
```

### 3. Testar Conexão

Execute o RPA normalmente. Se o monitor estiver habilitado, você verá no log:

```
[rpa-monitor-client] Conectando em wss://... (RPA-FGbularmaci-5)
[rpa-monitor-client] ✅ Conectado via WebSocket: wss://...
[rpa] 2025-11-19T17:30:00 [MONITOR] ✅ RPA Monitor conectado via WebSocket: RPA-FGbularmaci-5 @ wss://... (região: Teste RPA Juridico)
```

## Funcionamento Automático

Uma vez configurado, o monitor funciona automaticamente:

### Logs
Todos os logs locais são automaticamente enviados para o servidor:

```python
log("Iniciando processo #123")  # ← Enviado local + remoto automaticamente
```

### Erros
Erros são capturados com stack trace completo:

```python
try:
    executar_rpa()
except Exception as e:
    log_error_to_monitor("Erro crítico", exc=e)  # ← Envia exceção completa
```

### Screenshots
Screenshots de erro são enviados automaticamente:

```python
send_screenshot_to_monitor(png_path, region="RPA_ERROR")
```

## Desabilitar Monitoramento

Para desabilitar completamente o monitoramento:

```bash
# No arquivo .env
RPA_MONITOR_ENABLED=false
```

Ou simplesmente remova/comente as variáveis `RPA_MONITOR_*`.

## Arquitetura de Integração

```
┌─────────────────────────────────────────────────────────────┐
│ rpa.py                                                      │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ execute_rpa(process_id)                              │  │
│  │   │                                                  │  │
│  │   ├─ _init_rpa_monitor()  ← Inicializa WebSocket   │  │
│  │   │                                                  │  │
│  │   └─ run_elaw_login_sync()                          │  │
│  │        │                                             │  │
│  │        └─ log("mensagem")  ← Wrapper automático     │  │
│  │             │                                        │  │
│  │             ├─ LOG.info() ← Log local (sempre)      │  │
│  │             └─ monitor_log.info() ← Log remoto WS  │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                          ↓ WebSocket (wss://)
┌─────────────────────────────────────────────────────────────┐
│ rpa-monitor-client (WebSocket)                              │
│   │                                                         │
│   ├─ Heartbeat automático (a cada 5s) ← OP=01             │
│   ├─ Envio de logs ← OP=02                                 │
│   └─ Envio de screenshots ← Base64                         │
└─────────────────────────────────────────────────────────────┘
                          ↓ wss://servidor/ws
┌─────────────────────────────────────────────────────────────┐
│ Servidor RPA Monitor (WebSocket Server)                    │
│   │                                                         │
│   ├─ Dashboard em tempo real                               │
│   ├─ Histórico de logs                                     │
│   └─ Alertas de falhas                                     │
└─────────────────────────────────────────────────────────────┘
```

## Código Modificado

Os seguintes arquivos foram modificados para suportar o monitor:

### `rpa.py`
- **Imports**: Adicionado `rpa_monitor_client`
- **Configuração**: Variáveis de ambiente `RPA_MONITOR_*`
- **Funções**:
  - `_init_rpa_monitor()`: Inicialização do cliente
  - `log()`: Modificada para enviar também para monitor
  - `send_screenshot_to_monitor()`: Envio de screenshots
  - `log_error_to_monitor()`: Envio de erros com exceções
- **Integração**: `execute_rpa()` chama `_init_rpa_monitor()` automaticamente

## Troubleshooting

### Monitor não conecta
```
[MONITOR] Configuração incompleta (RPA_MONITOR_ID ou RPA_MONITOR_HOST)
```
**Solução**: Verificar se `RPA_MONITOR_ID` e `RPA_MONITOR_HOST` estão configurados

### Monitor não disponível
```
[MONITOR] RPA Monitor Client não disponível
```
**Solução**: Reinstalar o pacote: `pip install -e ./rpa_monitor_client`

### Erro ao enviar logs
O monitor falha silenciosamente e não interrompe a execução do RPA. Verifique:
- Conexão de rede com o servidor
- Servidor de monitoramento está rodando
- Credenciais e URL corretas

## Benefícios

✅ **Visibilidade**: Ver todos os RPAs rodando em tempo real
✅ **Debugging**: Logs centralizados facilitam troubleshooting
✅ **Alertas**: Notificações automáticas de falhas
✅ **Histórico**: Rastreamento completo de execuções
✅ **Performance**: Monitorar tempo de execução e gargalos

---

**Última atualização**: 19 de Novembro de 2025
