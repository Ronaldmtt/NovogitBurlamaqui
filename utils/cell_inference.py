# utils/cell_inference.py
from __future__ import annotations
from typing import List, Dict, Optional
import re
import os

# Tentamos importar os utilitários do extrator; se não houver, seguimos resilientes
try:
    from extractors.dictionary import load_dictionary_from_docx  # type: ignore
except Exception:
    def load_dictionary_from_docx(path: str) -> List[Dict[str, str]]:
        return []

try:
    from extractors.brand_map import detect_grupo  # type: ignore
except Exception:
    def detect_grupo(_: str) -> Optional[str]:
        return None

# Locais comuns do DOCX "CLIENTE X CÉLULA"
DEFAULT_DOCX_PATHS = [
    "CLIENTE X CÉLULA.docx",
    "CLIENTE X CÉLULA.DOCX",
    "data/CLIENTE X CÉLULA.docx",
    "data/CLIENTE X CÉLULA.DOCX",
]

def _norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().upper()

def canonicalize_cell_label(label: Optional[str]) -> Optional[str]:
    """
    Normaliza rótulos de célula. Para o GPA (Pão de Açúcar / CBD / Sendas),
    padroniza como 'Trabalhista GPA'.
    """
    if not label:
        return None
    s = _norm(label)
    if re.search(r"\bGPA\b|P[ÃA]O\s+DE\s+A[CÇ]UCAR|CBD|SENDAS", s, re.I):
        return "Trabalhista GPA"
    return label.strip()

def load_alias_rows(paths: Optional[List[str]] = None) -> List[Dict[str, str]]:
    """
    Carrega as linhas do DOCX (CLIENTE / CÉLULA / PARTE INTERESSADA...), se existir.
    Retorna lista de dicts (vazia se arquivo não existir).
    """
    rows: List[Dict[str, str]] = []
    for p in (paths or DEFAULT_DOCX_PATHS):
        try:
            if os.path.isfile(p):
                rows.extend(load_dictionary_from_docx(p))
        except Exception:
            continue
    return rows

def build_alias_index(rows: List[Dict[str, str]]) -> Dict[str, str]:
    """
    Constrói um dicionário {CLIENTE_NORMALIZADO -> CÉLULA_CANÔNICA}.
    """
    idx: Dict[str, str] = {}
    for r in rows or []:
        client = _norm(r.get("CLIENTE") or r.get("Cliente") or r.get("cliente"))
        cell = r.get("CÉLULA") or r.get("Celula") or r.get("Célula") or r.get("celula")
        cell = canonicalize_cell_label(cell) or cell
        if client and cell:
            idx[client] = cell
    return idx

def guess_cell_from_pdf_text(text: str,
                             rows: Optional[List[Dict[str, str]]] = None,
                             default: Optional[str] = None) -> Optional[str]:
    """
    Adivinha a CÉLULA a partir do texto do PDF e do DOCX (se presente).
    1) Detecta grupo (GPA/CBD/Sendas) → retorna mapeamento do DOCX se houver;
       caso contrário, 'Trabalhista GPA'.
    2) Sem DOCX, usa heurística (menções a Pão de Açúcar/GPA/CBD/Sendas).
    """
    up = _norm(text)

    # 1) Detecta grupo
    grp = detect_grupo(text) if callable(detect_grupo) else None

    # 2) Se tiver DOCX, tenta bater CLIENTE -> CÉLULA
    if rows is None:
        rows = load_alias_rows()
    idx = build_alias_index(rows)

    if grp and _norm(grp) in idx:
        return idx[_norm(grp)]

    # 3) Heurística robusta (sem DOCX)
    if re.search(r"\bGPA\b|P[ÃA]O\s+DE\s+A[CÇ]UCAR|COMPANHIA\s+BRASILEIRA\s+DE\s+DISTRIBU(I|Ç)[CÇ](A|Ã)O|CBD|SENDAS", up, re.I):
        return "Trabalhista GPA"

    # 4) Default opcional
    return canonicalize_cell_label(default)

# ---------- Compatibilidade retroativa (nomes usados em versões antigas) ----------

def decide_celula_from_sources(text: str,
                               cliente_grupo: Optional[str] = None,
                               rows: Optional[List[Dict[str, str]]] = None) -> Optional[str]:
    """
    Preferimos o mapeamento DOCX por 'CLIENTE' quando houver; senão caímos na heurística do PDF.
    """
    if rows is None:
        rows = load_alias_rows()
    idx = build_alias_index(rows)

    if cliente_grupo:
        key = _norm(cliente_grupo)
        if key in idx:
            return idx[key]

    return guess_cell_from_pdf_text(text, rows=rows)

__all__ = [
    "load_alias_rows",
    "build_alias_index",
    "guess_cell_from_pdf_text",
    "decide_celula_from_sources",
    "canonicalize_cell_label",
]
