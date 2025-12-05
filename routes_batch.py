"""
Rotas para processamento em lote de PDFs
"""
import os
import json
import logging
from datetime import datetime
from pathlib import Path
from werkzeug.utils import secure_filename
from flask import Blueprint, request, render_template, redirect, url_for, flash, jsonify, send_from_directory
from flask_login import login_required, current_user
from extensions import db
from models import BatchUpload, BatchItem, Process

logger = logging.getLogger(__name__)

# Integração com monitor remoto
try:
    from monitor_integration import log_info, log_error
    MONITOR_AVAILABLE = True
except ImportError:
    MONITOR_AVAILABLE = False
    def log_info(msg, region=""): pass
    def log_error(msg, exc=None, region=""): pass

batch_bp = Blueprint("batch", __name__, url_prefix="/processos/batch")

# Configurações
ALLOWED_EXTENSIONS = {'pdf'}
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB por arquivo (suporte a PDFs grandes)
MAX_FILES_PER_BATCH = 20  # Máximo de PDFs por batch


# =============================================================================
# Função de Limpeza de Processos Travados
# =============================================================================
def cleanup_stuck_processes():
    """
    Detecta e cancela processos batch que estão travados há mais de 10 minutos.
    Chamada automaticamente durante operações batch ou manualmente via endpoint.
    
    Returns:
        int: Número de processos cancelados
    """
    from datetime import timedelta
    
    try:
        timeout_threshold = datetime.utcnow() - timedelta(minutes=10)
        
        # Buscar items travados
        stuck_items = BatchItem.query.filter(
            BatchItem.status == 'running',
            BatchItem.updated_at < timeout_threshold
        ).all()
        
        if not stuck_items:
            return 0
        
        logger.warning(f"[CLEANUP] Detectados {len(stuck_items)} processos travados")
        
        for item in stuck_items:
            logger.warning(f"[CLEANUP] Cancelando item #{item.id} (batch #{item.batch_id}) travado desde {item.updated_at}")
            
            # Resetar item
            item.status = 'error'
            item.last_error = f'Processo travado (timeout > 10min). Última atualização: {item.updated_at}'
            
            # Resetar processo associado
            if item.process_id:
                process = Process.query.get(item.process_id)
                if process and process.elaw_status == 'running':
                    process.elaw_status = 'error'
                    process.elaw_error_message = 'RPA travado (timeout > 10min)'
        
        db.session.commit()
        logger.info(f"[CLEANUP] ✅ {len(stuck_items)} processos travados foram cancelados")
        return len(stuck_items)
        
    except Exception as e:
        logger.error(f"[CLEANUP] Erro ao limpar processos travados: {e}", exc_info=True)
        db.session.rollback()
        return 0

def reset_sequence_if_empty(table_name: str):
    """
    Reseta a sequência de IDs de uma tabela se ela estiver vazia.
    Isso permite que os IDs comecem novamente do 1.
    
    Args:
        table_name: Nome da tabela ('batch_upload', 'process', 'batch_item')
    """
    from sqlalchemy import text
    
    try:
        result = db.session.execute(text(f"SELECT COUNT(*) FROM {table_name}"))
        count = result.scalar()
        
        if count == 0:
            sequence_name = f"{table_name}_id_seq"
            db.session.execute(text(f"ALTER SEQUENCE {sequence_name} RESTART WITH 1"))
            db.session.commit()
            logger.info(f"[RESET_SEQ] ✅ Sequência {sequence_name} resetada para 1")
            return True
    except Exception as e:
        logger.warning(f"[RESET_SEQ] Não foi possível resetar sequência de {table_name}: {e}")
        db.session.rollback()
    
    return False


def reset_all_sequences_if_empty():
    """
    Verifica e reseta sequências de todas as tabelas principais se estiverem vazias.
    Chamada após deleções em massa.
    """
    reset_sequence_if_empty('batch_upload')
    reset_sequence_if_empty('batch_item')
    reset_sequence_if_empty('process')


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _create_process_from_data(data, user_id):
    """Helper para criar Process a partir de dados extraídos"""
    from models import Process
    from datetime import datetime
    
    # Debug logging
    logger.debug(f"[CREATE_PROCESS] data type: {type(data)}, user_id: {user_id}")
    if not data:
        logger.error("[CREATE_PROCESS] ❌ data é None ou vazio!")
        raise ValueError("Dados extraídos são None ou vazios")
    if not user_id:
        logger.error("[CREATE_PROCESS] ❌ user_id é None!")
        raise ValueError("user_id não pode ser None")
    
    # CNJ - ✅ CORRIGIDO: campo cnj é String(3) para "Sim"/"Não", número vai para numero_processo
    cnj_sim_nao = "Sim" if data.get("numero_processo") else "Não"
    numero_processo_value = data.get("numero_processo", "").strip()
    
    # Estado
    estado = data.get("estado", "").strip() or "SP"
    
    # Célula
    celula = (data.get("celula") or data.get("escritorio") or 
             data.get("cliente") or "Em Segredo").strip()
    
    logger.debug(f"[CREATE_PROCESS] Criando processo para user_id={user_id}, numero={numero_processo_value}")
    
    # Criar processo
    proc = Process(
        owner_id=user_id,
        created_by=user_id,
        updated_by=user_id,
        cnj=cnj_sim_nao,
        tipo_processo=(data.get("tipo_processo") or "Eletrônico").strip(),
        numero_processo=numero_processo_value,
        sistema_eletronico=(data.get("sistema_eletronico") or "PJE").strip(),
        area_direito=(data.get("area_direito") or "Trabalhista").strip(),
        sub_area_direito=data.get("sub_area_direito"),
        estado=estado,
        comarca=data.get("comarca"),
        numero_orgao=data.get("numero_orgao", "01"),
        origem=data.get("origem"),
        orgao=data.get("orgao"),
        vara=data.get("vara"),
        celula=celula,
        foro=data.get("foro"),
        instancia=data.get("instancia", "Primeira Instância"),
        assunto=data.get("assunto"),
        objeto=data.get("objeto"),
        sub_objeto=data.get("sub_objeto"),
        cliente=data.get("cliente"),
        parte=data.get("parte"),
        valor_causa=data.get("valor_causa"),
        data_distribuicao=data.get("data_distribuicao"),
        audiencia_inicial=data.get("audiencia_inicial"),
        cadastrar_primeira_audiencia=data.get("cadastrar_primeira_audiencia", False),
        link_audiencia=data.get("link_audiencia"),
        subtipo_audiencia=data.get("subtipo_audiencia"),
        envolvido_audiencia=data.get("envolvido_audiencia"),
        outra_reclamada_cliente=data.get("outra_reclamada_cliente"),
        parte_interessada=data.get("parte_interessada"),
        posicao_parte_interessada=data.get("posicao_parte_interessada"),
        parte_adversa_nome=data.get("parte_adversa_nome"),
        parte_adversa_tipo=data.get("parte_adversa_tipo"),
        cpf_cnpj_parte_adversa=data.get("cpf_cnpj_parte_adversa"),
        data_admissao=data.get("data_admissao"),
        data_demissao=data.get("data_demissao"),
        motivo_demissao=data.get("motivo_demissao"),
        salario=data.get("salario"),
        cargo_funcao=data.get("cargo_funcao") or data.get("cargo"),
        empregador=data.get("empregador"),
        local_trabalho=data.get("local_trabalho"),
        pis=data.get("pis"),
        ctps=data.get("ctps"),
        pdf_filename=data.get("pdf_filename"),
        pedidos_json=json.dumps(data.get("pedidos", [])) if data.get("pedidos") else None
    )
    db.session.add(proc)
    db.session.flush()
    
    logger.debug(f"[CREATE_PROCESS] ✅ Processo criado com ID: {proc.id}")
    return proc.id


# =============================================================================
# CONFIGURAÇÃO DE PARALELISMO - UNIFICADA
# 2025-12-03: 5 workers fixos para Google Cloud (recursos suficientes)
# Pode ser sobrescrito via variáveis de ambiente
# =============================================================================
MAX_EXTRACTION_WORKERS = int(os.getenv("MAX_EXTRACTION_WORKERS", "5"))  # Extração paralela de PDFs
MAX_RPA_WORKERS = int(os.getenv("MAX_RPA_WORKERS", "5"))  # RPA paralelo no eLaw
MAX_UPLOAD_WORKERS = int(os.getenv("MAX_UPLOAD_WORKERS", "5"))  # Salvamento paralelo de uploads


def _extract_single_item(item_id: int, upload_path: str, source_filename: str, user_id: int) -> dict:
    """
    Extrai dados de um único PDF de forma thread-safe.
    
    Cada thread tem sua própria sessão do banco de dados para evitar conflitos.
    
    Args:
        item_id: ID do BatchItem
        upload_path: Caminho do arquivo PDF
        source_filename: Nome original do arquivo
        user_id: ID do usuário dono do batch
        
    Returns:
        dict com resultado: {'item_id': int, 'success': bool, 'process_id': int|None, 'error': str|None}
    """
    from main import app
    from extractors.pipeline import run_extraction_from_file
    
    result = {
        'item_id': item_id,
        'success': False,
        'process_id': None,
        'error': None
    }
    
    try:
        logger.info(f"[EXTRACT][THREAD] Iniciando extração do item {item_id}: {source_filename}")
        
        # ✅ CRÍTICO: Cada thread precisa de seu próprio app_context para sessão DB isolada
        with app.app_context():
            # Atualizar status para 'extracting'
            item = BatchItem.query.get(item_id)
            if not item:
                result['error'] = f'Item {item_id} não encontrado'
                logger.error(f"[EXTRACT][THREAD] {result['error']}")
                return result
            
            item.status = 'extracting'
            item.updated_at = datetime.utcnow()
            db.session.commit()
            
            # Extrair dados do PDF
            extracted_data = run_extraction_from_file(
                path=upload_path,
                filename=source_filename
            )
            
            if extracted_data:
                # ✅ CRÍTICO: Incluir pdf_filename para permitir extração de reclamadas no RPA
                extracted_data["pdf_filename"] = upload_path
                
                # Extrair tarefa OCR diferida antes de criar processo (não deve ir pro banco)
                deferred_ocr = extracted_data.pop("_deferred_ocr_task", None)
                
                # Criar processo no banco
                process_id = _create_process_from_data(extracted_data, user_id)
                
                # 2025-12-05: Enfileirar OCR diferido agora que temos process_id
                if deferred_ocr and process_id:
                    try:
                        from extractors.ocr_utils import queue_ocr_task
                        queue_ocr_task(
                            process_id, 
                            deferred_ocr["pdf_path"],
                            deferred_ocr["doc_pages"],
                            deferred_ocr["missing_fields"]
                        )
                        logger.info(f"[EXTRACT][THREAD] 📥 OCR diferido enfileirado para processo {process_id}")
                    except Exception as ocr_ex:
                        logger.warning(f"[EXTRACT][THREAD] Erro ao enfileirar OCR: {ocr_ex}")
                
                # Atualizar item com sucesso
                item.process_id = process_id
                item.status = 'ready'
                item.updated_at = datetime.utcnow()
                db.session.commit()
                
                result['success'] = True
                result['process_id'] = process_id
                logger.info(f"[EXTRACT][THREAD] ✅ Item {item_id} processado! Process ID: {process_id}")
            else:
                item.status = 'error'
                item.last_error = 'Falha na extração de dados'
                item.updated_at = datetime.utcnow()
                db.session.commit()
                
                result['error'] = 'Falha na extração de dados'
                logger.warning(f"[EXTRACT][THREAD] ❌ Erro na extração do item {item_id}")
                
    except Exception as ex:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"[EXTRACT][THREAD] ❌ Exceção ao processar item {item_id}: {ex}")
        logger.error(f"[EXTRACT][THREAD][TRACEBACK] {tb}")
        
        result['error'] = str(ex)[:500]
        
        # Tentar atualizar status no banco
        try:
            with app.app_context():
                item = BatchItem.query.get(item_id)
                if item:
                    item.status = 'error'
                    item.last_error = result['error']
                    item.updated_at = datetime.utcnow()
                    db.session.commit()
        except Exception as db_ex:
            logger.error(f"[EXTRACT][THREAD] Erro ao atualizar status do item {item_id}: {db_ex}")
    
    return result


