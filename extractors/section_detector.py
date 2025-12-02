import re
from typing import List, Dict, Tuple
import logging

logger = logging.getLogger(__name__)

class DocumentSection:
    """Representa uma seção de documento dentro de um PDF"""
    def __init__(self, start_pos: int, end_pos: int, text: str, break_type: str):
        self.start_pos = start_pos
        self.end_pos = end_pos
        self.text = text
        self.break_type = break_type  # Tipo de quebra detectada
        
    def __repr__(self):
        return f"DocumentSection(start={self.start_pos}, end={self.end_pos}, type={self.break_type}, len={len(self.text)})"

def detect_document_sections(full_text: str) -> List[DocumentSection]:
    """
    Detecta múltiplas seções/documentos dentro de um único PDF.
    
    Returns:
        Lista de DocumentSection, cada uma representando um documento separado
    """
    if not full_text or len(full_text.strip()) < 50:
        return [DocumentSection(0, len(full_text), full_text, "documento_unico")]
    
    # Padrões que indicam INÍCIO de novo documento
    section_break_patterns = [
        # Petição Inicial
        (r'\n\s*EXMO\.?\s+SR\.?\s+DR\.?\s+JUIZ', 'peticao_inicial'),
        (r'\n\s*PETIÇÃO\s+INICIAL', 'peticao_inicial'),
        
        # Notificação/Intimação
        (r'\n\s*INTIMAÇÃO', 'notificacao'),
        (r'\n\s*NOTIFICAÇÃO', 'notificacao'),
        (r'\n\s*FICA\s+.*\s+INTIMAD[OA]', 'notificacao'),
        
        # Decisão/Despacho
        (r'\n\s*DECISÃO\s*(INTERLOCUTÓRIA)?', 'decisao'),
        (r'\n\s*DESPACHO', 'decisao'),
        (r'\n\s*VISTOS?\.?\s*\n', 'decisao'),
        
        # Sentença
        (r'\n\s*SENTENÇA', 'sentenca'),
        (r'\n\s*S\s*E\s*N\s*T\s*E\s*N\s*[ÇC]\s*A', 'sentenca'),  # Espaçado
        
        # Acórdão
        (r'\n\s*AC[OÓ]RD[ÃA]O', 'acordao'),
        (r'\n\s*TRIBUNAL\s+REGIONAL', 'acordao'),
        
        # Ata de Audiência
        (r'\n\s*ATA\s+DE\s+AUDI[ÊE]NCIA', 'ata_audiencia'),
        (r'\n\s*TERMO\s+DE\s+AUDI[ÊE]NCIA', 'ata_audiencia'),
        
        # Manifestação/Contestação
        (r'\n\s*CONTESTAÇÃO', 'manifestacao'),
        (r'\n\s*IMPUGNAÇÃO', 'manifestacao'),
        (r'\n\s*RESPOSTA\s+[AÀ]', 'manifestacao'),
        
        # Outros marcadores de novo documento
        (r'\n\s*PROCESSO\s+N[º°]', 'novo_processo'),
        (r'\n\s*--- Página \d+ ---\s*\n\s*EXMO', 'nova_pagina_documento'),
    ]
    
    # Encontrar todas as quebras
    breaks = []
    for pattern, break_type in section_break_patterns:
        for match in re.finditer(pattern, full_text, re.IGNORECASE):
            breaks.append({
                'position': match.start(),
                'type': break_type,
                'match': match.group()
            })
    
    # Ordenar quebras por posição
    breaks.sort(key=lambda x: x['position'])
    
    # Se não encontrou quebras, retornar documento único
    if not breaks:
        logger.info("[SEÇÕES] Nenhuma quebra detectada - documento único")
        return [DocumentSection(0, len(full_text), full_text, "documento_unico")]
    
    # Remover quebras muito próximas (dentro de 200 caracteres)
    # Isso evita detectar múltiplas quebras para o mesmo documento
    filtered_breaks = []
    last_pos = -1000
    for brk in breaks:
        if brk['position'] - last_pos > 200:
            filtered_breaks.append(brk)
            last_pos = brk['position']
    
    breaks = filtered_breaks
    
    # SEMPRE adicionar seção inicial em 0 se houver quebras E primeira quebra não está em 0
    # Isso garante que headers/metadata no início não sejam perdidos
    if breaks and breaks[0]['position'] > 0:
        breaks.insert(0, {
            'position': 0,
            'type': 'inicio_documento',
            'match': ''
        })
    
    # Criar seções baseadas nas quebras
    sections = []
    for i in range(len(breaks)):
        start_pos = breaks[i]['position']
        end_pos = breaks[i + 1]['position'] if i + 1 < len(breaks) else len(full_text)
        section_text = full_text[start_pos:end_pos].strip()
        
        # SEMPRE preservar primeira seção (pode conter headers importantes)
        # Para outras seções, exigir conteúdo significativo (>100 chars)
        if start_pos == 0 or len(section_text) > 100:
            section = DocumentSection(
                start_pos=start_pos,
                end_pos=end_pos,
                text=section_text,
                break_type=breaks[i]['type']
            )
            sections.append(section)
            logger.info(f"[SEÇÕES] Seção {i+1}: {breaks[i]['type']} ({len(section_text)} chars)")
    
    # Se não criou seções válidas, retornar documento único
    if not sections:
        logger.info("[SEÇÕES] Nenhuma seção válida - retornando documento único")
        return [DocumentSection(0, len(full_text), full_text, "documento_unico")]
    
    logger.info(f"[SEÇÕES] Total de {len(sections)} seções detectadas")
    return sections

