# extractors/ocr_utils.py
"""
Módulo OCR para extração de PDFs escaneados usando Tesseract.
Aplica OCR seletivo apenas quando texto nativo é insuficiente.

2025-12-05: Sistema de Fila OCR Assíncrona
- Se não conseguir slot OCR imediato, adiciona à fila e retorna
- Worker de background processa a fila quando slots liberarem
- Atualiza os processos no banco quando OCR completar
- Isso acelera a extração geral pois outros processos não ficam esperando
"""
import logging
import os
import threading
import queue
import re
from typing import Optional, Dict, List, Tuple, Callable, Any
from pdf2image import convert_from_path
import pytesseract
from PIL import Image

# Semáforo global para limitar OCRs simultâneos (evita sobrecarga do sistema)
# 2025-12-05: Reduzido de ilimitado para 2 simultâneos após travamentos com 5 workers
OCR_SEMAPHORE = threading.Semaphore(2)
# 2025-12-05: Timeout aumentado de 60s para 120s - páginas complexas levam 25-30s para OCR
OCR_TIMEOUT_SECONDS = 120  # Timeout por página para evitar travamento

# ============================================================================
# SISTEMA DE FILA OCR ASSÍNCRONA
# ============================================================================
# Estrutura da tarefa: (process_id, pdf_path, doc_pages, missing_fields)
OCR_QUEUE: queue.Queue = queue.Queue()
OCR_WORKER_RUNNING = False
OCR_WORKER_THREAD: Optional[threading.Thread] = None
OCR_WORKER_LOCK = threading.Lock()

def _preprocess_image_for_ocr(img: Image.Image, doc_type: str = "generic") -> Image.Image:
    """
    Pré-processa imagem para melhorar velocidade e qualidade do OCR.
    
    2025-12-05: Otimizações baseadas em boas práticas:
    - Binarização (threshold) para remover ruídos
    - Redimensionamento se muito grande
    - Ajuste de contraste
    
    Args:
        img: Imagem PIL em modo L (grayscale)
        doc_type: Tipo de documento (trct, ctps, contracheque)
    
    Returns:
        Imagem pré-processada
    """
    # 1. Garantir grayscale
    if img.mode != 'L':
        img = img.convert('L')
    
    # 2. Redimensionar se muito grande (economiza tempo de OCR)
    max_width = 1200  # Reduzido de ~1700 (150 DPI em A4)
    if img.width > max_width:
        ratio = max_width / img.width
        new_size = (int(img.width * ratio), int(img.height * ratio))
        img = img.resize(new_size, Image.Resampling.LANCZOS)
    
    # 3. Binarização adaptativa usando threshold
    # Converte para preto/branco puro, remove ruídos de fundo
    threshold = 180  # Valor para documentos escaneados típicos
    img = img.point(lambda x: 255 if x > threshold else 0, mode='1')
    
    # Converter de volta para L para Tesseract
    img = img.convert('L')
    
    return img


def _get_psm_for_doc_type(doc_type: str) -> str:
    """
    Retorna o PSM (Page Segmentation Mode) ideal para cada tipo de documento.
    
    PSM options:
    - 3: Fully automatic page segmentation (lento, preciso)
    - 4: Single column of text
    - 6: Single uniform block of text (default)
    - 11: Sparse text (bom para formulários como TRCT)
    
    Args:
        doc_type: Tipo de documento
    
    Returns:
        String de configuração PSM
    """
    psm_map = {
        "trct": "--psm 11",       # TRCT é formulário com texto esparso
        "contracheque": "--psm 6", # Contracheque é bloco uniforme
        "ctps": "--psm 4",         # CTPS é coluna única
        "generic": "--psm 6"       # Padrão
    }
    return psm_map.get(doc_type, "--psm 6")


def _process_ocr_task(process_id: int, pdf_path: str, doc_pages: Dict[str, int], 
                      missing_fields: List[str]) -> Dict[str, str]:
    """
    Processa uma tarefa OCR da fila.
    Extrai campos de documentos escaneados.
    
    2025-12-05: Otimizações de velocidade:
    - DPI reduzido de 150 para 100 (suficiente para texto)
    - Pré-processamento com binarização
    - PSM otimizado por tipo de documento
    """
    logger = logging.getLogger(__name__)
    result = {}
    campos_faltantes = set(missing_fields)
    
    # Ordenar páginas por prioridade: Contracheque > TRCT > CTPS
    ordered_pages = []
    if doc_pages.get("contracheque"):
        ordered_pages.append(("contracheque", doc_pages["contracheque"]))
    if doc_pages.get("trct"):
        ordered_pages.append(("trct", doc_pages["trct"]))
    if doc_pages.get("ctps"):
        ordered_pages.append(("ctps", doc_pages["ctps"]))
    
    for doc_type, page_num in ordered_pages:
        if not campos_faltantes:
            break
        
        # Aguarda slot (bloqueante, pois estamos no worker de background)
        OCR_SEMAPHORE.acquire()
        texto_pagina = ""
        try:
            logger.info(f"[OCR-QUEUE] 📷 Proc {process_id}: {doc_type.upper()} (página {page_num})...")
            
            # ✅ DPI reduzido de 150 para 100 (mais rápido, suficiente para texto)
            images = convert_from_path(
                pdf_path,
                dpi=100,  # Reduzido de 150
                first_page=page_num,
                last_page=page_num,
                poppler_path=POPPLER_PATH,
                timeout=OCR_TIMEOUT_SECONDS
            )
            
            if images:
                img = images[0]
                
                # ✅ Pré-processamento para velocidade
                img_processed = _preprocess_image_for_ocr(img, doc_type)
                
                # ✅ PSM otimizado por tipo de documento
                psm = _get_psm_for_doc_type(doc_type)
                config = f'{psm} -l por'
                
                texto_pagina = pytesseract.image_to_string(
                    img_processed, 
                    config=config,
                    timeout=OCR_TIMEOUT_SECONDS
                )
        except Exception as e:
            logger.warning(f"[OCR-QUEUE] Erro proc {process_id} página {page_num}: {e}")
        finally:
            OCR_SEMAPHORE.release()
        
        if not texto_pagina:
            logger.warning(f"[OCR-QUEUE] ⚠️ Proc {process_id}: {doc_type.upper()} página {page_num} - sem texto extraído!")
            continue
        
        # ✅ DEBUG: Mostrar primeiros 500 chars do texto para diagnóstico
        logger.debug(f"[OCR-QUEUE] Proc {process_id} {doc_type.upper()}: {len(texto_pagina)} chars")
        preview = texto_pagina[:500].replace('\n', ' ')
        logger.debug(f"[OCR-QUEUE] Preview: {preview}")
        
        # Extrair campos
        if "salario" in campos_faltantes:
            salario_patterns = [
                r'(?:sal[aá]rio\s*(?:base|contratual|mensal)?|remunera[çc][ãa]o(?:\s*mensal)?)[:\s]*R?\$?\s*([\d]{1,3}(?:[.,]\d{3})*[,\.]\d{2})',
                r'(?:maior\s*remunera[çc][ãa]o|base\s*de\s*c[aá]lculo)[:\s]*R?\$?\s*([\d]{1,3}(?:[.,]\d{3})*[,\.]\d{2})',
            ]
            for pattern in salario_patterns:
                m = re.search(pattern, texto_pagina, re.IGNORECASE)
                if m:
                    val_str = m.group(1).replace('.', '').replace(',', '.')
                    try:
                        val = float(val_str)
                        if 1000 <= val <= 100000:
                            result["salario"] = f"R$ {m.group(1)}"
                            campos_faltantes.discard("salario")
                            logger.info(f"[OCR-QUEUE] ✅ Proc {process_id} Salário: {result['salario']}")
                            break
                    except:
                        pass
        
        if "data_admissao" in campos_faltantes:
            # Padrões tradicionais
            admissao_patterns = [
                r'(?:data\s*(?:de\s*)?admiss[ãa]o|admitido\s*em)[:\s]*(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})',
                # CTPS Digital: "Contratos de trabalho 15/04/2024 - 05/06/2025" (primeira data)
                r'[Cc]ontratos?\s*(?:de\s*)?trabalho\s*(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})\s*[-–]\s*\d',
            ]
            for pattern in admissao_patterns:
                m = re.search(pattern, texto_pagina, re.IGNORECASE)
                if m:
                    result["data_admissao"] = m.group(1)
                    campos_faltantes.discard("data_admissao")
                    logger.info(f"[OCR-QUEUE] ✅ Proc {process_id} Data Admissão: {result['data_admissao']}")
                    break
        
        if "data_demissao" in campos_faltantes:
            # Padrões tradicionais + CTPS Digital
            demissao_patterns = [
                r'(?:data\s*(?:de\s*)?(?:demiss[ãa]o|desligamento|rescis[ãa]o|afastamento))[:\s]*(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})',
                # CTPS Digital: "Contratos de trabalho 15/04/2024 - 05/06/2025" (segunda data)
                r'[Cc]ontratos?\s*(?:de\s*)?trabalho\s*\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\s*[-–]\s*(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})',
                # "projeção do aviso prévio indenizado 08/07/2025"
                r'(?:proje[çc][ãa]o\s*(?:do\s*)?aviso\s*pr[ée]vio)[:\s]*(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})',
            ]
            for pattern in demissao_patterns:
                m = re.search(pattern, texto_pagina, re.IGNORECASE)
                if m:
                    result["data_demissao"] = m.group(1)
                    campos_faltantes.discard("data_demissao")
                    logger.info(f"[OCR-QUEUE] ✅ Proc {process_id} Data Demissão: {result['data_demissao']}")
                    break
        
        if "pis" in campos_faltantes:
            m = re.search(r'(?:PIS|PASEP|NIT)[:\s/]*(\d{3}[.\s]?\d{5}[.\s]?\d{2}[.\s-]?\d)', texto_pagina, re.IGNORECASE)
            if m:
                pis_raw = re.sub(r'[^\d]', '', m.group(1))
                if len(pis_raw) == 11:
                    result["pis"] = f"{pis_raw[:3]}.{pis_raw[3:8]}.{pis_raw[8:10]}-{pis_raw[10]}"
                    campos_faltantes.discard("pis")
                    logger.info(f"[OCR-QUEUE] ✅ Proc {process_id} PIS: {result['pis']}")
        
        if "ctps" in campos_faltantes:
            m = re.search(r'(?:CTPS|Carteira)[:\s]*[nN]?[º°]?\s*(\d{5,7})', texto_pagina, re.IGNORECASE)
            if m:
                result["ctps"] = m.group(1)
                campos_faltantes.discard("ctps")
                logger.info(f"[OCR-QUEUE] ✅ Proc {process_id} CTPS: {result['ctps']}")
        
        if "serie_ctps" in campos_faltantes:
            m = re.search(r'[sS][eéE][rR][iI][eE][:\s]*(\d{3,5})', texto_pagina)
            if m:
                result["serie_ctps"] = m.group(1)
                campos_faltantes.discard("serie_ctps")
                logger.info(f"[OCR-QUEUE] ✅ Proc {process_id} Série CTPS: {result['serie_ctps']}")
    
    # ✅ Log final do resultado
    if result:
        logger.info(f"[OCR-QUEUE] 🎯 Proc {process_id}: Extraído {list(result.keys())}")
    else:
        logger.warning(f"[OCR-QUEUE] ⚠️ Proc {process_id}: NENHUM campo extraído das páginas {list(doc_pages.values())}")
        logger.warning(f"[OCR-QUEUE] ⚠️ Campos faltantes ainda: {list(campos_faltantes)}")
    
    return result


