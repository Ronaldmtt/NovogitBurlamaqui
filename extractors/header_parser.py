# extractors/header_parser.py
"""
Parser centralizado para cabeçalho de processos judiciais.
Combina informações do filename (ATSum/ATOrd) + conteúdo PDF + regex robusto.
"""
import re
from typing import Dict, Any, Optional


# Mapeamento comarca → UF (expandido para cobrir principais comarcas brasileiras)
COMARCA_UF_MAP = {
    # Rio de Janeiro
    "rio de janeiro": "RJ", "niterói": "RJ", "são gonçalo": "RJ", "duque de caxias": "RJ",
    "nova iguaçu": "RJ", "belford roxo": "RJ", "são joão de meriti": "RJ", "campos dos goytacazes": "RJ",
    "petrópolis": "RJ", "volta redonda": "RJ", "macaé": "RJ", "cabo frio": "RJ",
    "nova friburgo": "RJ", "barra mansa": "RJ", "niter\u00f3i": "RJ", "angra dos reis": "RJ",
    "mesquita": "RJ", "teresópolis": "RJ", "magé": "RJ", "itaboraí": "RJ",
    "maricá": "RJ", "itaguaí": "RJ", "resende": "RJ", "araruama": "RJ",
    "queimados": "RJ", "são pedro da aldeia": "RJ", "nilópolis": "RJ",
    "três rios": "RJ", "tres rios": "RJ",  # Adicionado para suporte a recurso ordinário
    
    # São Paulo
    "são paulo": "SP", "guarulhos": "SP", "campinas": "SP", "são bernardo do campo": "SP",
    "santo andré": "SP", "osasco": "SP", "sorocaba": "SP", "ribeirão preto": "SP",
    "santos": "SP", "mauá": "SP", "são josé dos campos": "SP", "diadema": "SP",
    "carapicuíba": "SP", "mogi das cruzes": "SP", "piracicaba": "SP", "bauru": "SP",
    "são vicente": "SP", "itaquaquecetuba": "SP", "jundiaí": "SP", "franca": "SP",
    "praia grande": "SP", "guarujá": "SP", "taubaté": "SP", "são carlos": "SP",
    "limeira": "SP", "suzano": "SP", "taboão da serra": "SP", "embu das artes": "SP",
    
    # Minas Gerais
    "belo horizonte": "MG", "uberlândia": "MG", "contagem": "MG", "juiz de fora": "MG",
    "betim": "MG", "montes claros": "MG", "ribeirão das neves": "MG", "uberaba": "MG",
    "governador valadares": "MG", "ipatinga": "MG", "sete lagoas": "MG", "divinópolis": "MG",
    "santa luzia": "MG", "ibirité": "MG", "poços de caldas": "MG", "patos de minas": "MG",
    
    # Outras capitais e grandes cidades
    "salvador": "BA", "fortaleza": "CE", "recife": "PE", "porto alegre": "RS",
    "curitiba": "PR", "brasília": "DF", "manaus": "AM", "belém": "PA",
    "goiânia": "GO", "são luís": "MA", "maceió": "AL", "natal": "RN",
    "teresina": "PI", "joão pessoa": "PB", "aracaju": "SE", "cuiabá": "MT",
    "campo grande": "MS", "florianópolis": "SC", "vitória": "ES", "palmas": "TO",
}


