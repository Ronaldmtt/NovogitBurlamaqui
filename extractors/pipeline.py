# -*- coding: utf-8 -*-
import logging
import re
from typing import Dict, Any, Optional, List
from .regex_utils import (
    parse_numero_processo_cnj, extract_estado_sigla, extract_foro,
    parse_vara, parse_celula, parse_cliente_parte,
    assunto_from_text, objeto_from_text, subobj_from_text,
    detect_orgao_origem_instancia, extract_valor_causa, extract_data_distribuicao,
    extract_link_audiencia, extract_subtipo_audiencia, extract_envolvido_audiencia,
    extract_advogados, extract_telefone_parte_adversa, extract_email_parte_adversa,
    extract_prazo, extract_numero_processo_antigo, extract_cpf_cnpj_parte_adversa,
    # Dados trabalhistas
    extract_data_admissao, extract_data_demissao, extract_salario,
    extract_cargo_funcao, extract_empregador, extract_pis, extract_ctps,
    extract_local_trabalho, extract_motivo_demissao,
    # Novos campos
    extract_pedidos, extract_advogado_adverso,
    # Múltiplas reclamadas
    extract_todas_reclamadas
)
from .audiencia import parse_audiencia_inicial
from .brand_map import detect_grupo

# Integração com monitor remoto
try:
    from monitor_integration import log_info, log_error
    MONITOR_AVAILABLE = True
except ImportError:
    MONITOR_AVAILABLE = False
    def log_info(msg, region=""): pass
    def log_error(msg, exc=None, region=""): pass

logger = logging.getLogger(__name__)

ANNEX_HEADER_PATTERNS = [
    r'^Perfil\s*Profissiogr[aá]fico\s*Previdenci[aá]rio',
    r'^ARQUIVO\s*DE\s*M[IÍ]DIA',
    r'^SUM[AÁ]RIO\s*\n.*Documentos',
    r'Página\s+\d+\s+de\s+\d+\s*$.*PPP',
]

def extract_text_from_pdf(path: str, max_pages: int = 30, max_chars: int = 120000) -> str:
    """
    Extrai texto do PDF usando EXTRAÇÃO POR ZONAS INTELIGENTE.
    
    🆕 ESTRATÉGIA DE 3 ZONAS (resolve problema de pedidos no meio do PDF):
    - ZONA PETIÇÃO (páginas 1-35): CNJ, partes, comarca, valor causa, datas, PEDIDOS
    - ZONA TRCT (últimas 15 páginas): Salário, PIS, CTPS, datas admissão/demissão
    
    2025-12-01: AMPLIADO para 35 páginas iniciais porque petições trabalhistas
    costumam ter os pedidos entre páginas 15-35 (após fundamentação).
    
    Isso garante captura de dados mesmo em PDFs com 100+ páginas onde
    o TRCT está no final (ex: página 126 de 138).
    """
    import os
    
    try:
        from PyPDF2 import PdfReader
        
        file_size_mb = os.path.getsize(path) / (1024 * 1024) if os.path.exists(path) else 0
        r = PdfReader(path)
        total_pages = len(r.pages)
        
        # 2025-12-01: Ampliado front_pages para 35 para capturar PEDIDOS
        # Em petições trabalhistas, pedidos geralmente estão entre páginas 15-35
        front_pages = 35
        back_pages = 15
        
        if file_size_mb > 10:
            front_pages = 30  # Ainda amplo para capturar pedidos
            back_pages = 12
            logger.warning(f"[PDF_EXTRACT] ⚠️ PDF GRANDE ({file_size_mb:.1f}MB, {total_pages}p) - Zonas: {front_pages} início + {back_pages} fim")
        elif file_size_mb > 5:
            front_pages = 32
            back_pages = 12
            logger.info(f"[PDF_EXTRACT] 📄 PDF médio ({file_size_mb:.1f}MB, {total_pages}p) - Zonas: {front_pages} início + {back_pages} fim")
        
        texts = []
        pages_read = set()
        
        # ZONA 1: Primeiras páginas (petição + pedidos)
        for i in range(min(front_pages, total_pages)):
            try:
                page_text = r.pages[i].extract_text() or ""
                texts.append(f"[PÁGINA {i+1}]\n{page_text}")
                pages_read.add(i)
            except Exception as page_err:
                logger.warning(f"[PDF_EXTRACT] ⚠️ Erro na página {i+1}: {page_err}")
        
        # ZONA 2: Últimas páginas (TRCT/anexos trabalhistas)
        if total_pages > front_pages:
            back_start = max(front_pages, total_pages - back_pages)
            
            if back_start > front_pages:
                texts.append(f"\n[... páginas {front_pages+1}-{back_start} omitidas ...]\n")
            
            for i in range(back_start, total_pages):
                if i not in pages_read:
                    try:
                        page_text = r.pages[i].extract_text() or ""
                        texts.append(f"[PÁGINA {i+1}]\n{page_text}")
                        pages_read.add(i)
                    except Exception as page_err:
                        logger.warning(f"[PDF_EXTRACT] ⚠️ Erro na página {i+1}: {page_err}")
        
        result = "\n".join(texts)
        
        if len(result) > max_chars:
            result = result[:max_chars]
            logger.debug(f"[PDF_EXTRACT] Truncado para {max_chars} chars")
        
        logger.info(f"[PDF_EXTRACT] ✅ ZONAS: {len(pages_read)} páginas extraídas de {total_pages} total ({file_size_mb:.1f}MB, {len(result)} chars)")
        logger.info(f"[PDF_EXTRACT]    Início: 1-{min(front_pages, total_pages)}, Fim: {max(front_pages, total_pages - back_pages) + 1}-{total_pages}")
        return result
        
    except Exception as e:
        logger.exception("Falha ao ler PDF")
        log_error(f"Erro ao ler PDF: {path}", exc=e, region="EXTRACTOR")
        return ""

