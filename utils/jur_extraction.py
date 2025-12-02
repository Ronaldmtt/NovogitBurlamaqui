# utils/jur_extraction.py
# Corrige a inversão Parte Interessada/Adversa e enriquece campos a partir do PDF

import re, json, unicodedata
from typing import Dict, Any

def _norm(s:str)->str:
    s=(s or "").strip().lower()
    s=unicodedata.normalize("NFD", s)
    return "".join(ch for ch in s if unicodedata.category(ch)!="Mn")

def _any_in(text:str, words):
    s=_norm(text)
    return any(_norm(w) in s for w in words)

def _extract_valor_causa(pdf_text:str)->str:
    import re
    m = re.search(r"valor\s+da\s+causa\s*[:\-]?\s*(?:R?\$)?\s*([0-9\.\,]{3,})", pdf_text, re.I)
    if not m: return ""
    return _money_br_to_field(m.group(1))

def _money_br_to_field(s:str)->str:
    s=(s or "").strip()
    if not s: return s
    raw=re.sub(r"[^\d,\.]","",s)
    if re.match(r"^\d{1,3}(\.\d{3})*,\d{2}$", raw):
        return raw
    if "," in raw and "." in raw and raw.rfind(".") > raw.rfind(","):
        number = raw.replace(",", "").replace(".", ",")
    elif "," in raw and "." not in raw:
        integer, cents = raw.split(",")[0], raw.split(",")[1]
        integer = integer[::-1]
        groups = ".".join([integer[i:i+3] for i in range(0, len(integer), 3)])[::-1]
        number = f"{groups},{cents[:2].ljust(2,'0')}"
    elif "." in raw and "," not in raw:
        integer, cents = raw.split(".")[0], raw.split(".")[1]
        integer = integer[::-1]
        groups = ".".join([integer[i:i+3] for i in range(0, len(integer), 3)])[::-1]
        number = f"{groups},{cents[:2].ljust(2,'0')}"
    else:
        if len(raw) <=2:
            number=f"0,{raw.zfill(2)}"
        else:
            integer, cents = raw[:-2], raw[-2:]
            integer=integer[::-1]
            groups=".".join([integer[i:i+3] for i in range(0,len(integer),3)])[::-1]
            number=f"{groups},{cents}"
    return number

def _extract_partes_trabalhista(pdf_text:str)->Dict[str,str]:
    out={}
    for lab in ["RECLAMANTE", "RECLAMADO", "RECLAMADA"]:
        m = re.search(rf"{lab}\s*[:\-]\s*(.+?)(?:\n|$)", pdf_text, re.I)
        if m:
            out[lab.lower()] = m.group(1).strip()
    return {
        "reclamante": out.get("reclamante",""),
        "reclamado": out.get("reclamado","") or out.get("reclamada",""),
    }

def _looks_company(name:str, bag:str)->bool:
    if _any_in(name, ["ltda","s/a","s.a","sa ","eireli","epp","me ","companhia","cooperativa","fundação","banco","associação","prefeitura","município","união","estado","instituto"]):
        return True
    if re.search(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b", bag):  # CNPJ
        return True
    return False

def _guess_tipo_pessoa_adversa(adverso_nome:str, bag:str)->str:
    if _looks_company(adverso_nome, bag): return "JURIDICA"
    if re.search(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b", bag): return "FISICA"
    if len(_norm(adverso_nome).split())>=2 and not _looks_company(adverso_nome, adverso_nome): return "FISICA"
    return "FISICA"

def smart_enrich_data(data:Dict[str,Any], pdf_text:str)->Dict[str,Any]:
    out=dict(data)
    partes=_extract_partes_trabalhista(pdf_text)

    # Interessada=seu cliente (empresa) → preferir RECLAMADO
    if partes.get("reclamado"):
        out.setdefault("parte_interessada", partes["reclamado"])
        out.setdefault("cliente", partes["reclamado"])
        out.setdefault("empresa", partes["reclamado"])
    if partes.get("reclamante"):
        out.setdefault("parte_adversa", partes["reclamante"])
        out.setdefault("adverso_nome", partes["reclamante"])

    # Grupo (GPA etc.)
    bag = json.dumps(out, ensure_ascii=False) + "\n" + pdf_text
    for key,label in {
        "gpa":"Grupo Pão de Açúcar", "grupo pao de acucar":"Grupo Pão de Açúcar",
        "pao de acucar":"Grupo Pão de Açúcar", "companhia brasileira de distribuicao":"Grupo Pão de Açúcar",
        "sendas distribuidora":"Grupo Pão de Açúcar",
    }.items():
        if key in _norm(bag):
            out.setdefault("grupo", label); break

    # Tipo pessoa adversa
    if not out.get("tipo_pessoa_adversa"):
        out["tipo_pessoa_adversa"] = _guess_tipo_pessoa_adversa(out.get("parte_adversa","") or out.get("adverso_nome",""), bag)

    # Posição parte interessada
    if not out.get("posicao_parte_interessada"):
        if partes.get("reclamado") and _any_in(out.get("parte_interessada","")+out.get("cliente",""), [partes["reclamado"]]):
            out["posicao_parte_interessada"] = "RECLAMADO"
        elif partes.get("reclamante"):
            out["posicao_parte_interessada"] = "RECLAMANTE"

    # Valor da Causa
    if not out.get("valor_causa"):
        vc=_extract_valor_causa(pdf_text)
        if vc: out["valor_causa"]=vc

    # Tipo de Ação: prioriza sub_area_direito e assunto
    if not out.get("tipo_acao"):
        cand = (out.get("sub_area_direito") or "").strip() or (out.get("assunto") or "").strip()
        out["tipo_acao"]=cand

    return out
