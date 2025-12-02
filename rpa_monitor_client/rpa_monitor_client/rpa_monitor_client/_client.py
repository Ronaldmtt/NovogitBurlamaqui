import asyncio
import base64
import queue
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Optional

try:
    import websockets  # type: ignore
except ImportError:
    websockets = None  # Só será usado se transport="ws"


class _RPAMonitorClient:
    """
    Cliente interno para enviar heartbeats, logs e imagens.

    Suporta dois transports:
      - TCP (padrão): host/port -> socket TCP puro
      - WebSocket: host = URL completa (ws:// ou wss://), port opcional

    Protocolo de linha:
      V1|OP|RPA_ID|TS|REGIAO|NIVEL|PAYLOAD|NONCE|SIG

    OP codes usados pela lib:
      01 = HEARTBEAT
      02 = LOG
      04 = IMAGE
    """

    def __init__(
        self,
        rpa_id: str,
        host: str,
        port: Optional[int],
        region: str = "default",
        heartbeat_interval: int = 5,
        transport: str = "tcp",
    ):
        self.rpa_id = rpa_id
        self.host = host
        self.port = port
        self.region = region
        self.heartbeat_interval = heartbeat_interval
        self.transport = (transport or "tcp").lower()

        # --- TCP state ---
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()

        # --- WS state ---
        self._ws_url: Optional[str] = None
        self._ws_queue: "queue.Queue[str]" = queue.Queue()
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_running = False

        # --- common state ---
        self._running = False
        self._heartbeat_thread: Optional[threading.Thread] = None

        if self.transport == "ws":
            if not websockets:
                raise RuntimeError(
                    "Transporte 'ws' requer o pacote 'websockets'. "
                    "Instale com: pip install websockets"
                )
            # host é a URL completa ws://.../ws ou wss://.../ws
            self._ws_url = self.host

    # ==========================================================
    # Public lifecycle
    # ==========================================================
    def start(self) -> bool:
        """
        Inicia o cliente:
          - TCP: conecta e inicia heartbeat
          - WS: inicia loop WS + heartbeat
        """
        if self._running:
            print("[rpa-monitor-client] Cliente já está rodando")
            return True

        print(f"[rpa-monitor-client] Iniciando RPA: {self.rpa_id}")
        print(f"[rpa-monitor-client] Transport: {self.transport}")
        print(
            f"[rpa-monitor-client] Destino: {self.host}"
            + (f":{self.port}" if self.port and self.transport == "tcp" else "")
        )

        if self.transport == "tcp":
            if not self._connect_tcp():
                print("[rpa-monitor-client] Falha na conexão TCP inicial")
                # ainda assim marca running pra tentar reconectar nos envios
        elif self.transport == "ws":
            self._start_ws_loop()
        else:
            raise ValueError(f"Transport desconhecido: {self.transport}")

        self._running = True

        # Thread de heartbeat (comum aos dois)
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
        )
        self._heartbeat_thread.start()

        # Log de inicialização
        self.log_info(f"RPA {self.rpa_id} iniciado")

        return True

    def stop(self) -> None:
        """Encerra heartbeat e transport correspondente."""
        if not self._running:
            return

        print("[rpa-monitor-client] Parando cliente...")
        self._running = False

        # Log de finalização
        try:
            self.log_info(f"RPA {self.rpa_id} finalizado")
        except Exception:
            pass

        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=5)

        if self.transport == "tcp":
            with self._lock:
                if self._sock:
                    try:
                        self._sock.close()
                    except Exception:
                        pass
                    self._sock = None
        elif self.transport == "ws":
            self._ws_running = False
            if self._ws_thread and self._ws_thread.is_alive():
                # coloca algo na fila pra destravar o get()
                self._ws_queue.put("")
                self._ws_thread.join(timeout=5)

        print("[rpa-monitor-client] Cliente parado")

    # ==========================================================
    # TCP transport
    # ==========================================================
    def _connect_tcp(self) -> bool:
        """Abre (ou reabre) a conexão TCP."""
        if not self.port:
            print("[rpa-monitor-client] Porta TCP não configurada.")
            return False

        try:
            # fecha anterior
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass

            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10.0)
            s.connect((self.host, self.port))
            with self._lock:
                self._sock = s
            print(
                f"[rpa-monitor-client] TCP conectado em "
                f"{self.host}:{self.port} ({self.rpa_id})"
            )
            return True
        except Exception as e:
            print(f"[rpa-monitor-client] Erro ao conectar TCP: {e}")
            with self._lock:
                self._sock = None
            return False

    def _ensure_tcp_connected(self) -> bool:
        with self._lock:
            ok = self._sock is not None
        if ok:
            return True
        return self._connect_tcp()

    # ==========================================================
    # WebSocket transport
    # ==========================================================
    def _start_ws_loop(self) -> None:
        """Inicia thread com loop asyncio para WebSocket."""
        if not self._ws_url:
            raise RuntimeError("URL WebSocket não configurada.")

        self._ws_running = True
        self._ws_thread = threading.Thread(
            target=self._ws_loop_thread,
            daemon=True,
        )
        self._ws_thread.start()
        print(f"[rpa-monitor-client] WS loop iniciado em {self._ws_url}")

    def _ws_loop_thread(self) -> None:
        asyncio.run(self._ws_loop())

    async def _ws_loop(self) -> None:
        """Loop principal do WebSocket: reconecta e envia linhas da fila."""
        assert self._ws_url is not None
        url = self._ws_url

        while self._ws_running:
            try:
                print(f"[rpa-monitor-client] Conectando WebSocket em {url} ...")
                async with websockets.connect(url) as ws:  # type: ignore[attr-defined]
                    print("[rpa-monitor-client] WebSocket conectado.")
                    while self._ws_running:
                        # bloqueia até ter algo para enviar
                        loop = asyncio.get_running_loop()
                        line = await loop.run_in_executor(None, self._ws_queue.get)
                        if not self._ws_running:
                            break
                        if not line:
                            continue
                        await ws.send(line)
            except Exception as e:
                print(
                    "[rpa-monitor-client] WS desconectado "
                    f"({e}), tentando reconectar em 3s..."
                )
                await asyncio.sleep(3.0)

    # ==========================================================
    # Heartbeat + logs (comum)
    # ==========================================================
    def _heartbeat_loop(self) -> None:
        """Envia heartbeat (OP=01) periodicamente."""
        print(
            "[rpa-monitor-client] Heartbeat iniciado "
            f"(intervalo: {self.heartbeat_interval}s)"
        )
        while self._running:
            try:
                self._send_message(
                    op="01",
                    nivel="INFO",
                    payload="alive",
                    regiao=self.region,
                )
            except Exception as e:
                print(f"[rpa-monitor-client] Falha no heartbeat: {e}")
            time.sleep(self.heartbeat_interval)

    def log(
        self,
        mensagem: str,
        nivel: str = "INFO",
        regiao: Optional[str] = None,
    ) -> None:
        try:
            self._send_message(
                op="02",
                nivel=nivel,
                payload=mensagem,
                regiao=regiao or self.region,
            )
        except Exception as e:
            print(f"[rpa-monitor-client] Falha ao enviar log: {e}")

    def log_error(
        self,
        mensagem: str,
        exc: Optional[BaseException] = None,
        regiao: Optional[str] = None,
    ) -> None:
        txt = mensagem
        if exc is not None:
            txt = f"{mensagem} | {type(exc).__name__}: {exc}"
        self.log(txt, nivel="ERROR", regiao=regiao)

    def log_warn(self, mensagem: str, regiao: Optional[str] = None) -> None:
        self.log(mensagem, nivel="WARN", regiao=regiao)

    def log_info(self, mensagem: str, regiao: Optional[str] = None) -> None:
        self.log(mensagem, nivel="INFO", regiao=regiao)

    # ---------- IMAGEM (OP=04) ----------
    def send_image(
        self,
        image_bytes: bytes,
        content_type: str = "image/png",
        filename: str = "screenshot.png",
        regiao: Optional[str] = None,
        nivel: str = "INFO",
    ) -> None:
        """
        Envia uma imagem para o servidor no formato:

        V1|04|RPA_ID|TS|REGIAO|NIVEL|content_type=...;filename=...;base64=<BASE64_DATA>||

        - image_bytes: bytes da imagem (PNG/JPEG/etc)
        - content_type: ex. 'image/png'
        - filename: nome sugerido
        """
        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = (
            f"content_type={content_type};"
            f"filename={filename};"
            f"base64={b64}"
        )

        self._send_message(
            op="04",
            nivel=nivel,
            payload=payload,
            regiao=regiao or self.region,
        )

    # ==========================================================
    # Envio de mensagem (decide TCP x WS)
    # ==========================================================
    def _send_message(
        self,
        op: str,
        nivel: str,
        payload: str,
        regiao: Optional[str] = None,
        nonce: str = "",
        sig: str = "",
    ) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        reg = regiao or self.region
        line = f"V1|{op}|{self.rpa_id}|{ts}|{reg}|{nivel}|{payload}|{nonce}|{sig}\n"

        if self.transport == "ws":
            # Só enfileira: o loop WS envia e reconecta se precisar
            self._ws_queue.put(line)
            return

        # --- caminho TCP ---
        def _do_send() -> None:
            with self._lock:
                if not self._sock:
                    raise RuntimeError("Socket TCP não conectado")
                self._sock.sendall(line.encode("utf-8"))

        # garante conexão
        if not self._ensure_tcp_connected():
            raise RuntimeError("Não foi possível conectar ao servidor TCP")

        try:
            _do_send()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            print(
                "[rpa-monitor-client] Conexão TCP perdida "
                f"({e}), tentando reconectar..."
            )
            if not self._connect_tcp():
                print(
                    "[rpa-monitor-client] Reconexão TCP falhou, mensagem perdida."
                )
                return
            try:
                _do_send()
                print(
                    "[rpa-monitor-client] Mensagem reenviada após reconexão TCP."
                )
            except Exception as e2:
                print(
                    "[rpa-monitor-client] Falha ao reenviar após reconexão TCP: "
                    f"{e2}"
                )