def _infer_tipo_processo(texto: str) -> str | None:
    if "P JE" in texto.upper() or "PJE" in texto.upper():
        return "Eletrônico"
    return None

def run_extraction_from_text(texto: str, brand_map_path: Optional[str] = None, filename: Optional[str] = None, celula_options: Optional[List[str]] = None, pdf_path: Optional[str] = None) -> Dict[str, Any]:
    log_info(f"Iniciando extração de dados: {filename or 'N/A'}", region="EXTRACTOR")
    t = texto or ""
    data: Dict[str, Any] = {}

    # 1) CNJ + flags
    cnj = parse_numero_processo_cnj(t)
    if cnj:
        data["numero_processo"] = cnj
        data["cnj_sim"] = True
        data["cnj"] = "Sim"

    # 2) Área/Sub área/Assunto/Objeto
    data["area_direito"] = "Trabalhista"
    # NÃO pré-configura rito - será inferido por parse_header_info()
    data["objeto"] = objeto_from_text(t) or "Verbas rescisórias"
    data["sub_objeto"] = subobj_from_text(t) or ""

    # 3) Localização e órgão - usa parse_header_info() compartilhado
    from .header_parser import parse_header_info
    header_data = parse_header_info(t, filename=filename)
    
    # Comarca, número do órgão e rito vêm do header parser
    if header_data.get("comarca"):
        data["comarca"] = header_data["comarca"]
    if header_data.get("numero_orgao"):
        data["numero_orgao"] = header_data["numero_orgao"]
    if header_data.get("rito"):
        rito = header_data["rito"]
        data["sub_area_direito"] = f"Ação Trabalhista - Rito {rito}"
        data["assunto"] = f"Reclamação Trabalhista No Rito {rito}"
    
    # Estado inferido da comarca ou fallback
    if header_data.get("estado"):
        data["estado"] = header_data["estado"]
    else:
        data["estado"] = extract_estado_sigla(t) or ""
    
    data["foro"]    = extract_foro(t) or ""
    data["vara"]    = parse_vara(t) or ""
    orgao, origem, instancia = detect_orgao_origem_instancia(t)
    if orgao:   data["orgao"] = orgao
    if origem:  data["origem"] = origem
    if instancia: data["instancia"] = instancia

    # 4) Sistema/Tipo de processo
    tipo = _infer_tipo_processo(t)
    if tipo: data["tipo_processo"] = tipo
    if "PJE" in t.upper():
        data["sistema_eletronico"] = "PJE"

    # 5) Cliente vs Parte adversa
    # ✅ NOVA LÓGICA: Extrai cliente do RECLAMADO, não força GPA como padrão
    from .brand_map import normalize_cliente, detect_grupo
    
    # Primeiro extrai as partes sem cliente_hint para obter o nome bruto
    partes = parse_cliente_parte(t, cliente_hint=None)
    data.update(partes)
    
    # ✅ NOVO: Propaga segunda parte interessada para campo do modelo (ex: CSN MINERAÇÃO quando CBSI é principal)
    cliente_secundario_raw = partes.get("cliente_secundario", "")
    if cliente_secundario_raw and cliente_secundario_raw.strip():
        data["outra_reclamada_cliente"] = cliente_secundario_raw.strip()
        logger.debug(f"[MULTI_PARTE] Segunda parte interessada detectada: {cliente_secundario_raw.strip()}")
    
    # ✅ MÚLTIPLAS RECLAMADAS - Extrai TODAS as partes reclamadas para o RPA
    # Formato: [{"nome": "...", "posicao": "RECLAMADO|REU|...", "tipo_pessoa": "fisica|juridica"}]
    # 2025-12-02: Com fallback LLM quando regex não encontra
    reclamadas = extract_todas_reclamadas(t)
    if reclamadas:
        data["reclamadas"] = reclamadas
        logger.info(f"[RECLAMADAS] Extraídas via REGEX: {len(reclamadas)} reclamadas: {[r['nome'][:30] for r in reclamadas]}")
    else:
        # ✅ FALLBACK LLM para reclamadas - quando regex não encontra nenhuma
        logger.info("[RECLAMADAS_LLM] Regex não encontrou reclamadas - tentando LLM...")
        try:
            from .llm_extractor import extract_reclamadas_with_llm
            reclamadas_llm = extract_reclamadas_with_llm(t)
            
            if reclamadas_llm:
                data["reclamadas"] = reclamadas_llm
                logger.info(f"[RECLAMADAS] ✨ LLM encontrou {len(reclamadas_llm)} reclamadas")
            else:
                data["reclamadas"] = []
                logger.debug("[RECLAMADAS] Nenhuma reclamada encontrada (regex + LLM)")
        except Exception as e:
            data["reclamadas"] = []
            logger.debug(f"[RECLAMADAS_LLM] Erro: {e}")
    
    # Extrai o nome bruto do reclamado
    nome_reclamado_bruto = partes.get("nome_reclamado", "")
    
    # Normaliza o cliente usando o mapeamento de aliases
    cliente_normalizado = normalize_cliente(nome_reclamado_bruto) if nome_reclamado_bruto else "Cliente Não Identificado"
    
    # Define cliente e cliente_grupo
    data["cliente"] = cliente_normalizado
    
    # cliente_grupo só é preenchido se for um grupo conhecido (ex: GPA, Casas Bahia)
    # Para GPA: "Grupo Pão de Açúcar", para outros: o próprio cliente normalizado
    if detect_grupo(nome_reclamado_bruto):
        data["cliente_grupo"] = "Grupo Pão de Açúcar"
    else:
        data["cliente_grupo"] = cliente_normalizado

    # 6) Notificação/Audiência
    if "intima" in t.lower():
        data["tipo_notificacao"] = "Intimação"
    
    # 🔍 DEBUG: Verificar se texto contém palavras-chave de audiência
    texto_lower = t.lower()
    tem_audiencia = 'audiência' in texto_lower or 'audiencia' in texto_lower
    tem_inicial = 'inicial' in texto_lower
    tem_una = 'una' in texto_lower
    tem_data_possivel = any(f"{d:02d}/" in t for d in range(1, 32))
    logger.debug(f"[AUDIENCIA_DEBUG] Texto tem: audiência={tem_audiencia}, inicial={tem_inicial}, una={tem_una}, data_possivel={tem_data_possivel}")
    
    ai = parse_audiencia_inicial(t)
    if ai:
        data["audiencia_inicial"] = ai
        data["cadastrar_primeira_audiencia"] = True  # ✅ Marcar automaticamente quando audiência for detectada
        logger.info(f"[AUDIENCIA_INICIAL] ✅ Detectada: {ai} - cadastrar_primeira_audiencia=True")
    elif pdf_path:
        # ✅ FALLBACK OCR: Tenta extrair audiência de páginas escaneadas via mapeamento cirúrgico
        try:
            from .ocr_utils import extract_audiencia_from_mapping
            aud_ocr = extract_audiencia_from_mapping(pdf_path)
            if aud_ocr:
                data_aud = aud_ocr.get('data_audiencia', '')
                hora_aud = aud_ocr.get('hora_audiencia', '')
                if data_aud:
                    ai_combined = f"{data_aud} {hora_aud}".strip() if hora_aud else data_aud
                    data["audiencia_inicial"] = ai_combined
                    data["cadastrar_primeira_audiencia"] = True
                    logger.info(f"[AUDIENCIA_INICIAL_OCR] ✅ Detectada via OCR: {ai_combined}")
        except Exception as e:
            logger.debug(f"[AUDIENCIA_INICIAL_OCR] Fallback OCR não disponível: {e}")
    
    # ✅ Extrair link de audiência telepresencial (Zoom, Meet, Teams)
    link_aud = extract_link_audiencia(t)
    if link_aud:
        data["link_audiencia"] = link_aud
        logger.debug(f"[LINK_AUDIENCIA] Detectado: {link_aud}")
    
    # ✅ Extrair subtipo de audiência (Una, Não-Una, Tentativa Conciliação, etc)
    subtipo_aud = extract_subtipo_audiencia(t)
    if subtipo_aud:
        data["subtipo_audiencia"] = subtipo_aud
        logger.debug(f"[SUBTIPO_AUDIENCIA] Detectado: {subtipo_aud}")
    
    # ✅ Extrair envolvidos da audiência (Advogado, Preposto, Advogado e Preposto, etc)
    envolvido_aud = extract_envolvido_audiencia(t)
    if envolvido_aud:
        data["envolvido_audiencia"] = envolvido_aud
        logger.debug(f"[ENVOLVIDO_AUDIENCIA] Detectado: {envolvido_aud}")

    # 7) Data de Distribuição
    data_dist = extract_data_distribuicao(t)
    if data_dist:
        data["data_distribuicao"] = data_dist
        logger.debug(f"[DATA_DISTRIBUICAO] Extraída: {data_dist}")
    
    # ✅ VALIDAÇÃO: Audiência DEVE ser POSTERIOR à Distribuição
    # 2025-11-28: Plano Batman - validação preventiva para evitar datas iguais/invertidas
    if data.get("audiencia_inicial") and data.get("data_distribuicao"):
        from datetime import datetime
        try:
            aud_str = data["audiencia_inicial"]
            dist_str = data["data_distribuicao"]
            
            # Extrair apenas a data da audiência (pode ter hora: "25/12/2025 09:00")
            if ' ' in aud_str:
                aud_date_str = aud_str.split()[0]
            else:
                aud_date_str = aud_str
            
            # Normalizar formatos (DD/MM/YYYY ou YYYY-MM-DD)
            if '-' in aud_date_str and len(aud_date_str.split('-')[0]) == 4:
                dt_aud = datetime.strptime(aud_date_str, '%Y-%m-%d')
            else:
                dt_aud = datetime.strptime(aud_date_str, '%d/%m/%Y')
            
            if '-' in dist_str and len(dist_str.split('-')[0]) == 4:
                dt_dist = datetime.strptime(dist_str, '%Y-%m-%d')
            else:
                dt_dist = datetime.strptime(dist_str, '%d/%m/%Y')
            
            # Regra: Audiência deve ser ESTRITAMENTE POSTERIOR à distribuição
            if dt_aud.date() <= dt_dist.date():
                logger.warning(f"[VALIDACAO_AUDIENCIA] ⚠️ Audiência ({aud_str}) é IGUAL OU ANTERIOR à Distribuição ({dist_str}) - REMOVENDO audiência!")
                # Remover a audiência inválida
                del data["audiencia_inicial"]
                if "cadastrar_primeira_audiencia" in data:
                    data["cadastrar_primeira_audiencia"] = False
            else:
                dias_diff = (dt_aud.date() - dt_dist.date()).days
                logger.debug(f"[VALIDACAO_AUDIENCIA] ✅ Audiência ({aud_str}) é {dias_diff} dias após Distribuição ({dist_str})")
        except Exception as e:
            logger.warning(f"[VALIDACAO_AUDIENCIA] Erro ao validar datas: {e}")
    
    # 8) Valor da Causa
    valor_causa = extract_valor_causa(t)
    if valor_causa:
        data["valor_causa"] = valor_causa
        logger.debug(f"[VALOR_CAUSA] Extraído: {valor_causa}")
    
    # 9) Observação (ex.: Segredo de justiça)
    if "segredo de justiça" in t.lower():
        data["observacao"] = "Segredo de justiça"

    # 10) ✅ NOVOS CAMPOS ADICIONAIS
    # Advogados (autor e réu)
    advogado_autor, advogado_reu = extract_advogados(t)
    if advogado_autor:
        data["advogado_autor"] = advogado_autor
    if advogado_reu:
        data["advogado_reu"] = advogado_reu
    
    # CPF/CNPJ da parte adversa (com verificação rigorosa de contexto)
    cpf_cnpj = extract_cpf_cnpj_parte_adversa(t)
    if cpf_cnpj:
        data["cpf_cnpj_parte_adversa"] = cpf_cnpj
    
    # Telefone e email da parte adversa
    telefone = extract_telefone_parte_adversa(t)
    if telefone:
        data["telefone_parte_adversa"] = telefone
    
    email = extract_email_parte_adversa(t)
    if email:
        data["email_parte_adversa"] = email
    
    # Prazo
    prazo = extract_prazo(t)
    if prazo:
        data["prazo"] = prazo
    
    # Número de processo antigo
    num_antigo = extract_numero_processo_antigo(t)
    if num_antigo:
        data["numero_processo_antigo"] = num_antigo
    
    # 11) ✅ DADOS TRABALHISTAS (admissão, demissão, salário, cargo, PIS, CTPS, local, motivo)
    data_admissao = extract_data_admissao(t)
    if data_admissao:
        data["data_admissao"] = data_admissao
        logger.debug(f"[DATA_ADMISSAO] Extraída: {data_admissao}")
    
    data_demissao = extract_data_demissao(t)
    if data_demissao:
        data["data_demissao"] = data_demissao
        logger.debug(f"[DATA_DEMISSAO] Extraída: {data_demissao}")
    
    # ✅ VALIDAÇÃO DE DATAS TRABALHISTAS COM CONSOLIDAÇÃO INTELIGENTE
    # 2025-11-28: Regras de consolidação cautelosas (Plano Batman)
    # - Data de demissão DEVE estar entre admissão e distribuição
    # - Rejeitar datas futuras (audiências) e muito antigas
    # - Usar data de distribuição como limite máximo para demissão
    from datetime import datetime
    dt_hoje = datetime.now()
    
    # Extrair data de distribuição para usar como limite máximo
    data_distribuicao = extract_data_distribuicao(t)
    dt_distribuicao = None
    if data_distribuicao:
        try:
            dt_distribuicao = datetime.strptime(data_distribuicao, '%d/%m/%Y')
            data["data_distribuicao"] = data_distribuicao
            logger.debug(f"[DATA_DISTRIBUICAO] Extraída: {data_distribuicao} (usada como limite máximo)")
        except ValueError:
            pass
    
    def validar_data_trabalhista(data_str: str, campo: str, limite_max: datetime = None) -> str | None:
        """
        Valida se uma data trabalhista é plausível.
        
        Regras:
        1. Não pode ser futura (> hoje)
        2. Não pode ser muito antiga (< 1950)
        3. Se limite_max fornecido, não pode ultrapassá-lo (ex: demissão < distribuição)
        
        Retorna None se impossível.
        """
        if not data_str:
            return None
        try:
            dt = datetime.strptime(data_str, '%d/%m/%Y')
            
            # Regra 1: Datas no futuro são impossíveis para eventos passados
            if dt > dt_hoje:
                logger.warning(f"[VALIDACAO_DATAS] ⚠️ {campo} ({data_str}) é FUTURA - REMOVENDO!")
                return None
            
            # Regra 2: Datas muito antigas (antes de 1950) são suspeitas
            if dt.year < 1950:
                logger.warning(f"[VALIDACAO_DATAS] ⚠️ {campo} ({data_str}) é MUITO ANTIGA (< 1950) - REMOVENDO!")
                return None
            
            # Regra 3: Demissão não pode ser após distribuição do processo
            # (seria impossível - o trabalhador processa antes de ser demitido?!)
            if limite_max and dt >= limite_max:
                logger.warning(f"[VALIDACAO_DATAS] ⚠️ {campo} ({data_str}) é IGUAL OU POSTERIOR à distribuição ({limite_max.strftime('%d/%m/%Y')}) - REMOVENDO!")
                return None
            
            return data_str
        except ValueError:
            return None
    
    # Validar admissão (sem limite máximo além de "hoje")
    if data.get('data_admissao'):
        data["data_admissao"] = validar_data_trabalhista(data["data_admissao"], "Admissão")
    
    # Validar demissão COM limite máximo da distribuição
    # Regra cautelosa: demissão deve ser < distribuição (não pode processar antes de ser demitido)
    if data.get('data_demissao'):
        data["data_demissao"] = validar_data_trabalhista(
            data["data_demissao"], 
            "Demissão",
            limite_max=dt_distribuicao  # Usar distribuição como teto
        )
    
    # Se ambas as datas existem e são válidas, verificar ordem cronológica
    if data.get('data_admissao') and data.get('data_demissao'):
        try:
            dt_admissao = datetime.strptime(data['data_admissao'], '%d/%m/%Y')
            dt_demissao = datetime.strptime(data['data_demissao'], '%d/%m/%Y')
            
            if dt_admissao > dt_demissao:
                # Datas invertidas - corrigir se diferença razoável
                diferenca_anos = abs((dt_admissao - dt_demissao).days) / 365
                if diferenca_anos < 50:
                    logger.warning(f"[VALIDACAO_DATAS] ❌ Admissão ({data['data_admissao']}) POSTERIOR à Demissão ({data['data_demissao']}) - INVERTENDO!")
                    data["data_admissao"], data["data_demissao"] = data["data_demissao"], data["data_admissao"]
                    logger.info(f"[VALIDACAO_DATAS] ✅ Datas corrigidas: Admissão={data['data_admissao']}, Demissão={data['data_demissao']}")
                else:
                    logger.warning(f"[VALIDACAO_DATAS] ⚠️ Diferença muito grande ({diferenca_anos:.0f} anos) - possível erro de extração")
        except ValueError as e:
            logger.warning(f"[VALIDACAO_DATAS] Erro ao comparar datas: {e}")
    
    salario = extract_salario(t)
    if salario:
        data["salario"] = salario
        logger.debug(f"[SALARIO] Extraído: {salario}")
    
    cargo = extract_cargo_funcao(t)
    if cargo:
        data["cargo_funcao"] = cargo
        logger.debug(f"[CARGO_FUNCAO] Extraído: {cargo}")
    
    empregador = extract_empregador(t)
    if empregador:
        data["empregador"] = empregador
        logger.debug(f"[EMPREGADOR] Extraído: {empregador}")
    else:
        # FALLBACK: usar RECLAMADO quando não encontrar nome explícito no contexto de admissão
        # Comum em processos que dizem apenas "admitido pela primeira reclamada"
        if data.get("nome_reclamado"):
            data["empregador"] = data["nome_reclamado"]
            logger.debug(f"[EMPREGADOR] Usando RECLAMADO como fallback: {data['empregador']}")
    
    pis = extract_pis(t)
    if pis:
        data["pis"] = pis
        logger.debug(f"[PIS] Extraído: {pis}")
    
    ctps = extract_ctps(t)
    if ctps:
        data["ctps"] = ctps
        logger.debug(f"[CTPS] Extraído: {ctps}")
    
    local_trabalho = extract_local_trabalho(t)
    if local_trabalho:
        data["local_trabalho"] = local_trabalho
        logger.debug(f"[LOCAL_TRABALHO] Extraído via REGEX: {local_trabalho}")
    
    motivo_demissao = extract_motivo_demissao(t)
    if motivo_demissao:
        data["motivo_demissao"] = motivo_demissao
        logger.debug(f"[MOTIVO_DEMISSAO] Extraído via REGEX: {motivo_demissao}")
    
    # 12) ✅ PEDIDOS (lista estruturada da seção "DOS PEDIDOS")
    # 2025-12-02: Com fallback LLM APENAS quando regex não encontra NENHUM pedido
    pedidos = extract_pedidos(t)
    if pedidos:
        data["pedidos"] = pedidos
        logger.debug(f"[PEDIDOS] Extraídos via REGEX: {len(pedidos)} pedidos")
    else:
        # ✅ FALLBACK LLM para pedidos - APENAS quando regex não encontra nenhum
        logger.info("[PEDIDOS_LLM] Regex não encontrou pedidos - tentando LLM...")
        try:
            from .llm_extractor import extract_pedidos_with_llm
            pedidos_llm = extract_pedidos_with_llm(t)
            
            if pedidos_llm:
                # Extrair descrições e garantir formato consistente
                pedidos_descricoes = []
                for p in pedidos_llm:
                    if isinstance(p, dict) and p.get("descricao"):
                        pedidos_descricoes.append(p["descricao"])
                
                if pedidos_descricoes:
                    data["pedidos"] = pedidos_descricoes
                    data["pedidos_categorias"] = pedidos_llm  # Manter categorias para uso futuro
                    logger.info(f"[PEDIDOS] ✨ LLM encontrou {len(pedidos_descricoes)} pedidos")
        except Exception as e:
            logger.debug(f"[PEDIDOS_LLM] Erro: {e}")
    
    # 13) ✅ ADVOGADO DA PARTE ADVERSA (empregador)
    adv_adverso_nome, adv_adverso_oab = extract_advogado_adverso(t)
    if adv_adverso_nome:
        data["advogado_adverso_nome"] = adv_adverso_nome
        logger.debug(f"[ADV_ADVERSO] Nome: {adv_adverso_nome}")
    if adv_adverso_oab:
        data["advogado_adverso_oab"] = adv_adverso_oab
        logger.debug(f"[ADV_ADVERSO] OAB: {adv_adverso_oab}")
    
    # ✅ FALLBACK LLM - Campos Trabalhistas Críticos
    # Chama LLM se QUALQUER campo trabalhista crítico estiver faltando
    # Campos: salário, local_trabalho, pis, ctps, motivo_demissao
    
    campos_faltantes_criticos = []
    if not salario: campos_faltantes_criticos.append("salario")
    if not local_trabalho: campos_faltantes_criticos.append("local_trabalho")
    if not pis: campos_faltantes_criticos.append("pis")
    if not ctps: campos_faltantes_criticos.append("ctps")
    if not motivo_demissao: campos_faltantes_criticos.append("motivo_demissao")
    
    # Chamar LLM se qualquer campo trabalhista crítico estiver faltando
    if campos_faltantes_criticos:
        logger.info(f"[LLM_FALLBACK] Campos críticos faltantes: {campos_faltantes_criticos} - chamando LLM...")
        from .llm_extractor import extract_labor_fields_with_llm
        llm_data = extract_labor_fields_with_llm(t)
        
        if llm_data:
            if not local_trabalho and llm_data.get("local_trabalho"):
                data["local_trabalho"] = llm_data["local_trabalho"]
                logger.info(f"[LOCAL_TRABALHO] ✨ Recuperado via LLM: {llm_data['local_trabalho']}")
            
            if not salario and llm_data.get("salario"):
                data["salario"] = llm_data["salario"]
                logger.info(f"[SALARIO] ✨ Recuperado via LLM: {llm_data['salario']}")
            
            # Aproveitar para preencher outros campos se vieram na mesma chamada
            if not motivo_demissao and llm_data.get("motivo_demissao"):
                data["motivo_demissao"] = llm_data["motivo_demissao"]
                logger.info(f"[MOTIVO_DEMISSAO] ✨ Recuperado via LLM: {llm_data['motivo_demissao']}")
            
            if not pis and llm_data.get("pis"):
                data["pis"] = llm_data["pis"]
                logger.info(f"[PIS] ✨ Recuperado via LLM: {llm_data['pis']}")
            
            if not ctps and (llm_data.get("ctps_numero") or llm_data.get("ctps_serie_uf")):
                if llm_data.get("ctps_numero") and llm_data.get("ctps_serie_uf"):
                    data["ctps"] = f"{llm_data['ctps_numero']} série {llm_data['ctps_serie_uf']}"
                elif llm_data.get("ctps_numero"):
                    data["ctps"] = llm_data["ctps_numero"]
                logger.info(f"[CTPS] ✨ Recuperado via LLM: {data.get('ctps', 'N/A')}")
    else:
        logger.debug("[LLM_SKIP] Regex extraiu campos críticos - LLM não necessário")
    
    # ✅ CAMADA 4: OCR SELETIVO INTELIGENTE (2025-12-01 - Plano Batman)
    # Aplica OCR APENAS em páginas escaneadas detectadas automaticamente
    # Evita OCR cego em 8 páginas - otimiza tempo sem perder cobertura
    
    campos_ocr = []
    if not data.get("salario"): campos_ocr.append("salario")
    if not data.get("pis"): campos_ocr.append("pis")
    if not data.get("ctps"): campos_ocr.append("ctps")
    
    if campos_ocr and pdf_path:
        try:
            from .ocr_utils import detect_scanned_pages, ocr_extract_from_pages
            
            # Detectar páginas escaneadas (text_len < 100)
            scanned_pages = detect_scanned_pages(pdf_path)
            
            if scanned_pages:
                logger.info(f"[OCR_BATMAN] Detectadas {len(scanned_pages)} páginas escaneadas: {scanned_pages}")
                # OCR apenas nas páginas escaneadas (máx 5 para performance)
                target_pages = scanned_pages[-5:]  # Últimas 5 páginas escaneadas (TRCT/contracheques)
                ocr_result = ocr_extract_from_pages(pdf_path, target_pages)
                
                if ocr_result:
                    if not data.get("salario") and ocr_result.get("salario"):
                        data["salario"] = ocr_result["salario"]
                        logger.info(f"[SALARIO] 📷 OCR cirúrgico: {ocr_result['salario']}")
                    
                    if not data.get("pis") and ocr_result.get("pis"):
                        data["pis"] = ocr_result["pis"]
                        logger.info(f"[PIS] 📷 OCR cirúrgico: {ocr_result['pis']}")
                    
                    if not data.get("ctps") and ocr_result.get("ctps"):
                        data["ctps"] = ocr_result["ctps"]
                        logger.info(f"[CTPS] 📷 OCR cirúrgico: {ocr_result['ctps']}")
            else:
                logger.debug(f"[OCR_SKIP] Campos {campos_ocr} vazios mas PDF é 100% texto nativo - OCR não necessário")
        except Exception as e:
            logger.debug(f"[OCR_FALLBACK] Erro: {e}")
    elif campos_ocr:
        logger.debug(f"[OCR_SKIP] Campos {campos_ocr} vazios mas pdf_path não fornecido")

    # 12) Pós-processamento (inclui inferência de célula a partir do cliente)
    from .postprocess import full_postprocess
    data = full_postprocess(data, t, celula_options=celula_options or [])
    
    # NOTA: Validação LLM disponível via validate_extracted_data_with_llm()
    # Chamada manual quando necessário - não automática para evitar chamadas extras

    logger.debug("Resultado (pipeline): %s", data)
    
    # Enviar log de conclusão para monitor
    extracted_count = len([v for v in data.values() if v])
    log_info(f"Extração concluída: {extracted_count} campos extraídos ({filename or 'N/A'})", region="EXTRACTOR")
    
    return data

