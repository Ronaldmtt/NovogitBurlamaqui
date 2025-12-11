"""
Sistema de Logging Centralizado para o Sistema Jurídico Inteligente.

Este módulo fornece:
- Configuração centralizada de logging
- Decorators para instrumentação automática
- Helpers para logging estruturado de eventos
- Context managers para tracking de operações longas
- Preparação para integração com RPA Monitor externo

Formato de log:
[TIMESTAMP] [LEVEL] [MODULE] [ACTION] [USER] [DETAILS]

Uso:
    from logging_config import log_event, log_start, log_end, log_success, log_error, timed_operation

    # Logging de eventos
    log_event("LOGIN", "Usuário tentando login", user="admin")
    log_start("RPA_BATCH", "Iniciando batch com 19 processos", batch_id=5)
    log_end("RPA_BATCH", "Batch finalizado", batch_id=5, total=19, success=18, errors=1)
    log_success("PDF_EXTRACTION", "PDF extraído com sucesso", process_id=123)
    log_error("RPA_STEP", "Falha ao preencher campo", error=str(e), process_id=123)

    # Context manager para operações
    with timed_operation("THREAD_BATCH", batch_id=5, total_processes=19):
        # ... código do batch ...

    # Decorator para funções
    @log_function
    def minha_funcao():
        pass
"""

import logging
import time
import functools
import traceback
from datetime import datetime
from typing import Optional, Any, Dict
from contextlib import contextmanager
from flask import request, g, has_request_context
from flask_login import current_user

# Configuração do logger principal
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(name)-20s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Logger principal do sistema
logger = logging.getLogger("SISTEMA_JURIDICO")
logger.setLevel(logging.DEBUG)

# Loggers específicos por módulo
rpa_logger = logging.getLogger("RPA")
batch_logger = logging.getLogger("BATCH")
extraction_logger = logging.getLogger("EXTRACTION")
auth_logger = logging.getLogger("AUTH")
routes_logger = logging.getLogger("ROUTES")
db_logger = logging.getLogger("DATABASE")


def _get_user_info() -> str:
    """Retorna informação do usuário atual (se disponível)."""
    try:
        if has_request_context() and current_user and not current_user.is_anonymous:
            return f"user={current_user.username}(id={current_user.id})"
        return "user=anonymous"
    except Exception:
        return "user=system"


def _get_request_info() -> str:
    """Retorna informação do request atual (se disponível)."""
    try:
        if has_request_context():
            return f"endpoint={request.endpoint} method={request.method}"
        return ""
    except Exception:
        return ""


def _format_extras(**kwargs) -> str:
    """Formata parâmetros extras para o log."""
    if not kwargs:
        return ""
    parts = [f"{k}={v}" for k, v in kwargs.items() if v is not None]
    return " | " + " ".join(parts) if parts else ""


# ============================================================
# FUNÇÕES PRINCIPAIS DE LOGGING
# ============================================================

def log_event(action: str, message: str, level: str = "INFO", module: str = "SYSTEM", **kwargs):
    """
    Log de evento genérico.
    
    Args:
        action: Nome da ação (ex: "LOGIN", "CLICK_BUTTON", "NAVIGATE")
        message: Mensagem descritiva
        level: Nível do log (DEBUG, INFO, WARNING, ERROR)
        module: Módulo que está logando
        **kwargs: Parâmetros extras (process_id, batch_id, etc)
    """
    user_info = _get_user_info()
    extras = _format_extras(**kwargs)
    log_msg = f"[{action}] {message} | {user_info}{extras}"
    
    log_level = getattr(logging, level.upper(), logging.INFO)
    logger.log(log_level, log_msg)


def log_start(action: str, message: str, module: str = "SYSTEM", **kwargs):
    """Log de INÍCIO de uma operação."""
    user_info = _get_user_info()
    extras = _format_extras(**kwargs)
    log_msg = f"[{action}][START] ▶️ {message} | {user_info}{extras}"
    logger.info(log_msg)


