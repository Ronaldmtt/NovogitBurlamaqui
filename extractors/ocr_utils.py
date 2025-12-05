# extractors/ocr_utils.py
"""
Módulo OCR para extração de PDFs escaneados usando Tesseract.
Aplica OCR seletivo apenas quando texto nativo é insuficiente.
"""
import logging
import os
from typing import Optional, Dict, List
from pdf2image import convert_from_path
import pytesseract
from PIL import Image

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
                                  missing_fields: List[str]) -> Dict[str, str]:
    """
    Resolve campos trabalhistas faltantes usando OCR seletivo via bookmarks.
    
    2025-12-04: Nova camada de fallback inteligente.
    2025-12-05: OTIMIZAÇÃO - Usa bookmarks do PDF primeiro (OCR em 1-2 páginas apenas)
    
    Estratégia (ordem de prioridade):
    1. Extrai bookmarks do PDF (PDFs PJe têm links diretos para cada anexo)
    2. Se não encontrar, analisa sumário textual
    3. Se não encontrar, usa heurística de páginas escaneadas
    4. Aplica OCR apenas nas páginas identificadas (mínimo possível)
    
    Args:
        pdf_path: Caminho do PDF
        current_data: Dados já extraídos (para não sobrescrever)
        missing_fields: Lista de campos faltantes ["salario", "pis", "data_admissao", etc]
    
    Returns:
        Dict com campos recuperados via OCR seletivo
    """
    import re
    
    result = {}
    logger = logging.getLogger(__name__)
    
    if not missing_fields or not pdf_path:
        return result
    
    logger.info(f"[OCR_SELETIVO] Iniciando fallback para: {missing_fields}")
    
    target_pages = set()
    
    # Mapeamento de campo → tipos de documento que contêm o campo
    field_to_doc = {
        "salario": ["contracheque", "trct"],
        "data_admissao": ["ctps", "trct"],
        "data_demissao": ["trct"],
        "pis": ["ctps", "trct"],
        "ctps": ["ctps", "trct"],
        "serie_ctps": ["ctps"],
    }
    
    # Carregar todas as fontes de mapeamento uma vez
    bookmarks = extract_pdf_bookmarks(pdf_path)
    toc_pages = parse_toc_from_pdf(pdf_path)
    scanned_pages = None  # Lazy load
    
    if bookmarks:
        logger.info(f"[OCR_SELETIVO] ✅ Bookmarks disponíveis: {bookmarks}")
    if any(v for v in toc_pages.values()):
        logger.info(f"[OCR_SELETIVO] ✅ TOC disponível: {toc_pages}")
    
    # ===== ESTRATÉGIA POR CAMPO: Fallback hierárquico para CADA campo =====
    fields_resolved = {}
    
    for field in missing_fields:
        doc_types = field_to_doc.get(field, [])
        page_found = None
        source = None
        
        # PRIORIDADE 1: Tentar bookmarks primeiro
        for doc_type in doc_types:
            if doc_type in bookmarks:
                page_found = bookmarks[doc_type]
                source = f"bookmark:{doc_type}"
                break
        
        # PRIORIDADE 2: Tentar TOC se bookmark não encontrou
        if not page_found:
            for doc_type in doc_types:
                if toc_pages.get(doc_type):
                    page_found = toc_pages[doc_type][0]
                    source = f"toc:{doc_type}"
                    break
        
        # PRIORIDADE 3: Heurística se nada encontrou (lazy load)
        if not page_found:
            if scanned_pages is None:
                scanned_pages = detect_scanned_pages(pdf_path)
            if scanned_pages:
                # Pegar primeiras 3 + últimas 2 páginas escaneadas
                first_pages = scanned_pages[:3]
                last_pages = scanned_pages[-2:] if len(scanned_pages) > 3 else []
                heuristic_pages = list(set(first_pages + last_pages))
                if heuristic_pages:
                    page_found = heuristic_pages[0]  # Pegar primeira
                    source = "heuristic"
                    # Adicionar todas as heurísticas para campos não mapeados
                    for hp in heuristic_pages:
                        target_pages.add(hp)
        
        if page_found:
            target_pages.add(page_found)
            fields_resolved[field] = source
            logger.debug(f"[OCR_SELETIVO] {field} → página {page_found} via {source}")
    
    if fields_resolved:
        logger.info(f"[OCR_SELETIVO] Campos mapeados: {fields_resolved}")
    
    if not target_pages:
        logger.debug("[OCR_SUMARIO] Nenhuma página alvo identificada")
        return result
    
    target_list = sorted(list(target_pages))[:5]
    logger.info(f"[OCR_SUMARIO] 📷 Aplicando OCR nas páginas: {target_list}")
    
    try:
        texto_ocr = ""
        for page_num in target_list:
            try:
                images = convert_from_path(
                    pdf_path,
                    dpi=200,
                    first_page=page_num,
                    last_page=page_num,
                    poppler_path=POPPLER_PATH
                )
                
                for img in images:
                    img_gray = img.convert('L')
                    config = '--psm 6 -l por+eng'
                    texto_pagina = pytesseract.image_to_string(img_gray, config=config)
                    texto_ocr += f"\n--- PÁGINA {page_num} ---\n{texto_pagina}"
                    logger.debug(f"[OCR_SUMARIO] Página {page_num}: {len(texto_pagina)} chars extraídos")
            except Exception as e:
                logger.warning(f"[OCR_SUMARIO] Erro página {page_num}: {e}")
        
        if not texto_ocr:
            return result
        
        logger.debug(f"[OCR_SUMARIO] Total texto OCR: {len(texto_ocr)} chars")
        
        if "salario" in missing_fields:
            salario_patterns = [
                r'(?:sal[aá]rio\s*(?:base|contratual|mensal)?|remunera[çc][ãa]o(?:\s*mensal)?)[:\s]*R?\$?\s*([\d]{1,3}(?:[.,]\d{3})*[,\.]\d{2})',
                r'(?:maior\s*remunera[çc][ãa]o|base\s*de\s*c[aá]lculo)[:\s]*R?\$?\s*([\d]{1,3}(?:[.,]\d{3})*[,\.]\d{2})',
                r'(?:vencimento|proventos)[:\s]*R?\$?\s*([\d]{1,3}(?:[.,]\d{3})*[,\.]\d{2})',
                r'(?:total\s*bruto|bruto)[:\s]*R?\$?\s*([\d]{1,3}(?:[.,]\d{3})*[,\.]\d{2})',
                r'R\$\s*([\d]{1,3}(?:\.\d{3})*,\d{2})',
            ]
            for pattern in salario_patterns:
                m = re.search(pattern, texto_ocr, re.IGNORECASE)
                if m:
                    val_str = m.group(1).replace('.', '').replace(',', '.')
                    try:
                        val = float(val_str)
                        if 1000 <= val <= 100000:
                            result["salario"] = f"R$ {m.group(1)}"
                            logger.info(f"[OCR_SUMARIO] ✅ Salário: {result['salario']}")
                            break
                    except:
                        pass
        
        if "data_admissao" in missing_fields:
            admissao_patterns = [
                r'(?:data\s*(?:de\s*)?admiss[ãa]o|admitido\s*em|in[ií]cio\s*(?:do\s*)?contrato)[:\s]*(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})',
                r'admiss[ãa]o[:\s]*(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})',
            ]
            for pattern in admissao_patterns:
                m = re.search(pattern, texto_ocr, re.IGNORECASE)
                if m:
                    result["data_admissao"] = m.group(1)
                    logger.info(f"[OCR_SUMARIO] ✅ Data Admissão: {result['data_admissao']}")
                    break
        
        if "data_demissao" in missing_fields:
            demissao_patterns = [
                r'(?:data\s*(?:de\s*)?(?:demiss[ãa]o|desligamento|sa[ií]da|rescis[ãa]o)|demitido\s*em|t[eé]rmino\s*(?:do\s*)?contrato)[:\s]*(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})',
                r'(?:aviso\s*pr[eé]vio\s*(?:at[eé]|fim)|[uú]ltimo\s*dia\s*trabalhado)[:\s]*(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})',
            ]
            for pattern in demissao_patterns:
                m = re.search(pattern, texto_ocr, re.IGNORECASE)
                if m:
                    result["data_demissao"] = m.group(1)
                    logger.info(f"[OCR_SUMARIO] ✅ Data Demissão: {result['data_demissao']}")
                    break
        
        if "pis" in missing_fields:
            pis_patterns = [
                r'(?:PIS|PASEP|NIT|PIS/PASEP)[:\s/]*(\d{3}[.\s]?\d{5}[.\s]?\d{2}[.\s-]?\d)',
                r'\b(\d{3}\.\d{5}\.\d{2}[.-]\d)\b',
                r'\b(\d{11})\b',
            ]
            for pattern in pis_patterns:
                m = re.search(pattern, texto_ocr, re.IGNORECASE)
                if m:
                    pis_raw = re.sub(r'[^\d]', '', m.group(1))
                    if len(pis_raw) == 11:
                        result["pis"] = f"{pis_raw[:3]}.{pis_raw[3:8]}.{pis_raw[8:10]}-{pis_raw[10]}"
                        logger.info(f"[OCR_SUMARIO] ✅ PIS: {result['pis']}")
                        break
        
        if "ctps" in missing_fields:
            ctps_patterns = [
                r'(?:CTPS|Carteira\s*(?:de\s*)?Trabalho)[:\s]*[nN]?[º°]?\s*(\d{5,7})[/\s,]*(?:s[eé]rie|série)[:\s]*(\d{3,5})(?:[/\s-]*([A-Z]{2}))?',
                r'[nN]?[º°]?\s*(\d{5,7})[/\s]*[sS][eéE][rR][iI][eE][:\s]*(\d{3,5})(?:[/\s-]*([A-Z]{2}))?',
            ]
            for pattern in ctps_patterns:
                m = re.search(pattern, texto_ocr, re.IGNORECASE)
                if m:
                    numero = m.group(1)
                    serie = m.group(2)
                    uf = m.group(3) if len(m.groups()) >= 3 and m.group(3) else None
                    if uf:
                        result["ctps"] = f"{numero} série {serie}-{uf}"
                    else:
                        result["ctps"] = f"{numero} série {serie}"
                    logger.info(f"[OCR_SUMARIO] ✅ CTPS: {result['ctps']}")
                    break
        
        if result:
            logger.info(f"[OCR_SUMARIO] 🎯 Recuperados {len(result)} campos via OCR seletivo: {list(result.keys())}")
        else:
            logger.debug("[OCR_SUMARIO] Nenhum campo recuperado via OCR")
        
        return result
        
    except Exception as e:
        logger.error(f"[OCR_SUMARIO] ❌ Erro no OCR seletivo: {e}")
        return result


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
                images = convert_from_path(
                    pdf_path,
                    dpi=150,
                    first_page=page_num,
                    last_page=page_num,
                    poppler_path=POPPLER_PATH
                )
                
                for img in images:
                    img_gray = img.convert('L')
                    config = '--psm 6 -l por+eng'
                    texto_pagina = pytesseract.image_to_string(img_gray, config=config)
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
        
        # Converter PDF para imagens (primeiras N páginas)
        images = convert_from_path(
            pdf_path, 
            dpi=300,  # Alta resolução para melhor OCR
            first_page=1,
            last_page=first_pages,
            poppler_path=POPPLER_PATH
        )
        
        logger.info(f"[OCR] Converteu {len(images)} páginas para imagem")
        
        # Aplicar OCR em cada página
        texto_completo = []
        for i, img in enumerate(images, 1):
            # Pré-processamento: converter para escala de cinza
            img_gray = img.convert('L')
            
            # OCR com Tesseract (pt-BR + eng)
            config = '--psm 6 -l por+eng'  # PSM 6 = blocos de texto, português + inglês
            texto_pagina = pytesseract.image_to_string(img_gray, config=config)
            
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
        
        images = convert_from_path(
            pdf_path,
            dpi=150,
            first_page=start_page,
            last_page=total_pages,
            poppler_path=POPPLER_PATH
        )
        
        texto_ocr = ""
        for i, img in enumerate(images, start_page):
            img_gray = img.convert('L')
            config = '--psm 6 -l por+eng'
            texto_pagina = pytesseract.image_to_string(img_gray, config=config)
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
