# utils/gap_filler.py
from typing import Dict, Any, List, Tuple, Optional
from rapidfuzz import fuzz
from .normalization import normalize_text, contains_any
from .option_catalog import load_catalog

def _pick_from_choices(text: str, choices: List[dict], prefer: Optional[List[str]] = None) -> Optional[str]:
    if not choices:
        return None
    if prefer:
        t = normalize_text(text or "")
        for p in prefer:
            pnorm = normalize_text(p)
            for c in choices:
                if pnorm in c["normalized"]:
                    return c["text"]
    candidates = [(c["text"], fuzz.token_set_ratio(normalize_text(text or ""), c["normalized"])) for c in choices]
    candidates.sort(key=lambda x: x[1], reverse=True)
    best = candidates[0] if candidates else None
    if best and best[1] >= 70:
        return best[0]
    return None

def _infer_instancia(text: str, cnj: Optional[str]) -> Optional[str]:
    if contains_any(text, "juizado especial", "vara", "tribunal", "trt", "tj", "trf", "2 grau", "segundo grau"):
        return "Judicial"
    return None

def gap_fill(data: Dict[str, Any], pdf_text: str) -> Tuple[Dict[str, Any], List[str], Dict[str, Any]]:
    needs, ev = [], {}
    txt = (pdf_text or "")[:30000]

    # Instância
    if not data.get("instancia"):
        val = _infer_instancia(txt, data.get("cnj"))
        if val:
            data["instancia"] = val
            ev["instancia"] = {"source": "rule", "value": val}
        else:
            needs.append("instancia")

    # Sistema Eletrônico
    if not data.get("sistema_eletronico"):
        cat = load_catalog("SistemaEletronicoId")
        val = _pick_from_choices(txt, cat, prefer=["PJE", "Juízo 100% Digital", "Eproc", "Projudi", "EPROC"])
        if not val:
            val = "PJE"
            ev["sistema_eletronico"] = {"source": "default", "value": val}
        else:
            ev["sistema_eletronico"] = {"source": "catalog+match", "value": val}
        data["sistema_eletronico"] = val

    # Área do Direito
    if not data.get("area_direito") and not data.get("area"):
        cat = load_catalog("AreaDireitoId")
        val = _pick_from_choices(txt, cat, prefer=["Cível", "Trabalhista", "Criminal", "Tributário", "Previdenciário"])
        if val:
            data["area_direito"] = val
            ev["area_direito"] = {"source": "catalog+match", "value": val}
        else:
            data["area_direito"] = "Cível"
            ev["area_direito"] = {"source": "default", "value": "Cível"}

    # Assunto / Objeto / Tipo de Ação
    for field, catalog_name, fallback in [
        ("assunto", "AreaProcessoId", "INDEFINIDO"),
        ("objeto", "ClasseId", "INDEFINIDO"),
        ("tipo_acao", "TipoAcaoId", "INDEFINIDO"),
    ]:
        if not data.get(field):
            cat = load_catalog(catalog_name)
            val = _pick_from_choices(txt, cat)
            if val:
                data[field] = val
                ev[field] = {"source": "catalog+match", "value": val}
            else:
                data[field] = fallback
                ev[field] = {"source": "default", "value": fallback}
                needs.append(field)

    # Órgão / Foro (rascunho; o definitivo no RPA depende de UF/Cidade)
    if not data.get("orgao"):
        cat = load_catalog("NaturezaId")
        val = _pick_from_choices(txt, cat)
        if val:
            data["orgao"] = val
            ev["orgao"] = {"source": "catalog+match", "value": val}
        else:
            needs.append("orgao")

    if not data.get("foro"):
        needs.append("foro")

    # Célula (preferência trabalhista)
    if not data.get("celula"):
        cat = load_catalog("EscritorioId")
        prefer = ["Trabalhista Outros Clientes"] if "trabalh" in normalize_text(data.get("area_direito", "")) else None
        val = _pick_from_choices(" ".join([txt, data.get("cliente", "")]), cat, prefer=prefer)
        if val:
            data["celula"] = val
            ev["celula"] = {"source": "catalog+match", "value": val}
        else:
            data["celula"] = "Em Segredo"
            ev["celula"] = {"source": "default", "value": "Em Segredo"}
            needs.append("celula")

    return data, needs, ev
