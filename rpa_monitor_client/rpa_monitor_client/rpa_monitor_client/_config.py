import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class RPAConfig:
    rpa_id: str
    host: str
    port: Optional[int]
    region: str = "default"
    heartbeat_interval: int = 5
    transport: str = "tcp"  # "tcp" ou "ws"


def load_from_env() -> RPAConfig:
    """
    Lê configurações de ambiente.

    Com TCP (padrão):
      RPA_MONITOR_TRANSPORT=tcp
      RPA_MONITOR_HOST=meu-servidor.local
      RPA_MONITOR_PORT=5051

    Com WebSocket:
      RPA_MONITOR_TRANSPORT=ws
      RPA_MONITOR_HOST=wss://meu-repl.replit.dev/ws
      (RPA_MONITOR_PORT opcional)

    Comum:
      RPA_MONITOR_ID
      RPA_MONITOR_REGION (opcional)
      RPA_MONITOR_HEARTBEAT (opcional)
    """
    rpa_id = os.getenv("RPA_MONITOR_ID", "").strip()
    host = os.getenv("RPA_MONITOR_HOST", "").strip()
    transport = os.getenv("RPA_MONITOR_TRANSPORT", "tcp").strip().lower()

    if not rpa_id or not host:
        raise RuntimeError(
            "Variáveis obrigatórias: RPA_MONITOR_ID, RPA_MONITOR_HOST "
            "(e RPA_MONITOR_PORT se transport=tcp)."
        )

    port: Optional[int] = None
    if transport == "tcp":
        port_str = os.getenv("RPA_MONITOR_PORT", "").strip()
        if not port_str:
            raise RuntimeError("RPA_MONITOR_PORT é obrigatório quando transport=tcp")
        port = int(port_str)
    else:
        port_str = os.getenv("RPA_MONITOR_PORT", "").strip()
        port = int(port_str) if port_str else None

    region = os.getenv("RPA_MONITOR_REGION", "default").strip()
    heartbeat = int(os.getenv("RPA_MONITOR_HEARTBEAT", "5"))

    return RPAConfig(
        rpa_id=rpa_id,
        host=host,
        port=port,
        region=region,
        heartbeat_interval=heartbeat,
        transport=transport,
    )