def process_batch_async(batch_id, user_id):
    """
    Processa batch em thread separada com extração PARALELA de PDFs.
    
    Usa ThreadPoolExecutor para processar múltiplos PDFs simultaneamente,
    melhorando significativamente o tempo de processamento de batches grandes.
    """
    from main import app
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    
    with app.app_context():
        batch = BatchUpload.query.get(batch_id)
        if not batch:
            logger.error(f"Batch {batch_id} não encontrado!")
            return
        
        logger.info(f"[BATCH] Iniciando processamento PARALELO do batch {batch_id}")
        batch.status = 'processing'
        db.session.commit()
        
        # Coletar itens pendentes ORDENADOS POR TAMANHO (menor primeiro)
        # 2025-12-05: Processar PDFs menores primeiro dá sensação de progresso mais rápido
        pending_items = BatchItem.query.filter_by(batch_id=batch_id, status='pending')\
            .order_by(BatchItem.file_size.asc().nullslast())\
            .all()
        total_items = len(pending_items)
        logger.info(f"[BATCH] {total_items} itens pendentes ordenados por tamanho (menor→maior, max {MAX_EXTRACTION_WORKERS} workers)")
        
        if total_items == 0:
            batch.status = 'ready'
            batch.finished_at = datetime.utcnow()
            db.session.commit()
            logger.info(f"[BATCH] Batch {batch_id} sem itens pendentes")
            return
        
        # Preparar dados para processamento paralelo (evitar passar objetos SQLAlchemy entre threads)
        items_data = [
            {
                'item_id': item.id,
                'upload_path': item.upload_path,
                'source_filename': item.source_filename
            }
            for item in pending_items
        ]
        
        processed = 0
        errors = 0
        
        # ✅ Processar em paralelo usando ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=MAX_EXTRACTION_WORKERS) as executor:
            # Submeter todas as tarefas
            future_to_item = {
                executor.submit(
                    _extract_single_item,
                    item_data['item_id'],
                    item_data['upload_path'],
                    item_data['source_filename'],
                    user_id
                ): item_data
                for item_data in items_data
            }
            
            logger.info(f"[BATCH] {len(future_to_item)} tarefas submetidas ao executor")
            
            # 🚀 PLANO BATMAN: Timeout de 45s por PDF para evitar travamentos (aumentado para PDFs grandes)
            EXTRACTION_TIMEOUT = 45  # segundos
            
            # 🆕 Usar as_completed com timeout global para detectar travamentos
            import time
            start_time = time.time()
            MAX_BATCH_TIME = 300  # 5 minutos máximo para todo o batch
            
            # Processar resultados à medida que ficam prontos
            for future in as_completed(future_to_item, timeout=MAX_BATCH_TIME):
                item_data = future_to_item[future]
                
                try:
                    result = future.result(timeout=EXTRACTION_TIMEOUT)
                    
                    if result['success']:
                        processed += 1
                        logger.info(f"[BATCH] ✅ Concluído: item {result['item_id']} -> process {result['process_id']}")
                    else:
                        errors += 1
                        logger.warning(f"[BATCH] ❌ Falhou: item {result['item_id']} -> {result['error']}")
                    
                    # Atualizar progresso do batch em tempo real
                    batch.processed_count = processed + errors
                    db.session.commit()
                    
                    logger.info(f"[BATCH] Progresso: {processed + errors}/{total_items} ({processed} sucesso, {errors} erros)")
                
                except (TimeoutError, Exception) as ex:
                    errors += 1
                    error_type = "TIMEOUT" if "Timeout" in str(type(ex).__name__) else "ERRO"
                    logger.error(f"[BATCH] ⏱️ {error_type}: item {item_data['item_id']} - {ex}")
                    
                    # Marcar item como erro
                    try:
                        timeout_item = BatchItem.query.get(item_data['item_id'])
                        if timeout_item and timeout_item.status == 'extracting':
                            timeout_item.status = 'error'
                            timeout_item.last_error = f'{error_type}: {str(ex)[:200]}'
                            db.session.commit()
                    except Exception:
                        pass
            
            # 🆕 Verificar se há itens ainda em 'extracting' (travados) e marcar como erro
            stuck_items = BatchItem.query.filter_by(batch_id=batch_id, status='extracting').all()
            for stuck in stuck_items:
                stuck.status = 'error'
                stuck.last_error = 'Travou durante processamento'
                errors += 1
                logger.error(f"[BATCH] ⚠️ Item {stuck.id} estava travado em 'extracting' - marcado como erro")
            if stuck_items:
                db.session.commit()
        
        # Finalizar batch com status correto para permitir RPA
        # ready = todos extraídos com sucesso
        # partial_ready = alguns extraídos, alguns erros (ainda pode iniciar RPA)
        # error = todos falharam
        if errors == 0:
            batch.status = 'ready'
        elif processed > 0:
            batch.status = 'partial_ready'  # Permite iniciar RPA para os que funcionaram
        else:
            batch.status = 'error'
        
        batch.processed_count = processed + errors
        batch.finished_at = datetime.utcnow()
        db.session.commit()
        
        logger.info(f"[BATCH] ✅ Batch {batch_id} finalizado: status={batch.status}, {processed} sucesso(s), {errors} erro(s) em {total_items} itens")


