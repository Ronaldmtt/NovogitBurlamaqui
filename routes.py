# routes.py
from __future__ import annotations

import io
import os
import re
import sys
import json
import logging
import subprocess
import time
from pathlib import Path
from datetime import datetime
from collections import Counter

logger = logging.getLogger(__name__)

# Sistema de Logging Centralizado
try:
    from logging_config import (
        log_event, log_start, log_end, log_success, log_error as log_err,
        log_warning, log_debug, auth, ui, extraction, timed_operation
    )
    LOGGING_AVAILABLE = True
except ImportError:
    LOGGING_AVAILABLE = False
    def log_event(*args, **kwargs): pass
    def log_start(*args, **kwargs): pass
    def log_end(*args, **kwargs): pass
    def log_success(*args, **kwargs): pass
    def log_err(*args, **kwargs): pass
    def log_warning(*args, **kwargs): pass
    def log_debug(*args, **kwargs): pass

# Integração com monitor remoto (RPA Monitor Client)
try:
    from monitor_integration import log_info, log_warning as monitor_warn, log_error, send_screenshot
    MONITOR_AVAILABLE = True
except ImportError:
    MONITOR_AVAILABLE = False
    def log_info(msg, region=""): pass
    def monitor_warn(msg, region=""): pass
    def log_error(msg, exc=None, region="", screenshot_path=None): pass
    def send_screenshot(path, region=""): pass

from flask import (
    Blueprint, render_template, render_template_string, request, redirect,
    url_for, flash, session, current_app, send_from_directory, abort, jsonify
)
from flask_login import login_required, login_user, logout_user, current_user
from jinja2 import TemplateNotFound
from werkzeug.security import check_password_hash

from sqlalchemy.exc import SQLAlchemyError

from models import db, User, Process
from forms import LoginForm  # <-- usa seu forms.py
from extractors import (
    extract_text_from_pdf,
    run_extraction_from_text,
    run_extraction_from_file,
)
# Se você usa o parser direto aqui também:
from extractors.cadastro import parse_pdf_text

bp = Blueprint("core", __name__)


def _reset_process_sequence_if_empty():
    """
    Reseta a sequência de IDs da tabela process se ela estiver vazia.
    Isso permite que os IDs comecem novamente do 1.
    """
    from sqlalchemy import text
    
    try:
        result = db.session.execute(text("SELECT COUNT(*) FROM process"))
        count = result.scalar()
        
        if count == 0:
            db.session.execute(text("ALTER SEQUENCE process_id_seq RESTART WITH 1"))
            db.session.commit()
            logger.info("[RESET_SEQ] ✅ Sequência process_id_seq resetada para 1")
            log_info("Sequência process_id_seq resetada para 1", region="ROUTES")
            return True
    except Exception as e:
        logger.warning(f"[RESET_SEQ] Não foi possível resetar sequência de process: {e}")
        monitor_warn(f"Não foi possível resetar sequência de process: {e}", region="ROUTES")
        db.session.rollback()
    
    return False

# ============================================================
# Rotas públicas / auth
# ============================================================

@bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("core.dashboard"))
    return redirect(url_for("core.login"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        log_debug("LOGIN", "Usuário já autenticado, redirecionando para dashboard")
        return redirect(url_for("core.dashboard"))

    next_url = request.args.get("next") or url_for("core.dashboard")
    form = LoginForm()

    if request.method == "POST":
        log_start("LOGIN", "Processando tentativa de login")
        
        if form.validate_on_submit():
            username_or_email = (form.username.data or "").strip()
            password = form.password.data or ""
            
            auth.login_attempt(username_or_email)
            log_debug("LOGIN", f"Buscando usuário no banco", username=username_or_email)

            user = (
                User.query.filter_by(username=username_or_email).first()
                or User.query.filter_by(email=username_or_email).first()
            )
            
            log_debug("LOGIN", f"Usuário encontrado: {user is not None}", username=username_or_email)

            ok = False
            if user:
                if hasattr(user, "check_password"):
                    ok = user.check_password(password)
                elif hasattr(user, "verify_password"):
                    ok = user.verify_password(password)
                else:
                    ok = check_password_hash(getattr(user, "password_hash", ""), password)
                
                log_debug("LOGIN", f"Verificação de senha: {'OK' if ok else 'FALHOU'}", username=username_or_email)

            if not user or not ok:
                auth.login_failed(username_or_email, reason="Credenciais inválidas")
                log_end("LOGIN", "Login falhou - credenciais inválidas", username=username_or_email)
                monitor_warn(f"Login falhou para usuário: {username_or_email}", region="ROUTES")
                flash("Credenciais inválidas.", "danger")
                return render_template("login.html", form=form, next_url=next_url)

            login_user(user)
            auth.login_success(username_or_email, user_id=user.id)
            log_success("LOGIN", f"Login realizado com sucesso", username=user.username, user_id=user.id)
            log_end("LOGIN", "Processo de login concluído com sucesso")
            log_info(f"Login realizado com sucesso: {user.username}", region="ROUTES")
            flash("Login efetuado com sucesso.", "success")
            return redirect(next_url)
        else:
            log_warning("LOGIN", f"Formulário inválido", errors=str(form.errors))
            log_end("LOGIN", "Login falhou - formulário inválido")

    ui.page_view("login")
    return render_template("login.html", form=form, next_url=next_url)


@bp.route("/logout")
@login_required
def logout():
    username = current_user.username if current_user else "unknown"
    user_id = current_user.id if current_user else None
    log_start("LOGOUT", f"Usuário {username} saindo do sistema")
    auth.logout(username, user_id=user_id)
    logout_user()
    log_success("LOGOUT", f"Logout realizado com sucesso")
    log_end("LOGOUT", "Sessão encerrada")
    log_info(f"Logout realizado: {username}", region="ROUTES")
    flash("Você saiu da sessão.", "success")
    return redirect(url_for("core.login"))


@bp.route("/uploads/<filename>")
@login_required
def uploaded_file(filename):
    """Serve PDF files from uploads directory"""
    uploads_dir = Path(__file__).parent / "uploads"
    return send_from_directory(uploads_dir, filename)


# ============================================================
# Dashboard
# ============================================================

@bp.route("/dashboard")
@login_required
def dashboard():
    total_processes = Process.query.count()
    user_processes = Process.query.filter_by(owner_id=current_user.id).count()
    recent_processes = Process.query.order_by(Process.created_at.desc()).limit(10).all()
    return render_template(
        "dashboard.html",
        total_processes=total_processes,
        user_processes=user_processes,
        recent_processes=recent_processes,
    )

# ============================================================
# Processos
# ============================================================

@bp.route("/processos")
@login_required
def process_list():
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '', type=str)
    
    query = Process.query
    if search:
        query = query.filter(
            (Process.cnj.ilike(f'%{search}%')) |
            (Process.numero_processo.ilike(f'%{search}%')) |
            (Process.assunto.ilike(f'%{search}%')) |
            (Process.objeto.ilike(f'%{search}%'))
        )
    
    processes = query.order_by(Process.created_at.desc()).paginate(
        page=page, per_page=20, error_out=False
    )
    return render_template("processes/list.html", processes=processes, search=search)


@bp.route("/processos/<int:id>")
@login_required
def process_view(id: int):
    proc = Process.query.get_or_404(id)
    batch_id = request.args.get('batch_id', type=int)
    return render_template("processes/view.html", process=proc, batch_id=batch_id)


@bp.route("/processos/<int:id>/screenshot")
@login_required
def process_screenshot(id: int):
    """Serve o screenshot PNG do formulário eLaw preenchido (DEPRECATED - usar /screenshot/before ou /screenshot/after)"""
    proc = Process.query.get_or_404(id)
    
    # Verificar ownership
    if proc.owner_id != current_user.id:
        abort(403)
    
    # Verificar se screenshot existe
    if not proc.elaw_screenshot_path:
        abort(404)
    
    # Servir arquivo do diretório rpa_screenshots (NÃO static!)
    screenshot_filename = Path(proc.elaw_screenshot_path).name
    screenshot_dir = Path('/home/runner/workspace/rpa_screenshots')
    
    return send_from_directory(
        directory=str(screenshot_dir),
        path=screenshot_filename,
        as_attachment=False
    )


@bp.route("/rpa_screenshots/<path:filename>")
@login_required
def serve_rpa_screenshot(filename):
    """Serve screenshots do RPA (before/after) - procura em ambos diretórios"""
    # Normalizar filename: remover prefixo 'rpa_screenshots/' se presente (legado)
    # Isso garante compatibilidade com paths salvos como "rpa_screenshots/file.png" e "file.png"
    clean_filename = Path(filename).name
    
    # Tentar primeiro o novo diretório (static/rpa_screenshots)
    screenshot_dir_new = Path('static') / 'rpa_screenshots'
    full_path_new = screenshot_dir_new / clean_filename
    
    if full_path_new.exists() and full_path_new.is_file():
        return send_from_directory(
            directory=str(screenshot_dir_new),
            path=clean_filename,
            as_attachment=False
        )
    
    # Fallback para diretório legado (screenshots de erro, login, etc)
    screenshot_dir_legacy = Path('/home/runner/workspace/rpa_screenshots')
    full_path_legacy = screenshot_dir_legacy / clean_filename
    
    if full_path_legacy.exists() and full_path_legacy.is_file():
        return send_from_directory(
            directory=str(screenshot_dir_legacy),
            path=clean_filename,
            as_attachment=False
        )
    
    # Arquivo não encontrado - retornar imagem placeholder SVG
    svg_placeholder = '''<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300" viewBox="0 0 400 300">
      <rect fill="#f8f9fa" width="400" height="300"/>
      <text x="200" y="140" text-anchor="middle" font-family="Arial, sans-serif" font-size="16" fill="#6c757d">Screenshot não disponível</text>
      <text x="200" y="165" text-anchor="middle" font-family="Arial, sans-serif" font-size="12" fill="#adb5bd">Arquivo foi processado em outro ambiente</text>
    </svg>'''
    from flask import Response
    return Response(svg_placeholder, mimetype='image/svg+xml')


@bp.route("/processos/<int:id>/update-field", methods=["POST"])
@login_required
def process_update_field(id: int):
    """Atualiza um campo individual do processo via AJAX (para edição inline)."""
    proc = Process.query.get_or_404(id)
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Dados inválidos'}), 400
        
        field_name = data.get('field')
        field_value = data.get('value', '').strip()
        
        # Campos protegidos que não podem ser editados
        protected_fields = ['id', 'created_at', 'updated_at', 'owner_id', 'created_by', 
                           'elaw_status', 'elaw_filled_at', 'elaw_error_message',
                           'elaw_screenshot_before_path', 'elaw_screenshot_after_path',
                           'elaw_screenshot_path', 'elaw_detail_url', 
                           'elaw_screenshot_reclamadas_path', 'elaw_screenshot_pedidos_path']
        
        if not field_name:
            return jsonify({'success': False, 'error': 'Nome do campo não informado'}), 400
        
        if field_name in protected_fields:
            return jsonify({'success': False, 'error': 'Campo protegido'}), 403
        
        if not hasattr(proc, field_name):
            return jsonify({'success': False, 'error': f'Campo {field_name} não existe'}), 400
        
        # Atualizar o campo
        setattr(proc, field_name, field_value if field_value else None)
        proc.updated_by = current_user.id
        
        db.session.commit()
        
        return jsonify({
            'success': True, 
            'field': field_name, 
            'value': field_value,
            'message': f'Campo {field_name} atualizado com sucesso'
        })
        
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route("/processos/<int:id>/editar", methods=["GET", "POST"])
@login_required
def process_edit(id: int):
    proc = Process.query.get_or_404(id)
    batch_id = request.args.get('batch_id', type=int)
    
    if request.method == "POST":
        form_data = request.form.to_dict(flat=True)
        
        # Atualizar todos os campos do processo
        for field, value in form_data.items():
            if hasattr(proc, field) and field not in ['id', 'created_at', 'updated_at', 'owner_id', 'created_by']:
                setattr(proc, field, value if value else None)
        
        # Atualizar campos booleanos (checkbox envia "on" quando marcado)
        proc.cadastrar_primeira_audiencia = form_data.get('cadastrar_primeira_audiencia') in ['on', 'Sim', '1', 'true']
        
        # Atualizar updated_by
        proc.updated_by = current_user.id
        
        try:
            db.session.commit()
            flash(f"Processo #{proc.id} atualizado com sucesso!", "success")
            # Redirecionar de volta ao batch se veio de lá
            if batch_id:
                return redirect(url_for("core.process_view", id=proc.id, batch_id=batch_id))
            else:
                return redirect(url_for("core.process_view", id=proc.id))
        except SQLAlchemyError as e:
            db.session.rollback()
            flash(f"Erro ao atualizar processo: {e}", "danger")
    
    # GET: preparar dados para o formulário (mesma estrutura do confirm_extracted)
    data = {
        'cnj': proc.cnj,
        'tipo_processo': proc.tipo_processo,
        'numero_processo': proc.numero_processo,
        'numero_processo_antigo': proc.numero_processo_antigo,
        'sistema_eletronico': proc.sistema_eletronico,
        'area_direito': proc.area_direito,
        'sub_area_direito': proc.sub_area_direito,
        'estado': proc.estado,
        'comarca': proc.comarca,
        'numero_orgao': proc.numero_orgao,
        'origem': proc.origem,
        'orgao': proc.orgao,
        'vara': proc.vara,
        'celula': proc.celula,
        'foro': proc.foro,
        'instancia': proc.instancia,
        'assunto': proc.assunto,
        'npc': proc.npc,
        'objeto': proc.objeto,
        'sub_objeto': proc.sub_objeto,
        'cliente': proc.cliente,
        'parte': proc.parte,
        'cliente_parte': proc.cliente_parte,
        'advogado_autor': proc.advogado_autor,
        'advogado_reu': proc.advogado_reu,
        'audiencia_inicial': proc.audiencia_inicial,
        'prazo': proc.prazo,
        'tipo_notificacao': proc.tipo_notificacao,
        'resultado_audiencia': proc.resultado_audiencia,
        'prazos_derivados_audiencia': proc.prazos_derivados_audiencia,
        'decisao_tipo': proc.decisao_tipo,
        'decisao_resultado': proc.decisao_resultado,
        'decisao_fundamentacao_resumida': proc.decisao_fundamentacao_resumida,
        'id_interno_hilo': proc.id_interno_hilo,
        'estrategia': proc.estrategia,
        'indice_atualizacao': proc.indice_atualizacao,
        'posicao_parte_interessada': proc.posicao_parte_interessada,
        'parte_interessada': proc.parte_interessada,
        'parte_adversa_tipo': proc.parte_adversa_tipo,
        'parte_adversa_nome': proc.parte_adversa_nome,
        'escritorio_parte_adversa': proc.escritorio_parte_adversa,
        'uf_oab_advogado_adverso': proc.uf_oab_advogado_adverso,
        'cpf_cnpj_parte_adversa': proc.cpf_cnpj_parte_adversa,
        'telefone_parte_adversa': proc.telefone_parte_adversa,
        'email_parte_adversa': proc.email_parte_adversa,
        'endereco_parte_adversa': proc.endereco_parte_adversa,
        'data_distribuicao': proc.data_distribuicao,
        'data_citacao': proc.data_citacao,
        'risco': proc.risco,
        'valor_causa': proc.valor_causa,
        'rito': proc.rito,
        'observacao': proc.observacao,
        'cadastrar_primeira_audiencia': 'Sim' if proc.cadastrar_primeira_audiencia else 'Não',
        # Campos trabalhistas (RPA)
        'data_admissao': proc.data_admissao,
        'data_demissao': proc.data_demissao,
        'motivo_demissao': proc.motivo_demissao,
        'salario': proc.salario,
        'cargo_funcao': proc.cargo_funcao,
        'cargo': proc.cargo,
        'empregador': proc.empregador,
        'local_trabalho': proc.local_trabalho,
        'pis': proc.pis,
        'ctps': proc.ctps,
        # Campos de audiência (RPA)
        'link_audiencia': proc.link_audiencia,
        'subtipo_audiencia': proc.subtipo_audiencia,
        'envolvido_audiencia': proc.envolvido_audiencia,
    }
    return render_template("processes/edit.html", data=data, process_id=id, batch_id=batch_id)



def _launch_rpa_thread(process_id: int) -> None:
    """Dispara RPA em thread background (mesma abordagem do batch)."""
    import threading
    import rpa
    from main import app  # Import DENTRO da função (evita circular)
    
    def run_rpa_in_background(proc_id: int):
        try:
            with app.app_context():
                logger.info(f"[SINGLE RPA] Iniciando RPA para processo {proc_id}")
                log_info(f"Iniciando RPA para processo {proc_id}", region="ROUTES")
                rpa.execute_rpa(proc_id)
                logger.info(f"[SINGLE RPA] ✅ Processo {proc_id} concluído")
                log_info(f"RPA concluído para processo {proc_id}", region="ROUTES")
        except Exception as e:
            logger.error(f"[SINGLE RPA] ❌ Erro: {e}", exc_info=True)
            log_error(f"Erro no RPA para processo {proc_id}: {e}", exc=e, region="ROUTES")
            try:
                from main import app as app_err
                with app_err.app_context():
                    proc = Process.query.get(proc_id)
                    if proc:
                        proc.elaw_status = 'error'
                        proc.elaw_error_message = str(e)
                        db.session.commit()
            except Exception as db_err:
                logger.error(f"[SINGLE RPA] Erro ao marcar erro: {db_err}")
                log_error(f"Erro ao marcar erro no banco: {db_err}", exc=db_err, region="ROUTES")
    
    thread = threading.Thread(
        target=run_rpa_in_background,
        args=(process_id,),
        daemon=True,
        name=f"RPA-Single-{process_id}"
    )
    thread.start()
    logger.info(f"[SINGLE RPA] Thread iniciada para processo {process_id}")
    log_info(f"Thread RPA iniciada para processo {process_id}", region="ROUTES")

@bp.route("/processos/<int:id>/preencher-elaw", methods=["POST"])
@login_required
def process_fill_elaw(id: int):
    import rpa
    from main import app as main_app
    
    proc = Process.query.get_or_404(id)
    batch_id = request.args.get('batch_id', type=int)
    
    # ✅ CRITICAL: Configurar flask_app globalmente ANTES de lançar thread
    rpa.flask_app = main_app._get_current_object() if hasattr(main_app, '_get_current_object') else main_app
    logger.info(f"[SINGLE RPA] Flask app configurado para processo {proc.id}")
    log_info(f"Flask app configurado para RPA processo {proc.id}", region="ROUTES")
    
    # Atualizar status para 'running'
    proc.elaw_status = 'running'
    proc.elaw_error_message = None
    db.session.commit()
    
    try:
        _launch_rpa_thread(process_id=proc.id)
        flash(f"Iniciando preenchimento automático no eLaw para o processo #{proc.id}...", "info")
        if batch_id:
            return redirect(url_for("core.rpa_progress", process_id=proc.id, batch_id=batch_id))
        else:
            return redirect(url_for("core.rpa_progress", process_id=proc.id))
    except Exception as e:
        proc.elaw_status = 'error'
        proc.elaw_error_message = str(e)
        db.session.commit()
        flash(f"Não foi possível iniciar o RPA: {e}", "danger")
        if batch_id:
            return redirect(url_for("core.process_view", id=proc.id, batch_id=batch_id))
        else:
            return redirect(url_for("core.process_view", id=proc.id))


@bp.route("/processos/<int:id>/deletar", methods=["POST"])
@login_required
def process_delete(id: int):
    proc = Process.query.get_or_404(id)
    
    # Verificação de autorização: apenas o dono ou admin pode deletar
    if proc.owner_id != current_user.id and not current_user.is_admin:
        flash("Você não tem permissão para deletar este processo.", "danger")
        return redirect(url_for("core.process_list"))
    
    try:
        cnj = proc.cnj or f"#{proc.id}"
        db.session.delete(proc)
        db.session.commit()
        
        # Resetar sequência se tabela ficou vazia
        _reset_process_sequence_if_empty()
        
        flash(f"Processo {cnj} deletado com sucesso!", "success")
    except SQLAlchemyError as e:
        db.session.rollback()
        flash(f"Erro ao deletar processo: {e}", "danger")
    
    return redirect(url_for("core.process_list"))


@bp.route("/processos/deletar-multiplos", methods=["POST"])
@login_required
def process_delete_multiple():
    """Deletar múltiplos processos de uma vez"""
    from flask import jsonify
    
    try:
        data = request.get_json()
        process_ids = data.get('process_ids', [])
        
        if not process_ids or not isinstance(process_ids, list):
            return jsonify({'success': False, 'error': 'Lista de IDs inválida'}), 400
        
        # Converter IDs para inteiros
        try:
            process_ids = [int(pid) for pid in process_ids]
        except (ValueError, TypeError):
            return jsonify({'success': False, 'error': 'IDs devem ser números'}), 400
        
        # Buscar processos e verificar permissões
        processes = Process.query.filter(Process.id.in_(process_ids)).all()
        
        # Verificar se todos os processos existem
        if len(processes) != len(process_ids):
            return jsonify({'success': False, 'error': 'Alguns processos não foram encontrados'}), 404
        
        # Verificar permissões - apenas dono ou admin pode deletar
        unauthorized = []
        for proc in processes:
            if proc.owner_id != current_user.id and not current_user.is_admin:
                unauthorized.append(proc.id)
        
        if unauthorized:
            return jsonify({
                'success': False, 
                'error': f'Você não tem permissão para deletar {len(unauthorized)} processo(s)'
            }), 403
        
        # Deletar todos os processos
        deleted_count = 0
        for proc in processes:
            try:
                db.session.delete(proc)
                deleted_count += 1
            except Exception as e:
                logger.error(f"Erro ao deletar processo {proc.id}: {e}")
                log_error(f"Erro ao deletar processo {proc.id}: {e}", exc=e, region="ROUTES")
        
        db.session.commit()
        
        # Resetar sequência se tabela ficou vazia
        _reset_process_sequence_if_empty()
        
        return jsonify({
            'success': True,
            'message': f'{deleted_count} processo(s) deletado(s) com sucesso!'
        })
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Erro ao deletar processos múltiplos: {e}", exc_info=True)
        log_error(f"Erro ao deletar processos múltiplos: {e}", exc=e, region="ROUTES")
        return jsonify({'success': False, 'error': f'Erro ao deletar processos: {str(e)}'}), 500


@bp.route("/processos/criar", methods=["GET", "POST"])
@login_required
def process_create():
    if request.method == "POST":
        form = request.form.to_dict(flat=True)

        # checkbox → bool
        form["cadastrar_primeira_audiencia"] = (
            form.get("cadastrar_primeira_audiencia") in ["on", "true", "True", "1", 1, True]
        )

        allowed = {
            "cnj_sim", "tipo_processo", "numero_processo", "numero_processo_antigo",
            "sistema_eletronico", "area_direito", "sub_area_direito", "estado",
            "comarca", "numero_orgao", "origem", "orgao", "vara", "celula", "foro",
            "instancia", "assunto", "npc", "objeto", "sub_objeto",
            "audiencia_inicial", "data_hora_cadastro_manual", "cliente",
            "parte", "advogado_autor", "advogado_reu",
            "posicao_parte_interessada", "parte_interessada",
            "parte_adversa_tipo", "parte_adversa_nome",
            "uf_oab_adv_adverso", "cpf_cnpj_parte_adversa", "telefone_parte_adversa",
            "email_parte_adversa", "endereco_parte_adversa",
            "data_distribuicao", "valor_causa", "rito",
            "tipo_notificacao", "resultado_decisao", "fundamentacao_resumida",
            "estrategia", "indice_atualizacao", "id_interno_hilo",
            "observacao", "cadastrar_primeira_audiencia"
        }
        payload = {k: v for k, v in form.items() if k in allowed}
        payload["owner_id"] = current_user.id

        proc = Process(**payload)
        db.session.add(proc)
        db.session.commit()

        session.pop("extracted_data", None)
        flash("Processo criado com sucesso.", "success")
        return redirect(url_for("core.process_view", id=proc.id))

    return render_template("processes/create.html")

# ============================================================
# Extração por PDF → confirmação
# ============================================================

def _extract_text_from_pdf(file_storage) -> str:
    """
    Extrai texto de um PDF de forma resiliente:
      1) pdfminer.six  2) PyPDF2  3) decode() como último recurso
    """
    file_storage.stream.seek(0)
    raw = file_storage.read()
    text = ""

    # 1) pdfminer.six
    try:
        from pdfminer.high_level import extract_text  # type: ignore
        text = extract_text(io.BytesIO(raw)) or ""
    except Exception:
        text = ""

    # 2) PyPDF2 (fallback)
    if not text.strip():
        try:
            import PyPDF2  # type: ignore
            reader = PyPDF2.PdfReader(io.BytesIO(raw))
            pages = []
            for p in reader.pages:
                try:
                    pages.append(p.extract_text() or "")
                except Exception:
                    pass
            text = "\n".join(pages)
        except Exception:
            text = ""

    # 3) último recurso: tentativa de decodificação
    if not text.strip():
        try:
            text = raw.decode("utf-8", errors="ignore")
        except Exception:
            text = ""

    return text or ""


@bp.route("/processos/extrair-pdf", methods=["GET", "POST"])
@login_required
def extract_from_pdf():
    if request.method == "POST":
        file = request.files.get("pdf_file")
        if not file or file.filename == "":
            flash("Selecione um PDF válido.", "danger")
            return redirect(url_for("core.extract_from_pdf"))

        try:
            # CRÍTICO: Salvar o PDF no diretório uploads/ para vinculação ao processo
            import uuid
            from werkzeug.utils import secure_filename
            
            upload_dir = os.path.join(current_app.root_path, "uploads")
            os.makedirs(upload_dir, exist_ok=True)
            
            # Nome único: timestamp_uuid_filename
            original_name = secure_filename(file.filename or "documento.pdf")
            unique_filename = f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{original_name}"
            pdf_path = os.path.join(upload_dir, unique_filename)
            
            # Salvar arquivo fisicamente
            file.save(pdf_path)
            logger.info(f"[UPLOAD_PDF] PDF salvo: {unique_filename}")
            log_info(f"PDF salvo: {unique_filename}", region="ROUTES")
            
            # ✅ MUDANÇA: Usar run_extraction_from_file que já tem OCR/LLM integrado
            # Passa o caminho do PDF salvo para habilitar OCR quando necessário
            extracted = run_extraction_from_file(
                path=pdf_path,
                brand_map_path=None,  # Usa database JSON padrão
                filename=file.filename
            )
            # Blindagem caso o extrator não tenha setado
            extracted.setdefault("cnj", "Sim" if extracted.get("cnj_sim") else "Não")
            extracted.setdefault("tipo_processo", "Eletrônico")
            extracted.setdefault("sistema_eletronico", "PJE")
            extracted.setdefault("area_direito", "Trabalhista")

            # CRÍTICO: Guardar filename do PDF na sessão para vincular ao processo
            session["uploaded_pdf_filename"] = unique_filename
            session["extracted_data"] = extracted or {}
            session.modified = True
            current_app.logger.debug("Dados extraídos (pipeline): %s", extracted)
            logger.info(f"[UPLOAD_PDF] PDF '{unique_filename}' vinculado à sessão")
            log_info(f"PDF '{unique_filename}' vinculado à sessão", region="ROUTES")

            flash("Extração concluída! Revise os dados abaixo antes de salvar.", "success")
            return redirect(url_for("core.confirm_extracted"))

        except Exception as e:
            current_app.logger.exception("Erro ao processar PDF: %s", e)
            flash(f"Erro ao processar PDF: {e}", "danger")
            return redirect(url_for("core.extract_from_pdf"))

    # GET -> renderiza a tela de upload
    return render_template("processes/extract_from_pdf.html")


def _launch_rpa(process_id: int | None = None) -> int:
    """
    Dispara o RPA em background passando RPA_PROCESS_ID no ambiente.
    Retorna o PID do processo filho (útil para log/diagnóstico).
    """
    project_root = Path(current_app.root_path)
    rpa_script = project_root / "rpa.py"  # ajuste se estiver em outra pasta
    if not rpa_script.exists():
        raise RuntimeError(f"Arquivo RPA não encontrado: {rpa_script}")

    env = os.environ.copy()
    if process_id is not None:
        env["RPA_PROCESS_ID"] = str(process_id)

    # Pode deixar o .env decidir; forcei defaults “visuais”:
    env["RPA_HEADLESS"] = "true"  # Força headless para rodar em backend (Linux)
    env.setdefault("RPA_KEEP_OPEN_AFTER_LOGIN_SECONDS", "10")

    logs_dir = project_root / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_path = logs_dir / f"rpa_{process_id or 'latest'}.log"

    cmd = [sys.executable, "-u", str(rpa_script)]

    creationflags = 0
    if os.name == "nt":
        # use CREATE_NEW_CONSOLE se quiser ver o console do RPA
        creationflags = subprocess.CREATE_NO_WINDOW

    log_file = open(log_path, "a", encoding="utf-8")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(project_root),
            env=env,
            stdout=log_file,
            stderr=log_file,
            creationflags=creationflags,
            close_fds=(os.name != "nt"),
            shell=False,
        )
        return proc.pid
    except Exception:
        try:
            log_file.close()
        except Exception:
            pass
        raise


