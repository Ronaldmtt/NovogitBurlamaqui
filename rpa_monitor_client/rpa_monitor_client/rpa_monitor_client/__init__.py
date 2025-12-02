from typing import Optional

from ._client import _RPAMonitorClient
from ._config import RPAConfig, load_from_env
from ._logging_api import rpa_log, set_client

__all__ = [
    "setup_rpa_monitor",
    "auto_setup_rpa_monitor",
    "shutdown_rpa_monitor",
    "rpa_log",
]

_client: Optional[_RPAMonitorClient] = None


def setup_rpa_monitor(
    rpa_id: str,
    host: str,
    port: Optional[int],
    region: str = "default",
    heartbeat_interval: int = 5,
    transport: str = "tcp",
) -> None:
    """
    Configuração explícita.

    Exemplo TCP:
        setup_rpa_monitor(
            "RPA-001",
            "meu-servidor.local",
            5051,
            region="SISTEMA_X",
            transport="tcp",
        )

    Exemplo WebSocket (Replit):
        setup_rpa_monitor(
            "RPA-001",
            "wss://meu-repl.replit.dev/ws",
            None,
            region="SISTEMA_X",
            transport="ws",
        )
    """
    cfg = RPAConfig(
        rpa_id=rpa_id,
        host=host,
        port=port,
        region=region,
        heartbeat_interval=heartbeat_interval,
        transport=transport,
    )
    _start_with_config(cfg)


def auto_setup_rpa_monitor() -> None:
    """Configuração automática via variáveis de ambiente."""
    cfg = load_from_env()
    _start_with_config(cfg)


def _start_with_config(cfg: RPAConfig) -> None:
    global _client
    client = _RPAMonitorClient(
        rpa_id=cfg.rpa_id,
        host=cfg.host,
        port=cfg.port,
        region=cfg.region,
        heartbeat_interval=cfg.heartbeat_interval,
        transport=cfg.transport,
    )
    ok = client.start()
    if ok:
        _client = client
        set_client(client)
    else:
        raise RuntimeError("Não foi possível inicializar o cliente de monitoramento")


def shutdown_rpa_monitor() -> None:
    global _client
    if _client is not None:
        _client.stop()
        _client = None
        set_client(None)  # type: ignore[arg-type]
