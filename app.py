# app.py
import os
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from flask import (
    Flask, flash, redirect, url_for, request, send_from_directory, make_response, current_app
)
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_migrate import Migrate
from sqlalchemy.exc import OperationalError, IntegrityError, SQLAlchemyError

# ÚNICA instância de db e login_manager do projeto
from models import db, User, ensure_admin_user
from extensions import login_manager

load_dotenv(override=True)
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

logger.info("="*80)
logger.info("SISTEMA JURÍDICO - Gerenciamento de Processos Trabalhistas - Iniciando")
logger.info("="*80)

# Inicializar monitor remoto (se habilitado)
try:
    from monitor_integration import init_monitor
    monitor_conectado = init_monitor(rpa_id="RPA-FGbularmaci-5")
    if monitor_conectado:
        logger.info("✅ Monitor remoto ATIVO")
    else:
        logger.warning("⚠️ Monitor remoto DESABILITADO ou não configurado")
except Exception as e:
    logger.warning(f"Erro ao inicializar monitor: {e}")

# Timezone brasileiro (UTC-3)
BRAZIL_TZ = ZoneInfo("America/Sao_Paulo")


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("SESSION_SECRET", "dev-secret-key-change-in-production")
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    # ==============================
    # Config Banco
    # ==============================
    BASEDIR = os.path.abspath(os.path.dirname(__file__))
    
    # Usar PostgreSQL se disponível, senão SQLite com caminho absoluto
    default_db = f"sqlite:///{os.path.join(BASEDIR, 'instance', 'processos.db')}"
    database_uri = os.environ.get("DATABASE_URL", default_db)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_uri
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    
    # Log para debug
    print(f"[CONFIG] Usando banco de dados: {database_uri[:50]}...")
    logging.info(f"Database URI configurado: {database_uri[:50]}...")
    app.config["CLIENTE_CELULA_DOCX"] = os.path.join(BASEDIR, "data", "CLIENTE_X_CELULA.docx")

    if os.environ.get("DATABASE_URL", "").startswith("postgresql"):
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "pool_recycle": 270,
            "pool_pre_ping": True,
            "pool_size": 2,
            "max_overflow": 1,
            "pool_timeout": 30,
            "connect_args": {
                "connect_timeout": 20,
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 3,
                "options": "-c statement_timeout=120000",
                "application_name": "flask_legal_app",
            },
        }
    else:
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "pool_recycle": 300,
            "pool_pre_ping": True,
        }

    # Uploads
    app.config["UPLOAD_FOLDER"] = os.path.join(os.getcwd(), "uploads")
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2GB (20 arquivos x 100MB cada)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    # Recarregar templates automaticamente em dev
    app.config.setdefault("TEMPLATES_AUTO_RELOAD", True)

    # ==============================
    # Inicializa extensões
    # ==============================
    os.makedirs(app.instance_path, exist_ok=True)

    db.init_app(app)
    Migrate(app, db, render_as_batch=True, compare_type=True, compare_server_default=True)

    login_manager.init_app(app)
    login_manager.login_view = "core.login"
    login_manager.login_message = "Por favor, faça login para acessar esta página."

    @login_manager.user_loader
    def load_user(user_id):
        try:
            return User.query.get(int(user_id))
        except Exception:
            # Em caso de sessão quebrada, limpe a sessão do SQLAlchemy
            try:
                db.session.remove()
            except Exception:
                pass
            return None

    # ==============================
    # Filtros Jinja
    # ==============================
    @app.template_filter("from_json")
    def from_json_filter(value):
        if not value:
            return []
        try:
            return json.loads(value) if isinstance(value, str) else value
        except (json.JSONDecodeError, TypeError):
            return []
    
    @app.template_filter("brazil_datetime")
    def brazil_datetime_filter(dt, format_str="%d/%m/%Y %H:%M:%S"):
        """Converte datetime UTC para horário de Brasília"""
        if dt is None:
            return ""
        try:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            dt_brazil = dt.astimezone(BRAZIL_TZ)
            return dt_brazil.strftime(format_str)
        except Exception:
            return str(dt)

    # ==============================
    # Sistema de Logging Centralizado
    # ==============================
    try:
        from logging_config import init_flask_logging
        init_flask_logging(app)
        logger.info("✅ Sistema de logging centralizado inicializado")
    except Exception as e:
        logger.warning(f"⚠️ Erro ao inicializar logging centralizado: {e}")

    # ==============================
    # Blueprints / Rotas
    # ==============================
    # Importa DEPOIS de init_app para evitar import cíclico
    from routes import bp as core_bp
    from routes_batch import batch_bp
    
    app.register_blueprint(core_bp)
    app.register_blueprint(batch_bp)

    # favicon (evita 404 quando não há arquivo)
    @app.route("/favicon.ico")
    def favicon():
        static_favicon = os.path.join(app.static_folder or "static", "favicon.ico")
        if os.path.exists(static_favicon):
            return send_from_directory(app.static_folder, "favicon.ico")
        # Sem favicon: responde 204 para não poluir logs
        return make_response(("", 204))

    # Rota raiz “fallback” (se a sua index real estiver no blueprint, ela será usada)
    @app.route("/")
    def root_index():
        if "core.index" in app.view_functions:
            return redirect(url_for("core.index"))
        if "index" in app.view_functions:
            return redirect(url_for("index"))
        if "dashboard" in app.view_functions:
            return redirect(url_for("dashboard"))
        if "process_create" in app.view_functions:
            return redirect(url_for("process_create"))
        if "core.login" in app.view_functions:
            return redirect(url_for("core.login"))
        return "Rota raiz não configurada."

    # ==============================
    # Bootstrap de DB (dev-friendly)
    # ==============================
    with app.app_context():
        try:
            db.create_all()  # útil em SQLite/dev; em produção prefira Alembic
        except Exception:
            pass

        try:
            ensure_admin_user()
        except Exception:
            pass
        
        # ==============================
        # Limpeza automática de screenshots antigos
        # Executa ao iniciar o servidor para liberar espaço
        # ==============================
        try:
            from rpa_status import run_all_cleanup
            import threading
            
            def delayed_cleanup():
                """Executa limpeza após 30 segundos para não atrasar startup"""
                import time
                time.sleep(30)
                try:
                    from main import app as main_app
                    with main_app.app_context():
                        run_all_cleanup(screenshot_days=2, status_days=7)
                except Exception as cleanup_err:
                    logging.warning(f"Limpeza automática falhou: {cleanup_err}")
            
            cleanup_thread = threading.Thread(target=delayed_cleanup, daemon=True)
            cleanup_thread.start()
            logging.info("🧹 Limpeza automática agendada (executa em 30s)")
        except Exception as e:
            logging.warning(f"Não foi possível agendar limpeza automática: {e}")

    # ==============================
    # Handlers de erro (DB)
    # ==============================
    # Capture qualquer erro de SQLAlchemy (inclui OperationalError/IntegrityError)
    @app.errorhandler(SQLAlchemyError)
    def handle_sqlalchemy_error(error):
        db.session.rollback()
        current_app.logger.exception("DB error: %s", error)
        flash("Houve um problema no banco de dados. Tente novamente.", "danger")
        return redirect(request.referrer or url_for("core.dashboard")), 302

    # (Opcional) se quiser tratar especificamente OperationalError também:
    @app.errorhandler(OperationalError)
    def handle_operational_error(error):
        db.session.rollback()
        current_app.logger.exception("Operational DB error: %s", error)
        flash("Falha de conexão/operacional com o banco. Tente novamente.", "danger")
        return redirect(request.referrer or url_for("core.dashboard")), 302

    # Handler para erro 413 - Request Entity Too Large
    @app.errorhandler(413)
    def handle_request_too_large(error):
        current_app.logger.error(f"[UPLOAD][ERROR] 413 - Request Too Large: {error}")
        current_app.logger.error(f"[UPLOAD][ERROR] Content-Length: {request.content_length}")
        current_app.logger.error(f"[UPLOAD][ERROR] MAX_CONTENT_LENGTH: {app.config.get('MAX_CONTENT_LENGTH')}")
        flash("O arquivo é muito grande. O limite máximo é 350MB por upload.", "danger")
        return redirect(url_for("batch.batch_new")), 302

    # Handler para erro 400 - Bad Request (pode acontecer com uploads malformados)
    @app.errorhandler(400)
    def handle_bad_request(error):
        current_app.logger.error(f"[UPLOAD][ERROR] 400 - Bad Request: {error}")
        flash("Requisição inválida. Verifique os arquivos e tente novamente.", "danger")
        return redirect(request.referrer or url_for("core.dashboard")), 302

    # Handler para erro 500 - Internal Server Error
    @app.errorhandler(500)
    def handle_internal_error(error):
        current_app.logger.exception(f"[UPLOAD][ERROR] 500 - Internal Server Error: {error}")
        flash("Erro interno do servidor. Por favor, tente novamente.", "danger")
        return redirect(request.referrer or url_for("core.dashboard")), 302

    return app


if __name__ == "__main__":
    # Em dev você pode rodar: python app.py
    app = create_app()
    app.run(debug=True)
