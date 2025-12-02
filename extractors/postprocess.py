from typing import Dict, Any, List
import re

from .regex_utils import (
    parse_comarca as _parse_comarca,
    extract_estado_sigla,
    parse_numero_orgao_from_cnj,
)
from .dictionary import load_dictionary_from_docx

# tenta nas duas localizações (raiz e /data)
_CELL_DOCX_PATHS = [
    "CLIENTE X CÉLULA.docx",
    "CLIENTE X CÉLULA.DOCX",
    "data/CLIENTE X CÉLULA.docx",
    "data/CLIENTE X CÉLULA.DOCX",
]


def _choose_instancia(vara: str | None, orgao: str | None, text: str) -> str | None:
    if vara and re.search(r'\bvara\s+do\s+trabalho\b', vara, re.I):
        return "1ª Instância"
    if orgao and re.search(r'\bTRT', orgao, re.I):
        if re.search(r'\b1[ºo]\s*grau\b', text, re.I):
            return "1ª Instância"
        return "2ª Instância"
    return None

def _numero_orgao_from_vara(vara: str | None, texto: str | None) -> str | None:
    """
    Extrai o ordinal da Vara:
      - casa '1ª Vara', '1a Vara', ou até '1 Vara'
      - procura primeiro em data['vara'] e depois no texto bruto
      - 'Vara Única' -> '1'
    """
    for src in [(vara or ""), (texto or "")]:
        # 1ª / 1a / 1  + "Vara"
        m = re.search(r'\b([1-9][0-9]?)\s*(?:ª|a)?\s*Vara\b', src, re.I)
        if m:
            return str(int(m.group(1)))  # normaliza sem zeros à esquerda

        # Em alguns PDFs vem como "Vara do Trabalho de ..."
        m2 = re.search(r'\b([1-9][0-9]?)\s*(?:ª|a)?\s*Vara\s+do\s+Trabalho\b', src, re.I)
        if m2:
            return str(int(m2.group(1)))

    # "Vara Única" conta como 1
    if re.search(r'\bVara\s+Única\b', (vara or texto or ""), re.I):
        return "1"

    return None

def _prefer_parte_interessada(texto: str, cliente_grupo: str | None) -> str | None:
    up = (texto or "").upper()
    if re.search(r'COMPANHIA\s+BRASILEIRA\s+DE\s+DISTRIBUI', up):
        return "Companhia Brasileira de Distribuição"
    if re.search(r'\bSENDAS\s+DISTRIBUIDOR(A|A)\b|\bSENDAS\s+DISTRIBUIDORA', up):
        return "Sendas Distribuidora S/A"

    for p in _CELL_DOCX_PATHS:
        try:
            rows = load_dictionary_from_docx(p)
            for r in rows:
                if r.get("CLIENTE","").strip().upper() == (cliente_grupo or "").upper():
                    pi = r.get("PARTE INTERESSADA") or r.get("Parte Interessada")
                    if pi:
                        return pi.strip()
        except Exception:
            pass
    return None

def _pick_celula(cliente_grupo: str | None, texto: str) -> str | None:
    """
    Retorna a célula correspondente ao cliente baseado no clientes_database.json.
    
    Mapeia clientes para suas células no eLaw:
    - Casas Bahia → "Trabalhista Casas Bahia"
    - CSN → "Trabalhista CSN"
    - Grupo Pão de Açúcar → "Trabalhista GPA"
    - Profarma → "Trabalhista Pro Pharma"
    - Prudential → "Trabalhista Prudential"
    - Outros → "Trabalhista Outros Clientes"
    """
    if not cliente_grupo:
        return None
    
    # Carregar database de clientes
    import json
    import os
    
    json_path = os.path.join(os.path.dirname(__file__), "..", "data", "clientes_database.json")
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        # Buscar cliente no database
        for nome_cliente, info in data.get("clientes", {}).items():
            if nome_cliente.strip().upper() == cliente_grupo.strip().upper():
                celula = info.get("celula")
                if celula:
                    return celula.strip()
    except Exception:
        pass
    
    # Fallback: se não encontrar no JSON, tenta heurística
    if re.search(r'P(Ã|A)O\s+DE\s+A(C|Ç)U(C|C)AR|GPA|CBD|SENDAS', cliente_grupo, re.I):
        return "Trabalhista GPA"
    elif re.search(r'CASAS?\s+BAHIA', cliente_grupo, re.I):
        return "Trabalhista Casas Bahia"
    elif re.search(r'\bCSN\b|CBSI', cliente_grupo, re.I):
        return "Trabalhista CSN"
    elif re.search(r'PROFARMA', cliente_grupo, re.I):
        return "Trabalhista Pro Pharma"
    elif re.search(r'PRUDENTIAL', cliente_grupo, re.I):
        return "Trabalhista Prudential"
    
    # Default para clientes não mapeados
    return "Trabalhista Outros Clientes"

