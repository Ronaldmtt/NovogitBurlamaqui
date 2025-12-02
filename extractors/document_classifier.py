from enum import Enum
import re
from typing import Tuple, Optional

class DocumentType(Enum):
    PETICAO_INICIAL = "Petição Inicial"
    NOTIFICACAO = "Notificação/Intimação"
    DECISAO_INTERLOCUTORIA = "Decisão Interlocutória"
    SENTENCA = "Sentença"
    ACORDAO = "Acórdão"
    ATA_AUDIENCIA = "Ata de Audiência"
    MANIFESTACAO = "Manifestação/Contestação"
    OUTROS = "Outros"

# Mapeamento: tipo de documento → lista de extractors que devem ser executados
DOCUMENT_TYPE_EXTRACTORS = {
    DocumentType.PETICAO_INICIAL: [
        'parse_numero_processo_cnj',
        'parse_numero_processo_antigo',
        'parse_autor',
        'parse_reu', 
        'parse_advogado_autor',
        'parse_advogado_reu',
        'parse_vara',
        'parse_celula',
        'parse_orgao',
        'parse_comarca',
        'parse_uf',
        'parse_id_interno_hilo'
    ],
    DocumentType.NOTIFICACAO: [
        'parse_numero_processo_cnj',
        'parse_numero_processo_antigo',
        'parse_autor',
        'parse_reu',
        'parse_prazo',
        'parse_tipo_notificacao',
        'parse_prazos_derivados',
        'parse_vara',
        'parse_celula',
        'parse_orgao',
        'parse_comarca',
        'parse_uf',
        'parse_id_interno_hilo'
    ],
    DocumentType.DECISAO_INTERLOCUTORIA: [
        'parse_numero_processo_cnj',
        'parse_numero_processo_antigo',
        'parse_autor',
        'parse_reu',
        'parse_decisao_tipo',
        'parse_decisao_resultado',
        'parse_vara',
        'parse_celula',
        'parse_orgao',
        'parse_comarca',
        'parse_uf',
        'parse_fundamentacao_resumida',
        'parse_id_interno_hilo'
    ],
    DocumentType.SENTENCA: [
        'parse_numero_processo_cnj',
        'parse_numero_processo_antigo',
        'parse_autor',
        'parse_reu',
        'parse_decisao_tipo',
        'parse_decisao_resultado',
        'parse_vara',
        'parse_celula',
        'parse_orgao',
        'parse_comarca',
        'parse_uf',
        'parse_fundamentacao_resumida',
        'parse_id_interno_hilo'
    ],
    DocumentType.ACORDAO: [
        'parse_numero_processo_cnj',
        'parse_numero_processo_antigo',
        'parse_autor',
        'parse_reu',
        'parse_decisao_tipo',
        'parse_decisao_resultado',
        'parse_vara',
        'parse_celula',
        'parse_orgao',
        'parse_comarca',
        'parse_uf',
        'parse_fundamentacao_resumida',
        'parse_id_interno_hilo'
    ],
    DocumentType.ATA_AUDIENCIA: [
        'parse_numero_processo_cnj',
        'parse_numero_processo_antigo',
        'parse_autor',
        'parse_reu',
        'parse_audiencia_inicial',
        'parse_resultado_audiencia',
        'parse_vara',
        'parse_celula',
        'parse_orgao',
        'parse_comarca',
        'parse_uf',
        'parse_id_interno_hilo'
    ],
    DocumentType.MANIFESTACAO: [
        'parse_numero_processo_cnj',
        'parse_numero_processo_antigo',
        'parse_autor',
        'parse_reu',
        'parse_vara',
        'parse_celula',
        'parse_orgao',
        'parse_comarca',
        'parse_uf',
        'parse_id_interno_hilo'
    ],
    DocumentType.OUTROS: [
        'parse_numero_processo_cnj',
        'parse_numero_processo_antigo',
        'parse_autor',
        'parse_reu',
        'parse_vara',
        'parse_celula',
        'parse_orgao',
        'parse_comarca',
        'parse_uf',
        'parse_id_interno_hilo'
    ]
}