def _update_process_with_ocr_results(process_id: int, ocr_data: Dict[str, str]):
    """
    Atualiza o processo no banco com os dados extraídos via OCR.
    """
    if not ocr_data:
        return
    
    logger = logging.getLogger(__name__)
    
    try:
        from main import app
        from extensions import db
        from models import Process
        
        with app.app_context():
            process = db.session.get(Process, process_id)
            if not process:
                logger.warning(f"[OCR-QUEUE] Processo {process_id} não encontrado no banco")
                return
            
            updated = []
            for field, value in ocr_data.items():
                current = getattr(process, field, None)
                if not current and value:
                    setattr(process, field, value)
                    updated.append(field)
            
            if updated:
                db.session.commit()
                logger.info(f"[OCR-QUEUE] ✅ Processo {process_id} atualizado: {updated}")
            
    except Exception as e:
        logger.error(f"[OCR-QUEUE] ❌ Erro ao atualizar processo {process_id}: {e}")


def _ocr_queue_worker():
    """
    Worker de background que processa a fila de OCR.
    Roda em thread separada para não bloquear a extração principal.
    """
    global OCR_WORKER_RUNNING
    logger = logging.getLogger(__name__)
    logger.info("[OCR-QUEUE] 🚀 Worker iniciado")
    
    while OCR_WORKER_RUNNING:
        try:
            # Aguarda tarefa com timeout para poder verificar OCR_WORKER_RUNNING
            task = OCR_QUEUE.get(timeout=2.0)
            
            process_id, pdf_path, doc_pages, missing_fields = task
            logger.info(f"[OCR-QUEUE] 📋 Processando tarefa: processo {process_id}, campos {missing_fields}")
            
            # Processa OCR
            ocr_data = _process_ocr_task(process_id, pdf_path, doc_pages, missing_fields)
            
            # Atualiza banco
            if ocr_data:
                _update_process_with_ocr_results(process_id, ocr_data)
            
            OCR_QUEUE.task_done()
            
        except queue.Empty:
            continue
        except Exception as e:
            logger.error(f"[OCR-QUEUE] ❌ Erro no worker: {e}")
    
    logger.info("[OCR-QUEUE] 🛑 Worker encerrado")


def start_ocr_queue_worker():
    """
    Inicia o worker de processamento da fila OCR.
    """
    global OCR_WORKER_RUNNING, OCR_WORKER_THREAD
    
    with OCR_WORKER_LOCK:
        if OCR_WORKER_RUNNING:
            return
        
        OCR_WORKER_RUNNING = True
        OCR_WORKER_THREAD = threading.Thread(target=_ocr_queue_worker, daemon=True)
        OCR_WORKER_THREAD.start()
        logging.getLogger(__name__).info("[OCR-QUEUE] 🟢 Worker de fila OCR iniciado")


def stop_ocr_queue_worker():
    """
    Para o worker de processamento da fila OCR.
    """
    global OCR_WORKER_RUNNING, OCR_WORKER_THREAD
    
    with OCR_WORKER_LOCK:
        OCR_WORKER_RUNNING = False
        if OCR_WORKER_THREAD:
            OCR_WORKER_THREAD.join(timeout=5.0)
            OCR_WORKER_THREAD = None
        logging.getLogger(__name__).info("[OCR-QUEUE] 🔴 Worker de fila OCR encerrado")


def queue_ocr_task(process_id: int, pdf_path: str, doc_pages: Dict[str, int], 
                   missing_fields: List[str]) -> bool:
    """
    Adiciona uma tarefa de OCR à fila para processamento assíncrono.
    
    Returns:
        True se adicionou à fila, False se fila cheia
    """
    logger = logging.getLogger(__name__)
    
    # Garantir que worker está rodando
    start_ocr_queue_worker()
    
    try:
        OCR_QUEUE.put_nowait((process_id, pdf_path, doc_pages, missing_fields))
        logger.info(f"[OCR-QUEUE] 📥 Tarefa enfileirada: processo {process_id} ({OCR_QUEUE.qsize()} na fila)")
        return True
    except queue.Full:
        logger.warning(f"[OCR-QUEUE] ⚠️ Fila cheia, tarefa descartada: processo {process_id}")
        return False


def get_ocr_queue_status() -> Dict:
    """
    Retorna status da fila OCR.
    """
    return {
        "queue_size": OCR_QUEUE.qsize(),
        "worker_running": OCR_WORKER_RUNNING,
        "max_concurrent": 2
    }

# ============================================================================
# FIM DO SISTEMA DE FILA OCR
# ============================================================================

def _get_poppler_path() -> Optional[str]:
    """
    Encontra o caminho do poppler no ambiente Nix.
    O poppler_utils pode estar em diferentes locais dependendo da instalação.
    Retorna None se não estiver em ambiente Nix ou poppler não estiver instalado.
    """
    nix_store = "/nix/store"
    
    if not os.path.isdir(nix_store):
        return None
    
    possible_paths = [
        "/nix/store/ibb9lajxj2jr8z0bmriqyc43648b7fql-poppler-utils-25.05.0/bin",
    ]
    
    try:
        for nix_dir in os.listdir(nix_store):
            if "poppler" in nix_dir.lower() and "utils" in nix_dir.lower():
                path = f"{nix_store}/{nix_dir}/bin"
                if os.path.exists(path):
                    possible_paths.append(path)
    except (PermissionError, OSError) as e:
        logging.getLogger(__name__).warning(f"[OCR] Não foi possível listar {nix_store}: {e}")
    
    for path in possible_paths:
        pdftoppm = os.path.join(path, "pdftoppm")
        if os.path.exists(pdftoppm):
            return path
    
    return None

POPPLER_PATH = _get_poppler_path()
if POPPLER_PATH:
    logging.getLogger(__name__).info(f"[OCR] Poppler encontrado: {POPPLER_PATH}")
else:
    logging.getLogger(__name__).warning("[OCR] Poppler não encontrado - OCR usará configuração padrão do sistema")


ANNEX_KEYWORDS = {
    "trct": ["trct", "termo de rescisão", "termo rescisório", "rescisao contrato", "rescisão do contrato"],
    "contracheque": ["contracheque", "holerite", "recibo de pagamento", "demonstrativo de pagamento", "folha de pagamento"],
    "documentos": ["documentos", "anexos", "docs", "comprovantes"],
    "ctps": ["ctps", "carteira de trabalho", "carteira profissional"],
    "pis": ["pis", "pasep", "nit"],
    # 🆕 2025-12-05: Novos documentos com dados trabalhistas
    "termo_devolucao": ["termo de devolução", "termo devolução", "devolução de uniforme", "devolução uniforme", "epi"],
    "termo_quitacao": ["termo de quitação", "termo quitação", "quitação anual", "quitação de obrigações"],
    "ficha_registro": ["ficha de registro", "registro de empregado", "ficha cadastral"],
    "aso": ["aso", "atestado de saúde ocupacional", "exame admissional", "exame demissional"],
}


def extract_pdf_bookmarks(pdf_path: str) -> Dict[str, int]:
    """
    Extrai bookmarks/outlines do PDF e mapeia para números de página.
    
    2025-12-05: Nova função para extrair mapeamento EXATO de documentos PJe.
    Os PDFs do PJe têm bookmarks clicáveis que apontam diretamente para cada anexo.
    
    Args:
        pdf_path: Caminho do PDF
    
    Returns:
        Dict com {tipo_documento: página} ex: {"ctps": 19, "trct": 21, "contracheque": 29}
    """
    from PyPDF2 import PdfReader
    
    result = {}
    logger = logging.getLogger(__name__)
    
    try:
        reader = PdfReader(pdf_path)
        outlines = reader.outline if hasattr(reader, 'outline') else None
        
        if not outlines:
            logger.debug("[BOOKMARK] PDF não tem bookmarks")
            return result
        
        logger.info(f"[BOOKMARK] PDF tem {len(outlines)} bookmarks")
        
        for outline in outlines:
            try:
                title = outline.get('/Title', '')
                page_ref = outline.get('/Page')
                
                if not page_ref:
                    continue
                
                # Encontrar número da página
                page_num = None
                for i, page in enumerate(reader.pages, 1):
                    if page.indirect_reference == page_ref:
                        page_num = i
                        break
                
                if not page_num:
                    continue
                
                # Classificar tipo de documento
                title_lower = title.lower()
                
                if "ctps" in title_lower or "carteira de trabalho" in title_lower:
                    if "ctps" not in result:
                        result["ctps"] = page_num
                        logger.info(f"[BOOKMARK] ✅ CTPS → página {page_num}")
                
                elif "trct" in title_lower or ("rescis" in title_lower and "termo" in title_lower):
                    if "trct" not in result:
                        result["trct"] = page_num
                        logger.info(f"[BOOKMARK] ✅ TRCT → página {page_num}")
                
                elif "contracheque" in title_lower or "holerite" in title_lower or "recibo de salário" in title_lower:
                    if "contracheque" not in result:
                        result["contracheque"] = page_num
                        logger.info(f"[BOOKMARK] ✅ Contracheque → página {page_num}")
                
                elif "ficha de registro" in title_lower:
                    if "ficha_registro" not in result:
                        result["ficha_registro"] = page_num
                        logger.info(f"[BOOKMARK] ✅ Ficha Registro → página {page_num}")
                
                # 🆕 2025-12-05: TERMO DE DEVOLUÇÃO (uniforme/EPI)
                elif "devolução" in title_lower or "devolucao" in title_lower or "uniforme" in title_lower or "epi" in title_lower:
                    if "termo_devolucao" not in result:
                        result["termo_devolucao"] = page_num
                        logger.info(f"[BOOKMARK] ✅ Termo Devolução → página {page_num}")
                
                # 🆕 2025-12-05: TERMO DE QUITAÇÃO
                elif "quitação" in title_lower or "quitacao" in title_lower:
                    if "termo_quitacao" not in result:
                        result["termo_quitacao"] = page_num
                        logger.info(f"[BOOKMARK] ✅ Termo Quitação → página {page_num}")
                
                # 🆕 2025-12-05: ASO (Atestado de Saúde Ocupacional)
                elif "aso" in title_lower or "atestado de saúde" in title_lower or "exame admissional" in title_lower:
                    if "aso" not in result:
                        result["aso"] = page_num
                        logger.info(f"[BOOKMARK] ✅ ASO → página {page_num}")
                
                elif "ppp" in title_lower or "perfil profissiográfico" in title_lower:
                    if "ppp" not in result:
                        result["ppp"] = page_num
                        logger.info(f"[BOOKMARK] ✅ PPP → página {page_num}")
            
            except Exception as e:
                continue
        
        if result:
            logger.info(f"[BOOKMARK] 🎯 Mapeamento extraído: {result}")
        
        return result
    
    except Exception as e:
        logger.debug(f"[BOOKMARK] Erro ao extrair bookmarks: {e}")
        return result


