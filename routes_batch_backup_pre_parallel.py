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
MAX_FILE_SIZE = 16 * 1024 * 1024  # 16MB por arquivo
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
# Configuração de Processamento Paralelo
# =============================================================================
MAX_EXTRACTION_WORKERS = 4  # Número máximo de threads para extração de PDFs
MAX_RPA_WORKERS = 4  # Número máximo de threads para execução RPA paralela


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
                
                # Criar processo no banco
                process_id = _create_process_from_data(extracted_data, user_id)
                
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
        
        # Coletar itens pendentes
        pending_items = BatchItem.query.filter_by(batch_id=batch_id, status='pending').all()
        total_items = len(pending_items)
        logger.info(f"[BATCH] {total_items} itens pendentes para processar em paralelo (max {MAX_EXTRACTION_WORKERS} workers)")
        
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
            
            # Processar resultados à medida que ficam prontos
            for future in as_completed(future_to_item):
                item_data = future_to_item[future]
                
                try:
                    result = future.result()
                    
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
                    
                except Exception as ex:
                    errors += 1
                    logger.error(f"[BATCH] ❌ Exceção no future do item {item_data['item_id']}: {ex}")
        
        # Finalizar batch
        batch.status = 'ready' if errors == 0 else 'completed'
        batch.processed_count = processed + errors
        batch.finished_at = datetime.utcnow()
        db.session.commit()
        
        logger.info(f"[BATCH] ✅ Batch {batch_id} finalizado: {processed} sucesso(s), {errors} erro(s) em {total_items} itens")


@batch_bp.route("/new", methods=["GET", "POST"])
@login_required
def batch_new():
    """Upload de múltiplos PDFs"""
    if request.method == "POST":
        files = request.files.getlist('pdfs')
        
        if not files or len(files) == 0:
            flash("Nenhum arquivo selecionado.", "danger")
            return redirect(request.url)
        
        if len(files) > MAX_FILES_PER_BATCH:
            flash(f"Máximo de {MAX_FILES_PER_BATCH} arquivos por vez.", "danger")
            return redirect(request.url)
        
        # Validar arquivos
        valid_files = []
        total_size = 0
        MAX_TOTAL_SIZE = 350 * 1024 * 1024  # 350MB total
        
        for file in files:
            if file.filename == '':
                continue
            
            if not allowed_file(file.filename):
                flash(f"Arquivo '{file.filename}' não é um PDF válido.", "danger")
                return redirect(request.url)
            
            # Verificar tamanho (aproximado)
            file.seek(0, os.SEEK_END)
            size = file.tell()
            file.seek(0)
            
            if size > MAX_FILE_SIZE:
                flash(f"Arquivo '{file.filename}' excede {MAX_FILE_SIZE // (1024*1024)}MB.", "danger")
                return redirect(request.url)
            
            total_size += size
            valid_files.append(file)
        
        # Verificar limite total
        if total_size > MAX_TOTAL_SIZE:
            total_mb = total_size / (1024 * 1024)
            flash(f"Tamanho total dos arquivos ({total_mb:.1f}MB) excede o limite de 350MB.", "danger")
            return redirect(request.url)
        
        if not valid_files:
            flash("Nenhum arquivo válido para processar.", "danger")
            return redirect(request.url)
        
        try:
            # Criar batch
            batch = BatchUpload(
                owner_id=current_user.id,
                status='uploading',
                total_count=len(valid_files)
            )
            db.session.add(batch)
            db.session.flush()  # Obter batch.id
            
            # Criar diretório para este batch
            batch_dir = Path('uploads') / 'batch' / str(batch.id)
            batch_dir.mkdir(parents=True, exist_ok=True)
            
            # Salvar arquivos de forma otimizada (sem flush intermediário)
            batch_items = []
            for file in valid_files:
                filename = secure_filename(file.filename)
                filepath = batch_dir / filename
                
                # Salvar arquivo diretamente (sem buffer intermediário)
                file.save(str(filepath))
                
                item = BatchItem(
                    batch_id=batch.id,
                    source_filename=filename,
                    upload_path=str(filepath),
                    status='pending'
                )
                batch_items.append(item)
                db.session.add(item)
            
            # Commit único de todos os items
            batch.status = 'pending'
            db.session.commit()
            
            # Redirecionar para tela de progresso IMEDIATAMENTE
            # Processar em background via thread
            import threading
            
            # Iniciar processamento em background ANTES de redirecionar
            thread = threading.Thread(target=process_batch_async, args=(batch.id, current_user.id))
            thread.daemon = True
            thread.start()
            logger.info(f"[BATCH] Thread de processamento iniciada para batch {batch.id}")
            
            # Toast de sucesso
            flash(f"Batch criado com sucesso! {len(valid_files)} arquivo(s) em processamento.", "success")
            
            # Redirecionar para tela de progresso
            return redirect(url_for('batch.batch_progress', id=batch.id))
        
        except Exception as e:
            db.session.rollback()
            logger.error(f"Erro ao criar batch: {e}")
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
    
    # Carregar items com eager loading dos processos
    items = BatchItem.query.filter_by(batch_id=id).order_by(BatchItem.id).all()
    
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
    batch = BatchUpload.query.get_or_404(id)
    
    # Verificar permissão
    if batch.owner_id != current_user.id:
        return jsonify({'error': 'Permissão negada'}), 403
    
    items = BatchItem.query.filter_by(batch_id=id).order_by(BatchItem.id).all()
    
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