def classify_document(text: str) -> Tuple[DocumentType, float]:
    """
    Classifica o tipo de documento jurídico baseado em heurísticas.
    
    Returns:
        Tuple[DocumentType, float]: (tipo_documento, confiança 0-1)
    """
    if not text:
        return DocumentType.OUTROS, 0.0
    
    text_lower = text.lower()
    text_sample = text[:3000]  # Primeiras 3000 chars para análise
    
    # Heurísticas por ordem de especificidade
    
    # 1. Ata de Audiência - muito específica
    ata_patterns = [
        r'ata\s+de\s+audi[eê]ncia',
        r'termo\s+de\s+audi[eê]ncia',
        r'aos?\s+\d+\s+dias?\s+de\s+\w+.*realizou-se\s+audi[eê]ncia',
        r'audi[eê]ncia\s+.*\s+realizada'
    ]
    if any(re.search(pattern, text_lower[:1000]) for pattern in ata_patterns):
        return DocumentType.ATA_AUDIENCIA, 0.95
    
    # 2. Acórdão - detectar antes de sentença
    acordao_patterns = [
        r'ac[oó]rd[aã]o',
        r'tribunal\s+(regional|de\s+justi[cç]a)',
        r'relatora?:',
        r'vistos?,?\s+relatados?\s+e\s+discutidos?'
    ]
    acordao_count = sum(1 for p in acordao_patterns if re.search(p, text_lower[:2000]))
    if acordao_count >= 2:
        return DocumentType.ACORDAO, 0.9
    
    # 3. Sentença - muito específica
    sentenca_patterns = [
        r'senten[cç]a',
        r'julgo\s+(im)?procedente',
        r'ante\s+o\s+exposto.*julgo',
        r'dispositivo:?\s*julgo'
    ]
    sentenca_count = sum(1 for p in sentenca_patterns if re.search(p, text_lower[:2000]))
    if sentenca_count >= 2:
        return DocumentType.SENTENCA, 0.9
    
    # 4. Decisão Interlocutória
    decisao_patterns = [
        r'decis[aã]o\s+interlocut[oó]ria',
        r'(indefiro|defiro)(?!\s+(o\s+)?pedido\s+de)',  # defiro/indefiro mas não "o pedido de"
        r'julgo\s+extinto',
        r'determino\s+a\s+(cita[cç][aã]o|intima[cç][aã]o)',
        r'vistos?\.?\s+(indefiro|defiro|determino)'
    ]
    decisao_count = sum(1 for p in decisao_patterns if re.search(p, text_lower[:2000]))
    if decisao_count >= 1:
        return DocumentType.DECISAO_INTERLOCUTORIA, 0.85
    
    # 5. Notificação/Intimação
    notificacao_patterns = [
        r'intima[cç][aã]o',
        r'notifica[cç][aã]o',
        r'fica\s+(v\.?\s*s\.?a?\.?|vossa\s+excel[eê]ncia|a\s+parte)\s+intimad[ao]',
        r'prazo\s+de\s+\d+\s+dias',
        r'cientificad[ao]'
    ]
    notif_count = sum(1 for p in notificacao_patterns if re.search(p, text_lower[:1500]))
    if notif_count >= 2:
        return DocumentType.NOTIFICACAO, 0.85
    
    # 6. Manifestação/Contestação
    manifestacao_patterns = [
        r'contesta[cç][aã]o',
        r'impugna[cç][aã]o',
        r'resposta\s+[aà]\s+(inicial|peti[cç][aã]o)',
        r'defesa\s+(pr[ée]via)?',
        r'vem\s+.*\s+apresentar\s+(contesta[cç][aã]o|impugna[cç][aã]o)'
    ]
    if any(re.search(pattern, text_lower[:1500]) for pattern in manifestacao_patterns):
        return DocumentType.MANIFESTACAO, 0.8
    
    # 7. Petição Inicial - menos específica, verificar por último
    peticao_patterns = [
        r'peti[cç][aã]o\s+inicial',
        r'exmo\.?\s+sr\.?\s+dr\.?\s+juiz',
        r'vem\s+.*\s+perante\s+v\.?\s*s\.?a?\.?',
        r'da\s+causa\s+de\s+pedir',
        r'dos\s+fatos',
        r'requer\s+a\s+cita[cç][aã]o',
        # Padrões específicos por área do direito
        # Trabalhista
        r'reclama[cç][aã]o\s+trabalhista',
        r'reclamante\s*:',
        r'reclamad[ao]\s*:',
        # Cível
        r'a[cç][aã]o\s+(de\s+)?(cobran[cç]a|indeniza[cç][aã]o|rescis[aã]o|despejo)',
        r'autor(?:a)?\s*:.*r[ée]u',
        # Execução
        r'execu[cç][aã]o\s+(fiscal|de\s+t[ií]tulo)',
        r'exequente\s*:',
        r'executad[ao]\s*:',
        # Criminal
        r'den[uú]ncia\s+(criminal)?',
        r'minist[ée]rio\s+p[uú]blico\s*:',
        r'acusad[ao]\s*:',
    ]
    peticao_count = sum(1 for p in peticao_patterns if re.search(p, text_lower[:2000]))
    
    # Se tem padrões de petição E não tem padrões fortes de outros tipos
    if peticao_count >= 2:
        return DocumentType.PETICAO_INICIAL, 0.75
    
    # 8. Fallback - se não classificou, é OUTROS
    return DocumentType.OUTROS, 0.5


def get_extractors_for_type(doc_type: DocumentType) -> list:
    """
    Retorna lista de extractors que devem ser executados para este tipo de documento.
    """
    return DOCUMENT_TYPE_EXTRACTORS.get(doc_type, DOCUMENT_TYPE_EXTRACTORS[DocumentType.OUTROS])