def _truthy(v):
    s = str(v).strip().lower()
    if s in {"1", "true", "t", "sim", "s", "yes", "on"}:
        return True
    if s in {"0", "false", "f", "nao", "não", "n", "no", "off"}:
        return False
    return None


def _pick_uf(data: dict) -> str:
    """
    Pega UF de várias fontes: 'estado', 'uf', dentro de 'comarca' (ex: 'Macaé - RJ'),
    ou de qualquer string do payload (último recurso).
    """
    cand = (data.get("estado") or data.get("uf") or "").strip()
    m = re.fullmatch(r"[A-Za-z]{2}", cand)
    if m:
        return cand.upper()

    # tenta na comarca (ex.: 'Macaé - RJ' ou 'São Paulo/SP')
    for k in ("comarca", "foro", "orgao", "vara"):
        txt = (data.get(k) or "").upper()
        m = re.search(r"[-/\s]([A-Z]{2})(?:\b|$)", txt)
        if m:
            return m.group(1)

    # varre todo payload por um padrão de UF
    blob = " ".join(str(v) for v in data.values()).upper()
    m = re.search(r"\b([A-Z]{2})\b", blob)
    if m:
        return m.group(1)

    # último recurso – escolha algo válido pro seu caso
    return "RJ"


@bp.route("/processos/confirmar-extracao", methods=["GET", "POST"])
@login_required
def confirm_extracted():
    if request.method == "POST":
        # 1) mescla os dados extraídos (sessão) com o que veio do form
        base = session.get("extracted_data", {}) or {}
        form = request.form.to_dict(flat=True)
        data = {**base, **form}

        # 2) CNJ (booleano/Sim|Não, conforme tipo da coluna)
        cnj_src = data.get("cnj") or data.get("cnj_sim") or data.get("is_judicial")
        cnj_bool = _truthy(cnj_src)
        if cnj_bool is None:
            cnj_bool = True if data.get("numero_processo") else False

        is_bool_cnj = False
        try:
            is_bool_cnj = (Process.__table__.c.cnj.type.python_type is bool)
        except Exception:
            pass
        cnj_db_value = cnj_bool if is_bool_cnj else ("Sim" if cnj_bool else "Não")

        # 3) Campos NOT NULL com fallback
        estado = _pick_uf(data)
        celula = (data.get("celula") or data.get("escritorio") or
                  data.get("cliente") or "Em Segredo").strip()

        # 4) Monte SOMENTE colunas que existem no modelo
        kwargs = {
            "owner_id": current_user.id,        # ✅ sempre preencha
            "created_by": current_user.id,      # ✅ exigido pelo seu schema
            "updated_by": current_user.id,      # ✅ se a coluna existir

            "cnj": cnj_db_value,
            "tipo_processo": (data.get("tipo_processo") or "Eletrônico").strip(),
            "numero_processo": (data.get("numero_processo") or "").strip(),
            "numero_processo_antigo": (data.get("numero_processo_antigo") or None),
            "sistema_eletronico": (data.get("sistema_eletronico") or "PJE").strip(),
            "area_direito": (data.get("area_direito") or "Cível").strip(),
            "sub_area_direito": (data.get("sub_area_direito") or None),
            "estado": estado,
            "comarca": (data.get("comarca") or None),
            "numero_orgao": (data.get("numero_orgao") or "01"),
            "origem": (data.get("origem") or None),
            "orgao": (data.get("orgao") or None),
            "vara": (data.get("vara") or None),
            "celula": celula,
            "foro": (data.get("foro") or None),
            "instancia": (data.get("instancia") or "Primeira Instância"),
            "assunto": (data.get("assunto") or None),
            "npc": (data.get("npc") or None),
            "objeto": (data.get("objeto") or None),
            "sub_objeto": (data.get("sub_objeto") or None),
            "audiencia_inicial": (data.get("audiencia_inicial") or None),
            "link_audiencia": (data.get("link_audiencia") or None),
            "subtipo_audiencia": (data.get("subtipo_audiencia") or None),
            "envolvido_audiencia": (data.get("envolvido_audiencia") or None),
            "data_hora_cadastro_manual": (data.get("data_hora_cadastro_manual") or None),
            "cliente_parte": (data.get("cliente_parte") or None),
            "advogado_autor": (data.get("advogado_autor") or None),
            "advogado_reu": (data.get("advogado_reu") or None),
            "prazo": (data.get("prazo") or None),
            "tipo_notificacao": (data.get("tipo_notificacao") or None),
            "resultado_audiencia": (data.get("resultado_audiencia") or None),
            "prazos_derivados_audiencia": (data.get("prazos_derivados_audiencia") or None),
            "decisao_tipo": (data.get("decisao_tipo") or None),
            "decisao_resultado": (data.get("decisao_resultado") or None),
            "decisao_fundamentacao_resumida": (data.get("decisao_fundamentacao_resumida") or None),
            "id_interno_hilo": (data.get("id_interno_hilo") or None),
            "estrategia": (data.get("estrategia") or None),
            "indice_atualizacao": (data.get("indice_atualizacao") or None),
            "posicao_parte_interessada": (data.get("posicao_parte_interessada") or None),
            "parte_interessada": (data.get("parte_interessada") or None),
            "parte_adversa_tipo": (data.get("parte_adversa_tipo") or None),
            "parte_adversa_nome": (data.get("parte_adversa_nome") or None),
            "escritorio_parte_adversa": (data.get("escritorio_parte_adversa") or None),
            "uf_oab_advogado_adverso": (data.get("uf_oab_advogado_adverso") or None),
            "cpf_cnpj_parte_adversa": (data.get("cpf_cnpj_parte_adversa") or None),
            "telefone_parte_adversa": (data.get("telefone_parte_adversa") or None),
            "email_parte_adversa": (data.get("email_parte_adversa") or None),
            "endereco_parte_adversa": (data.get("endereco_parte_adversa") or None),
            "data_distribuicao": (data.get("data_distribuicao") or None),
            "data_citacao": (data.get("data_citacao") or None),
            "risco": (data.get("risco") or None),
            "valor_causa": (data.get("valor_causa") or None),
            "rito": (data.get("rito") or None),
            "observacao": (data.get("observacao") or None),
            "cadastrar_primeira_audiencia": bool(_truthy(data.get("cadastrar_primeira_audiencia") or False))
                if hasattr(Process, "cadastrar_primeira_audiencia") else None,
            
            # 🔧 Campos Trabalhistas
            "data_admissao": (data.get("data_admissao") or None),
            "data_demissao": (data.get("data_demissao") or None),
            "motivo_demissao": (data.get("motivo_demissao") or None),
            "salario": (data.get("salario") or None),
            "cargo_funcao": (data.get("cargo_funcao") or None),
            "cargo": (data.get("cargo") or None),
            "empregador": (data.get("empregador") or None),
            "local_trabalho": (data.get("local_trabalho") or None),
            "pis": (data.get("pis") or None),
            "ctps": (data.get("ctps") or None),
            
            # 🔧 Pedidos extraídos do PDF (JSON)
            "pedidos_json": json.dumps(data.get("pedidos", [])) if data.get("pedidos") else None,
        }

        # Remove chaves que não existem no modelo (evita TypeError)
        kwargs = {k: v for k, v in kwargs.items() if hasattr(Process, k)}

        # Adicionar campos adicionais
        if hasattr(Process, 'cliente'):
            kwargs['cliente'] = data.get('cliente') or None
        if hasattr(Process, 'parte'):
            kwargs['parte'] = data.get('parte') or None
        
        # CRÍTICO: Vincular PDF ao processo para evitar confusão de dados no RPA
        uploaded_pdf = session.get('uploaded_pdf_filename')
        if hasattr(Process, 'pdf_filename'):
            if uploaded_pdf:
                kwargs['pdf_filename'] = uploaded_pdf
                logger.info(f"[CREATE_PROCESS] PDF vinculado ao processo: {uploaded_pdf}")
                log_info(f"PDF vinculado ao processo: {uploaded_pdf}", region="ROUTES")
            else:
                kwargs['pdf_filename'] = None
                logger.warning("[CREATE_PROCESS] Processo criado sem PDF vinculado (entrada manual)")
                monitor_warn("Processo criado sem PDF vinculado (entrada manual)", region="ROUTES")
        
        if hasattr(Process, 'elaw_status'):
            kwargs['elaw_status'] = 'pending'

        try:
            proc = Process(**kwargs)
            db.session.add(proc)
            db.session.commit()
            
            # Limpar sessão após salvar com sucesso
            if uploaded_pdf:
                session.pop('uploaded_pdf_filename', None)
                logger.info(f"[CREATE_PROCESS] Sessão limpa após vincular PDF ao processo #{proc.id}")
                log_info(f"Processo #{proc.id} criado com sucesso", region="ROUTES")
            
            flash(f"Processo #{proc.id} salvo com sucesso!", "success")
            return redirect(url_for("core.process_view", id=proc.id))

        except SQLAlchemyError as e:
            db.session.rollback()
            flash(f"Falha ao salvar processo: {e.__class__.__name__}: {e}", "danger")

        return redirect(url_for("core.process_list"))

    # GET: exibe tela com os dados extraídos
    data = session.get("extracted_data", {}) or {}
    return render_template("processes/confirm_extracted.html", data=data)

