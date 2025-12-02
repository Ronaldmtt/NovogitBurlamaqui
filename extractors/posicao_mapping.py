"""
Mapeamento de posições da parte interessada para o eLaw.
Baseado no dropdown real do sistema eLaw com 61 opções.
"""

import re
from typing import Optional, Dict
from rapidfuzz import fuzz
import unicodedata

# Mapeamento completo ID -> LABEL do dropdown do eLaw
ELAW_POSICOES = {
    "1": "AUTOR",
    "2": "REU",
    "3": "TERCEIRO INTERESSADO",
    "4": "ADVOGADO",
    "5": "AGRAVANTE",
    "6": "AGRAVADO",
    "7": "APELADO",
    "8": "APELANTE",
    "9": "AUTUANTE",
    "10": "AUTUADO",
    "11": "ALIMENTANTE",
    "12": "BENEFICIÁRIO",
    "13": "CONFINANTE",
    "14": "REQUENTE",
    "15": "CONSIGNANTE",
    "16": "CONSIGNADOV",
    "17": "CREDOR",
    "18": "DEVEDOR",
    "19": "DEMANDANTE",
    "20": "DEMANDADO",
    "21": "DENUNCIANTE",
    "22": "DENUNCIADO",
    "23": "DEPRECANTE",
    "24": "DEPRECADO",
    "25": "EMBARGANTE",
    "26": "EMBARGADO",
    "27": "EXCIPIENTE",
    "28": "EXCEPTO",
    "29": "EXEQUENTE",
    "30": "EXECUTADO",
    "31": "FALÊNCIA",
    "32": "HABTE",
    "33": "HABILITANTE",
    "34": "HABILITADA",
    "35": "IMPETRANTE",
    "36": "IMPETTRADO",
    "37": "IMPUGNANTE",
    "38": "IMPUGNADO",
    "39": "INTERPELANTE",
    "40": "INTERPELADO",
    "41": "INVENT",
    "42": "REQT",
    "43": "INVENTARIANTE",
    "44": "INVENTARIADO",
    "45": "INTERDITANTE",
    "46": "INTERDITADO",
    "47": "NOTIFICANTE",
    "48": "NOTIFICADO",
    "49": "OPOENTE",
    "50": "OPOSTO",
    "51": "RECLAMANTE",
    "52": "RECLAMADO",
    "53": "RECOVINTE",
    "54": "RECOVINDO",
    "55": "RECORRENTE",
    "56": "RECORRIDO",
    "57": "REQUERENTE",
    "58": "REQUERIDO",
    "59": "SUSCITANTE",
    "60": "SUSCITADO",
    "63": "PARTES",
}

# Mapeamento reverso LABEL -> ID para busca rápida
LABEL_TO_ID = {label: id_val for id_val, label in ELAW_POSICOES.items()}

# Sinônimos e variações comuns separados por contexto
# IMPORTANTE: Abreviações ambíguas (RTE, RDO, RDA) variam por contexto processual

SINONIMOS_TRABALHISTA = {
    # Trabalhistas (ações iniciais)
    "RECLAMANTE": "RECLAMANTE",
    "RECLAMADO": "RECLAMADO",
    "RECLAMADA": "RECLAMADO",
    "RTE": "RECLAMANTE",  # Abreviação trabalhista
    "RDO": "RECLAMADO",   # Abreviação trabalhista
    "RDA": "RECLAMADO",   # Abreviação trabalhista
}

SINONIMOS_RECURSOS = {
    # Recursos trabalhistas e cíveis
    "RECORRENTE": "RECORRENTE",
    "RECORRIDO": "RECORRIDO",
    "RECORRIDA": "RECORRIDO",
    "RTE": "RECORRENTE",  # Abreviação em recursos
    "RDO": "RECORRIDO",   # Abreviação em recursos
    "RDA": "RECORRIDO",   # Abreviação em recursos
    
    # Apelações
    "APELANTE": "APELANTE",
    "APELADO": "APELADO",
    "APELADA": "APELADO",
    
    # Agravos
    "AGRAVANTE": "AGRAVANTE",
    "AGRAVADO": "AGRAVADO",
    "AGRAVADA": "AGRAVADO",
    
    # Embargos
    "EMBARGANTE": "EMBARGANTE",
    "EMBARGADO": "EMBARGADO",
    "EMBARGADA": "EMBARGADO",
}

SINONIMOS_CIVEIS = {
    # Cíveis (ações iniciais)
    "AUTOR": "AUTOR",
    "AUTORA": "AUTOR",
    "REU": "REU",
    "RE": "REU",
    "RÉ": "REU",
    "RÉU": "REU",
    
    # Requerimentos
    "REQUERENTE": "REQUERENTE",
    "REQUERIDO": "REQUERIDO",
    "REQUERIDA": "REQUERIDO",
    
    # Outros cíveis
    "DEMANDANTE": "DEMANDANTE",
    "DEMANDADO": "DEMANDADO",
    "DEMANDADA": "DEMANDADO",
}