def parse_toc_from_pdf(pdf_path: str, max_pages: int = 6) -> Dict[str, List[int]]:
    """
    Analisa o sumário (TOC) do PDF para encontrar páginas de anexos trabalhistas.
    
    2025-12-04: OCR Seletivo via Sumário - Extrai links do índice para TRCT, Contracheques, etc.
    2025-12-05: Corrigido para buscar sumário também nas últimas páginas (PJe coloca no final)
    
    Padrões reconhecidos no sumário:
    - "TRCT............45" ou "TRCT - pg 45" ou "TRCT (página 45)"
    - "Contracheques...........67-72"
    - Links clicáveis com destino para páginas
    
    Args:
        pdf_path: Caminho do PDF
        max_pages: Quantas páginas iniciais E finais analisar para o sumário (default: 6)
    
    Returns:
        Dict com {categoria: [páginas]} ex: {"trct": [45], "contracheque": [67, 68, 69]}
    """
    import re
    from PyPDF2 import PdfReader
    
    result = {k: [] for k in ANNEX_KEYWORDS.keys()}
    logger = logging.getLogger(__name__)
    
    try:
        reader = PdfReader(pdf_path)
        total_pages = len(reader.pages)
        
        pages_to_read = set()
        for i in range(min(max_pages, total_pages)):
            pages_to_read.add(i)
        for i in range(max(0, total_pages - max_pages), total_pages):
            pages_to_read.add(i)
        
        toc_text = ""
        for i in sorted(pages_to_read):
            page = reader.pages[i]
            text = page.extract_text() or ""
            toc_text += f"\n{text}"
        
        toc_lower = toc_text.lower()
        
        toc_patterns = [
            r'([A-Za-zÀ-ú\s]+)[\.\s]{3,}(\d+)',
            r'([A-Za-zÀ-ú\s]+)\s*[-–—]\s*(?:pg\.?|p\.?|página)\s*(\d+)',
            r'([A-Za-zÀ-ú\s]+)\s*\((?:pg\.?|p\.?|página)\s*(\d+)\)',
            r'([A-Za-zÀ-ú\s]+)\s+(\d{2,3})$',
        ]
        
        for pattern in toc_patterns:
            matches = re.finditer(pattern, toc_text, re.IGNORECASE | re.MULTILINE)
            for match in matches:
                label = match.group(1).strip().lower()
                try:
                    page_num = int(match.group(2))
                    if page_num > 0 and page_num <= total_pages:
                        for category, keywords in ANNEX_KEYWORDS.items():
                            for kw in keywords:
                                if kw in label:
                                    if page_num not in result[category]:
                                        result[category].append(page_num)
                                        logger.debug(f"[TOC_PARSER] '{label}' → {category} página {page_num}")
                                    break
                except ValueError:
                    continue
        
        range_pattern = r'([A-Za-zÀ-ú\s]+)[\.\s]{3,}(\d+)\s*[-–—a]\s*(\d+)'
        range_matches = re.finditer(range_pattern, toc_text, re.IGNORECASE)
        for match in range_matches:
            label = match.group(1).strip().lower()
            try:
                start_page = int(match.group(2))
                end_page = int(match.group(3))
                if start_page > 0 and end_page <= total_pages and end_page >= start_page:
                    for category, keywords in ANNEX_KEYWORDS.items():
                        for kw in keywords:
                            if kw in label:
                                for pg in range(start_page, min(end_page + 1, start_page + 5)):
                                    if pg not in result[category]:
                                        result[category].append(pg)
                                break
            except ValueError:
                continue
        
        found_any = any(pages for pages in result.values())
        if found_any:
            summary = {k: v for k, v in result.items() if v}
            logger.info(f"[TOC_PARSER] ✅ Sumário encontrado: {summary}")
        else:
            logger.debug("[TOC_PARSER] Nenhum link de anexo trabalhista encontrado no sumário")
        
        return result
        
    except Exception as e:
        logger.warning(f"[TOC_PARSER] Erro ao analisar sumário: {e}")
        return result