def _extract_comarca_from_text(text: str) -> Optional[str]:
    """
    Extrai comarca do cabeçalho usando pipeline hierárquico de 3 tiers.
    
    TIER 1: Regex autoritativo para frases judiciais canônicas
    TIER 2: Busca de cidades conhecidas próximas a palavras-chave judiciais
    TIER 3: (implementar futuramente) Fallback CNJ-based
    """
    # Normalização: remove underscores, normaliza espaços, pega primeiros 3000 chars
    cabecalho = text[:3000]
    cabecalho_norm = re.sub(r'[_\s]+', ' ', cabecalho)
    
    # ============================================================================
    # TIER 1: Regex autoritativo para frases judiciais canônicas
    # ============================================================================
    
    # Padrão 1a: "FÓRUM TRABALHISTA DE [CIDADE]"
    m = re.search(r'F[OÓU]R[UO]M\s+(?:TRABALHISTA|CRIMINAL|C[IÍ]VEL)\s+DE\s+([A-ZÀÁÂÃÉÊÍÓÔÕÚÇ][a-zàáâãéêíóôõúç\s]+?)(?:\s*[-–/]\s*[A-Z]{2}|\s*[-–])', cabecalho_norm, re.IGNORECASE)
    if m:
        comarca = m.group(1).strip()
        return _clean_comarca(comarca)
    
    # Padrão 1b: "VARA DO TRABALHO DE/DO [CIDADE]" (cidades compostas específicas)
    padroes_vara = [
        (r'VARA\s+DO\s+TRABALHO\s+D[EOA]\s+RIO\s+DE\s+JANEIRO', 'Rio de Janeiro'),
        (r'VARA\s+DO\s+TRABALHO\s+D[EOA]\s+SÃO\s+PAULO', 'São Paulo'),
        (r'VARA\s+DO\s+TRABALHO\s+D[EOA]\s+BELO\s+HORIZONTE', 'Belo Horizonte'),
        (r'VARA\s+DO\s+TRABALHO\s+D[EOA]\s+PORTO\s+ALEGRE', 'Porto Alegre'),
    ]
    
    for pattern, cidade_oficial in padroes_vara:
        if re.search(pattern, cabecalho_norm, re.IGNORECASE):
            return cidade_oficial
    
    # Padrão 1c: "COMARCA DE [CIDADE]"
    m = re.search(r'COMARCA\s+D[EOA]\s+([A-ZÀÁÂÃÉÊÍÓÔÕÚÇ][a-zàáâãéêíóôõúç\s]+?)(?:\s*[-–,/]\s*[A-Z]{2}|\s*[-–]|$)', cabecalho_norm, re.IGNORECASE)
    if m:
        comarca = m.group(1).strip()
        return _clean_comarca(comarca)
    
    # ============================================================================
    # TIER 2: Ranking de cidades candidatas por proximidade a keywords judiciais
    # ============================================================================
    # Mapeamento cidade uppercase → nome oficial formatado (expandido)
    # Gerado dinamicamente do COMARCA_UF_MAP para consistência
    CIDADES_CONHECIDAS = {k.upper(): k.title() if ' de ' not in k and ' da ' not in k and ' do ' not in k 
                          else ' '.join(palavra.capitalize() if i == 0 or palavra not in ['de', 'da', 'do', 'das', 'dos'] 
                                      else palavra for i, palavra in enumerate(k.split()))
                          for k in COMARCA_UF_MAP.keys()}
    
    candidatos = []
    cabecalho_upper = cabecalho_norm.upper()
    
    # Busca cada cidade no cabeçalho (do maior para o menor nome)
    for cidade_upper, cidade_oficial in sorted(CIDADES_CONHECIDAS.items(), key=lambda x: -len(x[0])):
        idx = cabecalho_upper.find(cidade_upper)
        if idx >= 0:
            # Calcula score baseado em proximidade a palavras judiciais
            contexto_antes = cabecalho_upper[max(0, idx-300):idx]
            contexto_depois = cabecalho_upper[idx+len(cidade_upper):min(len(cabecalho_upper), idx+len(cidade_upper)+50)]
            
            # Palavras judiciais aumentam score
            palavras_judiciais = ['VARA', 'JUIZ', 'JUÍZO', 'TRIBUNAL', 'FÓRUM', 'FORUM', 'TRABALHO']
            score_judicial = sum(1 for kw in palavras_judiciais if kw in contexto_antes)
            
            # Palavras de endereço diminuem score
            palavras_endereco = [
                'RESIDENTE', 'DOMICILIAD', 'ESCRITÓRIO', 'ESCRITORIO',
                'ADVOGADO', 'PROCURADOR', 'ESTABELECID', 'INSCRIT'
            ]
            penalidade = sum(1 for kw in palavras_endereco if kw in contexto_antes)
            
            score_final = score_judicial - penalidade * 2
            
            # Se score é positivo, é candidato válido
            if score_final > 0:
                candidatos.append((cidade_oficial, score_final, idx))
    
    # Retorna candidato com maior score (mais provável de ser comarca)
    if candidatos:
        candidatos.sort(key=lambda x: (-x[1], x[2]))  # Maior score, menor índice
        return candidatos[0][0]
    
    # ============================================================================
    # TIER 3: Fallback genérico - extrai qualquer cidade após palavra judicial
    # ============================================================================
    # Se Tier 1 e 2 falharam, tenta extrair qualquer nome de cidade após keywords judiciais
    # Padrão permissivo: captura até 5 palavras após preposição, incluindo preposições internas
    
    # Busca padrão muito permissivo: qualquer sequência de palavras até encontrar sufixo UF ou quebra
    m = re.search(
        r'(?:VARA|JUIZ|JUÍZO|TRIBUNAL|F[ÓO]R[UO]M)\s+(?:[\w\s]{0,40}?)\s+(?:DA|DE|DO)\s+([A-ZÀÁÂÃÉÊÍÓÔÕÚÇ][\wàáâãéêíóôõúç\s]+?)(?:\s*[-–/]\s*[A-Z]{2}|\s*RJ|\s*SP|\s*MG|$)',
        cabecalho_norm,
        re.IGNORECASE
    )
    if m:
        cidade = m.group(1).strip()
        # Remove palavras-chave judiciais que podem ter sido capturadas
        cidade = re.sub(r'\b(VARA|TRABALHO|JUSTIÇA|TRIBUNAL|FEDERAL|ESTADUAL|JUIZ|JUÍZO)\b', '', cidade, flags=re.IGNORECASE).strip()
        cidade = re.sub(r'\s+', ' ', cidade).strip()  # Normaliza espaços
        
        # Se sobrou algo e não é apenas preposição/número
        if cidade and len(cidade) > 2 and not re.match(r'^(da|de|do|das|dos|\d+[aªº]?)$', cidade, re.IGNORECASE):
            return _clean_comarca(cidade)
    
    # Último recurso: procura por "DE [CIDADE]" nas primeiras linhas
    linhas = cabecalho_norm.split('\n')[:5]
    for linha in linhas:
        if any(kw in linha.upper() for kw in ['VARA', 'JUIZ', 'TRIBUNAL', 'FÓRUM']):
            # Procura padrão simples: preposição + palavras capitalizadas + sufixo UF
            m = re.search(r'\b(?:DE|DO|DA)\s+([A-ZÀÁÂÃÉÊÍÓÔÕÚÇ][\wàáâãéêíóôõúç\s]{3,40}?)(?:\s*[-–/]?\s*[A-Z]{2}\b|\s*[-–]\s|$)', linha)
            if m:
                cidade = m.group(1).strip()
                cidade = re.sub(r'\b(VARA|TRABALHO|JUSTIÇA|TRIBUNAL)\b', '', cidade, flags=re.IGNORECASE).strip()
                cidade = re.sub(r'\s+', ' ', cidade).strip()
                if cidade and len(cidade) > 2:
                    return _clean_comarca(cidade)
    
    return None