SINONIMOS_EXECUCAO = {
    # Execução
    "EXEQUENTE": "EXEQUENTE",
    "EXECUTADO": "EXECUTADO",
    "EXECUTADA": "EXECUTADO",
}

SINONIMOS_OUTROS = {
    # Mandado de Segurança
    "IMPETRANTE": "IMPETRANTE",
    "IMPETRADO": "IMPETTRADO",
    "IMPETTRADO": "IMPETTRADO",
    
    # Denúncias
    "DENUNCIANTE": "DENUNCIANTE",
    "DENUNCIADO": "DENUNCIADO",
    "DENUNCIADA": "DENUNCIADO",
}

# Dicionário consolidado para busca rápida (sem abreviações ambíguas)
# Usado como fallback quando contexto não é especificado
SINONIMOS_POSICAO = {
    # Trabalhistas (PRIORIDADE - maior volume)
    "RECLAMANTE": "RECLAMANTE",
    "RECLAMADO": "RECLAMADO",
    "RECLAMADA": "RECLAMADO",
    
    # Recursos
    "RECORRENTE": "RECORRENTE",
    "RECORRIDO": "RECORRIDO",
    "RECORRIDA": "RECORRIDO",
    
    # Cíveis
    "AUTOR": "AUTOR",
    "AUTORA": "AUTOR",
    "REU": "REU",
    "RE": "REU",
    "RÉ": "REU",
    "RÉU": "REU",
    
    # Apelações
    "APELANTE": "APELANTE",
    "APELADO": "APELADO",
    "APELADA": "APELADO",
    
    # Agravos
    "AGRAVANTE": "AGRAVANTE",
    "AGRAVADO": "AGRAVADO",
    "AGRAVADA": "AGRAVADO",
    
    # Embargos
    "EMBARGANTE": "EMBARGANTE",
    "EMBARGADO": "EMBARGADO",
    "EMBARGADA": "EMBARGADO",
    
    # Execução
    "EXEQUENTE": "EXEQUENTE",
    "EXECUTADO": "EXECUTADO",
    "EXECUTADA": "EXECUTADO",
    
    # Mandado de Segurança
    "IMPETRANTE": "IMPETRANTE",
    "IMPETRADO": "IMPETTRADO",
    "IMPETTRADO": "IMPETTRADO",
    
    # Requerimentos
    "REQUERENTE": "REQUERENTE",
    "REQUERIDO": "REQUERIDO",
    "REQUERIDA": "REQUERIDO",
    
    # Outros
    "DEMANDANTE": "DEMANDANTE",
    "DEMANDADO": "DEMANDADO",
    "DEMANDADA": "DEMANDADO",
    "DENUNCIANTE": "DENUNCIANTE",
    "DENUNCIADO": "DENUNCIADO",
    "DENUNCIADA": "DENUNCIADO",
}


def _normalizar(texto: str) -> str:
    """Remove acentos, uppercase, remove espaços extras."""
    if not texto:
        return ""
    # Remove acentos
    texto = unicodedata.normalize('NFKD', texto)
    texto = ''.join([c for c in texto if not unicodedata.combining(c)])
    # Uppercase e remove espaços
    return re.sub(r'\s+', ' ', texto.upper().strip())


def resolve_posicao(posicao: str, contexto: str = "trabalhista") -> str:
    """
    Resolve uma posição considerando o contexto processual.
    IMPORTANTE: Use esta função quando tiver informação de contexto.
    
    Args:
        posicao: Posição extraída do PDF ou abreviação (ex: "RTE", "RECLAMANTE")
        contexto: Contexto processual - "trabalhista" (padrão), "recursos", "civeis", "execucao", "outros"
    
    Returns:
        Label oficial do eLaw normalizado por contexto
    
    Examples:
        >>> resolve_posicao("RTE", "trabalhista")
        "RECLAMANTE"
        >>> resolve_posicao("RTE", "recursos")
        "RECORRENTE"
    """
    if not posicao:
        return ""
    
    posicao_norm = _normalizar(posicao)
    
    # Seleciona o dicionário de sinônimos baseado no contexto
    contexto_map = {
        "trabalhista": SINONIMOS_TRABALHISTA,
        "recursos": SINONIMOS_RECURSOS,
        "civeis": SINONIMOS_CIVEIS,
        "execucao": SINONIMOS_EXECUCAO,
        "outros": SINONIMOS_OUTROS,
    }
    
    sinonimos = contexto_map.get(contexto.lower(), SINONIMOS_TRABALHISTA)
    
    # 1. Busca exata no sinônimos do contexto
    if posicao_norm in sinonimos:
        return sinonimos[posicao_norm]
    
    # 2. Busca exata nos labels oficiais
    if posicao_norm in LABEL_TO_ID:
        return posicao_norm
    
    # 3. Fuzzy matching
    best_match = find_posicao_fuzzy(posicao_norm, threshold=85)
    if best_match:
        return best_match
    
    # 4. Fallback
    return posicao_norm