def resolve_missing_labor_fields(pdf_path: str, current_data: Dict[str, any], 
                                  missing_fields: List[str],
                                  defer_if_busy: bool = True) -> Tuple[Dict[str, str], Optional[Dict]]:
    """
    Resolve campos trabalhistas faltantes usando OCR seletivo via bookmarks.
    
    2025-12-05: LÓGICA SIMPLIFICADA - OCR por DOCUMENTO, não por campo.
    2025-12-05: FILA ASSÍNCRONA - Se slots ocupados, retorna tarefa diferida
    
    Estratégia SIMPLES:
    1. Ler bookmarks do PDF (CTPS→pág.X, TRCT→pág.Y, Contracheque→pág.Z)
    2. Para cada documento necessário: OCR 1x na primeira página
    3. Extrair TODOS os campos de cada documento em uma passada
    
    Documentos e seus campos:
    - CTPS: data_admissao, pis, ctps, serie_ctps
    - TRCT: data_demissao, salario (fallback)
    - Contracheque: salario
    
    Args:
        pdf_path: Caminho do PDF
        current_data: Dados já extraídos (para não sobrescrever)
        missing_fields: Lista de campos faltantes
        defer_if_busy: Se True, retorna tarefa diferida quando slots ocupados
    
    Returns:
        Tuple: (ocr_data, deferred_task)
        - ocr_data: Dict com campos recuperados via OCR
        - deferred_task: Dict com info para enfileirar depois, ou None se processou imediato
    """
    result = {}
    deferred_task = None
    logger = logging.getLogger(__name__)
    
    if not missing_fields or not pdf_path:
        return result, None
    
    logger.info(f"[OCR] Campos faltantes: {missing_fields}")
    
    # ===== PASSO 1: Identificar quais DOCUMENTOS precisamos processar =====
    docs_needed = set()
    
    # 🆕 2025-12-05: Mapeamento EXPANDIDO - qual documento contém qual campo
    # Incluindo TERMO DE DEVOLUÇÃO e TERMO DE QUITAÇÃO como fontes de dados
    
    if any(f in missing_fields for f in ["data_admissao", "pis", "ctps", "serie_ctps"]):
        docs_needed.add("ctps")
        docs_needed.add("termo_devolucao")   # 🆕 RG/CTPS, Função
        docs_needed.add("termo_quitacao")    # 🆕 PIS, CTPS, datas
        docs_needed.add("ficha_registro")    # 🆕 Dados cadastrais
    if any(f in missing_fields for f in ["data_demissao"]):
        docs_needed.add("trct")
        docs_needed.add("termo_quitacao")    # 🆕 Data de Afastamento
    if "salario" in missing_fields:
        docs_needed.add("contracheque")
        docs_needed.add("trct")  # fallback para salário
    if "cargo_funcao" in missing_fields:
        docs_needed.add("termo_devolucao")   # 🆕 Campo Função
        docs_needed.add("ficha_registro")    # 🆕 Campo Cargo
        docs_needed.add("ctps")              # Cargo na CTPS
    
    if not docs_needed:
        return result, None
    
    logger.info(f"[OCR] Documentos necessários: {docs_needed}")
    
    # ===== PASSO 2: Obter páginas dos documentos (bookmarks → TOC → inferência → heurística) =====
    bookmarks = extract_pdf_bookmarks(pdf_path)
    toc_pages = parse_toc_from_pdf(pdf_path) if not bookmarks else {}
    total_pages = get_pdf_total_pages(pdf_path)
    
    doc_pages = {}  # {doc_type: page_number}
    
    for doc in docs_needed:
        # Prioridade 1: Bookmarks
        if doc in bookmarks:
            doc_pages[doc] = bookmarks[doc]
            logger.info(f"[OCR] {doc.upper()} → página {bookmarks[doc]} (bookmark)")
        # Prioridade 2: TOC
        elif toc_pages.get(doc):
            doc_pages[doc] = toc_pages[doc][0]
            logger.info(f"[OCR] {doc.upper()} → página {toc_pages[doc][0]} (sumário)")
    
    # Prioridade 2.5: Inferência baseada em histórico do banco
    docs_sem_pagina = docs_needed - set(doc_pages.keys())
    if docs_sem_pagina and total_pages > 0:
        inferred = infer_annex_pages_from_history(total_pages, docs_sem_pagina)
        for doc, pages in inferred.items():
            if pages and doc not in doc_pages:
                doc_pages[doc] = pages[0]  # Usar página mais provável
                logger.info(f"[OCR] {doc.upper()} → página {pages[0]} (inferido do histórico)")
    
    # Prioridade 3: Heurística para documentos INDIVIDUAIS não encontrados
    # ⚠️ 2025-12-05: Corrigido - aplica heurística para CADA documento faltante, não só quando tudo falhou
    docs_sem_pagina = docs_needed - set(doc_pages.keys())
    if docs_sem_pagina and total_pages > 0:
        logger.info(f"[OCR] Documentos sem página: {docs_sem_pagina} - aplicando heurística")
        
        # Heurísticas baseadas em posição típica de documentos em PDFs trabalhistas
        # TRCT: geralmente nas últimas 15% páginas
        # Contracheque: geralmente nas últimas 25% páginas
        # CTPS: geralmente nas últimas 20% páginas
        
        for doc in docs_sem_pagina:
            if doc == "trct":
                # TRCT normalmente está nas últimas 15% páginas
                page_estimate = max(1, int(total_pages * 0.87))
                doc_pages[doc] = page_estimate
                logger.info(f"[OCR] TRCT → página {page_estimate} (heurística 87%)")
            elif doc == "contracheque":
                # Contracheque normalmente nas últimas 25% páginas
                page_estimate = max(1, int(total_pages * 0.77))
                doc_pages[doc] = page_estimate
                logger.info(f"[OCR] CONTRACHEQUE → página {page_estimate} (heurística 77%)")
            elif doc == "ctps":
                # CTPS normalmente nas últimas 20% páginas
                page_estimate = max(1, int(total_pages * 0.82))
                doc_pages[doc] = page_estimate
                logger.info(f"[OCR] CTPS → página {page_estimate} (heurística 82%)")
    
    if not doc_pages:
        logger.warning("[OCR] Nenhuma página de documento encontrada")
        return result, None
    
    # ===== PASSO 3: TENTAR OCR IMEDIATO OU RETORNAR TAREFA DIFERIDA =====
    # Tenta adquirir slot imediato (timeout curto de 0.5 segundos)
    # Se não conseguir e defer_if_busy=True, retorna tarefa para enfileirar depois
    
    if defer_if_busy:
        acquired = OCR_SEMAPHORE.acquire(timeout=0.5)
        if not acquired:
            # Slots ocupados → retorna tarefa para enfileirar após criar processo
            logger.info(f"[OCR] 📥 Slots ocupados - preparando tarefa diferida")
            deferred_task = {
                "pdf_path": pdf_path,
                "doc_pages": doc_pages,
                "missing_fields": list(missing_fields)
            }
            return result, deferred_task
        else:
            # Conseguiu slot → libera imediatamente, vai processar normalmente
            OCR_SEMAPHORE.release()
    
    # ===== PASSO 4: OCR SEQUENCIAL COM EARLY EXIT =====
    # Abre uma página, lê, achou os dados? PARA. Não achou? Próxima página.
    
    # 🆕 2025-12-05: Ordenar páginas por prioridade EXPANDIDA
    # Prioridade: Termo Quitação > Termo Devolução > Contracheque > TRCT > CTPS > Ficha Registro
    ordered_pages = []
    if doc_pages.get("termo_quitacao"):
        ordered_pages.append(("termo_quitacao", doc_pages["termo_quitacao"]))
    if doc_pages.get("termo_devolucao"):
        ordered_pages.append(("termo_devolucao", doc_pages["termo_devolucao"]))
    if doc_pages.get("contracheque"):
        ordered_pages.append(("contracheque", doc_pages["contracheque"]))
    if doc_pages.get("trct"):
        ordered_pages.append(("trct", doc_pages["trct"]))
    if doc_pages.get("ctps"):
        ordered_pages.append(("ctps", doc_pages["ctps"]))
    if doc_pages.get("ficha_registro"):
        ordered_pages.append(("ficha_registro", doc_pages["ficha_registro"]))
    
    campos_faltantes = set(missing_fields)
    
    try:
        for doc_type, page_num in ordered_pages:
            # Se já encontrou todos os campos, PARA
            if not campos_faltantes:
                logger.info(f"[OCR] ✅ Todos os campos encontrados - parando")
                break
            
            # 2025-12-05: Usar semáforo para limitar OCRs simultâneos (máx 2)
            acquired = OCR_SEMAPHORE.acquire(timeout=OCR_TIMEOUT_SECONDS)
            if not acquired:
                logger.warning(f"[OCR] ⏱️ Timeout aguardando slot OCR - pulando página {page_num}")
                continue
            
            texto_pagina = ""
            try:
                logger.info(f"[OCR] 📷 Lendo {doc_type.upper()} (página {page_num})...")
                
                # ✅ DPI 100 = mais rápido (suficiente para texto)
                images = convert_from_path(
                    pdf_path,
                    dpi=100,  # Reduzido de 150
                    first_page=page_num,
                    last_page=page_num,
                    poppler_path=POPPLER_PATH,
                    timeout=OCR_TIMEOUT_SECONDS
                )
                
                if images:
                    img = images[0]
                    # ✅ Pré-processamento otimizado
                    img_processed = _preprocess_image_for_ocr(img, doc_type)
                    # ✅ PSM otimizado por tipo de documento
                    psm = _get_psm_for_doc_type(doc_type)
                    config = f'{psm} -l por'
                    
                    texto_pagina = pytesseract.image_to_string(
                        img_processed, 
                        config=config,
                        timeout=OCR_TIMEOUT_SECONDS
                    )
            except Exception as e:
                logger.warning(f"[OCR] Erro página {page_num}: {e}")
            finally:
                OCR_SEMAPHORE.release()
            
            if not texto_pagina:
                continue
            
            logger.debug(f"[OCR] Página {page_num}: {len(texto_pagina)} chars")
            
            # Extrair campos desta página - EARLY EXIT por campo
            if "salario" in campos_faltantes:
                salario_patterns = [
                    r'(?:sal[aá]rio\s*(?:base|contratual|mensal)?|remunera[çc][ãa]o(?:\s*mensal)?)[:\s]*R?\$?\s*([\d]{1,3}(?:[.,]\d{3})*[,\.]\d{2})',
                    r'(?:maior\s*remunera[çc][ãa]o|base\s*de\s*c[aá]lculo)[:\s]*R?\$?\s*([\d]{1,3}(?:[.,]\d{3})*[,\.]\d{2})',
                ]
                for pattern in salario_patterns:
                    m = re.search(pattern, texto_pagina, re.IGNORECASE)
                    if m:
                        val_str = m.group(1).replace('.', '').replace(',', '.')
                        try:
                            val = float(val_str)
                            if 1000 <= val <= 100000:
                                result["salario"] = f"R$ {m.group(1)}"
                                campos_faltantes.discard("salario")
                                logger.info(f"[OCR] ✅ Salário: {result['salario']}")
                                break
                        except:
                            pass
            
            if "data_admissao" in campos_faltantes:
                # Padrões tradicionais + CTPS Digital
                admissao_patterns = [
                    r'(?:data\s*(?:de\s*)?admiss[ãa]o|admitido\s*em)[:\s]*(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})',
                    # CTPS Digital: "Contratos de trabalho 15/04/2024 - 05/06/2025"
                    r'[Cc]ontratos?\s*(?:de\s*)?trabalho\s*(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})\s*[-–]\s*\d',
                ]
                for pattern in admissao_patterns:
                    m = re.search(pattern, texto_pagina, re.IGNORECASE)
                    if m:
                        result["data_admissao"] = m.group(1)
                        campos_faltantes.discard("data_admissao")
                        logger.info(f"[OCR] ✅ Data Admissão: {result['data_admissao']}")
                        break
            
            if "data_demissao" in campos_faltantes:
                # Padrões tradicionais + CTPS Digital
                demissao_patterns = [
                    r'(?:data\s*(?:de\s*)?(?:demiss[ãa]o|desligamento|rescis[ãa]o|afastamento))[:\s]*(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})',
                    # CTPS Digital: "Contratos de trabalho 15/04/2024 - 05/06/2025"
                    r'[Cc]ontratos?\s*(?:de\s*)?trabalho\s*\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\s*[-–]\s*(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})',
                    # "projeção do aviso prévio indenizado 08/07/2025"
                    r'(?:proje[çc][ãa]o\s*(?:do\s*)?aviso\s*pr[ée]vio)[:\s]*(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})',
                ]
                for pattern in demissao_patterns:
                    m = re.search(pattern, texto_pagina, re.IGNORECASE)
                    if m:
                        result["data_demissao"] = m.group(1)
                        campos_faltantes.discard("data_demissao")
                        logger.info(f"[OCR] ✅ Data Demissão: {result['data_demissao']}")
                        break
            
            if "pis" in campos_faltantes:
                m = re.search(r'(?:PIS|PASEP|NIT)[:\s/]*(\d{3}[.\s]?\d{5}[.\s]?\d{2}[.\s-]?\d)', texto_pagina, re.IGNORECASE)
                if m:
                    pis_raw = re.sub(r'[^\d]', '', m.group(1))
                    if len(pis_raw) == 11:
                        result["pis"] = f"{pis_raw[:3]}.{pis_raw[3:8]}.{pis_raw[8:10]}-{pis_raw[10]}"
                        campos_faltantes.discard("pis")
                        logger.info(f"[OCR] ✅ PIS: {result['pis']}")
            
            if "ctps" in campos_faltantes:
                # 🆕 2025-12-05: Padrões ordenados por prioridade (específicos primeiro)
                ctps_patterns = [
                    # TERMO DE DEVOLUÇÃO: "RG/CTPS: 085227296" ou OCR "RGICTPS: |085227296"
                    r'RG\s*/\s*CTPS\s*[:\s]*(\d{6,12})',
                    r'RGICTPS\s*[:\s]*[\|\[\]]?(\d{6,12})',  # OCR lê "/" como "I"
                    # TERMO DE QUITAÇÃO Campo 17
                    r'17\s*CTPS[^\d]{0,20}(\d{6,12})',
                    # Genérico com contexto
                    r'(?:CTPS|Carteira)[:\s]*[nN]?[º°]?\s*(\d{5,12})',
                ]
                for pattern in ctps_patterns:
                    m = re.search(pattern, texto_pagina, re.IGNORECASE)
                    if m:
                        result["ctps"] = m.group(1)
                        campos_faltantes.discard("ctps")
                        logger.info(f"[OCR] ✅ CTPS: {result['ctps']}")
                        break
            
            if "serie_ctps" in campos_faltantes:
                m = re.search(r'[sS][eéE][rR][iI][eE][:\s]*(\d{3,5})', texto_pagina)
                if m:
                    result["serie_ctps"] = m.group(1)
                    campos_faltantes.discard("serie_ctps")
                    logger.info(f"[OCR] ✅ Série CTPS: {result['serie_ctps']}")
            
            # 🆕 2025-12-05: Cargo/Função - TERMO DE DEVOLUÇÃO e outros documentos
            if "cargo_funcao" in campos_faltantes:
                cargo_patterns = [
                    # TERMO DE DEVOLUÇÃO: "Função |MAQUINISTA DE TEATRO" ou "[Função |..."
                    r'Fun[çc][ãa]o\s*[\|\[\]:]?\s*([A-ZÀ-Ú][A-ZÀ-Ú\s/\-]{3,40})(?:\n|Setor|Matr|$)',
                    # TRCT Campo 22: "22 Cargo OPERADOR"
                    r'22\s*(?:Cargo|Fun[çc][ãa]o)\s*([A-ZÀ-Ú][A-ZÀ-Ú\s/\-]{3,40})',
                    # Ficha de registro: "Cargo/Função: OPERADOR"
                    r'Cargo\s*/?\s*Fun[çc][ãa]o\s*[:\s]+([A-Za-zÀ-ú][A-Za-zÀ-ú\s/\-]{3,40})',
                    # "Ocupação: OPERADOR"
                    r'Ocupa[çc][ãa]o\s*[:\s]+([A-Za-zÀ-ú][A-Za-zÀ-ú\s/\-]{3,40})',
                    # Genérico com pipe do OCR: "Função |OPERADOR"
                    r'Fun[çc][ãa]o\s*[\|\[\]:]\s*([A-Za-zÀ-ú][A-Za-zÀ-ú\s/\-]{3,40})',
                    r'Cargo\s*[\|\[\]:]\s*([A-Za-zÀ-ú][A-Za-zÀ-ú\s/\-]{3,40})',
                ]
                for pattern in cargo_patterns:
                    m = re.search(pattern, texto_pagina, re.IGNORECASE)
                    if m:
                        cargo = m.group(1).strip()
                        # Limpar trailing de palavras desnecessárias e pipes
                        cargo = re.sub(r'^[\|\[\]]+', '', cargo).strip()  # Remove pipes no início
                        cargo = re.sub(r'\s+(Setor|Matr|de|da|do|e)$', '', cargo, flags=re.I).strip()
                        if len(cargo) >= 3:
                            result["cargo_funcao"] = cargo
                            campos_faltantes.discard("cargo_funcao")
                            logger.info(f"[OCR] ✅ Cargo/Função: {result['cargo_funcao']}")
                            break
        
        if result:
            logger.info(f"[OCR] 🎯 Recuperados: {list(result.keys())}")
        
        return result, None  # Processou imediato, sem tarefa diferida
        
    except Exception as e:
        logger.error(f"[OCR] ❌ Erro: {e}")
        return result, None