def log_end(action: str, message: str, module: str = "SYSTEM", duration_ms: Optional[float] = None, **kwargs):
    """Log de FIM de uma operação."""
    user_info = _get_user_info()
    duration_str = f"duration={duration_ms:.0f}ms" if duration_ms else ""
    extras = _format_extras(**kwargs)
    log_msg = f"[{action}][END] ⏹️ {message} | {user_info} | {duration_str}{extras}"
    logger.info(log_msg)


def log_success(action: str, message: str, module: str = "SYSTEM", **kwargs):
    """Log de SUCESSO de uma operação."""
    user_info = _get_user_info()
    extras = _format_extras(**kwargs)
    log_msg = f"[{action}][SUCCESS] ✅ {message} | {user_info}{extras}"
    logger.info(log_msg)


def log_error(action: str, message: str, error: Optional[str] = None, module: str = "SYSTEM", include_traceback: bool = False, **kwargs):
    """Log de ERRO de uma operação."""
    user_info = _get_user_info()
    error_str = f"error={error}" if error else ""
    extras = _format_extras(**kwargs)
    log_msg = f"[{action}][ERROR] ❌ {message} | {user_info} | {error_str}{extras}"
    
    if include_traceback:
        log_msg += f"\n{traceback.format_exc()}"
    
    logger.error(log_msg)


def log_warning(action: str, message: str, module: str = "SYSTEM", **kwargs):
    """Log de WARNING."""
    user_info = _get_user_info()
    extras = _format_extras(**kwargs)
    log_msg = f"[{action}][WARN] ⚠️ {message} | {user_info}{extras}"
    logger.warning(log_msg)


def log_debug(action: str, message: str, module: str = "SYSTEM", **kwargs):
    """Log de DEBUG (detalhes técnicos)."""
    user_info = _get_user_info()
    extras = _format_extras(**kwargs)
    log_msg = f"[{action}][DEBUG] 🔍 {message} | {user_info}{extras}"
    logger.debug(log_msg)


# ============================================================
# CONTEXT MANAGER PARA OPERAÇÕES LONGAS
# ============================================================

@contextmanager
def timed_operation(action: str, message: str = "", **kwargs):
    """
    Context manager que loga início e fim de uma operação com duração.
    
    Uso:
        with timed_operation("RPA_BATCH", batch_id=5, total=19):
            # código do batch
    """
    start_time = time.time()
    start_msg = message or f"Iniciando {action}"
    log_start(action, start_msg, **kwargs)
    
    try:
        yield
        duration_ms = (time.time() - start_time) * 1000
        end_msg = message.replace("Iniciando", "Finalizado") if message else f"Finalizado {action}"
        log_end(action, end_msg, duration_ms=duration_ms, **kwargs)
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        log_error(action, f"Falha em {action}", error=str(e), duration_ms=duration_ms, include_traceback=True, **kwargs)
        raise


# ============================================================
# DECORATOR PARA FUNÇÕES
# ============================================================