@batch_bp.route("/new", methods=["GET", "POST"])
@login_required
def batch_new():
    """Upload de múltiplos PDFs"""
    # 🔧 DEBUG 2025-12-03: Log IMEDIATO para verificar se a requisição chega
    import traceback
    logger.info(f"[UPLOAD][TRACE] ========== REQUISIÇÃO RECEBIDA ==========")
    logger.info(f"[UPLOAD][TRACE] Method: {request.method}")
    logger.info(f"[UPLOAD][TRACE] URL: {request.url}")
    logger.info(f"[UPLOAD][TRACE] User-Agent: {request.headers.get('User-Agent', 'N/A')[:50]}")
    
    if request.method == "POST":
        # 🔧 DEBUG 2025-12-03: Log detalhado para identificar problemas de upload em produção
        logger.info(f"[UPLOAD][DEBUG] ========== INÍCIO DO UPLOAD ==========")
        logger.info(f"[UPLOAD][DEBUG] User: {current_user.id} ({current_user.username})")
        logger.info(f"[UPLOAD][DEBUG] Content-Type: {request.content_type}")
        logger.info(f"[UPLOAD][DEBUG] Content-Length: {request.content_length}")
        logger.info(f"[UPLOAD][DEBUG] Request files keys: {list(request.files.keys())}")
        
        files = request.files.getlist('pdfs')
        logger.info(f"[UPLOAD][DEBUG] Arquivos recebidos: {len(files)}")
        for i, f in enumerate(files):
            logger.info(f"[UPLOAD][DEBUG]   [{i}] filename='{f.filename}', content_type='{f.content_type}'")
        
        if not files or len(files) == 0:
            logger.warning(f"[UPLOAD][DEBUG] ERRO: Nenhum arquivo selecionado")
            flash("Nenhum arquivo selecionado.", "danger")
            return redirect(request.url)
        
        if len(files) > MAX_FILES_PER_BATCH:
            logger.warning(f"[UPLOAD][DEBUG] ERRO: Limite excedido ({len(files)} > {MAX_FILES_PER_BATCH})")
            flash(f"Máximo de {MAX_FILES_PER_BATCH} arquivos por vez.", "danger")
            return redirect(request.url)
        
        # Validar arquivos
        valid_files = []
        total_size = 0
        MAX_TOTAL_SIZE = 350 * 1024 * 1024  # 350MB total
        
        logger.info(f"[UPLOAD][DEBUG] Iniciando validação de {len(files)} arquivos...")
        for idx, file in enumerate(files):
            if file.filename == '':
                logger.info(f"[UPLOAD][DEBUG]   [{idx}] Pulando arquivo vazio")
                continue
            
            if not allowed_file(file.filename):
                logger.warning(f"[UPLOAD][DEBUG]   [{idx}] ERRO: '{file.filename}' não é PDF válido")
                flash(f"Arquivo '{file.filename}' não é um PDF válido.", "danger")
                return redirect(request.url)
            
            # Verificar tamanho (aproximado)
            file.seek(0, os.SEEK_END)
            size = file.tell()
            file.seek(0)
            
            logger.info(f"[UPLOAD][DEBUG]   [{idx}] '{file.filename}' = {size:,} bytes ({size/1024/1024:.2f} MB)")
            
            if size > MAX_FILE_SIZE:
                logger.warning(f"[UPLOAD][DEBUG]   [{idx}] ERRO: Arquivo muito grande ({size} > {MAX_FILE_SIZE})")
                flash(f"Arquivo '{file.filename}' excede {MAX_FILE_SIZE // (1024*1024)}MB.", "danger")
                return redirect(request.url)
            
            total_size += size
            valid_files.append(file)
        
        logger.info(f"[UPLOAD][DEBUG] Validação concluída: {len(valid_files)} válidos, total={total_size:,} bytes")
        
        # Verificar limite total
        if total_size > MAX_TOTAL_SIZE:
            total_mb = total_size / (1024 * 1024)
            logger.warning(f"[UPLOAD][DEBUG] ERRO: Tamanho total excede limite ({total_mb:.1f}MB > 350MB)")
            flash(f"Tamanho total dos arquivos ({total_mb:.1f}MB) excede o limite de 350MB.", "danger")
            return redirect(request.url)
        
        if not valid_files:
            logger.warning(f"[UPLOAD][DEBUG] ERRO: Nenhum arquivo válido após filtro")
            flash("Nenhum arquivo válido para processar.", "danger")
            return redirect(request.url)
        
        try:
            # 🚀 PLANO BATMAN: Ler arquivos para memória PRIMEIRO (inevitável com Flask)
            # Depois redirecionar IMEDIATAMENTE e processar em background
            logger.info(f"[UPLOAD][DEBUG] Lendo {len(valid_files)} arquivos para memória...")
            file_data = []
            for idx, file in enumerate(valid_files):
                filename = secure_filename(file.filename)
                try:
                    content = file.read()  # Lê para memória (necessário antes do redirect)
                    logger.info(f"[UPLOAD][DEBUG]   [{idx}] Lido '{filename}' = {len(content):,} bytes")
                    file_data.append((filename, content))
                except Exception as read_err:
                    logger.error(f"[UPLOAD][DEBUG]   [{idx}] ERRO ao ler '{filename}': {read_err}")
                    logger.error(f"[UPLOAD][DEBUG] Stack: {traceback.format_exc()}")
                    raise
            
            logger.info(f"[UPLOAD][DEBUG] Total lido: {len(file_data)} arquivos em memória")
            
            # Criar batch
            logger.info(f"[UPLOAD][DEBUG] Criando batch no banco de dados...")
            batch = BatchUpload(
                owner_id=current_user.id,
                status='uploading',
                total_count=len(file_data)
            )
            db.session.add(batch)
            db.session.flush()  # Obter batch.id
            logger.info(f"[UPLOAD][DEBUG] Batch criado: id={batch.id}")
            
            # Criar diretório para este batch
            batch_dir = Path('uploads') / 'batch' / str(batch.id)
            logger.info(f"[UPLOAD][DEBUG] Criando diretório: {batch_dir}")
            try:
                batch_dir.mkdir(parents=True, exist_ok=True)
                logger.info(f"[UPLOAD][DEBUG] Diretório criado com sucesso")
            except Exception as dir_err:
                logger.error(f"[UPLOAD][DEBUG] ERRO ao criar diretório: {dir_err}")
                logger.error(f"[UPLOAD][DEBUG] Stack: {traceback.format_exc()}")
                raise
            
            # Criar BatchItems ANTES de salvar (para mostrar na tela)
            # 2025-12-05: Salvar file_size para ordenação por tamanho (menor primeiro)
            logger.info(f"[UPLOAD][DEBUG] Criando {len(file_data)} BatchItems...")
            for idx, (filename, content) in enumerate(file_data):
                item = BatchItem(
                    batch_id=batch.id,
                    source_filename=filename,
                    upload_path=str(batch_dir / filename),
                    file_size=len(content),  # Tamanho em bytes
                    status='uploading'
                )
                db.session.add(item)
                logger.info(f"[UPLOAD][DEBUG]   [{idx}] BatchItem criado: {filename} ({len(content):,} bytes)")
            
            batch.status = 'pending'
            logger.info(f"[UPLOAD][DEBUG] Fazendo commit no banco...")
            db.session.commit()
            logger.info(f"[UPLOAD][DEBUG] Commit OK! Batch {batch.id} salvo com {len(file_data)} items")
            
            # 🚀 TUDO EM BACKGROUND: Salvar arquivos + extrair
            import threading
            
            def save_and_process(batch_id, user_id, file_data_list, batch_dir_str):
                """Salva arquivos e processa tudo em background"""
                from main import app
                from concurrent.futures import ThreadPoolExecutor
                import traceback as tb
                
                logger.info(f"[BACKGROUND][DEBUG] ========== THREAD INICIADA ==========")
                logger.info(f"[BACKGROUND][DEBUG] batch_id={batch_id}, user_id={user_id}")
                logger.info(f"[BACKGROUND][DEBUG] batch_dir={batch_dir_str}")
                logger.info(f"[BACKGROUND][DEBUG] Arquivos a salvar: {len(file_data_list)}")
                
                with app.app_context():
                    try:
                        batch_dir_path = Path(batch_dir_str)
                        logger.info(f"[BACKGROUND][DEBUG] Verificando diretório: {batch_dir_path}")
                        logger.info(f"[BACKGROUND][DEBUG] Diretório existe: {batch_dir_path.exists()}")
                        
                        # Salvar arquivos em paralelo
                        def save_file(args):
                            fname, content = args
                            fpath = batch_dir_path / fname
                            try:
                                with open(str(fpath), 'wb') as f:
                                    f.write(content)
                                logger.info(f"[BACKGROUND][DEBUG] Salvo: {fname} ({len(content):,} bytes)")
                                return fname, str(fpath), None
                            except Exception as save_err:
                                logger.error(f"[BACKGROUND][DEBUG] ERRO ao salvar {fname}: {save_err}")
                                return fname, None, str(save_err)
                        
                        logger.info(f"[BACKGROUND][DEBUG] Iniciando salvamento paralelo ({MAX_UPLOAD_WORKERS} workers)...")
                        with ThreadPoolExecutor(max_workers=MAX_UPLOAD_WORKERS) as executor:
                            results = list(executor.map(save_file, file_data_list))
                        
                        # Verificar resultados
                        saved_count = sum(1 for r in results if r[1] is not None)
                        error_count = sum(1 for r in results if r[2] is not None)
                        logger.info(f"[BACKGROUND][DEBUG] Salvamento concluído: {saved_count} OK, {error_count} erros")
                        
                        if error_count > 0:
                            for fname, fpath, err in results:
                                if err:
                                    logger.error(f"[BACKGROUND][DEBUG]   ERRO em '{fname}': {err}")
                        
                        logger.info(f"[BATCH] {len(file_data_list)} arquivos salvos em disco")
                        
                        # Atualizar status dos items para 'pending'
                        logger.info(f"[BACKGROUND][DEBUG] Atualizando status dos items para 'pending'...")
                        items = BatchItem.query.filter_by(batch_id=batch_id).all()
                        for item in items:
                            item.status = 'pending'
                        db.session.commit()
                        logger.info(f"[BACKGROUND][DEBUG] Status atualizado para {len(items)} items")
                        
                        # Agora processar extração
                        logger.info(f"[BACKGROUND][DEBUG] Iniciando extração (process_batch_async)...")
                        process_batch_async(batch_id, user_id)
                        logger.info(f"[BACKGROUND][DEBUG] ========== THREAD CONCLUÍDA ==========")
                        
                    except Exception as e:
                        logger.error(f"[BACKGROUND][DEBUG] ========== ERRO NA THREAD ==========")
                        logger.error(f"[BACKGROUND][DEBUG] Erro: {e}")
                        logger.error(f"[BACKGROUND][DEBUG] Stack trace:\n{tb.format_exc()}")
                        
                        # Tentar marcar batch como erro
                        try:
                            batch_obj = BatchUpload.query.get(batch_id)
                            if batch_obj:
                                batch_obj.status = 'error'
                                db.session.commit()
                                logger.info(f"[BACKGROUND][DEBUG] Batch {batch_id} marcado como 'error'")
                        except Exception as db_err:
                            logger.error(f"[BACKGROUND][DEBUG] Erro ao marcar batch como error: {db_err}")
            
            thread = threading.Thread(
                target=save_and_process, 
                args=(batch.id, current_user.id, file_data, str(batch_dir))
            )
            thread.daemon = True
            thread.start()
            logger.info(f"[UPLOAD][DEBUG] Thread de background iniciada para batch {batch.id}")
            logger.info(f"[UPLOAD][DEBUG] ========== REDIRECIONANDO PARA PROGRESSO ==========")
            
            # Toast de sucesso
            flash(f"Batch criado! {len(file_data)} arquivo(s) sendo enviados e processados.", "success")
            
            # Redirecionar IMEDIATAMENTE para tela de progresso
            return redirect(url_for('batch.batch_progress', id=batch.id))
        
        except Exception as e:
            db.session.rollback()
            logger.error(f"[UPLOAD][DEBUG] ========== ERRO GERAL NO UPLOAD ==========")
            logger.error(f"[UPLOAD][DEBUG] Erro: {e}")
            logger.error(f"[UPLOAD][DEBUG] Stack trace:\n{traceback.format_exc()}")
            flash(f"Erro ao processar arquivos: {str(e)}", "danger")
            return redirect(request.url)
    
    return render_template("processes/batch_upload.html")


@batch_bp.route("/list")
@login_required
def batch_list():
    """Lista todos os batches do usuário"""
    batches = BatchUpload.query.filter_by(owner_id=current_user.id).order_by(BatchUpload.created_at.desc()).all()
    return render_template("processes/batch_list.html", batches=batches)


@batch_bp.route("/<int:id>")
@login_required
def batch_detail(id):
    """Detalhes de um batch"""
    from models import Process
    batch = BatchUpload.query.get_or_404(id)
    
    # Verificar permissão
    if batch.owner_id != current_user.id:
        flash("Você não tem permissão para acessar este batch.", "danger")
        return redirect(url_for('batch.batch_list'))
    
    # Carregar items com eager loading dos processos - ordenar por process_id para exibição sequencial
    items = BatchItem.query.filter_by(batch_id=id).order_by(BatchItem.process_id.asc().nullslast()).all()
    
    # Carregar processos para cada item (eager loading manual)
    for item in items:
        if item.process_id:
            item.process = Process.query.get(item.process_id)
        else:
            item.process = None
    
    return render_template("processes/batch_detail.html", batch=batch, items=items)


@batch_bp.route("/<int:id>/progress")
@login_required
def batch_progress(id):
    """Tela de progresso do processamento em lote"""
    batch = BatchUpload.query.get_or_404(id)
    
    # Verificar permissão
    if batch.owner_id != current_user.id:
        flash("Você não tem permissão para acessar este batch.", "danger")
        return redirect(url_for('batch.batch_list'))
    
    return render_template("processes/batch_progress.html", batch_id=batch.id)


