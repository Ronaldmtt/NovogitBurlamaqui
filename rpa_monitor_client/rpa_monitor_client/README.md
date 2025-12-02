# rpa-monitor-client

Cliente Python plug-and-play para enviar heartbeats (OP=01) e logs (OP=02)
para o servidor de monitoramento RPA.

## Instalação (local)

```bash
pip install -e ./rpa_monitor_client
```

## Uso básico (config explícita)

```python
from rpa_monitor_client import setup_rpa_monitor, rpa_log

setup_rpa_monitor(
    rpa_id="RPA-CLIENTE-001",
    host="seu-servidor.com",
    port=5051,
    region="SISTEMA_X",
)

rpa_log.info("RPA iniciado")
```

## Uso com variáveis de ambiente

```env
RPA_MONITOR_ID=RPA-CLIENTE-XYZ
RPA_MONITOR_HOST=seu-servidor.com
RPA_MONITOR_PORT=5051
RPA_MONITOR_REGION=MINHA_REGIAO
RPA_MONITOR_HEARTBEAT=5
```

```python
from rpa_monitor_client import auto_setup_rpa_monitor, rpa_log

auto_setup_rpa_monitor()
rpa_log.info("Subiu com auto_setup")
```
