import re
import logging
logger = logging.getLogger(__name__)

def parse_decisao_tipo(text: str):
    # Verifica contexto de decisão judicial, não apenas palavra solta
    text_lower = (text or "").lower()
    
    # Padrões que indicam REALMENTE uma sentença/acórdão
    sentenca_patterns = [
        r"sentença\s+(proferida|publicada|transitada)",
        r"julgo\s+(procedente|improcedente)",
        r"dispositivo.*sentença",
        r"sentença.*julg"
    ]
    acordao_patterns = [
        r"acórdão\s+(proferido|publicado)",
        r"acordam\s+os\s+desembargadores",
        r"vistos.*relatados.*e\s+discutidos"
    ]
    
    for pattern in sentenca_patterns:
        if re.search(pattern, text_lower):
            return "Sentença"
    
    for pattern in acordao_patterns:
        if re.search(pattern, text_lower):
            return "Acórdão"
    
    return None

def parse_decisao_resultado(text: str):
    # Só identifica DECISÕES efetivas, não pedidos ou condicionais
    text_lower = (text or "").lower()
    
    # Padrões que indicam DECISÃO já tomada (verbos no pretérito/conclusivo)
    # IMPORTANTE: verificar negativo PRIMEIRO (improcedente contém "procedente")
    
    # Padrões negativos (indeferido/improcedente) - APENAS com verbos decisórios
    negative_patterns = [
        r"(julgo|julguei)\s+.*improcedente",
        r"(julgo|julguei)\s+.*indeferid[ao]",
        r"(foi|restou|encontra-se)\s+.*improcedente",
        r"(foi|restou|encontra-se)\s+.*indeferid[ao]",
        r"(ação|pedido|recurso)\s+(foi|restou|julgad[ao])\s+.*improcedente",
        r"(ação|pedido|recurso)\s+(foi|restou|julgad[ao])\s+.*indeferid[ao]"
    ]
    
    for pattern in negative_patterns:
        if re.search(pattern, text_lower):
            return "Indeferido"
    
    # Padrões positivos (deferido/procedente) - APENAS com verbos decisórios conclusivos
    positive_patterns = [
        r"(julgo|julguei)\s+.*(procedente|deferid[ao])",
        r"(foi|restou|encontra-se)\s+.*(procedente|deferid[ao])",
        r"(ação|pedido|recurso)\s+(foi|restou|julgad[ao])\s+.*(procedente|deferid[ao])"
    ]
    
    for pattern in positive_patterns:
        if re.search(pattern, text_lower):
            return "Deferido"
    
    return None

def parse_fundamentacao_resumida(text: str):
    if not text:
        return None
    parts = [p.strip() for p in text.split(".") if len(p.strip()) > 60]
    return parts[0] if parts else None