@batch_bp.route("/<int:id>/progress-status")
@login_required
def batch_progress_status(id):
    """Retorna status do progresso (JSON para polling)"""
    # 🔧 2025-11-27: FORÇAR dados frescos do banco (evitar cache de sessão SQLAlchemy)
    db.session.expire_all()
    
    batch = BatchUpload.query.get_or_404(id)
    
    # Verificar permissão
    if batch.owner_id != current_user.id:
        return jsonify({'error': 'Permissão negada'}), 403
    
    items = BatchItem.query.filter_by(batch_id=id).order_by(BatchItem.process_id.asc().nullslast()).all()
    
    # Encontrar arquivo sendo processado atualmente
    current_file = None
    for item in items:
        if item.status == 'extracting':
            current_file = item.source_filename
            break
    
    return jsonify({
        'batch_id': batch.id,
        'status': batch.status,
        'total_count': batch.total_count,
        'processed_count': sum(1 for item in items if item.status in ['ready', 'success', 'error']),
        'current_file': current_file,
        'items': [
            {
                'id': item.id,
                'filename': item.source_filename,
                'status': item.status,
                'process_id': item.process_id,
                'last_error': item.last_error
            }
            for item in items
        ]
    })


def _execute_single_rpa(item_id: int, process_id: int, worker_id: int = 0) -> dict:
    """
    Executa RPA para um único processo de forma thread-safe.
    
    🆕 2025-11-27: Usa a nova função execute_rpa_parallel() que:
    - Usa contextvars (thread-local) em vez de globals
    - Permite execuções paralelas via semáforo
    - Cada worker tem seu próprio browser isolado
    
    Args:
        item_id: ID do BatchItem
        process_id: ID do Process a ser processado
        worker_id: ID do worker no ThreadPoolExecutor
        
    Returns:
        dict com resultado: {'item_id': int, 'process_id': int, 'success': bool, 'error': str|None}
    """
    from main import app
    import rpa
    
    # ✅ CRÍTICO: Garantir que flask_app está disponível para verificação de status
    if not rpa.flask_app:
        rpa.flask_app = app._get_current_object() if hasattr(app, '_get_current_object') else app
    
    result = {
        'item_id': item_id,
        'process_id': process_id,
        'worker_id': worker_id,
        'success': False,
        'error': None
    }
    
    try:
        logger.info(f"[RPA][WORKER-{worker_id}] Iniciando RPA para item {item_id}, processo {process_id}")
        
        # ✅ CRÍTICO: Cada thread precisa de seu próprio app_context para sessão DB isolada
        with app.app_context():
            # Atualizar status para 'running'
            item = BatchItem.query.get(item_id)
            if not item:
                result['error'] = f'Item {item_id} não encontrado'
                logger.error(f"[RPA][WORKER-{worker_id}] {result['error']}")
                return result
            
            item.status = 'running'
            item.attempt_count += 1
            item.updated_at = datetime.utcnow()
            db.session.commit()
            
            # ✅ CRÍTICO: Limpar sessão ANTES de chamar RPA para evitar conflitos
            db.session.remove()
        
        # 🆕 Executar RPA PARALELO (fora do app_context, usa seu próprio contexto interno)
        logger.info(f"[RPA][WORKER-{worker_id}] Executando execute_rpa_parallel({process_id}, worker_id={worker_id})")
        rpa_result = rpa.execute_rpa_parallel(process_id, worker_id=worker_id)
        logger.info(f"[RPA][WORKER-{worker_id}] execute_rpa_parallel retornou: {rpa_result}")
        
        # Atualizar BatchItem com resultado (nova sessão limpa)
        with app.app_context():
            item = BatchItem.query.get(item_id)
            if item:
                if rpa_result.get('status') == 'success':
                    item.status = 'success'
                    item.last_error = None
                    result['success'] = True
                    logger.info(f"[RPA][WORKER-{worker_id}] ✅ Item {item_id} processado com sucesso!")
                else:
                    item.status = 'error'
                    item.last_error = rpa_result.get('error', rpa_result.get('message', 'Erro desconhecido'))[:500]
                    result['error'] = item.last_error
                    logger.warning(f"[RPA][WORKER-{worker_id}] ❌ Item {item_id} com erro: {item.last_error}")
                
                item.updated_at = datetime.utcnow()
                db.session.commit()
            
            # ✅ CRÍTICO: Limpar sessão após uso
            db.session.remove()
                
    except Exception as ex:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"[RPA][WORKER-{worker_id}] ❌ Exceção ao processar item {item_id}: {ex}")
        logger.error(f"[RPA][WORKER-{worker_id}][TRACEBACK] {tb}")
        
        result['error'] = str(ex)[:500]
        
        # Tentar atualizar status no banco
        try:
            with app.app_context():
                item = BatchItem.query.get(item_id)
                if item:
                    item.status = 'error'
                    item.last_error = result['error']
                    item.updated_at = datetime.utcnow()
                    db.session.commit()
                db.session.remove()
        except Exception as db_ex:
            logger.error(f"[RPA][WORKER-{worker_id}] Erro ao atualizar status do item {item_id}: {db_ex}")
    
    return result


