# extractors/reextract.py
"""
Módulo de Re-extração OCR Seletiva para campos críticos vazios.

PLANO BATMAN v2 - OCR como fallback CIRÚRGICO:
- Só aciona OCR quando Regex+LLM falharam
- Processa APENAS páginas relevantes (TRCT, contracheques)
- Prioriza: salário > PIS > CTPS (ordem de importância)
- Tempo esperado: 5-15s por campo (vs minutos do OCR completo)
"""

import logging
from typing import Dict, Any, Optional, List
from pathlib import Path

logger = logging.getLogger(__name__)

CRITICAL_LABOR_FIELDS = ['salario', 'pis', 'ctps']


def get_missing_critical_fields(data: Dict[str, Any]) -> List[str]:
    """
    Identifica campos críticos que estão vazios/None.
    
    Returns:
        Lista de nomes de campos faltantes
    """
    missing = []
    for field in CRITICAL_LABOR_FIELDS:
        val = data.get(field, "")
        if not val or str(val).strip() == "":
            missing.append(field)
    return missing


def reextract_field_with_ocr(
    pdf_path: str,
    field: str,
    existing_data: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """
    Re-extrai UM campo específico via OCR cirúrgico.
    
    Estratégia por campo:
    - salario: Páginas de TRCT/contracheque (salary_candidates)
    - pis: Páginas de CTPS/ficha de registro
    - ctps: Páginas de CTPS/ficha de registro
    
    Args:
        pdf_path: Caminho do PDF
        field: Nome do campo a extrair ('salario', 'pis', 'ctps')
        existing_data: Dados já extraídos (para evitar re-trabalho)
    
    Returns:
        Valor extraído ou None
    """
    from .ocr_utils import (
        map_pdf_annexes, 
        extract_ocr_from_specific_pages,
        extract_salario_from_contracheque_ocr
    )
    from .regex_utils import extract_salario, extract_pis, extract_ctps
    
    if not pdf_path or not Path(pdf_path).exists():
        logger.warning(f"[REEXTRACT] PDF não encontrado: {pdf_path}")
        return None
    
    logger.info(f"[REEXTRACT] Iniciando OCR seletivo para '{field}': {pdf_path}")
    
    try:
        mapping = map_pdf_annexes(pdf_path)
        scanned_pages = set(mapping.get('scanned', []))
        
        if not scanned_pages:
            logger.debug(f"[REEXTRACT] Nenhuma página escaneada - {field} provavelmente não está em imagem")
            return None
        
        if field == 'salario':
            pages_priority = []
            
            trct = [p for p in mapping.get('trct', []) if p in scanned_pages]
            contracheque = [p for p in mapping.get('contracheque', []) if p in scanned_pages]
            candidates = mapping.get('salary_candidates', [])
            
            pages_priority.extend(trct[:3])
            pages_priority.extend(contracheque[:3])
            pages_priority.extend([p for p in candidates if p not in pages_priority][:4])
            
            if not pages_priority:
                logger.info(f"[REEXTRACT] Nenhuma página candidata para salário")
                return None
            
            pages_to_ocr = list(dict.fromkeys(pages_priority))[:8]
            logger.info(f"[REEXTRACT_SALARIO] Processando páginas: {pages_to_ocr}")
            
            texto_ocr = extract_ocr_from_specific_pages(pdf_path, pages_to_ocr, max_pages=8)
            
            if not texto_ocr:
                return None
            
            salario = extract_salario_from_contracheque_ocr(texto_ocr)
            if not salario:
                salario = extract_salario(texto_ocr)
            
            if salario:
                logger.info(f"[REEXTRACT_SALARIO] ✅ Encontrado: {salario}")
            return salario
            
        elif field in ('pis', 'ctps'):
            pages_priority = []
            
            ctps_pages = [p for p in mapping.get('ctps', []) if p in scanned_pages]
            ficha = [p for p in mapping.get('ficha_registro', []) if p in scanned_pages]
            trct = [p for p in mapping.get('trct', []) if p in scanned_pages]
            
            pages_priority.extend(ctps_pages[:3])
            pages_priority.extend(ficha[:2])
            pages_priority.extend(trct[:2])
            
            if not pages_priority:
                all_scanned = list(scanned_pages)
                if len(all_scanned) >= 3:
                    pages_priority = all_scanned[:5]
                else:
                    logger.info(f"[REEXTRACT] Nenhuma página candidata para {field}")
                    return None
            
            pages_to_ocr = list(dict.fromkeys(pages_priority))[:6]
            logger.info(f"[REEXTRACT_{field.upper()}] Processando páginas: {pages_to_ocr}")
            
            texto_ocr = extract_ocr_from_specific_pages(pdf_path, pages_to_ocr, max_pages=6)
            
            if not texto_ocr:
                return None
            
            if field == 'pis':
                result = extract_pis(texto_ocr)
            else:
                result = extract_ctps(texto_ocr)
            
            if result:
                logger.info(f"[REEXTRACT_{field.upper()}] ✅ Encontrado: {result}")
            return result
        
        else:
            logger.warning(f"[REEXTRACT] Campo não suportado: {field}")
            return None
            
    except Exception as e:
        logger.error(f"[REEXTRACT] Erro ao processar {field}: {e}")
        return None


def reextract_missing_fields(
    process_id: int,
    pdf_path: str,
    existing_data: Optional[Dict[str, Any]] = None,
    fields_to_extract: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Re-extrai campos críticos faltantes de um processo via OCR seletivo.
    
    IMPORTANTE: Esta função é para uso MANUAL ou em lote separado.
    NÃO é chamada automaticamente durante a extração inicial (para manter velocidade).
    
    Args:
        process_id: ID do processo no banco
        pdf_path: Caminho do PDF original
        existing_data: Dados já extraídos (para identificar o que falta)
        fields_to_extract: Lista específica de campos ou None para detectar automaticamente
    
    Returns:
        Dict com campos extraídos via OCR
    """
    existing = existing_data or {}
    
    if fields_to_extract:
        missing = [f for f in fields_to_extract if f in CRITICAL_LABOR_FIELDS]
    else:
        missing = get_missing_critical_fields(existing)
    
    if not missing:
        logger.info(f"[REEXTRACT] Processo {process_id}: todos campos críticos preenchidos")
        return {}
    
    logger.info(f"[REEXTRACT] Processo {process_id}: campos faltantes = {missing}")
    
    extracted = {}
    
    priority_order = ['salario', 'pis', 'ctps']
    for field in priority_order:
        if field in missing:
            value = reextract_field_with_ocr(pdf_path, field, existing)
            if value:
                extracted[field] = value
    
    if extracted:
        logger.info(f"[REEXTRACT] Processo {process_id}: recuperados {len(extracted)} campos via OCR")
    else:
        logger.info(f"[REEXTRACT] Processo {process_id}: nenhum campo recuperado via OCR")
    
    return extracted


def find_pdf_path(process, upload_folder: str) -> Optional[str]:
    """
    Encontra o caminho do PDF de um processo.
    
    Estratégia de busca (ordem de prioridade):
    1. pdf_filename do Process se existir
    2. Buscar via BatchItem -> batch_id -> uploads/batch/<batch_id>/
    3. Buscar na pasta uploads/ por padrão do filename
    4. Glob patterns como fallback
    
    Returns:
        Caminho absoluto do PDF ou None
    """
    base_path = Path(upload_folder)
    
    if hasattr(process, 'pdf_filename') and process.pdf_filename:
        pdf_path = base_path / process.pdf_filename
        if pdf_path.exists():
            return str(pdf_path)
        
        for subdir in ['', 'batch']:
            subpath = base_path / subdir
            if subpath.exists():
                for batch_dir in subpath.iterdir():
                    if batch_dir.is_dir():
                        candidate = batch_dir / process.pdf_filename
                        if candidate.exists():
                            return str(candidate)
    
    try:
        from models import BatchItem
        batch_item = BatchItem.query.filter_by(process_id=process.id).first()
        if batch_item:
            batch_folder = base_path / 'batch' / str(batch_item.batch_id)
            if batch_folder.exists():
                for pdf_file in batch_folder.glob('*.pdf'):
                    if process.pdf_filename and process.pdf_filename in pdf_file.name:
                        return str(pdf_file)
                    pdfs = list(batch_folder.glob('*.pdf'))
                    if pdfs:
                        for pdf in pdfs:
                            if process.numero_processo and any(
                                part in pdf.name for part in process.numero_processo.replace('.', '-').replace('/', '-').split('-')[:3]
                            ):
                                return str(pdf)
    except Exception as e:
        logger.debug(f"[FIND_PDF] Erro ao buscar via BatchItem: {e}")
    
    patterns = []
    if hasattr(process, 'pdf_filename') and process.pdf_filename:
        patterns.append(f"**/{process.pdf_filename}")
    if process.numero_processo:
        cnj_parts = process.numero_processo.replace('.', '*').replace('-', '*').replace('/', '*')
        patterns.append(f"**/*{cnj_parts[:20]}*.pdf")
    
    for pattern in patterns:
        matches = list(base_path.glob(pattern))
        if matches:
            return str(matches[0])
    
    return None


def batch_reextract_missing(
    session,
    ProcessModel,
    upload_folder: str,
    limit: int = 50,
    fields: Optional[List[str]] = None,
    user_id: Optional[int] = None
) -> Dict[str, Any]:
    """
    Re-extração em lote de processos com campos críticos vazios.
    
    FLUXO:
    1. Busca processos com campos vazios (salário, PIS, CTPS) do usuário
    2. Para cada um, localiza o PDF via BatchItem ou padrões
    3. Aplica OCR seletivo nas páginas relevantes
    4. Atualiza o banco de dados
    
    Args:
        session: Sessão SQLAlchemy
        ProcessModel: Classe do modelo Process
        upload_folder: Pasta onde os PDFs estão armazenados
        limit: Máximo de processos a processar
        fields: Campos específicos ou None para todos críticos
        user_id: ID do usuário para escopo (obrigatório para segurança)
    
    Returns:
        Estatísticas do processamento
    """
    from sqlalchemy import or_, and_
    
    target_fields = fields or CRITICAL_LABOR_FIELDS
    
    conditions = []
    for field in target_fields:
        col = getattr(ProcessModel, field, None)
        if col is not None:
            conditions.append(or_(col.is_(None), col == ""))
    
    if not conditions:
        return {"error": "Nenhum campo crítico encontrado no modelo"}
    
    query = session.query(ProcessModel).filter(
        or_(*conditions)
    )
    
    if user_id:
        query = query.filter(ProcessModel.user_id == user_id)
    
    query = query.order_by(ProcessModel.id.desc()).limit(limit)
    
    processes = query.all()
    
    stats = {
        'total_processados': 0,
        'campos_recuperados': 0,
        'por_campo': {f: {'tentativas': 0, 'sucesso': 0} for f in target_fields},
        'erros': 0,
        'pdfs_nao_encontrados': 0,
        'processos_atualizados': []
    }
    
    for proc in processes:
        try:
            pdf_path = find_pdf_path(proc, upload_folder)
            
            if not pdf_path or not Path(pdf_path).exists():
                stats['pdfs_nao_encontrados'] += 1
                logger.debug(f"[BATCH_REEXTRACT] PDF não encontrado para processo {proc.id}")
                continue
            
            existing_data = {f: getattr(proc, f, "") for f in target_fields}
            missing = get_missing_critical_fields(existing_data)
            
            if not missing:
                continue
            
            stats['total_processados'] += 1
            
            extracted = reextract_missing_fields(
                process_id=proc.id,
                pdf_path=str(pdf_path),
                existing_data=existing_data,
                fields_to_extract=missing
            )
            
            if extracted:
                for field, value in extracted.items():
                    setattr(proc, field, value)
                    stats['campos_recuperados'] += 1
                    stats['por_campo'][field]['sucesso'] += 1
                
                session.commit()
                stats['processos_atualizados'].append(proc.id)
            
            for field in missing:
                stats['por_campo'][field]['tentativas'] += 1
                
        except Exception as e:
            logger.error(f"[BATCH_REEXTRACT] Erro no processo {proc.id}: {e}")
            stats['erros'] += 1
            session.rollback()
    
    logger.info(f"[BATCH_REEXTRACT] Concluído: {stats['total_processados']} processados, "
                f"{stats['campos_recuperados']} campos recuperados, "
                f"{stats['pdfs_nao_encontrados']} PDFs não encontrados, "
                f"{stats['erros']} erros")
    
    return stats