# ============================================================
# Admin
# ============================================================

def _admin_required():
    if not current_user.is_authenticated or not getattr(current_user, "is_admin", False):
        flash("Acesso permitido apenas para administradores.", "danger")
        return False
    return True


@bp.route("/admin")
@login_required
def admin_index():
    if not _admin_required():
        return redirect(url_for("core.dashboard"))
    return redirect(url_for("core.admin_users"))


@bp.route("/admin/usuarios")
@login_required
def admin_users():
    if not _admin_required():
        return redirect(url_for("core.dashboard"))
    users = User.query.order_by(User.created_at.desc()).all()
    try:
        return render_template("admin/users.html", users=users)
    except TemplateNotFound:
        rows = "".join(
            f"<tr><td>{u.id}</td><td>{u.username}</td><td>{u.email}</td><td>{'Sim' if u.is_admin else 'Não'}</td></tr>"
            for u in users
        )
        html = f"""
        <div class="container py-4">
          <h1>Usuários</h1>
          <a class="btn btn-primary mb-3" href="{url_for('core.admin_create_user')}">Novo usuário</a>
          <table class="table table-striped">
            <thead><tr><th>ID</th><th>Usuário</th><th>E-mail</th><th>Admin</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
          <a class="btn btn-secondary" href="{url_for('core.dashboard')}">Voltar</a>
        </div>
        """
        return render_template_string(html)