def _comarca_por_vara(vara: str, uf_hint: str | None, texto: str) -> str | None:
    m = re.search(r'Vara\s+do\s+Trabalho\s+de\s+([A-Za-zÀ-ú\s\'\-]+)', vara or "", re.I)
    if not m:
        return None
    cidade = m.group(1).strip().title()
    uf = uf_hint or extract_estado_sigla(texto) or ""
    return f"{cidade} - {uf}" if uf else cidade

def full_postprocess(base: Dict[str, Any], text: str, celula_options: List[str] | None = None) -> Dict[str, Any]:
    data = dict(base or {})
    texto = text or ""

    # --- COMARCA --------------------------------------------------------------
    if not data.get("comarca"):
        c = _parse_comarca(texto)
        if not c:
            combo = " ".join([data.get("foro",""), data.get("vara","")])
            c = _parse_comarca(combo)
        if not c and data.get("vara"):
            c = _comarca_por_vara(data["vara"], data.get("estado"), texto)
        if c:
            data["comarca"] = c

        # --- CLIENTE (fallback via parte_adversa_nome) --------------------------------
    if not data.get("cliente_grupo") and data.get("parte_adversa_nome"):
        from extractors.brand_map import find_cliente_by_parte_interessada
        cliente_encontrado = find_cliente_by_parte_interessada(data["parte_adversa_nome"])
        if cliente_encontrado:
            data["cliente_grupo"] = cliente_encontrado
            if not data.get("cliente"):
                data["cliente"] = cliente_encontrado
    
    # --- CÉLULA ---------------------------------------------------------------
    if not data.get("celula"):
        cel = _pick_celula(data.get("cliente_grupo"), texto)
        # fallback extra se por algum motivo o heurístico não bateu
        if not cel and data.get("cliente_grupo") and re.search(r'P[ÃA]O\s+DE\s+A[CÇ]ÚCAR|GPA|CBD|SENDAS',
                                                               data["cliente_grupo"], re.I):
            cel = "Trabalhista GPA"
        if cel:
            data["celula"] = cel

    # --- INSTÂNCIA ------------------------------------------------------------
    inst = _choose_instancia(data.get("vara"), data.get("orgao"), texto)
    if inst:
        data["instancia"] = inst

    # --- NÚMERO DO ÓRGÃO (ordinal da Vara: 1 para 1ª, 2 para 2ª...) ----------
        no = _numero_orgao_from_vara(data.get("vara"), texto)
        if no:
            data["numero_orgao"] = no
        else:
            # Se REALMENTE não conseguir achar o ordinal, NÃO force o sufixo do CNJ (ex.: 0481),
            # pois isso é código do órgão e não o ordinal da Vara.
            # Deixe em branco para a UI ou rotas tratarem fallback se necessário.
            pass

    # --- PARTE INTERESSADA ----------------------------------------------------
    if not data.get("parte_interessada") or data["parte_interessada"] in {data.get("cliente"), data.get("cliente_grupo")}:
        pi = _prefer_parte_interessada(texto, data.get("cliente_grupo"))
        if pi:
            data["parte_interessada"] = pi

        # --- CPF/CNPJ PARTE ADVERSA (reforço) ------------------------------------
        if data.get("parte_adversa_nome") and not data.get("cpf_cnpj_parte_adversa"):
            try:
                from .regex_utils import extract_cpf_cnpj_near
                doc_id = extract_cpf_cnpj_near(texto, data["parte_adversa_nome"])
                if not doc_id:
                    # último recurso: primeiro CPF que aparecer no documento
                    m_any = re.search(r'\b\d{3}[.\s]?\d{3}[.\s]?\d{3}[-]?\d{2}\b', texto)
                    if m_any:
                        doc_id = re.sub(r'\D', '', m_any.group(0))
                if doc_id:
                    data["cpf_cnpj_parte_adversa"] = doc_id
                    if len(doc_id) == 11:
                        data["parte_adversa_tipo"] = "PESSOA FISICA"
                    elif len(doc_id) == 14:
                        data["parte_adversa_tipo"] = "PESSOA JURIDICA"
            except Exception:
                pass

    # Observação: segredo de justiça
    if not data.get("observacao") and re.search(r"segredo\s+de\s+justi[cç]a", (text or ""), re.I):
        data["observacao"] = "Segredo de justiça"

    return data
