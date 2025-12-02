import os
import time
import random

from rpa_monitor_client import auto_setup_rpa_monitor, setup_rpa_monitor, rpa_log


def main():
    use_env = os.getenv("USE_ENV_CONFIG", "0") == "1"

    if use_env:
        print("[FAKE RPA] Usando auto_setup_rpa_monitor() a partir do .env")
        auto_setup_rpa_monitor()
    else:
        print("[FAKE RPA] Usando setup_rpa_monitor() com parâmetros fixos (WebSocket)")
        setup_rpa_monitor(
            rpa_id="RPA-Juridico",
            host="wss://7e7d7d39-e41c-4573-8d51-d1d8b03590af-00-sr2xlgswtkl0.worf.replit.dev/ws",
            port=None,
            region="Sudeste",
            transport="ws",
        )

    rpa_log.info("RPA-Juridico iniciado")

    contador = 0
    ultimo_print = 0.0
    intervalo_screenshot = 20.0  # 20 segundos

    while True:
        contador += 1
        rpa_log.info(f"Loop {contador}: executando rotina fake")

        # Simula erro às vezes e, quando acontecer, envia screenshot junto
        if random.random() < 0.3:
            try:
                1 / 0
            except Exception as e:
                rpa_log.error("Erro simulado no fake RPA", exc=e, regiao="processo_fake")
                # testa envio de screenshot na exceção
                rpa_log.screenshot(
                    filename=f"erro_{int(time.time())}.png",
                    regiao="erro_processamento",
                )
                print("[FAKE RPA] Screenshot enviada após erro.")

        # Screenshot periódica a cada 20s (opcional, só pra teste)
        agora = time.time()
        if agora - ultimo_print >= intervalo_screenshot:
            rpa_log.screenshot(
                filename=f"screen_{int(agora)}.png",
                regiao="screenshot_periodica",
            )
            print("[FAKE RPA] Screenshot periódica enviada.")
            ultimo_print = agora

        time.sleep(5)


if __name__ == "__main__":
    main()