@bp.route("/admin/usuarios/novo", methods=["GET", "POST"])
@login_required
def admin_create_user():
    if not _admin_required():
        return redirect(url_for("core.dashboard"))

    form_obj = None
    try:
        from forms import CreateUserForm
        form_obj = CreateUserForm()
    except Exception:
        form_obj = None

    if request.method == "POST":
        if form_obj and hasattr(form_obj, "validate_on_submit") and form_obj.validate_on_submit():
            username = form_obj.name.data.strip()      # seu CreateUserForm tem 'name' e 'email'
            email = form_obj.email.data.strip()
            password = form_obj.password.data
            is_admin_flag = (form_obj.role.data == "admin")
        else:
            username = (request.form.get("username") or "").strip()
            email = (request.form.get("email") or "").strip()
            password = request.form.get("password") or ""
            is_admin_flag = request.form.get("is_admin") in ["on", "true", "True", "1", 1, True]

        if not username or not email or not password:
            flash("Preencha usuário, e-mail e senha.", "danger")
            try:
                return render_template("admin/create_user.html", form=form_obj)
            except TemplateNotFound:
                pass

        user = User(username=username, email=email, is_admin=is_admin_flag)
        if hasattr(user, "set_password"):
            user.set_password(password)
        else:
            from werkzeug.security import generate_password_hash
            user.password_hash = generate_password_hash(password)

        db.session.add(user)
        db.session.commit()
        flash("Usuário criado com sucesso.", "success")
        return redirect(url_for("core.admin_users"))

    try:
        return render_template("admin/create_user.html", form=form_obj)
    except TemplateNotFound:
        html = f"""
        <div class="container py-4">
          <h1>Novo Usuário</h1>
          <form method="POST">
            <div class="mb-3">
              <label class="form-label">Usuário</label>
              <input class="form-control" name="username" required>
            </div>
            <div class="mb-3">
              <label class="form-label">E-mail</label>
              <input type="email" class="form-control" name="email" required>
            </div>
            <div class="mb-3">
              <label class="form-label">Senha</label>
              <input type="password" class="form-control" name="password" required>
            </div>
            <div class="form-check mb-3">
              <input class="form-check-input" type="checkbox" id="is_admin" name="is_admin">
              <label class="form-check-label" for="is_admin">Administrador</label>
            </div>
            <button class="btn btn-primary" type="submit">Criar</button>
            <a class="btn btn-secondary ms-2" href="{url_for('core.admin_users')}">Cancelar</a>
          </form>
        </div>
        """
        return render_template_string(html)