def log_function(action: str = None, log_args: bool = False, log_result: bool = False):
    """
    Decorator que loga início/fim de uma função.
    
    Uso:
        @log_function("PDF_EXTRACTION")
        def extract_pdf(file_path):
            ...
        
        @log_function(log_args=True)
        def process_batch(batch_id, items):
            ...
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            func_action = action or func.__name__.upper()
            
            # Log de início
            args_str = ""
            if log_args:
                args_str = f"args={args[:3]}... kwargs_keys={list(kwargs.keys())}"
            log_start(func_action, f"Executando {func.__name__}()", args=args_str if args_str else None)
            
            start_time = time.time()
            try:
                result = func(*args, **kwargs)
                duration_ms = (time.time() - start_time) * 1000
                
                result_str = ""
                if log_result and result is not None:
                    result_str = f"result_type={type(result).__name__}"
                
                log_end(func_action, f"Concluído {func.__name__}()", duration_ms=duration_ms, result=result_str if result_str else None)
                return result
            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                log_error(func_action, f"Falha em {func.__name__}()", error=str(e), duration_ms=duration_ms, include_traceback=True)
                raise
        
        return wrapper
    
    # Permite uso com ou sem parênteses: @log_function ou @log_function("ACTION")
    if callable(action):
        func = action
        action = None
        return decorator(func)
    
    return decorator


def log_async_function(action: str = None, log_args: bool = False):
    """
    Decorator para funções assíncronas.
    
    Uso:
        @log_async_function("RPA_LOGIN")
        async def login_elaw():
            ...
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            func_action = action or func.__name__.upper()
            
            args_str = ""
            if log_args:
                args_str = f"args={args[:3]}... kwargs_keys={list(kwargs.keys())}"
            log_start(func_action, f"Executando async {func.__name__}()", args=args_str if args_str else None)
            
            start_time = time.time()
            try:
                result = await func(*args, **kwargs)
                duration_ms = (time.time() - start_time) * 1000
                log_end(func_action, f"Concluído async {func.__name__}()", duration_ms=duration_ms)
                return result
            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                log_error(func_action, f"Falha em async {func.__name__}()", error=str(e), duration_ms=duration_ms, include_traceback=True)
                raise
        
        return wrapper
    
    if callable(action):
        func = action
        action = None
        return decorator(func)
    
    return decorator


# ============================================================
# LOGGING ESPECÍFICO POR MÓDULO
# ============================================================

class RPALogger:
    """Logger especializado para operações RPA."""
    
    @staticmethod
    def step(step_name: str, message: str, process_id: int = None, **kwargs):
        """Log de etapa do RPA."""
        log_event(f"RPA_STEP_{step_name}", message, module="RPA", process_id=process_id, **kwargs)
    
    @staticmethod
    def navigation(target: str, process_id: int = None, **kwargs):
        """Log de navegação no eLaw."""
        log_event("RPA_NAVIGATION", f"Navegando para: {target}", module="RPA", process_id=process_id, **kwargs)
    
    @staticmethod
    def click(element: str, process_id: int = None, **kwargs):
        """Log de clique em elemento."""
        log_event("RPA_CLICK", f"Clicando em: {element}", module="RPA", process_id=process_id, **kwargs)
    
    @staticmethod
    def fill(field: str, process_id: int = None, **kwargs):
        """Log de preenchimento de campo."""
        log_event("RPA_FILL", f"Preenchendo campo: {field}", module="RPA", process_id=process_id, **kwargs)
    
    @staticmethod
    def screenshot(name: str, process_id: int = None, **kwargs):
        """Log de captura de screenshot."""
        log_event("RPA_SCREENSHOT", f"Capturando screenshot: {name}", module="RPA", process_id=process_id, **kwargs)
    
    @staticmethod
    def browser_start(process_id: int = None, headless: bool = True, **kwargs):
        """Log de início do browser."""
        log_start("RPA_BROWSER", f"Iniciando browser (headless={headless})", process_id=process_id, **kwargs)
    
    @staticmethod
    def browser_end(process_id: int = None, **kwargs):
        """Log de fechamento do browser."""
        log_end("RPA_BROWSER", "Browser fechado", process_id=process_id, **kwargs)


