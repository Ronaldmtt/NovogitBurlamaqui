# -*- coding: utf-8 -*-
import re
import json
import os
import logging
from typing import Dict, Optional, Tuple, List

CNJ_RE = re.compile(r"\b\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}\b")

# ===== UTILITÁRIOS DE NORMALIZAÇÃO COMPARTILHADOS =====
# 2025-12-01: Funções centralizadas para normalizar texto antes de aplicar regex
# Evita duplicação e garante consistência em todas as funções de extração

def normalize_text(text: str) -> str:
    """
    Normalização básica de texto para TODOS os campos.
    Remove espaços múltiplos, normaliza quebras de linha, limpa hífens especiais.
    
    Usar como PRIMEIRO passo em todas as funções extract_*.
    """
    if not text:
        return ""
    # Substituir hífens especiais por hífen comum
    text = text.replace('–', '-').replace('—', '-')
    # Remover quebras de linha múltiplas
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Normalizar espaços múltiplos para um só
    text = re.sub(r' {2,}', ' ', text)
    # Remover espaços no início/fim de linhas
    text = re.sub(r'^ +| +$', '', text, flags=re.MULTILINE)
    return text.strip()

def normalize_monetary(text: str) -> str:
    """
    Normalização específica para VALORES MONETÁRIOS.
    Corrige problemas comuns de OCR/PyPDF2 em valores como R$ 1.234,56
    
    Exemplos de correção:
    - "R $ 3.093, 10" → "R$ 3.093,10"
    - "R$ 2 . 802,31" → "R$ 2.802,31"
    - "1 . 234 , 56" → "1.234,56"
    """
    if not text:
        return ""
    # R $ → R$
    text = re.sub(r'R\s*\$', 'R$', text)
    # Vírgula com espaço: ", 10" → ",10"
    text = re.sub(r',\s+', ',', text)
    # Ponto com espaço: "3. 093" → "3.093"
    text = re.sub(r'\.\s+', '.', text)
    # Espaço antes do ponto: "2 ." → "2."
    text = re.sub(r'\s+\.', '.', text)
    # Dígitos separados por espaço: "2 802" → "2802" (problema PyPDF2)
    text = re.sub(r'(\d)\s+(\d)', r'\1\2', text)
    return text

def normalize_date_separators(text: str) -> str:
    """
    Normalização específica para DATAS.
    Converte separadores variados para formato padrão DD/MM/AAAA.
    
    Exemplos:
    - "25.09.2025" → "25/09/2025"
    - "25-09-2025" → "25/09/2025"
    - "25 / 09 / 2025" → "25/09/2025"
    """
    if not text:
        return ""
    # Espaços ao redor de separadores: "25 / 09" → "25/09"
    text = re.sub(r'\s*[/.\-]\s*', '/', text)
    return text

def normalize_identifiers(text: str) -> str:
    """
    Normalização específica para IDENTIFICADORES (PIS, CTPS, CPF, RG).
    Remove "nº", "n°", "n º" e normaliza espaços.
    
    Exemplos:
    - "PIS nº 204.05911.17.8" → "PIS  204.05911.17.8" (espaço extra removido depois)
    - "CTPS n° 1210996" → "CTPS  1210996"
    """
    if not text:
        return ""
    # Remover "nº", "n°", "N°", "Nº" com espaços opcionais
    text = re.sub(r'n\s*[º°]\.?\s*', ' ', text, flags=re.I)
    # Normalizar espaços múltiplos resultantes
    text = re.sub(r'\s+', ' ', text)
    # Normalizar hífens especiais
    text = text.replace('–', '-').replace('—', '-')
    return text

def clean_extracted_value(value: str) -> str:
    """
    Limpeza final de valores extraídos.
    Remove espaços extras, pontuação pendente, etc.
    """
    if not value:
        return ""
    value = value.strip()
    # Remover pontuação pendente no final
    value = re.sub(r'[,;:\.\s]+$', '', value)
    # Normalizar espaços internos
    value = re.sub(r'\s+', ' ', value)
    return value

# Mapeamento de meses por extenso (compartilhado)
MESES_MAP = {
    'janeiro': '01', 'jan': '01', 'jan.': '01',
    'fevereiro': '02', 'fev': '02', 'fev.': '02',
    'março': '03', 'marco': '03', 'mar': '03', 'mar.': '03',
    'abril': '04', 'abr': '04', 'abr.': '04',
    'maio': '05', 'mai': '05', 'mai.': '05',
    'junho': '06', 'jun': '06', 'jun.': '06',
    'julho': '07', 'jul': '07', 'jul.': '07',
    'agosto': '08', 'ago': '08', 'ago.': '08',
    'setembro': '09', 'set': '09', 'set.': '09',
    'outubro': '10', 'out': '10', 'out.': '10',
    'novembro': '11', 'nov': '11', 'nov.': '11',
    'dezembro': '12', 'dez': '12', 'dez.': '12'
}

def parse_date_extenso(text: str) -> Optional[str]:
    """
    Converte data por extenso para DD/MM/AAAA.
    
    IMPORTANTE: Requer dia explícito para evitar fabricação de dados.
    
    Exemplos:
    - "01 de junho de 2024" → "01/06/2024"
    - "15 de março de 2023" → "15/03/2023"
    - "junho de 2024" → None (dia não especificado, não fabrica)
    
    Returns:
        Data formatada ou None se não conseguir parsear OU se faltando dia
    """
    if not text:
        return None
    
    text_lower = text.lower().strip()
    
    # Padrão SOMENTE com dia explícito: "01 de junho de 2024"
    # NOTA: Removido fallback para mês/ano pois fabricar dia=01 viola regra ZERO ERRORS
    m = re.search(r'(\d{1,2})\s*(?:de\s+)?(' + '|'.join(MESES_MAP.keys()) + r')\.?\s*(?:de\s+)?(\d{4})', text_lower)
    if m:
        dia = m.group(1).zfill(2)
        mes = MESES_MAP.get(m.group(2).replace('.', ''), None)
        ano = m.group(3)
        if mes:
            return f"{dia}/{mes}/{ano}"
    
    # Não retorna data se só tiver mês/ano - evita fabricar dia=01
    return None

def is_valid_brazilian_date(date_str: str) -> bool:
    """
    Valida se uma data no formato DD/MM/AAAA é válida.
    
    Regras:
    - Dia: 01-31
    - Mês: 01-12
    - Ano: 1900-2100 (razoável para contexto trabalhista)
    """
    if not date_str:
        return False
    
    m = re.match(r'^(\d{2})/(\d{2})/(\d{4})$', date_str)
    if not m:
        return False
    
    dia, mes, ano = int(m.group(1)), int(m.group(2)), int(m.group(3))
    
    if not (1 <= dia <= 31):
        return False
    if not (1 <= mes <= 12):
        return False
    if not (1900 <= ano <= 2100):
        return False
    
    return True

def is_invalid_date_context(match_obj, text: str, keywords: List[str] = None) -> bool:
    """
    Verifica se uma data está em contexto inválido (assinatura eletrônica, distribuição, etc).
    
    Args:
        match_obj: Objeto match do regex
        text: Texto original
        keywords: Lista de keywords que invalidam a data (default: assinaturas, distribuição)
    
    Returns:
        True se a data está em contexto inválido
    """
    if keywords is None:
        keywords = [
            'assinado eletronicamente', 'documento assinado', 
            'data da autuação', 'data da distribuição', 'distribuído em',
            'intimação', 'notificação', 'publicação', 'certifico',
            'assinatura digital', 'validar este documento'
        ]
    
    # Contexto: 50 chars antes e 50 depois
    start = max(0, match_obj.start() - 50)
    end = min(len(text), match_obj.end() + 50)
    context = text[start:end].lower()
    
    return any(kw in context for kw in keywords)

# Logger para funções de extração
_extract_logger = logging.getLogger(__name__)

# ===== MAPEAMENTO TRT CENTRALIZADO =====
_TRT_MAP_CACHE = None

