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
        print("[FAKE RPA] Usando setup_rpa_monitor() com parâmetros fixos")
        setup_rpa_monitor(
            rpa_id="RPA-FAKE-001",
            host="127.0.0.1",
            port=5051,
            region="FAKE_RPA_TESTE",
        )

    rpa_log.info("Fake RPA iniciado")

    contador = 0
    while True:
        contador += 1
        rpa_log.info(f"Loop {contador}: executando rotina fake")

        if random.random() < 0.3:
            try:
                1 / 0
            except Exception as e:
                rpa_log.error("Erro simulado no fake RPA", exc=e)

        time.sleep(10)


if __name__ == "__main__":
    main()