def normalize_posicao(posicao: str, contexto: Optional[str] = None) -> str:
    """
    Normaliza uma posição encontrada no PDF para o label oficial do eLaw.
    
    Args:
        posicao: Posição extraída do PDF (ex: "RECLAMANTE", "Reclamado", "Apelante")
        contexto: (Opcional) Contexto processual para desambiguar abreviações
    
    Returns:
        Label oficial do eLaw (ex: "RECLAMANTE", "RECLAMADO", "APELANTE")
    
    Note:
        Se contexto não for fornecido, assume trabalhista (maior volume de casos).
        Para abreviações ambíguas (RTE, RDO), use resolve_posicao() com contexto explícito.
    """
    if not posicao:
        return ""
    
    # Se contexto foi fornecido, usa resolve_posicao
    if contexto:
        return resolve_posicao(posicao, contexto)
    
    posicao_norm = _normalizar(posicao)
    
    # 1. Busca exata no sinônimos (sem abreviações ambíguas)
    if posicao_norm in SINONIMOS_POSICAO:
        return SINONIMOS_POSICAO[posicao_norm]
    
    # 2. Busca exata nos labels oficiais
    if posicao_norm in LABEL_TO_ID:
        return posicao_norm
    
    # 3. Se for abreviação ambígua, assume trabalhista e emite warning
    if posicao_norm in ["RTE", "RDO", "RDA"]:
        import logging
        logging.warning(f"[POSICAO] Abreviação ambígua '{posicao_norm}' sem contexto - assumindo trabalhista. Use resolve_posicao() com contexto explícito.")
        return resolve_posicao(posicao, "trabalhista")
    
    # 4. Fuzzy matching com labels oficiais
    best_match = find_posicao_fuzzy(posicao_norm, threshold=85)
    if best_match:
        return best_match
    
    # 5. Fallback: retorna normalizado
    return posicao_norm


def find_posicao_fuzzy(posicao: str, threshold: int = 85) -> Optional[str]:
    """
    Busca a posição mais próxima usando fuzzy matching.
    
    Args:
        posicao: Posição a buscar
        threshold: Threshold de similaridade (0-100)
    
    Returns:
        Label oficial do eLaw ou None se não encontrou
    """
    if not posicao:
        return None
    
    posicao_norm = _normalizar(posicao)
    best_score = 0
    best_label = None
    
    # Busca nos labels oficiais
    for label in LABEL_TO_ID.keys():
        score = fuzz.ratio(posicao_norm, label)
        if score > best_score:
            best_score = score
            best_label = label
    
    if best_score >= threshold:
        return best_label
    
    return None


def get_posicao_id(posicao: str) -> Optional[str]:
    """
    Retorna o ID do eLaw para uma posição.
    
    Args:
        posicao: Posição (pode ser label ou termo do PDF)
    
    Returns:
        ID do eLaw (ex: "51" para RECLAMANTE) ou None
    """
    if not posicao:
        return None
    
    # Normaliza primeiro
    label_oficial = normalize_posicao(posicao)
    
    # Busca o ID
    return LABEL_TO_ID.get(label_oficial)


def get_posicao_label(id_elaw: str) -> Optional[str]:
    """
    Retorna o label oficial do eLaw para um ID.
    
    Args:
        id_elaw: ID do eLaw (ex: "51")
    
    Returns:
        Label oficial (ex: "RECLAMANTE") ou None
    """
    return ELAW_POSICOES.get(str(id_elaw))


def get_all_posicoes() -> Dict[str, str]:
    """Retorna todas as posições disponíveis (ID -> LABEL)."""
    return ELAW_POSICOES.copy()


def get_posicoes_trabalhistas() -> Dict[str, str]:
    """Retorna apenas as posições mais comuns em processos trabalhistas."""
    trabalhistas = {
        "51": "RECLAMANTE",
        "52": "RECLAMADO",
        "55": "RECORRENTE",
        "56": "RECORRIDO",
        "8": "APELANTE",
        "7": "APELADO",
        "25": "EMBARGANTE",
        "26": "EMBARGADO",
    }
    return trabalhistas


# Testes rápidos
if __name__ == "__main__":
    print("=== TESTES DE NORMALIZAÇÃO DE POSIÇÕES ===\n")
    
    testes = [
        "RECLAMANTE",
        "Reclamado",
        "RECORRENTE",
        "Recorrido",
        "APELANTE",
        "Apelado",
        "AUTOR",
        "REU",
        "RÉ",
        "EMBARGANTE",
        "Executado",
        "IMPETRANTE",
        "Requerente",
    ]
    
    for termo in testes:
        label = normalize_posicao(termo)
        id_elaw = get_posicao_id(termo)
        print(f"{termo:20s} -> Label: {label:20s} | ID eLaw: {id_elaw or 'N/A'}")
    
    print("\n=== BUSCA FUZZY ===\n")
    fuzzy_testes = [
        "RECLAMNTE",  # erro de digitação
        "REQUERIDO",
        "APELADA",  # variação feminina
        "AGRAVADO",
    ]
    
    for termo in fuzzy_testes:
        label = normalize_posicao(termo)
        id_elaw = get_posicao_id(termo)
        print(f"{termo:20s} -> Label: {label:20s} | ID eLaw: {id_elaw or 'N/A'}")