def _execute_single_rpa(item_id: int, process_id: int) -> dict:
    """
    Executa RPA para um único processo de forma thread-safe.
    
    Cada thread tem sua própria sessão do banco de dados para evitar conflitos.
    
    Args:
        item_id: ID do BatchItem
        process_id: ID do Process a ser processado
        
    Returns:
        dict com resultado: {'item_id': int, 'process_id': int, 'success': bool, 'error': str|None}
    """
    from main import app
    import rpa
    
    result = {
        'item_id': item_id,
        'process_id': process_id,
        'success': False,
        'error': None
    }
    
    try:
        logger.info(f"[RPA][THREAD] Iniciando RPA para item {item_id}, processo {process_id}")
        
        # ✅ CRÍTICO: Cada thread precisa de seu próprio app_context para sessão DB isolada
        with app.app_context():
            # Atualizar status para 'running'
            item = BatchItem.query.get(item_id)
            if not item:
                result['error'] = f'Item {item_id} não encontrado'
                logger.error(f"[RPA][THREAD] {result['error']}")
                return result
            
            item.status = 'running'
            item.attempt_count += 1
            item.updated_at = datetime.utcnow()
            db.session.commit()
            
            # Executar RPA
            logger.info(f"[RPA][THREAD] Executando execute_rpa({process_id})")
            rpa_result = rpa.execute_rpa(process_id)
            logger.info(f"[RPA][THREAD] execute_rpa retornou: {rpa_result}")
            
            # Processar resultado
            if rpa_result.get('status') == 'success':
                item.status = 'success'
                item.last_error = None
                result['success'] = True
                logger.info(f"[RPA][THREAD] ✅ Item {item_id} processado com sucesso!")
            else:
                item.status = 'error'
                item.last_error = rpa_result.get('error', rpa_result.get('message', 'Erro desconhecido'))[:500]
                result['error'] = item.last_error
                logger.warning(f"[RPA][THREAD] ❌ Item {item_id} com erro: {item.last_error}")
            
            item.updated_at = datetime.utcnow()
            db.session.commit()
                
    except Exception as ex:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"[RPA][THREAD] ❌ Exceção ao processar item {item_id}: {ex}")
        logger.error(f"[RPA][THREAD][TRACEBACK] {tb}")
        
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
            logger.error(f"[RPA][THREAD] Erro ao atualizar status do item {item_id}: {db_ex}")
    
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
                                items_data.append({
                                    'item_id': item.id,
                                    'process_id': item.process_id
                                })
                        db.session.commit()
                        
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
                            # Submeter todas as tarefas
                            future_to_item = {
                                executor.submit(
                                    _execute_single_rpa,
                                    item_data['item_id'],
                                    item_data['process_id']
                                ): item_data
                                for item_data in items_data
                            }
                            
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
        batch = BatchUpload.query.get(id)
        if not batch:
            return jsonify({'success': False, 'error': 'Batch não encontrado'}), 404
        
        # Verificar permissão
        if batch.owner_id != current_user.id:
            return jsonify({'success': False, 'error': 'Permissão negada'}), 403
        
        items = BatchItem.query.filter_by(batch_id=id).order_by(BatchItem.id).all()
        
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
    """Reenfileira item com erro para reprocessamento"""
    item = BatchItem.query.get_or_404(id)
    batch = item.batch
    
    # Verificar permissão
    if batch.owner_id != current_user.id:
        return jsonify({'error': 'Permissão negada'}), 403
    
    # Verificar se item pode ser reprocessado
    if item.status not in ['error']:
        return jsonify({'error': f'Item não está em estado de erro (status: {item.status})'}), 400
    
    try:
        # Resetar status
        item.status = 'ready'
        item.last_error = None
        db.session.commit()
        
        flash(f"Item '{item.source_filename}' reenfileirado com sucesso!", "success")
        return redirect(url_for('batch.batch_detail', id=batch.id))
    
    except Exception as e:
        db.session.rollback()
        logger.error(f"Erro ao reenfileirar item {id}: {e}")
        return jsonify({'error': str(e)}), 500


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
            """Executa reprocessamento completo: EXTRAÇÃO + RPA"""
            with flask_app_main.app_context():
                try:
                    # ✅ FIX: Garantir que rpa.py usa o flask_app correto
                    rpa.flask_app = flask_app_main
                    logger.info(f"[BATCH REPROCESS] Flask app context configurado (user_id={user_id})")
                    
                    batch_reload = BatchUpload.query.get(id)
                    if not batch_reload:
                        logger.error(f"Batch {id} não encontrado no background")
                        return
                    
                    # FASE 1: EXTRAÇÃO DOS PDFs
                    logger.info(f"[BATCH REPROCESS] ══════ FASE 1: EXTRAÇÃO ══════")
                    batch_reload.status = 'processing'
                    batch_reload.started_at = datetime.utcnow()
                    db.session.commit()
                    
                    from extractors.pipeline import run_extraction_from_file
                    
                    pending_items = BatchItem.query.filter_by(batch_id=id, status='pending').all()
                    logger.info(f"[BATCH REPROCESS] {len(pending_items)} itens para extrair")
                    
                    extracted_count = 0
                    extraction_errors = 0
                    
                    for item in pending_items:
                        try:
                            item.status = 'extracting'
                            db.session.commit()
                            logger.info(f"[BATCH REPROCESS] Extraindo dados: {item.source_filename}")
                            
                            # Extrair dados do PDF
                            extracted_data = run_extraction_from_file(
                                path=item.upload_path,
                                filename=item.source_filename
                            )
                            
                            if extracted_data:
                                # ✅ CRÍTICO: Incluir pdf_filename para permitir extração de reclamadas no RPA
                                # Usar caminho relativo completo (batch/<id>/arquivo.pdf) para RPA encontrar
                                extracted_data["pdf_filename"] = item.upload_path  # ex: uploads/batch/68/arquivo.pdf
                                
                                # Criar processo no banco (usando user_id capturado antes da thread)
                                process_id = _create_process_from_data(extracted_data, user_id)
                                item.process_id = process_id
                                item.status = 'ready'
                                extracted_count += 1
                                logger.info(f"[BATCH REPROCESS] ✅ Item {item.id} extraído! Process ID: {process_id}, PDF: {extracted_data['pdf_filename']}")
                            else:
                                item.status = 'error'
                                item.last_error = 'Falha na extração de dados'
                                extraction_errors += 1
                                logger.warning(f"[BATCH REPROCESS] ❌ Erro na extração do item {item.id}")
                                
                        except Exception as ex:
                            logger.error(f"[BATCH REPROCESS] Erro ao extrair item {item.id}: {ex}", exc_info=True)
                            item.status = 'error'
                            item.last_error = str(ex)[:500]
                            extraction_errors += 1
                        
                        item.updated_at = datetime.utcnow()
                        db.session.commit()
                    
                    logger.info(f"[BATCH REPROCESS] Extração finalizada: {extracted_count} sucesso(s), {extraction_errors} erro(s)")
                    
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


@batch_bp.route("/<int:id>/delete", methods=["POST"])
@login_required
def batch_delete(id):
    """Deletar batch e todos os processos associados"""
    try:
        batch = BatchUpload.query.get_or_404(id)
        
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
        
        flash(f"{len(batches)} batch(es) e {total_processes} processo(s) deletados com sucesso!", "success")
        logger.info(f"[BATCH DELETE MULTIPLE] {len(batches)} batches deletados pelo usuário #{current_user.id}")
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Erro ao deletar batches múltiplos: {e}", exc_info=True)
        flash(f"Erro ao deletar batches: {str(e)}", "danger")
    
    return redirect(url_for('batch.batch_list'))