def _load_trt_map() -> Dict:
    """Carrega o mapeamento TRT → Estado/UF do arquivo JSON."""
    global _TRT_MAP_CACHE
    if _TRT_MAP_CACHE is not None:
        return _TRT_MAP_CACHE
    
    try:
        map_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trt_map.json")
        with open(map_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _TRT_MAP_CACHE = {k: v for k, v in data.items() if not k.startswith("_")}
        return _TRT_MAP_CACHE
    except Exception as e:
        print(f"[TRT_MAP] Erro ao carregar mapeamento: {e}")
        return {}

def get_trt_info(codigo_trt: str) -> Dict:
    """Retorna informações do TRT pelo código (01-24)."""
    trt_map = _load_trt_map()
    codigo = codigo_trt.zfill(2)
    return trt_map.get(codigo, {})

def get_estado_from_trt(codigo_trt: str) -> str:
    """Retorna o nome do estado a partir do código TRT."""
    info = get_trt_info(codigo_trt)
    return info.get("estado", "")

def get_uf_from_trt(codigo_trt: str) -> str:
    """Retorna a sigla UF principal a partir do código TRT."""
    info = get_trt_info(codigo_trt)
    return info.get("uf", "")

def get_ufs_from_trt(codigo_trt: str) -> List[str]:
    """Retorna lista de todas UFs cobertas pelo TRT (para TRTs multi-estado)."""
    info = get_trt_info(codigo_trt)
    return info.get("ufs", [info.get("uf", "")]) if info else []

def get_estado_alt_from_uf(codigo_trt: str, uf: str) -> str:
    """Retorna o nome do estado alternativo para uma UF em TRTs multi-estado."""
    info = get_trt_info(codigo_trt)
    estados_alt = info.get("estados_alt", {})
    return estados_alt.get(uf, info.get("estado", ""))

def disambiguate_trt_uf(codigo_trt: str, text: str) -> str:
    """
    Para TRTs que cobrem múltiplos estados (08, 10, 11, 14), 
    tenta desambiguar qual UF é a correta baseando-se no texto do PDF.
    
    TRTs multi-estado:
    - TRT 08: PA, AP (Pará e Amapá)
    - TRT 10: DF, TO (Distrito Federal e Tocantins)
    - TRT 11: AM, RR (Amazonas e Roraima)
    - TRT 14: RO, AC (Rondônia e Acre)
    
    Returns:
        UF correta baseada no texto, ou UF principal como fallback
    """
    ufs = get_ufs_from_trt(codigo_trt)
    
    if len(ufs) <= 1:
        return ufs[0] if ufs else ""
    
    t = (text or "").upper()
    
    uf_scores = {}
    
    uf_palavras = {
        "PA": ["PARÁ", "PARA", "BELÉM", "BELEM", "SANTAREM", "SANTARÉM", "MARABÁ", "MARABA"],
        "AP": ["AMAPÁ", "AMAPA", "MACAPÁ", "MACAPA"],
        "DF": ["DISTRITO FEDERAL", "BRASÍLIA", "BRASILIA"],
        "TO": ["TOCANTINS", "PALMAS", "ARAGUAINA", "ARAGUAÍNA", "GURUPI"],
        "AM": ["AMAZONAS", "MANAUS"],
        "RR": ["RORAIMA", "BOA VISTA"],
        "RO": ["RONDÔNIA", "RONDONIA", "PORTO VELHO"],
        "AC": ["ACRE", "RIO BRANCO"]
    }
    
    for uf in ufs:
        score = 0
        for palavra in uf_palavras.get(uf, []):
            if palavra in t:
                score += 10
        if re.search(rf'\b{uf}\b', t):
            score += 5
        uf_scores[uf] = score
    
    sorted_ufs = sorted(uf_scores.items(), key=lambda x: x[1], reverse=True)
    
    if sorted_ufs[0][1] > sorted_ufs[1][1]:
        return sorted_ufs[0][0]
    
    return ufs[0]

def get_estado_variantes(codigo_trt: str) -> List[str]:
    """Retorna lista de variantes do nome do estado para seleção em dropdowns."""
    info = get_trt_info(codigo_trt)
    variantes = info.get("variantes", [])
    estado = info.get("estado", "")
    uf = info.get("uf", "")
    
    result = []
    if estado:
        result.append(estado)
    if uf:
        result.append(uf)
    for v in variantes:
        if v not in result:
            result.append(v)
    return result

def extract_trt_from_cnj(cnj: str) -> str:
    """Extrai o código TRT (2 dígitos) do número CNJ."""
    if not cnj:
        return ""
    cnj_digits = re.sub(r'\D', '', cnj)
    if len(cnj_digits) >= 16:
        return cnj_digits[14:16]
    return ""

def extract_trt_from_text(text: str) -> str:
    """Extrai o código TRT do texto do PDF (ex: 'TRT da 13ª Região' → '13')."""
    if not text:
        return ""
    
    # Padrão: "TRT da Xª Região" ou "Tribunal Regional do Trabalho da Xª Região"
    m = re.search(r'(?:TRT|Tribunal\s+Regional\s+do\s+Trabalho)\s+(?:da|de)\s+(\d{1,2})[ªaº]?\s*Regi[ãa]o', text, re.I)
    if m:
        return m.group(1).zfill(2)
    
    # Padrão: "TRT-13" ou "TRT13"
    m = re.search(r'\bTRT[-\s]?(\d{1,2})\b', text, re.I)
    if m:
        return m.group(1).zfill(2)
    
    return ""

UF = r"(AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)"
_CPF_RE  = r'\b(\d{3}[.\s]?\d{3}[.\s]?\d{3}[-]?\d{2})\b'
_CNPJ_RE = r'\b(\d{2}[.\s]?\d{3}[.\s]?\d{3}[\/]?\d{4}[-]?\d{2})\b'
UF_RE = re.compile(r"\b([A-Z]{2})\b")
FORO_RE = re.compile(r"(F[ÓO]RUM\s+[A-ZÇÃÂÉÍÓÚ\- ]+?\s-\s+[A-Z]{2})")
VARA_RE = re.compile(r"\b\d{1,2}ª?\s+Vara do Trabalho de\s+[A-Za-zÀ-ú\s'\-]+")
CELULA_RE = re.compile(r"\bC[ÉE]LULA\s+([A-Za-zÀ-ú0-9\s\-/]+)")
DATA_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
HORA_RE = re.compile(r"\b(\d{1,2}:\d{2})\b")

# "RECLAMANTE: FULANO CPF 123.456.789-00"
PESSOA_FISICA_RE = re.compile(r"\b(CPF)\s*[:\-]?\s*(\d{3}\.?\d{3}\.?\d{3}-?\d{2})")
PESSOA_JURIDICA_RE = re.compile(r"\b(CNPJ)\s*[:\-]?\s*(\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2})")

# ✅ REGEX COMPLETO: Suporta TODAS as 61 nomenclaturas do eLaw
# Baseado no dropdown "Posição da Parte Interessada" do sistema eLaw

# PARTE AUTORA (quem move a ação) - Todas as opções do polo ativo
RECLAMANTE_RE = re.compile(
    r"(?:RECLAMANTE|RECORRENTE|AUTOR|APELANTE|AGRAVANTE|EMBARGANTE|EXEQUENTE|"
    r"IMPETRANTE|IMPUGNANTE|DENUNCIANTE|EXCIPIENTE|OPOENTE|REQUERENTE|"
    r"SUSCITANTE|NOTIFICANTE|INTERPELANTE|DEMANDANTE|DEPRECANTE|AUTUANTE|"
    r"HABILITANTE|INVENTARIANTE|CONSIGNANTE|ALIMENTANTE|INTERDITANTE|RECOVINTE|"
    r"REQUENTE)\s*[:\-]\s*([^\n\r]+)", 
    re.I | re.M
)

# PARTE RECLAMADA (quem sofre a ação) - Todas as opções do polo passivo + neutras
RECLAMADO_RE = re.compile(
    r"(?:RECLAMADO|RECORRIDO|R[ÉE]U|REU|APELADO|AGRAVADO|EMBARGADO|EXECUTADO|"
    r"IMPETTRADO|IMPUGNADO|DENUNCIADO|EXCEPTO|REQUERIDO|SUSCITADO|NOTIFICADO|"
    r"INTERPELADO|DEMANDADO|DEPRECADO|AUTUADO|HABILITADA|INVENTARIADO|"
    r"CONSIGNADOV|INTERDITADO|RECOVINDO|OPOSTO|BENEFICI[ÁA]RIO|CONFINANTE|"
    r"CREDOR|DEVEDOR|FAL[ÊE]NCIA|HABTE|INVENT|ADVOGADO|PARTES|REQT|"
    r"TERCEIRO\s+INTERESSADO)\s*[:\-]\s*([^\n\r]+)", 
    re.I | re.M
)


# ===== PADRÕES DE DETECÇÃO DE ÓRGÃOS JUDICIAIS E ADMINISTRATIVOS =====
# Tribunais Trabalhistas
TRT_RE = re.compile(r"Tribunal Regional do Trabalho\s+da\s+(\d+)[ªa]\s+Regi[aã]o", re.I)
TST_RE = re.compile(r"\bTribunal Superior do Trabalho\b", re.I)

# Tribunais Superiores
STF_RE = re.compile(r"\bSupremo\s+Tribunal\s+Federal\b|\bSTF\b", re.I)
STJ_RE = re.compile(r"\bSuperior\s+Tribunal\s+de\s+Justi[çc]a\b|\bSTJ\b", re.I)

# Justiça Federal
TRF_RE = re.compile(r"Tribunal Regional Federal\s+da\s+(\d+)[ªa]\s+Regi[aã]o", re.I)
JF_RE = re.compile(r"\bJusti[çc]a\s+Federal\b|\bVara\s+Federal\b|\bJF\b", re.I)

# Órgãos Administrativos
PROCON_RE = re.compile(r"\bPROCON\b", re.I)
RECEITA_FEDERAL_RE = re.compile(r"\bReceita\s+Federal\b|\bRFB\b", re.I)
PREFEITURA_RE = re.compile(r"\bPrefeitura\s+(Municipal\s+)?de\b", re.I)
ORGAO_ADMIN_RE = re.compile(r"\b[ÓO]rg[aã]o\s+Administrativo\b", re.I)

def normalize(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def is_pessoa_juridica(nome: str) -> bool:
    """
    Detecta se um nome é de pessoa jurídica através de sufixos empresariais e entidades institucionais.
    Usa word boundaries para evitar falsos positivos (ex: "CIA" em "GARCIA").
    
    Cobre:
    - Sufixos empresariais (LTDA, S.A., EIRELI, etc)
    - Entidades institucionais (SINDICATO, UNIÃO, MUNICÍPIO, ESTADO, etc)
    - Entidades públicas (PREFEITURA, BANCO CENTRAL, MINISTÉRIO, etc)
    
    Returns:
        True se detectar indicadores de PJ, False caso contrário
    """
    if not nome:
        return False
    
    nome_upper = normalize(nome).upper()
    
    # Remover acentos para matching mais robusto (ex: UNIÃO → UNIAO)
    import unicodedata
    nome_normalizado = unicodedata.normalize('NFD', nome_upper)
    nome_normalizado = ''.join(c for c in nome_normalizado if unicodedata.category(c) != 'Mn')
    
    # Lista de sufixos/indicadores empresariais robustos
    indicadores_pj = [
        # Sufixos empresariais comuns
        r"\bLTDA\b", r"\bLIMITADA\b",
        r"\bS\.?A\.?\b", r"\bS/A\b", r"\bSOCIEDADE\s+ANONIMA\b",
        r"\bEIRELI\b",
        r"\bCOMPANHIA\b", r"\bCIA\.?\b",
        r"\b-\s*ME\b", r"\b-\s*EPP\b",  # Microempresa, Empresa de Pequeno Porte
        r"\bHOLDING\b",
        r"\bSOCIEDADE\b",
        r"\bEMPRESA\b",
        r"\bINDUSTRIA\b",
        r"\bCOMERCIO\b",
        
        # Entidades institucionais (✅ FIX CRÍTICO para sindicatos e órgãos públicos)
        r"\bSINDICATO\b",
        r"\bUNIAO\b",  # UNIÃO FEDERAL sem acento
        r"\bMUNICIPIO\b",
        r"\bESTADO\s+(DE|DO|DA)\b",  # ESTADO DE/DO/DA (ex: "ESTADO DO RIO DE JANEIRO")
        r"\bGOVERNO\s+(DO|DA|DE|FEDERAL|ESTADUAL)\b",
        r"\bPREFEITURA\b",
        r"\bBANCO\s+CENTRAL\b",
        r"\bMINISTERIO\b",
        r"\bSECRETARIA\b",
        r"\bAUTARQUIA\b",
        r"\bCONSELHO\b",
        r"\bTRIBUNAL\b",
        r"\bFAZENDA\s+(PUBLICA|NACIONAL|ESTADUAL|MUNICIPAL)\b",
        r"\bPROCURADORIA\b",  # Procuradoria Geral (órgão público)
        r"\bDEFENSORIA\b",   # Defensoria Pública
        r"\bCAMARA\s+(MUNICIPAL|DOS\s+DEPUTADOS)\b",
        r"\bASSEMBLEIA\s+LEGISLATIVA\b",
        r"\bINSS\b",
        r"\bCAIXA\s+ECONOMICA\b",
        
        # Outras entidades coletivas
        r"\bASSOCIACAO\b",
        r"\bCOOPERATIVA\b",
        r"\bFUNDACAO\b",
        r"\bINSTITUTO\b",
        r"\bORGANIZACAO\b",
        r"\bFEDERACAO\b",
        r"\bCONFEDERACAO\b"
    ]
    
    # Verifica se algum indicador está presente (com word boundaries)
    for indicador in indicadores_pj:
        if re.search(indicador, nome_normalizado):
            return True
    
    return False

def is_pessoa_fisica(nome: str) -> bool:
    """
    Detecta se um nome é provavelmente de pessoa física através de padrões típicos.
    
    Indicadores:
    - Presença de prenomes compostos típicos (ex: "MARIA", "JOSÉ", "JOÃO")
    - Ausência de sufixos empresariais
    - Formato de nome completo (2+ palavras sem indicadores PJ)
    
    Returns:
        True se detectar padrões de PF, False caso contrário
    """
    if not nome:
        return False
    
    nome_upper = normalize(nome).upper()
    
    # Se já detectou PJ, não é PF
    if is_pessoa_juridica(nome):
        return False
    
    # Prenomes brasileiros comuns (forte indicador de PF)
    prenomes_comuns = [
        r"\bMARIA\b", r"\bJOS[EÉ]\b", r"\bJO[AÃ]O\b", r"\bANT[OÔ]NIO\b", r"\bANA\b",
        r"\bPAULO\b", r"\bPEDRO\b", r"\bFRANCISCO\b", r"\bCARL(OS|A)\b", r"\bFERNAND(O|A)\b",
        r"\bLUCIA\b", r"\bLUIZ\b", r"\bMARCOS\b", r"\bPAULA\b", r"\bROBERT(O|A)\b",
        r"\bADRIAN(O|A)\b", r"\bANDR[EÉ](A)?\b", r"\bBRUNO\b", r"\bRAFAEL\b", r"\bGABRIEL(A)?\b"
    ]
    
    # Verifica prenomes comuns
    for prenome in prenomes_comuns:
        if re.search(prenome, nome_upper):
            return True
    
    # Se tem 2+ palavras e não tem indicadores PJ, provavelmente é PF
    palavras = nome_upper.split()
    if len(palavras) >= 2:
        # Verifica se tem partículas típicas de nomes (DE, DA, DOS, DAS, DO)
        particulas_nome = ["DE", "DA", "DO", "DOS", "DAS", "E"]
        tem_particula = any(p in palavras for p in particulas_nome)
        
        # Nome com 3+ palavras ou com partículas → provável PF
        if len(palavras) >= 3 or tem_particula:
            return True
    
    return False

# --- API ESTÁVEL ---

def parse_numero_processo_cnj(text: str) -> Optional[str]:
    m = CNJ_RE.search(text or "")
    return m.group(0) if m else None

# Alias para compatibilidade retroativa
def parse_numero_processo(text: str) -> Optional[str]:
    return parse_numero_processo_cnj(text)

def extract_estado_sigla(text: str, cnj: str = None) -> str | None:
    """
    Extrai a sigla UF do estado a partir do texto do PDF.
    
    Ordem de prioridade:
    1. UF explícita após município (ex: "João Pessoa/PB", "Campina Grande-PB")
    2. UF em contexto de localização (ex: "Comarca de X-PB", "Foro de X/PB")
    3. UF explícita (ex: "Estado: PB", "UF: PB")
    4. TRT do texto → UF via mapeamento (com desambiguação para TRTs multi-estado)
    5. TRT do CNJ → UF via mapeamento (com desambiguação para TRTs multi-estado)
    6. UF isolada próximo a contexto judicial
    
    Para TRTs multi-estado (08, 10, 11, 14), usa desambiguação baseada em
    palavras-chave do texto (cidades, estados) para escolher a UF correta.
    """
    t = text or ""
    
    # Prioridade 1: UF após nome de município (padrão: "Município/UF" ou "Município-UF")
    m = re.search(r'[A-ZÀÂÁÃÉÊÍÓÔÕÚ][a-zàâáãçéêíóôõú]+(?:\s+[A-ZÀÂÁÃÉÊÍÓÔÕÚ][a-zàâáãçéêíóôõú]+)*\s*[-/]\s*' + rf'({UF})\b', t, flags=re.I)
    if m:
        return m.group(1).upper()
    
    # Prioridade 2: UF em contexto de localização (Comarca, Foro, Vara)
    m = re.search(rf'(?:Comarca|Foro|F[óo]rum|Vara)\s+(?:Trabalhista\s+)?(?:de|do|da)\s+[A-ZÁÂÃÉÊÍÓÔÕÚÇ][a-záâãéêíóôõúç]+(?:\s+[A-ZÁÂÃÉÊÍÓÔÕÚÇ][a-záâãéêíóôõúç]+)*\s*[-–/]\s*({UF})\b', t, flags=re.I)
    if m:
        return m.group(1).upper()
    
    # Prioridade 3: UF explícita (ex: "Estado: PB", "UF: PB")
    m = re.search(rf'\b(?:Estado|UF)\s*:\s*({UF})\b', t, flags=re.I)
    if m:
        return m.group(1).upper()
    
    # Prioridade 4: TRT do texto do PDF → UF via mapeamento (com desambiguação)
    trt_texto = extract_trt_from_text(t)
    if trt_texto:
        uf = disambiguate_trt_uf(trt_texto, t)
        if uf:
            return uf
    
    # Prioridade 5: TRT do CNJ (se fornecido) → UF via mapeamento (com desambiguação)
    if cnj:
        trt_cnj = extract_trt_from_cnj(cnj)
        if trt_cnj:
            uf = disambiguate_trt_uf(trt_cnj, t)
            if uf:
                return uf
    
    # Prioridade 6: Hífen seguido de UF (padrão genérico)
    m = re.search(rf"[-–]\s*({UF})\b", t, flags=re.I)
    if m:
        return m.group(1).upper()
    
    # Prioridade 7: UF isolada próximo a contexto judicial (cabeçalho)
    cabecalho = t[:2000]
    ufs_seguras = ['RJ', 'SP', 'MG', 'RS', 'PR', 'SC', 'BA', 'PE', 'CE', 'PA', 'GO', 'MA', 'ES', 'PB', 'RN', 'MT', 'MS', 'PI', 'AL', 'SE', 'RO', 'AC', 'AM', 'RR', 'AP', 'TO', 'DF']
    if re.search(r'\b(?:TRIBUNAL|VARA|JUIZ|JUÍZO|FÓRUM|FORUM|TRT|REGIONAL)\b', cabecalho, re.I):
        for uf_segura in ufs_seguras:
            if re.search(rf'\b{uf_segura}\b', cabecalho):
                return uf_segura
    
    return None

def extract_foro(text: str) -> str | None:
    # pega linhas com FÓRUM TRABALHISTA / FORO TRABALHISTA / JUSTIÇA DO TRABALHO
    m = re.search(r"(F[ÓO]RUM\s+TRABALHISTA[^\n]+|F[ÓO]RO\s+TRABALHISTA[^\n]+|Justi[cç]a do Trabalho[^\n]+)", text or "", flags=re.I)
    return (m.group(1).strip().upper() if m else None)

def parse_vara(text: str) -> str | None:
    m = re.search(r"(\d+\s*ª?\s*Vara do Trabalho[^\n]*)", text or "", flags=re.I)
    return m.group(1).strip() if m else None

def parse_celula(text: str) -> str | None:
    # não inventa; só retorna se houver pista explícita
    m = re.search(r"C[ÉE]LULA\s*:\s*([A-Za-z0-9\s\-/]+)", text or "", flags=re.I)
    return m.group(1).strip() if m else None

def extract_datetime(text: str) -> Optional[str]:
    # Retorna "dd/mm/aaaa hh:mm" se encontrar ambos
    mdata = DATA_RE.search(text or "")
    mhora = HORA_RE.search(text or "")
    if mdata and mhora:
        return f"{mdata.group(1)} {mhora.group(1)}"
    return None

def extract_data_distribuicao(text: str) -> Optional[str]:
    """
    Extrai a data de distribuição do processo.
    
    2025-12-01: OTIMIZAÇÃO COMPLETA - Plano Batman
    - Usa utilitários compartilhados
    - Novos padrões para formatos alternativos
    - Validação de data
    
    1ª Instância: "Distribuído em DD/MM/AAAA"
    2ª Instância: "Data da Autuação: DD/MM/AAAA"
    """
    logger = _extract_logger
    
    if not text:
        return None
    
    # NORMALIZAÇÃO
    text_norm = normalize_text(text)
    
    def format_date(raw: str) -> str:
        return raw.strip().replace('.', '/').replace('-', '/')
    
    # ===== PRIORIDADE 1: Distribuição (1ª instância) =====
    dist_patterns = [
        r'Distribu[íi]d[oa]?\s+em\s*:?\s*(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        r'Data\s+(?:da\s+)?distribui[çc][aã]o\s*[:\s]+(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # 🆕 "Protocolo em DD/MM/AAAA"
        r'Protocola?d?[oa]?\s+em\s*:?\s*(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
    ]
    
    for pattern in dist_patterns:
        m = re.search(pattern, text_norm, re.I)
        if m:
            data = format_date(m.group(1))
            if is_valid_brazilian_date(data):
                logger.debug(f"[DATA_DISTRIBUICAO] ✅ Distribuição: {data}")
                return data
    
    # ===== PRIORIDADE 2: Autuação (2ª instância) =====
    autua_patterns = [
        r'(?:Data\s+da\s+)?Autua[çc][ãa]o(?:\s+em)?\s*:?\s*(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        r'Autuad[oa]\s+em\s*:?\s*(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # 🆕 "Recebido em DD/MM/AAAA" (recursos)
        r'Recebid[oa]\s+em\s*:?\s*(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
    ]
    
    for pattern in autua_patterns:
        m = re.search(pattern, text_norm, re.I)
        if m:
            data = format_date(m.group(1))
            if is_valid_brazilian_date(data):
                logger.debug(f"[DATA_DISTRIBUICAO] ✅ Autuação: {data}")
                return data
    
    # NOTA: Removido fallback "primeira data no cabeçalho" para evitar 
    # capturar datas de assinatura/publicação como distribuição
    
    logger.debug("[DATA_DISTRIBUICAO] ❌ Nenhuma data encontrada")
    return None

def extract_valor_causa(text: str) -> Optional[str]:
    """
    Extrai o valor da causa do PDF.
    Procura por padrões como:
    - "Valor da causa: R$ 462.289,03"
    - "Valor da Causa: 1.234,56"
    - "Valor: R$ 10.000,00"
    
    Retorna o valor formatado como string (ex: "462.289,03")
    """
    t = text or ""
    
    # Padrão 1: "Valor da causa: R$ X.XXX,XX" (mais comum em capas de processo)
    m = re.search(r"Valor\s+da\s+[Cc]ausa\s*[:\-]?\s*R?\$?\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)", t, re.I)
    if m:
        return m.group(1).strip()
    
    # Padrão 2: "Valor: R$ X.XXX,XX" (alternativo)
    m = re.search(r"(?<!\w)Valor\s*[:\-]\s*R?\$?\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)", t, re.I)
    if m:
        return m.group(1).strip()
    
    return None

def detect_orgao_origem_instancia(text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Retorna (orgao, origem, instancia)
      - orgao: "TRT-1", "STF", "STJ", "TRF-3", "JF", etc
      - origem: "TRT", "TST", "STF", "STJ", "TRF", "JF", "PROCON", "RECEITA FEDERAL", "Prefeitura", "ÓRGÃO ADMINISTRATIVO"
      - instancia: "1ª Instância" / "2ª Instância" / "3ª Instância" / None
    
    ✅ PRIORIDADE CORRIGIDA: Detecta tribunais regionais ANTES de superiores para evitar
       falsos positivos com citações jurídicas (ex: "DECISÃO DO STF" em petições TRT).
       
    Ordem de detecção:
    1. Tribunais Regionais (TRT, TRF, JF) - aparecem no cabeçalho oficial
    2. Tribunais Superiores (STF, STJ, TST) - frequentemente citados no corpo do texto
    3. Órgãos Administrativos (PROCON, etc)
    """
    t = text or ""
    
    # ===== JUSTIÇA DO TRABALHO (prioridade 1) =====
    # TRT (Tribunal Regional do Trabalho) - pode ser 1ª ou 2ª instância
    m_trt = TRT_RE.search(t)
    if m_trt:
        n = m_trt.group(1)
        trt_orgao = f"TRT-{n}"
        
        # ✅ FIX: Detectar 2ª instância APENAS com sinais no cabeçalho (primeiros 1500 chars)
        # para evitar falsos positivos de citações de jurisprudência
        cabecalho = t[:1500]  # Primeiros 1500 caracteres = cabeçalho do documento
        
        # Sinais FORTES de 2ª instância (devem aparecer no cabeçalho)
        sinais_2a_cabecalho = [
            r"\bRecurso\s+Ordin[aá]rio",  # RO/ROT no cabeçalho
            r"\bRecurso\s+de\s+Revista",  # RR no cabeçalho
            r"\bAgravo",                   # Agravo de Petição/Instrumento
            r"\bAc[óo]rd[aã]o",           # Acórdão (decisão colegiada)
            r"\bTurma\b",                  # Turma do Tribunal
            r"\bRelator[:\-]",             # Relator (desembargador)
            r"2[aª]\s+Inst[aâ]ncia",      # Menção explícita
            r"\bDesembargador",            # Desembargador (2ª instância)
        ]
        
        for padrao in sinais_2a_cabecalho:
            if re.search(padrao, cabecalho, re.I):
                return trt_orgao, "TRT", "2ª Instância"
        
        # Sinais de 1ª instância (petições iniciais em Vara):
        sinais_1a_instancia = [
            r"A[çc][aã]o\s+Trabalhista",   # Ação Trabalhista
            r"\bRito\s+(Ordin[aá]rio|Sumar[ií]ssimo)",  # Rito (só 1ª instância)
            r"\bPeti[çc][aã]o\s+Inicial",  # Petição Inicial
            r"Distribu[ií]do?\s+em",       # Distribuição (ações usam, recursos usam autuação)
        ]
        
        for padrao in sinais_1a_instancia:
            if re.search(padrao, t, re.I):
                return trt_orgao, "TRT", "1ª Instância"
        
        # Se menciona Vara, é 1ª instância
        if VARA_RE.search(t):
            return trt_orgao, "TRT", "1ª Instância"
        
        # Fallback: Indeterminada
        return trt_orgao, "TRT", None
    
    # Se menciona Vara do Trabalho mas não detectou TRT, assume TRT desconhecido
    if VARA_RE.search(t):
        return None, "TRT", "1ª Instância"
    
    # ===== JUSTIÇA FEDERAL (prioridade 2) =====
    # TRF (Tribunal Regional Federal) - 2ª instância da Justiça Federal
    m_trf = TRF_RE.search(t)
    if m_trf:
        n = m_trf.group(1)
        return f"TRF-{n}", "TRF", "2ª Instância"
    
    # JF (Justiça Federal/Vara Federal) - 1ª instância
    if JF_RE.search(t):
        return "JF", "JF", "1ª Instância"
    
    # ===== TRIBUNAIS SUPERIORES (prioridade 3 - citações frequentes) =====
    if STF_RE.search(t):
        return "STF", "STF", "3ª Instância"
    
    if STJ_RE.search(t):
        return "STJ", "STJ", "3ª Instância"
    
    if TST_RE.search(t):
        return "TST", "TST", "3ª Instância"
    
    # ===== ÓRGÃOS ADMINISTRATIVOS (prioridade 4) =====
    if PROCON_RE.search(t):
        return "PROCON", "PROCON", None
    
    if RECEITA_FEDERAL_RE.search(t):
        return "Receita Federal", "RECEITA FEDERAL", None
    
    if PREFEITURA_RE.search(t):
        return "Prefeitura", "Prefeitura", None
    
    if ORGAO_ADMIN_RE.search(t):
        return "Órgão Administrativo", "ÓRGÃO ADMINISTRATIVO", None
    
    return None, None, None

def _strip_id(line: str) -> str:
    # remove “CPF …”, “CNPJ …”, refs de página e lixo comum
    s = re.sub(PESSOA_FISICA_RE, "", line)
    s = re.sub(PESSOA_JURIDICA_RE, "", s)
    s = re.sub(r"PAGINA?_CAPA_PROCESSO_PJE", "", s, flags=re.I)
    return normalize(s)

def _cpf_cnpj_near(line: str) -> Optional[str]:
    m = PESSOA_FISICA_RE.search(line)
    if m: return m.group(2)
    m = PESSOA_JURIDICA_RE.search(line)
    if m: return m.group(2)
    return None

def parse_cliente_parte(text: str, cliente_hint: Optional[str] = None) -> Dict[str, str]:
    """
    Mapeia corretamente parte interessada (cliente) e parte adversa.
    
    ✅ SUPORTE A MÚLTIPLOS RECLAMADOS: Quando há múltiplos reclamados (ex: pessoa física + empresa),
    prioriza a pessoa jurídica (empresa) como cliente.
    
    ✅ NOVO: MÚLTIPLOS CLIENTES NO POLO: Quando há múltiplos clientes conhecidos no polo reclamado
    (ex: CBSI + CSN), aplica regra de prioridade e retorna ambos (principal + secundário).
    
    Args:
        text: Texto do PDF
        cliente_hint: Dica opcional do cliente (DEPRECATED - mantido por compatibilidade)
    
    Returns:
        Dicionário com campos legados (retrocompatibilidade) + novos campos opcionais:
        
        Legados:
        - nome_reclamado: Nome bruto do RECLAMADO extraído (prioriza PJ)
        - nome_reclamante: Nome bruto do RECLAMANTE extraído
        - parte_interessada: Cliente identificado (com heurística)
        - parte_adversa_nome: Parte adversa identificada
        - parte: Posição do cliente (RECLAMADO/RECLAMANTE)
        - posicao_parte_interessada: Alias de 'parte'
        
        Novos (opcionais):
        - cliente_principal: Cliente com maior prioridade (ex: "CBSI")
        - cliente_secundario: Segundo cliente se existir (ex: "CSN") ou None
        - clientes_encontrados: Lista de todos os clientes conhecidos detectados
    """
    # Importações lazy para evitar circular import
    from extractors.brand_map import find_cliente_by_parte_interessada
    
    t = text or ""
    reclte = RECLAMANTE_RE.search(t)
    
    # ✅ BUSCA TODOS OS RECLAMADOS (não apenas o primeiro)
    todos_reclamados = RECLAMADO_RE.findall(t)
    
    # ✅ NOVO: Captura o TERMO ORIGINAL usado no PDF (RÉU, AUTOR, RECLAMADO, etc)
    termo_parte_autora = None
    termo_parte_reclamada = None
    
    if reclte:
        # Extrai o label antes dos dois pontos (AUTOR:, RECLAMANTE:, etc)
        match_label = re.search(r'(RECLAMANTE|RECORRENTE|AUTOR|APELANTE|AGRAVANTE|EMBARGANTE|EXEQUENTE|IMPETRANTE|IMPUGNANTE|DENUNCIANTE|EXCIPIENTE|OPOENTE|REQUERENTE|SUSCITANTE|NOTIFICANTE|INTERPELANTE|DEMANDANTE|DEPRECANTE|AUTUANTE|HABILITANTE|INVENTARIANTE|CONSIGNANTE|ALIMENTANTE|INTERDITANTE|RECOVINTE|REQUENTE)\s*[:\-]', reclte.group(0), re.I)
        if match_label:
            termo_parte_autora = match_label.group(1).upper()
    
    # 🔒 BUSCA O LABEL DA PARTE RECLAMADA - Itera por todos os matches até achar válido
    # Lista de TODOS os 61 labels conhecidos do eLaw (para filtrar "PARTES:" como título)
    KNOWN_LABELS = {
        # Polo ativo (autores)
        "RECLAMANTE", "RECORRENTE", "AUTOR", "APELANTE", "AGRAVANTE", "EMBARGANTE",
        "EXEQUENTE", "IMPETRANTE", "IMPUGNANTE", "DENUNCIANTE", "EXCIPIENTE", "OPOENTE",
        "REQUERENTE", "SUSCITANTE", "NOTIFICANTE", "INTERPELANTE", "DEMANDANTE",
        "DEPRECANTE", "AUTUANTE", "HABILITANTE", "INVENTARIANTE", "CONSIGNANTE",
        "ALIMENTANTE", "INTERDITANTE", "RECOVINTE", "REQUENTE",
        # Polo passivo (réus)
        "RECLAMADO", "RECORRIDO", "RÉU", "REU", "APELADO", "AGRAVADO", "EMBARGADO",
        "EXECUTADO", "IMPETTRADO", "IMPUGNADO", "DENUNCIADO", "EXCEPTO", "REQUERIDO",
        "SUSCITADO", "NOTIFICADO", "INTERPELADO", "DEMANDADO", "DEPRECADO", "AUTUADO",
        "HABILITADA", "INVENTARIADO", "CONSIGNADOV", "INTERDITADO", "RECOVINDO",
        "OPOSTO", "BENEFICIÁRIO", "BENEFICIARIO", "CONFINANTE", "CREDOR", "DEVEDOR",
        "FALÊNCIA", "FALENCIA", "HABTE", "INVENT", "REQT", "TERCEIRO INTERESSADO",
        # Outros
        "ADVOGADO"
    }
    
    # Itera por TODOS os matches até encontrar um label válido (não "PARTES:" como título)
    for reclamado_match_full in RECLAMADO_RE.finditer(t):
        match_label = re.search(r'(RECLAMADO|RECORRIDO|R[ÉE]U|REU|APELADO|AGRAVADO|EMBARGADO|EXECUTADO|IMPETTRADO|IMPUGNADO|DENUNCIADO|EXCEPTO|REQUERIDO|SUSCITADO|NOTIFICADO|INTERPELADO|DEMANDADO|DEPRECADO|AUTUADO|HABILITADA|INVENTARIADO|CONSIGNADOV|INTERDITADO|RECOVINDO|OPOSTO|BENEFICI[ÁA]RIO|CONFINANTE|CREDOR|DEVEDOR|FAL[ÊE]NCIA|HABTE|INVENT|ADVOGADO|PARTES|REQT|TERCEIRO\s+INTERESSADO)\s*[:\-]', reclamado_match_full.group(0), re.I)
        if match_label:
            label_candidato = match_label.group(1).upper()
            
            # 🔒 FILTRO INTELIGENTE: Ignora "PARTES:" quando é apenas título de seção
            # Problema: "Partes:\nAUTOR: Nome" casa com regex e captura "AUTOR: Nome" como valor
            # Solução: Se valor capturado COMEÇA COM outro label conhecido, é título de seção
            if label_candidato == "PARTES":
                valor_apos_partes = reclamado_match_full.group(1).strip().upper()
                
                # Se valor começa com qualquer label conhecida, "PARTES:" era apenas título - SKIP
                if any(valor_apos_partes.startswith(lbl) for lbl in KNOWN_LABELS):
                    continue  # Pula este match e tenta o próximo
            
            # 🔒 FILTRO: Ignora "ADVOGADO:" pois não é posição de parte interessada
            if label_candidato == "ADVOGADO":
                continue  # Pula e tenta o próximo
            
            # Label válido encontrado!
            termo_parte_reclamada = label_candidato
            break  # Para de iterar
    
    nome_reclamante = _strip_id(reclte.group(1)) if reclte else ""
    
    # Limpa nomes dos reclamados
    reclamados_limpos = []
    for recldo_nome in todos_reclamados:
        nome_limpo = _strip_id(recldo_nome)
        # corta ruído comum
        for tok in (r"\bADVOGADO\b", r"\bRECLAMADO\b", r"\bRECLAMANTE\b"):
            nome_limpo = re.split(tok, nome_limpo, flags=re.I)[0].strip()
        if nome_limpo:
            reclamados_limpos.append(nome_limpo)
    
    # ✅ NOVO: Detecta TODAS as partes interessadas conhecidas no polo reclamado
    partes_conhecidas = []
    cliente_detectado = None
    clientes_detectados = []  # Mantido para retrocompatibilidade
    
    for nome in reclamados_limpos:
        cliente = find_cliente_by_parte_interessada(nome, threshold=85)
        if cliente:
            partes_conhecidas.append(nome)
            if not cliente_detectado:
                cliente_detectado = cliente  # Todos pertencem ao mesmo cliente (ex: CSN)
            # Mantém estrutura legada
            clientes_detectados.append({"cliente": cliente, "parte": nome})
    
    # ✅ NOVO: Aplica regra de prioridade se há múltiplas partes (ex: CBSI + CSN MINERAÇÃO)
    from extractors.client_priority import assign_primary_secondary_partes
    parte_principal, parte_secundaria = assign_primary_secondary_partes(partes_conhecidas)
    
    # Mantém variáveis legadas para retrocompatibilidade
    # IMPORTANTE: Mapeamos as PARTES para os campos legados de "cliente" para não quebrar consumidores
    cliente_principal = parte_principal  # Parte prioritária (ex: "CBSI LTDA")
    cliente_secundario = parte_secundaria  # Segunda parte se existir (ex: "CSN MINERAÇÃO")
    
    # ✅ ESTRATÉGIA DE PRIORIZAÇÃO (ORDEM IMPORTA):
    # 1º: Se há cliente conhecido com maior prioridade, usa ele
    # 2º: Senão, busca qualquer empresa (PJ com CNPJ ou sufixos LTDA/SA)
    # 3º: Se não achou nada, pega o último reclamado (fallback)
    
    nome_reclamado = ""
    id_reclamado = None
    
    # 1º PRIORIDADE: Cliente conhecido detectado
    if parte_principal:
        nome_reclamado = parte_principal
        # Busca CNPJ do cliente conhecido
        nome_original = [n for n in todos_reclamados if _strip_id(n) == parte_principal]
        if nome_original:
            id_reclamado = _cpf_cnpj_near(nome_original[0])
    
    # 2º PRIORIDADE: Qualquer empresa com CNPJ ou indicadores (LTDA, S.A, etc)
    if not nome_reclamado:
        for nome in reclamados_limpos:
            # Busca CNPJ no nome original (antes de limpar)
            nome_original = [n for n in todos_reclamados if _strip_id(n) == nome]
            texto_busca = nome_original[0] if nome_original else nome
            
            id_temp = _cpf_cnpj_near(texto_busca)
            if id_temp and len(_digits(id_temp)) == 14:
                # É CNPJ (empresa)
                nome_reclamado = nome
                id_reclamado = id_temp
                break
            elif "COMPANHIA" in nome.upper() or "LTDA" in nome.upper() or "S.A" in nome.upper() or "EIRELI" in nome.upper():
                # Tem indicador de empresa
                nome_reclamado = nome
                id_reclamado = id_temp or _cpf_cnpj_near(texto_busca)
                break
    
    # 3º PRIORIDADE: Fallback - último reclamado
    if not nome_reclamado and reclamados_limpos:
        nome_reclamado = reclamados_limpos[-1]
        nome_original = [n for n in todos_reclamados if _strip_id(n) == nome_reclamado]
        if nome_original:
            id_reclamado = _cpf_cnpj_near(nome_original[0])
    
    # Limpa reclamante
    for tok in (r"\bADVOGADO\b", r"\bRECLAMADO\b", r"\bRECLAMANTE\b"):
        nome_reclamante = re.split(tok, nome_reclamante, flags=re.I)[0].strip()

    id_reclamante = _cpf_cnpj_near(reclte.group(0)) if reclte else None

    # Detecção híbrida: CPF/CNPJ + padrões de nome
    reclamante_pf = bool(id_reclamante and len(_digits(id_reclamante)) == 11)
    reclamado_pj  = bool(id_reclamado and len(_digits(id_reclamado)) == 14) or ("COMPANHIA" in nome_reclamado.upper())
    
    # ✅ FIX CRÍTICO: Detecção inteligente quando CPF/CNPJ ausente
    # Se não tem CPF mas nome indica PF, marca como PF
    if not reclamante_pf and is_pessoa_fisica(nome_reclamante):
        reclamante_pf = True
    # Se não tem CNPJ mas nome indica PJ, marca como PJ
    if not reclamado_pj and is_pessoa_juridica(nome_reclamado):
        reclamado_pj = True
    
    # ✅ FIX CRÍTICO: Se detectamos cliente conhecido, forçamos reclamado_pj = True
    # Isso garante que entramos no branch correto mesmo quando CNPJ não tem label "CNPJ:"
    if parte_principal and not reclamado_pj:
        reclamado_pj = True

    out: Dict[str, str] = {}
    
    # ✅ SEMPRE expõe os nomes brutos para permitir extração genérica
    out["nome_reclamado"] = nome_reclamado
    out["nome_reclamante"] = nome_reclamante

    # Heurística: Se reclamante é PF OU reclamado é PJ → cliente é RECLAMADO (caso trabalhista típico)
    if reclamante_pf or reclamado_pj:
        # ✅ USA O TERMO ORIGINAL DO PDF (RÉU, RECLAMADO, etc) ao invés de normalizar
        termo_final = termo_parte_reclamada if termo_parte_reclamada else "RECLAMADO"
        out["parte"] = out["posicao_parte_interessada"] = termo_final
        
        # ✅ PRIORIZA: Se temos cliente conhecido (parte_principal), usa ele. Caso contrário, usa nome_reclamado
        parte_a_usar = parte_principal if parte_principal else nome_reclamado
        
        out["parte_interessada"] = parte_a_usar or "Cliente Não Identificado"
        out["parte_adversa_nome"] = nome_reclamante or "PARTE ADVERSA"
        
        # ✅ DETECÇÃO INTELIGENTE: CPF/CNPJ primeiro, fallback para padrões de nome, default PF (trabalhista)
        if reclamante_pf:
            out["parte_adversa_tipo"] = "PESSOA FISICA"
        elif is_pessoa_juridica(nome_reclamante):
            out["parte_adversa_tipo"] = "PESSOA JURIDICA"
        else:
            # Fallback: assumir PF (padrão em ações trabalhistas, minimiza risco regulatório)
            out["parte_adversa_tipo"] = "PESSOA FISICA"
        
        out["cpf_cnpj_parte_adversa"] = _digits(id_reclamante) if id_reclamante else ""
        
        # ✅ NOVO: Adiciona campos de múltiplas partes interessadas (opcionais)
        out["parte_interessada_principal"] = parte_principal
        out["outra_parte_interessada"] = parte_secundaria
        out["cliente_detectado"] = cliente_detectado
        # Campos legados (deprecated mas mantidos por compatibilidade)
        out["cliente_principal"] = cliente_principal
        out["cliente_secundario"] = cliente_secundario
        out["clientes_encontrados"] = [c["cliente"] for c in clientes_detectados] if clientes_detectados else []
        
        return out

    # Fallback: Verifica se cliente_hint (legado) está no RECLAMADO
    if cliente_hint and nome_reclamado and (cliente_hint.lower() in nome_reclamado.lower()):
        # ✅ USA O TERMO ORIGINAL DO PDF (RÉU, RECLAMADO, etc) ao invés de normalizar
        termo_final = termo_parte_reclamada if termo_parte_reclamada else "RECLAMADO"
        out["parte"] = out["posicao_parte_interessada"] = termo_final
        out["parte_interessada"] = nome_reclamado or cliente_hint
        out["parte_adversa_nome"] = nome_reclamante or "PARTE ADVERSA"
        
        # ✅ DETECÇÃO INTELIGENTE: CPF/CNPJ primeiro, fallback para padrões de nome, default PF (trabalhista)
        if reclamante_pf:
            out["parte_adversa_tipo"] = "PESSOA FISICA"
        elif is_pessoa_juridica(nome_reclamante):
            out["parte_adversa_tipo"] = "PESSOA JURIDICA"
        else:
            # Fallback: assumir PF (padrão em ações trabalhistas, minimiza risco regulatório)
            out["parte_adversa_tipo"] = "PESSOA FISICA"
        
        out["cpf_cnpj_parte_adversa"] = _digits(id_reclamante) if id_reclamante else ""
        
        # ✅ NOVO: Adiciona campos de múltiplas partes interessadas (opcionais)
        out["parte_interessada_principal"] = parte_principal
        out["outra_parte_interessada"] = parte_secundaria
        out["cliente_detectado"] = cliente_detectado
        # Campos legados (deprecated mas mantidos por compatibilidade)
        out["cliente_principal"] = cliente_principal
        out["cliente_secundario"] = cliente_secundario
        out["clientes_encontrados"] = [c["cliente"] for c in clientes_detectados] if clientes_detectados else []
        
        return out

    # Caso padrão: cliente como RECLAMANTE (menos comum em casos trabalhistas)
    # ✅ USA O TERMO ORIGINAL DO PDF (AUTOR, RECLAMANTE, etc) ao invés de normalizar
    termo_final_autora = termo_parte_autora if termo_parte_autora else "RECLAMANTE"
    out["parte"] = out["posicao_parte_interessada"] = termo_final_autora
    out["parte_interessada"] = nome_reclamante or "Cliente Não Identificado"
    out["parte_adversa_nome"] = nome_reclamado or "PARTE ADVERSA"
    
    # ✅ DETECÇÃO INTELIGENTE: CNPJ primeiro, fallback para padrões de nome, default PJ (adverso típico)
    if reclamado_pj:
        out["parte_adversa_tipo"] = "PESSOA JURIDICA"
    elif is_pessoa_fisica(nome_reclamado):
        out["parte_adversa_tipo"] = "PESSOA FISICA"
    else:
        # Fallback: neste branch raro (cliente=reclamante), adverso geralmente é PJ
        out["parte_adversa_tipo"] = "PESSOA JURIDICA"
    
    out["cpf_cnpj_parte_adversa"] = _digits(id_reclamado) if id_reclamado else ""
    
    # ✅ NOVO: Adiciona campos de múltiplos clientes (opcionais)
    out["cliente_principal"] = cliente_principal or None
    out["cliente_secundario"] = cliente_secundario or None
    out["clientes_encontrados"] = [c["cliente"] for c in clientes_detectados] if clientes_detectados else []
    
    return out

# Assunto/Objeto
def assunto_from_text(t: str) -> Optional[str]:
    if re.search(r"reclama[cç][aã]o\s+trabalhista", (t or ""), re.I):
        return "Reclamação Trabalhista No Rito Ordinário"
    return None

def objeto_from_text(t: str) -> Optional[str]:
    if re.search(r"verbas?\s+rescis[oó]rias", (t or ""), re.I):
        return "Verbas rescisórias"
    return None

# --- Adições em extractors/regex_utils.py ---

def parse_numero_orgao_from_cnj(cnj: str) -> str | None:
    """
    Retorna OOOO do CNJ  nnnnnnn-dd.aaaa.j.tr.oooo  -> oooo
    """
    if not cnj:
        return None
    m = re.search(r'\b\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.(\d{4})\b', cnj)
    return m.group(1) if m else None

def parse_comarca(text: str, cnj: str = None) -> str | None:
    """
    Extrai Comarca/Cidade do texto do PDF trabalhista.
    
    Suporta múltiplos formatos:
    - "FÓRUM TRABALHISTA DE JOÃO PESSOA - PB"
    - "Fórum Trabalhista de Campina Grande/PB"
    - "Comarca de João Pessoa"
    - "1ª Vara do Trabalho de Guarabira"
    - "Justiça do Trabalho de Campina Grande"
    - "Juízo de Patos - PB"
    - "PODER JUDICIÁRIO - JUSTIÇA DO TRABALHO - TRT 13ª REGIÃO - VARA DO TRABALHO DE GUARABIRA"
    
    Returns:
        "Cidade - UF" ou apenas "Cidade" se UF não encontrada
    """
    if not text:
        return None
    
    t = text
    cidade = None
    uf = None
    
    # Padrão 1: FÓRUM TRABALHISTA DE CIDADE - UF
    m = re.search(r'F[ÓO]RUM\s+TRABALHISTA\s+DE\s+([A-ZÁÂÃÉÊÍÓÔÕÚÇ][A-ZÁÂÃÉÊÍÓÔÕÚÇa-záâãéêíóôõúç\s]+?)\s*[-–/]\s*([A-Z]{2})\b', t, re.I)
    if m:
        cidade, uf = m.group(1).strip(), m.group(2).strip().upper()
    
    # Padrão 2: Comarca de CIDADE (com ou sem UF)
    if not cidade:
        m = re.search(r'\bComarca\s+(?:de|do|da)\s+([A-ZÁÂÃÉÊÍÓÔÕÚÇ][A-ZÁÂÃÉÊÍÓÔÕÚÇa-záâãéêíóôõúç\s]+?)(?:\s*[-–/]\s*([A-Z]{2}))?\b', t, re.I)
        if m:
            cidade = m.group(1).strip()
            if m.group(2):
                uf = m.group(2).strip().upper()
    
    # Padrão 3: Vara do Trabalho de CIDADE
    if not cidade:
        m = re.search(r'\bVara\s+do\s+Trabalho\s+de\s+([A-ZÁÂÃÉÊÍÓÔÕÚÇ][A-ZÁÂÃÉÊÍÓÔÕÚÇa-záâãéêíóôõúç\s]+?)(?:\s*[-–/]\s*([A-Z]{2}))?\b', t, re.I)
        if m:
            cidade = m.group(1).strip()
            if m.group(2):
                uf = m.group(2).strip().upper()
    
    # Padrão 4: Foro/Juízo de CIDADE
    if not cidade:
        m = re.search(r'\b(?:Foro|Ju[íi]zo)\s+(?:Trabalhista\s+)?(?:de|do|da)\s+([A-ZÁÂÃÉÊÍÓÔÕÚÇ][A-ZÁÂÃÉÊÍÓÔÕÚÇa-záâãéêíóôõúç\s]+?)(?:\s*[-–/]\s*([A-Z]{2}))?\b', t, re.I)
        if m:
            cidade = m.group(1).strip()
            if m.group(2):
                uf = m.group(2).strip().upper()
    
    # Padrão 5: Justiça do Trabalho de CIDADE
    if not cidade:
        m = re.search(r'\bJusti[çc]a\s+do\s+Trabalho\s+de\s+([A-ZÁÂÃÉÊÍÓÔÕÚÇ][A-ZÁÂÃÉÊÍÓÔÕÚÇa-záâãéêíóôõúç\s]+?)(?:\s*[-–/]\s*([A-Z]{2}))?\b', t, re.I)
        if m:
            cidade = m.group(1).strip()
            if m.group(2):
                uf = m.group(2).strip().upper()
    
    # Padrão 6: CIDADE/UF ou CIDADE-UF em contexto judicial (ex: após TRT)
    if not cidade:
        m = re.search(r'(?:TRT|Tribunal|Regi[ãa]o)[^\n]{0,50}?\bde\s+([A-ZÁÂÃÉÊÍÓÔÕÚÇ][a-záâãéêíóôõúç]+(?:\s+[A-ZÁÂÃÉÊÍÓÔÕÚÇ][a-záâãéêíóôõúç]+)*)\s*[-–/]\s*([A-Z]{2})\b', t, re.I)
        if m:
            cidade, uf = m.group(1).strip(), m.group(2).strip().upper()
    
    if cidade:
        # Limpar cidade (remover termos irrelevantes)
        cidade = re.split(r'\bsegredo\b|\bju[ií]za?\b|\bProcesso\b|\bATOS\b|\bPJ[eE]\b', cidade, flags=re.I)[0].strip()
        cidade = re.sub(r'\s{2,}', ' ', cidade)
        cidade = re.sub(r'\s*[-–/]\s*$', '', cidade).strip()
        
        # Normalizar capitalização
        cidade = cidade.title()
        
        # Buscar UF se ainda não encontrada
        if not uf:
            uf = extract_estado_sigla(t, cnj) or ""
        
        return f"{cidade} - {uf}" if uf else cidade
    
    return None


def extract_cpf_cnpj_near(text: str, name: str, window: int = 200) -> str | None:
    """
    Procura CPF/CNPJ numa janela ao redor do nome informado.
    Normaliza espaços/acentos e, se não achar “perto”, usa o único CPF/CNPJ do PDF.
    """
    if not text or not name:
        return None

    def _norm(s: str) -> str:
        s = re.sub(r"\s+", " ", s or "")
        try:
            from unicodedata import normalize as _u
            s = _u("NFKD", s).encode("ascii", "ignore").decode("ascii")
        except Exception:
            pass
        return s

    T = _norm(text)
    N = _norm(name)

    for m in re.finditer(re.escape(N), T, flags=re.I):
        i = m.end()
        snip = T[max(0, i - window): i + window]
        mcpf = re.search(_CPF_RE, snip)
        if mcpf:
            return _digits(mcpf.group(1))
        mcnpj = re.search(_CNPJ_RE, snip)
        if mcnpj:
            return _digits(mcnpj.group(1))

    all_ids = re.findall(_CPF_RE, T) + re.findall(_CNPJ_RE, T)
    if len(all_ids) == 1:
        return _digits(all_ids[0])
    return None

def subobj_from_text(text: str) -> str:
    """
    Amplia padrões de sub-objeto. Se nada for encontrado,
    usa heurística com termos comuns do trabalhista.
    """
    t = (text or "").replace("\n", " ")

    # Sub-objeto explícito
    m = re.search(r'(?:sub-?objeto|assunto\s+complementar|pedido(?:\s+principal)?)\s*[:\-]\s*([^\n\r]{3,160})', t, re.I)
    if m:
        return m.group(1).strip()

    # Heurísticas relevantes
    if re.search(r'verbas?\s+rescis[óo]rias?', t, re.I):
        return 'Verbas rescisórias'

    if re.search(r'horas?\s+extras?', t, re.I):
        return 'Horas extras'

    return ""

def _digits(s: str | None) -> str:
    return re.sub(r'\D', '', s or '')


def extract_data_hora_audiencia(text: str) -> tuple[Optional[str], Optional[str]]:
    """
    Extrai data e hora de audiência do texto do PDF.
    
    2025-12-01: OTIMIZAÇÃO COMPLETA - Plano Batman
    - Busca em contexto de audiência
    - Suporta múltiplos formatos de data/hora
    - Validação de data
    
    Formatos de hora suportados:
    - "13:30", "13h30", "13 h 30", "13h", "às 13h30"
    
    Returns:
        Tuple (data, hora) no formato ("DD/MM/AAAA", "HH:MM") ou (None, None)
    """
    logger = _extract_logger
    
    if not text:
        return None, None
    
    # NORMALIZAÇÃO
    text_norm = normalize_text(text)
    
    def format_date(raw: str) -> str:
        return raw.strip().replace('.', '/').replace('-', '/')
    
    def extract_hora_from_context(context: str) -> Optional[str]:
        """Extrai hora do contexto em vários formatos portugueses.
        
        IMPORTANTE: Só retorna hora se minutos estiverem explícitos.
        Não fabrica ":00" para horas sem minutos - isso viola ZERO ERRORS.
        """
        # Padrões de hora COM minutos explícitos (ordem de prioridade):
        hora_patterns = [
            r'(\d{1,2})\s*[hH]\s*(\d{2})',  # 13h30, 13 h 30, 13H30
            r'(\d{1,2})\s*:\s*(\d{2})',  # 13:30
            r'[àa]s?\s+(\d{1,2})\s*[hH]\s*(\d{2})',  # às 13h30
            r'[àa]s?\s+(\d{1,2})\s*:\s*(\d{2})',  # às 13:30
        ]
        
        for pattern in hora_patterns:
            match = re.search(pattern, context, re.I)
            if match:
                h = match.group(1).zfill(2)
                m = match.group(2)
                return f"{h}:{m}"
        
        # NOTA: Removido fallback para horas sem minutos (9h, 10h)
        # Fabricar ":00" viola a regra ZERO ERRORS
        # Se precisar da hora sem minutos, retornar None e deixar o usuário preencher
        
        return None
    
    # Padrões de data
    data_pattern = r'(\d{2}[/.\-]\d{2}[/.\-]\d{4})'
    
    # Buscar em contexto de audiência
    lines = text_norm.split('\n')
    
    for i, line in enumerate(lines):
        if re.search(r'audi[êe]ncia|design(?:o|a|ada?)|realizar|marcad[oa]|convoc', line, re.I):
            # Contexto: 3 linhas antes e 5 depois
            context_lines = lines[max(0, i-3):min(i+6, len(lines))]
            context = ' '.join(context_lines)
            
            # Buscar data
            data_match = re.search(data_pattern, context)
            if data_match:
                data = format_date(data_match.group(1))
                if not is_valid_brazilian_date(data):
                    continue
                
                # Buscar hora
                hora = extract_hora_from_context(context)
                
                logger.debug(f"[AUDIENCIA] ✅ Data: {data}, Hora: {hora}")
                return data, hora
    
    # Fallback: padrão específico de designação COM minutos explícitos
    # NOTA: Só aceita padrões que incluem minutos para evitar fabricar ":00"
    designacao_patterns = [
        r'design[oa]\s+audi[êe]ncia[^,]*para\s+(?:o\s+dia\s+)?(\d{2}[/.\-]\d{2}[/.\-]\d{4})[,\s]+[àa]s?\s+(\d{1,2})\s*[h:]\s*(\d{2})',
        r'audi[êe]ncia\s+(?:marcada|designada)\s+para\s+(\d{2}[/.\-]\d{2}[/.\-]\d{4})[,\s]+[àa]s?\s+(\d{1,2})\s*[h:]\s*(\d{2})',
        r'realizar[áa]\s+audi[êe]ncia[^,]*em\s+(\d{2}[/.\-]\d{2}[/.\-]\d{4})[,\s]+[àa]s?\s+(\d{1,2})\s*[h:]\s*(\d{2})',
    ]
    
    for pattern in designacao_patterns:
        match = re.search(pattern, text_norm, re.I)
        if match:
            data = format_date(match.group(1))
            if not is_valid_brazilian_date(data):
                continue
            
            # Só retorna hora se minutos estiverem presentes
            if match.group(3):
                h = match.group(2).zfill(2)
                m = match.group(3)
                hora = f"{h}:{m}"
            else:
                hora = None  # Não fabrica :00
            
            logger.debug(f"[AUDIENCIA] ✅ Designação: Data: {data}, Hora: {hora}")
            return data, hora
    
    logger.debug("[AUDIENCIA] ❌ Data/hora não encontrada")
    return None, None


def extract_envolvido_audiencia(text: str) -> Optional[str]:
    """
    Extrai os envolvidos que devem comparecer à audiência do texto do PDF.
    
    Mapeia menções encontradas no PDF para valores do dropdown eLaw:
    - "Advogado e Preposto" → quando menciona ambos
    - "Advogado" → quando menciona apenas advogado
    - "Preposto" → quando menciona apenas preposto
    - "Dispensadas as partes" → quando dispensa comparecimento
    - "Facultada as partes" → quando faculta comparecimento
    
    Args:
        text: Texto completo do PDF
        
    Returns:
        String do envolvido ou None se não detectado
    """
    if not text:
        return None
    
    text_upper = text.upper()
    
    # Procurar padrões de envolvidos próximos ao contexto de audiência
    lines = text.split('\n')
    
    for i, line in enumerate(lines):
        if re.search(r'audi[eê]ncia|audiencia', line, re.I):
            # Contexto: pegar 5 linhas antes e depois
            context_lines = lines[max(0, i-5):min(i+6, len(lines))]
            context = ' '.join(context_lines).upper()
            
            # Padrões específicos (ordem de prioridade)
            
            # 1. Dispensadas as partes
            if re.search(r'DISPENSAD[OA]S?\s+AS\s+PARTES|COMPARECIMENTO\s+DISPENSADO', context):
                return "Dispensadas as partes"
            
            # 2. Facultada as partes
            if re.search(r'FACULTAD[OA]S?\s+AS\s+PARTES|COMPARECIMENTO\s+FACULTADO', context):
                return "Facultada as partes"
            
            # 3. Advogado e Preposto (deve vir antes dos individuais)
            if re.search(r'ADVOGADO.*PREPOSTO|PREPOSTO.*ADVOGADO', context):
                return "Advogado e Preposto"
            
            # 4. Preposto
            if re.search(r'\bPREPOSTO\b', context):
                return "Preposto"
            
            # 5. Advogado
            if re.search(r'\bADVOGAD[OA]S?\b', context):
                return "Advogado"
    
    # Padrões gerais no documento (sem contexto de audiência)
    if re.search(r'INTIM[AE].*ADVOGAD[OA].*PREPOSTO|INTIM[AE].*PREPOSTO.*ADVOGAD[OA]', text_upper):
        return "Advogado e Preposto"
    
    if re.search(r'CARTA\s+DE\s+PREPOSTO|ANEXAR\s+.*PREPOSTO', text_upper):
        return "Preposto"
    
    # Default para audiências trabalhistas: geralmente Advogado e Preposto
    if re.search(r'audi[eê]ncia.*inicial|audiencia.*inicial', text, re.I):
        return "Advogado e Preposto"
    
    return None

def extract_subtipo_audiencia(text: str) -> Optional[str]:
    """
    Extrai o subtipo de audiência do texto do PDF.
    
    Mapeia menções encontradas no PDF para valores do dropdown eLaw:
    - "Una" / "UNA" → "Audiência Inicial Una (IU)"
    - "Não-Una" / "Inicial Não Una" → "Audiência Inicial Não-Una (INU)"
    - "Tentativa de Conciliação" / "ITC" → "Audiência Inicial Apenas Tentativa Conciliação (ITC)"
    - "Homologação de Acordo" → "AUDIÊNCIA HOMOLOGAÇÃO DE ACORDO (HA)"
    
    Args:
        text: Texto completo do PDF
        
    Returns:
        String do subtipo ou None se não detectado
    """
    if not text:
        return None
    
    # Procurar padrões de audiência com contexto
    lines = text.split('\n')
    
    for i, line in enumerate(lines):
        if re.search(r'audi[eê]ncia|audiencia', line, re.I):
            # Contexto: pegar 3 linhas antes e depois
            context_lines = lines[max(0, i-3):min(i+4, len(lines))]
            context = ' '.join(context_lines).upper()
            
            # Padrões específicos (ordem de prioridade)
            if re.search(r'\bUNA\b.*\bTELEPRESENCIAL\b|\bTELEPRESENCIAL\b.*\bUNA\b', context):
                return "Audiência Inicial Una (IU)"
            
            if re.search(r'\bUNA\b(?!\s*[-–]\s*N[ÃA]O)', context):
                return "Audiência Inicial Una (IU)"
            
            if re.search(r'N[ÃA]O\s*[-–]?\s*UNA|INICIAL\s+N[ÃA]O\s+UNA', context):
                return "Audiência Inicial Não-Una (INU)"
            
            if re.search(r'APENAS\s+TENTATIVA\s+CONCILIA[ÇC][ÃA]O|ITC\b', context):
                return "Audiência Inicial Apenas Tentativa Conciliação (ITC)"
            
            if re.search(r'TENTATIVA\s+DE?\s+CONCILIA[ÇC][ÃA]O|CONCILIAT[ÓO]RIA', context):
                return "Audiência Inicial Apenas Tentativa Conciliação (ITC)"
            
            if re.search(r'HOMOLOGA[ÇC][ÃA]O\s+DE?\s+ACORDO', context):
                return "AUDIÊNCIA HOMOLOGAÇÃO DE ACORDO (HA)"
            
            if re.search(r'INSTRU[ÇC][ÃA]O', context):
                return "AUDIÊNCIA ENCERRAMENTO INSTRUÇÃO (ENCINSTR)"
            
            if re.search(r'LEITURA\s+DE?\s+SENTEN[ÇC]A', context):
                return "Audiência Leitura de Sentença (LS)"
    
    return None

def extract_link_audiencia(text: str) -> Optional[str]:
    """
    Extrai link de audiência telepresencial (Zoom, Google Meet, Teams, etc) do texto do PDF.
    
    Procura por URLs próximas a contextos de audiência e reconstrói links quebrados em múltiplas linhas.
    
    Args:
        text: Texto completo do PDF
        
    Returns:
        URL do link de audiência ou None se não encontrado
        
    Examples:
        >>> text = "Designo audiência telepresencial via Zoom: https://trt1-jus-br.zoom.us/my/vt01mac"
        >>> extract_link_audiencia(text)
        'https://trt1-jus-br.zoom.us/my/vt01mac'
    """
    if not text:
        return None
    
    # Procurar por padrões de audiência + link nas proximidades
    lines = text.split('\n')
    
    for i, line in enumerate(lines):
        # Se linha menciona audiência ou "plataforma ZOOM/Meet"
        if re.search(r'audi[eê]ncia|audiencia|plataforma\s+(zoom|meet|teams)', line, re.I):
            # Procurar link nas próximas 10 linhas (aumentado para capturar links quebrados)
            context_lines = lines[i:min(i+11, len(lines))]
            context_text = '\n'.join(context_lines)
            
            # Remover espaços/quebras dentro de URLs (comum em PDFs)
            # Exemplo: "https://zoom.us/j\n/12345" → "https://zoom.us/j/12345"
            context_clean = re.sub(r'(https?://[^\s]*)\s*/\s*', r'\1/', context_text)
            context_clean = re.sub(r'\n(?=[^\s])', '', context_clean)  # Juntar linhas quebradas
            
            # Procurar URLs de plataformas conhecidas (prioridade)
            platforms = [
                r'https?://[^\s]*zoom\.us[^\s]*',
                r'https?://meet\.google\.com[^\s]*',
                r'https?://teams\.microsoft\.com[^\s]*',
                r'https?://[^\s]*\.webex\.com[^\s]*',
            ]
            
            for platform_pattern in platforms:
                match = re.search(platform_pattern, context_clean, re.I)
                if match:
                    url = match.group(0)
                    # Limpar pontuação final e palavras portuguesas grudadas (comum em PDFs)
                    url = re.sub(r'[,;.!?]+$', '', url)
                    url = re.sub(r'(acesso|senha|participante|reuniao|meeting)$', '', url, flags=re.I)
                    return url
            
            # Fallback: qualquer https:// próximo a audiência
            url_match = re.search(r'https?://[^\s]+', context_clean, re.I)
            if url_match:
                url = url_match.group(0)
                # Limpar pontuação final
                url = re.sub(r'[,;.!?]+$', '', url)
                # Filtrar se for apenas link de validação (não é link de audiência)
                if 'validacao' not in url and 'pjekz' not in url:
                    return url
    
    return None

# ==============================================================================
# NOVAS FUNÇÕES DE EXTRAÇÃO PARA CAMPOS ADICIONAIS
# ==============================================================================

def extract_advogados(text: str) -> tuple:
    """
    Extrai advogados do reclamante (autor) e do reclamado (réu).
    
    Returns:
        Tupla (advogado_autor, advogado_reu)
    """
    if not text:
        return None, None
    
    advogado_autor = None
    advogado_reu = None
    
    lines = text.split('\n')
    
    # Detectar tipo de documento (inicial vs recurso)
    is_recurso = bool(re.search(r'RECORRENTE|RECORRIDO|RECURSO\s+ORDINÁRIO', text, re.I))
    
    for i, line in enumerate(lines):
        # Linha com "ADVOGADO:" seguido de nome
        adv_match = re.search(r'ADVOGADO:\s*([A-Z][A-ZÀ-Ü\s]+(?:[A-Z]+)?)', line)
        if adv_match:
            nome_advogado = adv_match.group(1).strip()
            # Limpar lixo comum
            nome_advogado = re.sub(r'\b(RECORRENTE|RECORRIDO|RECLAMANTE|RECLAMADO|ADVOGADO)\b', '', nome_advogado).strip()
            
            if nome_advogado and len(nome_advogado) > 5:
                # Verificar contexto nas linhas anteriores (até 3 linhas antes)
                context_lines = '\n'.join(lines[max(0, i-3):i])
                
                if is_recurso:
                    # Em recursos: RECORRENTE = autor do recurso, RECORRIDO = réu
                    if 'RECORRENTE' in context_lines:
                        if not advogado_autor:  # Pega o primeiro
                            advogado_autor = nome_advogado
                    elif 'RECORRIDO' in context_lines:
                        if not advogado_reu:  # Pega o primeiro
                            advogado_reu = nome_advogado
                else:
                    # Em iniciais: RECLAMANTE = autor, RECLAMADO = réu
                    if 'RECLAMANTE' in context_lines and 'RECLAMADO' not in context_lines:
                        if not advogado_autor:
                            advogado_autor = nome_advogado
                    elif 'RECLAMADO' in context_lines:
                        if not advogado_reu:
                            advogado_reu = nome_advogado
    
    return advogado_autor, advogado_reu


def _identificar_parte_adversa(text: str) -> tuple[str, list[str]]:
    """
    Identifica quem é a parte adversa e retorna marcadores a buscar.
    
    Returns:
        Tupla (nome_parte_adversa, marcadores_a_buscar)
        Ex: ("FABIANA DE FATIMA GRADES", ["RECORRENTE", "FABIANA"])
    """
    # Detectar tipo de documento
    is_recurso = bool(re.search(r'Recurso\s+Ordinário|RECORRENTE|RECORRIDO', text, re.I))
    
    if is_recurso:
        # Em recursos: RECORRENTE = parte adversa
        match = re.search(r'RECORRENTE:\s*([A-ZÀ-Ü\s\.]+?)(?:\n|ADVOGADO)', text, re.I)
        if match:
            nome = match.group(1).strip()
            # Pegar primeira palavra significativa do nome (geralmente nome/sobrenome)
            palavras = [p for p in nome.split() if len(p) > 3]
            return nome, ["RECORRENTE"] + palavras[:2]
    else:
        # Em iniciais: RECLAMANTE = parte adversa
        match = re.search(r'RECLAMANTE:\s*([A-ZÀ-Ü\s\.]+?)(?:\n|ADVOGADO)', text, re.I)
        if match:
            nome = match.group(1).strip()
            palavras = [p for p in nome.split() if len(p) > 3]
            return nome, ["RECLAMANTE"] + palavras[:2]
    
    return "", []


def extract_telefone_parte_adversa(text: str) -> str | None:
    """
    Extrai telefone da parte adversa com verificação rigorosa de contexto.
    Garante que o telefone pertence à parte adversa, não à parte interessada (cliente).
    """
    if not text:
        return None
    
    # ✅ PASSO 1: Identificar quem é a parte adversa
    nome_adversa, marcadores = _identificar_parte_adversa(text)
    if not marcadores:
        return None
    
    # Buscar todos os telefones no documento
    all_telefones = re.findall(r'\(?\s*(\d{2})\s*\)?\s*9?\s*(\d{4,5})[-\s]?(\d{4})', text)
    
    for ddd, parte1, parte2 in all_telefones:
        # Validar que não é número genérico (ex: 0800, 4003, etc)
        if ddd in ['08', '40', '30', '00']:
            continue
        
        # Formatar telefone
        telefone_formatado = f"({ddd}) {parte1}-{parte2}"
        telefone_raw = f"{ddd}{parte1}{parte2}"
        
        # Encontrar contexto onde telefone aparece
        pos = text.find(telefone_raw)
        if pos < 0:
            # Tentar com formatação
            pos = text.find(f"({ddd})")
            
        if pos < 0:
            continue
            
        context = text[max(0, pos-200):min(len(text), pos+200)]
        context_upper = context.upper()
        
        # ✅ FILTRO: Verificar contexto
        marcadores_adversa_encontrados = sum(1 for m in marcadores if m in context_upper)
        
        # Contexto positivo (parte autor/reclamante)
        contexto_positivo = any(marker in context_upper for marker in [
            'PARTE AUTORA', 'RECLAMANTE', 'RECORRENTE', 'CELULAR', 'TELEFONE'
        ] + marcadores)
        
        # Contexto negativo (parte cliente)
        contexto_negativo = any(marker in context_upper for marker in [
            'GRUPO CASAS BAHIA', 'CASAS BAHIA', 'BANQI', 'CNOVA',
            'RECORRIDO', 'RECLAMADO', 'PATRONOS DO'
        ])
        
        # Telefone válido se está em contexto positivo e não negativo
        if (contexto_positivo or marcadores_adversa_encontrados >= 1) and not contexto_negativo:
            return telefone_formatado
    
    return None


def extract_email_parte_adversa(text: str) -> str | None:
    """
    Extrai email da parte adversa com verificação rigorosa de contexto.
    Garante que o email pertence à parte adversa, não à parte interessada (cliente).
    
    Estratégia em 2 fases:
    1. Buscar próximo ao nome da parte adversa
    2. Buscar em contextos que mencionam "parte autora"/"reclamante" sem referência ao cliente
    """
    if not text:
        return None
    
    # ✅ PASSO 1: Identificar quem é a parte adversa
    nome_adversa, marcadores = _identificar_parte_adversa(text)
    if not marcadores:
        return None
    
    # Detectar se é recurso ou inicial
    is_recurso = bool(re.search(r'Recurso\s+Ordinário|RECORRENTE|RECORRIDO', text, re.I))
    
    # Todos os emails encontrados no documento
    all_emails = re.findall(r'([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})', text, re.I)
    
    for email in all_emails:
        email = email.lower()
        
        # ✅ FILTRO 1: Ignorar emails genéricos de escritórios
        if any(skip in email for skip in ['contato@', 'juridico@', 'adv@', 'escritorio@', 'advocacia@']):
            continue
        
        # Encontrar contexto onde email aparece (200 chars antes e depois)
        pos = text.lower().find(email.lower())
        if pos < 0:
            continue
            
        context = text[max(0, pos-200):min(len(text), pos+200)]
        context_upper = context.upper()
        
        # ✅ FILTRO 2: Verificar se está em contexto da parte adversa
        marcadores_adversa_encontrados = sum(1 for m in marcadores if m in context_upper)
        
        # Marcadores de contexto positivo (parte autor/reclamante)
        contexto_positivo = any(marker in context_upper for marker in [
            'PARTE AUTORA', 'RECLAMANTE', 'RECORRENTE', 'AUTOR',
        ] + marcadores)
        
        # Marcadores de contexto negativo (parte cliente/reclamado)
        contexto_negativo = any(marker in context_upper for marker in [
            'GRUPO CASAS BAHIA', 'CASAS BAHIA', 'BANQI', 'BANQ', 'CNOVA',
            'RECORRIDO', 'RECLAMADO', 'PATRONOS DO', 'ADVOGADOS DO'
        ])
        
        # ✅ FILTRO 3: Email só é válido se:
        # - Está em contexto positivo (autor/reclamante) OU
        # - Está próximo aos marcadores da parte adversa
        # E NÃO está em contexto negativo (cliente)
        if (contexto_positivo or marcadores_adversa_encontrados >= 1) and not contexto_negativo:
            return email
    
    return None


def extract_prazo(text: str) -> str | None:
    """
    Extrai prazo de intimação/notificação.
    Ex: "Prazo: 15 dias", "Prazo de recurso: 8 dias"
    """
    if not text:
        return None
    
    # Buscar padrões de prazo
    prazo_patterns = [
        r'prazo[:\s]+([0-9]+\s*(?:dias?|meses?))',
        r'prazo\s+de\s+recurso[:\s]+([0-9]+\s*(?:dias?|meses?))',
        r'intimad[oa].*prazo\s+de\s+([0-9]+\s*(?:dias?|meses?))',
    ]
    
    for pattern in prazo_patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1).strip()
    
    return None


def extract_cpf_cnpj_parte_adversa(text: str) -> str | None:
    """
    Extrai CPF/CNPJ da parte adversa com verificação rigorosa de contexto.
    Usa múltiplas estratégias para garantir precisão:
    1. Identifica janelas contextuais da parte adversa
    2. Busca CPF/CNPJ apenas nessas janelas
    3. Valida ausência de marcadores negativos (cliente)
    """
    if not text:
        return None
    
    # PASSO 1: Identificar quem é a parte adversa
    nome_adversa, marcadores = _identificar_parte_adversa(text)
    if not marcadores or not nome_adversa:
        return None
    
    # PASSO 2: Criar janelas contextuais válidas
    # Encontrar todas as ocorrências dos marcadores da parte adversa
    lines = text.split('\n')
    janelas_validas = []
    
    for i, line in enumerate(lines):
        # Se linha contém marcador forte da parte adversa
        if any(m in line.upper() for m in marcadores[:2]):  # Primeiros 2 marcadores são mais confiáveis
            # Criar janela de contexto (15 linhas antes e depois)
            inicio = max(0, i - 15)
            fim = min(len(lines), i + 15)
            janela = '\n'.join(lines[inicio:fim])
            janelas_validas.append(janela)
    
    # PASSO 3: Buscar CPF/CNPJ nas janelas válidas
    for janela in janelas_validas:
        janela_upper = janela.upper()
        
        # Verificar se é uma janela segura (sem marcadores de cliente)
        marcadores_negativos = ['RECORRIDO:', 'RECLAMADO:', 'GRUPO', 'INSTITUICAO', 'COMERCIO']
        if any(neg in janela_upper for neg in marcadores_negativos):
            continue  # Pular janela com referências ao cliente
        
        # Buscar CPF/CNPJ nesta janela específica
        cpfs = re.findall(_CPF_RE, janela)
        cnpjs = re.findall(_CNPJ_RE, janela)
        
        # Priorizar CPF (parte adversa geralmente é pessoa física)
        if cpfs:
            return _digits(cpfs[0])
        if cnpjs:
            return _digits(cnpjs[0])
    
    return None


def extract_numero_processo_antigo(text: str) -> str | None:
    """
    Extrai número de processo antigo (formato anterior ao CNJ).
    Ex: "123/2010", "456/2015"
    """
    if not text:
        return None
    
    # Padrões de processo antigo
    old_patterns = [
        r'processo\s+(?:anterior|antigo)[:\s]*([0-9]+/[0-9]{4})',
        r'n[úu]mero\s+antigo[:\s]*([0-9]+/[0-9]{4})',
    ]
    
    for pattern in old_patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1).strip()
    
    return None


# ============================================================================
# DADOS TRABALHISTAS - Extração de informações específicas de processos trabalhistas
# ============================================================================

def extract_data_admissao(text: str) -> str | None:
    """
    Extrai data de admissão do trabalhador.
    
    2025-12-01: OTIMIZAÇÃO COMPLETA - Plano Batman
    - Usa utilitários compartilhados (normalize_text, MESES_MAP, is_invalid_date_context)
    - Novos padrões para formatos menos comuns
    - Validação de data mais robusta
    
    PRIORIDADE DE FONTES:
    1. TRCT / CTPS (tabelas estruturadas)
    2. Petição inicial (narrativa)
    3. Mês por extenso
    4. Planilha de cálculos (fallback)
    """
    logger = _extract_logger
    
    if not text:
        return None
    
    # NORMALIZAÇÃO usando utilitário compartilhado
    text_norm = normalize_text(text)
    text_lower = text_norm.lower()
    
    def format_date(raw: str) -> str:
        """Formata data para DD/MM/AAAA"""
        return raw.strip().replace('.', '/').replace('-', '/')
    
    def validate_and_return(match_obj, data: str, source: str) -> str | None:
        """Valida contexto e retorna data se válida"""
        if is_invalid_date_context(match_obj, text_lower):
            return None
        if is_valid_brazilian_date(data):
            logger.debug(f"[DATA_ADMISSAO] ✅ {source}: {data}")
            return data
        return None
    
    # ===== PRIORIDADE 1: TRCT/CTPS (alta confiança) =====
    trct_patterns = [
        r'data\s+de\s+admiss[aã]o\s*[:\s]+(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        r'admiss[aã]o\s*[:\s]+(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        r'in[íi]cio\s+do\s+contrato\s*[:\s]+(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # 🆕 TRCT campo numérico: "02. Data de Admissão 01/01/2020"
        r'\d+\.\s*(?:data\s+de\s+)?admiss[aã]o\s*[:\s]*(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # 🆕 Tabela com pipe: "| Admissão | 01/01/2020 |"
        r'\|\s*admiss[aã]o\s*\|\s*(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
    ]
    
    for pattern in trct_patterns:
        match = re.search(pattern, text_lower, re.I)
        if match:
            data = format_date(match.group(1))
            result = validate_and_return(match, data, "TRCT/CTPS")
            if result:
                return result
    
    # ===== PRIORIDADE 2: Narrativa com data numérica =====
    narrativa_patterns = [
        # "foi admitido/contratado em DD/MM/AAAA"
        r'foi\s+(?:admitid[oa]|contratad[oa])\s+(?:pela\s+)?(?:reclamada\s+)?(?:em\s+)?(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # "admitido na reclamada em DD/MM/AAAA"
        r'(?:admitid[oa]|contratad[oa])\s+(?:na|pela)\s+(?:reclamada|empresa|r[ée])[,\s]+(?:em\s+)?(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # "contratado em DD/MM/AAAA"
        r'contratad[oa]\s+(?:em\s+)?(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # "iniciou suas atividades em DD/MM/AAAA"
        r'iniciou\s+suas?\s+atividades?.{0,50}?(?:em\s+)?(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # "reclamante foi contratado em DD/MM/AAAA"
        r'reclamante\s+foi\s+(?:contratad[oa]|admitid[oa]).{0,50}?em\s+(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # 🆕 "começou a trabalhar em DD/MM/AAAA"
        r'come[çc]ou\s+a\s+trabalhar\s+(?:em\s+)?(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # 🆕 "ingressou na empresa em DD/MM/AAAA"
        r'ingressou\s+(?:na|na\s+empresa)\s+(?:em\s+)?(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # 🆕 "desde DD/MM/AAAA" (próximo a vínculo/contrato)
        r'v[íi]nculo.{0,30}?desde\s+(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # 🆕 "a partir de DD/MM/AAAA"
        r'(?:admitid[oa]|contratad[oa]).{0,20}?a\s+partir\s+de\s+(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # 🆕 "admitida pela reclamada em DD/MM/AAAA" (ordem diferente)
        # Aceita espaços opcionais dentro da palavra (problema PyPDF2: "admitid a")
        r'admitid\s*[oa]\s+pela\s+[Rr]eclamada\s+em\s+(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        r'contratad\s*[oa]\s+pela\s+[Rr]eclamada\s+em\s+(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # 🆕 "admissão" em contexto CTPS digital: "06/03/2017 - admissão"
        r'(\d{2}[/.\-]\d{2}[/.\-]\d{4})\s*[-–]\s*admiss[ãa]o',
        # 🆕 "foi admitida pela reclamada em" com espaços
        r'foi\s+admitid\s*[oa]\s+pela\s+[Rr]eclamada\s+em\s+(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
    ]
    
    for pattern in narrativa_patterns:
        match = re.search(pattern, text_lower, re.I | re.S)
        if match:
            data = format_date(match.group(1))
            result = validate_and_return(match, data, "Narrativa")
            if result:
                return result
    
    # ===== PRIORIDADE 3: Mês por extenso (SOMENTE com dia explícito) =====
    # NOTA: Removidos padrões só mês/ano para evitar datas inválidas como "01/00/2024"
    meses_pattern = '|'.join(MESES_MAP.keys())
    ano_pattern = r'(\d{4}|\d{1,2}\s*\d{2,3})'  # Aceita "2024" ou "2 024" do PyPDF2
    
    extenso_patterns = [
        # "admitido em 01 de junho de 2024" - dia OBRIGATÓRIO
        r'(?:admitid[oa]|contratad[oa])\s+(?:em\s+)?(\d{1,2})[ºoa]?\s+de\s+(' + meses_pattern + r')\.?\s+de\s+' + ano_pattern,
        # "foi admitido em 01 de junho de 2024" - dia OBRIGATÓRIO
        r'foi\s+(?:admitid[oa]|contratad[oa])\s+(?:em\s+)?(\d{1,2})[ºoa]?\s+de\s+(' + meses_pattern + r')\.?\s+de\s+' + ano_pattern,
        # "começou a trabalhar em 15 de março de 2024" - dia OBRIGATÓRIO
        r'(?:iniciou|come[çc]ou)\s+(?:a\s+trabalhar|suas?\s+atividades?)\s+(?:em\s+)?(\d{1,2})[ºoa]?\s+de\s+(' + meses_pattern + r')\.?\s+de\s+' + ano_pattern,
        # 🆕 "admitida no dia 19 de julho de 2023" - com "no dia"
        r'(?:admitid[oa]|contratad[oa])\s+no\s+dia\s+(\d{1,2})[ºoa]?\s+de\s+(' + meses_pattern + r')\.?\s+(?:de\s+)?' + ano_pattern,
        # 🆕 "foi admitido no dia 19 de julho de 2023"
        r'foi\s+(?:admitid[oa]|contratad[oa])\s+no\s+dia\s+(\d{1,2})[ºoa]?\s+de\s+(' + meses_pattern + r')\.?\s+(?:de\s+)?' + ano_pattern,
    ]
    
    for pattern in extenso_patterns:
        match = re.search(pattern, text_lower, re.I)
        if match:
            groups = match.groups()
            # Só processa se tiver 3 grupos: dia, mês, ano
            if len(groups) >= 3:
                dia = groups[0].zfill(2)
                mes_nome = groups[1].lower().replace('.', '')
                ano = groups[2].replace(' ', '')
                
                if mes_nome in MESES_MAP:
                    data = f"{dia}/{MESES_MAP[mes_nome]}/{ano}"
                    if is_valid_brazilian_date(data):
                        logger.debug(f"[DATA_ADMISSAO] ✅ Extenso: {data}")
                        return data
    
    # ===== PRIORIDADE 4: Período (fallback) =====
    periodo_patterns = [
        r'per[íi]odo\s*[:\s]+(\d{2}[/.\-]\d{2}[/.\-]\d{4})\s+a\s+\d{2}[/.\-]\d{2}[/.\-]\d{4}',
        r'trabalhou.{0,30}?de\s+(\d{2}[/.\-]\d{2}[/.\-]\d{4})\s+(?:a|at[ée])',
        r'laborou.{0,30}?de\s+(\d{2}[/.\-]\d{2}[/.\-]\d{4})\s+(?:a|at[ée])',
        r'v[íi]nculo.{0,30}?(\d{2}[/.\-]\d{2}[/.\-]\d{4})\s+(?:a|at[ée])',
    ]
    
    for pattern in periodo_patterns:
        match = re.search(pattern, text_lower, re.I)
        if match:
            data = format_date(match.group(1))
            if is_valid_brazilian_date(data):
                logger.debug(f"[DATA_ADMISSAO] ✅ Período: {data}")
                return data
    
    logger.debug("[DATA_ADMISSAO] ❌ Nenhuma data encontrada")
    return None


def extract_data_demissao(text: str) -> str | None:
    """
    Extrai data de demissão/dispensa do trabalhador.
    
    2025-12-01: OTIMIZAÇÃO COMPLETA - Plano Batman
    - Usa utilitários compartilhados (normalize_text, MESES_MAP, is_invalid_date_context)
    - Novos padrões para formatos menos comuns
    - Validação de data mais robusta
    
    PRIORIDADE DE FONTES:
    1. TRCT / CTPS (tabelas estruturadas)
    2. Petição inicial (narrativa)
    3. Mês por extenso
    4. Período (fallback - pega 2ª data)
    """
    logger = _extract_logger
    
    if not text:
        return None
    
    # NORMALIZAÇÃO usando utilitário compartilhado
    text_norm = normalize_text(text)
    text_lower = text_norm.lower()
    
    def format_date(raw: str) -> str:
        """Formata data para DD/MM/AAAA"""
        return raw.strip().replace('.', '/').replace('-', '/')
    
    def validate_and_return(match_obj, data: str, source: str) -> str | None:
        """Valida contexto e retorna data se válida"""
        if is_invalid_date_context(match_obj, text_lower, [
            'assinado eletronicamente', 'documento assinado', 
            'data da autuação', 'data da distribuição', 'distribuído em',
            'audiência', 'audiencia', 'intimação', 'notificação',
            'publicação', 'certifico'
        ]):
            return None
        if is_valid_brazilian_date(data):
            logger.debug(f"[DATA_DEMISSAO] ✅ {source}: {data}")
            return data
        return None
    
    # ===== PRIORIDADE 1: TRCT/CTPS (alta confiança) =====
    trct_patterns = [
        r'data\s+de\s+(?:demiss[aã]o|dispensa|desligamento)\s*[:\s]+(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        r'(?:demiss[aã]o|dispensa|desligamento|sa[íi]da)\s*[:\s]+(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        r'data\s+de\s+sa[íi]da\s*[:\s]+(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # 🆕 TRCT campo numérico: "03. Data de Demissão 01/01/2020"
        r'\d+\.\s*(?:data\s+de\s+)?(?:demiss[aã]o|dispensa)\s*[:\s]*(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # 🆕 Tabela com pipe: "| Demissão | 01/01/2020 |"
        r'\|\s*(?:demiss[aã]o|dispensa)\s*\|\s*(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
    ]
    
    for pattern in trct_patterns:
        match = re.search(pattern, text_lower, re.I)
        if match:
            data = format_date(match.group(1))
            result = validate_and_return(match, data, "TRCT/CTPS")
            if result:
                return result
    
    # ===== PRIORIDADE 2: Narrativa com data numérica =====
    narrativa_patterns = [
        # "foi demitido/dispensado em DD/MM/AAAA"
        r'foi\s+(?:demitid[oa]|dispensad[oa]|desligad[oa])\s+(?:sem\s+justa\s+causa\s+)?(?:em\s+)?(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # "dispensado sem justo motivo em DD/MM/AAAA"
        r'(?:dispensad[oa]|demitid[oa])\s+(?:sem\s+(?:justo\s+)?(?:motivo|causa)\s+)?(?:em\s+)?(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # "tendo sido dispensado em DD/MM/AAAA"
        r'tendo\s+sido\s+(?:dispensad[oa]|demitid[oa]|desligad[oa])\s+(?:em\s+)?(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # "rescindido/desligado em DD/MM/AAAA"
        r'(?:rescindid[oa]|desligad[oa])\s+(?:em\s+)?(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # "pediu demissão em DD/MM/AAAA"
        r'pedi(?:u|do|r)\s+demiss[aã]o\s+(?:em\s+)?(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # 🆕 "deixou a empresa em DD/MM/AAAA"
        r'deixou\s+(?:a\s+)?(?:empresa|r[ée])\s+(?:em\s+)?(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # 🆕 "encerrou o contrato em DD/MM/AAAA"
        r'encerrou\s+(?:o\s+)?contrato\s+(?:em\s+)?(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # 🆕 "teve seu contrato encerrado em DD/MM/AAAA"
        r'teve\s+(?:seu\s+)?contrato\s+(?:encerrad[oa]|rescindid[oa])\s+(?:em\s+)?(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # 🆕 "até DD/MM/AAAA quando foi demitido"
        r'at[ée]\s+(\d{2}[/.\-]\d{2}[/.\-]\d{4})\s+(?:quando\s+)?(?:foi\s+)?(?:demitid[oa]|dispensad[oa])',
        # 🆕 "demissão no dia DD/MM/AAAA" - com "no dia"
        r'demiss[aã]o\s+no\s+dia\s+(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # 🆕 "dispensado no dia DD/MM/AAAA" - com "no dia"
        r'(?:dispensad[oa]|demitid[oa]|desligad[oa])\s+no\s+dia\s+(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        # 🆕 "foi dispensado no dia DD/MM/AAAA"
        r'foi\s+(?:dispensad[oa]|demitid[oa]|desligad[oa])\s+no\s+dia\s+(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
    ]
    
    for pattern in narrativa_patterns:
        match = re.search(pattern, text_lower, re.I | re.S)
        if match:
            data = format_date(match.group(1))
            result = validate_and_return(match, data, "Narrativa")
            if result:
                return result
    
    # ===== PRIORIDADE 3: Mês por extenso =====
    meses_pattern = '|'.join(MESES_MAP.keys())
    
    extenso_patterns = [
        # "dispensado/demitido em DD de MES de AAAA"
        r'(?:dispensad[oa]|demitid[oa]|desligad[oa])\s+(?:sem\s+justa\s+causa\s+)?(?:em\s+)?(\d{1,2})[ºoa]?\s+de\s+(' + meses_pattern + r')\.?\s+de\s+(\d{4})',
        # "foi demitida em DD de MES de AAAA"
        r'foi\s+(?:demitid[oa]|dispensad[oa])\s+(?:em\s+)?(\d{1,2})[ºoa]?\s+de\s+(' + meses_pattern + r')\.?\s+de\s+(\d{4})',
        # "rescisão em DD de MES de AAAA"
        r'(?:rescis[aã]o|dispensa|demiss[aã]o).{0,50}em\s+(\d{1,2})[ºoa]?\s+de\s+(' + meses_pattern + r')\.?\s+de\s+(\d{4})',
    ]
    
    for pattern in extenso_patterns:
        match = re.search(pattern, text_lower, re.I)
        if match:
            dia = match.group(1).zfill(2)
            mes_nome = match.group(2).lower().replace('.', '')
            ano = match.group(3)
            if mes_nome in MESES_MAP:
                data = f"{dia}/{MESES_MAP[mes_nome]}/{ano}"
                if is_valid_brazilian_date(data):
                    logger.debug(f"[DATA_DEMISSAO] ✅ Extenso: {data}")
                    return data
    
    # ===== PRIORIDADE 4: Período (fallback - pega 2ª data) =====
    periodo_patterns = [
        r'per[íi]odo\s*[:\s]+\d{2}[/.\-]\d{2}[/.\-]\d{4}\s+(?:a|at[ée])\s+(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        r'trabalhou.{0,30}?\d{2}[/.\-]\d{2}[/.\-]\d{4}\s+(?:a|at[ée])\s+(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        r'laborou.{0,30}?\d{2}[/.\-]\d{2}[/.\-]\d{4}\s+(?:a|at[ée])\s+(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
        r'v[íi]nculo.{0,30}?\d{2}[/.\-]\d{2}[/.\-]\d{4}\s+(?:a|at[ée])\s+(\d{2}[/.\-]\d{2}[/.\-]\d{4})',
    ]
    
    for pattern in periodo_patterns:
        match = re.search(pattern, text_lower, re.I)
        if match:
            data = format_date(match.group(1))
            if is_valid_brazilian_date(data):
                logger.debug(f"[DATA_DEMISSAO] ✅ Período: {data}")
                return data
    
    logger.debug("[DATA_DEMISSAO] ❌ Nenhuma data encontrada")
    return None


def extract_salario(text: str) -> str | None:
    """
    Extrai salário efetivo do trabalhador (não piso salarial).
    Prioriza: "perceb", "recebia", "ganhava" sobre "deveria receber", "piso"
    
    2025-12-01: OTIMIZAÇÃO COMPLETA - Plano Batman
    - Usa normalize_monetary() para corrigir espaços em valores
    - Novos padrões para TRCT, contracheques, tabelas
    - Validação de contexto mais robusta
    - Suporte a formatos brasileiros e internacionais
    """
    logger = _extract_logger
    
    if not text:
        return None
    
    # NORMALIZAÇÃO: usar utilitário compartilhado + normalização monetária
    text_norm = normalize_text(text)
    text_norm = normalize_monetary(text_norm)
    
    # Regex para capturar valores monetários brasileiros
    # Suporta: 3.093,10 / 3093,10 / 1.516,00 / 10000,00
    valor_pattern = r'([0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{1,2}|[0-9]+,[0-9]{1,2})'
    
    def format_valor(raw: str) -> str:
        """Formata valor extraído para padrão R$ X.XXX,XX"""
        valor = re.sub(r'\s+', '', raw.strip())
        # Garantir 2 casas decimais
        if ',' in valor and len(valor.split(',')[1]) == 1:
            valor = valor + '0'
        return f"R$ {valor}"
    
    # ===== PRIORIDADE 1: TRCT / Documentos Estruturados =====
    # Estes são os mais confiáveis pois vêm de documentos oficiais
    patterns_trct = [
        # TRCT: "Salário Base: R$ 1.516,00" ou "Remuneração: R$ 2.500,00"
        r'(?:sal[aá]rio\s+(?:base|contratual)|remunera[cç][aã]o)\s*[:\-]?\s*R\$\s*' + valor_pattern,
        # Tabela: "| Salário | R$ 1.500,00 |"
        r'\|\s*sal[aá]rio\s*\|\s*R\$\s*' + valor_pattern,
        # TRCT campo: "03. Remuneração R$ 1.234,56"
        r'\d+\.\s*(?:sal[aá]rio|remunera[cç][aã]o)\s*R\$\s*' + valor_pattern,
    ]
    
    for pattern in patterns_trct:
        match = re.search(pattern, text_norm, re.I)
        if match:
            logger.debug(f"[SALARIO] ✅ TRCT/Estruturado: {match.group(1)}")
            return format_valor(match.group(1))
    
    # ===== PRIORIDADE 2: Verbos de recebimento (alta confiança) =====
    patterns_efetivo = [
        # "percebia/recebia/ganhava R$ X"
        r'\b(?:percebia|percebeu|percebesse|recebia|recebeu|ganhava|ganhou|percebendo|recebendo)\b\s+(?:o\s+)?(?:valor\s+de\s+)?(?:apenas\s+)?(?:como\s+sal[aá]rio\s+)?(?:sal[aá]rio\s+(?:de\s+|mensal\s+)?)?R\$\s*' + valor_pattern,
        # "com salário de R$ X"
        r'com\s+(?:o\s+)?sal[aá]rio\s+(?:de\s+)?R\$\s*' + valor_pattern,
        # "contratado/admitido com salário de R$ X"
        r'(?:contratad[oa]|admitid[oa])\s+(?:com\s+)?(?:o\s+)?sal[aá]rio\s+(?:de\s+)?R\$\s*' + valor_pattern,
        # "salário vigente era de R$ X"
        r'sal[aá]rio\s+(?:vigente|atual)\s+[^\n]{0,30}?(?:era\s+de\s+|de\s+)?R\$\s*' + valor_pattern,
        # "remuneração fixa de R$ X"
        r'remunera[cç][aã]o\s+(?:fixa|mensal)\s+(?:de\s+|no\s+valor\s+de\s+)?R\$\s*' + valor_pattern,
    ]
    
    for pattern in patterns_efetivo:
        match = re.search(pattern, text_norm, re.I)
        if match:
            logger.debug(f"[SALARIO] ✅ Verbo recebimento: {match.group(1)}")
            return format_valor(match.group(1))
    
    # ===== PRIORIDADE 3: Padrões genéricos com validação de contexto =====
    patterns_genericos = [
        # Último salário
        r'[úu]ltim[oa]\s+(?:remunera[cç][aã]o|sal[aá]rio)\s+(?:mensal\s+)?(?:foi\s+de\s+|de\s+|era\s+)?R\$\s*' + valor_pattern,
        # Salário de R$ X
        r'sal[aá]rio\s+(?:de\s+|base\s+de\s+|mensal\s+(?:de\s+)?)?R\$\s*' + valor_pattern,
        # Remuneração de R$ X
        r'remunera[cç][aã]o\s+(?:de\s+|mensal\s+(?:de\s+)?)?R\$\s*' + valor_pattern,
        # Salário-base
        r'sal[aá]rio[\s\-]base\s+(?:de\s+)?R\$\s*' + valor_pattern,
        # Vencimentos
        r'vencimentos?\s+(?:de\s+)?R\$\s*' + valor_pattern,
        # Holerite/contracheque
        r'(?:holerite|contracheque).{0,50}R\$\s*' + valor_pattern,
        # Remuneração total
        r'remunera[cç][aã]o\s+total\s+(?:de\s+)?R\$\s*' + valor_pattern,
        # "R$ X por mês" / "R$ X mensais"
        r'R\$\s*' + valor_pattern + r'\s+(?:por\s+m[êe]s|mensais?)',
        # 🆕 "no importe de R$ X" / "no valor de R$ X" (próximo a salário)
        r'sal[aá]rio.{0,30}?(?:no\s+)?(?:importe|valor)\s+de\s+R\$\s*' + valor_pattern,
        # 🆕 "média salarial de R$ X"
        r'm[ée]dia\s+salarial\s+(?:de\s+)?R\$\s*' + valor_pattern,
        # 🆕 "proventos de R$ X"
        r'proventos?\s+(?:de\s+)?R\$\s*' + valor_pattern,
    ]
    
    # Contextos inválidos (não é salário real)
    contextos_invalidos = [
        'deveria', 'piso', 'devem receber', 'deveriam', 'base de cálculo',
        'valor da causa', 'multa', 'indenização', 'honorários', 'custas',
        'mínimo nacional', 'salário mínimo'
    ]
    
    for pattern in patterns_genericos:
        match = re.search(pattern, text_norm, re.I)
        if match:
            # Validar contexto: não deve estar perto de palavras que indicam valor teórico
            start_ctx = max(0, match.start() - 60)
            end_ctx = min(len(text_norm), match.end() + 60)
            contexto = text_norm[start_ctx:end_ctx].lower()
            
            if any(kw in contexto for kw in contextos_invalidos):
                continue
            
            logger.debug(f"[SALARIO] ✅ Genérico: {match.group(1)}")
            return format_valor(match.group(1))
    
    # ===== PRIORIDADE 4: Fallback - primeiro valor monetário significativo =====
    # Buscar em contexto de emprego (primeiros 5000 chars)
    texto_emprego = text_norm[:5000]
    
    # Padrão genérico: R$ seguido de valor > 500 (filtrar valores muito baixos)
    all_valores = re.findall(r'R\$\s*' + valor_pattern, texto_emprego, re.I)
    for valor_match in all_valores:
        try:
            valor_num = float(valor_match.replace('.', '').replace(',', '.'))
            if 500 <= valor_num <= 100000:  # Faixa salarial razoável
                logger.debug(f"[SALARIO] ✅ Fallback: {valor_match}")
                return format_valor(valor_match)
        except ValueError:
            continue
    
    logger.debug("[SALARIO] ❌ Nenhum valor encontrado")
    return None


def extract_cargo_funcao(text: str) -> str | None:
    """
    Extrai cargo/função do trabalhador.
    
    2025-12-01: OTIMIZAÇÃO COMPLETA - Plano Batman
    - Usa utilitários compartilhados
    - Validação melhorada de cargos truncados
    - Rejeição de frases inválidas
    
    FONTES DE EXTRAÇÃO:
    1. TRCT / CTPS (alta confiança)
    2. Narrativa da petição inicial
    3. Tabela de dados
    """
    logger = _extract_logger
    
    if not text:
        return None
    
    # Normalizar usando utilitário compartilhado
    text_norm = normalize_text(text)
    
    # Lista para armazenar todas as funções encontradas (pegar última)
    funcoes_encontradas = []
    
    # Palavras proibidas (falsos positivos comuns)
    palavras_proibidas = [
        'direito', 'pessoa', 'acordo', 'contratada', 'reclamada', 'reclamante',
        'advogado', 'juiz', 'processo', 'trabalho', 'empresa', 'autor', 'reu',
        'janeiro', 'fevereiro', 'março', 'marco', 'abril', 'maio', 'junho',
        'julho', 'agosto', 'setembro', 'outubro', 'novembro', 'dezembro'
    ]
    
    # Abordagem GREEDY: capturar o máximo possível e depois limpar no pós-processamento
    # Isso evita problemas com cargos longos como "Técnico em Segurança do Trabalho"
    
    patterns = [
        # PRIORIDADE 1: Padrões específicos de contratação (case sensitive para cargos em maiúsculo)
        # Captura até 60 caracteres greedy, depois limpa
        r'para\s+exercer\s+(?:a\s+)?fun[cç][aã]o\s+de\s+([A-ZÀ-Ú][A-ZÀ-Ú\s]{3,60})',
        r'o\s+cargo\s+de\s+([A-ZÀ-Ú][A-ZÀ-Ú\s]{3,60})',
        r'exercendo\s+(?:a\s+)?fun[cç][aã]o\s+de\s+([A-ZÀ-Ú][A-ZÀ-Ú\s]{3,60})',
        r'exercia\s+(?:a\s+)?fun[cç][aã]o\s+de\s+([A-ZÀ-Ú][A-ZÀ-Ú\s]{3,60})',
        
        # PRIORIDADE 2: Padrões de narrativa (case insensitive) - GREEDY
        r'fun[cç][aã]o\s+de\s+([A-Za-zÀ-ú][A-Za-zÀ-ú\s]{3,60})',
        r'cargo\s+de\s+([A-Za-zÀ-ú][A-Za-zÀ-ú\s]{3,60})',
        r'contratad[oa]\s+como\s+([A-Za-zÀ-ú][A-Za-zÀ-ú\s]{3,60})',
        
        # PRIORIDADE 3: Tabelas TRCT/CTPS
        r'fun[cç][aã]o\s*:\s*([A-Za-zÀ-ú][A-Za-zÀ-ú\s]{3,60})',
        r'cargo\s*:\s*([A-Za-zÀ-ú][A-Za-zÀ-ú\s]{3,60})',
    ]
    
    def limpar_cargo(funcao: str) -> str | None:
        """Limpa e valida o cargo extraído
        
        Abordagem: Captura greedy seguida de limpeza de palavras de contexto
        que não fazem parte do cargo (verbos, preposições de continuação, etc)
        
        2025-11-27: Adicionada rejeição de textos inválidos como:
        - "CONDIÇÕES ESPECIAIS EM QUE O TRABALHO É REALIZADO"
        - Frases longas com verbos conjugados
        """
        if not funcao:
            return None
        
        funcao = funcao.strip()
        funcao = re.sub(r'\s+', ' ', funcao)
        
        # 🆕 REJEITAR FRASES INVÁLIDAS ANTES DE QUALQUER PROCESSAMENTO
        # Estes padrões indicam que capturamos texto descritivo, não um cargo
        frases_invalidas = [
            r'condi[çc][oõ]es?\s+especiais?',  # "CONDIÇÕES ESPECIAIS"
            r'em\s+que\s+o\s+trabalho',  # "EM QUE O TRABALHO É REALIZADO"
            r'realizado\s+e\s+com',  # "REALIZADO E COM ELES"
            r'trabalho\s+[eé]\s+realizado',  # "TRABALHO É REALIZADO"
            r'desempenha(?:va|ndo)',  # "desempenhava", "desempenhando"
            r'(?:foi|era|estava)\s+(?:contratad[oa]|admitid[oa])',  # "foi contratada"
            r'exerc(?:ia|eu|endo)\s+as?\s+atividade',  # "exercia as atividades"
            r'atividades?\s+(?:de|do|da)',  # "atividades de..."
            r'exerc[ií]cio\s+(?:de|da|do)',  # "exercício de..."
            r'fun[cç][oõ]es?\s+de\s+(?:nature|car[aá]ter)',  # "funções de natureza"
            # 🆕 2025-11-27: Rejeitar padrões CIPA (Comissão Interna de Prevenção de Acidentes)
            r'comiss[oõ]es?\s+internas?',  # "COMISSÕES INTERNAS"
            r'preven[çc][aã]o\s+de\s+acidentes?',  # "PREVENÇÃO DE ACIDENTES"
            r'dire[çc][aã]o\s+de\s+comiss',  # "DIREÇÃO DE COMISSÕES"
            r'\bcipa\b',  # "CIPA"
            r'desde\s+o\s+registro',  # "desde o registro de sua candidatura"
            r'prote[çc][aã]o\s+social',  # "proteção social da CIPA"
        ]
        
        funcao_lower = funcao.lower()
        for pattern in frases_invalidas:
            if re.search(pattern, funcao_lower, re.I):
                return None
        
        # Palavras que indicam FIM DO CARGO e início de outra parte da frase
        # Essas palavras e tudo depois delas devem ser removidas
        corte_palavras = [
            r'\s+sendo\b.*$',
            r'\s+foi\b.*$',
            r'\s+e\s+foi\b.*$',
            r'\s+percebendo\b.*$',
            r'\s+recebendo\b.*$',
            r'\s+ganhando\b.*$',
            r'\s+sob\b.*$',
            r'\s+onde\b.*$',
            r'\s+quando\b.*$',
            r'\s+contratad[oa]\b.*$',
            r'\s+para\s+(?!de\b).*$',  # "para" mas não "para de"
            r'\s+com\s+(?:sal[aá]rio|remunera|vencimento).*$',  # "com salário"
            r'\s+na\s+(?:empresa|reclamada|ré|reclam).*$',  # "na empresa"
            r'\s+no\s+(?:setor|departamento|estabelecimento).*$',  # "no setor"
            r'\s+da\s+(?:empresa|reclamada|ré|reclam|companhia|sociedade).*$',  # "da empresa"
            r'\s+do\s+(?:setor|departamento|estabelecimento).*$',  # "do setor"
        ]
        
        for pattern in corte_palavras:
            funcao = re.sub(pattern, '', funcao, flags=re.I)
        
        funcao = funcao.strip()
        
        # Limpar trailing de artigos/preposições/conjunções soltas
        # Loop para remover múltiplos trailing (ex: "Diretor da" → "Diretor")
        for _ in range(3):  # Máximo 3 iterações
            old_len = len(funcao)
            # Só remove se for palavra solta no final (não seguida de substantivo)
            funcao = re.sub(r'\s+(e|ou|com|para|em|no|na|da|do|de|das|dos|a|o|os|as|até|ate)$', '', funcao, flags=re.I)
            if len(funcao) == old_len:
                break
        
        funcao = funcao.strip()
        
        # Rejeitar se termina em preposição (cargo incompleto)
        if re.search(r'\s+(de|da|do|das|dos)$', funcao, re.I):
            return None
        
        # 🆕 Rejeitar se tem mais de 6 palavras (provavelmente é frase, não cargo)
        palavras = funcao.split()
        if len(palavras) > 6:
            return None
        
        # Validar tamanho mínimo
        if len(funcao) <= 3:
            return None
        
        return funcao
    
    for pattern in patterns:
        for match in re.finditer(pattern, text_norm, re.I):
            funcao = match.group(1).strip()
            
            # Limpar e validar
            funcao = limpar_cargo(funcao)
            
            if not funcao:
                continue
            
            # Validar:
            # 1. Não é palavra proibida
            # 2. Não começa com artigo/preposição
            if (funcao.lower() not in palavras_proibidas and
                not re.match(r'^(o|a|os|as|de|do|da|em|no|na|para)\s', funcao, re.I)):
                
                funcoes_encontradas.append(funcao.upper())
    
    # Retornar a última função encontrada (mais recente no texto)
    if funcoes_encontradas:
        # Deduplicate mantendo ordem
        seen = set()
        unique = []
        for f in funcoes_encontradas:
            if f not in seen:
                seen.add(f)
                unique.append(f)
        
        logger.debug(f"[CARGO] Funções encontradas: {unique}")
        return unique[-1]  # Retorna a última (mais recente)
    
    return None


def extract_pis(text: str) -> str | None:
    """
    Extrai número do PIS.
    
    Padrões suportados (2025-11-28 atualizado):
    - "PIS 123.45678.90-1"
    - "cadastrado no PIS sob o nº. 164.295.786-75"
    - "PIS-PASEP: 123.45678.90-1"
    - "NIT: 12345678901"
    - "NIS 123.45678.90-1"
    - "inscrito no PIS/PASEP sob o número 123..."
    
    2025-11-28: Melhorada busca com janela de contexto ±2 linhas
    para capturar números que aparecem em linhas vizinhas da âncora.
    """
    import logging
    logger = logging.getLogger(__name__)
    
    logger.info(f"[PIS] ⚡ FUNÇÃO CHAMADA com texto de {len(text) if text else 0} chars")
    
    if not text:
        logger.info("[PIS] ❌ Texto vazio, retornando None")
        return None
    
    # NORMALIZAR TEXTO COM REGEX: remover TODAS as variantes de "nº" (com/sem espaços, quebras de linha)
    # Captura: n°, nº, N°, Nº com pontos opcionais e WHITESPACE entre "n" e "°/º"
    text_norm = re.sub(r'n\s*[º°]\.?', '', text, flags=re.I)
    # Normalizar hífens especiais
    text_norm = text_norm.replace('–', '-').replace('—', '-')
    # 2025-12-01: CRÍTICO - Normalizar espaços múltiplos (após remoção de "nº" ficam espaços duplos)
    text_norm = re.sub(r'\s+', ' ', text_norm)
    
    logger.info(f"[PIS] Buscando em texto normalizado de {len(text_norm)} chars")
    
    # Padrões PIS: 11 dígitos com separadores opcionais (pontos, hífens, espaços múltiplos)
    # PyPDF2 pode adicionar espaços extras: "cadastrad o" e "786 -75"
    # Aceita variantes: 204.05911.17.8, 124.13653.63-7, 164.29578.67-5
    # 2025-11-27: Adicionados PIS-PASEP, NIT, NIS + formatos com pontos em posições variadas
    
    # Regex UNIVERSAL para 11 dígitos com qualquer combinação de pontos/hífens/espaços
    # Ex: 204.05911.17.8, 204.05911.17-8, 124.13653.63-7, 161.94839.72-5
    UNIVERSAL_PIS = r'(\d{2,3}[\.\s\-]*\d{3,5}[\.\s\-]*\d{2,5}[\.\s\-]*\d{1,2})'
    
    patterns = [
        # 🆕 PIS-PASEP com hífen
        r'(?:pis[-\s]*pasep|pis/pasep)\s*[:\-]?\s*' + UNIVERSAL_PIS,
        # 🆕 NIT (Número de Identificação do Trabalhador)
        r'(?:^|[\s:])nit\s*[:\-]?\s*' + UNIVERSAL_PIS,
        r'(?:^|[\s:])nit\s*[:\-]?\s*(\d{11})\b',
        # 🆕 NIS (Número de Identificação Social)
        r'(?:^|[\s:])nis\s*[:\-]?\s*' + UNIVERSAL_PIS,
        r'(?:^|[\s:])nis\s*[:\-]?\s*(\d{11})\b',
        # 🆕 "inscrito no PIS sob o número..."
        r'inscrit[oa]\s+no\s+pis\s*(?:/pasep)?\s*(?:sob\s+o?\s*)?(?:n[úu]mero\s+)?' + UNIVERSAL_PIS,
        # Genérico: qualquer combinação de 11 dígitos com separadores (mais flexível)
        r'(?:^|[\s:,])pis\s*[:\-]?\s*' + UNIVERSAL_PIS,
        # "cadastrado no pis sob o 164.295.786-75" (aceita espaços extras)
        r'cadastrad\s*o\s+no\s+pis\s*(?:sob\s+o?\s*)?\s*' + UNIVERSAL_PIS,
        # "pis 12345678901" (sem separadores)
        r'(?:^|[\s:])pis\s*[:\-]?\s*(\d{11})\b',
        # 🆕 "portador do PIS 123..."
        r'portador[a]?\s+(?:do|da)\s+pis\s*(?:/pasep)?\s*' + UNIVERSAL_PIS,
    ]
    
    for i, pattern in enumerate(patterns):
        match = re.search(pattern, text_norm, re.I)
        if match:
            pis = match.group(1).strip()
            # Normalizar: remover pontos, hífens e espaços
            pis_digits = re.sub(r'[\.\-\s]', '', pis)
            # VALIDAÇÃO: exatamente 11 dígitos (evita capturar números maiores)
            if len(pis_digits) == 11 and pis_digits.isdigit():
                logger.info(f"[PIS] ✅ Match pattern {i}: {pis_digits} → formatado")
                return f"{pis_digits[:3]}.{pis_digits[3:8]}.{pis_digits[8:10]}-{pis_digits[10]}"
            else:
                logger.debug(f"[PIS] ❌ Rejeitado (não tem 11 dígitos): {pis_digits}")
    
    # 🆕 2025-11-28: BUSCA COM JANELA DE CONTEXTO ±2 LINHAS
    # Procura âncora "PIS" e depois busca número de 11 dígitos nas linhas vizinhas
    lines = text_norm.split('\n')
    for i, line in enumerate(lines):
        if re.search(r'\bpis\b', line, re.I):
            # Janela de contexto: linha atual + 2 linhas seguintes
            context_lines = lines[i:i+3]
            context = ' '.join(context_lines)
            
            # Buscar número de 11 dígitos no contexto expandido
            nums = re.findall(r'\d{2,3}[\.\s\-]*\d{3,5}[\.\s\-]*\d{2,5}[\.\s\-]*\d{1,2}', context)
            for num in nums:
                digits = re.sub(r'[\.\-\s]', '', num)
                if len(digits) == 11 and digits.isdigit():
                    logger.info(f"[PIS] ✅ Match via janela de contexto: {digits}")
                    return f"{digits[:3]}.{digits[3:8]}.{digits[8:10]}-{digits[10]}"
    
    logger.info("[PIS] ❌ Nenhum match encontrado")
    return None


def extract_ctps(text: str) -> str | None:
    """
    Extrai número da CTPS (Carteira de Trabalho).
    Formatos: "CTPS nº 95524 série 149/RJ", "CTPS sob nº 0048610-00080/RJ"
    
    2025-11-27: Novos formatos adicionados:
    - "CTPS DIGITAL" (sem número) - apenas como fallback final
    - "CTPS nº 1210996, série 2780/RJ" (série com espaço)
    - Espaços extras do OCR: "CTPS   nº  95524  série  149/RJ"
    - Formato com hífen na série: "936665 série 00014-PB"
    
    2025-11-28: Correção crítica - "DIGITAL" só retorna se não houver número real
    """
    if not text:
        return None
    
    # Normalizar: remover "nº" e normalizar hífens
    text_norm = re.sub(r'n\s*[º°]\.?', '', text, flags=re.I)
    text_norm = text_norm.replace('–', '-').replace('—', '-')
    # Normalizar espaços múltiplos
    text_norm = re.sub(r'\s+', ' ', text_norm)
    
    # PADRÕES COM NÚMERO (prioritários - tentar todos primeiro)
    patterns_with_number = [
        # Formato COMPACTO: "CTPS sob nº 0048610 -00080/RJ" (PyPDF2 adiciona espaços)
        r'(?:portador\s+da\s+)?CTPS\s+(?:sob\s+)?(\d+[\s\-]+\d+[/][A-Z]{2})',
        # 🆕 Formato com série: "CTPS nº 1210996, série 2780/RJ" ou "CTPS 1210996, série 2780/MA"
        r'CTPS\s*(\d+)\s*,?\s*s[ée]rie\s+(\d+)\s*/?\s*([A-Z]{2})',
        # Formato SEPARADO com vírgula: "CTPS nº 1210996, série 149/RJ"
        r'CTPS\s*(\d+)\s*,?\s*s[ée]rie\s+(\d+[-/][A-Z]{2})',
        # 🆕 Formato série com hífen: "936665 série 00014-PB"
        r'CTPS\s*(\d+)\s*,?\s*s[ée]rie\s+(\d+[-]\s*[A-Z]{2})',
        # Formato apenas série: "série 149/RJ" ou "serie 00014-PB"
        r'CTPS\s*(\d+)\s*,?\s*s[ée]rie\s+([\dA-Z\-/]+)',
        # Formato COMPACTO genérico: "CTPS 98765-00123/SP"
        r'CTPS\s*(\d+[-/]\d+[-/][A-Z]{2})',
        # Apenas número com contexto: "portador da CTPS 123456" ou "CTPS 123456"
        # ⚠️ IMPORTANTE: Não capturar "CTPS DIGITAL" como número
        r'(?:portador\s+da\s+)?CTPS\s+(?!DIGITAL)(\d{5,})',
        # 🆕 Formato sem prefixo CTPS: "número série" perto de contexto Carteira
        r'(?:Carteira\s+de\s+Trabalho|CTPS)[^\d]*(\d{5,8})\s*(?:s[ée]rie\s+)?(\d{3,6}[-/]?[A-Z]{0,2})',
        # 🆕 Formato com parêntese colado: "CTPS)1173470" ou "CTPS )1173470"
        r'CTPS\s*\)\s*(\d{5,})',
        # 🆕 Formato "CTPSSCarteira": texto corrompido OCR "CTPSS" ou "CTPS S"
        r'CTPSS?\s*(?:Carteira[^\d]+)?(\d{5,})',
    ]
    
    for pattern in patterns_with_number:
        match = re.search(pattern, text_norm, re.I)
        if match:
            if match.lastindex == 3:
                # Formato com série e UF separados: (numero, serie, UF)
                numero = match.group(1).strip()
                serie = match.group(2).strip()
                uf = match.group(3).strip().upper()
                return f"{numero} série {serie}/{uf}"
            elif match.lastindex == 2:
                # Formato com série separada - validar UF OU ser numérica pura
                serie = match.group(2).strip()
                
                # VALIDAÇÃO: série deve ter /UF (ex: 149/RJ) OU ser apenas numérica (ex: 00014-PB)
                tem_uf = re.search(r'[/-][A-Z]{2}$', serie)
                eh_numerica = re.match(r'^[\d\-/A-Z]+$', serie) and any(c.isdigit() for c in serie)
                
                if tem_uf or (eh_numerica and len(serie) <= 15):
                    return f"{match.group(1)} série {serie}"
                # Rejeitar séries que não têm UF nem são numéricas (ex: "DO RÉU")
            else:
                # Limpar espaços extras
                ctps = match.group(1).strip()
                ctps = re.sub(r'\s+', '', ctps)  # Remove espaços internos
                
                # VALIDAÇÃO: se tem barra, deve terminar com /UF (2 letras maiúsculas)
                if '/' in ctps:
                    if not re.search(r'/[A-Z]{2}$', ctps):
                        continue  # Rejeitar se não termina com /UF
                
                return ctps
    
    # 🆕 FALLBACK: CTPS DIGITAL (quando não há número físico)
    # 2025-11-28: Retornar "CTPS DIGITAL" como valor válido para eLaw
    # 2025-12-01: Extrair CPF associado à CTPS Digital como identificador
    if re.search(r'[Cc]arteira\s+de\s+[Tt]rabalho\s+[Dd]igital', text, re.I):
        # Tentar extrair CPF próximo à CTPS Digital
        cpf_patterns = [
            r'CPF\s*[:\-]?\s*(\d{3}[.\s]?\d{3}[.\s]?\d{3}[.\-]?\d{2})',
            r'Dados\s+Pessoais[^\d]*(\d{3}\.\d{3}\.\d{3}[-.\s]\d{2})',
        ]
        for cpf_pattern in cpf_patterns:
            cpf_match = re.search(cpf_pattern, text, re.I | re.S)
            if cpf_match:
                cpf_raw = cpf_match.group(1).replace(' ', '').replace('.', '').replace('-', '')
                if len(cpf_raw) == 11 and cpf_raw.isdigit():
                    # Formatar CPF: 123.456.789-01
                    cpf_fmt = f'{cpf_raw[:3]}.{cpf_raw[3:6]}.{cpf_raw[6:9]}-{cpf_raw[9:]}'
                    return f"Digital ({cpf_fmt})"
        
        # Fallback sem CPF
        return "CTPS DIGITAL"
    
    return None


def extract_local_trabalho(text: str) -> str | None:
    """
    Extrai local de trabalho (endereço completo).
    Padrões: "local de trabalho:", "Último local de trabalho:", "prestou serviços em"
    """
    if not text:
        return None
    
    patterns = [
        # Padrões explícitos de local (mais específicos primeiro)
        r'[úu]ltimo\s+local\s+de\s+trabalho[:\s]+([^\n\.]+)',
        r'local\s+(?:de\s+)?trabalho[:\s]+([^\n\.]+)',
        # Endereço específico com indicadores geográficos
        r'(?:laborou|trabalhou|prestou\s+servi[cç]os)\s+(?:em|na|no)\s+([^,\.]+,\s*[^,\.]+(?:,\s*[^,\.]+)?)',
    ]
    
    # Buscar em TODO o texto (não limitar a 3000 chars)
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            local = match.group(1).strip()
            # Limpar quebras de linha excessivas e espaços
            local = re.sub(r'\s+', ' ', local)
            # Parar em pontos que indicam fim do endereço
            local = re.split(r'(?:\s+CEP|\s+para\s+|\s+conforme)', local)[0].strip()
            
            # FILTRO MÍNIMO: apenas bloquear falsos positivos conhecidos
            local_lower = local.lower()
            blacklist = [
                'do reclamante, se o objeto', 'versar sobre pedido', 
                'condições ambientais de trabalho', 'informa o autor que em toda',
                'era realizada dedetização', 'coincidia com a sua jornada',
                'para a segunda reclamada', 'para a primeira reclamada',
                'desde sua admissão'
            ]
            
            # Se contém falso positivo conhecido, pular
            if any(lixo in local_lower for lixo in blacklist):
                continue
            
            # Validação: deve ter pelo menos um indicador de endereço real
            indicadores = ['rua', 'avenida', 'av.', 'largo', 'praça', 'rodovia', 
                          'estrada', 'bairro', ' - ', 'nº', 'n°', 'número']
            tem_indicador = any(ind in local_lower for ind in indicadores)
            
            # Aceitar se tiver tamanho razoável (>10 chars) E indicador de endereço
            if local and len(local) > 10 and tem_indicador:
                return local
    
    return None


def extract_empregador(text: str) -> str | None:
    """
    Extrai nome da empresa empregadora.
    Se não encontrar menção explícita, retorna None (fallback será feito em extract_metadata).
    
    2025-11-27: Melhorada validação para evitar falsos positivos como:
    - "RECLAMADA" literal (sem nome da empresa)
    - Frases como "sempre controlou a jornada de seus funcionarios"
    """
    if not text:
        return None
    
    # Normalizar texto para busca
    text_norm = re.sub(r'\s+', ' ', text).strip()
    
    # PRIORIDADE 1: Padrões de RECLAMADO na capa do processo (mais confiável)
    # Ex: "RECLAMADO: CBSI - COMPANHIA BRASILEIRA DE SERVICOS DE INFRAESTRUTURA"
    capa_patterns = [
        r'RECLAMAD[OA]:\s*([A-ZÀ-Ú][A-ZÀ-Úa-zà-ú\s\.\-&,]{10,150}?)(?:\n|RECLAMAD|ADVOGAD|PAGINA)',
        r'RÉU:\s*([A-ZÀ-Ú][A-ZÀ-Úa-zà-ú\s\.\-&,]{10,150}?)(?:\n|ADVOGAD|PAGINA)',
    ]
    
    for pattern in capa_patterns:
        match = re.search(pattern, text_norm)
        if match:
            empregador = match.group(1).strip()
            empregador = re.sub(r'\s+', ' ', empregador).strip(' ,-')
            if _validar_empregador(empregador):
                return empregador
    
    # PRIORIDADE 2: Padrões de narrativa (admitido pela EMPRESA)
    narrativa_patterns = [
        # "admitido pela primeira reclamada NOME" ou "admitido pela NOME"
        r'admitid[oa]\s+pel[oa]\s+(?:primeira\s+)?(?:reclamada\s+)?([A-ZÀ-Ú][A-ZÀ-Úa-zà-ú\s\.\-&]{5,100}?)(?:\s*,|\s+em\s+\d{2}/|\s+CNPJ)',
        # "contratado pela EMPRESA"
        r'contratad[oa]\s+pel[oa]\s+(?:empresa\s+)?([A-ZÀ-Ú][A-ZÀ-Úa-zà-ú\s\.\-&]{5,100}?)(?:\s*,|\s+em\s+\d{2}/|\s+para)',
        # "em face de EMPRESA NOME COMPLETO, pessoa jurídica"
        r'em\s+face\s+de\s+([A-ZÀ-Ú][A-ZÀ-Úa-zà-ú\s\.\-&]{5,100}?),?\s+pessoa\s+jur[íi]dica',
    ]
    
    for pattern in narrativa_patterns:
        match = re.search(pattern, text_norm, re.I)
        if match:
            empregador = match.group(1).strip()
            empregador = re.sub(r'\s+', ' ', empregador).strip(' ,-')
            if _validar_empregador(empregador):
                return empregador
    
    return None


def _validar_empregador(empregador: str) -> bool:
    """
    Valida se o texto extraído é um nome de empresa válido.
    Evita falsos positivos como "RECLAMADA", frases descritivas, etc.
    """
    if not empregador or len(empregador) < 5:
        return False
    
    lixo_lower = empregador.lower()
    
    # Blacklist de palavras que indicam falso positivo
    blacklist_exato = [
        'reclamada', 'reclamado', 'reclamante', 'autor', 'autora', 'reu', 'ré',
        'primeira reclamada', 'segunda reclamada', 'terceira reclamada'
    ]
    
    # Se é exatamente uma dessas palavras, rejeitar
    if lixo_lower in blacklist_exato:
        return False
    
    # Blacklist de frases que NÃO são nomes de empresa
    blacklist_frases = [
        'se enquadra', 'enquadra em', 'área de risco', 'area de risco',
        'na presente lide', 'presente lide', 'benefício da',
        'documento assinado', 'condenada a pagar', 'for condenada',
        'presente demanda', 'presente ação', 'da inicial',
        'na inicial', 'dos autos', 'aos autos',
        # 2025-11-27: Novos falsos positivos identificados
        'sempre controlou', 'controlou a jornada', 'de seus funcionarios',
        'seus funcionários', 'seus empregados', 'foi demitida', 'foi dispensada',
        'em benefício', 'deverá responder', 'deve responder'
    ]
    
    if any(lixo in lixo_lower for lixo in blacklist_frases):
        return False
    
    # Verbos conjugados indicam que é uma frase, não um nome
    verbos_proibidos = [
        'for', 'seja', 'fica', 'tenha', 'deve', 'possa', 'foi', 'era', 'está',
        'sempre', 'controlou', 'pagou', 'demitiu', 'dispensou', 'contratou',
        'realizou', 'efetuou', 'cumpriu', 'prestou', 'laborou', 'trabalhou'
    ]
    palavras = lixo_lower.split()
    if any(verbo in palavras for verbo in verbos_proibidos):
        return False
    
    # Nome de empresa deve ter pelo menos uma palavra capitalizada ou sigla
    # e não deve começar com artigo/preposição
    palavras_iniciais_invalidas = ['a', 'o', 'as', 'os', 'de', 'da', 'do', 'em', 'no', 'na', 'que', 'e']
    if palavras and palavras[0] in palavras_iniciais_invalidas:
        return False
    
    return True


def extract_motivo_demissao(text: str) -> str | None:
    """
    Extrai motivo da demissão.
    
    2025-11-27: Atualizado com padrões expandidos:
    - "sem justa causa", "pedido de demissão", "rescisão indireta"
    - "dispensado injustamente", "demissão imotivada"
    - "término de contrato de experiência"
    - "acordo mútuo", "distrato"
    """
    if not text:
        return None
    
    # Lista de motivos comuns (ordem importa: mais específico primeiro)
    motivos = [
        # Rescisão indireta (alta prioridade)
        (r'rescis[aã]o\s+indireta', 'Rescisão Indireta'),
        (r'pede\s+(?:a\s+)?rescis[aã]o\s+indireta', 'Rescisão Indireta'),
        (r'requerer\s+(?:a\s+)?rescis[aã]o\s+indireta', 'Rescisão Indireta'),
        
        # Rescisão contratual genérica
        (r'rescis[aã]o\s+contratual', 'Rescisão Contratual'),
        
        # Dispensa sem justa causa (padrões específicos)
        (r'dispens(?:a|ad[oa])\s+sem\s+justa\s+causa', 'Dispensa Sem Justa Causa'),
        (r'demitid[oa]\s+sem\s+justa\s+causa', 'Dispensa Sem Justa Causa'),
        (r'desligad[oa]\s+sem\s+justa\s+causa', 'Dispensa Sem Justa Causa'),
        # 🆕 "dispensado injustamente", "demissão imotivada"
        (r'dispens(?:a|ad[oa])\s+injustamente', 'Dispensa Sem Justa Causa'),
        (r'demiss[aã]o\s+imotivada', 'Dispensa Sem Justa Causa'),
        (r'demitid[oa]\s+imotivadamente', 'Dispensa Sem Justa Causa'),
        # 🆕 "foi desligado pela empregadora" (sem especificar causa = sem justa causa)
        (r'desligad[oa]\s+(?:pela?\s+)?(?:empregador|empresa|reclamada)', 'Dispensa Sem Justa Causa'),
        
        # Dispensa com justa causa
        (r'dispens(?:a|ad[oa])\s+com\s+justa\s+causa', 'Dispensa Com Justa Causa'),
        (r'demitid[oa]\s+(?:por|com)\s+justa\s+causa', 'Dispensa Com Justa Causa'),
        (r'justa\s+causa(?!\s+(?:na|para))', 'Dispensa Com Justa Causa'),
        # 🆕 "despedimento por justa causa"
        (r'despediment[oa]\s+(?:por|com)\s+justa\s+causa', 'Dispensa Com Justa Causa'),
        
        # Pedido de demissão
        (r'pedido\s+(?:de\s+)?demiss[aã]o', 'Pedido de Demissão'),
        # 🆕 "demitiu-se", "pediu para sair"
        (r'demitiu[\-\s]se', 'Pedido de Demissão'),
        (r'pediu\s+para\s+sair', 'Pedido de Demissão'),
        (r'requereu\s+(?:sua\s+)?demiss[aã]o', 'Pedido de Demissão'),
        
        # Término de contrato
        (r't[ée]rmino\s+(?:de\s+|do\s+)contrato', 'Término de Contrato'),
        # 🆕 "término de contrato de experiência", "encerramento de contrato temporário"
        (r't[ée]rmino\s+(?:do\s+)?contrato\s+(?:de\s+)?experi[êe]ncia', 'Término de Contrato'),
        (r'encerramento\s+(?:do\s+)?contrato', 'Término de Contrato'),
        (r'fim\s+(?:do\s+)?contrato\s+(?:de\s+)?experi[êe]ncia', 'Término de Contrato'),
        
        # Acordo entre as partes
        (r'acordo\s+entre\s+as\s+partes', 'Acordo Entre as Partes'),
        # 🆕 "acordo mútuo", "distrato"
        (r'acordo\s+m[úu]tuo', 'Acordo Entre as Partes'),
        (r'\bdistrato\b', 'Acordo Entre as Partes'),
        (r'comum\s+acordo', 'Acordo Entre as Partes'),
        
        # Padrões genéricos (menor prioridade - fallback)
        (r'(?:foi\s+)?dispensad[oa](?!\s+com)', 'Dispensa Sem Justa Causa'),
    ]
    
    # Buscar primeiros 5000 caracteres para contexto de demissão
    texto_inicial = text[:5000]
    
    for pattern, motivo_label in motivos:
        if re.search(pattern, texto_inicial, re.I):
            return motivo_label
    
    return None


def extract_pedidos(text: str) -> list:
    """
    Extrai lista de pedidos da petição inicial.
    
    FONTE: Seção "DOS PEDIDOS" ou "DOS REQUERIMENTOS"
    
    REGRAS:
    - Localizar título "DOS PEDIDOS" 
    - Capturar até marcadores de fim: "Nestes termos", "Dá-se à causa"
    - Identificar cada pedido por bullets/letras: a), b), c)... ou Requer/Seja/Condenar
    
    Returns:
        Lista de strings com cada pedido
    """
    import logging
    logger = logging.getLogger(__name__)
    
    if not text:
        return []
    
    # Normalizar texto
    text_norm = re.sub(r'\s+', ' ', text).strip()
    
    # Localizar início da seção de pedidos
    # 2025-11-27 FIX: Abordagem simplificada - encontrar início e capturar bloco de até 6000 chars
    # 2025-11-28 FIX: Adicionados padrões para formatos com numeral romano e variantes
    inicio_patterns = [
        r'D\s*O\s*S\s+P\s*E\s*D\s*I\s*D\s*O\s*S\s*(?:Diante\s+o\s+exposto,?\s*)?requer\s*:?\s*',
        r'DOS\s+PEDIDOS\s*[:\s]*',
        r'DOS\s+REQUERIMENTOS\s*[:\s]*',
        r'PEDIDOS\s+FINAIS\s*[:\s]*',
        r'(?:Diante|Ante)\s+(?:do|o)\s+exposto,?\s*requer\s*:?\s*',
        r'(?:Pelo\s+exposto|Diante\s+disso),?\s*requer\s*:?\s*',
        # 2025-11-28: Formatos com numeral romano (VI-PEDIDOS, VII-PEDIDOS, etc)
        r'[IVX]{1,4}\s*[-–]\s*PEDIDOS?\s*:?\s*(?:Assim\s+[ée]\s+a\s+presente\s+para\s+reclamar\s*:?\s*)?',
        # 2025-11-28: "PEDIDOS:" simples no início de seção
        r'\bPEDIDOS\s*:\s*(?:Assim\s+[ée]\s+a\s+presente\s+para\s+reclamar\s*:?\s*)?',
        # 2025-11-28: "Assim requer:" ou "Requer:" como início de lista de pedidos
        r'Assim\s+[ée]\s+a\s+presente\s+para\s+reclamar\s*:?\s*',
    ]
    
    inicio_match = None
    for pattern in inicio_patterns:
        match = re.search(pattern, text_norm, re.I)
        if match:
            inicio_match = match
            logger.debug(f"[PEDIDOS] Início encontrado com '{pattern[:30]}...' em pos {match.end()}")
            break
    
    if not inicio_match:
        logger.debug("[PEDIDOS] Nenhum padrão de início de pedidos encontrado")
        return []
    
    # Capturar bloco de até 12000 chars a partir do fim do match inicial
    # 2025-11-27: Aumentado para 12000 para petições com 30+ pedidos
    start_pos = inicio_match.end()
    max_len = 12000
    raw_block = text_norm[start_pos:start_pos + max_len]
    
    # Tentar encontrar um terminador próximo para delimitar melhor
    # 2025-11-28: Adicionados terminadores para texto pós-lista de pedidos
    terminators = [
        (r'Atribui-se\s+[àa]\s+causa', 'Atribui-se'),
        (r'Termos\s+em\s+que', 'Termos em que'),
        (r'Nestes\s+termos', 'Nestes termos'),
        (r'D[áa]-se\s+[àa]\s+causa', 'Dá-se à causa'),
        (r'P\.\s*Deferimento', 'P. Deferimento'),
        (r'Pede\s+deferimento', 'Pede deferimento'),
        (r'TUDO\s+A\s+SER\s+APURADO', 'TUDO A SER APURADO'),
        (r'Isto\s+posto\s+requer', 'Isto posto requer'),
        (r'Protesta\s+pela\s+produ[çc][ãa]o', 'Protesta pela produção'),
    ]
    
    end_pos = len(raw_block)
    for pattern, name in terminators:
        term_match = re.search(pattern, raw_block, re.I)
        if term_match:
            end_pos = min(end_pos, term_match.start())
            logger.debug(f"[PEDIDOS] Terminador '{name}' encontrado em pos {term_match.start()}")
            break
    
    secao_pedidos = raw_block[:end_pos].strip()
    logger.debug(f"[PEDIDOS] Seção encontrada com {len(secao_pedidos)} chars")
    
    pedidos = []
    
    # Padrão 1: Letras a), b), c)... - split por letra seguida de parêntese
    # Usa split para dividir corretamente
    partes = re.split(r'(?:^|\s)([a-z]\))', secao_pedidos, flags=re.I)
    
    # partes será algo como ['Ante o exposto, requer:', 'a)', 'condenação...', 'b)', 'pagamento...', etc]
    i = 1
    while i < len(partes) - 1:
        if re.match(r'^[a-z]\)$', partes[i], re.I):
            # Próximo elemento é o texto do pedido
            if i + 1 < len(partes):
                pedido = partes[i + 1].strip()
                # Limpar ponto e vírgula do final
                pedido = re.sub(r';?\s*$', '', pedido)
                pedido = re.sub(r'\s+', ' ', pedido)
                if pedido and len(pedido) > 10:
                    pedidos.append(pedido)
            i += 2
        else:
            i += 1
    
    # 🆕 2025-11-28: Padrão 1.2: Letras a-, b-, c-... (hífen ao invés de parêntese)
    if not pedidos:
        partes_hifen = re.split(r'(?:^|\s)([a-z]-)', secao_pedidos, flags=re.I)
        i = 1
        while i < len(partes_hifen) - 1:
            if re.match(r'^[a-z]-$', partes_hifen[i], re.I):
                if i + 1 < len(partes_hifen):
                    pedido = partes_hifen[i + 1].strip()
                    pedido = re.sub(r';?\s*$', '', pedido)
                    pedido = re.sub(r'\s+', ' ', pedido)
                    if pedido and len(pedido) > 10:
                        pedidos.append(pedido)
                i += 2
            else:
                i += 1
    
    # 🆕 2025-11-27: Padrão 1.5: Números 1), 2), 3)... (formato numerado)
    if not pedidos:
        partes_num = re.split(r'(?:^|\s)(\d{1,2}\))', secao_pedidos)
        i = 1
        while i < len(partes_num) - 1:
            if re.match(r'^\d{1,2}\)$', partes_num[i]):
                if i + 1 < len(partes_num):
                    pedido = partes_num[i + 1].strip()
                    pedido = re.sub(r';?\s*$', '', pedido)
                    pedido = re.sub(r'\s+', ' ', pedido)
                    if pedido and len(pedido) > 10:
                        pedidos.append(pedido)
                i += 2
            else:
                i += 1
    
    # Se não encontrou por letras, tentar por verbos
    if not pedidos:
        # Padrão 2: Verbos imperativos (Requer, Seja, Condenar)
        verbo_patterns = [
            r'(?:Requer(?:-se)?|Requeiro)\s+([^\.]{20,300}\.)',
            r'(?:Seja|Sejam)\s+([^\.]{20,300}\.)',
            r'(?:Condenar|A\s+condenação)\s+([^\.]{20,300}\.)',
        ]
        
        for pattern in verbo_patterns:
            for match in re.finditer(pattern, secao_pedidos, re.I):
                pedido = match.group(0).strip()
                pedido = re.sub(r'\s+', ' ', pedido)
                if pedido and len(pedido) > 20:
                    pedidos.append(pedido)
    
    # 2025-11-28: Limpar texto residual de cada pedido (frases pós-lista)
    post_list_markers = [
        r'\s*TUDO\s+A\s+SER\s+APURADO.*',
        r'\s*Isto\s+posto\s+requer.*',
        r'\s*Protesta\s+pela\s+produ[çc][ãa]o.*',
        r'\s*Os\s+valores\s+acima\s+s[ãa]o\s+de\s+al[çc]ada.*',
    ]
    pedidos_limpos = []
    for pedido in pedidos:
        for marker in post_list_markers:
            pedido = re.sub(marker, '', pedido, flags=re.I | re.DOTALL)
        pedido = pedido.strip()
        if pedido and len(pedido) > 10:
            pedidos_limpos.append(pedido)
    
    # Remover duplicatas mantendo ordem
    seen = set()
    unique_pedidos = []
    for p in pedidos_limpos:
        p_norm = p.lower()[:50]  # Comparar primeiros 50 chars
        if p_norm not in seen:
            seen.add(p_norm)
            unique_pedidos.append(p)
    
    logger.debug(f"[PEDIDOS] Extraídos {len(unique_pedidos)} pedidos")
    return unique_pedidos[:15]  # Limitar a 15 pedidos


def extract_advogado_adverso(text: str) -> tuple:
    """
    Extrai advogado da parte adversa (RECLAMADO/empregador).
    
    REGRAS:
    - Em processos trabalhistas, a parte adversa é o RECLAMADO (empregador)
    - Buscar advogado listado após RECLAMADO/RÉU na capa do processo
    - Retornar (nome, OAB) ou (None, None)
    
    Returns:
        Tupla (advogado_nome, advogado_oab)
    """
    import logging
    logger = logging.getLogger(__name__)
    
    if not text:
        return None, None
    
    # Normalizar texto
    text_norm = re.sub(r'\s+', ' ', text).strip()
    
    # PADRÃO 1: Advogado com OAB em qualquer contexto de contestação/defesa
    # "Advogado Dr. CARLOS ALBERTO SOUZA, OAB/RJ 98765"
    # "por seu Advogado MARIA SILVA OAB/SP 12345"
    contestacao_patterns = [
        # "Advogado Dr. NOME, OAB/XX 12345"
        r'(?:Advogado|Procurador)\s+(?:Dr\.?\s+|Dra\.?\s+)?([A-ZÀ-Ú][A-ZÀ-Úa-zà-ú\s\.]+?)[,\s]+OAB[/\s\-]*([A-Z]{2})\s*[:\s\-]*(\d+)',
        # "Advogado NOME OAB/XX 12345" (sem vírgula)
        r'(?:Advogado|Procurador)\s+(?:Dr\.?\s+|Dra\.?\s+)?([A-ZÀ-Ú][A-ZÀ-Úa-zà-ú\s\.]+?)\s+OAB[/\s\-]*([A-Z]{2})\s*[:\s\-]*(\d+)',
        # "por seu Advogado NOME, OAB/XX 12345"  
        r'por\s+seu\s+(?:Advogado|Procurador)\s+(?:Dr\.?\s+|Dra\.?\s+)?([A-ZÀ-Ú][A-ZÀ-Úa-zà-ú\s\.]+?)[,\s]+OAB[/\s\-]*([A-Z]{2})\s*[:\s\-]*(\d+)',
    ]
    
    # Buscar primeiro no contexto de contestação/defesa (mais provável ser adverso)
    for pattern in contestacao_patterns:
        match = re.search(pattern, text_norm, re.I)
        if match:
            nome = match.group(1).strip()
            nome = re.sub(r'\s+', ' ', nome)
            # Limpar trailing como vírgulas
            nome = nome.rstrip(',. ')
            oab = f"OAB/{match.group(2).upper()} {match.group(3)}"
            
            # Validar que não é muito curto
            if len(nome) > 5:
                logger.debug(f"[ADV_ADVERSO] Encontrado: {nome}, {oab}")
                return nome, oab
    
    # PADRÃO 2: Capa do processo - após "RECLAMADO/RÉU"
    # "RECLAMADO: EMPRESA ADVOGADO: NOME OAB/XX 12345"
    reu_advogado = re.search(
        r'(?:RECLAMADO|RECLAMADA|R[ÉE]U)[:\s]+.{5,200}?(?:ADVOGADO|ADV\.?)[:\s]+([A-ZÀ-Úa-zà-ú\s\.]+?)\s+OAB[/\s\-]*([A-Z]{2})\s*[:\s\-]*(\d+)',
        text_norm,
        re.I
    )
    
    if reu_advogado:
        nome = reu_advogado.group(1).strip()
        nome = re.sub(r'\s+', ' ', nome)
        nome = nome.rstrip(',. ')
        oab = f"OAB/{reu_advogado.group(2).upper()} {reu_advogado.group(3)}"
        
        if len(nome) > 5:
            logger.debug(f"[ADV_ADVERSO] Encontrado na capa: {nome}, {oab}")
            return nome, oab
    
    return None, None


# ============================================================================
# MÚLTIPLAS RECLAMADAS - Extração de todas as partes reclamadas
# ============================================================================

def _normalizar_nome_reclamada(nome: str) -> str:
    """
    Normaliza o nome de uma reclamada para comparação e deduplicação.
    Remove sufixos de metadados do PDF, espaços extras, etc.
    """
    if not nome:
        return ""
    
    nome_norm = nome.upper().strip()
    
    sufixos_remover = [
        r'\s+PAGINA_CAPA_PROCESSO_PJE?$',
        r'\s+PAGINA_CAPA_PROCESSO$',
        r'\s+CAPA_PROCESSO$',
        r'\s+PJE$',
        r'\s+\(POLO PASSIVO\)$',
        r'\s+\(RECLAMAD[OA]\)$',
        r'\s+\(R[ÉE]U?\)$',
    ]
    
    for sufixo in sufixos_remover:
        nome_norm = re.sub(sufixo, '', nome_norm, flags=re.I)
    
    nome_norm = re.sub(r'\s+', ' ', nome_norm).strip()
    nome_norm = nome_norm.rstrip('.,;:-')
    
    return nome_norm


def _is_nome_invalido(nome: str) -> bool:
    """
    Verifica se o nome é inválido/lixo que não deveria ser considerado como reclamada.
    """
    if not nome or len(nome) < 5:
        return True
    
    nome_upper = nome.upper()
    
    termos_invalidos = [
        'AUDIENCIA',
        'AUDIÊNCIA',
        'DOMICILIO ELETRONICO',
        'DOMICÍLIO ELETRÔNICO',
        'NOTIFICAÇÃO',
        'NOTIFICACAO',
        'DATA AJUIZAMENTO',
        'PERIODO DO CALCULO',
        'PERÍODO DO CÁLCULO',
        'PLANILHA DE',
        'PAGINA_',
        'CAPA_PROCESSO',
        'POLO PASSIVO',
        'VALOR DA CAUSA',
        'CLASSE JUDICIAL',
        'ASSUNTO CNJ',
        'DISTRIBUICAO',
        'DISTRIBUIÇÃO',
        'COMPETENCIA',
        'COMPETÊNCIA',
        'ORGAO JULGADOR',
        'ÓRGÃO JULGADOR',
        'RELATOR',
        'JULGAMENTO',
        'RECUSA EM',
        'CONDUTA DA',
        'EXPOSIÇÃO AOS',
        'PRODUÇÃO DE PROVA',
        'MEDIANTE',
        'INSTRUÇÃO',
        'PERFIL PRO',
        'RETIFICAÇÃO',
        'AGENTES NOCIVOS',
        'PROVA DOCUMENTAL',
        'PROVA PERICIAL',
        'PEDIDO DE',
        'PEDIDOS PREVI',
        'PPP E O LTCAT',
        'EMITIR O PPP',
    ]
    
    for termo in termos_invalidos:
        if termo in nome_upper:
            return True
    
    if re.match(r'^\d{2}/\d{2}/\d{4}', nome_upper):
        return True
    
    if re.match(r'^\d+\s+[A-Z]{2},?\s+RELATOR', nome_upper):
        return True
    
    if not re.search(r'[A-ZÀ-Ú]{3,}', nome_upper):
        return True
    
    palavras = nome_upper.split()
    if len(palavras) < 2:
        return True
    
    if len(palavras[0]) < 2 or not palavras[0][0].isalpha():
        return True
    
    if any(termo in nome_upper for termo in ['DOLOSA', 'RECUSA', 'ADOTAR', 'GISTRO', 'TIFICAÇÃO', 'CLAMADA']):
        if not any(termo in nome_upper for termo in ['LTDA', 'S.A', 'S/A', 'EIRELI', 'ME', 'EPP', 'CIA', 'COMPANHIA']):
            return True
    
    return False


def _nomes_sao_similares(nome1: str, nome2: str, threshold: float = 0.85) -> bool:
    """
    Verifica se dois nomes são similares usando fuzzy matching.
    Retorna True se a similaridade for >= threshold.
    """
    from rapidfuzz import fuzz
    
    n1 = _normalizar_nome_reclamada(nome1)
    n2 = _normalizar_nome_reclamada(nome2)
    
    if not n1 or not n2:
        return False
    
    if n1 == n2:
        return True
    
    if n1 in n2 or n2 in n1:
        return True
    
    ratio = fuzz.ratio(n1, n2) / 100.0
    return ratio >= threshold


def extract_todas_reclamadas(text: str) -> list:
    """
    Extrai TODAS as partes reclamadas do texto do PDF.
    
    Identifica todas as partes com papel de ré/reclamada, como:
    - RECLAMADO, RECLAMADA, RECLAMADOS, RECLAMADAS
    - RÉU, RÉ, RÉUS
    - APELADO, AGRAVADO, etc.
    
    Inclui:
    - Normalização de nomes (remove sufixos de metadados do PDF)
    - Deduplicação com fuzzy matching
    - Filtro de nomes inválidos
    
    Returns:
        Lista de dicionários, cada um contendo:
        {
            "nome": "<NOME COMPLETO DA RECLAMADA>",
            "posicao": "<posição no processo: RECLAMADO, RÉU, etc>",
            "tipo_pessoa": "fisica" | "juridica" | None
        }
        
        A primeira posição (índice 0) é a reclamada principal.
        As demais (índice 1+) são reclamadas extras.
    """
    import logging
    logger = logging.getLogger(__name__)
    
    if not text:
        logger.debug("[RECLAMADAS][EXTRACT] Texto vazio, retornando lista vazia")
        return []
    
    t = text
    candidatas = []
    
    pattern = r'\b(RECLAMAD[OA]S?|R[ÉE]US?|R[ÉE]\b|APOLAD[OA]|AGRAVAD[OA]|EMBARGAD[OA]|EXECUTAD[OA]|REQUERID[OA]|DEMANDAD[OA])\s*[:\-]\s*([^\n]+)'
    
    for match in re.finditer(pattern, t, re.I):
        label = match.group(1).upper()
        nome_bruto = match.group(2).strip()
        
        tokens_fim = [
            r'\s+ADVOGADO\b', r'\s+ADV\.', r'\s+CPF\b', r'\s+CNPJ\b',
            r'\s+RECLAMANTE\b', r'\s+AUTOR\b', r'\s+PARTES\b',
            r'\s+\d{2}\.\d{3}\.\d{3}',
            r'\s+\d{2}\.\d{3}\.\d{3}/\d{4}',
        ]
        
        nome_limpo = nome_bruto
        for tok in tokens_fim:
            nome_limpo = re.split(tok, nome_limpo, flags=re.I)[0].strip()
        
        nome_limpo = nome_limpo.rstrip('.,;:')
        nome_limpo = re.sub(r'\s+', ' ', nome_limpo).strip()
        
        nome_normalizado = _normalizar_nome_reclamada(nome_limpo)
        
        if _is_nome_invalido(nome_normalizado):
            logger.debug(f"[RECLAMADAS][EXTRACT] Ignorando nome inválido: {nome_limpo[:50]}")
            continue
        
        label_norm = label.replace("É", "E").replace("Ã", "A")
        if "RECLAM" in label_norm:
            posicao = "RECLAMADO"
        elif "REU" in label_norm or "RE" in label_norm:
            posicao = "REU"
        else:
            posicao = label
        
        tipo = "juridica" if is_pessoa_juridica(nome_limpo) else "fisica"
        
        candidatas.append({
            "nome": nome_normalizado,
            "nome_original": nome_limpo,
            "posicao": posicao,
            "tipo_pessoa": tipo
        })
    
    reclamadas = []
    for candidata in candidatas:
        is_duplicata = False
        for existente in reclamadas:
            if _nomes_sao_similares(candidata["nome"], existente["nome"]):
                is_duplicata = True
                logger.debug(f"[RECLAMADAS][EXTRACT] Duplicata ignorada: '{candidata['nome'][:40]}' similar a '{existente['nome'][:40]}'")
                break
        
        if not is_duplicata:
            reclamadas.append({
                "nome": candidata["nome"],
                "posicao": candidata["posicao"],
                "tipo_pessoa": candidata["tipo_pessoa"]
            })
            logger.info(f"[RECLAMADAS][EXTRACT] Encontrada: {candidata['nome']} ({candidata['posicao']}, {candidata['tipo_pessoa']})")
    
    reclamadas_pj = [r for r in reclamadas if r["tipo_pessoa"] == "juridica"]
    reclamadas_pf = [r for r in reclamadas if r["tipo_pessoa"] != "juridica"]
    reclamadas = reclamadas_pj + reclamadas_pf
    
    logger.info(f"[RECLAMADAS][EXTRACT] Total: {len(reclamadas)} reclamadas únicas (PJ: {len(reclamadas_pj)}, PF: {len(reclamadas_pf)})")
    
    return reclamadas