class BatchLogger:
    """Logger especializado para operações de batch."""
    
    @staticmethod
    def batch_start(batch_id: int, total_items: int, **kwargs):
        """Log de início de batch."""
        log_start("BATCH", f"Iniciando batch com {total_items} itens", batch_id=batch_id, total_items=total_items, **kwargs)
    
    @staticmethod
    def batch_end(batch_id: int, total: int, success: int, errors: int, duration_ms: float = None, **kwargs):
        """Log de fim de batch."""
        log_end("BATCH", f"Batch finalizado: {success}/{total} sucesso, {errors} erros", 
                batch_id=batch_id, total=total, success=success, errors=errors, duration_ms=duration_ms, **kwargs)
    
    @staticmethod
    def item_start(batch_id: int, item_id: int, process_id: int = None, **kwargs):
        """Log de início de item do batch."""
        log_start("BATCH_ITEM", f"Processando item", batch_id=batch_id, item_id=item_id, process_id=process_id, **kwargs)
    
    @staticmethod
    def item_end(batch_id: int, item_id: int, status: str, process_id: int = None, **kwargs):
        """Log de fim de item do batch."""
        log_end("BATCH_ITEM", f"Item finalizado com status: {status}", 
                batch_id=batch_id, item_id=item_id, status=status, process_id=process_id, **kwargs)
    
    @staticmethod
    def thread_start(thread_id: int, batch_id: int, **kwargs):
        """Log de início de thread."""
        log_start("BATCH_THREAD", f"Thread iniciada", thread_id=thread_id, batch_id=batch_id, **kwargs)
    
    @staticmethod
    def thread_end(thread_id: int, batch_id: int, **kwargs):
        """Log de fim de thread."""
        log_end("BATCH_THREAD", f"Thread finalizada", thread_id=thread_id, batch_id=batch_id, **kwargs)


class ExtractionLogger:
    """Logger especializado para extração de dados."""
    
    @staticmethod
    def pdf_start(filename: str, process_id: int = None, **kwargs):
        """Log de início de extração de PDF."""
        log_start("PDF_EXTRACTION", f"Extraindo dados de: {filename}", process_id=process_id, filename=filename, **kwargs)
    
    @staticmethod
    def pdf_end(filename: str, fields_extracted: int = 0, process_id: int = None, duration_ms: float = None, **kwargs):
        """Log de fim de extração de PDF."""
        log_end("PDF_EXTRACTION", f"Extração concluída: {fields_extracted} campos", 
                process_id=process_id, filename=filename, fields_extracted=fields_extracted, duration_ms=duration_ms, **kwargs)
    
    @staticmethod
    def regex_attempt(field: str, success: bool, process_id: int = None, **kwargs):
        """Log de tentativa de extração via Regex."""
        status = "✅ encontrado" if success else "❌ não encontrado"
        log_debug("EXTRACTION_REGEX", f"Campo {field}: {status}", process_id=process_id, field=field, success=success, **kwargs)
    
    @staticmethod
    def llm_fallback(field: str, process_id: int = None, **kwargs):
        """Log de fallback para LLM."""
        log_event("EXTRACTION_LLM", f"Usando LLM para campo: {field}", process_id=process_id, field=field, **kwargs)
    
    @staticmethod
    def ocr_start(filename: str, process_id: int = None, **kwargs):
        """Log de início de OCR."""
        log_start("OCR", f"Iniciando OCR em: {filename}", process_id=process_id, filename=filename, **kwargs)
    
    @staticmethod
    def ocr_end(filename: str, pages_processed: int = 0, process_id: int = None, duration_ms: float = None, **kwargs):
        """Log de fim de OCR."""
        log_end("OCR", f"OCR concluído: {pages_processed} páginas", 
                process_id=process_id, filename=filename, pages_processed=pages_processed, duration_ms=duration_ms, **kwargs)


class AuthLogger:
    """Logger especializado para autenticação."""
    
    @staticmethod
    def login_attempt(username: str, **kwargs):
        """Log de tentativa de login."""
        log_event("AUTH_LOGIN", f"Tentativa de login", username=username, **kwargs)
    
    @staticmethod
    def login_success(username: str, user_id: int = None, **kwargs):
        """Log de login bem-sucedido."""
        log_success("AUTH_LOGIN", f"Login realizado com sucesso", username=username, user_id=user_id, **kwargs)
    
    @staticmethod
    def login_failed(username: str, reason: str = None, **kwargs):
        """Log de login falhou."""
        log_error("AUTH_LOGIN", f"Falha no login", error=reason, username=username, **kwargs)
    
    @staticmethod
    def logout(username: str, user_id: int = None, **kwargs):
        """Log de logout."""
        log_event("AUTH_LOGOUT", f"Logout realizado", username=username, user_id=user_id, **kwargs)
    
    @staticmethod
    def access_denied(endpoint: str, **kwargs):
        """Log de acesso negado."""
        log_warning("AUTH_ACCESS_DENIED", f"Acesso negado ao endpoint: {endpoint}", endpoint=endpoint, **kwargs)


