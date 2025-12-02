# extractors/cadastro.py
from __future__ import annotations
from typing import Dict, Any

from .pipeline import run_extraction_from_text  # usa a versão correta do pipeline

def parse_pdf_text(texto: str, filename: str | None = None, **kwargs) -> Dict[str, Any]:
    data = run_extraction_from_text(texto or "", filename=filename)
    # garante chaves que o template espera
    defaults = {
        "comarca": "",
        "numero_orgao": "",
        "instancia": "",
        "celula": "",
        "parte_interessada": "",
        "cpf_cnpj_parte_adversa": "",
    }
    for k, v in defaults.items():
        data.setdefault(k, v)
    return data
