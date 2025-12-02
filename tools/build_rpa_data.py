import json, os, sys
from pathlib import Path
from juridico_inteligente import extract_from_pdf

def main():
    if len(sys.argv) < 3:
        print("Usage: python -m tools.build_rpa_data <pdf_path> <out_json_path>")
        sys.exit(2)
    pdf_path = sys.argv[1]
    out_json = sys.argv[2]
    brand_map = os.environ.get("BRAND_MAP_JSON", "config/brand_map.json")
    data = extract_from_pdf(pdf_path, brand_map_path=brand_map)

    mapped = {
        "numero_processo": data.get("numero_processo", ""),
        "tipo_processo": "eletrônico",
        "sistema_eletronico": "PJE",
        "sub_area_direito": data.get("sub_area_direito", ""),
        "assunto": data.get("assunto", ""),
        "tipo_acao": data.get("tipo_acao_sugerido", ""),
        "valor_causa": data.get("valor_causa_raw", ""),
        "cliente_grupo": data.get("cliente_grupo", ""),
        "cliente": data.get("cliente", ""),
        "posicao_parte_interessada": data.get("posicao_cliente",""),
        "parte_adversa_tipo": data.get("parte_adversa_tipo",""),
        "parte_adversa": data.get("parte_adversa_nome",""),
        "grupo": data.get("cliente_grupo",""),
        "parte_interessada": data.get("cliente",""),
        "parte_adversa_nome": data.get("parte_adversa_nome","")
    }
    Path(out_json).parent.mkdir(parents=True, exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(mapped, f, ensure_ascii=False, indent=2)
    print(f"Wrote {out_json}")

if __name__ == "__main__":
    main()