@bp.route("/admin/configuracoes")
@login_required
def admin_settings():
    if not _admin_required():
        return redirect(url_for("core.dashboard"))
    try:
        return render_template("admin/settings.html")
    except TemplateNotFound:
        return render_template_string(
            f'<div class="container py-4"><h1>Configurações</h1><p>Template admin/settings.html não encontrado.</p>'
            f'<a class="btn btn-secondary" href="{url_for("core.dashboard")}">Voltar</a></div>'
        )


@bp.route("/admin/auditoria")
@login_required
def admin_audit():
    if not _admin_required():
        return redirect(url_for("core.dashboard"))
    try:
        return render_template("admin/audit.html")
    except TemplateNotFound:
        return render_template_string(
            f'<div class="container py-4"><h1>Auditoria</h1><p>Template admin/audit.html não encontrado.</p>'
            f'<a class="btn btn-secondary" href="{url_for("core.dashboard")}">Voltar</a></div>'
        )


# ============================================================
# Endpoints RPA - Status em Tempo Real
# ============================================================

@bp.route("/api/rpa-status/<int:process_id>")
@login_required
def api_rpa_status(process_id):
    """Endpoint REST que retorna status do RPA em JSON para polling"""
    from rpa_status import get_rpa_status
    
    # 🔧 2025-11-27: FORÇAR dados frescos do banco (evitar cache de sessão SQLAlchemy)
    db.session.expire_all()
    
    # Verifica se o usuário tem permissão para ver este processo
    process = Process.query.get_or_404(process_id)
    if process.owner_id != current_user.id and not current_user.is_admin:
        return jsonify({"error": "Acesso negado"}), 403
    
    status = get_rpa_status(process_id)
    
    if not status:
        return jsonify({
            "process_id": process_id,
            "status": "not_started",
            "message": "RPA ainda não iniciado"
        })
    
    return jsonify(status)