class UILogger:
    """Logger especializado para ações de UI."""
    
    @staticmethod
    def tab_click(tab_name: str, **kwargs):
        """Log de clique em aba."""
        log_event("UI_TAB_CLICK", f"Clicou na aba: {tab_name}", tab_name=tab_name, **kwargs)
    
    @staticmethod
    def button_click(button_name: str, **kwargs):
        """Log de clique em botão."""
        log_event("UI_BUTTON_CLICK", f"Clicou no botão: {button_name}", button_name=button_name, **kwargs)
    
    @staticmethod
    def form_submit(form_name: str, **kwargs):
        """Log de submit de formulário."""
        log_event("UI_FORM_SUBMIT", f"Formulário submetido: {form_name}", form_name=form_name, **kwargs)
    
    @staticmethod
    def page_view(page_name: str, **kwargs):
        """Log de visualização de página."""
        log_event("UI_PAGE_VIEW", f"Visualizando página: {page_name}", page_name=page_name, **kwargs)
    
    @staticmethod
    def file_upload(filename: str, size_mb: float = None, **kwargs):
        """Log de upload de arquivo."""
        size_str = f" ({size_mb:.2f}MB)" if size_mb else ""
        log_event("UI_FILE_UPLOAD", f"Upload de arquivo: {filename}{size_str}", filename=filename, size_mb=size_mb, **kwargs)


# Instâncias dos loggers especializados
rpa = RPALogger()
batch = BatchLogger()
extraction = ExtractionLogger()
auth = AuthLogger()
ui = UILogger()


# ============================================================
# MIDDLEWARE FLASK
# ============================================================

def init_flask_logging(app):
    """
    Inicializa logging no Flask app.
    
    Uso:
        from logging_config import init_flask_logging
        init_flask_logging(app)
    """
    
    @app.before_request
    def log_request_start():
        """Log antes de cada request."""
        g.request_start_time = time.time()
        
        # Não logar requests estáticos e de polling
        if request.endpoint and ('static' in request.endpoint or 'status' in request.endpoint.lower()):
            g.skip_logging = True
            return
        
        g.skip_logging = False
        user_info = _get_user_info()
        log_event("HTTP_REQUEST", f"{request.method} {request.path}", 
                  method=request.method, path=request.path, endpoint=request.endpoint)
    
    @app.after_request
    def log_request_end(response):
        """Log após cada request."""
        if hasattr(g, 'skip_logging') and g.skip_logging:
            return response
        
        if hasattr(g, 'request_start_time'):
            duration_ms = (time.time() - g.request_start_time) * 1000
            log_event("HTTP_RESPONSE", f"{request.method} {request.path} -> {response.status_code}",
                      method=request.method, path=request.path, 
                      status_code=response.status_code, duration_ms=f"{duration_ms:.0f}ms")
        
        return response
    
    @app.errorhandler(Exception)
    def log_exception(error):
        """Log de exceções não tratadas."""
        log_error("HTTP_ERROR", f"Exceção não tratada: {type(error).__name__}",
                  error=str(error), include_traceback=True,
                  method=request.method if has_request_context() else None,
                  path=request.path if has_request_context() else None)
        raise error
    
    logger.info("[LOGGING] ✅ Flask logging middleware inicializado")


# Log de inicialização do módulo
logger.info("=" * 60)
logger.info("[LOGGING] Sistema de Logging Centralizado inicializado")
logger.info("[LOGGING] Níveis disponíveis: DEBUG, INFO, WARNING, ERROR")
logger.info("[LOGGING] Módulos: RPA, BATCH, EXTRACTION, AUTH, UI, SYSTEM")
logger.info("=" * 60)