def merge_section_results(section_results: List[Dict]) -> Dict:
    """
    Mescla resultados de múltiplas seções de forma inteligente.
    
    Estratégia de mesclagem:
    1. Campos básicos (CNJ, número processo): usa o primeiro encontrado
    2. Campos de localização (vara, comarca): usa o mais completo
    3. Campos de partes: combina todos (autor, réu)
    4. Campos de eventos: mantém o mais recente (audiência, decisão)
    5. Prazos: mantém o mais próximo/urgente
    """
    if not section_results:
        return {}
    
    if len(section_results) == 1:
        return section_results[0]
    
    logger.info(f"[MESCLAGEM] Mesclando {len(section_results)} seções")
    
    merged = {}
    
    # 1. Campos básicos - primeiro valor encontrado
    basic_fields = ['numero_processo', 'cnj', 'tipo_processo', 'sistema_eletronico', 
                    'area_direito', 'sub_area_direito', 'npc', 'instancia']
    for field in basic_fields:
        for result in section_results:
            if result.get(field) and str(result[field]).strip():
                merged[field] = result[field]
                break
    
    # 2. Campos de localização - valor mais completo (maior)
    location_fields = ['estado', 'comarca', 'foro', 'vara', 'celula', 'origem', 'orgao', 'numero_orgao']
    for field in location_fields:
        values = [r.get(field, '') for r in section_results if r.get(field) and str(r[field]).strip()]
        if values:
            # Pega o valor mais longo (geralmente mais completo)
            merged[field] = max(values, key=len)
    
    # 3. Campos de partes - combinar (pode haver múltiplas partes)
    party_fields = ['cliente', 'parte', 'cliente_parte', 'autor', 'reu', 'advogado_autor', 'advogado_reu']
    for field in party_fields:
        values = [r.get(field, '') for r in section_results if r.get(field) and str(r[field]).strip()]
        if values:
            # Remove duplicatas mantendo ordem
            unique_values = []
            seen = set()
            for v in values:
                v_lower = str(v).lower().strip()
                if v_lower not in seen:
                    unique_values.append(v)
                    seen.add(v_lower)
            # Se múltiplos valores, combina com vírgula
            merged[field] = unique_values[0] if len(unique_values) == 1 else ', '.join(unique_values[:3])
    
    # 4. Campos de eventos - último/mais recente (ordem das seções)
    event_fields = ['decisao_tipo', 'decisao_resultado', 'decisao_fundamentacao_resumida',
                    'audiencia_inicial', 'resultado_audiencia', 'prazos_derivados_audiencia']
    for field in event_fields:
        # Percorre de trás pra frente (último tem prioridade)
        for result in reversed(section_results):
            if result.get(field) and str(result[field]).strip():
                merged[field] = result[field]
                break
    
    # 5. Prazos - mantém o mais urgente (menor data futura)
    if any(r.get('prazo') for r in section_results):
        prazos = [r.get('prazo') for r in section_results if r.get('prazo')]
        if prazos:
            # Por simplicidade, pega o primeiro prazo encontrado
            # Em produção, deveria comparar datas e pegar o mais próximo
            merged['prazo'] = prazos[0]
    
    # 6. Tipo de notificação
    for result in section_results:
        if result.get('tipo_notificacao') and str(result['tipo_notificacao']).strip():
            merged['tipo_notificacao'] = result['tipo_notificacao']
            break
    
    # 7. Assunto/Objeto - valor mais completo
    for field in ['assunto', 'objeto', 'sub_objeto']:
        values = [r.get(field, '') for r in section_results if r.get(field) and str(r[field]).strip()]
        if values:
            merged[field] = max(values, key=len)
    
    # 8. Informações de classificação - última seção
    if section_results[-1].get('document_type'):
        merged['document_type'] = section_results[-1]['document_type']
        merged['document_type_confidence'] = section_results[-1].get('document_type_confidence', 0)
        # Adicionar flag indicando que é multi-documento
        merged['multi_document'] = True
        merged['num_sections'] = len(section_results)
    
    logger.info(f"[MESCLAGEM] Resultado mesclado: {list(merged.keys())}")
    return merged