@bp.route("/api/process/<int:process_id>/details")
@login_required
def api_process_details(process_id):
    """Endpoint REST que retorna dados completos do processo incluindo screenshots"""
    # Buscar processo
    process = Process.query.get_or_404(process_id)
    
    # Verifica permissão
    if process.owner_id != current_user.id and not current_user.is_admin:
        return jsonify({"error": "Acesso negado"}), 403
    
    # Retornar dados incluindo screenshots
    return jsonify({
        "id": process.id,
        "numero_processo": process.numero_processo,
        "elaw_status": process.elaw_status,
        "elaw_error_message": process.elaw_error_message,
        "elaw_screenshot_before_path": process.elaw_screenshot_before_path,
        "elaw_screenshot_after_path": process.elaw_screenshot_after_path,
        "elaw_filled_at": process.elaw_filled_at.isoformat() if process.elaw_filled_at else None
    })


@bp.route("/processos/<int:process_id>/rpa-progress")
@login_required
def rpa_progress(process_id):
    """Tela de loading dinâmica mostrando progresso do RPA em tempo real"""
    process = Process.query.get_or_404(process_id)
    batch_id = request.args.get('batch_id', type=int)
    
    # Verifica permissão
    if process.owner_id != current_user.id and not current_user.is_admin:
        flash("Acesso negado.", "danger")
        return redirect(url_for("core.process_list"))
    
    try:
        return render_template(
            "processes/rpa_progress.html",
            process=process,
            process_id=process_id,
            batch_id=batch_id
        )
    except TemplateNotFound:
        # Fallback inline se template não existir ainda
        return render_template_string("""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Processando RPA...</title>
                <meta charset="utf-8">
                <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
                <style>
                    .spinner { animation: spin 1s linear infinite; }
                    @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
                    .progress-item { padding: 0.5rem; border-left: 3px solid #dee2e6; margin-bottom: 0.5rem; }
                    .progress-item.active { border-left-color: #0d6efd; background-color: #e7f1ff; }
                    .progress-item.completed { border-left-color: #198754; }
                    .progress-item.error { border-left-color: #dc3545; }
                </style>
            </head>
            <body class="bg-light">
                <div class="container py-5">
                    <div class="card shadow">
                        <div class="card-header bg-primary text-white">
                            <h4 class="mb-0">
                                <span class="spinner me-2">⏳</span> Processo {{ process_id }}
                            </h4>
                        </div>
                        <div class="card-body">
                            <div id="status-message" class="alert alert-info">
                                <strong>Iniciando RPA...</strong>
                            </div>
                            <div id="progress-history" class="mt-3"></div>
                        </div>
                    </div>
                </div>
                
                <script>
                    const processId = {{ process_id }};
                    const pollInterval = 1500; // 1.5 segundos
                    const maxDuration = 300000; // 5 minutos timeout
                    let startTime = Date.now();
                    
                    async function checkStatus() {
                        try {
                            const response = await fetch(`/api/rpa-status/${processId}`);
                            const data = await response.json();
                            
                            // Atualiza mensagem principal
                            const statusMsg = document.getElementById('status-message');
                            statusMsg.className = `alert alert-${getAlertClass(data.status)}`;
                            statusMsg.innerHTML = `<strong>${data.current_step || 'Processando'}</strong><br>${data.message || ''}`;
                            
                            // Atualiza histórico
                            if (data.history && data.history.length > 0) {
                                updateHistory(data.history);
                            }
                            
                            // Verifica se terminou
                            if (data.status === 'completed') {
                                statusMsg.innerHTML += '<br><br><div class="spinner-border spinner-border-sm me-2"></div>Redirecionando...';
                                setTimeout(() => {
                                    window.location.href = '/processos';
                                }, 2000);
                                return;
                            } else if (data.status === 'error') {
                                statusMsg.innerHTML += '<br><a href="/processos" class="btn btn-sm btn-secondary mt-2">Voltar para lista</a>';
                                return;
                            }
                            
                            // Continua polling
                            if (Date.now() - startTime < maxDuration) {
                                setTimeout(checkStatus, pollInterval);
                            } else {
                                statusMsg.className = 'alert alert-warning';
                                statusMsg.innerHTML = '<strong>Timeout</strong><br>O RPA está demorando mais do que o esperado. <a href="/processos">Voltar</a>';
                            }
                        } catch (error) {
                            console.error('Erro ao verificar status:', error);
                            setTimeout(checkStatus, pollInterval * 2);
                        }
                    }
                    
                    function getAlertClass(status) {
                        switch(status) {
                            case 'completed': return 'success';
                            case 'error': return 'danger';
                            case 'running': return 'info';
                            default: return 'secondary';
                        }
                    }
                    
                    function updateHistory(history) {
                        const container = document.getElementById('progress-history');
                        container.innerHTML = '<h6 class="text-muted mb-3">Histórico:</h6>';
                        history.forEach((item, index) => {
                            const div = document.createElement('div');
                            div.className = 'progress-item' + (index === history.length - 1 ? ' active' : ' completed');
                            const time = new Date(item.timestamp).toLocaleTimeString('pt-BR');
                            const dataStr = item.data && Object.keys(item.data).length > 0 ? 
                                ` - ${JSON.stringify(item.data)}` : '';
                            div.innerHTML = `<small class="text-muted">${time}</small><br><strong>${item.step}</strong>: ${item.message}${dataStr}`;
                            container.appendChild(div);
                        });
                    }
                    
                    // Inicia polling
                    checkStatus();
                </script>
            </body>
            </html>
        """, process=process, process_id=process_id)