def _clean_comarca(comarca: str) -> str:
    """
    Limpa e normaliza nome de comarca:
    - Remove sufixos como "/RJ", "- RJ", "RJ", "- SE"
    - Remove prefixos espúrios como "De " inicial
    - Capitaliza corretamente (mantém preposições em minúsculo)
    - Remove espaços extras
    """
    # Remove sufixos de UF (incluindo variantes como "- SE", "/RJ", etc.)
    comarca = re.sub(r'\s*[-–/]\s*[A-Z]{2}\s*$', '', comarca)
    comarca = re.sub(r'\s+[A-Z]{2}\s*$', '', comarca)
    
    # Remove prefixos espúrios como "De " no início (comum em falsos positivos)
    comarca = re.sub(r'^\s*De\s+', '', comarca, flags=re.IGNORECASE)
    
    # Normaliza espaços
    comarca = re.sub(r'\s+', ' ', comarca).strip()
    
    # Capitaliza corretamente (mantém preposições em minúsculo)
    palavras = comarca.split()
    palavras_capitalizadas = []
    
    preposicoes = {'de', 'da', 'do', 'das', 'dos'}
    
    for i, palavra in enumerate(palavras):
        palavra_lower = palavra.lower()
        # Primeira palavra sempre maiúscula, preposições sempre minúsculas
        if i == 0 or palavra_lower not in preposicoes:
            palavras_capitalizadas.append(palavra_lower.capitalize())
        else:
            palavras_capitalizadas.append(palavra_lower)
    
    return ' '.join(palavras_capitalizadas)