def run_extraction_from_file(path: str, brand_map_path: Optional[str] = None, filename: Optional[str] = None) -> Dict[str, Any]:
    if not filename:
        import os
        filename = os.path.basename(path)
    return run_extraction_from_text(
        extract_text_from_pdf(path), 
        brand_map_path=brand_map_path, 
        filename=filename,
        pdf_path=path  # ✅ Passar caminho do PDF para habilitar OCR fallback
    )

def run_pipeline_from_text(text: str, celula_options: List[str] | None = None, filename: Optional[str] = None, pdf_path: Optional[str] = None, brand_map_path: Optional[str] = None) -> Dict[str, Any]:
    data = run_extraction_from_text(
        text, 
        filename=filename, 
        celula_options=celula_options,
        pdf_path=pdf_path,  # ✅ Propagar pdf_path para habilitar OCR
        brand_map_path=brand_map_path  # ✅ Propagar brand_map_path para normalização de clientes
    )
    # Preenche orgao/origem apenas se faltarem
    orgao, origem, instancia_guess = detect_orgao_origem_instancia(text or "")
    if orgao and not data.get("orgao"):
        data["orgao"] = orgao
    if origem and not data.get("origem"):
        data["origem"] = origem
    # NÃO sobrescreva 'instancia' se o postprocess já definiu
    if instancia_guess and not data.get("instancia"):
        data["instancia"] = instancia_guess
    return data

# compat com o resto do projeto
run_pipeline = run_pipeline_from_text