def detect_scanned_pages(pdf_path: str, min_text_len: int = 200, 
                         search_all: bool = True) -> List[int]:
    """
    Detecta páginas escaneadas/imagens em um PDF usando heurística robusta.
    
    2025-12-01: Plano Batman - Mapeamento cirúrgico para OCR seletivo.
    2025-12-05: Corrigido para buscar em TODO o PDF (não só últimas 30%).
               PDFs do PJe têm anexos em qualquer posição.
    
    Heurística: Uma página é considerada escaneada/imagem se:
    - Tem menos de 200 caracteres de texto nativo E
    - Contém apenas texto de rodapé ("Documento assinado eletronicamente...")
    
    Args:
        pdf_path: Caminho do PDF
        min_text_len: Mínimo de caracteres para considerar página como texto (default: 200)
        search_all: Se True, busca em todo o PDF. Se False, só nas últimas 30%.
    
    Returns:
        Lista de números de páginas escaneadas (1-indexed)
    """
    from PyPDF2 import PdfReader
    
    scanned_pages = []
    logger = logging.getLogger(__name__)
    
    try:
        reader = PdfReader(pdf_path)
        total_pages = len(reader.pages)
        
        # Determinar onde começar a busca
        if search_all:
            start_page = 1
        else:
            start_page = max(1, int(total_pages * 0.7))
        
        for i, page in enumerate(reader.pages, 1):
            if i < start_page:
                continue
            
            text = page.extract_text() or ""
            text_len = len(text.strip())
            
            # Página com menos de 200 chars = provável imagem/scan
            if text_len < min_text_len:
                scanned_pages.append(i)
                logger.debug(f"[DETECT_SCANNED] Página {i}/{total_pages}: {text_len} chars - ESCANEADA")
        
        if scanned_pages:
            logger.info(f"[DETECT_SCANNED] {len(scanned_pages)} páginas escaneadas encontradas: {scanned_pages}")
        
    except Exception as e:
        logger.debug(f"[DETECT_SCANNED] Erro ao analisar PDF: {e}")
    
    return scanned_pages


def ocr_extract_from_pages(pdf_path: str, pages: List[int]) -> Dict[str, str]:
    """
    Aplica OCR apenas nas páginas específicas e extrai campos trabalhistas.
    
    2025-12-01: Plano Batman - OCR cirúrgico apenas nas páginas mapeadas.
    
    Args:
        pdf_path: Caminho do PDF
        pages: Lista de números de páginas para processar (1-indexed)
    
    Returns:
        Dict com campos extraídos: {"salario": "...", "pis": "...", "ctps": "..."}
    """
    import re
    
    result = {}
    
    if not pages:
        return result
    
    try:
        logger = logging.getLogger(__name__)
        logger.info(f"[OCR_CIRURGICO] Processando {len(pages)} páginas: {pages}")
        
        texto_ocr = ""
        for page_num in pages:
            try:
                # ✅ DPI 100 = mais rápido
                images = convert_from_path(
                    pdf_path,
                    dpi=100,  # Reduzido de 150
                    first_page=page_num,
                    last_page=page_num,
                    poppler_path=POPPLER_PATH
                )
                
                for img in images:
                    # ✅ Pré-processamento otimizado
                    img_processed = _preprocess_image_for_ocr(img, "generic")
                    # ✅ PSM 6 para bloco uniforme
                    config = '--psm 6 -l por+eng'
                    texto_pagina = pytesseract.image_to_string(img_processed, config=config)
                    texto_ocr += f"\n{texto_pagina}"
            except Exception as e:
                logger.debug(f"[OCR_CIRURGICO] Erro página {page_num}: {e}")
        
        if not texto_ocr:
            return result
        
        # Extrair salário
        salario_patterns = [
            r'(?:sal[aá]rio|remunera[cç][aã]o|vencimento)[:\s]*R?\$?\s*([\d.,]+)',
            r'R\$\s*([\d]{1,3}(?:\.?\d{3})*[,\.]\d{2})',
        ]
        for pattern in salario_patterns:
            m = re.search(pattern, texto_ocr, re.IGNORECASE)
            if m:
                val = m.group(1).replace('.', '').replace(',', '.')
                try:
                    if float(val) > 500:
                        result["salario"] = f"R$ {m.group(1)}"
                        logger.info(f"[OCR_CIRURGICO] Salário: {result['salario']}")
                        break
                except:
                    pass
        
        # Extrair PIS
        pis_patterns = [
            r'(?:PIS|PASEP|NIT)[:\s/]*(\d{3}[.\s]?\d{5}[.\s]?\d{2}[.\s-]?\d)',
            r'\b(\d{3}\.\d{5}\.\d{2}[.-]\d)\b',
        ]
        for pattern in pis_patterns:
            m = re.search(pattern, texto_ocr, re.IGNORECASE)
            if m:
                pis_raw = m.group(1).replace(' ', '').replace('.', '').replace('-', '')
                if len(pis_raw) == 11:
                    result["pis"] = f"{pis_raw[:3]}.{pis_raw[3:8]}.{pis_raw[8:10]}-{pis_raw[10]}"
                    logger.info(f"[OCR_CIRURGICO] PIS: {result['pis']}")
                    break
        
        # Extrair CTPS (com UF quando disponível)
        ctps_patterns = [
            # Formato com UF: "CTPS 1234567 série 123/RJ" ou "1234567/123/RJ"
            r'(?:CTPS|Carteira)[:\s]*(\d{5,7})[/\s]*(?:s[eé]rie|série)[:\s]*(\d{3,5})[/\s-]*([A-Z]{2})',
            r'(\d{5,7})[/\s-]+(\d{3,5})[/\s-]+([A-Z]{2})',
            # Formato sem UF: "CTPS 1234567 série 123"
            r'(?:CTPS|Carteira)[:\s]*(\d{5,7})[/\s]*(?:s[eé]rie|série)[:\s]*(\d{3,5})',
        ]
        for pattern in ctps_patterns:
            m = re.search(pattern, texto_ocr, re.IGNORECASE)
            if m:
                if len(m.groups()) >= 3 and m.group(3):
                    # Com UF
                    result["ctps"] = f"{m.group(1)} série {m.group(2)}/{m.group(3)}"
                elif len(m.groups()) >= 2:
                    # Sem UF
                    result["ctps"] = f"{m.group(1)} série {m.group(2)}"
                else:
                    result["ctps"] = m.group(1)
                logger.info(f"[OCR_CIRURGICO] CTPS: {result['ctps']}")
                break
        
        logger.info(f"[OCR_CIRURGICO] ✅ Extraídos {len(result)} campos via OCR cirúrgico")
        return result
        
    except Exception as e:
        logging.getLogger(__name__).error(f"[OCR_CIRURGICO] ❌ Erro: {e}")
        return result


# Integração com monitor remoto
try:
    from monitor_integration import log_info, log_error
    MONITOR_AVAILABLE = True
except ImportError:
    MONITOR_AVAILABLE = False
    def log_info(msg, region=""): pass
    def log_error(msg, exc=None, region=""): pass

logger = logging.getLogger(__name__)

def is_scanned_pdf(text: str, page_count: int = 1) -> bool:
    """
    Detecta se PDF é escaneado (densidade de texto baixa).
    
    Args:
        text: Texto extraído do PDF
        page_count: Número de páginas do PDF
    
    Returns:
        True se densidade < 200 chars/página (provável scan)
    """
    if not text or len(text.strip()) == 0:
        return True
    
    densidade = len(text) / page_count if page_count > 0 else 0
    return densidade < 200


def extract_text_with_ocr(pdf_path: str, first_pages: int = 3) -> str:
    """
    Extrai texto usando OCR (Tesseract) nas primeiras páginas do PDF.
    
    2025-12-05: Otimizações de velocidade:
    - DPI reduzido de 300 para 150 (ainda muito bom para texto)
    - Pré-processamento com binarização
    
    Args:
        pdf_path: Caminho do arquivo PDF
        first_pages: Número de páginas para processar (default: 3)
    
    Returns:
        Texto extraído via OCR
    """
    try:
        logger.info(f"[OCR] Iniciando extração OCR: {pdf_path}")
        if POPPLER_PATH:
            logger.info(f"[OCR] Usando poppler de: {POPPLER_PATH}")
        
        # ✅ DPI 150 = 2x mais rápido que 300, suficiente para texto
        images = convert_from_path(
            pdf_path, 
            dpi=150,  # Reduzido de 300
            first_page=1,
            last_page=first_pages,
            poppler_path=POPPLER_PATH
        )
        
        logger.info(f"[OCR] Converteu {len(images)} páginas para imagem")
        
        texto_completo = []
        for i, img in enumerate(images, 1):
            # ✅ Pré-processamento otimizado
            img_processed = _preprocess_image_for_ocr(img, "generic")
            
            config = '--psm 6 -l por+eng'
            texto_pagina = pytesseract.image_to_string(img_processed, config=config)
            
            texto_completo.append(f"\n--- PÁGINA {i} (OCR) ---\n{texto_pagina}")
            logger.debug(f"[OCR] Página {i}: {len(texto_pagina)} chars")
        
        texto_final = "\n".join(texto_completo)
        logger.info(f"[OCR] ✅ Extração concluída: {len(texto_final)} chars total")
        
        return texto_final
        
    except Exception as e:
        logger.error(f"[OCR] ❌ Erro ao processar PDF: {e}")
        return ""


def ocr_extract_labor_fields(pdf_path: str, max_pages: int = 8) -> Dict[str, str]:
    """
    Extrai campos trabalhistas críticos (salário, PIS, CTPS) via OCR seletivo.
    
    Faz OCR nas ÚLTIMAS páginas do PDF onde geralmente estão TRCT/contracheques.
    
    2025-12-05: Otimizações de velocidade:
    - DPI 100 (reduzido de 150)
    - Pré-processamento com binarização
    
    Args:
        pdf_path: Caminho do PDF
        max_pages: Máximo de páginas para processar (default: 8)
    
    Returns:
        Dict com campos extraídos: {"salario": "...", "pis": "...", "ctps": "..."}
    """
    import re
    
    result = {}
    
    try:
        from PyPDF2 import PdfReader
        
        reader = PdfReader(pdf_path)
        total_pages = len(reader.pages)
        
        start_page = max(1, total_pages - max_pages + 1)
        
        logger.info(f"[OCR_LABOR] Extraindo campos trabalhistas via OCR: páginas {start_page}-{total_pages}")
        
        # ✅ DPI 100 = mais rápido
        images = convert_from_path(
            pdf_path,
            dpi=100,  # Reduzido de 150
            first_page=start_page,
            last_page=total_pages,
            poppler_path=POPPLER_PATH
        )
        
        texto_ocr = ""
        for i, img in enumerate(images, start_page):
            # ✅ Pré-processamento otimizado
            img_processed = _preprocess_image_for_ocr(img, "generic")
            config = '--psm 6 -l por+eng'
            texto_pagina = pytesseract.image_to_string(img_processed, config=config)
            texto_ocr += f"\n{texto_pagina}"
        
        if not texto_ocr:
            return result
        
        salario_patterns = [
            r'(?:sal[aá]rio|remunera[cç][aã]o|vencimento)[:\s]*R?\$?\s*([\d.,]+)',
            r'R\$\s*([\d]{1,3}(?:\.?\d{3})*[,\.]\d{2})',
        ]
        for pattern in salario_patterns:
            m = re.search(pattern, texto_ocr, re.IGNORECASE)
            if m:
                val = m.group(1).replace('.', '').replace(',', '.')
                try:
                    if float(val) > 500:
                        result["salario"] = f"R$ {m.group(1)}"
                        logger.info(f"[OCR_LABOR] Salário: {result['salario']}")
                        break
                except:
                    pass
        
        pis_patterns = [
            r'(?:PIS|PASEP|NIT)[:\s/]*(\d{3}[.\s]?\d{5}[.\s]?\d{2}[.\s-]?\d)',
            r'\b(\d{3}\.\d{5}\.\d{2}[.-]\d)\b',
        ]
        for pattern in pis_patterns:
            m = re.search(pattern, texto_ocr, re.IGNORECASE)
            if m:
                result["pis"] = m.group(1).replace(' ', '').replace('.', '').replace('-', '')
                logger.info(f"[OCR_LABOR] PIS: {result['pis']}")
                break
        
        ctps_patterns = [
            r'(?:CTPS|Carteira)[:\s]*(\d{5,7})[/\s]*(?:s[eé]rie|série)[:\s]*(\d{3,5})',
            r'(\d{5,7})[/\s-]+(\d{3,5})[/\s-]*([A-Z]{2})',
        ]
        for pattern in ctps_patterns:
            m = re.search(pattern, texto_ocr, re.IGNORECASE)
            if m:
                if len(m.groups()) >= 2:
                    result["ctps"] = f"{m.group(1)} série {m.group(2)}"
                else:
                    result["ctps"] = m.group(1)
                logger.info(f"[OCR_LABOR] CTPS: {result['ctps']}")
                break
        
        logger.info(f"[OCR_LABOR] ✅ Extraídos {len(result)} campos via OCR")
        return result
        
    except Exception as e:
        logger.error(f"[OCR_LABOR] ❌ Erro: {e}")
        return result


