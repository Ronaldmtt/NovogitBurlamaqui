from io import BytesIO
from typing import Optional

from ._client import _RPAMonitorClient

_client_instance: Optional[_RPAMonitorClient] = None


def set_client(client: Optional[_RPAMonitorClient]) -> None:
    global _client_instance
    _client_instance = client


class _RPALogProxy:
    """Proxy simples para expor métodos de log e envio de imagem."""

    def info(self, msg: str, regiao: Optional[str] = None) -> None:
        if _client_instance:
            _client_instance.log_info(msg, regiao=regiao)

    def warn(self, msg: str, regiao: Optional[str] = None) -> None:
        if _client_instance:
            _client_instance.log_warn(msg, regiao=regiao)

    def error(
        self,
        msg: str,
        exc: Optional[BaseException] = None,
        regiao: Optional[str] = None,
    ) -> None:
        if _client_instance:
            _client_instance.log_error(msg, exc=exc, regiao=regiao)

    def image(
        self,
        image_bytes: bytes,
        content_type: str = "image/png",
        filename: str = "screenshot.png",
        regiao: Optional[str] = None,
        nivel: str = "INFO",
    ) -> None:
        """
        Envia uma imagem (OP=04) usando bytes já capturados pelo código do RPA.

        Exemplo:
            with open("tela.png", "rb") as f:
                rpa_log.image(f.read(), filename="tela.png")
        """
        if _client_instance:
            _client_instance.send_image(
                image_bytes=image_bytes,
                content_type=content_type,
                filename=filename,
                regiao=regiao,
                nivel=nivel,
            )

    def screenshot(
        self,
        content_type: str = "image/png",
        filename: str = "screenshot.png",
        regiao: Optional[str] = "screenshot",
        nivel: str = "INFO",
    ) -> None:
        """
        Captura a tela inteira e envia como imagem (OP=04).

        Requer 'pyautogui' instalado no projeto do RPA:
            pip install pyautogui

        Exemplo de uso:
            try:
                ...
            except Exception as e:
                rpa_log.error("Erro X", exc=e)
                rpa_log.screenshot(filename="erro_x.png")
        """
        if not _client_instance:
            print(
                "[rpa-monitor-client] Nenhum cliente configurado para enviar screenshot."
            )
            return

        try:
            import pyautogui  # dependência opcional, só usada aqui
        except ImportError:
            print(
                "[rpa-monitor-client] pyautogui não está instalado. "
                "Instale com: pip install pyautogui"
            )
            return

        try:
            img = pyautogui.screenshot()
            buffer = BytesIO()
            img.save(buffer, format="PNG")
            image_bytes = buffer.getvalue()

            _client_instance.send_image(
                image_bytes=image_bytes,
                content_type=content_type,
                filename=filename,
                regiao=regiao,
                nivel=nivel,
            )
        except Exception as e:
            print(f"[rpa-monitor-client] Falha ao capturar/enviar screenshot: {e}")


rpa_log = _RPALogProxy()