def _extract_numero_orgao_from_text(text: str) -> Optional[str]:
    """Extrai número do órgão (ex: 71ª Vara → 71)."""
    cabecalho = text[:3000]
    m = re.search(r'(\d+)[ªº]?\s*VARA\s+DO\s+TRABALHO', cabecalho, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _extract_rito_from_text(text: str) -> Optional[str]:
    """
    Extrai rito do processo (Sumaríssimo, Ordinário, Sumário).
    Prioridade para padrões mais específicos.
    """
    cabecalho = text[:3000]
    
    # Padrão 1: "Rito Sumaríssimo" ou "Sumaríssimo"
    if re.search(r"Rito\s+Sumarí[sS]+imo|Sumarí[sS]+imo|SUMARÍSSIMO|procedimento\s+sumarí[sS]+imo", cabecalho, re.IGNORECASE):
        return "Sumaríssimo"
    
    # Padrão 2: "Rito Ordinário" ou "Ordinário"
    if re.search(r"Rito\s+Ordin[aá]rio|Ordin[aá]rio|ORDINÁRIO|Ação\s+Trabalhista\s+[-–]\s+Rito\s+Ordin[aá]rio", cabecalho, re.IGNORECASE):
        return "Ordinário"
    
    # Padrão 3: "Rito Sumário" (evitar pegar "Sumaríssimo")
    if re.search(r"Rito\s+Sum[aá]rio(?!\s*[sí])|Sum[aá]rio(?!\s*[sí])|SUMÁRIO(?!SSI)", cabecalho, re.IGNORECASE):
        return "Sumário"
    
    return None


def _extract_rito_from_filename(filename: Optional[str]) -> Optional[str]:
    """
    Extrai rito do prefixo do filename:
    - ATSum → Sumaríssimo
    - ATOrd → Ordinário
    """
    if not filename:
        return None
    
    filename_upper = filename.upper()
    
    if filename_upper.startswith("ATSUM"):
        return "Sumaríssimo"
    elif filename_upper.startswith("ATORD"):
        return "Ordinário"
    
    return None


def _infer_estado_from_comarca(comarca: Optional[str]) -> Optional[str]:
    """Infere UF a partir da comarca usando mapeamento."""
    if not comarca:
        return None
    
    comarca_norm = comarca.lower().strip()
    return COMARCA_UF_MAP.get(comarca_norm)


def parse_header_info(text: str, filename: Optional[str] = None) -> Dict[str, Any]:
    """
    Parser centralizado que retorna {comarca, numero_orgao, rito, estado}.
    Combina informações do filename + conteúdo do PDF.
    
    Prioridade para RITO:
    1. Filename (ATSum/ATOrd) - mais confiável
    2. Conteúdo do PDF
    
    Prioridade para ESTADO:
    1. Mapeamento comarca → UF
    2. Fallback para extract_estado_sigla()
    """
    result = {}
    
    # 1. Extrai comarca
    comarca = _extract_comarca_from_text(text)
    if comarca:
        result["comarca"] = comarca
    
    # 2. Extrai número do órgão
    numero_orgao = _extract_numero_orgao_from_text(text)
    if numero_orgao:
        result["numero_orgao"] = numero_orgao
    
    # 3. Extrai rito (filename tem prioridade)
    rito_filename = _extract_rito_from_filename(filename)
    rito_content = _extract_rito_from_text(text)
    
    # Prioriza filename, mas valida com conteúdo se ambos existirem
    if rito_filename and rito_content:
        # Se ambos concordam, usa
        if rito_filename == rito_content:
            result["rito"] = rito_filename
        else:
            # Filename tem prioridade (convenção de nomenclatura oficial)
            result["rito"] = rito_filename
    elif rito_filename:
        result["rito"] = rito_filename
    elif rito_content:
        result["rito"] = rito_content
    
    # 4. Infere estado da comarca
    if comarca:
        estado = _infer_estado_from_comarca(comarca)
        if estado:
            result["estado"] = estado
    
    return result
