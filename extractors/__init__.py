# -*- coding: utf-8 -*-
"""
Interface pública estável do pacote `extractors`.

Exporta sempre:
    extract_text_from_pdf
    run_extraction_from_text
    run_extraction_from_file
    run_pipeline            (alias seguro; usa o pipeline se disponível)
    run_pipeline_from_text  (quando existir)
"""

# 1) Entradas do pipeline (quando disponíveis)
try:
    from .pipeline import extract_text_from_pdf, run_extraction_from_file, run_pipeline_from_text
except Exception:
    extract_text_from_pdf = None
    run_extraction_from_file = None
    run_pipeline_from_text = None

# 2) Fonte da verdade do extrator de texto
from juridico_inteligente.extract import run_extraction_from_text

# 3) Alias robusto: tenta o pipeline, senão cai para o extrator puro
def run_pipeline(*args, **kwargs):
    """
    Alias robusto: tenta o pipeline; se não aceitar certos kwargs
    (ex.: brand_map_path), reenvia sem eles; cai para o extrator puro se precisar.
    """
    from .pipeline import run_pipeline_from_text, run_extraction_from_text  # garantimos import local

    # kwargs que nossos extratores realmente entendem
    allowed = {"celula_options"}  # adicione aqui se surgir outro kw suportado
    safe_kwargs = {k: v for k, v in kwargs.items() if k in allowed}

    if callable(run_pipeline_from_text):
        try:
            return run_pipeline_from_text(*args, **safe_kwargs)
        except TypeError:
            pass  # assinatura não bate: cai para o extrator puro

    return run_extraction_from_text(*args, **safe_kwargs)


__all__ = [
    "extract_text_from_pdf",
    "run_extraction_from_text",
    "run_extraction_from_file",
    "run_pipeline",
    "run_pipeline_from_text",
]