def extract_text_from_annex_pages(pdf_path: str, last_pages: int = 5) -> str:
    """
    Extrai texto via OCR das ÚLTIMAS páginas do PDF (onde estão anexos como TRCT/contracheques).
    
    2025-11-28: Função criada para resolver problema de PDFs híbridos:
    - Petição inicial nas primeiras páginas (texto nativo)
    - Anexos (TRCT, contracheques) nas últimas páginas (escaneados/imagens)
    
    Args:
        pdf_path: Caminho do arquivo PDF
        last_pages: Número de páginas finais para processar (default: 5)
    
    Returns:
        Texto extraído via OCR das últimas páginas
    """
    try:
        from PyPDF2 import PdfReader
        
        reader = PdfReader(pdf_path)
        total_pages = len(reader.pages)
        
        if total_pages <= last_pages:
            start_page = 1
        else:
            start_page = total_pages - last_pages + 1
        
        logger.info(f"[OCR_ANNEX] Processando páginas {start_page}-{total_pages} de {total_pages}")
        
        images = convert_from_path(
            pdf_path, 
            dpi=150,
            first_page=start_page,
            last_page=total_pages,
            poppler_path=POPPLER_PATH
        )
        
        texto_completo = []
        for i, img in enumerate(images, start_page):
            img_gray = img.convert('L')
            config = '--psm 6 -l por+eng'
            texto_pagina = pytesseract.image_to_string(img_gray, config=config)
            
            if texto_pagina and len(texto_pagina.strip()) > 50:
                texto_completo.append(f"\n--- ANEXO PÁGINA {i} (OCR) ---\n{texto_pagina}")
                logger.debug(f"[OCR_ANNEX] Página {i}: {len(texto_pagina)} chars")
        
        texto_final = "\n".join(texto_completo)
        logger.info(f"[OCR_ANNEX] ✅ Extração de anexos: {len(texto_final)} chars")
        
        return texto_final
        
    except Exception as e:
        logger.error(f"[OCR_ANNEX] ❌ Erro ao processar anexos: {e}")
        return ""