@batch_bp.route("/<int:id>/start", methods=["POST"])
@login_required
def batch_start(id):
    """Inicia processamento RPA do batch com execução PARALELA"""
    try:
        # 🔧 Limpeza automática de processos travados antes de iniciar
        cleaned = cleanup_stuck_processes()
        if cleaned > 0:
            logger.info(f"[BATCH START] Limpou {cleaned} processos travados antes de iniciar")
        
        batch = BatchUpload.query.get(id)
        if not batch:
            return jsonify({'success': False, 'error': 'Batch não encontrado'}), 404
        
        # Verificar permissão
        if batch.owner_id != current_user.id:
            return jsonify({'success': False, 'error': 'Permissão negada'}), 403
        
        # Verificar se batch está pronto (permite reprocessar batches com erro que têm itens ready)
        if batch.status not in ['ready', 'partial_ready', 'partial_completed', 'completed', 'error']:
            return jsonify({'success': False, 'error': f'Batch não está pronto (status: {batch.status})'}), 400
        
        # Se batch está em error, verificar se há itens prontos para processar
        if batch.status == 'error':
            ready_items = BatchItem.query.filter_by(batch_id=id, status='ready').count()
            if ready_items == 0:
                return jsonify({'success': False, 'error': 'Batch com erro não possui itens prontos para reprocessar'}), 400
            logger.info(f"[BATCH START] Batch {id} em erro será reprocessado ({ready_items} itens ready)")
        
        # Verificar se já está rodando
        if batch.lock_owner:
            return jsonify({'success': False, 'error': 'Batch já está sendo processado'}), 409
    
    except Exception as e:
        logger.error(f"Erro ao validar batch {id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
    
    try:
        # Executar RPA em thread separada (não bloqueante)
        import threading
        from main import app
        import rpa
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        # ✅ CRITICAL: Definir flask_app ANTES da thread para garantir disponibilidade no RPA
        rpa.flask_app = app._get_current_object() if hasattr(app, '_get_current_object') else app
        logger.info(f"[BATCH RPA] Flask app configurado globalmente para RPA")
        
        def execute_batch_rpa_parallel():
            """Executa RPA batch em PARALELO com ThreadPoolExecutor"""
            logger.info(f"[BATCH RPA][PARALLEL] Thread principal iniciada para batch {id}")
            
            try:
                with app.app_context():
                    try:
                        logger.info(f"[BATCH RPA] Iniciando processamento PARALELO em thread background")
                        
                        batch_reload = BatchUpload.query.get(id)
                        if not batch_reload:
                            logger.error(f"Batch {id} não encontrado no background")
                            return
                        
                        batch_reload.status = 'running'
                        batch_reload.started_at = datetime.utcnow()
                        batch_reload.processed_count = 0
                        db.session.commit()
                        
                        # Coletar itens prontos
                        items = BatchItem.query.filter_by(batch_id=id, status='ready').all()
                        total_items = len(items)
                        
                        logger.info(f"[BATCH RPA] {total_items} itens prontos para processar em paralelo (max {MAX_RPA_WORKERS} workers)")
                        
                        if total_items == 0:
                            batch_reload.status = 'completed'
                            batch_reload.finished_at = datetime.utcnow()
                            db.session.commit()
                            logger.info(f"[BATCH RPA] Batch {id} sem itens pendentes")
                            return
                        
                        # Preparar dados para processamento paralelo (evitar passar objetos SQLAlchemy entre threads)
                        items_data = []
                        for item in items:
                            if not item.process_id:
                                # Marcar itens sem process_id como erro antes do paralelo
                                item.status = 'error'
                                item.last_error = 'Processo não encontrado no banco de dados'
                                item.updated_at = datetime.utcnow()
                                logger.warning(f"[BATCH RPA] Item {item.id} sem process_id - marcado como erro")
                            else:
                                # Calcular peso do processo baseado em campos preenchidos
                                process = Process.query.get(item.process_id)
                                data_weight = 0
                                if process:
                                    # Conta campos preenchidos que serão enviados ao eLaw
                                    rpa_fields = [
                                        'numero_processo', 'area_direito', 'estado', 'comarca',
                                        'numero_orgao', 'orgao', 'celula', 'foro', 'instancia',
                                        'assunto', 'cliente', 'posicao_parte_interessada',
                                        'parte_interessada', 'parte_adversa_tipo', 'parte_adversa_nome',
                                        'data_distribuicao', 'data_admissao', 'data_demissao',
                                        'salario', 'cargo_funcao', 'pis', 'ctps', 'valor_causa',
                                        'audiencia_inicial', 'link_audiencia', 'pedidos_json',
                                        'outras_reclamadas_json'
                                    ]
                                    for field in rpa_fields:
                                        val = getattr(process, field, None)
                                        if val:
                                            data_weight += 1
                                            # Campos com muitos dados pesam mais
                                            if field == 'pedidos_json' and len(str(val)) > 100:
                                                data_weight += 2
                                            if field == 'outras_reclamadas_json' and len(str(val)) > 50:
                                                data_weight += 1
                                
                                items_data.append({
                                    'item_id': item.id,
                                    'process_id': item.process_id,
                                    'data_weight': data_weight
                                })
                        db.session.commit()
                        
                        # Ordenar por quantidade de dados (menor primeiro = mais rápido)
                        items_data.sort(key=lambda x: x['data_weight'])
                        logger.info(f"[BATCH RPA] Itens ordenados por peso de dados (menor→maior para RPA mais rápido)")
                        
                        success_count = 0
                        error_count = len(items) - len(items_data)  # Contar erros de itens sem process_id
                        
                        if not items_data:
                            batch_reload.status = 'partial_completed'
                            batch_reload.processed_count = error_count
                            batch_reload.finished_at = datetime.utcnow()
                            db.session.commit()
                            logger.info(f"[BATCH RPA] Batch {id} - todos os itens tinham erros")
                            return
                        
                        # ✅ Processar em paralelo usando ThreadPoolExecutor
                        logger.info(f"[BATCH RPA] Iniciando ThreadPoolExecutor com {MAX_RPA_WORKERS} workers para {len(items_data)} itens")
                        
                        with ThreadPoolExecutor(max_workers=MAX_RPA_WORKERS) as executor:
                            # Submeter todas as tarefas com worker_id único para cada uma
                            future_to_item = {}
                            for idx, item_data in enumerate(items_data):
                                worker_id = idx % MAX_RPA_WORKERS  # Cicla entre 0 e MAX_RPA_WORKERS-1
                                future = executor.submit(
                                    _execute_single_rpa,
                                    item_data['item_id'],
                                    item_data['process_id'],
                                    worker_id
                                )
                                future_to_item[future] = {**item_data, 'worker_id': worker_id}
                            
                            logger.info(f"[BATCH RPA] {len(future_to_item)} tarefas RPA submetidas ao executor")
                            
                            # Processar resultados à medida que ficam prontos
                            for future in as_completed(future_to_item):
                                item_data = future_to_item[future]
                                
                                try:
                                    result = future.result()
                                    
                                    if result['success']:
                                        success_count += 1
                                        logger.info(f"[BATCH RPA] ✅ Concluído: item {result['item_id']} -> processo {result['process_id']}")
                                    else:
                                        error_count += 1
                                        logger.warning(f"[BATCH RPA] ❌ Falhou: item {result['item_id']} -> {result['error']}")
                                    
                                    # Atualizar progresso do batch em tempo real
                                    batch_reload.processed_count = success_count + error_count
                                    db.session.commit()
                                    
                                    logger.info(f"[BATCH RPA] Progresso: {success_count + error_count}/{total_items} ({success_count} sucesso, {error_count} erros)")
                                    
                                except Exception as ex:
                                    error_count += 1
                                    logger.error(f"[BATCH RPA] ❌ Exceção no future do item {item_data['item_id']}: {ex}")
                        
                        # Finalizar batch
                        batch_reload.status = 'completed' if error_count == 0 else 'partial_completed'
                        batch_reload.processed_count = success_count + error_count
                        batch_reload.finished_at = datetime.utcnow()
                        db.session.commit()
                        
                        logger.info(f"[BATCH RPA] ✅ Batch {id} finalizado: {success_count} sucesso(s), {error_count} erro(s) em {total_items} itens")
                        
                    except Exception as e:
                        import traceback
                        tb = traceback.format_exc()
                        logger.error(f"[BATCH RPA] ❌ Erro fatal ao processar batch {id}: {e}")
                        logger.error(f"[BATCH RPA][TRACEBACK] {tb}")
                        try:
                            batch_reload = BatchUpload.query.get(id)
                            if batch_reload:
                                batch_reload.status = 'error'
                                batch_reload.finished_at = datetime.utcnow()
                                db.session.commit()
                        except:
                            pass
            
            except Exception as outer_ex:
                import traceback
                tb = traceback.format_exc()
                logger.error(f"[BATCH RPA][PARALLEL] ❌ Exceção FORA do app_context: {outer_ex}")
                logger.error(f"[BATCH RPA][PARALLEL][TRACEBACK] {tb}")
            
            finally:
                logger.info(f"[BATCH RPA][PARALLEL] Thread principal finalizada para batch {id}")
            
        # Iniciar thread principal
        thread = threading.Thread(target=execute_batch_rpa_parallel, daemon=True)
        thread.start()
        logger.info(f"[BATCH RPA] Thread de processamento PARALELO iniciada para batch {id}")
        
        # Retornar imediatamente
        return jsonify({
            'success': True,
            'message': f'Processamento RPA em lote iniciado com {MAX_RPA_WORKERS} processos simultâneos! Acompanhe o progresso na lista.'
        })
    
    except Exception as e:
        logger.error(f"Erro ao iniciar batch {id}: {e}", exc_info=True)
        try:
            batch.status = 'error'
            db.session.commit()
        except:
            pass
        return jsonify({'success': False, 'error': str(e)}), 500


@batch_bp.route("/<int:id>/status")
@login_required
def batch_status(id):
    """Retorna status atual do batch (JSON para polling)"""
    try:
        # 🔧 2025-11-27: FORÇAR dados frescos do banco (evitar cache de sessão SQLAlchemy)
        db.session.expire_all()
        
        batch = BatchUpload.query.get(id)
        if not batch:
            return jsonify({'success': False, 'error': 'Batch não encontrado'}), 404
        
        # Verificar permissão
        if batch.owner_id != current_user.id:
            return jsonify({'success': False, 'error': 'Permissão negada'}), 403
        
        items = BatchItem.query.filter_by(batch_id=id).order_by(BatchItem.process_id.asc().nullslast()).all()
        
        # 🔧 FIX: Carregar dados do processo para incluir screenshots
        from models import Process
        items_data = []
        rpa_completed_count = 0  # Contar processos com RPA finalizado
        needs_commit = False
        
        for item in items:
            # ✅ SYNC FIX: Sincronizar batch_item.status com process.elaw_status
            # Corrige casos onde a thread morreu antes de atualizar o status
            if item.status == 'running' and item.process_id:
                proc = Process.query.get(item.process_id)
                if proc and proc.elaw_status in ('success', 'error'):
                    # Processo terminou mas item não foi atualizado
                    item.status = 'success' if proc.elaw_status == 'success' else 'error'
                    item.updated_at = datetime.utcnow()
                    needs_commit = True
                    logger.info(f"[BATCH STATUS SYNC] Item {item.id} sincronizado: running -> {item.status}")
        
        # Commit sincronização se necessário
        if needs_commit:
            try:
                db.session.commit()
                # Também verificar se o batch precisa ser atualizado
                all_done = all(i.status in ('success', 'error') for i in items)
                if all_done and batch.status == 'running':
                    success_count = sum(1 for i in items if i.status == 'success')
                    error_count = sum(1 for i in items if i.status == 'error')
                    batch.status = 'completed' if error_count == 0 else 'partial_completed'
                    batch.processed_count = success_count + error_count
                    batch.finished_at = datetime.utcnow()
                    db.session.commit()
                    logger.info(f"[BATCH STATUS SYNC] Batch {id} sincronizado: running -> {batch.status}")
            except Exception as sync_ex:
                logger.error(f"[BATCH STATUS SYNC] Erro: {sync_ex}")
                db.session.rollback()
        
        for item in items:
            item_dict = {
                'id': item.id,
                'filename': item.source_filename,
                'status': item.status,
                'process_id': item.process_id,
                'attempt_count': item.attempt_count,
                'last_error': item.last_error
            }
            
            # Adicionar dados do processo se existir
            if item.process_id:
                proc = Process.query.get(item.process_id)
                if proc:
                    item_dict['process'] = {
                        'id': proc.id,
                        'elaw_status': proc.elaw_status,
                        'elaw_screenshot_before_path': proc.elaw_screenshot_before_path,
                        'elaw_screenshot_after_path': proc.elaw_screenshot_after_path,
                        'elaw_screenshot_reclamadas_path': proc.elaw_screenshot_reclamadas_path,
                        'elaw_screenshot_pedidos_path': proc.elaw_screenshot_pedidos_path
                    }
                    
                    # 🔧 2025-11-27: Adicionar status detalhado do RPA para tempo real
                    from models import RPAStatus
                    rpa_status = RPAStatus.query.filter_by(process_id=item.process_id).first()
                    if rpa_status:
                        item_dict['rpa_status'] = {
                            'current_step': rpa_status.current_step,
                            'message': rpa_status.message,
                            'status': rpa_status.status,
                            'updated_at': rpa_status.updated_at.isoformat() if rpa_status.updated_at else None
                        }
                    
                    # Contar apenas processos com RPA finalizado (success ou error)
                    if proc.elaw_status in ('success', 'error'):
                        rpa_completed_count += 1
            
            items_data.append(item_dict)
        
        # Durante RPA, usar contagem de processos finalizados; caso contrário usar processed_count do batch
        if batch.status == 'running':
            actual_processed = rpa_completed_count
        else:
            actual_processed = batch.processed_count
        
        return jsonify({
            'success': True,
            'batch_id': batch.id,
            'status': batch.status,
            'total_count': batch.total_count,
            'processed_count': actual_processed,
            'progress_percent': int((actual_processed / batch.total_count * 100)) if batch.total_count > 0 else 0,
            'started_at': batch.started_at.isoformat() if batch.started_at else None,
            'finished_at': batch.finished_at.isoformat() if batch.finished_at else None,
            'items': items_data
        })
    except Exception as e:
        logger.error(f"Erro ao obter status do batch {id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@batch_bp.route("/item/<int:id>/retry", methods=["POST"])
@login_required
def batch_item_retry(id):
    """Reenfileira item com erro para reprocessamento E executa RPA automaticamente"""
    item = BatchItem.query.get_or_404(id)
    batch = item.batch
    
    # Verificar permissão
    if batch.owner_id != current_user.id:
        flash("Permissão negada", "danger")
        return redirect(url_for('batch.batch_detail', id=batch.id))
    
    # Verificar se item pode ser reprocessado (error ou ready sem RPA)
    if item.status not in ['error', 'ready']:
        flash(f"Item não pode ser reprocessado (status: {item.status})", "warning")
        return redirect(url_for('batch.batch_detail', id=batch.id))
    
    # Verificar se já está em processamento (previne cliques duplicados)
    if item.status == 'running':
        flash(f"Item '{item.source_filename}' já está em processamento.", "info")
        return redirect(url_for('batch.batch_detail', id=batch.id))
    
    # Verificar se tem processo associado
    if not item.process_id:
        flash(f"Item '{item.source_filename}' não tem processo associado. Refaça a extração.", "warning")
        return redirect(url_for('batch.batch_detail', id=batch.id))
    
    # Verificar quantos RPAs estão rodando no momento
    running_count = BatchItem.query.filter_by(batch_id=batch.id, status='running').count()
    if running_count >= MAX_RPA_WORKERS:
        flash(f"Limite de {MAX_RPA_WORKERS} RPAs simultâneos atingido. Aguarde uma vaga.", "warning")
        return redirect(url_for('batch.batch_detail', id=batch.id))
    
    try:
        # Resetar status para running
        item.status = 'running'
        item.last_error = None
        item.attempt_count += 1
        item.updated_at = datetime.utcnow()
        db.session.commit()
        
        # Capturar IDs antes de iniciar thread
        item_id = item.id
        process_id = item.process_id
        batch_id = batch.id
        filename = item.source_filename
        
        # Executar RPA em thread separada para não bloquear a UI
        def run_rpa_async():
            logger.info(f"[RETRY RPA] ▶️ INICIANDO RPA para item {item_id}, processo {process_id}")
            result = _execute_single_rpa(item_id, process_id, worker_id=99)
            if result.get('success'):
                logger.info(f"[RETRY RPA] ✅ SUCESSO: item {item_id} processado!")
            else:
                logger.warning(f"[RETRY RPA] ❌ ERRO: item {item_id} - {result.get('error')}")
        
        import threading
        thread = threading.Thread(target=run_rpa_async, daemon=True)
        thread.start()
        
        logger.info(f"[RETRY RPA] Thread iniciada para item {item_id} (total running: {running_count + 1})")
        flash(f"RPA iniciado para '{filename}'! Acompanhe o progresso na tela.", "success")
        return redirect(url_for('batch.batch_detail', id=batch_id))
    
    except Exception as e:
        db.session.rollback()
        logger.error(f"Erro ao iniciar retry para item {id}: {e}")
        flash(f"Erro ao reprocessar: {str(e)}", "danger")
        return redirect(url_for('batch.batch_detail', id=batch.id))


@batch_bp.route("/item/<int:id>/pdf")
@login_required
def batch_item_pdf(id):
    """Visualizar PDF original do item"""
    item = BatchItem.query.get_or_404(id)
    batch = item.batch
    
    # Verificar permissão
    if batch.owner_id != current_user.id:
        flash("Você não tem permissão para acessar este arquivo.", "danger")
        return redirect(url_for('batch.batch_list'))
    
    # Servir o PDF
    filepath = Path(item.upload_path)
    return send_from_directory(filepath.parent, filepath.name, as_attachment=False)


@batch_bp.route("/item/<int:id>/delete", methods=["POST"])
@login_required
def batch_item_delete(id):
    """Deletar um item do batch"""
    item = BatchItem.query.get_or_404(id)
    batch = item.batch
    
    # Verificar permissão
    if batch.owner_id != current_user.id:
        flash("Você não tem permissão para deletar este item.", "danger")
        return redirect(url_for('batch.batch_list'))
    
    try:
        batch_id = batch.id
        
        # Deletar arquivo físico
        if os.path.exists(item.upload_path):
            os.remove(item.upload_path)
        
        # Deletar do banco
        db.session.delete(item)
        
        # Atualizar contagem do batch
        batch.total_count = max(0, batch.total_count - 1)
        db.session.commit()
        
        flash(f"Item '{item.source_filename}' deletado com sucesso!", "success")
        return redirect(url_for('batch.batch_detail', id=batch_id))
    
    except Exception as e:
        db.session.rollback()
        logger.error(f"Erro ao deletar item {id}: {e}")
        flash(f"Erro ao deletar item: {str(e)}", "danger")
        return redirect(url_for('batch.batch_detail', id=batch.id))


@batch_bp.route("/<int:id>/cleanup", methods=["POST"])
@login_required
def batch_cleanup(id):
    """Endpoint manual para limpar processos travados de um batch específico"""
    batch = BatchUpload.query.get_or_404(id)
    
    # Verificar permissão
    if batch.owner_id != current_user.id:
        return jsonify({'success': False, 'error': 'Permissão negada'}), 403
    
    try:
        cleaned = cleanup_stuck_processes()
        return jsonify({
            'success': True,
            'cleaned': cleaned,
            'message': f'{cleaned} processo(s) travado(s) foram cancelados' if cleaned > 0 else 'Nenhum processo travado detectado'
        })
    except Exception as e:
        logger.error(f"Erro ao limpar processos: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@batch_bp.route("/<int:id>/reprocess", methods=["POST"])
@login_required
def batch_reprocess(id):
    """Reprocessar TODOS os processos do batch, resetando status e executando RPA novamente"""
    import threading
    from main import app
    from models import Process
    
    batch = BatchUpload.query.get_or_404(id)
    
    # Verificar permissão
    if batch.owner_id != current_user.id:
        flash("Você não tem permissão para reprocessar este batch.", "danger")
        return redirect(url_for('batch.batch_list'))
    
    try:
        # 1. Limpeza automática de processos travados
        cleaned = cleanup_stuck_processes()
        if cleaned > 0:
            logger.info(f"[REPROCESS] Limpou {cleaned} processos travados")
        
        # 2. Cancelar processos travados específicos deste batch (redundância)
        from datetime import datetime, timedelta
        timeout_threshold = datetime.utcnow() - timedelta(minutes=10)
        
        stuck_items = BatchItem.query.filter(
            BatchItem.batch_id == id,
            BatchItem.status == 'running',
            BatchItem.updated_at < timeout_threshold
        ).all()
        
        for item in stuck_items:
            logger.warning(f"[REPROCESS] Cancelando item travado {item.id} (travado desde {item.updated_at})")
            item.status = 'ready'
            if item.process_id:
                process = Process.query.get(item.process_id)
                if process and process.elaw_status == 'running':
                    process.elaw_status = 'pending'
                    process.elaw_error_message = 'Cancelado por timeout (travado > 10min)'
        
        # 3. Resetar TODOS os items para 'ready' e limpar status RPA
        items_to_reprocess = BatchItem.query.filter_by(batch_id=id).all()
        
        logger.info(f"[REPROCESS] Resetando {len(items_to_reprocess)} itens do batch {id}")
        logger.info(f"[REPROCESS] Cancelados {len(stuck_items)} processos travados específicos do batch")
        
        for item in items_to_reprocess:
            # Resetar item para pending
            old_status = item.status
            item.status = 'pending'
            item.last_error = None
            item.attempt_count = 0  # Resetar contador de tentativas
            
            # Resetar status RPA do processo associado
            if item.process_id:
                process = Process.query.get(item.process_id)
                if process:
                    process.elaw_status = 'pending'
                    process.elaw_error_message = None
                    process.elaw_filled_at = None
                    # Não apagar screenshots - manter histórico
                    logger.info(f"[REPROCESS] Item {item.id}: {old_status} → pending (Processo #{process.id} resetado)")
        
        # Atualizar status do batch
        batch.status = 'pending'
        batch.processed_count = 0
        batch.started_at = None
        batch.finished_at = None
        db.session.commit()
        
        logger.info(f"[REPROCESS] Batch {id} resetado completamente. Iniciando RPA...")
        
        # Iniciar thread de processamento (replicando lógica do batch_start)
        import threading
        from main import app as flask_app_main
        import rpa
        
        # ✅ FIX: Capturar user_id ANTES da thread (current_user não existe em thread)
        user_id = current_user.id
        
        def execute_batch_reprocess_background():
            """Executa reprocessamento completo: EXTRAÇÃO PARALELA + RPA"""
            with flask_app_main.app_context():
                try:
                    # ✅ FIX: Garantir que rpa.py usa o flask_app correto
                    rpa.flask_app = flask_app_main
                    logger.info(f"[BATCH REPROCESS] Flask app context configurado (user_id={user_id})")
                    
                    batch_reload = BatchUpload.query.get(id)
                    if not batch_reload:
                        logger.error(f"Batch {id} não encontrado no background")
                        return
                    
                    # FASE 1: EXTRAÇÃO PARALELA DOS PDFs
                    logger.info(f"[BATCH REPROCESS] ══════ FASE 1: EXTRAÇÃO PARALELA ══════")
                    batch_reload.status = 'processing'
                    batch_reload.started_at = datetime.utcnow()
                    db.session.commit()
                    
                    pending_items = BatchItem.query.filter_by(batch_id=id, status='pending').all()
                    logger.info(f"[BATCH REPROCESS] {len(pending_items)} itens para extrair em PARALELO ({MAX_EXTRACTION_WORKERS} workers)")
                    
                    # ✅ PROCESSAMENTO PARALELO usando ThreadPoolExecutor
                    from concurrent.futures import ThreadPoolExecutor, as_completed
                    
                    # Preparar dados dos itens (snapshot para evitar problemas de sessão)
                    items_data = [(item.id, item.upload_path, item.source_filename) for item in pending_items]
                    
                    extracted_count = 0
                    extraction_errors = 0
                    
                    with ThreadPoolExecutor(max_workers=MAX_EXTRACTION_WORKERS) as executor:
                        # Submeter todas as tarefas de extração
                        future_to_item = {
                            executor.submit(
                                _extract_single_item,
                                item_id, upload_path, source_filename, user_id
                            ): item_id
                            for item_id, upload_path, source_filename in items_data
                        }
                        
                        # Processar conforme completam
                        for future in as_completed(future_to_item):
                            item_id = future_to_item[future]
                            try:
                                result = future.result()
                                if result.get('success'):
                                    extracted_count += 1
                                    logger.info(f"[BATCH REPROCESS] ✅ Item {item_id} extraído! Process ID: {result.get('process_id')}")
                                else:
                                    extraction_errors += 1
                                    logger.warning(f"[BATCH REPROCESS] ❌ Item {item_id} falhou: {result.get('error')}")
                            except Exception as ex:
                                extraction_errors += 1
                                logger.error(f"[BATCH REPROCESS] Erro no future do item {item_id}: {ex}")
                    
                    logger.info(f"[BATCH REPROCESS] Extração PARALELA finalizada: {extracted_count} sucesso(s), {extraction_errors} erro(s)")
                    
                    # Marcar batch como 'ready' (extração completa, aguardando usuário iniciar RPA)
                    batch_reload.status = 'ready' if extraction_errors == 0 else 'partial_ready'
                    batch_reload.processed_count = extracted_count + extraction_errors
                    batch_reload.finished_at = datetime.utcnow()
                    db.session.commit()
                    
                    logger.info(f"[BATCH REPROCESS] ══════ EXTRAÇÃO FINALIZADA ══════")
                    logger.info(f"[BATCH REPROCESS] {extracted_count} processos extraídos e prontos")
                    logger.info(f"[BATCH REPROCESS] Usuário pode iniciar preenchimento eLaw em batch")
                    
                except Exception as e:
                    logger.error(f"[BATCH REPROCESS] Erro fatal ao processar batch {id}: {e}", exc_info=True)
                    try:
                        batch_reload = BatchUpload.query.get(id)
                        if batch_reload:
                            batch_reload.status = 'error'
                            db.session.commit()
                    except:
                        pass
        
        thread = threading.Thread(target=execute_batch_reprocess_background, daemon=True)
        thread.start()
        logger.info(f"[BATCH REPROCESS] Thread de reprocessamento iniciada para batch {id}")
        
        flash(f"Reprocessamento iniciado! {len(items_to_reprocess)} itens serão processados novamente.", "success")
        return redirect(url_for('batch.batch_progress', id=id))
    
    except Exception as e:
        db.session.rollback()
        logger.error(f"Erro ao reprocessar batch {id}: {e}", exc_info=True)
        flash(f"Erro ao reprocessar: {str(e)}", "danger")
        return redirect(url_for('batch.batch_detail', id=id))


@batch_bp.route("/<int:id>/reextract", methods=["POST"])
@login_required
def batch_reextract(id):
    """
    Reprocessar EXTRAÇÃO dos PDFs selecionados.
    Reseta os dados do processo e executa novamente a extração.
    """
    import threading
    from main import app as flask_app_main
    
    batch = BatchUpload.query.get_or_404(id)
    
    if batch.owner_id != current_user.id:
        flash("Você não tem permissão para reprocessar este batch.", "danger")
        return redirect(url_for('batch.batch_list'))
    
    item_ids_str = request.form.get('item_ids', '')
    if not item_ids_str:
        flash("Nenhum item selecionado para reprocessar.", "warning")
        return redirect(url_for('batch.batch_detail', id=id))
    
    try:
        item_ids = [int(x.strip()) for x in item_ids_str.split(',') if x.strip().isdigit()]
    except ValueError:
        flash("IDs de itens inválidos.", "danger")
        return redirect(url_for('batch.batch_detail', id=id))
    
    if not item_ids:
        flash("Nenhum item selecionado para reprocessar.", "warning")
        return redirect(url_for('batch.batch_detail', id=id))
    
    try:
        items_to_reextract = BatchItem.query.filter(
            BatchItem.id.in_(item_ids),
            BatchItem.batch_id == id
        ).all()
        
        if not items_to_reextract:
            flash("Itens não encontrados.", "warning")
            return redirect(url_for('batch.batch_detail', id=id))
        
        logger.info(f"[REEXTRACT] Iniciando reextração de {len(items_to_reextract)} itens do batch {id}")
        
        for item in items_to_reextract:
            item.status = 'pending'
            item.last_error = None
            item.attempt_count = 0
            
            if item.process_id:
                old_process_id = item.process_id
                process = Process.query.get(item.process_id)
                if process:
                    db.session.delete(process)
                    logger.info(f"[REEXTRACT] Processo #{old_process_id} deletado para reextração")
                item.process_id = None
        
        batch.status = 'pending'
        db.session.commit()
        
        user_id = current_user.id
        items_data = [(item.id, item.upload_path, item.source_filename) for item in items_to_reextract]
        
        def execute_reextract_background():
            with flask_app_main.app_context():
                try:
                    from concurrent.futures import ThreadPoolExecutor, as_completed
                    
                    batch_reload = BatchUpload.query.get(id)
                    if not batch_reload:
                        return
                    
                    batch_reload.status = 'processing'
                    db.session.commit()
                    
                    logger.info(f"[REEXTRACT] Iniciando extração paralela de {len(items_data)} PDFs")
                    
                    extracted_count = 0
                    extraction_errors = 0
                    
                    with ThreadPoolExecutor(max_workers=MAX_EXTRACTION_WORKERS) as executor:
                        future_to_item = {
                            executor.submit(
                                _extract_single_item,
                                item_id, upload_path, source_filename, user_id
                            ): item_id
                            for item_id, upload_path, source_filename in items_data
                        }
                        
                        for future in as_completed(future_to_item):
                            item_id = future_to_item[future]
                            try:
                                result = future.result()
                                if result.get('success'):
                                    extracted_count += 1
                                    logger.info(f"[REEXTRACT] ✅ Item {item_id} extraído!")
                                else:
                                    extraction_errors += 1
                                    logger.warning(f"[REEXTRACT] ❌ Item {item_id} falhou: {result.get('error')}")
                            except Exception as ex:
                                extraction_errors += 1
                                logger.error(f"[REEXTRACT] Erro no item {item_id}: {ex}")
                    
                    batch_reload.status = 'ready' if extraction_errors == 0 else 'partial_ready'
                    db.session.commit()
                    
                    logger.info(f"[REEXTRACT] ✅ Finalizado: {extracted_count} sucesso(s), {extraction_errors} erro(s)")
                    
                except Exception as e:
                    logger.error(f"[REEXTRACT] Erro fatal: {e}", exc_info=True)
                    try:
                        batch_reload = BatchUpload.query.get(id)
                        if batch_reload:
                            batch_reload.status = 'partial_ready'
                            db.session.commit()
                    except:
                        pass
        
        thread = threading.Thread(target=execute_reextract_background, daemon=True)
        thread.start()
        
        flash(f"Reextração iniciada! {len(items_to_reextract)} PDF(s) serão reprocessados.", "success")
        return redirect(url_for('batch.batch_detail', id=id))
    
    except Exception as e:
        db.session.rollback()
        logger.error(f"[REEXTRACT] Erro: {e}", exc_info=True)
        flash(f"Erro ao reprocessar extração: {str(e)}", "danger")
        return redirect(url_for('batch.batch_detail', id=id))


@batch_bp.route("/<int:id>/rerpa", methods=["POST"])
@login_required
def batch_rerpa(id):
    """
    Reprocessar RPA (preenchimento eLaw) dos itens selecionados.
    Não refaz a extração, apenas o envio para o eLaw.
    """
    import threading
    from main import app as flask_app_main
    import rpa
    
    batch = BatchUpload.query.get_or_404(id)
    
    if batch.owner_id != current_user.id:
        flash("Você não tem permissão para reprocessar este batch.", "danger")
        return redirect(url_for('batch.batch_list'))
    
    item_ids_str = request.form.get('item_ids', '')
    if not item_ids_str:
        flash("Nenhum item selecionado para reprocessar.", "warning")
        return redirect(url_for('batch.batch_detail', id=id))
    
    try:
        item_ids = [int(x.strip()) for x in item_ids_str.split(',') if x.strip().isdigit()]
    except ValueError:
        flash("IDs de itens inválidos.", "danger")
        return redirect(url_for('batch.batch_detail', id=id))
    
    if not item_ids:
        flash("Nenhum item selecionado para reprocessar.", "warning")
        return redirect(url_for('batch.batch_detail', id=id))
    
    try:
        items_to_rerpa = BatchItem.query.filter(
            BatchItem.id.in_(item_ids),
            BatchItem.batch_id == id,
            BatchItem.process_id.isnot(None)
        ).all()
        
        if not items_to_rerpa:
            flash("Nenhum item com processo associado encontrado.", "warning")
            return redirect(url_for('batch.batch_detail', id=id))
        
        logger.info(f"[RERPA] Iniciando RPA para {len(items_to_rerpa)} itens do batch {id}")
        
        for item in items_to_rerpa:
            item.status = 'ready'
            item.last_error = None
            
            if item.process_id:
                process = Process.query.get(item.process_id)
                if process:
                    process.elaw_status = 'pending'
                    process.elaw_error_message = None
                    process.elaw_filled_at = None
        
        batch.status = 'ready'
        db.session.commit()
        
        # Calcular peso de dados para ordenação (menor primeiro = mais rápido)
        process_data = []
        for item in items_to_rerpa:
            process = Process.query.get(item.process_id)
            data_weight = 0
            if process:
                rpa_fields = [
                    'numero_processo', 'area_direito', 'estado', 'comarca',
                    'numero_orgao', 'orgao', 'celula', 'foro', 'instancia',
                    'assunto', 'cliente', 'posicao_parte_interessada',
                    'parte_interessada', 'parte_adversa_tipo', 'parte_adversa_nome',
                    'data_distribuicao', 'data_admissao', 'data_demissao',
                    'salario', 'cargo_funcao', 'pis', 'ctps', 'valor_causa',
                    'audiencia_inicial', 'link_audiencia', 'pedidos_json',
                    'outras_reclamadas_json'
                ]
                for field in rpa_fields:
                    val = getattr(process, field, None)
                    if val:
                        data_weight += 1
                        if field == 'pedidos_json' and len(str(val)) > 100:
                            data_weight += 2
                        if field == 'outras_reclamadas_json' and len(str(val)) > 50:
                            data_weight += 1
            process_data.append({'process_id': item.process_id, 'weight': data_weight})
        
        process_data.sort(key=lambda x: x['weight'])
        process_ids = [p['process_id'] for p in process_data]
        logger.info(f"[RERPA] Processos ordenados por peso de dados (menor→maior)")
        
        def execute_rerpa_background():
            with flask_app_main.app_context():
                try:
                    rpa.flask_app = flask_app_main
                    
                    batch_reload = BatchUpload.query.get(id)
                    if not batch_reload:
                        return
                    
                    batch_reload.status = 'running'
                    batch_reload.started_at = datetime.utcnow()
                    db.session.commit()
                    
                    logger.info(f"[RERPA] Iniciando RPA para {len(process_ids)} processos (ordenados por peso)")
                    
                    success_count = 0
                    error_count = 0
                    
                    for process_id in process_ids:
                        try:
                            process = Process.query.get(process_id)
                            if not process:
                                continue
                            
                            batch_item = BatchItem.query.filter_by(process_id=process_id).first()
                            if batch_item:
                                batch_item.status = 'running'
                                db.session.commit()
                            
                            success = rpa.fill_elaw_from_process(process_id)
                            
                            if success:
                                success_count += 1
                                if batch_item:
                                    batch_item.status = 'success'
                                logger.info(f"[RERPA] ✅ Processo #{process_id} preenchido com sucesso")
                            else:
                                error_count += 1
                                if batch_item:
                                    batch_item.status = 'error'
                                    batch_item.last_error = 'Falha no RPA'
                                logger.warning(f"[RERPA] ❌ Processo #{process_id} falhou")
                            
                            db.session.commit()
                            
                        except Exception as ex:
                            error_count += 1
                            logger.error(f"[RERPA] Erro no processo {process_id}: {ex}")
                    
                    batch_reload.status = 'completed' if error_count == 0 else 'partial_completed'
                    batch_reload.finished_at = datetime.utcnow()
                    db.session.commit()
                    
                    logger.info(f"[RERPA] ✅ Finalizado: {success_count} sucesso(s), {error_count} erro(s)")
                    
                except Exception as e:
                    logger.error(f"[RERPA] Erro fatal: {e}", exc_info=True)
                    try:
                        batch_reload = BatchUpload.query.get(id)
                        if batch_reload:
                            batch_reload.status = 'partial_completed'
                            db.session.commit()
                    except:
                        pass
        
        thread = threading.Thread(target=execute_rerpa_background, daemon=True)
        thread.start()
        
        flash(f"Reprocessamento RPA iniciado! {len(items_to_rerpa)} processo(s) serão enviados ao eLaw.", "success")
        return redirect(url_for('batch.batch_detail', id=id))
    
    except Exception as e:
        db.session.rollback()
        logger.error(f"[RERPA] Erro: {e}", exc_info=True)
        flash(f"Erro ao reprocessar RPA: {str(e)}", "danger")
        return redirect(url_for('batch.batch_detail', id=id))


@batch_bp.route("/<int:id>/delete", methods=["POST"])
@login_required
def batch_delete(id):
    """Deletar batch e todos os processos associados"""
    try:
        batch = BatchUpload.query.get(id)
        
        # Se batch não existe (já foi deletado), apenas redirecionar
        if not batch:
            logger.info(f"[BATCH DELETE] Batch #{id} já foi deletado (ignorando)")
            return redirect(url_for('batch.batch_list'))
        
        # Verificar propriedade
        if batch.owner_id != current_user.id:
            flash("Você não tem permissão para deletar este batch.", "danger")
            return redirect(url_for('batch.batch_list'))
        
        # Coletar process_ids dos itens
        items = BatchItem.query.filter_by(batch_id=id).all()
        process_ids = [item.process_id for item in items if item.process_id]
        
        # Deletar processos associados
        if process_ids:
            Process.query.filter(Process.id.in_(process_ids)).delete(synchronize_session=False)
            logger.info(f"[BATCH DELETE] Deletados {len(process_ids)} processos do batch #{id}")
        
        # Deletar batch (BatchItems serão deletados por CASCADE)
        db.session.delete(batch)
        db.session.commit()
        
        # Resetar sequências se tabelas ficaram vazias
        reset_all_sequences_if_empty()
        
        flash(f"Batch #{id} e {len(process_ids)} processo(s) deletados com sucesso!", "success")
        logger.info(f"[BATCH DELETE] Batch #{id} deletado pelo usuário #{current_user.id}")
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Erro ao deletar batch {id}: {e}", exc_info=True)
        flash(f"Erro ao deletar batch: {str(e)}", "danger")
    
    return redirect(url_for('batch.batch_list'))


@batch_bp.route("/delete-multiple", methods=["POST"])
@login_required
def batch_delete_multiple():
    """Deletar múltiplos batches e seus processos"""
    try:
        batch_ids = request.form.getlist('batch_ids')
        
        if not batch_ids:
            flash("Nenhum batch selecionado.", "warning")
            return redirect(url_for('batch.batch_list'))
        
        # Converter para inteiros
        batch_ids = [int(bid) for bid in batch_ids]
        
        # Buscar batches do usuário
        batches = BatchUpload.query.filter(
            BatchUpload.id.in_(batch_ids),
            BatchUpload.owner_id == current_user.id
        ).all()
        
        if not batches:
            flash("Nenhum batch válido encontrado.", "warning")
            return redirect(url_for('batch.batch_list'))
        
        total_processes = 0
        
        for batch in batches:
            # Coletar process_ids dos itens
            items = BatchItem.query.filter_by(batch_id=batch.id).all()
            process_ids = [item.process_id for item in items if item.process_id]
            
            # Deletar processos associados
            if process_ids:
                Process.query.filter(Process.id.in_(process_ids)).delete(synchronize_session=False)
                total_processes += len(process_ids)
            
            # Deletar batch
            db.session.delete(batch)
        
        db.session.commit()
        
        # Resetar sequências se tabelas ficaram vazias
        reset_all_sequences_if_empty()
        
        flash(f"{len(batches)} batch(es) e {total_processes} processo(s) deletados com sucesso!", "success")
        logger.info(f"[BATCH DELETE MULTIPLE] {len(batches)} batches deletados pelo usuário #{current_user.id}")
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Erro ao deletar batches múltiplos: {e}", exc_info=True)
        flash(f"Erro ao deletar batches: {str(e)}", "danger")
    
    return redirect(url_for('batch.batch_list'))


# =============================================================================
# RE-EXTRAÇÃO OCR SELETIVA (FALLBACK PARA CAMPOS CRÍTICOS VAZIOS)
# =============================================================================
@batch_bp.route("/reextract-ocr", methods=["GET", "POST"])
@login_required
def reextract_ocr():
    """
    Página de Re-extração OCR para campos críticos vazios.
    
    Permite ao usuário:
    1. Ver estatísticas de campos vazios (salário, PIS, CTPS)
    2. Iniciar re-extração OCR seletiva em lote
    3. Acompanhar progresso
    """
    from sqlalchemy import or_
    
    # Estatísticas de campos vazios
    stats = {
        'total': Process.query.filter_by(user_id=current_user.id).count(),
        'sem_salario': Process.query.filter(
            Process.user_id == current_user.id,
            or_(Process.salario.is_(None), Process.salario == "")
        ).count(),
        'sem_pis': Process.query.filter(
            Process.user_id == current_user.id,
            or_(Process.pis.is_(None), Process.pis == "")
        ).count(),
        'sem_ctps': Process.query.filter(
            Process.user_id == current_user.id,
            or_(Process.ctps.is_(None), Process.ctps == "")
        ).count()
    }
    
    if request.method == "POST":
        # Iniciar re-extração em lote
        fields = request.form.getlist('fields') or ['salario', 'pis', 'ctps']
        limit = int(request.form.get('limit', 20))
        
        try:
            from extractors.reextract import batch_reextract_missing
            
            # Pasta de uploads
            upload_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
            
            result = batch_reextract_missing(
                session=db.session,
                ProcessModel=Process,
                upload_folder=upload_folder,
                limit=limit,
                fields=fields,
                user_id=current_user.id
            )
            
            if result.get('error'):
                flash(f"Erro: {result['error']}", "danger")
            else:
                flash(
                    f"Re-extração concluída! "
                    f"Processados: {result['total_processados']}, "
                    f"Campos recuperados: {result['campos_recuperados']}, "
                    f"Erros: {result['erros']}",
                    "success"
                )
            
            return redirect(url_for('batch.reextract_ocr'))
            
        except Exception as e:
            logger.error(f"[REEXTRACT] Erro: {e}", exc_info=True)
            flash(f"Erro na re-extração: {str(e)}", "danger")
    
    return render_template('processes/reextract_ocr.html', stats=stats)


@batch_bp.route("/reextract-single/<int:process_id>", methods=["POST"])
@login_required
def reextract_single(process_id):
    """Re-extrai campos de um processo específico via OCR"""
    from extractors.reextract import reextract_missing_fields, get_missing_critical_fields, find_pdf_path
    
    process = Process.query.get_or_404(process_id)
    
    if process.user_id != current_user.id:
        return jsonify({"error": "Acesso negado"}), 403
    
    upload_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
    pdf_path = find_pdf_path(process, upload_folder)
    
    if not pdf_path or not Path(pdf_path).exists():
        return jsonify({"error": "PDF não encontrado"}), 404
    
    existing_data = {
        'salario': process.salario or "",
        'pis': process.pis or "",
        'ctps': process.ctps or ""
    }
    
    missing = get_missing_critical_fields(existing_data)
    if not missing:
        return jsonify({"message": "Todos campos críticos já preenchidos", "extracted": {}})
    
    try:
        extracted = reextract_missing_fields(
            process_id=process.id,
            pdf_path=pdf_path,
            existing_data=existing_data,
            fields_to_extract=missing
        )
        
        if extracted:
            for field, value in extracted.items():
                setattr(process, field, value)
            db.session.commit()
        
        return jsonify({
            "message": f"Re-extração concluída: {len(extracted)} campos recuperados",
            "extracted": extracted
        })
        
    except Exception as e:
        logger.error(f"[REEXTRACT_SINGLE] Erro: {e}", exc_info=True)
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@batch_bp.route("/queue-ocr-batch", methods=["POST"])
@login_required
def queue_ocr_batch():
    """
    Enfileira OCR assíncrono para todos os processos com campos trabalhistas faltantes.
    Usa o worker de background do servidor para processar e salvar no banco.
    
    2025-12-05: Criado para reprocessar OCR com timeout aumentado (120s).
    """
    from sqlalchemy import or_
    from extractors.ocr_utils import queue_ocr_task, extract_pdf_bookmarks, get_pdf_total_pages, get_ocr_queue_status
    
    try:
        # Encontrar processos com campos trabalhistas faltantes
        processes_with_missing = Process.query.filter(
            Process.user_id == current_user.id,
            or_(
                Process.pis.is_(None), Process.pis == "",
                Process.ctps.is_(None), Process.ctps == "",
                Process.data_admissao.is_(None), Process.data_admissao == "",
                Process.data_demissao.is_(None), Process.data_demissao == ""
            )
        ).limit(20).all()
        
        if not processes_with_missing:
            return jsonify({"message": "Nenhum processo com campos faltantes encontrado", "queued": 0})
        
        queued_count = 0
        errors = []
        
        for process in processes_with_missing:
            # Identificar campos faltantes
            missing = []
            if not process.pis: missing.append("pis")
            if not process.ctps: missing.append("ctps")
            if not process.data_admissao: missing.append("data_admissao")
            if not process.data_demissao: missing.append("data_demissao")
            
            if not missing:
                continue
            
            # Encontrar PDF associado
            batch_item = BatchItem.query.filter_by(process_id=process.id).first()
            if not batch_item or not batch_item.upload_path:
                errors.append(f"Processo {process.id}: PDF não encontrado")
                continue
            
            pdf_path = batch_item.upload_path
            if not os.path.exists(pdf_path):
                errors.append(f"Processo {process.id}: Arquivo não existe")
                continue
            
            # Determinar páginas para OCR
            docs_needed = set()
            if any(f in missing for f in ["data_admissao", "pis", "ctps"]):
                docs_needed.add("ctps")
            if "data_demissao" in missing:
                docs_needed.add("trct")
            
            total_pages = get_pdf_total_pages(pdf_path)
            bookmarks = extract_pdf_bookmarks(pdf_path)
            
            doc_pages = {}
            for doc in docs_needed:
                if doc in bookmarks:
                    doc_pages[doc] = bookmarks[doc]
                else:
                    # Heurística
                    if doc == "trct":
                        doc_pages[doc] = max(1, int(total_pages * 0.87))
                    elif doc == "ctps":
                        doc_pages[doc] = max(1, int(total_pages * 0.82))
            
            # Enfileirar para OCR
            if doc_pages:
                queued = queue_ocr_task(process.id, pdf_path, doc_pages, missing)
                if queued:
                    queued_count += 1
                    logger.info(f"[OCR-BATCH] Enfileirado processo {process.id}: {missing}")
        
        status = get_ocr_queue_status()
        
        return jsonify({
            "message": f"OCR enfileirado para {queued_count} processos",
            "queued": queued_count,
            "queue_status": status,
            "errors": errors[:5] if errors else []
        })
        
    except Exception as e:
        logger.error(f"[OCR-BATCH] Erro: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