def _parse_sumario_for_annex_ranges(reader, scanned_pages: List[int]) -> Dict[str, List[int]]:
    """
    Analisa páginas de sumário/índice para inferir quais páginas escaneadas 
    correspondem a cada tipo de documento.
    
    Estratégia: O sumário lista documentos em ordem. Identificamos a ordem
    e mapeamos para as páginas escaneadas na mesma sequência.
    """
    import re
    
    annex_order = []
    
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        text_lower = text.lower()
        
        if "sumário" in text_lower or "documentos" in text_lower:
            lines = text.split('\n')
            for line in lines:
                line_lower = line.lower()
                if "trct" in line_lower or "termo de rescis" in line_lower:
                    annex_order.append('trct')
                elif any(kw in line_lower for kw in ["contracheque", "holerite", "recibo de"]):
                    annex_order.append('contracheque')
                elif "ficha de registro" in line_lower:
                    annex_order.append('ficha_registro')
                elif "ctps" in line_lower or "carteira de trabalho" in line_lower:
                    annex_order.append('ctps')
    
    result = {'trct': [], 'contracheque': [], 'ficha_registro': [], 'ctps': []}
    
    if annex_order and scanned_pages:
        pages_per_annex = max(1, len(scanned_pages) // max(1, len(annex_order)))
        
        for idx, annex_type in enumerate(annex_order):
            start_idx = idx * pages_per_annex
            end_idx = min(start_idx + pages_per_annex, len(scanned_pages))
            if start_idx < len(scanned_pages):
                result[annex_type].extend(scanned_pages[start_idx:end_idx])
    
    return result


def map_pdf_annexes(pdf_path: str) -> Dict[str, List[int]]:
    """
    MAPEAMENTO CIRÚRGICO: Identifica localização exata de cada tipo de anexo no PDF.
    
    Estratégia (melhorada):
    1. Analisa texto nativo de cada página para identificar tipo de documento
    2. Páginas com < 200 chars = escaneadas (candidatas a OCR)
    3. Se não encontrar tipos específicos, usa sumário para inferir ordem
    4. Fallback: divide páginas escaneadas em grupos lógicos
    
    Returns:
        Dict com listas de páginas por tipo:
        {
            'trct': [17, 18],        # Páginas do TRCT
            'contracheque': [19, 20, 21],  # Páginas de contracheques
            'ficha_registro': [22],  # Ficha de registro
            'ctps': [23, 24],        # CTPS
            'audiencia': [35, 36],   # Notificações de audiência
            'scanned': [17, 18, 19, 20, 21, 22, 23, 24]  # Todas páginas escaneadas
            'salary_candidates': [17, 18, 19]  # Páginas prováveis para salário
        }
    """
    from PyPDF2 import PdfReader
    
    try:
        reader = PdfReader(pdf_path)
        total_pages = len(reader.pages)
        
        mapping = {
            'trct': [],
            'contracheque': [],
            'ficha_registro': [],
            'ctps': [],
            'audiencia': [],
            'scanned': [],
            'salary_candidates': [],
            'total_pages': total_pages
        }
        
        SCANNED_THRESHOLD = 200
        
        for i, page in enumerate(reader.pages):
            page_num = i + 1
            text = page.extract_text() or ""
            text_lower = text.lower()
            text_len = len(text.strip())
            
            is_scanned = text_len < SCANNED_THRESHOLD
            
            if is_scanned:
                mapping['scanned'].append(page_num)
            
            if "termo de rescis" in text_lower or ("trct" in text_lower and "contrato" in text_lower):
                mapping['trct'].append(page_num)
            
            if any(kw in text_lower for kw in ["contracheque", "holerite", "demonstrativo de pagamento", "folha de pagamento"]):
                mapping['contracheque'].append(page_num)
            
            if "ficha de registro" in text_lower or "registro de empregado" in text_lower:
                mapping['ficha_registro'].append(page_num)
            
            if "carteira de trabalho" in text_lower or ("ctps" in text_lower and len(text_lower) > 100):
                mapping['ctps'].append(page_num)
            
            if any(kw in text_lower for kw in ["notificação", "audiência", "comparecimento"]):
                if "data" in text_lower and ("hora" in text_lower or ":" in text):
                    mapping['audiencia'].append(page_num)
        
        has_specific_types = any([
            [p for p in mapping['trct'] if p in mapping['scanned']],
            [p for p in mapping['contracheque'] if p in mapping['scanned']],
            [p for p in mapping['ficha_registro'] if p in mapping['scanned']],
            [p for p in mapping['ctps'] if p in mapping['scanned']]
        ])
        
        if not has_specific_types and mapping['scanned']:
            inferred = _parse_sumario_for_annex_ranges(reader, mapping['scanned'])
            for key in ['trct', 'contracheque', 'ficha_registro', 'ctps']:
                if inferred.get(key):
                    mapping[key] = inferred[key]
        
        if mapping['scanned']:
            scanned = mapping['scanned']
            
            if len(scanned) <= 10:
                mapping['salary_candidates'] = scanned[:]
            else:
                first_chunk = scanned[:5]
                
                mid_start = len(scanned) // 3
                mid_chunk = scanned[mid_start:mid_start+3]
                
                mapping['salary_candidates'] = list(set(first_chunk + mid_chunk))
                mapping['salary_candidates'].sort()
        
        scanned_count = len(mapping['scanned'])
        logger.info(f"[MAP] PDF mapeado: {total_pages} páginas, {scanned_count} escaneadas")
        logger.info(f"[MAP] TRCT: {mapping['trct']}, Contracheque: {mapping['contracheque']}, Audiência: {mapping['audiencia']}")
        logger.info(f"[MAP] Candidatos salário: {mapping['salary_candidates'][:8]}")
        
        return mapping
        
    except Exception as e:
        logger.error(f"[MAP] Erro ao mapear PDF: {e}")
        return {'trct': [], 'contracheque': [], 'ficha_registro': [], 'ctps': [], 'audiencia': [], 'scanned': [], 'salary_candidates': [], 'total_pages': 0}


def extract_ocr_from_specific_pages(pdf_path: str, pages: List[int], max_pages: int = 10) -> str:
    """
    OCR CIRÚRGICO: Processa APENAS páginas específicas identificadas pelo mapeamento.
    
    Otimização máxima:
    - Processa somente páginas relevantes (não o PDF inteiro)
    - Limita a max_pages para evitar processamento excessivo
    - 150dpi para balanço entre qualidade e velocidade
    
    Args:
        pdf_path: Caminho do PDF
        pages: Lista de páginas específicas para processar (1-indexed)
        max_pages: Limite de páginas para processar (default: 10)
    
    Returns:
        Texto extraído via OCR das páginas especificadas
    """
    if not pages:
        return ""
    
    pages_to_process = pages[:max_pages]
    
    try:
        texto_completo = []
        
        for page_num in pages_to_process:
            try:
                images = convert_from_path(
                    pdf_path,
                    dpi=150,
                    first_page=page_num,
                    last_page=page_num,
                    poppler_path=POPPLER_PATH
                )
                
                if images:
                    img_gray = images[0].convert('L')
                    config = '--psm 6 -l por+eng'
                    texto_pagina = pytesseract.image_to_string(img_gray, config=config)
                    
                    if texto_pagina and len(texto_pagina.strip()) > 30:
                        texto_completo.append(f"\n--- PÁGINA {page_num} (OCR) ---\n{texto_pagina}")
                        logger.debug(f"[OCR_SURGICAL] Página {page_num}: {len(texto_pagina)} chars")
                        
            except Exception as e:
                logger.warning(f"[OCR_SURGICAL] Erro na página {page_num}: {e}")
                continue
        
        texto_final = "\n".join(texto_completo)
        logger.info(f"[OCR_SURGICAL] ✅ Processadas {len(pages_to_process)} páginas: {len(texto_final)} chars")
        
        return texto_final
        
    except Exception as e:
        logger.error(f"[OCR_SURGICAL] ❌ Erro: {e}")
        return ""


def extract_salario_from_contracheque_ocr(texto: str) -> Optional[str]:
    """
    Extrai salário de texto OCR de contracheque.
    
    Prioridade:
    1. Salário Base/Salário Contratual
    2. Maior Remuneração
    3. Último salário no histórico de alterações
    
    Padrões específicos de contracheque:
    - "Salário 220,000 1.632,31" (código + qtd + valor)
    - "Salário Base: 1.632,31"
    - "Maior Remuneração: 2.160,31"
    - Histórico: "01/05/2025 2.255,56 2.255,56 Acordo/Convencao"
    """
    import re
    
    if not texto:
        return None
    
    texto_norm = re.sub(r'\s+', ' ', texto).strip()
    texto_norm = re.sub(r'R\s*\$', 'R$', texto_norm)
    texto_norm = re.sub(r',\s+', ',', texto_norm)
    texto_norm = re.sub(r'\.\s+', '.', texto_norm)
    texto_norm = re.sub(r'\s+\.', '.', texto_norm)
    
    valor_pattern = r'([0-9]+(?:[\.][0-9]{3})*[,][0-9]{2})'
    
    patterns_priority = [
        (r'sal[aá]rio\s*(?:base|contratual)?[:\s]+[0-9,\.]+\s+' + valor_pattern, 'Salário Base'),
        (r'Sal\.?\s*Contr\.?\s*(?:INSS)?\s+' + valor_pattern, 'Sal. Contr.'),
        (r'maior\s*remunera[çc][aã]o[:\s]+' + valor_pattern, 'Maior Remuneração'),
        (r'(?:Total\s*)?Vencimentos[:\s]+' + valor_pattern, 'Total Vencimentos'),
    ]
    
    for pattern, name in patterns_priority:
        match = re.search(pattern, texto_norm, re.IGNORECASE)
        if match:
            valor = match.group(1)
            valor_float = float(valor.replace('.', '').replace(',', '.'))
            if 800 < valor_float < 50000:
                logger.debug(f"[OCR_CONTRA] {name}: R$ {valor}")
                return f"R$ {valor}"
    
    hist_pattern = r'(\d{2}/\d{2}/\d{4})\s+[\d/]+\s+' + valor_pattern + r'\s+' + valor_pattern
    historico = re.findall(hist_pattern, texto_norm)
    
    if historico:
        ultimo = historico[-1]
        valor = ultimo[1]
        valor_float = float(valor.replace('.', '').replace(',', '.'))
        if 800 < valor_float < 50000:
            logger.debug(f"[OCR_CONTRA] Histórico (último): R$ {valor}")
            return f"R$ {valor}"
    
    return None


def extract_salario_from_annexes(pdf_path: str) -> Optional[str]:
    """
    Extrai salário dos anexos do PDF (TRCT, contracheques) via OCR CIRÚRGICO.
    
    PLANO BATMAN - Estratégia otimizada (v2):
    1. Mapeia estrutura do PDF para identificar páginas de TRCT/contracheques
    2. Identifica páginas escaneadas (< 200 chars)
    3. Usa salary_candidates que inclui páginas do início E do meio
    4. Prioriza fontes: TRCT > Contracheque > Ficha > Candidatos
    5. Se não encontrar, tenta mais páginas em chunks
    
    Returns:
        Salário no formato "R$ X.XXX,XX" ou None
    """
    from .regex_utils import extract_salario
    
    logger.info(f"[OCR_SURGICAL] Iniciando extração cirúrgica de salário: {pdf_path}")
    log_info("Mapeando anexos para extração cirúrgica de salário", region="OCR_EXTRACTOR")
    
    mapping = map_pdf_annexes(pdf_path)
    
    scanned_pages = set(mapping['scanned'])
    
    trct_scanned = [p for p in mapping['trct'] if p in scanned_pages]
    contracheque_scanned = [p for p in mapping['contracheque'] if p in scanned_pages]
    ficha_scanned = [p for p in mapping['ficha_registro'] if p in scanned_pages]
    ctps_scanned = [p for p in mapping['ctps'] if p in scanned_pages]
    
    pages_to_ocr = []
    source_priority = []
    
    if trct_scanned:
        pages_to_ocr.extend(trct_scanned[:3])
        source_priority.append(f"TRCT({trct_scanned[:3]})")
    if contracheque_scanned:
        pages_to_ocr.extend(contracheque_scanned[:3])
        source_priority.append(f"Contracheque({contracheque_scanned[:3]})")
    if ficha_scanned:
        pages_to_ocr.extend(ficha_scanned[:2])
        source_priority.append(f"Ficha({ficha_scanned[:2]})")
    if ctps_scanned:
        pages_to_ocr.extend(ctps_scanned[:2])
        source_priority.append(f"CTPS({ctps_scanned[:2]})")
    
    if not pages_to_ocr:
        salary_candidates = mapping.get('salary_candidates', [])
        if salary_candidates:
            pages_to_ocr = salary_candidates[:8]
            source_priority.append(f"Candidatos({len(salary_candidates)} total, processando {len(pages_to_ocr)})")
    
    if not pages_to_ocr:
        logger.info("[OCR_SURGICAL] Nenhuma página candidata identificada")
        return None
    
    pages_to_ocr = list(dict.fromkeys(pages_to_ocr))[:8]
    
    logger.info(f"[OCR_SURGICAL] Prioridade: {' > '.join(source_priority)}")
    log_info(f"OCR cirúrgico em {len(pages_to_ocr)} páginas: {source_priority}", region="OCR_EXTRACTOR")
    
    texto_ocr = extract_ocr_from_specific_pages(pdf_path, pages_to_ocr, max_pages=8)
    
    if not texto_ocr:
        return None
    
    salario = extract_salario_from_contracheque_ocr(texto_ocr)
    
    if not salario:
        salario = extract_salario(texto_ocr)
    
    if salario:
        logger.info(f"[OCR_SURGICAL] ✅ Salário extraído: {salario}")
        log_info(f"Salário via OCR cirúrgico: {salario}", region="OCR_EXTRACTOR")
        return salario
    
    all_scanned = mapping.get('scanned', [])
    remaining = [p for p in all_scanned if p not in pages_to_ocr]
    
    if remaining and len(remaining) >= 3:
        mid_start = len(remaining) // 2
        extra_pages = remaining[mid_start:mid_start+3]
        
        logger.info(f"[OCR_SURGICAL] Tentando chunk adicional: {extra_pages}")
        texto_extra = extract_ocr_from_specific_pages(pdf_path, extra_pages, max_pages=3)
        
        if texto_extra:
            salario = extract_salario_from_contracheque_ocr(texto_extra)
            if not salario:
                salario = extract_salario(texto_extra)
            
            if salario:
                logger.info(f"[OCR_SURGICAL] ✅ Salário encontrado no chunk adicional: {salario}")
                return salario
    
    logger.info("[OCR_SURGICAL] Salário não encontrado em nenhuma página")
    return None


def extract_audiencia_from_mapping(pdf_path: str) -> Optional[Dict[str, str]]:
    """
    Extrai data de audiência das páginas mapeadas como notificações.
    
    PLANO BATMAN - Estratégia para audiência:
    1. Mapeia páginas com notificação de audiência
    2. Extrai texto (nativo ou OCR se necessário)
    3. Aplica regex específicos para data/hora de audiência
    
    Returns:
        Dict com data_audiencia e hora_audiencia, ou None
    """
    import re
    
    logger.info(f"[OCR_SURGICAL] Extraindo audiência via mapeamento: {pdf_path}")
    
    mapping = map_pdf_annexes(pdf_path)
    
    audiencia_pages = mapping.get('audiencia', [])
    
    if not audiencia_pages:
        logger.info("[OCR_SURGICAL] Nenhuma página de audiência identificada no mapeamento")
        return None
    
    from PyPDF2 import PdfReader
    
    try:
        reader = PdfReader(pdf_path)
        
        patterns_data = [
            r'(?:designad[ao]|marcad[ao]|realiz[ao]r)[^0-9]{0,30}(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})',
            r'(?:dia|data)[:\s]*(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})',
            r'(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})',
            r'comparec\w+[^0-9]{0,30}(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})',
        ]
        
        patterns_hora = [
            r'(?:às|as|hora)[:\s]*(\d{1,2})[h:](\d{2})',
            r'(\d{1,2})[h:](\d{2})\s*(?:h|hora|min)',
            r'horário[:\s]*(\d{1,2})[h:](\d{2})',
        ]
        
        meses = {
            'janeiro': '01', 'fevereiro': '02', 'março': '03', 'abril': '04',
            'maio': '05', 'junho': '06', 'julho': '07', 'agosto': '08',
            'setembro': '09', 'outubro': '10', 'novembro': '11', 'dezembro': '12'
        }
        
        for page_num in audiencia_pages:
            if page_num > len(reader.pages):
                continue
                
            page = reader.pages[page_num - 1]
            text = page.extract_text() or ""
            
            if len(text.strip()) < 100:
                ocr_text = extract_ocr_from_specific_pages(pdf_path, [page_num], max_pages=1)
                if ocr_text:
                    text = ocr_text
            
            text_lower = text.lower()
            
            data_encontrada = None
            hora_encontrada = None
            
            for pattern in patterns_data:
                match = re.search(pattern, text_lower, re.IGNORECASE)
                if match:
                    groups = match.groups()
                    if len(groups) == 3:
                        dia, mes_ou_nome, ano = groups
                        
                        if mes_ou_nome in meses:
                            mes = meses[mes_ou_nome]
                        else:
                            mes = mes_ou_nome
                        
                        if len(str(ano)) == 2:
                            ano = f"20{ano}"
                        
                        data_encontrada = f"{dia.zfill(2)}/{mes.zfill(2)}/{ano}"
                        break
            
            for pattern in patterns_hora:
                match = re.search(pattern, text_lower, re.IGNORECASE)
                if match:
                    hora, minuto = match.groups()
                    hora_encontrada = f"{hora.zfill(2)}:{minuto}"
                    break
            
            if data_encontrada:
                logger.info(f"[OCR_SURGICAL] ✅ Audiência encontrada página {page_num}: {data_encontrada} {hora_encontrada or ''}")
                return {
                    'data_audiencia': data_encontrada,
                    'hora_audiencia': hora_encontrada
                }
        
        logger.info("[OCR_SURGICAL] Audiência não encontrada nas páginas mapeadas")
        return None
        
    except Exception as e:
        logger.error(f"[OCR_SURGICAL] Erro ao extrair audiência: {e}")
        return None


def extract_pis_ctps_from_annexes(pdf_path: str) -> Dict[str, Optional[str]]:
    """
    Extrai PIS e CTPS de anexos escaneados (CTPS, TRCT, Ficha de Registro).
    
    PLANO BATMAN - OCR cirúrgico para campos de identificação:
    1. Mapeia estrutura do PDF para identificar páginas de CTPS/TRCT/Ficha
    2. Identifica páginas escaneadas (< 200 chars)
    3. Faz OCR apenas nas páginas relevantes
    4. Aplica regex específicos para PIS e CTPS
    
    Args:
        pdf_path: Caminho do arquivo PDF
    
    Returns:
        Dict com 'pis' e 'ctps' extraídos via OCR
    """
    import re
    from .regex_utils import extract_pis, extract_ctps
    
    logger.info(f"[OCR_SURGICAL] Iniciando extração de PIS/CTPS dos anexos: {pdf_path}")
    log_info("Mapeando anexos para extração cirúrgica de PIS/CTPS", region="OCR_EXTRACTOR")
    
    result = {'pis': None, 'ctps': None}
    
    try:
        mapping = map_pdf_annexes(pdf_path)
        scanned_pages = set(mapping.get('scanned', []))
        
        # Páginas prioritárias para PIS/CTPS (ordem de prioridade)
        priority_sources = [
            ('trct', mapping.get('trct', []), 3),       # TRCT tem PIS/CTPS
            ('ctps', mapping.get('ctps', []), 3),       # CTPS física tem número
            ('ficha_registro', mapping.get('ficha_registro', []), 2),  # Ficha tem PIS
        ]
        
        pages_to_ocr = []
        for source_name, pages, max_pages in priority_sources:
            scanned = [p for p in pages if p in scanned_pages]
            if scanned:
                pages_to_ocr.extend(scanned[:max_pages])
                logger.debug(f"[OCR_SURGICAL] {source_name}: {scanned[:max_pages]}")
        
        # Limitar a 8 páginas para performance
        pages_to_ocr = list(dict.fromkeys(pages_to_ocr))[:8]
        
        # 🆕 FALLBACK: Se não encontrou páginas mapeadas, usar as últimas N páginas escaneadas
        # 2025-11-28: Muitos PDFs têm anexos no final que não são detectados por keywords
        if not pages_to_ocr and scanned_pages:
            # Ordenar e pegar as últimas 5 páginas escaneadas
            sorted_scanned = sorted(scanned_pages)
            pages_to_ocr = sorted_scanned[-5:] if len(sorted_scanned) > 5 else sorted_scanned
            logger.info(f"[OCR_SURGICAL] Fallback: usando últimas {len(pages_to_ocr)} páginas escaneadas: {pages_to_ocr}")
        
        if not pages_to_ocr:
            logger.info("[OCR_SURGICAL] Nenhuma página escaneada encontrada para OCR")
            return result
        
        logger.info(f"[OCR_SURGICAL] Processando {len(pages_to_ocr)} páginas: {pages_to_ocr}")
        
        # OCR nas páginas selecionadas
        texto_ocr = extract_ocr_from_specific_pages(pdf_path, pages_to_ocr, max_pages=8)
        
        if not texto_ocr or len(texto_ocr.strip()) < 50:
            logger.info("[OCR_SURGICAL] OCR retornou texto insuficiente")
            return result
        
        logger.debug(f"[OCR_SURGICAL] OCR extraiu {len(texto_ocr)} chars")
        
        # Extrair PIS
        pis = extract_pis(texto_ocr)
        if pis:
            result['pis'] = pis
            logger.info(f"[OCR_SURGICAL] ✅ PIS encontrado: {pis}")
        
        # Extrair CTPS
        ctps = extract_ctps(texto_ocr)
        if ctps and ctps != "DIGITAL":  # Ignorar "DIGITAL"
            result['ctps'] = ctps
            logger.info(f"[OCR_SURGICAL] ✅ CTPS encontrado: {ctps}")
        
        # Se não encontrou CTPS com regex padrão, tentar padrões específicos de OCR
        if not result['ctps']:
            # Padrões específicos para texto OCR (mais tolerantes a erros)
            ocr_ctps_patterns = [
                r'(\d{5,8})\s*[-/]?\s*(\d{3,6})\s*[-/]?\s*([A-Z]{2})',  # 123456-789/RJ
                r'N[°º]?\s*(\d{5,8})',  # Nº 123456
                r'(?:CTPS|Carteira)[^\d]{0,20}(\d{5,8})',  # CTPS ... 123456
            ]
            
            for pattern in ocr_ctps_patterns:
                match = re.search(pattern, texto_ocr, re.I)
                if match:
                    if match.lastindex >= 3:
                        ctps_val = f"{match.group(1)} série {match.group(2)}/{match.group(3)}"
                    else:
                        ctps_val = match.group(1)
                    
                    result['ctps'] = ctps_val
                    logger.info(f"[OCR_SURGICAL] ✅ CTPS (padrão OCR): {ctps_val}")
                    break
        
        return result
        
    except Exception as e:
        logger.error(f"[OCR_SURGICAL] Erro na extração de PIS/CTPS: {e}")
        return result


def extract_fields_with_ocr(pdf_path: str) -> Dict[str, Optional[str]]:
    """
    Extrai campos trabalhistas de PDF escaneado usando OCR + regex.
    
    Args:
        pdf_path: Caminho do arquivo PDF
    
    Returns:
        Dict com campos extraídos via OCR
    """
    log_info(f"Iniciando extração OCR para PDF escaneado: {pdf_path}", region="OCR_EXTRACTOR")
    
    from .regex_utils import (
        extract_pis, extract_ctps, extract_local_trabalho,
        extract_motivo_demissao, extract_empregador,
        extract_data_admissao, extract_data_demissao, extract_salario
    )
    
    # Extrair texto via OCR
    texto_ocr = extract_text_with_ocr(pdf_path)
    
    if not texto_ocr:
        log_error("OCR não conseguiu extrair texto do PDF", region="OCR_EXTRACTOR")
        return {}
    
    # Aplicar regex patterns no texto OCR
    result = {
        'pis': extract_pis(texto_ocr),
        'ctps': extract_ctps(texto_ocr),
        'local_trabalho': extract_local_trabalho(texto_ocr),
        'motivo_demissao': extract_motivo_demissao(texto_ocr),
        'empregador': extract_empregador(texto_ocr),
        'data_admissao': extract_data_admissao(texto_ocr),
        'data_demissao': extract_data_demissao(texto_ocr),
        'salario': extract_salario(texto_ocr)
    }
    
    extracted_count = len([v for v in result.values() if v])
    log_info(f"OCR extraiu {extracted_count} campos", region="OCR_EXTRACTOR")
    
    return result


# =============================================================================
# INTELIGÊNCIA DE LOCALIZAÇÃO DE ANEXOS
# =============================================================================

def infer_annex_pages_from_history(total_pages: int, docs_needed: set) -> Dict[str, List[int]]:
    """
    Infere páginas prováveis de anexos baseado em dados históricos do banco.
    
    Consulta a tabela AnnexLocation para obter estatísticas de onde cada tipo
    de documento geralmente aparece em PDFs de tamanho similar.
    
    Args:
        total_pages: Total de páginas do PDF atual
        docs_needed: Set de tipos de documento necessários (ctps, trct, contracheque)
    
    Returns:
        Dict com {doc_type: [página1, página2, ...]} ordenado por probabilidade
    """
    from extensions import db
    from models import AnnexLocation
    from sqlalchemy import func
    
    logger = logging.getLogger(__name__)
    result = {}
    
    if not docs_needed or total_pages < 10:
        return result
    
    try:
        for doc_type in docs_needed:
            # Buscar estatísticas para este tipo de documento
            # Agrupar por faixas de tamanho de PDF (0-50, 51-100, 101-150, >150)
            page_range_start = (total_pages // 50) * 50
            page_range_end = page_range_start + 50
            
            stats = db.session.query(
                func.avg(AnnexLocation.page_ratio).label('avg_ratio'),
                func.min(AnnexLocation.page_ratio).label('min_ratio'),
                func.max(AnnexLocation.page_ratio).label('max_ratio'),
                func.count(AnnexLocation.id).label('count')
            ).filter(
                AnnexLocation.doc_type == doc_type,
                AnnexLocation.total_pages >= page_range_start,
                AnnexLocation.total_pages < page_range_end
            ).first()
            
            if stats and stats.count and stats.count >= 3:
                # Temos dados suficientes para inferir
                avg_ratio = stats.avg_ratio
                min_ratio = stats.min_ratio
                max_ratio = stats.max_ratio
                
                # Calcular páginas candidatas
                avg_page = int(total_pages * avg_ratio)
                min_page = max(1, int(total_pages * min_ratio) - 1)
                max_page = min(total_pages, int(total_pages * max_ratio) + 1)
                
                # Retornar 3 candidatos: média, -2, +2
                candidates = sorted(set([
                    max(1, avg_page - 2),
                    avg_page,
                    min(total_pages, avg_page + 2)
                ]))
                
                result[doc_type] = candidates
                logger.info(f"[INFER] {doc_type.upper()}: média página {avg_page} ({avg_ratio:.0%}), range [{min_page}-{max_page}], {stats.count} amostras")
            else:
                # Fallback: usar heurística baseada em padrões típicos de PDFs trabalhistas
                # CTPS geralmente ~80-85% do PDF
                # TRCT geralmente ~85-90% do PDF  
                # Contracheque geralmente ~75-80% do PDF
                defaults = {
                    'ctps': 0.82,
                    'trct': 0.87,
                    'contracheque': 0.77
                }
                if doc_type in defaults:
                    ratio = defaults[doc_type]
                    page = int(total_pages * ratio)
                    result[doc_type] = [max(1, page - 1), page, min(total_pages, page + 1)]
                    logger.info(f"[INFER] {doc_type.upper()}: usando default {ratio:.0%} → página {page}")
    
    except Exception as e:
        logger.warning(f"[INFER] Erro ao consultar histórico: {e}")
    
    return result


def record_annex_location(process_id: int, doc_type: str, page_number: int, 
                          total_pages: int, source: str = "ocr_found") -> bool:
    """
    Registra a localização de um anexo encontrado para uso futuro.
    
    Args:
        process_id: ID do processo
        doc_type: Tipo do documento (ctps, trct, contracheque)
        page_number: Página onde foi encontrado (1-indexed)
        total_pages: Total de páginas do PDF
        source: Origem da informação (bookmark, toc, ocr_found)
    
    Returns:
        True se registrou com sucesso
    """
    from extensions import db
    from models import AnnexLocation
    
    logger = logging.getLogger(__name__)
    
    try:
        # Calcular ratio
        page_ratio = page_number / total_pages
        
        # Definir confiança baseada na fonte
        confidence_map = {
            'bookmark': 1.0,
            'toc': 0.9,
            'ocr_found': 0.7
        }
        confidence = confidence_map.get(source, 0.5)
        
        # Verificar se já existe registro para este processo/doc_type
        existing = AnnexLocation.query.filter_by(
            process_id=process_id,
            doc_type=doc_type
        ).first()
        
        if existing:
            # Atualizar se a nova fonte for mais confiável
            if confidence > existing.confidence:
                existing.page_number = page_number
                existing.total_pages = total_pages
                existing.page_ratio = page_ratio
                existing.source = source
                existing.confidence = confidence
                db.session.commit()
                logger.info(f"[ANNEX_LOC] Atualizado: {doc_type} → pág {page_number}/{total_pages} ({page_ratio:.0%})")
        else:
            # Criar novo registro
            new_loc = AnnexLocation(
                process_id=process_id,
                doc_type=doc_type,
                page_number=page_number,
                total_pages=total_pages,
                page_ratio=page_ratio,
                source=source,
                confidence=confidence
            )
            db.session.add(new_loc)
            db.session.commit()
            logger.info(f"[ANNEX_LOC] Registrado: {doc_type} → pág {page_number}/{total_pages} ({page_ratio:.0%})")
        
        return True
        
    except Exception as e:
        logger.error(f"[ANNEX_LOC] Erro ao registrar localização: {e}")
        db.session.rollback()
        return False


def get_pdf_total_pages(pdf_path: str) -> int:
    """Retorna o total de páginas de um PDF."""
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(pdf_path)
        return len(reader.pages)
    except Exception:
        return 0
