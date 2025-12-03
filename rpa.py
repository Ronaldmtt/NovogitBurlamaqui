# rpa.py — eLaw RPA (Playwright)
# ---------------------------------------------------------------------------
# Preenche "Novo Processo" com robustez (bootstrap-select + nativo),
# usa PDF/endpoint para inferências e segue a ordem pedida
# (Cliente → Parte Adversa (Tipo) → Posição → Parte Adversa (Nome) →
#  Parte Interessada → Valor da Causa).
# Corrigido: norm(), fluxo da Instância/Tipo de Ação/Valor da Causa, imports
# opcionais e helpers unificados.
# ---------------------------------------------------------------------------

import os
import re
import json
import math
import sys
import asyncio
import logging
import unicodedata
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from contextlib import asynccontextmanager

import requests
from dotenv import load_dotenv
from PyPDF2 import PdfReader
from playwright.async_api import async_playwright, Page

# RPA Monitor - Monitoramento remoto via monitor_integration
try:
    from monitor_integration import init_monitor, log_info as monitor_log_info, log_error as monitor_log_error, send_screenshot as monitor_send_screenshot, is_initialized as monitor_is_initialized
    RPA_MONITOR_AVAILABLE = True
except ImportError:
    RPA_MONITOR_AVAILABLE = False
    def monitor_log_info(msg, region=""): pass
    def monitor_log_error(msg, exc=None, region=""): pass
    def monitor_send_screenshot(path, region=""): pass
    def monitor_is_initialized(): return False

# RPA Log - Acesso direto ao rpa_log para screenshots
try:
    sys.path.insert(0, '/home/runner/workspace/rpa_monitor_client/rpa_monitor_client')
    from rpa_monitor_client import rpa_log
    RPA_LOG_AVAILABLE = True
except ImportError:
    RPA_LOG_AVAILABLE = False
    rpa_log = None

# --- imports opcionais (ok se faltar python-docx) ----------------------------
try:
    from docx import Document as DocxDocument  # type: ignore
except Exception:  # linter e runtime safe
    DocxDocument = None  # type: ignore

# --- utils do seu projeto ----------------------------------------------------
from utils.cell_inference import (
    load_alias_rows,
    build_alias_index,
    guess_cell_from_pdf_text,
)
from utils.option_catalog import save_catalog
from utils.normalization import normalize_text

# Sistema de mapeamento de posições do eLaw
from extractors.posicao_mapping import (
    normalize_posicao,
    get_posicao_id,
    get_posicao_label,
)

# Sistema de status em tempo real
# IMPORTANTE: flask_app DEVE ser configurado pelo caller ANTES de executar RPA
# Exemplo: rpa.flask_app = app._get_current_object()
flask_app = None
STATUS_ENABLED = True

# =============================================================================
# SISTEMA DE CONTEXTO THREAD-LOCAL PARA RPA PARALELO
# =============================================================================
# 
# Arquitetura: Cada thread de RPA tem seu próprio contexto isolado usando
# contextvars (thread-safe e asyncio-safe). Isso permite execução paralela
# de múltiplos processos RPA sem conflitos de estado.
#
# Componentes:
# 1. RPAExecutionContext - Dataclass com dados do processo atual
# 2. _rpa_context - ContextVar que armazena o contexto por thread
# 3. Funções auxiliares para acessar o contexto de forma segura

import contextvars
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class RPAExecutionContext:
    """
    Contexto de execução RPA isolado por thread/worker.
    
    Cada worker de RPA paralelo cria sua própria instância deste contexto,
    garantindo isolamento total de estado entre execuções simultâneas.
    """
    process_id: int
    worker_id: Optional[int] = None  # ID do worker no ThreadPoolExecutor
    screenshot_prefix: str = ""  # Prefixo único para screenshots
    started_at: Optional[datetime] = None
    
    def __post_init__(self):
        from datetime import datetime
        if self.started_at is None:
            self.started_at = datetime.utcnow()
        if not self.screenshot_prefix:
            self.screenshot_prefix = f"process_{self.process_id}"
        if self.worker_id is not None:
            self.screenshot_prefix = f"w{self.worker_id}_{self.screenshot_prefix}"

# ContextVar para armazenar contexto por thread (thread-safe + asyncio-safe)
_rpa_context: contextvars.ContextVar[Optional[RPAExecutionContext]] = contextvars.ContextVar(
    '_rpa_context', default=None
)

def get_current_context() -> Optional[RPAExecutionContext]:
    """Retorna o contexto RPA da thread/task atual (thread-safe)."""
    return _rpa_context.get()

def get_current_process_id() -> Optional[int]:
    """
    Retorna o process_id do contexto atual (thread-safe).
    
    2025-11-27: Prioridade: contextvar → global
    - RPA paralelo: usa contextvar (isolado por thread)
    - RPA legado: fallback para global (compatibilidade)
    """
    ctx = _rpa_context.get()
    if ctx:
        return ctx.process_id
    # Fallback para global (necessário para execute_rpa() legado)
    return _current_process_id

def set_rpa_context(ctx: RPAExecutionContext) -> contextvars.Token:
    """
    Define o contexto RPA para a thread/task atual.
    Retorna token para reset posterior.
    """
    return _rpa_context.set(ctx)

def reset_rpa_context(token: contextvars.Token) -> None:
    """Reseta o contexto RPA usando o token."""
    _rpa_context.reset(token)

# =============================================================================
# LOCKS E SEMÁFOROS PARA PARALELISMO CONTROLADO
# =============================================================================

# Configuração de paralelismo
# 2025-12-03: Reduzido para 3 workers em produção para evitar falta de recursos
# Em desenvolvimento pode usar 5, mas produção Replit tem recursos limitados
_DEFAULT_WORKERS = "3" if os.getenv("REPL_DEPLOYMENT") else "5"
MAX_RPA_WORKERS = int(os.getenv("MAX_RPA_WORKERS", _DEFAULT_WORKERS))  # Máximo de RPAs paralelos

# Semáforo para controlar número máximo de execuções RPA simultâneas
# Substitui o antigo _execute_rpa_lock (mutex) por semáforo (permite N simultâneos)
_execute_rpa_semaphore = threading.Semaphore(MAX_RPA_WORKERS)

# Lock para serializar lançamentos do browser (evita picos de CPU/memória)
# Mantido como Lock para garantir que apenas 1 browser inicia por vez
_browser_launch_lock = threading.Lock()

# LEGADO: Mantido para compatibilidade durante migração
# TODO: Remover após migração completa para contextvars
_current_process_id: Optional[int] = None
_execute_rpa_lock = threading.Lock()  # LEGADO: Será substituído por semáforo

try:
    from rpa_status import RPAStatusManager
except Exception as e:
    STATUS_ENABLED = False
    print(f"[WARN] rpa_status não disponível - status em tempo real desabilitado: {e}")

# =========================
# Config
# =========================
load_dotenv(override=True)

BASE_URL = os.getenv("ELAW_BASE_URL", "https://acburlamaquihm.elawio.com.br/").strip()
ELAW_USER = os.getenv("ELAW_USER", "").strip()
ELAW_PASS = os.getenv("ELAW_PASS", "").strip()

RPA_DATA_JSON = os.getenv("RPA_DATA_JSON", "instance/rpa_current.json").strip()
UPLOADS_DIR = Path(os.getenv("RPA_UPLOADS_DIR", "./uploads")).resolve()

HEADLESS = os.getenv("RPA_HEADLESS", "true").strip().lower() in {"1", "true", "yes"}  # Default TRUE para VM sem X server
SLOWMO_MS = int(os.getenv("RPA_SLOWMO_MS", "0"))
DEFAULT_TIMEOUT_MS = int(os.getenv("RPA_DEFAULT_TIMEOUT_MS", "30000"))  # 30s (seguro para operações gerais)
NAV_TIMEOUT_MS = int(os.getenv("RPA_NAV_TIMEOUT_MS", "180000"))  # 180s (3 min - aumentado para produção Replit)
BROWSER_LAUNCH_TIMEOUT_MS = int(os.getenv("RPA_BROWSER_LAUNCH_TIMEOUT_MS", "180000"))  # 180s (3 min - aumentado para produção Replit)
SHORT_TIMEOUT_MS = int(os.getenv("RPA_SHORT_TIMEOUT_MS", "1500"))
VERY_SHORT_TIMEOUT_MS = int(os.getenv("RPA_VERY_SHORT_TIMEOUT_MS", "700"))

TYPE_DELAY_MS = int(os.getenv("RPA_TYPE_DELAY_MS", "6"))
CLICK_AFTER_OPEN_MS = int(os.getenv("RPA_CLICK_AFTER_OPEN_MS", "25"))
SETTLE_NET_MS = int(os.getenv("RPA_SETTLE_NET_MS", "60"))  # 90→60ms (economia ~0.5s acumulado)
SETTLE_SLEEP_MS = int(os.getenv("RPA_SETTLE_SLEEP_MS", "10"))  # 15→10ms (economia ~0.1s acumulado)

NAV_RETRIES = int(os.getenv("RPA_NAV_RETRIES", "2"))
KEEP_OPEN_AFTER_LOGIN_SECONDS = float(os.getenv("RPA_KEEP_OPEN_AFTER_LOGIN_SECONDS", "25"))

SCREENSHOT_DIR = Path(os.getenv("RPA_SCREENSHOT_DIR", "./rpa_screenshots")).resolve()
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

def _get_screenshot_path(filename: str, process_id: Optional[int] = None) -> Path:
    """
    🔧 2025-11-27: Atualizado para suportar RPA paralelo via contextvars
    
    Retorna caminho único de screenshot por processo, agora thread-safe.
    Usa contextvars para obter process_id quando não fornecido explicitamente.
    
    Args:
        filename: Nome base do arquivo (ex: 'elaw_flow_error.png')
        process_id: ID do processo (opcional - usa contexto thread-local se None)
    
    Returns:
        Path com nome único: 'process_123_elaw_flow_error.png'
        OU 'w2_process_123_elaw_flow_error.png' se worker_id disponível
        OU path genérico se process_id indisponível (graceful degradation)
    """
    # 1. Tentar process_id explícito
    pid = process_id
    prefix = None
    
    # 2. Se não fornecido, tentar contexto thread-local (novo sistema paralelo)
    if pid is None:
        ctx = get_current_context()
        if ctx:
            pid = ctx.process_id
            prefix = ctx.screenshot_prefix  # Já inclui worker_id se disponível
    
    # 3. Fallback para global legado (compatibilidade)
    if pid is None:
        pid = _current_process_id
    
    # 4. Graceful degradation se nenhum ID disponível
    if pid is None:
        log(f"[SCREENSHOT][WARN] _get_screenshot_path sem process_id - usando path genérico (filename={filename})")
        return SCREENSHOT_DIR / filename
    
    # Preservar extensão do arquivo
    parts = filename.rsplit('.', 1)
    if len(parts) == 2:
        name, ext = parts
        if prefix:
            unique_filename = f"{prefix}_{name}.{ext}"
        else:
            unique_filename = f"process_{pid}_{name}.{ext}"
    else:
        if prefix:
            unique_filename = f"{prefix}_{filename}"
        else:
            unique_filename = f"process_{pid}_{filename}"
    
    return SCREENSHOT_DIR / unique_filename

VIEWPORT_MODE = os.getenv("RPA_VIEWPORT_MODE", "MAX").strip().upper()
VIEWPORT_WIDTH = int(os.getenv("RPA_VIEWPORT_WIDTH", "1400"))
VIEWPORT_HEIGHT = int(os.getenv("RPA_VIEWPORT_HEIGHT", "900"))
FORCE_DEVICE_SCALE = os.getenv("RPA_FORCE_DEVICE_SCALE", "1").strip()
ENFORCE_ZOOM_RESET = os.getenv("RPA_ENFORCE_ZOOM_RESET", "true").strip().lower() in {"1", "true", "yes"}

HEADER_OFFSET_MANUAL = int(os.getenv("RPA_HEADER_OFFSET_PX", "0"))
BLOCK_LIGHT_RESOURCES = os.getenv("RPA_BLOCK_LIGHT_RESOURCES", "0").strip().lower() in {"1", "true", "yes"}

ENV_PROCESS_ID = os.getenv("RPA_PROCESS_ID", "").strip()
INSTANCIA_SELECT_ID = os.getenv("RPA_INSTANCIA_SELECT_ID", "InstanciaId").strip()

LLM_ENABLED = os.getenv("RPA_LLM_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

PW_TRACING = os.getenv("RPA_TRACING", "0").strip().lower() in {"1", "true", "yes"}

RPA_EXPECT_CNJ = os.getenv("RPA_EXPECT_CNJ", "").strip()
RPA_DATA_TTL_SECONDS = int(os.getenv("RPA_DATA_TTL_SECONDS", "900"))

RPA_CELL_DOCX_PATHS = os.getenv(
    "RPA_CELL_DOCX_PATHS",
    "config/CLIENTE X CÉLULA.docx;config/CLIENTE X CÉLULA x PARTE INTERESSADA.docx;docs/CLIENTE X CÉLULA.docx",
).split(";")
RPA_CELL_JSON_PATH = os.getenv("RPA_CELL_JSON_PATH", "config/client_cell_map.json").strip()

RPA_DEBUG = os.getenv("RPA_DEBUG", "1").strip().lower() in {"1", "true", "yes"}

RPA_DISALLOW_CAUTELAR = os.getenv("RPA_DISALLOW_CAUTELAR", "true").strip().lower() in {"1", "true", "yes"}
RPA_PREVIEW_SECONDS = float(os.getenv("RPA_PREVIEW_SECONDS", "5"))
RPA_SKIP_SAVE = os.getenv("RPA_SKIP_SAVE", "0").strip().lower() in {"1", "true", "yes"}

# RPA Monitor - Configurações de monitoramento remoto
RPA_MONITOR_ENABLED = os.getenv("RPA_MONITOR_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
RPA_MONITOR_ID = os.getenv("RPA_MONITOR_ID", "").strip()
RPA_MONITOR_HOST = os.getenv("RPA_MONITOR_HOST", "").strip()
RPA_MONITOR_PORT = os.getenv("RPA_MONITOR_PORT", "").strip()
RPA_MONITOR_REGION = os.getenv("RPA_MONITOR_REGION", "CBD-eLaw").strip()
RPA_MONITOR_TRANSPORT = os.getenv("RPA_MONITOR_TRANSPORT", "ws").strip()
_monitor_initialized = False

# --- LOG ---
LOG = logging.getLogger("rpa")
if not LOG.handlers:
    LOG.setLevel(logging.INFO)
    fmt = logging.Formatter("[rpa] %(asctime)s %(message)s", "%Y-%m-%dT%H:%M:%S")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)
    LOG.addHandler(ch)

def _init_rpa_monitor():
    """Inicializa o RPA Monitor Client se habilitado via monitor_integration"""
    global _monitor_initialized
    
    if _monitor_initialized or not RPA_MONITOR_ENABLED:
        return
    
    if not RPA_MONITOR_AVAILABLE:
        LOG.info("[MONITOR] monitor_integration não disponível - monitoramento desabilitado")
        return
    
    try:
        # Usar monitor_integration.init_monitor() para inicializar
        init_monitor(rpa_id=RPA_MONITOR_ID or "RPA-eLaw")
        _monitor_initialized = monitor_is_initialized()
        if _monitor_initialized:
            LOG.info(f"[MONITOR] ✅ RPA Monitor conectado via monitor_integration")
    except Exception as e:
        LOG.warning(f"[MONITOR] Erro ao inicializar monitor: {e}")

def log(msg: str) -> None:
    """Log local + envio para RPA Monitor (se habilitado)"""
    LOG.info(msg)
    
    # Enviar para monitor remoto se disponível
    if monitor_is_initialized():
        try:
            monitor_log_info(msg, region="RPA")
        except Exception:
            pass  # Não quebrar execução se monitor falhar

def dlog(msg: str) -> None:
    if RPA_DEBUG:
        LOG.info("[DEBUG] " + msg)

def send_screenshot_to_monitor(screenshot_path: Path, region: str = "RPA"):
    """Envia screenshot PNG para o RPA Monitor via monitor_integration"""
    if not monitor_is_initialized():
        return
    
    try:
        # Usar monitor_integration.send_screenshot()
        monitor_send_screenshot(screenshot_path, region=region)
        LOG.info(f"[MONITOR] 📸 Screenshot enviado via monitor_integration: {screenshot_path.name}")
    except Exception as e:
        LOG.warning(f"[MONITOR] Erro ao enviar screenshot: {e}")

def log_error_to_monitor(error_msg: str, exc: Optional[Exception] = None):
    """Envia log de erro para o RPA Monitor via monitor_integration"""
    if not monitor_is_initialized():
        return
    
    try:
        monitor_log_error(error_msg, exc=exc, region="RPA")
    except Exception:
        pass  # Não quebrar execução se monitor falhar

def validate_env():
    if not ELAW_USER or not ELAW_PASS:
        raise RuntimeError("Defina ELAW_USER e ELAW_PASS no .env")

# =========================
# Sistema de Status em Tempo Real
# =========================
# 2025-11-21: ZERO SHARED STATE - Architect Review aprovado
# Variáveis globais ELIMINADAS - process_id SEMPRE passado explicitamente
# Cada função EXIGE process_id como parâmetro - sem fallbacks!

def _init_status(process_id: int):
    """
    Inicializa o gerenciador de status para o processo
    2025-11-21: ZERO shared state - apenas cria manager inicial, não salva nada global
    
    Args:
        process_id: ID do processo (OBRIGATÓRIO)
    """
    if not process_id:
        log("[STATUS][WARN] _init_status chamado sem process_id - ignorando")
        return
    
    if STATUS_ENABLED and flask_app:
        try:
            with flask_app.app_context():
                manager = RPAStatusManager(process_id)
                manager.update("iniciando", "Sistema de automação iniciado", status="starting")
                log(f"[STATUS][#{process_id}] Status manager inicializado (ZERO shared state)")
        except Exception as e:
            log(f"[STATUS][#{process_id}][WARN] Erro ao inicializar status: {e}")

def update_status(step: str, message: str, status: str = "running", data: dict = None, process_id: Optional[int] = None):
    """
    Helper para atualizar status do RPA
    2025-11-27: Thread-safe - usa contextvar primeiro, global como último fallback
    
    Args:
        step: Identificador do passo
        message: Mensagem descritiva
        status: Status do processo (running, error, completed, etc)
        data: Dados adicionais (opcional)
        process_id: ID do processo (opcional - usa contextvar ou global se None)
    """
    global _current_process_id
    
    # 🆕 Prioridade: parâmetro explícito → contextvar → global
    pid = process_id
    if pid is None:
        pid = get_current_process_id()  # Tenta contextvar primeiro
    if pid is None:
        pid = _current_process_id  # Fallback para legado
    
    if pid is None:
        log(f"[STATUS][ERROR] update_status chamado sem process_id (contextvar e global=None) (step={step})")
        return
    
    if STATUS_ENABLED and flask_app:
        try:
            with flask_app.app_context():
                manager = RPAStatusManager(pid)
                manager.update(step, message, status, data)
        except Exception as e:
            log(f"[STATUS][#{pid}][WARN] Erro ao atualizar status: {e}")

def update_field_status(field_key: str, field_label: str, value: Any = None, process_id: Optional[int] = None):
    """
    Helper específico para atualizar status de preenchimento de campo individual
    2025-11-27: Thread-safe - usa contextvar primeiro, global como último fallback
    
    Args:
        field_key: Chave do campo
        field_label: Label do campo
        value: Valor do campo (opcional)
        process_id: ID do processo (opcional - usa contextvar ou global se None)
    """
    global _current_process_id
    
    # 🆕 Prioridade: parâmetro explícito → contextvar → global
    pid = process_id
    if pid is None:
        pid = get_current_process_id()  # Tenta contextvar primeiro
    if pid is None:
        pid = _current_process_id  # Fallback para legado
    
    if pid is None:
        log(f"[STATUS][ERROR] update_field_status chamado sem process_id (contextvar e global=None) para campo {field_key}")
        return
        
    msg = f"{field_label}"
    if value and str(value).strip():
        msg += f": {str(value)[:80]}"  # Limita tamanho do valor mostrado
    msg += " ✓"
    update_status(f"campo_{field_key}", msg, process_id=pid)

# =========================
# Normalização
# =========================
def norm(s: str) -> str:
    """normaliza para matching: sem acento, minúsculo, sem NBSP"""
    s = (s or "").replace("\xa0", " ").strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"\s+", " ", s).lower()
    return s.encode("utf-8", "ignore").decode("utf-8")

# manter compatibilidade com chamadas antigas
_norm = norm

# =========================
# Helpers gerais
# =========================
def tokens(s: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9]+", norm(s)) if len(t) >= 2]

def jaccard(a: List[str], b: List[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    inter = len(sa & sb)
    uni = len(sa | sb) or 1
    return inter / uni

def _detect_ordinal(text: str) -> Optional[int]:
    """
    Detecta se texto contém ordinal (1ª/primeira, 2ª/segunda, etc).
    Retorna: 1 para primeira/1ª/1º, 2 para segunda/2ª/2º, None se não detectar.
    
    ✅ FIX: Inclui símbolos ordinários (ª/º) que não são removidos pela normalização.
    """
    tn = norm(text)
    # Primeira instância (inclui símbolos ordinários ª/º)
    if any(x in tn for x in ["1a", "1o", "1ª", "1º", "primeira", "primeiro"]):
        return 1
    # Segunda instância (inclui símbolos ordinários ª/º)
    if any(x in tn for x in ["2a", "2o", "2ª", "2º", "segunda", "segundo"]):
        return 2
    return None

def _best_match(options: List[str], wanted: str, prefer_words: Optional[List[str]] = None, threshold: int = 28) -> Optional[str]:
    """
    Fuzzy matching com desempate por Jaccard similarity.
    
    Bug corrigido (2025-11-12): Antes dava score 55 para qualquer opção com token comum,
    causando empate entre "Reclamação Trabalhista" e "Reclamação Correicional".
    
    Bug corrigido (2025-11-14): Adicionada detecção de ordinais para evitar inversão
    entre "1ª Instância" ↔ "Segunda Instância". Quando ambos têm ordinal, bônus/penalidade
    garante que ordinais concordantes sempre ganhem.
    
    Nova lógica: Jaccard score SEMPRE conta, exact match e substring dão bônus ADITIVOS.
    """
    wn = norm(wanted)
    wt = tokens(wanted)
    best = None
    score_best = -1
    
    # Detecta ordinal no wanted (1ª/primeira ou 2ª/segunda)
    wanted_ordinal = _detect_ordinal(wanted)
    
    for opt in options:
        on = norm(opt)
        ot = tokens(opt)
        
        # Inicia com Jaccard similarity (0-70)
        jaccard_score = int(jaccard(wt, ot) * 70)
        score = jaccard_score
        
        # Bônus ADITIVO para exact match (+30)
        if on == wn and wn:
            score = 100  # Exact match sempre ganha
        # Bônus ADITIVO para substring (+15) - NÃO sobrescreve Jaccard
        elif wn and wn in on:
            score += 15
        
        # Bônus para palavras preferidas (+12)
        if prefer_words and any(norm(p) in on for p in prefer_words):
            score += 12
        
        # 🔧 NOVO: Detecção de ordinais (crítico para instâncias, varas, etc)
        if wanted_ordinal is not None:
            opt_ordinal = _detect_ordinal(opt)
            if opt_ordinal is not None:
                if wanted_ordinal == opt_ordinal:
                    # Ordinais concordam: BÔNUS massivo (+50)
                    score += 50
                    dlog(f"[ORDINAL] ✅ Match: {wanted} ({wanted_ordinal}) ↔ {opt} ({opt_ordinal}) → +50 bonus")
                else:
                    # Ordinais DISCORDAM: PENALIDADE massiva (-100)
                    score -= 100
                    dlog(f"[ORDINAL] ❌ Mismatch: {wanted} ({wanted_ordinal}) ≠ {opt} ({opt_ordinal}) → -100 penalty")
        
        # Atualiza melhor match (só se score for ESTRITAMENTE MAIOR)
        if score > score_best:
            best, score_best = opt, score
    
    return best if score_best >= threshold else None

async def short_sleep_ms(ms: int):
    await asyncio.sleep(ms / 1000.0)

async def wait_network_quiet(page, timeout_ms: int):
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass

def _fmt_ptbr(n: float | int | str) -> str:
    """Formata número como moeda pt-BR (sem símbolo R$)."""
    if isinstance(n, str):
        return n.strip()
    s = f"{float(n):,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")

# =========================
# PDF utils
# =========================
def _read_all_pdfs_text(base_dir: Path, limit: int = 4) -> str:
    if not base_dir.exists():
        return ""
    pdfs = sorted(base_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    chunks = []
    for p in pdfs:
        try:
            reader = PdfReader(str(p))
            txt = []
            for pg in reader.pages:
                try:
                    txt.append(pg.extract_text() or "")
                except Exception:
                    continue
            if txt:
                chunks.append("\n".join(txt))
        except Exception:
            continue
    return "\n\n".join(chunks)

def read_pdf_api_all_text() -> str:
    """DEPRECATED: Use get_process_pdf_text() instead to avoid reading wrong PDF"""
    api = os.getenv("PDF_SCRAPE_API", "").strip()
    if api:
        try:
            r = requests.get(api, timeout=25)
            if r.status_code == 200:
                data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"text": r.text}
                return (data.get("text") or "").strip()
        except Exception:
            pass
    return _read_all_pdfs_text(UPLOADS_DIR)

def get_process_pdf_text(data: Dict[str, Any], process_id: Optional[int] = None) -> str:
    """
    Carrega o texto do PDF ESPECÍFICO do processo, evitando mistura de dados.
    CRÍTICO: Só usa fallback genérico como último recurso (e lança exceção).
    
    Args:
        data: Dicionário com dados do processo (deve conter 'pdf_filename' se disponível)
        process_id: ID do processo (opcional, para logging)
    
    Returns:
        Texto completo do PDF do processo
    
    Raises:
        ValueError: Se tiver que usar fallback genérico (risco de mistura)
    """
    # Prioridade 1: PDF específico do processo via filename do banco
    pdf_filename = (data.get("pdf_filename") or "").strip()
    
    if pdf_filename and UPLOADS_DIR.exists():
        # Corrigir caso onde pdf_filename já contém "uploads/" no início
        if pdf_filename.startswith("uploads/"):
            pdf_path = Path(pdf_filename)  # Usar caminho direto sem duplicar
        else:
            pdf_path = UPLOADS_DIR / pdf_filename
        
        if pdf_path.exists() and pdf_path.is_file():
            try:
                reader = PdfReader(str(pdf_path))
                txt = []
                for pg in reader.pages:
                    try:
                        txt.append(pg.extract_text() or "")
                    except Exception:
                        continue
                if txt:
                    text = "\n".join(txt)
                    log(f"[PDF] ✅ PDF específico carregado: {pdf_filename} ({len(text)} chars, process_id={process_id})")
                    return text
            except Exception as e:
                log(f"[PDF][WARN] Erro ao ler PDF específico {pdf_filename}: {e}")
        else:
            log(f"[PDF][WARN] PDF vinculado não encontrado: {pdf_filename}")
    
    # Prioridade 2: API externa (se configurada)
    api = os.getenv("PDF_SCRAPE_API", "").strip()
    if api:
        try:
            r = requests.get(api, timeout=25)
            if r.status_code == 200:
                api_data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"text": r.text}
                text = (api_data.get("text") or "").strip()
                if text:
                    log(f"[PDF] ✅ Carregado via API externa ({len(text)} chars)")
                    return text
        except Exception as e:
            log(f"[PDF][WARN] Erro ao carregar PDF via API: {e}")
    
    # CRÍTICO: Fallback genérico é PERIGOSO - pode misturar processos!
    error_msg = f"Processo {process_id} sem PDF específico vinculado. ABORTANDO para evitar mistura de dados!"
    log(f"[PDF][CRITICAL] {error_msg}")
    log(f"[PDF][CRITICAL] Dados do banco devem ser suficientes, ou processo precisa ter PDF vinculado.")
    raise ValueError(error_msg)

# =========================
# Browser
# =========================
@asynccontextmanager
async def launch_browser():
    async with async_playwright() as p:
        args = [
            "--disable-dev-shm-usage",
            "--no-default-browser-check",
            "--no-first-run",
            "--start-maximized",
            "--window-position=0,0",
            "--window-size=1920,1080",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-software-rasterizer",
            "--disable-blink-features=AutomationControlled",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-breakpad",
            "--disable-client-side-phishing-detection",
            "--disable-component-extensions-with-background-pages",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-features=TranslateUI",
            "--disable-hang-monitor",
            "--disable-ipc-flooding-protection",
            "--disable-popup-blocking",
            "--disable-prompt-on-repost",
            "--disable-renderer-backgrounding",
            "--disable-sync",
            "--metrics-recording-only",
            "--no-first-run",
            "--safebrowsing-disable-auto-update",
            "--enable-automation",
            "--password-store=basic",
            "--use-mock-keychain",
            "--ignore-certificate-errors",
            "--ignore-certificate-errors-spki-list",
        ]
        
        # Encontrar Chromium do sistema (Nix) com fallbacks robustos
        # NOTA: Chromium está configurado em .replit nix.packages, então estará disponível
        # tanto em desenvolvimento quanto em produção (Reserved VM)
        import subprocess
        from glob import glob as glob_files
        
        def find_chromium_executable():
            """
            Busca o Chromium em múltiplos locais para garantir compatibilidade
            em desenvolvimento e produção (Replit deploy).
            
            Ordem de prioridade:
            1. Variável de ambiente CHROMIUM_PATH (configuração explícita)
            2. which chromium (padrão no Replit/Nix - PRINCIPAL)
            3. which chromium-browser (alternativo em alguns sistemas)
            4. which google-chrome (fallback para Chrome)
            5. Busca direta no /nix/store (fallback extra)
            """
            # 1. Variável de ambiente explícita
            env_path = os.getenv("CHROMIUM_PATH", "").strip()
            if env_path and os.path.isfile(env_path) and os.access(env_path, os.X_OK):
                log(f"[BROWSER] ✅ Chromium via CHROMIUM_PATH: {env_path}")
                return env_path
            
            # 2-4. Comandos which para diferentes nomes (PRINCIPAL - Nix Chromium)
            for cmd in ["chromium", "chromium-browser", "google-chrome"]:
                try:
                    path = subprocess.check_output(["which", cmd], text=True, stderr=subprocess.DEVNULL).strip()
                    if path and os.path.isfile(path):
                        log(f"[BROWSER] ✅ Chromium via 'which {cmd}': {path}")
                        return path
                except Exception:
                    continue
            
            # 6. Busca direta no /nix/store (fallback extra para edge cases)
            try:
                nix_patterns = [
                    "/nix/store/*-chromium-*/bin/chromium",
                    "/nix/store/*chromium*/bin/chromium",
                ]
                for pattern in nix_patterns:
                    matches = sorted(glob_files(pattern), reverse=True)
                    for match in matches:
                        if os.path.isfile(match) and os.access(match, os.X_OK):
                            log(f"[BROWSER] ✅ Chromium via Nix store: {match}")
                            return match
            except Exception as e:
                log(f"[BROWSER] Busca Nix store falhou (normal em alguns ambientes): {e}")
            
            return None
        
        executable_path = find_chromium_executable()
        
        if executable_path:
            log(f"[BROWSER] ✅ Chromium selecionado: {executable_path}")
            # Chromium 138+ requer novo modo headless
            if HEADLESS:
                args.append("--headless")
        else:
            log("[BROWSER] ❌ ERRO CRÍTICO: Chromium NÃO encontrado em nenhum local!")
            log("[BROWSER] Tentados: CHROMIUM_PATH, which chromium/chromium-browser/google-chrome, /nix/store")
            raise RuntimeError(
                "Chromium não encontrado. O RPA requer Chromium instalado no sistema. "
                "Em Replit, verifique se o módulo Nix 'chromium' está instalado. "
                "Ou defina CHROMIUM_PATH com o caminho do executável."
            )
        
        if os.getenv("RPA_FORCE_DEVICE_SCALE", "1").strip() in {"1", "true", "yes", "True"}:
            args.append("--force-device-scale-factor=1")

        log(f"[BROWSER] Iniciando Chromium (headless={HEADLESS}, exec={executable_path})...")
        log(f"[BROWSER] Timeout configurado: {BROWSER_LAUNCH_TIMEOUT_MS}ms")
        update_status("abrindo_navegador", "Lançando processo do Chromium...")
        
        # CRITICAL: Usar threading.Lock para serializar lançamentos entre threads do batch
        # (cada thread do batch cria seu próprio event loop, então asyncio.Lock não funciona)
        log("[BROWSER] 🔒 Aguardando lock de thread para lançamento serializado...")
        with _browser_launch_lock:
            log("[BROWSER] ✅ Lock adquirido - thread tem permissão para lançar browser")
            
            # 2025-12-03: Retry com backoff exponencial para produção
            max_browser_retries = 3
            browser = None
            last_error = None
            
            for attempt in range(max_browser_retries):
                launch_start_time = time.time()
                try:
                    if attempt > 0:
                        backoff_seconds = 5 * (2 ** (attempt - 1))  # 5s, 10s
                        log(f"[BROWSER] ⏳ Tentativa {attempt + 1}/{max_browser_retries} após aguardar {backoff_seconds}s...")
                        await asyncio.sleep(backoff_seconds)
                    
                    browser = await p.chromium.launch(
                        executable_path=executable_path,
                        headless=HEADLESS,  # CRITICAL: Deve ser True na VM (sem X server)
                        slow_mo=SLOWMO_MS, 
                        args=args, 
                        timeout=BROWSER_LAUNCH_TIMEOUT_MS
                    )
                    launch_duration = time.time() - launch_start_time
                    log(f"[BROWSER] ✅ Chromium iniciado com sucesso em {launch_duration:.2f}s (tentativa {attempt + 1})!")
                    log(f"[BROWSER] 🔓 Liberando lock - próxima thread pode iniciar browser")
                    update_status("abrindo_navegador", "Configurando navegador...")
                    break  # Sucesso - sair do loop
                    
                except Exception as e:
                    launch_duration = time.time() - launch_start_time
                    last_error = e
                    log(f"[BROWSER] ⚠️ Tentativa {attempt + 1}/{max_browser_retries} falhou após {launch_duration:.2f}s: {e}")
                    
                    if attempt == max_browser_retries - 1:
                        log(f"[BROWSER] ❌ ERRO CRÍTICO: Todas as {max_browser_retries} tentativas falharam")
                        log(f"[BROWSER] 🔓 Liberando lock após falha total")
                        update_status("erro_navegador", f"Falha ao iniciar navegador após {max_browser_retries} tentativas: {str(e)[:80]}", status="error")
                        raise RuntimeError(f"Não foi possível iniciar o navegador Chromium após {max_browser_retries} tentativas ({BROWSER_LAUNCH_TIMEOUT_MS}ms cada). Última tentativa: {launch_duration:.2f}s. Possível falta de recursos no ambiente de produção.") from e
        
        ctx_kwargs: Dict[str, Any] = {"ignore_https_errors": True}
        if VIEWPORT_MODE == "MAX":
            ctx_kwargs["viewport"] = None
            ctx_kwargs["device_scale_factor"] = 1.0
        else:
            ctx_kwargs["viewport"] = {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT}
            ctx_kwargs["device_scale_factor"] = 1.0

        context = await browser.new_context(**ctx_kwargs)

        if BLOCK_LIGHT_RESOURCES:
            async def _route(r):
                if r.request.resource_type in {"image", "media", "font"}:
                    await r.abort()
                else:
                    await r.continue_()
            await context.route("**/*", _route)

        context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        context.set_default_navigation_timeout(NAV_TIMEOUT_MS)

        page = await context.new_page()
        page.on("console", lambda m: LOG.info(f"[pw.console] {m.type.upper()}: {m.text}"))
        page.on("pageerror", lambda e: LOG.info(f"[pw.pageerror] {e}"))

        try:
            if PW_TRACING:
                Path("rpa_artifacts").mkdir(exist_ok=True)
                await context.tracing.start(screenshots=True, snapshots=True, sources=True)
                log("[TRACE] ON")
        except Exception:
            pass

        try:
            yield page
        finally:
            try:
                if PW_TRACING:
                    await context.tracing.stop(path="rpa_artifacts/trace.zip")
                    log("[TRACE] salvo em rpa_artifacts/trace.zip")
            except Exception:
                pass
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass

async def ensure_zoom_100(page, where: str):
    if not ENFORCE_ZOOM_RESET:
        return
    try:
        await page.bring_to_front()
        await page.keyboard.down("Control")
        await page.keyboard.press("Digit0")
        await page.keyboard.up("Control")
    except Exception:
        pass
    log(f"[ZOOM] reset 100% ({where})")

# =========================
# Navegação / Login
# =========================
async def goto_with_retries(page, url: str, attempts: int, nav_timeout_ms: int):
    last = None
    for i in range(attempts):
        try:
            log(f"[NAV] {i+1}/{attempts}: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
            await short_sleep_ms(250)
            return
        except Exception as e:
            last = e
            await short_sleep_ms(700)
    raise RuntimeError(f"Falha ao navegar: {last}")

async def _check_login_success(page) -> bool:
    """Predicado composto: Toast sucesso (NOVO!) ou URL válida + menu Processos"""
    try:
        # NOVA DETECÇÃO: Toast verde de sucesso (eLaw mudou!)
        success_indicators = [
            ".toast-success",
            ".alert-success", 
            "[class*='success']:has-text('Sucesso')",
            ".swal2-success",  # SweetAlert2
            "[role='alert']:has-text('Sucesso')"
        ]
        
        for selector in success_indicators:
            try:
                toast = page.locator(selector).first
                if await toast.is_visible(timeout=500):
                    log("[LOGIN] ✅ Toast de sucesso detectado - login aprovado!")
                    return True
            except:
                pass
        
        url = page.url
        # Rejeitar about:blank e URLs de login
        if "about:blank" in url or not "elawio.com" in url:
            return False
        if re.search(r"/Account/Login|/Login", url, re.I):
            return False
        
        # Verificar menu Processos (sinal de autenticação)
        menu_visible = await page.locator("a:has-text('Processos'), nav a:has-text('Processos')").first.is_visible(timeout=2000)
        if menu_visible:
            return True
        
        return False
    except:
        return False

async def _check_login_failure(page) -> bool:
    """Detecta falha explícita: formulário ainda visível + mensagens de erro"""
    try:
        # Mensagens de validação/erro (verificar PRIMEIRO)
        error_selectors = [
            ".validation-summary-errors",
            ".alert-danger",
            ".alert-error",
            ".error-message",
            "[class*='error']",
            "[class*='validation']"
        ]
        
        for selector in error_selectors:
            error_msg = page.locator(selector).first
            try:
                if await error_msg.is_visible(timeout=500):
                    msg_text = await error_msg.text_content()
                    if msg_text and len(msg_text.strip()) > 0:
                        log(f"[LOGIN] ❌ Erro do eLaw: {msg_text.strip()[:200]}")
                        return True
            except:
                pass
        
        # REMOVIDO: "Formulário visível" não é mais indicador de falha!
        # eLaw agora mantém formulário visível com overlay "Processando..." durante sucesso
        
        return False  # Apenas retorna True se houver MENSAGEM DE ERRO explícita
    except:
        return False

async def login_elaw(page, user: str, password: str, url: str) -> bool:
    """Login com retry logic, waits robustos e detecção precisa"""
    MAX_ATTEMPTS = 3
    
    for attempt in range(1, MAX_ATTEMPTS + 1):
        log(f"[LOGIN] Tentativa {attempt}/{MAX_ATTEMPTS}")
        
        # Verificar se já está logado
        if await _check_login_success(page):
            log("[LOGIN] ✅ Já autenticado!")
            return True
        
        # Navegar para página de login com timeout longo
        try:
            log(f"[LOGIN] Navegando para {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=180000)  # 3 minutos
            await short_sleep_ms(1000)
            log("[LOGIN] Página carregada")
        except Exception as e:
            log(f"[LOGIN] ⚠️ Timeout na navegação: {str(e)[:80]}")
            # Verificar se apesar do timeout, já está logado
            if await _check_login_success(page):
                log("[LOGIN] ✅ Login já realizado (apesar do timeout)")
                return True
            
            # Backoff exponencial antes de retry
            if attempt < MAX_ATTEMPTS:
                wait_secs = 2 ** attempt
                log(f"[LOGIN] Aguardando {wait_secs}s antes de retry...")
                await short_sleep_ms(wait_secs * 1000)
                continue
            else:
                log("[LOGIN] ❌ Esgotadas tentativas de navegação")
                return False
        
        await ensure_zoom_100(page, "login")
        
        # Aguardar formulário aparecer
        try:
            email_loc = page.locator("input[type='email'], input#Email, input[name='Email']").first
            pwd_loc = page.locator("input[type='password'], input#Password, input[name='Password']").first
            
            await email_loc.wait_for(state="attached", timeout=20000)  # 20s - tolerante a eLaw lento
            log("[LOGIN] Formulário de login detectado")
        except Exception as e:
            log(f"[LOGIN] ⚠️ Formulário não encontrado: {e}")
            # Pode já estar logado
            if await _check_login_success(page):
                log("[LOGIN] ✅ Já logado (sem formulário)")
                return True
            continue
        
        # Preencher credenciais COM EVENTOS (crítico para validação ASP.NET do eLaw)
        try:
            # Email: focus + type + events
            await email_loc.click()
            await email_loc.fill("")  # Limpar primeiro
            await email_loc.type(user, delay=50)
            await email_loc.evaluate("""el => {
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
            }""")
            await short_sleep_ms(300)
            
            # Password: focus + type + events
            await pwd_loc.click()
            await pwd_loc.fill("")  # Limpar primeiro
            await pwd_loc.type(password, delay=50)
            await pwd_loc.evaluate("""el => {
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
            }""")
            await short_sleep_ms(300)
            
            # Verificar se campos foram preenchidos
            email_value = await email_loc.input_value()
            pwd_value = await pwd_loc.input_value()
            
            if email_value != user or pwd_value != password:
                log(f"[LOGIN] ⚠️ Valores não correspondem! Email OK: {email_value == user}, Senha OK: {pwd_value == password}")
            else:
                log("[LOGIN] ✅ Credenciais preenchidas e verificadas")
            
            # DIAGNÓSTICO CRÍTICO: Verificar se há mensagens de erro ANTES do submit
            try:
                html_before = await page.content()
                if "inválid" in html_before.lower() or "incorret" in html_before.lower():
                    log(f"[LOGIN] ⚠️ Erro pré-submit detectado no HTML!")
                    
                # Verificar se há campos ocultos obrigatórios (CSRF, etc)
                hidden_inputs = await page.locator("input[type='hidden']").all()
                log(f"[LOGIN] Detectados {len(hidden_inputs)} campos ocultos no formulário")
            except:
                pass
        except Exception as e:
            log(f"[LOGIN] ❌ Erro ao preencher: {e}")
            continue
        
        # Submit
        try:
            btn = page.locator("button[type='submit'], input[type='submit'], button:has-text('Entrar')").first
            await btn.click(timeout=5000)
            log("[LOGIN] Formulário submetido")
        except Exception:
            try:
                await pwd_loc.press("Enter", timeout=3000)
                log("[LOGIN] Enter pressionado")
            except Exception as e:
                log(f"[LOGIN] ❌ Erro no submit: {e}")
                continue
        
        # Aguardar resposta (sucesso ou falha)
        # eLaw mudou: agora mostra toast + overlay antes de redirecionar!
        await short_sleep_ms(2000)
        
        # Race: success vs failure (aumentado de 30s para 45s)
        for check_attempt in range(45):  # 45s de espera para redirect
            if await _check_login_success(page):
                log(f"[LOGIN] ✅ Login bem-sucedido! (tentativa {attempt})")
                return True
            
            if await _check_login_failure(page):
                # Capturar screenshot da falha
                try:
                    screenshot_path = f"/home/runner/workspace/rpa_screenshots/login_falha_attempt{attempt}.png"
                    await page.screenshot(path=screenshot_path)
                    log(f"[LOGIN] Screenshot salvo: {screenshot_path}")
                    send_screenshot_to_monitor(Path(screenshot_path), region="LOGIN_FALHA")
                except:
                    pass
                
                log(f"[LOGIN] ❌ Falha detectada (tentativa {attempt})")
                break
            
            await short_sleep_ms(1000)
        else:
            # Timeout sem sucesso nem falha clara
            log(f"[LOGIN] ⏱️ Timeout na verificação (tentativa {attempt})")
            if attempt < MAX_ATTEMPTS:
                wait_secs = 2 ** attempt
                log(f"[LOGIN] Aguardando {wait_secs}s antes de retry...")
                await short_sleep_ms(wait_secs * 1000)
    
    log("[LOGIN] ❌ FALHA DEFINITIVA após todas as tentativas")
    return False

async def temporarily_disable_navbar(page, ms=800):
    try:
        await page.evaluate(
            """
        (ms)=>{
          const nav = document.querySelector('nav.navbar, .navbar, .navbar-fixed-top, header');
          if (!nav) return;
          const prev = nav.style.pointerEvents;
          nav.style.pointerEvents = 'none';
          setTimeout(()=>{ try{ nav.style.pointerEvents = prev }catch(e){} }, ms);
        }
        """,
            ms,
        )
        log("[NAVBAR] pointer-events:none (temp)")
    except Exception:
        pass

async def click_hard(locator, label=""):
    try:
        await locator.scroll_into_view_if_needed(timeout=600)
        await locator.click(timeout=1200)
        log(f"[CLICK] {label} OK")
        return True
    except Exception:
        pass
    try:
        await locator.click(timeout=1200, force=True)
        log(f"[CLICK] {label} force OK")
        return True
    except Exception:
        pass
    try:
        await locator.evaluate("el => { el.click(); el.dispatchEvent(new Event('click',{bubbles:true})); }")
        log(f"[CLICK] {label} js OK")
        return True
    except Exception:
        pass
    log(f"[CLICK] {label} FAIL")
    return False

async def open_menu_processos(page):
    MAX_TRIES = 6
    for _ in range(MAX_TRIES):
        for sel in [
            "nav a:has-text('Processos')",
            "a:has-text('Processos')",
            "xpath=//a[normalize-space()='Processos']",
        ]:
            loc = page.locator(sel).first
            if await loc.count():
                await temporarily_disable_navbar(page, 800)
                if await click_hard(loc, "Processos"):
                    await short_sleep_ms(250)
                    return
        await short_sleep_ms(250)

    target = BASE_URL.rstrip("/") + "/Processo/form"
    log(f"[FALLBACK] indo direto: {target}")
    await goto_with_retries(page, target, attempts=2, nav_timeout_ms=60000)  # 60s - aumentado para produção

async def click_novo_processo(page):
    for sel in ["a[href='/Processo/form']", "a:has-text('Novo Processo')", "xpath=//a[contains(., 'Novo Processo')]"]:
        loc = page.locator(sel).first
        if await loc.count():
            await temporarily_disable_navbar(page, 800)
            if await click_hard(loc, "Novo Processo"):
                break
    else:
        target = BASE_URL.rstrip("/") + "/Processo/form"
        await goto_with_retries(page, target, attempts=2, nav_timeout_ms=60000)  # 60s - aumentado para produção

    try:
        await page.wait_for_url(re.compile(r"/Processo/form"), timeout=4000)
    except Exception:
        pass
    try:
        png = _get_screenshot_path("novo_processo_aberto.png", process_id=process_id)  # 2025-11-21: Corrigido
        await page.screenshot(path=str(png), full_page=True)
        log(f"[SHOT] novo processo: {png}")
        send_screenshot_to_monitor(png, region="NOVO_PROCESSO_ABERTO")
    except Exception:
        pass
    await ensure_zoom_100(page, "form")

# =========================
# Helpers UI (bootstrap-select & friends)
# =========================
async def _header_offset(page) -> int:
    if HEADER_OFFSET_MANUAL > 0:
        return HEADER_OFFSET_MANUAL
    try:
        h = await page.evaluate(
            """
        () => {
          const el=document.querySelector('.navbar-fixed-top, nav.navbar-fixed-top, .navbar.navbar-fixed-top');
          return el? el.offsetHeight: 0;
        }"""
        )
        return int(h or 0) + 16
    except Exception:
        return 100

async def _scroll_into_view(locator):
    try:
        await locator.scroll_into_view_if_needed(timeout=800)
    except Exception:
        pass
    try:
        off = await _header_offset(locator.page)  # type: ignore
        await locator.evaluate(
            """(el,off)=>{
          const r=el.getBoundingClientRect();
          window.scrollBy({top:r.top - off, behavior:'instant'});
          el.focus({preventScroll:true});
        }""",
            off,
        )
    except Exception:
        pass

async def robust_click(desc: str, locator, timeout_ms: int = 1800) -> bool:
    try:
        await locator.wait_for(state="attached", timeout=timeout_ms)
    except Exception as e:
        log(f"{desc}: não anexado ({e})")
        return False
    await _scroll_into_view(locator)
    for kw in ({"force": False}, {"force": True}):
        try:
            await locator.click(timeout=timeout_ms, **kw)
            log(f"{desc}: OK")
            return True
        except Exception:
            continue
    try:
        await locator.evaluate("el=>el.click()")
        log(f"{desc}: evaluate OK")
        return True
    except Exception as e:
        log(f"{desc}: falhou ({e})")
        return False

async def _open_bs_and_get_container(page, select_id: str):
    btn = page.locator(f"button.btn.dropdown-toggle[data-id='{select_id}']").first
    
    # 🔧 BATCH FIX: Aguardar que botão esteja attached E VISÍVEL (não apenas attached)
    log(f"[BS_DROPDOWN] Aguardando botão #{select_id} estar visível...")
    await btn.wait_for(state="attached", timeout=max(SHORT_TIMEOUT_MS, 2000))
    await btn.wait_for(state="visible", timeout=max(SHORT_TIMEOUT_MS, 20000))  # 20s - tolerante a eLaw lento
    log(f"[BS_DROPDOWN] Botão #{select_id} está visível, prosseguindo...")
    
    for _ in range(2):
        caret = btn.locator(".bs-caret, .filter-option").first
        target = caret if (await caret.count()) > 0 else btn
        await _scroll_into_view(target)
        await target.click()
        await short_sleep_ms(max(CLICK_AFTER_OPEN_MS, 60))
        container = btn.locator("xpath=ancestor::*[contains(@class,'bootstrap-select')][1]")
        if await container.count() > 0:
            return btn, container
    return btn, None

async def _collect_options_from_container(container) -> List[str]:
    try:
        texts = await container.evaluate(
            """
        root => Array.from(
          root.querySelectorAll('.dropdown-menu li a span.text, .dropdown-menu li a span, .dropdown-menu li a')
        ).map(el => (el.textContent||'').trim()).filter(Boolean)
        """
        )
        return [t for t in texts if not re.search(r"selecion", t, re.I)]
    except Exception:
        return []

def _clean_choices(options: List[str]) -> List[str]:
    out = []
    seen = set()
    for o in options:
        t = (o or "").strip()
        if not t:
            continue
        if re.search(r"selecion", t, re.I):
            continue
        k = norm(t)
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out

async def nudge_change_event(page, select_id: str):
    try:
        await page.evaluate(
            """(sid)=>{
          const el=document.getElementById(sid); if(!el) return;
          for (const ev of ['input','change']) { try{ el.dispatchEvent(new Event(ev,{bubbles:true})) }catch(e){} }
          try{
            const $=window.jQuery||window.$;
            if ($ && $(el).selectpicker){ $(el).selectpicker('refresh').trigger('changed.bs.select'); }
          }catch(e){}
        }""",
            select_id,
        )
    except Exception:
        pass


async def force_select_bootstrap_by_text(page, select_id: str, wanted_text: str) -> bool:
    """
    Força a seleção em um dropdown bootstrap-select usando JavaScript puro.
    Mais robusto que set_select_fuzzy_any para campos problemáticos como Estado/Comarca.
    
    1. Busca opção que melhor corresponde ao texto desejado
    2. Usa selectpicker('val') para definir o valor
    3. Dispara eventos changed.bs.select
    4. Verifica se a seleção foi efetiva
    
    Returns:
        True se seleção foi bem-sucedida, False caso contrário
    """
    try:
        log(f"[FORCE_SELECT] Tentando selecionar '{wanted_text}' em #{select_id}...")
        
        # Passo 1: Buscar todas as opções e encontrar a melhor correspondência
        result = await page.evaluate(
            """({sid, wanted})=>{
            const norm = s => (s||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase().replace(/\\s+/g,' ').trim();
            const wNorm = norm(wanted);
            
            const el = document.getElementById(sid);
            if (!el || !el.options) return {success: false, error: 'Element not found'};
            
            // Encontrar melhor match
            let bestValue = null;
            let bestText = null;
            let bestScore = 0;
            
            for (const opt of el.options) {
                const text = (opt.textContent || '').trim();
                if (!text || /selecion/i.test(text)) continue;
                
                const tNorm = norm(text);
                let score = 0;
                
                if (tNorm === wNorm) score = 100;
                else if (tNorm.includes(wNorm)) score = 90;
                else if (wNorm.includes(tNorm)) score = 85;
                else {
                    // Fuzzy: contar palavras em comum
                    const wTokens = wNorm.split(' ').filter(Boolean);
                    const tTokens = tNorm.split(' ').filter(Boolean);
                    const common = wTokens.filter(w => tTokens.some(t => t.includes(w) || w.includes(t))).length;
                    score = Math.floor((common / Math.max(wTokens.length, 1)) * 70);
                }
                
                if (score > bestScore) {
                    bestScore = score;
                    bestValue = opt.value;
                    bestText = text;
                }
            }
            
            if (!bestValue || bestScore < 40) {
                return {success: false, error: 'No matching option found', score: bestScore};
            }
            
            // Passo 2: Usar selectpicker para definir o valor
            try {
                const $ = window.jQuery || window.$;
                if ($ && $(el).selectpicker) {
                    $(el).selectpicker('val', bestValue);
                    $(el).selectpicker('refresh');
                    $(el).trigger('changed.bs.select').trigger('change');
                } else {
                    // Fallback: definir valor diretamente
                    el.value = bestValue;
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }
            } catch(e) {
                el.value = bestValue;
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }
            
            // Passo 3: Verificar se foi selecionado
            const finalValue = el.value;
            const selected = finalValue === bestValue;
            
            return {
                success: selected, 
                selectedValue: finalValue, 
                selectedText: bestText,
                wantedValue: bestValue,
                score: bestScore
            };
        }""",
            {"sid": select_id, "wanted": wanted_text},
        )
        
        if result.get("success"):
            log(f"[FORCE_SELECT] ✅ Selecionado: '{result.get('selectedText')}' (score: {result.get('score')})")
            # Aguardar eventos propagarem
            await page.wait_for_timeout(300)
            return True
        else:
            log(f"[FORCE_SELECT] ❌ Falha: {result.get('error', 'unknown')}")
            return False
            
    except Exception as e:
        log(f"[FORCE_SELECT] ❌ Erro: {e}")
        return False


async def select_estado_comarca_manual(page, cnj: str, data: dict, process_id: int = None) -> tuple:
    """
    Preenche Estado e Comarca manualmente quando o autofill do eLaw falha.
    Usa force_select_bootstrap_by_text para garantir seleção efetiva.
    
    CADEIA DE DEPENDÊNCIAS:
    Estado → libera Comarca → libera Foro
    
    Se qualquer etapa falhar, as seguintes também falham.
    
    Usa mapeamento centralizado TRT → Estado do arquivo data/trt_map.json
    
    Returns:
        (estado, comarca) - strings com valores selecionados ou vazias se falhou
    """
    from rpa_status import update_status
    from extractors.regex_utils import (
        extract_trt_from_cnj, get_estado_variantes, 
        get_estado_from_trt, get_uf_from_trt,
        disambiguate_trt_uf, get_estado_alt_from_uf
    )
    
    estado = ""
    comarca = ""
    
    # === ESTADO ===
    log(f"[MANUAL] Preenchendo Estado manualmente...")
    update_status("preenchendo_estado_manual", "Preenchendo Estado manualmente...", process_id=process_id)
    
    # Determinar estado a partir do PDF ou CNJ usando mapeamento centralizado
    estado_nome = (data.get("estado") or data.get("uf") or "").strip()
    estado_uf = ""
    estado_variantes = []
    
    # Texto do PDF para desambiguação de TRTs multi-estado
    pdf_text = data.get("pdf_text", "") or data.get("full_text", "") or ""
    
    # Extrair TRT do CNJ e usar mapeamento centralizado
    codigo_trt = ""
    if cnj:
        try:
            codigo_trt = extract_trt_from_cnj(cnj)
            if codigo_trt:
                # Para TRTs multi-estado (08, 10, 11, 14), usar desambiguação
                estado_uf = disambiguate_trt_uf(codigo_trt, pdf_text)
                
                # Obter nome do estado correto baseado na UF desambiguada
                if estado_uf:
                    estado_nome_trt = get_estado_alt_from_uf(codigo_trt, estado_uf)
                    if estado_nome_trt:
                        estado_nome = estado_nome_trt
                
                # Usar mapeamento centralizado para obter variantes
                estado_variantes = get_estado_variantes(codigo_trt)
                
                log(f"[MANUAL] Estado do CNJ (TRT-{codigo_trt}): nome='{estado_nome}', UF='{estado_uf}' (desambiguado)")
                log(f"[MANUAL] Variantes do mapeamento: {estado_variantes[:5]}...")
        except Exception as e:
            log(f"[MANUAL] Erro extraindo TRT do CNJ: {e}")
    
    # Adicionar variantes do PDF/banco se não vieram do mapeamento
    if estado_nome and estado_nome not in estado_variantes:
        estado_variantes.insert(0, estado_nome)
    if estado_uf and estado_uf not in estado_variantes:
        estado_variantes.insert(1, estado_uf)
    
    # Remover duplicatas mantendo ordem
    estado_variantes = list(dict.fromkeys(estado_variantes))
    
    log(f"[MANUAL] Variantes de estado a tentar: {estado_variantes}")
    
    # Tentar selecionar Estado com múltiplas variantes
    estado_selecionado = False
    await wait_for_select_ready(page, "EstadoId", 1, 8000)
    
    for variante in estado_variantes:
        if not variante:
            continue
            
        log(f"[MANUAL] Tentando selecionar Estado: '{variante}'")
        
        # Tentativa 1: force_select_bootstrap_by_text
        if await force_select_bootstrap_by_text(page, "EstadoId", variante):
            estado_selecionado = True
            log(f"[MANUAL] ✅ Estado selecionado (força): {variante}")
            break
        
        # Tentativa 2: set_select_fuzzy_any (fuzzy matching)
        if await set_select_fuzzy_any(page, "EstadoId", variante, fallbacks=[variante]):
            estado_selecionado = True
            log(f"[MANUAL] ✅ Estado selecionado (fuzzy): {variante}")
            break
        
        # Tentativa 3: _set_native_select_fuzzy (seleção nativa)
        if await _set_native_select_fuzzy(page, "EstadoId", variante):
            estado_selecionado = True
            log(f"[MANUAL] ✅ Estado selecionado (nativo): {variante}")
            break
    
    if not estado_selecionado:
        log(f"[MANUAL] ❌ Todas as variantes falharam para Estado")
    
    # SEMPRE reler o valor do DOM para confirmar seleção
    estado_atual = await _get_selected_text(page, "EstadoId")
    if estado_atual and estado_atual.lower() not in ["selecione", "--", "---", ""]:
        estado = estado_atual
        log(f"[MANUAL] Estado confirmado do DOM: '{estado}'")
        
        # IMPORTANTE: Aguardar AJAX carregar cidades após selecionar Estado
        log(f"[MANUAL] Aguardando cidades do estado {estado} carregarem (3s)...")
        await page.wait_for_timeout(3000)
        
        # Verificar se CidadeId tem opções agora
        await wait_for_select_ready(page, "CidadeId", 2, 20000)  # 20s - tolerante a AJAX lento
    else:
        log(f"[MANUAL] ❌ Estado não confirmado no DOM (valor atual: '{estado_atual}')")
    
    # === COMARCA ===
    if estado:  # Só preencher Comarca se Estado foi selecionado
        log(f"[MANUAL] Preenchendo Comarca manualmente...")
        update_status("preenchendo_comarca_manual", "Preenchendo Comarca manualmente...", process_id=process_id)
        
        comarca_nome = (data.get("comarca") or data.get("cidade") or data.get("foro") or "").strip()
        
        # Aguardar select estar pronto (com opções do estado)
        await wait_for_select_ready(page, "CidadeId", 2, 20000)  # 20s - tolerante a AJAX lento
        
        # Coletar opções disponíveis para fallback
        comarca_opts = []
        try:
            raw_opts = await page.evaluate("""sid => {
                const el = document.getElementById(sid);
                if (!el || !el.options) return [];
                return [...el.options].map(o => (o.textContent || '').trim()).filter(t => t && t.toLowerCase() !== 'selecione');
            }""", "CidadeId") or []
            comarca_opts = [o for o in raw_opts if o]
            log(f"[MANUAL] Comarcas disponíveis: {len(comarca_opts)} opções")
        except Exception as e:
            log(f"[MANUAL] Erro ao coletar comarcas: {e}")
        
        comarca_selecionada = False
        
        # Tentativa 1: Usar comarca do PDF/banco
        if comarca_nome:
            if await force_select_bootstrap_by_text(page, "CidadeId", comarca_nome):
                comarca = comarca_nome
                comarca_selecionada = True
                log(f"[MANUAL] ✅ Comarca selecionada (força): {comarca}")
            else:
                # Tentar fuzzy
                if await set_select_fuzzy_any(page, "CidadeId", comarca_nome, fallbacks=comarca_opts[:5] if comarca_opts else None):
                    comarca = comarca_nome
                    comarca_selecionada = True
                    log(f"[MANUAL] ✅ Comarca selecionada (fuzzy): {comarca}")
        
        # Tentativa 2: Se falhou, usar primeira comarca disponível como fallback
        if not comarca_selecionada and comarca_opts:
            primeira_comarca = comarca_opts[0]
            log(f"[MANUAL] ⚠️ Usando primeira comarca disponível como fallback: {primeira_comarca}")
            if await force_select_bootstrap_by_text(page, "CidadeId", primeira_comarca):
                comarca = primeira_comarca
                comarca_selecionada = True
                log(f"[MANUAL] ✅ Comarca fallback selecionada: {comarca}")
            else:
                # Tentar nativo
                if await _set_native_select_fuzzy(page, "CidadeId", primeira_comarca):
                    comarca = primeira_comarca
                    comarca_selecionada = True
                    log(f"[MANUAL] ✅ Comarca fallback selecionada (nativo): {comarca}")
        
        if not comarca_selecionada:
            log(f"[MANUAL] ❌ Não foi possível selecionar nenhuma comarca")
        
        # Verificar seleção de Comarca
        comarca_atual = await _get_selected_text(page, "CidadeId")
        if comarca_atual and comarca_atual.lower() not in ["selecione", "--", "---", ""]:
            comarca = comarca_atual
            log(f"[MANUAL] Comarca confirmada: {comarca}")
        
        # IMPORTANTE: Aguardar Foro (JuizadoId) carregar após selecionar Comarca
        if comarca:
            log(f"[MANUAL] Aguardando Foro carregar após comarca {comarca} (2s)...")
            await page.wait_for_timeout(2000)
            # Verificar se JuizadoId tem opções
            try:
                await wait_for_select_ready(page, "JuizadoId", 1, 8000)
                log(f"[MANUAL] ✅ Foro (JuizadoId) pronto para seleção")
            except Exception:
                log(f"[MANUAL] ⚠️ Foro pode não ter carregado completamente")
    
    # Atualizar status
    if estado and comarca:
        update_status("localizacao_ok", f"✅ {estado} - {comarca}", process_id=process_id)
    elif estado:
        update_status("localizacao_parcial", f"⚠️ Estado: {estado} (Comarca não preenchida)", process_id=process_id)
    else:
        update_status("localizacao_erro", "❌ Estado e Comarca não preenchidos", process_id=process_id)
    
    return estado, comarca

async def set_bootstrap_select_fuzzy(
    page, select_id: str, wanted_text: str, fallbacks: Optional[List[str]] = None, prefer_words: Optional[List[str]] = None
) -> bool:
    try:
        btn, container = await _open_bs_and_get_container(page, select_id)
        if not container:
            return False
        sbox = container.locator(".bs-searchbox input").first
        if await sbox.count() > 0:
            await sbox.fill("")
            key = max(sorted(tokens(wanted_text), key=len), default=wanted_text)
            await sbox.type(key, delay=TYPE_DELAY_MS)
            await short_sleep_ms(110)

        options = _clean_choices(await _collect_options_from_container(container))
        target = _best_match(options, wanted_text, prefer_words=prefer_words, threshold=10)

        if not target and fallbacks:
            for fb in fallbacks:
                cand = _best_match(options, fb, prefer_words=prefer_words, threshold=8)
                if cand:
                    target = cand
                    break

        async def click_option(txt: str) -> bool:
            return await container.evaluate(
                """(root, wanted)=>{
                const norm=s=>(s||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase().replace(/\\s+/g,' ').trim();
                const w=norm(wanted);
                const items=[...root.querySelectorAll('.dropdown-menu li a')];
                let best=null, score=-1;
                const scoreFn=(t)=>{
                  const n=norm(t); if(!n) return -1;
                  if(n===w) return 100;
                  if(n.includes(w)||w.includes(n)) return 85;
                  const wt=w.split(' ').filter(Boolean), nt=n.split(' ').filter(Boolean);
                  const inter=wt.filter(x=>nt.includes(x)).length; const uni=new Set([...wt,...nt]).size||1;
                  return Math.floor((inter/uni)*70);
                };
                for (const a of items){
                  const t=(a.querySelector('span.text')?.textContent||a.textContent||'').trim();
                  const sc=scoreFn(t);
                  if (sc>score) {best=a; score=sc;}
                }
                if (!best || score<10) return false;
                best.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));
                best.click();
                best.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
                return true;
            }""",
                target or wanted_text,
            )

        clicked = await click_option(target or wanted_text)
        await btn.press("Escape")
        if clicked:
            await nudge_change_event(page, select_id)
            return True
        return False
    except Exception:
        return False

async def _set_native_select_fuzzy(
    page, select_id: str, wanted_text: str, fallbacks: Optional[List[str]] = None, prefer_words: Optional[List[str]] = None
) -> bool:
    try:
        opts = await page.evaluate(
            """sid=>{
          const el=document.getElementById(sid); if(!el||!el.options) return [];
          return [...el.options].map(o=>({text:(o.textContent||'').trim(), value:(o.value||'').trim()}));
        }""",
            select_id,
        ) or []
    except Exception:
        opts = []
    options = [o["text"] for o in opts if o.get("text") and not re.search(r"selecion", o["text"], re.I)]
    if not options:
        return False
    target = _best_match(options, wanted_text, prefer_words=prefer_words, threshold=12)
    if not target and fallbacks:
        for fb in fallbacks:
            cand = _best_match(options, fb, prefer_words=prefer_words, threshold=8)
            if cand:
                target = cand
                break
    if not target:
        return False

    ok = await page.evaluate(
        """({sid, label})=>{
      const norm=s=>(s||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase().replace(/\\s+/g,' ').trim();
      const el=document.getElementById(sid); if(!el||!el.options) return false;
      const w=norm(label); let val='';
      for (const o of [...el.options]){
        const t=(o.textContent||'').trim(); if(!t || /selecion/i.test(t)) continue;
        const n=norm(t);
        if (n===w || n.includes(w) || w.includes(n)) { val=(o.value||'').trim(); break; }
      }
      if(!val) return false;
      try{ el.disabled=false; el.readOnly=false; }catch(e){}
      el.value=val;
      ['input','change'].forEach(ev=>{ try{ el.dispatchEvent(new Event(ev,{bubbles:true})) }catch(e){} });
      try{
        const $=window.jQuery||window.$;
        if ($ && $(el).selectpicker){ $(el).selectpicker('val', val).trigger('changed.bs.select'); }
      }catch(e){}
      return true;
    }""",
        {"sid": select_id, "label": target},
    )
    if ok:
        await nudge_change_event(page, select_id)
    return bool(ok)

async def select_from_bootstrap_dropdown(
    page: Page, select_id: str, search_text: str, field_label: str = ""
) -> bool:
    """
    Seleciona opção em dropdown Bootstrap usando fuzzy matching.
    
    Args:
        page: Página do Playwright
        select_id: ID do select (ex: SubTipoPrimeiraAudienciaId)
        search_text: Texto a ser buscado (ex: "Audiência Inicial Una (IU)")
        field_label: Label do campo para logging
    
    Returns:
        True se selecionou com sucesso, False caso contrário
    """
    label = field_label if field_label else select_id
    log(f"[DROPDOWN] Selecionando '{search_text}' em {label}")
    
    try:
        result = await set_select_fuzzy_any(page, select_id, search_text, fallbacks=None, prefer_words=None)
        if result:
            log(f"[DROPDOWN] ✓ Selecionado: {search_text}")
        else:
            log(f"[DROPDOWN] ✗ Falha ao selecionar: {search_text}")
        return result
    except Exception as e:
        log(f"[DROPDOWN][ERRO] {label}: {e}")
        return False

async def set_select_fuzzy_any(
    page, select_id: str, wanted_text: str, fallbacks: Optional[List[str]] = None, prefer_words: Optional[List[str]] = None
) -> bool:
    try:
        has_bs = await page.locator(f"button.btn.dropdown-toggle[data-id='{select_id}']").count() > 0
    except Exception:
        has_bs = False
    if has_bs:
        ok = await set_bootstrap_select_fuzzy(page, select_id, wanted_text, fallbacks, prefer_words)
        if ok:
            return True
    return await _set_native_select_fuzzy(page, select_id, wanted_text, fallbacks, prefer_words)

async def wait_for_select_ready(page, select_id: str, min_opts: int = 1, timeout_ms: int = 15000) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000.0
    log(f"[WAIT] Aguardando select #{select_id} ficar pronto (timeout: {timeout_ms/1000}s)...")
    while True:
        try:
            count = await page.evaluate(
                """sid=>{
              const el=document.getElementById(sid);
              if(!el||!el.options) return 0;
              return [...el.options].filter(o=> (o.textContent||'').trim() && !/selecion/i.test(o.textContent||'')).length;
            }""",
                select_id,
            )
            if (count or 0) >= min_opts:
                log(f"[WAIT] Select #{select_id} pronto com {count} opções")
                return True
        except Exception:
            pass
        if asyncio.get_event_loop().time() >= deadline:
            return False
        await short_sleep_ms(140)

async def _get_selected_text(page, select_id: str) -> str:
    try:
        return (
            await page.evaluate(
                """sid=>{
          const el=document.getElementById(sid); if(!el) return '';
          const opt=el.options && el.options[el.selectedIndex];
          return (opt && (opt.textContent||'').trim()) || '';
        }""",
                select_id,
            )
            or ""
        )
    except Exception:
        return ""

# =========================
# Campos por label / inputs / radios
# =========================
async def _find_control_id_by_label_contains(page, label_substr: str, prefer_selector: str = "") -> str:
    try:
        return await page.evaluate(
            """({needle, preferSel})=>{
          const norm=s=>(s||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase();
          needle=norm(needle);
          const labs=[...document.querySelectorAll('label')];
          for(const lab of labs){
            const txt=norm(lab.textContent||'');
            if(!txt || !txt.includes(needle)) continue;
            const forId=lab.getAttribute('for');
            if(forId) return forId;
            const root=lab.closest('.form-group, .input-group, .row, .col, .mb-3, .mt-2') || lab.parentElement;
            if(!root) continue;
            if(preferSel){
              const pref=root.querySelector(preferSel);
              if(pref && pref.id) return pref.id;
            }
            const sel=root.querySelector('select, input, textarea, button.btn.dropdown-toggle[data-id]');
            if(sel){
              if(sel.tagName==='BUTTON' && sel.dataset.id) return sel.dataset.id;
              if(sel.id) return sel.id;
            }
          }
          return '';
        }""",
            {"needle": label_substr, "preferSel": prefer_selector},
        )
    except Exception:
        return ""

async def set_input_by_id(page, input_id: str, value: str, label_log: str) -> bool:
    try:
        ctrl = page.locator(f"#{input_id}").first
        await ctrl.wait_for(state="attached", timeout=SHORT_TIMEOUT_MS)
    except Exception:
        log(f"[FORM][WARN] '{label_log}' ({input_id}) não encontrado")
        return False
    try:
        await _scroll_into_view(ctrl)
        await ctrl.click()
        await ctrl.fill("")
        if value:
            await ctrl.type(value, delay=TYPE_DELAY_MS)
        log(f"[FORM] {label_log}: {value}")
        return True
    except Exception as e:
        log(f"[FORM][WARN] {label_log} erro: {e}")
        return False

async def set_input_by_label_contains(page, label_substr: str, value: str, label_log: str = "") -> bool:
    sid = await _find_control_id_by_label_contains(page, label_substr, prefer_selector="input, textarea")
    if not sid:
        return False
    return await set_input_by_id(page, sid, value, label_log or label_substr)

async def set_select_by_label_contains(
    page, label_substr: str, wanted: str, fallbacks: Optional[List[str]] = None, prefer_words: Optional[List[str]] = None
) -> bool:
    sid = await _find_control_id_by_label_contains(page, label_substr, prefer_selector="select, button.btn.dropdown-toggle")
    if not sid:
        return False
    return await set_select_fuzzy_any(page, sid, wanted, fallbacks=fallbacks, prefer_words=prefer_words)

async def set_radio_by_name(page, name: str, target_value: str, human_label: str = "") -> bool:
    try:
        await page.wait_for_selector(f"input[type='radio'][name='{name}']", timeout=5000)
    except Exception:
        return False
    radios = page.locator(f"input[type='radio'][name='{name}']")
    count = await radios.count()
    for i in range(count):
        r = radios.nth(i)
        v = (await r.get_attribute("value")) or ""
        if str(v).strip() != str(target_value).strip():
            continue
        await _scroll_into_view(r)
        try:
            await r.click(force=True)
        except Exception:
            try:
                parent = r.locator("xpath=ancestor::*[contains(@class,'iradio_')][1]").first
                helper = parent.locator("ins.iCheck-helper").first
                if await helper.count() > 0:
                    await helper.click()
            except Exception:
                pass
        await short_sleep_ms(60)
        cur = await page.evaluate(
            """(nm)=>{ const el=document.querySelector("input[type='radio'][name='"+nm+"']:checked"); return el ? String(el.value||'') : ''; }""",
            name,
        )
        if str(cur) == str(target_value):
            log(f"[RADIO] {human_label or name}: '{target_value}' marcado")
            return True
    return False

YES_VALUES = {"1", "true", "True", "on", "yes", "sim", "Sim", "S", "s"}

async def set_yes_radio_guess(page, name: str) -> bool:
    try:
        await page.wait_for_selector(f"input[type='radio'][name='{name}']", timeout=1000)
    except Exception:
        return False
    radios = page.locator(f"input[type='radio'][name='{name}']")
    count = await radios.count()
    for i in range(count):
        r = radios.nth(i)
        val = (await r.get_attribute("value")) or ""
        if val in YES_VALUES:
            try:
                await _scroll_into_view(r)
                await r.click(force=True)
                await short_sleep_ms(50)
                return True
            except Exception:
                pass
    for i in range(count):
        r = radios.nth(i)
        ok = await r.evaluate(
            """(el)=>{
            const t=((el.closest('label')?.textContent||'')+(el.parentElement?.textContent||'')); 
            const n=(t||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase(); 
            if(n.includes('sim')){ el.click(); return true; } return false;
        }"""
        )
        if ok:
            await short_sleep_ms(50)
            return True
    return False

# =========================
# CNJ
# =========================
CNJ_INPUT_IDS = [
    "NumeroProcesso",
    "Numero",
    "ProtocoloInicial",
    "Protocolo",
    "NumeroCNJ",
    "NumeroProcessoCNJ",
    "NumeroDoProcesso",
    "Numero_Processo",
]
CNJ_RADIO_NAMES = ["IsJudicial", "IsCNJ", "PossuiCNJ", "HasCNJ", "Cnj", "CnjFlag", "PossuiNumeroCNJ", "NumeroCNJFlag"]

async def ensure_cnj_flag_on(page) -> bool:
    for nm in CNJ_RADIO_NAMES:
        if await page.locator(f"input[type='radio'][name='{nm}']").count() > 0:
            if await set_yes_radio_guess(page, nm):
                log(f"[CNJ] Rádio '{nm}' = Sim")
                await short_sleep_ms(60)
                return True
    try:
        ok = await page.evaluate(
            """()=>{ for (const el of document.querySelectorAll("input[type='checkbox']")){
            const txt=((el.closest('label')||{}).textContent||'')+(el.parentElement?.textContent||'');
            const n=(txt||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase();
            if(n.includes('cnj')){ if(!el.checked) el.click(); return true; }
          } return false; }"""
        )
        if ok:
            log("[CNJ] Checkbox marcado")
            return True
    except Exception:
        pass
    return False

async def wait_for_cnj_container(page, max_retries: int = 2) -> bool:
    """
    Aguarda explicitamente o campo CNJ aparecer no DOM após callback AJAX do tipo Eletrônico.
    
    DIAGNÓSTICO (2025-11-21): O campo #ProtocoloInicial só é materializado DEPOIS que
    o eLaw processa o radio Eletrônico via AJAX. Sem esse wait, set_cnj_value() falha
    com count=0 em todos os seletores.
    
    Returns:
        True se campo CNJ foi encontrado, False caso contrário.
    """
    process_id = get_current_process_id()  # ✅ Thread-safe: usa contextvar primeiro
    log(f"[CNJ][WAIT][#{process_id}] ⏳ Aguardando campo CNJ aparecer no DOM após AJAX...")
    
    # Seletores pivô - pelo menos um deve aparecer quando formulário CNJ estiver pronto
    pivot_selectors = [
        "#ProtocoloInicial",  # Seletor principal após tipo Eletrônico
        "#NumeroProcesso",
        "input[name='NumeroProcesso']",
        "input[name='ProtocoloInicial']",
    ]
    
    for attempt in range(max_retries):
        if attempt > 0:
            log(f"[CNJ][WAIT][#{process_id}] Tentativa {attempt + 1}/{max_retries} - Religando flag CNJ...")
            await ensure_cnj_flag_on(page)
            await _settle(page, f"cnj_retry_{attempt}")
        
        # Tentar encontrar qualquer seletor pivô com timeout generoso
        for selector in pivot_selectors:
            try:
                log(f"[CNJ][WAIT][#{process_id}] Tentando aguardar: {selector}")
                await page.wait_for_selector(
                    selector,
                    state="visible",
                    timeout=20000  # 20s - tolerante a AJAX lento
                )
                log(f"[CNJ][WAIT][#{process_id}] ✅ Campo CNJ encontrado: {selector}")
                await short_sleep_ms(200)  # Estabilização adicional
                return True
            except Exception:
                # Não logar erro aqui - normal tentar múltiplos seletores
                continue
        
        log(f"[CNJ][WAIT][#{process_id}] ⚠️ Nenhum campo CNJ encontrado na tentativa {attempt + 1}")
    
    log(f"[CNJ][WAIT][#{process_id}] ❌ TIMEOUT: Campo CNJ não apareceu após {max_retries} tentativas")
    return False

CNJ_FMT_RE = re.compile(r"\b\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}\b")
CNJ_DIG_RE = re.compile(r"\d{20}")

def _cnj_normalize(s: str) -> str:
    d = re.sub(r"\D", "", s or "")
    if len(d) != 20:
        return (s or "").strip()
    return f"{d[:7]}-{d[7:9]}.{d[9:13]}.{d[13]}.{d[14:16]}.{d[16:]}"

def extract_cnj_from_anywhere(data: Dict[str, Any]) -> str:
    for k in ("numero_processo", "cnj", "numero", "NumeroProcesso", "NumeroCNJ"):
        v = (data.get(k) or "").strip()
        if not v:
            continue
        if CNJ_FMT_RE.search(v) or CNJ_DIG_RE.search(re.sub(r"\D", "", v)):
            return _cnj_normalize(v)
    # Usar PDF específico do processo (evita mistura de dados)
    pdf_text = data.get("_pdf_text") or read_pdf_api_all_text()
    bag = json.dumps(data, ensure_ascii=False) + "\n" + pdf_text
    m = CNJ_FMT_RE.search(bag)
    if m:
        return _cnj_normalize(m.group(0))
    d20 = CNJ_DIG_RE.search(re.sub(r"\D", "", bag))
    if d20:
        return _cnj_normalize(d20.group(0))
    return ""

async def set_cnj_value(page, cnj: str) -> bool:
    # 🔧 2025-11-21: DEBUGGING CNJ - Logs detalhados para troubleshooting batch
    process_id = get_current_process_id()  # ✅ Thread-safe: usa contextvar primeiro
    log(f"[CNJ][set_cnj_value] ═══ PROCESSO #{process_id} ═══")
    log(f"[CNJ][DEBUG][#{process_id}] Entrada CNJ recebida: '{cnj}' (type: {type(cnj)})")
    
    wanted_digits = re.sub(r"\D", "", cnj or "")
    if not wanted_digits:
        log(f"[CNJ][FAIL][#{process_id}] ❌ CNJ vazio ou sem dígitos: '{cnj}'")
        return False
    
    if len(wanted_digits) != 20:
        log(f"[CNJ][WARN][#{process_id}] CNJ com {len(wanted_digits)} dígitos (esperado: 20): '{wanted_digits}'")

    log(f"[CNJ][DEBUG][#{process_id}] Tentando preencher CNJ: '{cnj}' (dígitos: {wanted_digits})")
    
    # 🔧 CRITICAL FIX: APENAS seletores de TEXTBOX para número do processo (NÃO incluir radios/checkboxes CNJ!)
    loc_candidates = [
        '#NumeroProcesso',  # ID principal do campo de texto
        'input[name="NumeroProcesso"]',  # Por nome
        'input[placeholder*="Número do Processo" i]',  # Por placeholder
        '#Numero',  # Fallback alternativo
        'input[name="Numero"]',  # Fallback por nome alternativo
        '#ProtocoloInicial',  # Outro nome possível para o campo
        'input[placeholder*="numero do processo" i]'  # Case variations
    ]

    for css in dict.fromkeys(loc_candidates):
        try:
            log(f"[CNJ][DEBUG][#{process_id}] Tentando seletor: {css}")
            el = page.locator(css).first
            count = await el.count()
            if count == 0:
                log(f"[CNJ][DEBUG][#{process_id}] ❌ Seletor {css} NÃO encontrado (count=0)")
                continue
            
            log(f"[CNJ][DEBUG][#{process_id}] ✅ Elemento {css} encontrado (count={count})")
            await _scroll_into_view(el)
            
            # Verificar se elemento está visível e habilitado
            is_visible = await el.is_visible()
            is_enabled = await el.is_enabled()
            log(f"[CNJ][DEBUG][#{process_id}] Estado do campo: visible={is_visible}, enabled={is_enabled}")
            
            if not is_visible:
                log(f"[CNJ][FAIL][#{process_id}] Campo {css} encontrado mas NÃO visível")
                continue
            if not is_enabled:
                log(f"[CNJ][FAIL][#{process_id}] Campo {css} encontrado mas NÃO habilitado")
                continue
            
            # Verificar valor inicial
            initial_val = (await el.input_value()) or ""
            log(f"[CNJ][DEBUG][#{process_id}] Valor inicial do campo {css}: '{initial_val}'")
            
            # Limpar campo
            try:
                await el.fill("")
                log(f"[CNJ][DEBUG][#{process_id}] Campo {css} limpo com fill('')")
            except Exception as e:
                log(f"[CNJ][WARN][#{process_id}] Erro ao limpar campo {css}: {e}")
            
            # Digitar CNJ
            try:
                log(f"[CNJ][#{process_id}] 🎯 PREENCHENDO número do processo no seletor: {css}")
                await el.type(cnj, delay=TYPE_DELAY_MS)
                log(f"[CNJ][DEBUG][#{process_id}] CNJ digitado no campo {css} com type()")
            except Exception as e:
                log(f"[CNJ][ERROR][#{process_id}] ❌ Erro ao digitar CNJ no campo {css}: {e}")
                continue
            
            # Validar resultado após type()
            val = (await el.input_value()) or ""
            val_digits = re.sub(r"\D", "", val)
            log(f"[CNJ][DEBUG][#{process_id}] Valor após type(): '{val}' (dígitos: {val_digits})")
            
            if val_digits == wanted_digits:
                # 🔧 DOUBLE CHECK: Aguardar e verificar se valor persistiu (não sumiu após AJAX)
                await short_sleep_ms(300)
                val_final = (await el.input_value()) or ""
                val_final_digits = re.sub(r"\D", "", val_final)
                if val_final_digits == wanted_digits:
                    log(f"[CNJ][SUCCESS][#{process_id}] ✅ CNJ verificado e persistido: '{val_final}'")
                    return True
                else:
                    log(f"[CNJ][FAIL][#{process_id}] ⚠️ CNJ sumiu após Double Check. Esperado: {wanted_digits}, Obtido: {val_final_digits}")
                    # Tenta próximo seletor
                    continue
            else:
                log(f"[CNJ][WARN][#{process_id}] type() NÃO persistiu corretamente. Esperado: {wanted_digits}, Obtido: {val_digits}")
            
            # Tentativa 2: forçar via JavaScript
            log(f"[CNJ][DEBUG][#{process_id}] Tentativa #2: Forçando via JavaScript...")
            ok = await page.evaluate(
                """([sel, val])=>{
                const el=document.querySelector(sel); if(!el) return false;
                try{
                  const desc = Object.getOwnPropertyDescriptor(el.__proto__, 'value') ||
                               Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
                  if (desc && desc.set) desc.set.call(el, val); else el.value = val;
                } catch(e){ el.value = val; }
                for (const ev of ['input','keyup','change','blur']) {
                  try{ el.dispatchEvent(new Event(ev,{bubbles:true})) }catch(e){}
                }
                try{
                  const $=window.jQuery||window.$;
                  if ($) { $(el).trigger('input').trigger('keyup').trigger('change'); }
                }catch(e){}
                return true;
            }""",
                [css, cnj],
            )
            log(f"[CNJ][DEBUG][#{process_id}] JavaScript execute retornou: {ok}")
            
            if not ok:
                log(f"[CNJ][FAIL][#{process_id}] JavaScript falhou ao executar para {css}")
                continue
            
            await short_sleep_ms(80)
            
            # Validar resultado após JavaScript
            val = (await el.input_value()) or ""
            val_digits = re.sub(r"\D", "", val)
            log(f"[CNJ][DEBUG][#{process_id}] Valor após JavaScript: '{val}' (dígitos: {val_digits})")
            
            if val_digits == wanted_digits:
                # 🔧 DOUBLE CHECK: Aguardar e verificar se valor persistiu (não sumiu após AJAX)
                await short_sleep_ms(300)
                val_final = (await el.input_value()) or ""
                val_final_digits = re.sub(r"\D", "", val_final)
                if val_final_digits == wanted_digits:
                    log(f"[CNJ][SUCCESS][#{process_id}] ✅ CNJ verificado e persistido via JavaScript: '{val_final}'")
                    return True
                else:
                    log(f"[CNJ][FAIL][#{process_id}] ⚠️ CNJ sumiu após Double Check JS. Esperado: {wanted_digits}, Obtido: {val_final_digits}")
                    # Tenta próximo seletor
                    continue
            else:
                log(f"[CNJ][FAIL][#{process_id}] JavaScript NÃO persistiu. Esperado: {wanted_digits}, Obtido: {val_digits}")
            
            log(f"[CNJ][FAIL][#{process_id}] Seletor {css} testado mas valor não persistiu - próximo seletor...")
        except Exception as e:
            log(f"[CNJ][ERROR][#{process_id}] Exception ao processar seletor {css}: {e}")
            import traceback
            log(f"[CNJ][ERROR][#{process_id}] Traceback: {traceback.format_exc()}")
            continue

    # Falhou em todos os seletores
    log(f"[CNJ][ERROR][#{process_id}] ❌❌❌ FALHOU EM TODOS OS {len(loc_candidates)} SELETORES para CNJ '{cnj}' ❌❌❌")
    log(f"[CNJ][ERROR][#{process_id}] Seletores testados: {list(dict.fromkeys(loc_candidates))}")
    
    # 🔧 FIX PROBLEMA 2: Salvar screenshot de CNJ no Process.elaw_screenshot_after_path
    try:
        png = _get_screenshot_path("cnj_nao_preenchido.png", process_id=process_id)
        await page.screenshot(path=str(png), full_page=True)
        log(f"[CNJ][FAIL] Screenshot salvo: {png}")
        send_screenshot_to_monitor(png, region="CNJ_ERROR")
        
        # Salvar caminho do screenshot no banco para UI exibir botão
        if process_id and flask_app:
            try:
                from models import Process, db
                with flask_app.app_context():
                    proc = Process.query.get(process_id)
                    if proc:
                        proc.elaw_screenshot_after_path = f"rpa_screenshots/{png.name}"
                        db.session.commit()
                        log(f"[SCREENSHOT CNJ] ✅ Caminho salvo no banco: {proc.elaw_screenshot_after_path}")
            except Exception as db_ex:
                log(f"[SCREENSHOT CNJ][WARN] Erro ao salvar caminho no banco: {db_ex}")
    except Exception as e:
        log(f"[CNJ][DEBUG] Erro ao capturar screenshot: {e}")
    
    return False

async def ensure_cnj_still_present(page, expected: str):
    if not expected:
        return
    locs = [f"#{i}" for i in CNJ_INPUT_IDS] + [
        "input[name='NumeroProcesso']",
        "input[placeholder*='Número do Processo' i]",
        "input[name*='cnj' i]",
    ]
    try:
        for css in locs:
            el = page.locator(css).first
            if await el.count() == 0:
                continue
            val = (await el.input_value()) or ""
            if re.sub(r"\D", "", val) == re.sub(r"\D", "", expected):
                return
        log("[CNJ] sumiu após rerender — recolocando")
        await set_cnj_value(page, expected)
    except Exception:
        pass


async def wait_for_cnj_autofill(page, timeout_ms: int = 12000, process_id: int = None) -> bool:
    """
    2025-11-27: Aguarda campos automáticos do eLaw serem preenchidos após inserir CNJ.
    
    O eLaw dispara AJAX após preencher o número do processo (CNJ) e preenche
    automaticamente campos como Estado, Comarca, Vara, Foro, etc.
    
    Args:
        page: Playwright page
        timeout_ms: Timeout máximo em milissegundos (default: 12s)
        process_id: ID do processo para logging
        
    Returns:
        bool: True se pelo menos Estado foi preenchido, False se timeout
    """
    log(f"[CNJ_AUTOFILL][#{process_id}] Aguardando campos automáticos (timeout={timeout_ms}ms)...")
    update_status("aguardando_autofill", "Aguardando eLaw preencher campos automáticos...", process_id=process_id)
    
    start_time = time.time()
    max_wait_s = timeout_ms / 1000.0
    check_interval_ms = 500
    
    estado_filled = False
    cidade_filled = False
    
    while (time.time() - start_time) < max_wait_s:
        try:
            estado_el = page.locator('#EstadoId').first
            if await estado_el.count() > 0:
                estado_val = await estado_el.input_value()
                if estado_val and estado_val.strip() and estado_val != "0":
                    estado_filled = True
                    log(f"[CNJ_AUTOFILL][#{process_id}] ✅ Estado preenchido: {estado_val}")
            
            cidade_el = page.locator('#CidadeId').first
            if await cidade_el.count() > 0:
                cidade_val = await cidade_el.input_value()
                if cidade_val and cidade_val.strip() and cidade_val != "0":
                    cidade_filled = True
                    log(f"[CNJ_AUTOFILL][#{process_id}] ✅ Cidade/Comarca preenchida: {cidade_val}")
            
            if estado_filled and cidade_filled:
                elapsed = time.time() - start_time
                log(f"[CNJ_AUTOFILL][#{process_id}] ✅ Campos automáticos preenchidos em {elapsed:.1f}s")
                update_status("autofill_ok", "✅ Campos automáticos preenchidos", process_id=process_id)
                return True
                
        except Exception as e:
            log(f"[CNJ_AUTOFILL][#{process_id}] Erro ao verificar campos: {e}")
        
        await short_sleep_ms(check_interval_ms)
    
    elapsed = time.time() - start_time
    log(f"[CNJ_AUTOFILL][#{process_id}] ⚠️ Timeout após {elapsed:.1f}s - Estado={estado_filled}, Cidade={cidade_filled}")
    
    if estado_filled:
        log(f"[CNJ_AUTOFILL][#{process_id}] Continuando apenas com Estado preenchido...")
        return True
    
    log(f"[CNJ_AUTOFILL][#{process_id}] ❌ Campos automáticos não foram preenchidos - CNJ pode não ter sido reconhecido pelo eLaw")
    return False


# =========================
# Domínio / heurísticas
# =========================
AREA_LIST = ["Administrativo", "Ambiental", "Cível", "Criminal", "Família", "Federal", "Órfãos", "Trabalhista", "Tributário"]

def resolve_sistema_eletronico(data: Dict[str, Any]) -> str:
    bag = " ".join(str(v or "") for v in data.values())
    s = norm(bag)
    if re.search(r"\b(e-?proc)\b", s):
        return "E-PROC"
    if "projudi" in s:
        return "PROJUDI"
    if "juizo 100" in s:
        return "Juízo 100% Digital - PJE"
    if "pje" in s:
        return "PJE"
    # Usar PDF específico do processo (evita mistura de dados)
    text = data.get("_pdf_text") or read_pdf_api_all_text()
    if not data.get("_pdf_text"):
        log("[resolve_sistema_eletronico][WARN] Usando PDF genérico - data['_pdf_text'] não encontrado")
    s = norm(text)
    if "e-proc" in s or "eproc" in s:
        return "E-PROC"
    if "projudi" in s:
        return "PROJUDI"
    if "juizo 100" in s:
        return "Juízo 100% Digital - PJE"
    if "pje" in s:
        return "PJE"
    return "PJE"

def resolve_area_direito(data: Dict[str, Any]) -> str:
    for k in ("area_direito", "area"):
        v = (data.get(k) or "").strip()
        if v:
            for opt in AREA_LIST:
                if norm(opt) in norm(v) or norm(v) in norm(opt):
                    return opt
    # Usar PDF específico do processo (evita mistura de dados)
    text = data.get("_pdf_text") or read_pdf_api_all_text()
    if not data.get("_pdf_text"):
        log("[resolve_area_direito][WARN] Usando PDF genérico - data['_pdf_text'] não encontrado")
    s = norm(text)
    if any(w in s for w in ["trabalh", "reclamat", "clt"]):
        return "Trabalhista"
    if any(w in s for w in ["tribut", "receita federal", "icms", "iss", "iptu"]):
        return "Tributário"
    if any(w in s for w in ["criminal", "penal", "crime"]):
        return "Criminal"
    if any(w in s for w in ["ambiental", "ibama", "licenca"]):
        return "Ambiental"
    if any(w in s for w in ["administrat", "improbidade", "licitacao"]):
        return "Administrativo"
    if any(w in s for w in ["familia", "alimentos", "guarda", "divorcio"]):
        return "Família"
    if "federal" in s:
        return "Federal"
    return "Cível"

_CNJ_R = re.compile(r"\.(\d)\.(\d{2})\.")

def _origin_from_cnj(cnj: str) -> Optional[str]:
    m = _CNJ_R.search(cnj or "")
    if not m:
        return None
    ramo, tr = m.group(1), m.group(2)
    if ramo == "5":
        return "TST" if tr == "90" else "TRT"
    return None

def resolve_origem_final(data: Dict[str, Any], area_txt: str, pdf_text: str) -> str:
    for k in ("origem", "origem_sigla"):
        v = (data.get(k) or "").strip().upper()
        if v in {
            "TRT",
            "TST",
            "TRF",
            "JF",
            "STJ",
            "STF",
            "PROCON",
            "PREFEITURA",
            "RECEITA FEDERAL",
            "ÓRGÃO ADMINISTRATIVO",
        }:
            return v
    cnj = (data.get("numero_processo") or "").strip()
    o = _origin_from_cnj(cnj)
    if o:
        return o
    s = norm((area_txt or "") + " " + (pdf_text or ""))
    if "trabalh" in s:
        if re.search(r"\btribunal superior do trabalho\b|\bno\s+tst\b|\borgao\s*:\s*tst\b", s):
            return "TST"
        return "TRT"
    if "procon" in s:
        return "PROCON"
    if "prefeitura" in s or "municip" in s:
        return "PREFEITURA"
    if "receita federal" in s or "carf" in s:
        return "RECEITA FEDERAL"
    if "stj" in s:
        return "STJ"
    if "stf" in s:
        return "STF"
    if "trf" in s or "justica federal" in s:
        return "TRF"
    if "jf" in s:
        return "JF"
    if "orgao administrativo" in s:
        return "ÓRGÃO ADMINISTRATIVO"
    return "TRT"

def _coerce_numero_orgao(v: Any, default: int = 1) -> str:
    if v is None:
        n = default
    else:
        s = str(v).strip()
        m = re.search(r"\d+", s)
        n = int(m.group(0)) if m else default
    if n < 1 or n > 99:
        n = default
    return f"{n:02d}"

# =========================
# CÉLULA mapping (docx/json + aliases)
# =========================
def _load_cell_map_from_json(json_path: str) -> Dict[str, str]:
    try:
        if json_path and os.path.exists(json_path):
            with open(json_path, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
            return {norm(k): str(v).strip() for k, v in raw.items() if str(k).strip() and str(v).strip()}
    except Exception as e:
        log(f"[CÉLULA][WARN] JSON map: {e}")
    return {}

def _load_cell_map_from_docx(paths: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if DocxDocument is None:
        return out
    for p in paths:
        p = p.strip()
        if not p or not os.path.exists(p):
            continue
        try:
            doc = DocxDocument(p)  # type: ignore
            for tb in doc.tables:
                for i, row in enumerate(tb.rows):
                    try:
                        cols = [(c.text or "").strip() for c in row.cells]
                    except Exception:
                        cols = []
                    if not cols:
                        continue
                    cel = (cols[0] if len(cols) >= 1 else "").strip()
                    cli = (cols[1] if len(cols) >= 2 else "").strip()
                    pit = (cols[2] if len(cols) >= 3 else "").strip()
                    if cel and cli:
                        out[norm(cli)] = cel.strip()
                    if cel and pit:
                        out[norm(pit)] = cel.strip()
        except Exception as e:
            log(f"[CÉLULA][WARN] DOCX '{p}': {e}")
    return out

def _canonical_cell_label(celula: str) -> str:
    n = norm(celula)
    if "gpa" in n or "pao de acucar" in n or "pão de açúcar" in n or "companhia brasileira de distribuicao" in n or "sendas" in n:
        return "Trabalhista GPA"
    if "pro" in n and "pharm" in n:
        return "Trabalhista Pro Pharma"
    if "prudential" in n:
        return "Trabalhista Prudential"
    if "casas bahia" in n or "globex" in n or "cnova" in n or "banqi" in n:
        return "Trabalhista Casas Bahia"
    if "csn" in n or "companhia siderurgica nacional" in n or "cbsi" in n or "prada" in n or "sepetiba tecon" in n or "csn mineracao" in n:
        return "Trabalhista CSN"
    if "outros" in n:
        return "Trabalhista Outros Clientes"
    return celula.strip()

def _build_brand_synonyms() -> Dict[str, str]:
    pairs = {
        "gpa": "Trabalhista GPA",
        "grupo pao de acucar": "Trabalhista GPA",
        "pão de açúcar": "Trabalhista GPA",
        "companhia brasileira de distribuicao": "Trabalhista GPA",
        "cbd": "Trabalhista GPA",
        "sendas distribuidora": "Trabalhista GPA",
        "pao de acucar": "Trabalhista GPA",
        "casas bahia": "Trabalhista Casas Bahia",
        "viavarejo": "Trabalhista Casas Bahia",
        "via varejo": "Trabalhista Casas Bahia",
        "globex": "Trabalhista Casas Bahia",
        "cnova": "Trabalhista Casas Bahia",
        "banqi": "Trabalhista Casas Bahia",
        "bartira": "Trabalhista Casas Bahia",
        "integra solucoes para varejo": "Trabalhista Casas Bahia",
        "cnt log": "Trabalhista Casas Bahia",
        "csn": "Trabalhista CSN",
        "companhia siderurgica nacional": "Trabalhista CSN",
        "cbsi": "Trabalhista CSN",
        "prada": "Trabalhista CSN",
        "sepetiba tecon": "Trabalhista CSN",
        "csn mineracao": "Trabalhista CSN",
        "csn cimentos": "Trabalhista CSN",
        "fundacao csn": "Trabalhista CSN",
        "profarma": "Trabalhista Pro Pharma",
        "d1000": "Trabalhista Pro Pharma",
        "d 1000": "Trabalhista Pro Pharma",
        "pro pharma": "Trabalhista Pro Pharma",
        "prudential": "Trabalhista Prudential",
    }
    return {norm(k): v for k, v in pairs.items()}

def _load_cell_mapping() -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    mapping.update(_load_cell_map_from_json(RPA_CELL_JSON_PATH))
    mapping.update(_load_cell_map_from_docx(RPA_CELL_DOCX_PATHS))
    for k, v in _build_brand_synonyms().items():
        mapping.setdefault(k, v)
    mapping = {k: _canonical_cell_label(v) for k, v in mapping.items()}
    return mapping

def _score_text_hit(hay: str, needle: str, base: int) -> int:
    h = norm(hay)
    n = norm(needle)
    return base if n and n in h else 0

def decide_celula_from_sources(data: Dict[str, Any], pdf_text: str, options: List[str]) -> Tuple[str, str]:
    if not options:
        opt = (data.get("celula") or "").strip()
        if opt:
            return (_canonical_cell_label(opt), f"data.celula='{opt}'")
        area = (data.get("area_direito") or "").strip()
        if "trabalh" in norm(area):
            return ("Trabalhista Outros Clientes", "sem opções; área trabalhista")
        return ((data.get("celula") or "Em Segredo").strip() or "Em Segredo", "sem opções; fallback")
    explicit = (data.get("celula") or data.get("escritorio") or "").strip()
    if explicit:
        bm = _best_match(options, _canonical_cell_label(explicit), threshold=10)
        if bm:
            return (bm, f"data.celula='{explicit}'")
    mapping = _load_cell_mapping()
    ctx_fields = [
        ("cliente", 5),
        ("parte_interessada", 5),
        ("parte_adversa", 2),
        ("empresa", 3),
        ("grupo", 5),
        ("assunto", 1),
        ("objeto", 1),
    ]
    bag = " ".join(str(v or "") for v in data.values())
    best_label = ""
    best_score = 0
    reason = ""
    for key, base in ctx_fields:
        val = str(data.get(key) or "")
        if not val:
            continue
        for alias, cell in mapping.items():
            sc = _score_text_hit(val, alias, base)
            if sc > 0:
                sc += 2 if key in {"cliente", "parte_interessada", "grupo"} else 0
                if sc > best_score:
                    best_label = cell
                    best_score = sc
                    reason = f"hit: {key}='{val}' ⇒ célula '{cell}' (alias='{alias}')"
    if not best_label:
        check = (pdf_text or "") + " " + bag
        for alias, cell in mapping.items():
            sc = _score_text_hit(check, alias, 2)
            if sc > best_score:
                best_label = cell
                best_score = sc
                reason = f"hit: PDF/bag contém '{alias}' ⇒ célula '{cell}'"
    if best_label:
        match = _best_match(options, best_label, threshold=8) or _best_match(options, _canonical_cell_label(best_label), threshold=8)
        if match:
            return (match, reason or f"map '{best_label}'")
    alias_rows = load_alias_rows("config/client_aliases.json")
    alias_index = build_alias_index(alias_rows)
    pdf_pick = guess_cell_from_pdf_text(pdf_text or "", alias_index, options)
    if pdf_pick:
        return (pdf_pick, "utils.aliases por PDF")
    area = resolve_area_direito(data)
    if "trabalh" in norm(area):
        bm = _best_match(options, "Trabalhista Outros Clientes", threshold=1)
        if bm:
            return (bm, "fallback trabalhista")
    non_brand = [o for o in options if not any(k in norm(o) for k in ["gpa", "csn", "bahia", "pro pharma", "prudential"])]
    if non_brand:
        return (non_brand[0], "fallback non-brand")
    return (options[0], "fallback first")

# =========================
# “Tipo de processo” — Eletrônico/Físico
# =========================
RADIO_VIRTUAL_NAMES = ["IsProcessoVirtual", "ProcessoVirtual", "IsVirtual", "ProcessoEletronico", "IsEletronico"]

async def set_tipo_processo_virtual(page, want_virtual: bool = True) -> bool:
    try:
        want_label = "eletronico" if want_virtual else "fisico"
        # 1) Por label
        try:
            label_text = "Eletrônico" if want_virtual else "Físico"
            lab = page.locator(f"label:has-text('{label_text}')").first
            if await lab.count():
                await _scroll_into_view(lab)
                try:
                    await lab.click(force=True)
                except Exception:
                    pass
                changed = await page.evaluate(
                    """(labelTextNorm)=>{
                    const norm=(s)=> (s||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase();
                    const labs=[...document.querySelectorAll('label')];
                    for(const l of labs){
                        if(norm(l.textContent||'').includes(labelTextNorm)){
                            const input = l.control || l.querySelector('input[type="radio"]');
                            if(input){
                                try{
                                    input.checked = true;
                                    for(const ev of ['click','input','change']){
                                        input.dispatchEvent(new Event(ev,{bubbles:true}));
                                    }
                                    return true;
                                }catch(e){}
                            }
                        }
                    }
                    return false;
                }""",
                    want_label,
                )
                if changed:
                    await wait_network_quiet(page, timeout_ms=800)
                    return True
        except Exception:
            pass

        # 2) Por name/value
        truthy_vals = ["1", "true", "on", "eletronico", "eletrônico", "e", "sim"]
        falsy_vals = ["0", "false", "off", "fisico", "físico", "f", "nao", "não"]
        wanted_vals = truthy_vals if want_virtual else falsy_vals
        names_try = RADIO_VIRTUAL_NAMES + ["TipoProcesso", "Processo", "Tipo", "tipo_processo"]

        for nm in names_try:
            try:
                radios = page.locator(f"input[type='radio'][name='{nm}']")
                cnt = await radios.count()
                if cnt == 0:
                    continue
                chosen = None
                for i in range(cnt):
                    r = radios.nth(i)
                    v = (await r.get_attribute("value")) or ""
                    if norm(v) in [norm(x) for x in wanted_vals]:
                        chosen = r
                        break
                if not chosen:
                    for i in range(cnt):
                        r = radios.nth(i)
                        lab_text = await r.evaluate(
                            """(el)=>{
                            const lab = el.id ? document.querySelector(`label[for="${el.id}"]`) : (el.closest('label') || el.parentElement?.querySelector('label'));
                            return (lab && (lab.textContent||'').trim()) || '';
                        }"""
                        )
                        if norm(lab_text).find(want_label) >= 0:
                            chosen = r
                            break
                if chosen:
                    await _scroll_into_view(chosen)
                    
                    # 🔧 BATCH FIX: Se já estiver marcado, forçar toggle via CLICK para disparar AJAX do CNJ
                    is_checked = await chosen.is_checked()
                    if is_checked and want_virtual:
                        # Se queremos Eletrônico e JÁ ESTÁ Eletrônico, o AJAX pode não ter rodado se a página foi cacheada.
                        # Tática: Clicar em Físico via DOM interaction e voltar para Eletrônico para forçar eventos change/input
                        log("[RADIO] Já está Eletrônico - Forçando toggle via CLICK para disparar AJAX do CNJ...")
                        try:
                            # Buscar qualquer outro rádio do grupo (não-checked) e clicar
                            other_radio = await page.evaluate(
                                """(name, currentValue) => {
                                    const radios = document.querySelectorAll(`input[type='radio'][name='${name}']`);
                                    for (const r of radios) {
                                        if (r.value !== currentValue && !r.checked) {
                                            r.click();  // Dispara evento change no rádio perdedor
                                            return true;
                                        }
                                    }
                                    return false;
                                }""",
                                nm,
                                await chosen.get_attribute("value") or ""
                            )
                            if other_radio:
                                log("[RADIO] Clicou em outro rádio para desmarcar Eletrônico")
                                # Aguardar AJAX do Físico completar antes de voltar ao Eletrônico
                                await wait_network_quiet(page, timeout_ms=800)
                                await short_sleep_ms(150)
                        except Exception as e:
                            log(f"[RADIO][WARN] Erro ao clicar em outro rádio: {e}")
                    
                    # Clicar no rádio desejado (Eletrônico) - sempre dispara eventos
                    try:
                        await chosen.click(force=True)
                    except Exception:
                        try:
                            parent = chosen.locator("xpath=ancestor::*[contains(@class,'iradio_')][1]").first
                            helper = parent.locator("ins.iCheck-helper").first
                            if await helper.count() > 0:
                                await helper.click()
                        except Exception:
                            pass
                    try:
                        await page.evaluate(
                            """(nm)=>{
                            const el=document.querySelector(`input[type='radio'][name='${nm}']:checked`);
                            if(el){
                                for(const ev of ['click','input','change']){
                                    try{ el.dispatchEvent(new Event(ev,{bubbles:true})) }catch(e){}
                                }
                            }
                        }""",
                            nm,
                        )
                    except Exception:
                        pass
                    await wait_network_quiet(page, timeout_ms=700)
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False

# =========================
# Campos Trabalhistas (Data, Texto)
# =========================
async def set_text_field_by_id(page, field_id: str, value: str, field_name: str) -> bool:
    """
    Preenche um campo de texto simples por ID.
    """
    if not value:
        return False
    
    try:
        selector = f"#{field_id}"
        el = page.locator(selector).first
        
        if await el.count() == 0:
            log(f"[{field_name}][WARN] Campo #{field_id} não encontrado")
            return False
        
        await _scroll_into_view(el)
        
        # Remover readonly se existir
        try:
            await el.evaluate("e=>{ e.removeAttribute('readonly'); e.disabled=false; }")
        except Exception:
            pass
        
        # Limpar e preencher campo
        await el.click()
        await el.press("Control+A")
        await el.press("Backspace")
        await el.type(str(value), delay=TYPE_DELAY_MS)
        
        # Disparar eventos
        await el.evaluate("""el => {
            ['input', 'change', 'blur'].forEach(ev => {
                try { el.dispatchEvent(new Event(ev, {bubbles: true})); } catch(e) {}
            });
        }""")
        
        log(f"[{field_name}] ✅ Preenchido: {value}")
        return True
        
    except Exception as e:
        log(f"[{field_name}][WARN] Erro ao preencher: {e}")
        return False

async def set_date_field_by_id(page, field_id: str, date_value: str, field_name: str) -> bool:
    """
    Preenche um campo de data (formato DD/MM/YYYY).
    """
    if not date_value:
        return False
    
    try:
        selector = f"#{field_id}"
        el = page.locator(selector).first
        
        if await el.count() == 0:
            log(f"[{field_name}][WARN] Campo #{field_id} não encontrado")
            return False
        
        await _scroll_into_view(el)
        
        # Remover readonly se existir
        try:
            await el.evaluate("e=>{ e.removeAttribute('readonly'); e.disabled=false; }")
        except Exception:
            pass
        
        # Limpar e preencher campo
        await el.click()
        await el.press("Control+A")
        await el.press("Backspace")
        
        # Preencher data (formato DD/MM/YYYY)
        await el.type(str(date_value), delay=TYPE_DELAY_MS)
        
        # Disparar eventos para datepicker
        await el.evaluate("""el => {
            ['input', 'change', 'blur', 'keyup'].forEach(ev => {
                try { el.dispatchEvent(new Event(ev, {bubbles: true})); } catch(e) {}
            });
        }""")
        
        log(f"[{field_name}] ✅ Preenchido: {date_value}")
        return True
        
    except Exception as e:
        log(f"[{field_name}][WARN] Erro ao preencher: {e}")
        return False

# =========================
# Valor da Causa
# =========================
def _money_variants(raw: str) -> List[str]:
    s = (raw or "").strip()
    s = re.sub(r"[^\d,\.]", "", s)
    if not s:
        return []
    digits = re.sub(r"[^\d]", "", s)
    v = []
    if "," in s:
        v.append(s)
    if "." in s:
        v.append(s)
    if len(digits) >= 3:
        v.append(digits[:-2] + "," + digits[-2:])
        v.append(digits[:-2] + "." + digits[-2:])
    v.append(digits)
    out = []
    seen = set()
    for x in v:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out

async def _type_text_slow(el, text: str, delay: int):
    try:
        await el.click()
        try:
            await el.press("Control+A")
        except Exception:
            pass
        try:
            await el.press("Backspace")
        except Exception:
            pass
        if text:
            await el.type(text, delay=delay)
    except Exception:
        pass

async def set_valor_causa_any(page, valor: str) -> bool:
    if not valor:
        return False
    candidatos = [
        "#ValorCausa",
        "input#ValorCausa",
        "input[name*='Valor'][name*='Causa' i]",
        "input[id*='Valor'][id*='Causa' i]",
        "input[placeholder*='Valor da Causa' i]",
        "input.money",
        "input.currency",
        "input[class*='valor' i]",
    ]
    variants = _money_variants(valor) or [re.sub(r"[^\d]", "", valor or "")]
    for css in dict.fromkeys(candidatos):
        try:
            el = page.locator(css).first
            if await el.count() == 0:
                continue
            await _scroll_into_view(el)
            try:
                await el.evaluate("e=>{ e.removeAttribute('readonly'); e.disabled=false; }")
            except Exception:
                pass

            plugin_ok = await page.evaluate(
                """(sel, vs)=>{
              const el=document.querySelector(sel); if(!el) return false;
              const trySet=(val)=>{
                try{
                  const $=window.jQuery||window.$;
                  if ($ && ($(el).data && ($(el).data('autoNumeric') || $(el).autoNumeric))){
                    try{ $(el).autoNumeric('set', val); return true; }catch(e){}
                  }
                }catch(e){}
                try{ if(el.inputmask && el.inputmask.setValue){ el.inputmask.setValue(val); return true; } }catch(e){}
                try{
                  const w = (window.kendo||{}).ui && (window.jQuery||window.$) && ( (window.jQuery||window.$)(el).data && (window.jQuery||window.$)(el).data('kendoNumericTextBox') );
                  if (w){ w.value(parseFloat(String(val).replace('.','').replace(',','.'))); w.trigger('change'); return true; }
                }catch(e){}
                return false;
              };
              for(const v of vs){
                if(trySet(v)){
                  for(const ev of ['input','keyup','change','blur']){ try{ el.dispatchEvent(new Event(ev,{bubbles:true})) }catch(e){} }
                  return true;
                }
              }
              return false;
            }""",
                css,
                variants,
            )
            if plugin_ok:
                log(f"[FORM] Valor da Causa (plugin): {variants[0]} ({css})")
                return True

            for v in variants + [re.sub(r"[^\d]", "", variants[0] if variants else valor)]:
                await _type_text_slow(el, v, TYPE_DELAY_MS)
                await short_sleep_ms(90)
                val = (await el.input_value()) or ""
                if re.search(r"\d", val):
                    log(f"[FORM] Valor da Causa: {val} ({css})")
                    return True
        except Exception:
            continue
    ok = await set_input_by_label_contains(page, "valor da causa", variants[0] if variants else valor, "Valor da Causa")
    if ok:
        return True
    log("[FORM][WARN] Valor da Causa não encontrado")
    return False

# =========================
# Picks inteligentes
# =========================
def pick_tipo_acao_smart(opts, data, pdf_text, assunto_sel):
    """
    Decide o 'Tipo de Ação' (select id=TipoAcaoId) a partir do PDF e metadados.

    Regras principais:
      - Em contexto TRABALHISTA, NUNCA escolher itens "constitucional".
      - Prioriza âncoras de rito extraídas do PDF:
          • "Classe judicial: Ação Trabalhista - Rito Ordinário/Sumaríssimo"
          • "UNA SUMARÍSSIMO"
          • "rito ordinário/sumaríssimo"
      - Cautelar só quando detectado E permitido (RPA_DISALLOW_CAUTELAR == False),
        e apenas se o rótulo contiver 'cautelar/liminar/tutela'.
      - Sumaríssimo apenas quando detectado com clareza.
      - Ordinário → preferir "(100% DIGITAL - PJE)" e depois "(PJE)".
      - Fallback seguro favorecendo opções com 'trabalh' no nome.
    """
    if not opts:
        return None

    # -----------------------------
    # Contexto consolidado
    bag = " ".join(str(data.get(k) or "") for k in ("assunto", "sub_area_direito", "objeto", "classe", "tipo_acao"))
    ctx = norm(" ".join([assunto_sel or "", bag, pdf_text or ""]))

    def has(*terms: str) -> bool:
        return any(norm(t) in ctx for t in terms)

    # -----------------------------
    # Âncoras de rito diretamente do PDF (fortes)
    RE_CLASSE = re.compile(
        r"classe\s+judicial\s*:\s*a[cç][aã]o\s+trabalhista\s*-\s*rito\s*(ordin[áa]rio|sumar[íi]ssimo)",
        re.I
    )
    RE_UNA_SUM = re.compile(r"\buna\b[^\n]{0,60}sumar[íi]ssim", re.I)
    RE_RITO_TXT = re.compile(r"rito\s+(ordin[áa]rio|sumar[íi]ssimo)", re.I)

    def _scan_rito_anchor(txt: str) -> str | None:
        s = txt or ""
        m = RE_CLASSE.search(s)
        if m:
            hit = m.group(1)
            return "sumaríssimo" if "sumar" in norm(hit) else "ordinário"
        if RE_UNA_SUM.search(s):
            return "sumaríssimo"
        m = RE_RITO_TXT.search(s)
        if m:
            hit = m.group(1)
            return "sumaríssimo" if "sumar" in norm(hit) else "ordinário"
        return None

    hint = _scan_rito_anchor(pdf_text or "")

    # -----------------------------
    # Sinais de alto nível
    is_trabalhista = ("trabalh" in ctx) or has(
        "reclamação trabalhista", "reclamatoria trabalhista", "ação trabalhista", "acao trabalhista"
    )
    is_reclamacao = has("reclamação", "reclamacao", "reclamatória", "reclamatoria") or is_trabalhista

    # Se houver "hint" do PDF, ele manda. Sem hint, caímos nas heurísticas.
    is_sumarissimo = (hint == "sumaríssimo") or has("sumaríssimo", "sumarissimo", "rito sumaríssimo", "rito sumarissimo")
    is_ordinario   = (hint == "ordinário") or (not is_sumarissimo and has("rito ordinário", "rito ordinario", "procedimento comum", "ordinário", "ordinario"))

    # Detecção de cautelar APENAS no cabeçalho/título (primeiros 2000 caracteres),
    # NÃO no corpo inteiro do texto (para evitar falsos positivos de menções nos pedidos)
    pdf_header = (pdf_text or "")[:2000]
    pdf_header_norm = norm(pdf_header)
    is_cautelar_in_header = any(t in pdf_header_norm for t in [
        "pedido cautelar", "cautelar", 
        "tutela de urgencia", "tutela de urgência",
        "liminar", "tutela antecipada",
        "mandado de segurança com", "mandado de seguranca com"
    ])
    
    # PRIORIDADE ABSOLUTA: Se o PDF declarou explicitamente o rito (ordinário/sumaríssimo)
    # no cabeçalho (classe judicial/ação trabalhista), isso SOBREPÕE qualquer detecção de cautelar.
    # Exemplo: PDF com "Ação Trabalhista - Rito Ordinário" + menção a "liminar" nos pedidos
    # → deve ser "Reclamação Trabalhista (Ordinário)", NÃO "Reclamação com Pedido Cautelar"
    if hint in ("ordinário", "sumaríssimo"):
        is_cautelar = False
        log(f"[TipoAção] ⭐ Rito explícito '{hint}' detectado no PDF → cautelar=False (prioridade)")
    else:
        is_cautelar = is_cautelar_in_header
        if is_cautelar:
            log(f"[TipoAção] ⚠️ Cautelar detectado no cabeçalho (sem rito explícito)")
        else:
            log(f"[TipoAção] Sem cautelar detectado")

    # Respeita ENV p/ cautelar
    try:
        disallow_caut = bool(RPA_DISALLOW_CAUTELAR)  # type: ignore[name-defined]
    except Exception:
        disallow_caut = True
    if disallow_caut:
        is_cautelar = False

    # -----------------------------
    # Poda inicial dos candidatos
    cand = list(opts)

    # Em TRABALHISTA: nunca pegar "constitucional"
    if is_trabalhista:
        _cand = [o for o in cand if "constitucional" not in norm(o)]
        cand = _cand or cand

    # Se o PDF disse "ordinário", removemos sumaríssimo E cautelar para reduzir ruído
    if hint == "ordinário":
        _cand = [o for o in cand if "sumar" not in norm(o)]
        cand = _cand or cand
        # REMOVE cautelar quando há rito ordinário explícito
        _cand = [o for o in cand if not any(k in norm(o) for k in ("cautelar", "liminar", "tutela"))]
        if _cand:
            cand = _cand
            log(f"[TipoAção] Filtrado cautelar devido ao rito ordinário explícito → {len(cand)} opções restantes")
    # Se o PDF disse "sumaríssimo", removemos cautelar também
    elif hint == "sumaríssimo":
        _cand = [o for o in cand if not any(k in norm(o) for k in ("cautelar", "liminar", "tutela"))]
        if _cand:
            cand = _cand
            log(f"[TipoAção] Filtrado cautelar devido ao rito sumaríssimo explícito → {len(cand)} opções restantes")
    # Se não houve hint e não detectamos sumaríssimo, também removemos sumaríssimo
    elif hint is None and not is_sumarissimo:
        _cand = [o for o in cand if "sumar" not in norm(o)]
        cand = _cand or cand

    # -----------------------------
    # 1) Regra principal: Reclamação Trabalhista
    if is_reclamacao:
        # 1.a) Cautelar? (somente se permitido e rótulo contiver a palavra)
        if is_cautelar:
            cand_caut = [o for o in cand if any(k in norm(o) for k in ("cautelar", "liminar", "tutela"))]
            if cand_caut:
                m = (
                    _best_match(cand_caut, "Reclamação com Pedido Cautelar",
                                prefer_words=["reclama", "trabalh", "cautelar"], threshold=20)
                    or _best_match(cand_caut, "Ação Cautelar",
                                   prefer_words=["cautelar"], threshold=20)
                )
                if m:
                    log(f"[TipoAção] ✅ ESCOLHIDO (cautelar): {m}")
                    return m

        # 1.b) Sumaríssimo?
        if is_sumarissimo:
            for p in (
                "Reclamação Trabalhista Procedimento Sumaríssimo (PJE)",
                "Reclamação Trabalhista Rito Sumaríssimo (100% Digital - PJE)",
                "RECLAMAÇÃO TRABALHISTA PROCEDIMENTO SUMARÍSSIMO (PJE)",
            ):
                m = _best_match(cand, p, prefer_words=["reclama", "trabalh", "sumar"], threshold=12)
                if m:
                    log(f"[TipoAção] ✅ ESCOLHIDO (sumaríssimo): {m}")
                    return m

        # 1.c) Ordinário / padrão → preferir (100% DIGITAL - PJE) e depois (PJE)
        if is_ordinario or not is_sumarissimo:
            for p in (
                "RECLAMAÇÃO TRABALHISTA (100% DIGITAL - PJE)",
                "Reclamação Trabalhista (100% DIGITAL - PJE)",
                "RECLAMAÇÃO TRABALHISTA (PJE)",
                "Reclamação Trabalhista (PJE)",
                "Reclamação Trabalhista",
                "Reclamatória Trabalhista",
            ):
                m = _best_match(cand, p, prefer_words=["reclama", "trabalh", "pje", "digital"], threshold=10)
                if m:
                    log(f"[TipoAção] ✅ ESCOLHIDO (ordinário): {m}")
                    return m

    # -----------------------------
    # 2) Outra classe comum: Consignação em Pagamento
    if has("consignação", "consignacao"):
        for p in ("Ação de Consignação em Pagamento (100% PJE)", "AÇÃO DE CONSIGNAÇÃO EM PAGAMENTO"):
            m = _best_match(cand, p, prefer_words=["consign"], threshold=12)
            if m:
                dlog(f"[TipoAção] escolhido (consignação): {m}")
                return m

    # -----------------------------
    # 3) Tentativa por metadados vindos do sistema (classe/tipo/assunto)
    for k in ("tipo_acao", "classe", "assunto"):
        v = (data.get(k) or "").strip()
        if v:
            m = _best_match(cand, v, threshold=12)
            if m:
                dlog(f"[TipoAção] escolhido (metadata:{k}): {m}")
                return m

    # -----------------------------
    # 4) Fallbacks
    if is_trabalhista:
        labor = [o for o in cand if "trabalh" in norm(o)]
        if labor:
            m = (
                _best_match(labor, "Reclamação Trabalhista (PJE)", prefer_words=["reclama", "trabalh", "pje"], threshold=5)
                or labor[0]
            )
            dlog(f"[TipoAção] fallback trabalhista: {m}")
            return m

    # Evita cair em cautelar/tutela por engano no fallback
    plain = [o for o in cand if not any(k in norm(o) for k in ("cautelar", "liminar", "tutela"))] if disallow_caut else cand
    choice = plain[0] if plain else (cand[0] if cand else None)
    dlog(f"[TipoAção] fallback: {choice}")
    return choice



def pick_objeto_smart(opts: List[str], data: Dict[str, Any], pdf_text: str, assunto_sel: str, tipo_sel: str) -> Optional[str]:
    if not opts:
        return None
    prefer = []
    for k in ("objeto", "classe", "classe_processual", "tema", "assunto", "sub_assunto", "tipo_acao"):
        v = (data.get(k) or "").strip()
        if v:
            prefer.append(v)
    if assunto_sel:
        prefer.append(assunto_sel)
    if tipo_sel:
        prefer.append(tipo_sel)
    prefer += [
        "Reclamação Trabalhista",
        "Ação Trabalhista",
        "Verbas Rescisórias",
        "Horas Extras",
        "Danos Morais",
        "Rescisão Indireta",
        "Diferenças Salariais",
        "FGTS",
    ]
    if "cautelar" in norm(pdf_text or ""):
        prefer = ["Ação Cautelar"] + prefer
    for p in prefer:
        m = _best_match(opts, p, threshold=12)
        if m:
            return m
    return opts[0]

# =========================
# Dados de entrada
# =========================
def _http_get_json(url: str, timeout: int = 8) -> dict:
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 200:
            return r.json() or {}
    except Exception:
        pass
    return {}

def load_process_data_for_fill(process_id: Optional[int] = None) -> Dict[str, Any]:
    jur_api = os.getenv("JURIDICO_LAST_PROCESS_ENDPOINT", "").strip()
    if jur_api:
        d = _http_get_json(jur_api)
        if d and (d.get("numero_processo") or d.get("tipo_processo") or d.get("sistema_eletronico")):
            log("[DATA] Sistema Jurídico (endpoint) OK")
            return d

    try:
        from app import create_app  # type: ignore
        from models import db, Process  # type: ignore
        
        app = create_app()
        with app.app_context():  # type: ignore
            if process_id:
                p = Process.query.get(process_id)
                if p:
                    data = {k: getattr(p, k, "") or "" for k in [
                        "numero_processo", "tipo_processo", "sistema_eletronico", "numero_processo_antigo",
                        "area_direito", "sub_area_direito", "origem", "orgao", "numero_orgao",
                        "comarca", "estado", "assunto", "objeto", "celula",
                        "cliente", "parte_interessada", "parte_adversa_nome", "empresa", "grupo",
                        "posicao_parte_interessada", "parte_adversa_tipo", "valor_causa", "estrategia",
                        "escritorio_parte_adversa", "cpf_cnpj_parte_adversa", "telefone_parte_adversa",
                        "email_parte_adversa", "endereco_parte_adversa", "cnj", "pdf_filename",
                        "audiencia_inicial", "cadastrar_primeira_audiencia",
                        "link_audiencia", "subtipo_audiencia", "envolvido_audiencia",
                        "data_distribuicao", "data_admissao", "data_demissao", "motivo_demissao", "salario", 
                        "cargo_funcao", "cargo", "empregador", "local_trabalho", "pis", "ctps",
                        "outra_reclamada_cliente"  # ✅ Adicionado para reclamadas extras do banco
                    ]}
                    
                    # MÚLTIPLAS RECLAMADAS: Extrair do PDF se existir
                    pdf_filename = data.get("pdf_filename", "")
                    pdf_path = None
                    if pdf_filename:
                        # Tentar caminho completo primeiro (batch: uploads/batch/68/arquivo.pdf)
                        if Path(pdf_filename).exists():
                            pdf_path = Path(pdf_filename)
                        # Fallback: tentar dentro do UPLOADS_DIR
                        elif UPLOADS_DIR.exists():
                            pdf_path = UPLOADS_DIR / pdf_filename
                            if not pdf_path.exists():
                                # Tentar apenas o nome do arquivo no UPLOADS_DIR
                                pdf_path = UPLOADS_DIR / Path(pdf_filename).name
                                if not pdf_path.exists():
                                    pdf_path = None
                    
                    if pdf_path and pdf_path.exists():
                        try:
                            from extractors.regex_utils import extract_todas_reclamadas
                            from PyPDF2 import PdfReader
                            reader = PdfReader(str(pdf_path))
                            pdf_text = "\n".join([pg.extract_text() or "" for pg in reader.pages])
                            reclamadas = extract_todas_reclamadas(pdf_text)
                            data["reclamadas"] = reclamadas
                            log(f"[DATA] Reclamadas extraídas do PDF ({pdf_path}): {len(reclamadas)}")
                        except Exception as e:
                            log(f"[DATA][WARN] Erro ao extrair reclamadas: {e}")
                            data["reclamadas"] = []
                    else:
                        log(f"[DATA][WARN] PDF não encontrado: {pdf_filename}")
                        data["reclamadas"] = []
                    
                    # 🔧 2025-11-27: FALLBACK - Se não extraiu do PDF, usar outra_reclamada_cliente do banco
                    if len(data.get("reclamadas", [])) <= 1:
                        outra_reclamada = (data.get("outra_reclamada_cliente") or "").strip()
                        if outra_reclamada:
                            log(f"[DATA][RECLAMADAS] Usando outra_reclamada_cliente do banco: {outra_reclamada}")
                            # Criar lista de reclamadas com parte_adversa + outra_reclamada
                            reclamadas_from_db = []
                            
                            # Primeira reclamada = parte_adversa_nome (a principal)
                            parte_adversa = (data.get("parte_adversa_nome") or "").strip()
                            
                            # 🔧 2025-11-27: DEDUPLICAÇÃO - Verificar se outra_reclamada é diferente de parte_adversa
                            # Normalizar para comparação (ignorar case, espaços extras)
                            parte_normalizada = parte_adversa.upper().strip() if parte_adversa else ""
                            outra_normalizada = outra_reclamada.upper().strip()
                            
                            if parte_adversa:
                                reclamadas_from_db.append({
                                    "nome": parte_adversa,
                                    "posicao": "RECLAMADO",
                                    "tipo_pessoa": "juridica" if any(x in parte_adversa.upper() for x in ["LTDA", "S/A", "S.A.", "EIRELI", "ME", "EPP", "CIA", "COMPANHIA", "INDUSTRIA", "COMERCIO"]) else "fisica"
                                })
                            
                            # Segunda reclamada = outra_reclamada_cliente (somente se não for duplicata)
                            if outra_normalizada and outra_normalizada != parte_normalizada:
                                reclamadas_from_db.append({
                                    "nome": outra_reclamada,
                                    "posicao": "RECLAMADO",
                                    "tipo_pessoa": "juridica" if any(x in outra_reclamada.upper() for x in ["LTDA", "S/A", "S.A.", "EIRELI", "ME", "EPP", "CIA", "COMPANHIA", "INDUSTRIA", "COMERCIO"]) else "fisica"
                                })
                                log(f"[DATA][RECLAMADAS] ✅ Adicionada outra_reclamada: {outra_reclamada}")
                            elif outra_normalizada == parte_normalizada:
                                log(f"[DATA][RECLAMADAS] ⚠️ outra_reclamada é igual a parte_adversa - ignorando duplicata")
                            
                            data["reclamadas"] = reclamadas_from_db
                            log(f"[DATA][RECLAMADAS] Lista construída do banco: {len(reclamadas_from_db)} reclamadas")
                    
                    # PEDIDOS: Carregar do campo pedidos_json
                    pedidos_json = getattr(p, 'pedidos_json', None) or ""
                    if pedidos_json:
                        try:
                            import json
                            data["pedidos"] = json.loads(pedidos_json)
                            log(f"[DATA] Pedidos carregados do banco: {len(data['pedidos'])}")
                        except Exception as e:
                            log(f"[DATA][WARN] Erro ao carregar pedidos: {e}")
                            data["pedidos"] = []
                    else:
                        data["pedidos"] = []
                    
                    # 2025-12-02: Log expandido para campos trabalhistas
                    log(f"[DATA] DB (id={process_id}) OK - CNJ: {data.get('cnj', 'N/A')}, Parte Adversa: {data.get('parte_adversa_nome', 'N/A')}")
                    log(f"[DATA] DB (id={process_id}) Trabalhista: cargo={data.get('cargo_funcao') or data.get('cargo') or 'N/A'}, pis={data.get('pis', 'N/A')}, ctps={data.get('ctps', 'N/A')}")
                    log(f"[DATA] DB (id={process_id}) PDF: {data.get('pdf_filename', 'N/A')}, Reclamadas: {len(data.get('reclamadas', []))}, Pedidos: {len(data.get('pedidos', []))}")
                    return data
            p = (
                Process.query.filter(Process.numero_processo.isnot(None), Process.numero_processo != "")
                .order_by(Process.updated_at.desc(), Process.id.desc())
                .first()
            )
            if p:
                data = {k: getattr(p, k, "") or "" for k in [
                    "numero_processo", "tipo_processo", "sistema_eletronico", "numero_processo_antigo",
                    "area_direito", "sub_area_direito", "origem", "orgao", "numero_orgao",
                    "comarca", "estado", "assunto", "objeto", "celula",
                    "cliente", "parte_interessada", "parte_adversa_nome", "empresa", "grupo",
                    "posicao_parte_interessada", "parte_adversa_tipo", "valor_causa", "estrategia",
                    "escritorio_parte_adversa", "cpf_cnpj_parte_adversa", "telefone_parte_adversa",
                    "email_parte_adversa", "endereco_parte_adversa", "cnj", "pdf_filename",
                    "audiencia_inicial", "cadastrar_primeira_audiencia",
                    "link_audiencia", "subtipo_audiencia", "envolvido_audiencia",
                    "data_distribuicao", "data_admissao", "data_demissao", "motivo_demissao", "salario", 
                    "cargo_funcao", "cargo", "empregador", "local_trabalho", "pis", "ctps"
                ]}
                
                # MÚLTIPLAS RECLAMADAS: Extrair do PDF se existir
                pdf_filename = data.get("pdf_filename", "")
                pdf_path = None
                if pdf_filename:
                    # Tentar caminho completo primeiro (batch: uploads/batch/68/arquivo.pdf)
                    if Path(pdf_filename).exists():
                        pdf_path = Path(pdf_filename)
                    # Fallback: tentar dentro do UPLOADS_DIR
                    elif UPLOADS_DIR.exists():
                        pdf_path = UPLOADS_DIR / pdf_filename
                        if not pdf_path.exists():
                            # Tentar apenas o nome do arquivo no UPLOADS_DIR
                            pdf_path = UPLOADS_DIR / Path(pdf_filename).name
                            if not pdf_path.exists():
                                pdf_path = None
                
                if pdf_path and pdf_path.exists():
                    try:
                        from extractors.regex_utils import extract_todas_reclamadas
                        from PyPDF2 import PdfReader
                        reader = PdfReader(str(pdf_path))
                        pdf_text = "\n".join([pg.extract_text() or "" for pg in reader.pages])
                        reclamadas = extract_todas_reclamadas(pdf_text)
                        data["reclamadas"] = reclamadas
                        log(f"[DATA] Reclamadas extraídas do PDF ({pdf_path}): {len(reclamadas)}")
                    except Exception as e:
                        log(f"[DATA][WARN] Erro ao extrair reclamadas: {e}")
                        data["reclamadas"] = []
                else:
                    log(f"[DATA][WARN] PDF não encontrado: {pdf_filename}")
                    data["reclamadas"] = []
                
                log(f"[DATA] DB (último processo atualizado) OK - ID: {p.id}, CNJ: {data.get('cnj', 'N/A')}, Parte Adversa: {data.get('parte_adversa_nome', 'N/A')}, Reclamadas: {len(data.get('reclamadas', []))}")
                return data
    except Exception as e:
        log(f"[DATA][WARN] DB: {e}")

    try:
        if RPA_DATA_JSON and os.path.exists(RPA_DATA_JSON):
            if RPA_DATA_TTL_SECONDS > 0:
                age = datetime.now().timestamp() - os.path.getmtime(RPA_DATA_JSON)
                if age > RPA_DATA_TTL_SECONDS:
                    log(f"[DATA][WARN] JSON expirado ({age:.0f}s) — ignorando: {RPA_DATA_JSON}")
                else:
                    with open(RPA_DATA_JSON, "r", encoding="utf-8") as f:
                        data = json.load(f) or {}
                    
                    # VALIDAÇÃO CRÍTICA: Verificar se o JSON é do process_id correto
                    cached_pid = data.get("_cache_process_id")
                    if process_id is not None and cached_pid is not None:
                        if int(cached_pid) != int(process_id):
                            log(f"[DATA][WARN] JSON cache process_id mismatch! Cache: {cached_pid}, Solicitado: {process_id} — ignorando JSON")
                            # Não retornar dados do cache se for de outro processo
                        elif RPA_EXPECT_CNJ:
                            only_digits = lambda s: re.sub(r"\D", "", s or "")
                            if only_digits(data.get("numero_processo")) != only_digits(RPA_EXPECT_CNJ):
                                log("[DATA][WARN] JSON CNJ != RPA_EXPECT_CNJ — ignorando JSON")
                            else:
                                log(f"[DATA] JSON (válido, process_id={cached_pid}): {RPA_DATA_JSON}")
                                return data
                        else:
                            log(f"[DATA] JSON (válido, process_id={cached_pid}): {RPA_DATA_JSON}")
                            return data
                    elif RPA_EXPECT_CNJ:
                        only_digits = lambda s: re.sub(r"\D", "", s or "")
                        if only_digits(data.get("numero_processo")) != only_digits(RPA_EXPECT_CNJ):
                            log("[DATA][WARN] JSON CNJ != RPA_EXPECT_CNJ — ignorando JSON")
                        else:
                            log(f"[DATA] JSON (válido): {RPA_DATA_JSON}")
                            return data
                    else:
                        log(f"[DATA] JSON: {RPA_DATA_JSON}")
                        return data
    except Exception as e:
        log(f"[DATA][WARN] JSON: {e}")

    return {"numero_processo": "", "tipo_processo": "eletrônico", "sistema_eletronico": "PJE"}

# =========================
# SALVAR
# =========================
async def _has_validation_errors(page) -> bool:
    try:
        return await page.evaluate(
            "() => !!document.querySelector('.field-validation-error, .input-validation-error, .text-danger, .has-error, .validation-summary-errors')"
        )
    except Exception:
        return False

async def _first_visible(page: Page, selectors: List[str], timeout_ms: int = 1500) -> Optional[Any]:
    """
    Helper otimizado: aguarda PRIMEIRO seletor visível usando Promise.race pattern.
    
    Dispara todas as esperas em PARALELO e retorna assim que qualquer uma completar COM SUCESSO,
    cancelando as demais. Reduz tempo de ~48s para <2s por chamada.
    
    CORRIGIDO: Verifica se task completou com sucesso ou com TimeoutError antes de retornar.
    
    Returns:
        Tuple (locator, selector) se encontrar, ou None se timeout/erro
    """
    if not selectors:
        return None
    
    # Verificação rápida: algum já está visível?
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                is_vis = await loc.is_visible()
                if is_vis:
                    log(f"[_first_visible] ✅ Encontrado imediatamente: {sel}")
                    return (loc, sel)
        except Exception:
            pass
    
    # Nenhum visível agora - fazer race entre todos os seletores
    tasks = []
    for sel in selectors:
        try:
            task = asyncio.create_task(
                page.locator(sel).first.wait_for(state="visible", timeout=timeout_ms)
            )
            tasks.append((task, sel))
        except Exception:
            pass
    
    if not tasks:
        return None
    
    try:
        # Race: retorna primeira tarefa que completar
        done, pending = await asyncio.wait(
            [t[0] for t in tasks],
            timeout=timeout_ms / 1000.0,
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # Cancelar tarefas pendentes
        for task in pending:
            task.cancel()
        
        # Verificar se algum task completou COM SUCESSO (sem exceção)
        if done:
            for completed_task in done:
                # Verificar se task completou sem exceção
                if completed_task.exception() is None:
                    # Task completou com sucesso! Encontrar qual seletor foi
                    for task, sel in tasks:
                        if task == completed_task:
                            loc = page.locator(sel).first
                            log(f"[_first_visible] ✅ Encontrado via race: {sel}")
                            return (loc, sel)
                else:
                    # Task completou com TimeoutError ou outra exceção - ignorar
                    pass
        
        # Nenhum task completou com sucesso - todos deram timeout
        log(f"[_first_visible] ⏱️ Timeout ({timeout_ms}ms) - nenhum seletor encontrado")
        return None
        
    except asyncio.TimeoutError:
        log(f"[_first_visible] ⏱️ Timeout global ({timeout_ms}ms)")
        return None
    except Exception as e:
        log(f"[_first_visible] ❌ Erro: {e}")
        return None
    finally:
        # Garantir cancelamento de todas as tarefas pendentes
        for task, _ in tasks:
            if not task.done():
                task.cancel()


async def _find_salvar_button(page):
    sels = [
        "#buttonSave",
        "a#buttonSave",
        "button#buttonSave",
        "button[type='submit']",
        "button:has-text('Salvar')",
        "button:has-text('Gravar')",
        "button:has-text('Salvar e')",
        "input[type='submit'][value*='Salvar' i]",
        "a[onclick*='Salvar' i]",
        "button.btn-success",
        "button[data-action*='salvar' i]",
        "button[name='salvar']",
    ]
    for css in sels:
        try:
            loc = page.locator(css).first
            if await loc.count() > 0 and await loc.is_enabled():
                return loc
        except Exception:
            pass
    try:
        loc = page.get_by_role("button", name=re.compile(r"salvar|gravar", re.I))
        if await loc.count() > 0:
            return loc.first
    except Exception:
        pass
    return None

async def _check_success_signals(page, url_before: str) -> Dict[str, Any]:
    """
    Verifica sinais de sucesso no salvamento do eLaw com espera otimizada.
    
    OTIMIZADO: Usa Promise.race pattern para reduzir tempo de <2s por chamada
    (antes era ~48s devido a loops sequenciais de wait_for).
    
    Returns:
        dict: {
            'navigation_ok': bool,
            'toast_ok': bool,
            'toast_text': str,
            'toast_level': str ('success', 'error', 'warning', etc),
            'already_exists': bool,
            'validation_errors': list[str],
            'current_url': str
        }
    """
    signals = {
        'navigation_ok': False,
        'toast_ok': False,
        'toast_text': '',
        'toast_level': '',
        'already_exists': False,
        'validation_errors': [],
        'current_url': ''
    }
    
    try:
        signals['current_url'] = page.url
        # Verificar se navegou para página de detalhes (com "s" também)
        if signals['current_url'] != url_before and (
            '/detail' in signals['current_url'].lower() or 
            'id=' in signals['current_url'].lower()
        ):
            signals['navigation_ok'] = True
    except Exception:
        pass
    
    # DETECÇÃO 1: Modal/Dialog com TODOS os seletores possíveis (Bootbox, SweetAlert2, Bootstrap, Kendo)
    modal_selectors = [
        ".modal.show",
        ".modal-dialog",
        ".modal.in",
        ".modal.fade.in",
        "[role='dialog']",
        ".bootbox",
        ".bootbox-alert",
        ".bootbox-confirm",
        ".dialog",
        ".k-dialog",
        ".swal2-modal",
        ".swal2-popup",
        ".modal-content"
    ]
    
    try:
        # Usar helper otimizado: timeout único de 1.5s para TODOS os seletores em paralelo
        modal_result = await _first_visible(page, modal_selectors, timeout_ms=1500)
        
        if modal_result:
            modal_loc, modal_sel = modal_result
            
            # Modal encontrado! Ler texto
            try:
                modal_text = await modal_loc.text_content()
                if modal_text:
                    modal_text_clean = modal_text.strip()
                    log(f"[MODAL] 🔍 Detectado via {modal_sel}: {modal_text_clean[:150]}")
                    
                    # CASO 1: Modal "Já existe processo cadastrado" - SUCESSO!
                    # ⚠️ IMPORTANTE: NÃO clicar em "Sim" pois cria duplicatas!
                    # Devemos fechar o modal e usar fallback via ListRelatorio
                    if re.search(r"já existe processo cadastrado", modal_text_clean, re.I):
                        signals['already_exists'] = True
                        signals['toast_ok'] = True
                        signals['toast_level'] = 'success'
                        signals['toast_text'] = modal_text_clean[:200]
                        log(f"[MODAL] ✅ Processo já existe - NÃO clicando em Sim (evita duplicatas)")
                        
                        # Fechar o modal clicando em "Não" para NÃO criar duplicata
                        try:
                            # Tentar clicar em "Não" primeiro (opção segura que não cria duplicata)
                            btn_nao = page.locator(".bootbox.modal.in button:has-text('Não'), .bootbox.modal.in button:has-text('Nao'), .modal.in button:has-text('Não')").first
                            if await btn_nao.is_visible(timeout=1000):
                                await btn_nao.click(timeout=2000)
                                log(f"[MODAL] ✅ Clicou em 'Não' - modal fechado sem criar duplicata")
                            else:
                                # Fallback: tentar fechar via X ou botão de fechar
                                btn_close = page.locator(".bootbox.modal.in button.close, .modal.in button.close, .bootbox.modal.in [data-dismiss='modal']").first
                                if await btn_close.is_visible(timeout=1000):
                                    await btn_close.click(timeout=2000)
                                    log(f"[MODAL] ✅ Modal fechado via botão X")
                                else:
                                    # Último recurso: ESC
                                    await page.keyboard.press('Escape')
                                    log(f"[MODAL] ✅ Modal fechado via ESC")
                            
                            await short_sleep_ms(500)
                            
                            # Sinalizar que precisamos usar fallback via ListRelatorio
                            signals['needs_relatorio_fallback'] = True
                            log(f"[MODAL] ℹ️ Fallback via ListRelatorio será acionado para obter URL de detalhes")
                            
                        except Exception as e:
                            log(f"[MODAL] ⚠️ Erro ao fechar modal: {e}")
                            signals['needs_relatorio_fallback'] = True
                    
                    # CASO 2: Modal de "data processual próxima" - CONFIRMAR SALVAMENTO
                    elif re.search(r"data processual.*próxima.*data de hoje|deseja editar", modal_text_clean, re.I):
                        log(f"[MODAL] ⚠️ Modal de data processual - confirmando salvamento...")
                        
                        # Clicar em "Não" para confirmar salvamento sem editar
                        try:
                            btn_nao = page.locator("button:has-text('Não'), button:has-text('Nao'), button:has-text('NAO')").first
                            await btn_nao.click(timeout=2000)
                            log(f"[MODAL] ✅ Salvamento confirmado!")
                            
                            # Marcar como sucesso
                            signals['toast_ok'] = True
                            signals['toast_level'] = 'success'
                            signals['toast_text'] = "Processo salvo (data confirmada)"
                            await short_sleep_ms(500)
                        except Exception as e:
                            log(f"[MODAL] ⚠️ Erro ao clicar em 'Não': {e}")
                    
                    # CASO 3: Qualquer outro modal de confirmação genérico
                    else:
                        log(f"[MODAL] ℹ️ Modal genérico detectado - tentando confirmar...")
                        
                        # Tentar clicar em botão de confirmação genérico DENTRO do modal
                        try:
                            btn = page.locator(".modal.in button:has-text('OK'), .modal.in button:has-text('Sim'), .modal.in button:has-text('Confirmar'), .modal.in .btn-primary").first
                            if await btn.count() > 0:
                                await btn.click(timeout=2000)
                                log(f"[MODAL] ✅ Modal confirmado!")
                                
                                # Se conseguiu fechar, assumir sucesso
                                signals['toast_ok'] = True
                                signals['toast_level'] = 'success'
                                signals['toast_text'] = f"Confirmado: {modal_text_clean[:100]}"
                                await short_sleep_ms(500)
                        except:
                            pass
            except Exception as e:
                log(f"[MODAL] ⚠️ Erro ao processar modal: {e}")
    except Exception:
        pass
    
    # DETECÇÃO 2: Toast (apenas se modal não detectou) - OTIMIZADO com Promise.race
    if not signals['already_exists']:
        toast_selectors = [
            ".toast.show",
            ".toast-message",
            ".toast-container .toast",
            ".k-notification",
            ".k-notification-success",
            ".k-notification-error",
            ".alert.show",
            "[role='alert']",
            ".swal2-container",
            ".toast-success",
            ".alert-success",
            ".alert-danger"
        ]
        
        try:
            # Usar helper otimizado: timeout único de 1.5s para TODOS os seletores em paralelo
            toast_result = await _first_visible(page, toast_selectors, timeout_ms=1500)
            
            if toast_result:
                toast_loc, toast_sel = toast_result
                
                # Toast encontrado! Ler texto e classificar
                try:
                    toast_text = await toast_loc.text_content()
                    if toast_text:
                        signals['toast_text'] = toast_text.strip()
                        text_lower = signals['toast_text'].lower()
                        
                        # Classificar nível pelo texto OU classes CSS
                        classes = await toast_loc.get_attribute("class") or ""
                        classes_lower = classes.lower()
                        
                        # Palavras que indicam SUCESSO (no texto ou classes)
                        success_keywords_text = ['sucesso', 'salvo com sucesso', 'cadastrado', 'gravado']
                        success_keywords_class = ['success', 'sucesso']
                        
                        # Palavras que indicam ERRO (no texto ou classes)
                        error_keywords_text = ['erro', 'preencha', 'obrigatório', 'inválido', 'incorreto', 'falha', 'não pode', 'verifique']
                        error_keywords_class = ['danger', 'error', 'erro']
                        
                        # Classificar por CLASSES primeiro (mais confiável)
                        if any(term in classes_lower for term in success_keywords_class):
                            signals['toast_level'] = 'success'
                            signals['toast_ok'] = True
                        elif any(term in classes_lower for term in error_keywords_class):
                            signals['toast_level'] = 'error'
                        # Se classes não ajudaram, classificar por TEXTO
                        elif any(term in text_lower for term in success_keywords_text):
                            signals['toast_level'] = 'success'
                            signals['toast_ok'] = True
                        elif any(term in text_lower for term in error_keywords_text):
                            signals['toast_level'] = 'error'
                        elif any(term in classes_lower for term in ['warning', 'aviso']):
                            signals['toast_level'] = 'warning'
                        
                        log(f"[TOAST] Detectado ({signals['toast_level']}) via {toast_sel}: {signals['toast_text'][:100]}")
                except Exception:
                    pass
        except Exception:
            pass
    
    # DETECÇÃO 3: Erros de validação (rápido, sem timeout)
    try:
        error_locs = await page.locator(".field-validation-error, .input-validation-error, .validation-summary-errors li").all()
        for loc in error_locs:
            try:
                text = await loc.text_content()
                if text and text.strip():
                    signals['validation_errors'].append(text.strip())
            except Exception:
                pass
    except Exception:
        pass
    
    return signals

async def click_salvar_and_wait(page, cnj_expected: str = "") -> Dict[str, Any]:
    """
    Clica no botão Salvar com retry seguro e aguarda resposta (sucesso ou erro).
    
    Estratégia de 2 tentativas:
    - 1ª tentativa: clicar e aguardar 20s por navegação
    - Se não confirmar sucesso E botão estiver habilitado: 2ª tentativa
    
    Returns:
        dict: {
            'success': bool,
            'url_before': str,
            'url_after': str,
            'message': str,
            'url_changed': bool,
            'attempts': int
        }
    """
    result = {
        'success': False,
        'url_before': '',
        'url_after': '',
        'message': '',
        'url_changed': False,
        'attempts': 0
    }
    
    try:
        result['url_before'] = page.url
        log(f"[SALVAR] URL inicial: {result['url_before']}")
    except Exception:
        pass
    
    try:
        page.once("dialog", lambda d: asyncio.create_task(d.accept()))
    except Exception:
        pass

    btn = await _find_salvar_button(page)
    if not btn:
        result['message'] = "Botão 'Salvar' não encontrado"
        log(f"[SALVAR][ERRO] {result['message']}")
        try:
            png = _get_screenshot_path("salvar_nao_encontrado.png", process_id=process_id)  # 2025-11-21: Corrigido
            await page.screenshot(path=str(png), full_page=True)
            log(f"[SALVAR] Screenshot: {png}")
        except Exception:
            pass
        return result

    # Máximo de 2 tentativas
    max_attempts = 2
    navigation_succeeded = False
    
    for attempt in range(1, max_attempts + 1):
        result['attempts'] = attempt
        log(f"[SALVAR] ═══ TENTATIVA {attempt}/{max_attempts} ═══")
        
        await _scroll_into_view(btn)
        
        # Verificar se botão está habilitado antes de tentar
        try:
            is_enabled = await btn.is_enabled()
            log(f"[SALVAR][T{attempt}] Botão está {'HABILITADO' if is_enabled else 'DESABILITADO'}")
            if not is_enabled and attempt > 1:
                log(f"[SALVAR][T{attempt}] Botão desabilitado - processo pode estar sendo salvo, aguardando...")
                await short_sleep_ms(3000)
                continue
        except Exception:
            pass

        # Tentar clicar
        ok = await robust_click(f"Botão 'Salvar' (tentativa {attempt})", btn, timeout_ms=DEFAULT_TIMEOUT_MS)
        if not ok:
            try:
                await page.evaluate("() => document.querySelector('form')?.requestSubmit?.()")
                log(f"[SALVAR][T{attempt}] requestSubmit() acionado como fallback")
                ok = True
            except Exception:
                pass
        
        if not ok:
            result['message'] = f"Não consegui acionar o botão Salvar (tentativa {attempt})"
            log(f"[SALVAR][ERRO] {result['message']}")
            if attempt < max_attempts:
                await short_sleep_ms(2000)
                continue
            return result
        
        log(f"[SALVAR][T{attempt}] Clique executado. Aguardando resposta do servidor...")
        
        # Aguardar rede estabilizar após clique
        await wait_network_quiet(page, timeout_ms=max(SETTLE_NET_MS, 1500))
        await short_sleep_ms(500)
        
        # Aguardar navegação ou toast de sucesso com timeout progressivo
        wait_timeout = 20.0 if attempt == 1 else 25.0  # 2ª tentativa aguarda mais
        deadline = asyncio.get_event_loop().time() + wait_timeout
        
        log(f"[SALVAR][T{attempt}] Aguardando confirmação (até {wait_timeout}s)...")
        
        # Polling a cada 1s com logging de progresso
        check_interval = 1.0
        last_check_time = asyncio.get_event_loop().time()
        elapsed = 0
        
        while asyncio.get_event_loop().time() < deadline:
            await short_sleep_ms(500)
            
            current_time = asyncio.get_event_loop().time()
            if current_time - last_check_time >= check_interval:
                elapsed = int(current_time - (deadline - wait_timeout))
                signals = await _check_success_signals(page, result['url_before'])
                
                log(f"[SALVAR][T{attempt}][{elapsed}s] Nav:{signals['navigation_ok']}, Toast:{signals['toast_ok']}, JáExiste:{signals['already_exists']}, Erros:{len(signals['validation_errors'])}, URL:{signals['current_url']}")
                
                # Sucesso confirmado: navegação para /processo/details
                if signals['navigation_ok']:
                    log(f"[SALVAR][T{attempt}] ✅ CONFIRMADO: Navegação para {signals['current_url']}")
                    navigation_succeeded = True
                    # Aguardar página carregar completamente
                    await wait_network_quiet(page, timeout_ms=3000)
                    await short_sleep_ms(800)
                    break
                
                # Sucesso confirmado: processo JÁ EXISTE (não é erro!)
                if signals['already_exists']:
                    log(f"[SALVAR][T{attempt}] ✅ CONFIRMADO: {signals['toast_text']}")
                    result['message'] = 'Processo foi preenchido mas já existe no eLaw'
                    navigation_succeeded = True
                    # ✅ Se capturou URL de detalhes do modal, guardar no resultado
                    if signals.get('detail_url'):
                        result['detail_url'] = signals['detail_url']
                        log(f"[SALVAR] URL de detalhes do modal: {signals['detail_url']}")
                    break
                
                # Sucesso confirmado: toast verde explícito
                if signals['toast_ok'] and signals['toast_level'] == 'success':
                    log(f"[SALVAR][T{attempt}] ✅ CONFIRMADO: Toast verde - {signals['toast_text'][:50]}")
                    navigation_succeeded = True
                    break
                
                # Erro confirmado: toast vermelho
                if signals['toast_level'] == 'error':
                    log(f"[SALVAR][T{attempt}] ❌ ERRO: Toast vermelho - {signals['toast_text']}")
                    result['message'] = f"eLaw rejeitou: {signals['toast_text'][:100]}"
                    navigation_succeeded = False
                    break
                
                # Erro confirmado: mensagens de validação
                if signals['validation_errors']:
                    log(f"[SALVAR][T{attempt}] ❌ ERRO: Validação falhou: {signals['validation_errors']}")
                    result['message'] = f"eLaw rejeitou: {'; '.join(signals['validation_errors'][:3])}"
                    navigation_succeeded = False
                    break
                
                # DIAGNÓSTICO: Se passou 10s sem NENHUMA detecção, capturar HTML + Screenshot para debug
                if elapsed == 10 and not (signals['navigation_ok'] or signals['toast_ok'] or signals['already_exists']):
                    log(f"[SALVAR][DEBUG] ⚠️ 10s sem feedback - capturando diagnostics...")
                    try:
                        # Screenshot para debug visual
                        debug_screenshot = SCREENSHOT_DIR / f"debug_10s_sem_feedback_{attempt}.png"
                        await page.screenshot(path=str(debug_screenshot), full_page=True)
                        log(f"[SALVAR][DEBUG] Screenshot salvo: {debug_screenshot.name}")
                        send_screenshot_to_monitor(debug_screenshot, region="DEBUG_10S_SEM_FEEDBACK")
                        
                        # Dump HTML para análise de DOM
                        html_content = await page.content()
                        debug_html = SCREENSHOT_DIR / f"debug_10s_html_{attempt}.html"
                        with open(debug_html, 'w', encoding='utf-8') as f:
                            f.write(html_content)
                        log(f"[SALVAR][DEBUG] HTML salvo: {debug_html.name}")
                        
                        # Verificar se há QUALQUER modal visível (mesmo sem classes conhecidas)
                        all_modals = await page.locator("[role='dialog'], .modal, .bootbox, .swal2, .k-dialog").all()
                        log(f"[SALVAR][DEBUG] Encontrados {len(all_modals)} elementos de modal na página")
                        for i, modal in enumerate(all_modals[:3]):  # Apenas os 3 primeiros
                            try:
                                is_vis = await modal.is_visible()
                                text = await modal.text_content()
                                log(f"[SALVAR][DEBUG] Modal {i+1}: Visível={is_vis}, Texto={text[:100] if text else 'vazio'}")
                            except:
                                pass
                    except Exception as e:
                        log(f"[SALVAR][DEBUG] ⚠️ Erro ao capturar diagnostics: {e}")
                
                last_check_time = current_time
        
        # Avaliar resultado da tentativa
        if navigation_succeeded:
            log(f"[SALVAR][T{attempt}] ✅ Sucesso confirmado!")
            break
        else:
            log(f"[SALVAR][T{attempt}] ⚠️ Sem confirmação após {wait_timeout}s")
            
            # Verificar se deve tentar novamente
            if attempt < max_attempts:
                # Verificar se botão voltou a ficar habilitado (sinal de que pode tentar de novo)
                try:
                    btn_enabled_again = await btn.is_enabled()
                    log(f"[SALVAR][T{attempt}] Botão está {'HABILITADO' if btn_enabled_again else 'DESABILITADO'} para retry")
                    
                    if not btn_enabled_again:
                        log(f"[SALVAR][T{attempt}] Botão ainda processando - não vou tentar novamente")
                        result['message'] = "Botão ainda processando após timeout - possível salvamento em andamento"
                        break
                    
                    # Verificar novamente por sinais de sucesso antes de retry
                    final_check = await _check_success_signals(page, result['url_before'])
                    if final_check['navigation_ok'] or final_check['toast_ok']:
                        log(f"[SALVAR][T{attempt}] ✅ Sucesso detectado na verificação final!")
                        navigation_succeeded = True
                        break
                    
                    log(f"[SALVAR][T{attempt}] Preparando retry em 2s...")
                    await short_sleep_ms(2000)
                    
                except Exception as e:
                    log(f"[SALVAR][T{attempt}] Erro ao verificar condições de retry: {e}")
                    break
            else:
                log(f"[SALVAR] ❌ Esgotadas {max_attempts} tentativas sem confirmação")
                result['message'] = "eLaw não confirmou salvamento após múltiplas tentativas"
    
    # Atualizar URL após espera
    try:
        result['url_after'] = page.url
        result['url_changed'] = (result['url_after'] != result['url_before'])
        log(f"[SALVAR] URL final: {result['url_after']} (mudou: {result['url_changed']})")
    except Exception:
        pass
    
    # Captura screenshot APÓS navegação completa
    try:
        png = _get_screenshot_path("pos_salvar.png", process_id=process_id)  # 2025-11-21: Corrigido
        await page.screenshot(path=str(png), full_page=True)
        log(f"[SALVAR] Screenshot pós-salvar capturado: {png} (URL: {page.url})")
        send_screenshot_to_monitor(png, region="POS_SALVAR")
    except Exception:
        pass

    if cnj_expected:
        try:
            await ensure_cnj_still_present(page, cnj_expected)
        except Exception:
            pass

    # Verificação final de erros de validação
    if await _has_validation_errors(page):
        result['success'] = False
        result['message'] = result.get('message') or "Campos com erro de validação após salvar"
        log(f"[SALVAR][ERRO] {result['message']}")
        return result

    # Definir resultado baseado em navigation_succeeded
    if navigation_succeeded:
        result['success'] = True
        result['message'] = f"Processo salvo com sucesso após {result['attempts']} tentativa(s)"
        log(f"[SALVAR] ✅ SUCESSO FINAL confirmado após {result['attempts']} tentativa(s)")
    else:
        result['success'] = False
        result['message'] = result.get('message') or "Não foi possível confirmar sucesso no salvamento"
        log(f"[SALVAR] ❌ FALHA FINAL após {result['attempts']} tentativa(s)")
    
    return result

# =========================
# Fluxo principal do formulário
# =========================
async def _settle(page, tag: str):
    await wait_network_quiet(page, SETTLE_NET_MS)
    await short_sleep_ms(SETTLE_SLEEP_MS)
    dlog(f"[SETTLE] {tag}")

def _must(ok: bool, step: str):
    if not ok:
        raise RuntimeError(f"[FORM] Passo crítico falhou: {step}")

def parse_roles_from_pdf(pdf_text: str) -> Dict[str, str]:
    RE_TOP_RECLAMANTE = re.compile(r"\bRECLAMANTE\s*[:\-–]\s*(.+)", re.I)
    RE_TOP_RECLAMADO = re.compile(r"\bRECLAMADO\s*[:\-–]\s*(.+)", re.I)
    out = {"reclamante": "", "reclamado": ""}
    if not pdf_text:
        return out
    lines = [l.strip() for l in pdf_text.splitlines() if l.strip()]
    for ln in lines[:400]:
        m = RE_TOP_RECLAMANTE.search(ln)
        if m and not out["reclamante"]:
            out["reclamante"] = m.group(1).strip()
        m = RE_TOP_RECLAMADO.search(ln)
        if m and not out["reclamado"]:
            out["reclamado"] = m.group(1).strip()
        if out["reclamante"] and out["reclamado"]:
            break
    return out

def is_probably_pj(name: str) -> bool:
    n = norm(name)
    if re.search(r"\b(s\.?a\.?|ltda|eireli|me|mei|s\/a|s\.a\.)\b", n):
        return True
    if re.search(r"\bcompanhia\b|\bdistribuicao\b|\bdistribuição\b|\bgrupo\b|\bholding\b", n):
        return True
    if re.search(r"\b\d{2}\.?\d{3}\.?\d{3}\/?\d{4}\-?\d{2}\b", name):
        return True
    return False

def is_probably_pf(name: str) -> bool:
    return not is_probably_pj(name)

# =========================
# Sistema Universal de Fallback
# =========================
def extract_numero_orgao_from_pdf(pdf_text: str) -> str:
    """Extrai número do órgão do PDF."""
    if not pdf_text:
        return ""
    patterns = [
        r"(?i)(?:n[úu]mero\s+(?:do\s+)?(?:[óo]rg[ãa]o|vara|turma|juris(?:di[çc][ãa]o)?)[:\s]+)(\d+)",
        r"(?i)(?:vara|turma|juizado)\s+n[úu]mero\s+(\d+)",
        r"(?i)(\d+)[ªº]?\s+vara",
        r"(?i)vara[:\s]+(\d+)",
    ]
    for pat in patterns:
        m = re.search(pat, pdf_text)
        if m:
            return m.group(1).strip()
    return ""

def extract_valor_causa_from_pdf(pdf_text: str) -> str:
    """Extrai valor da causa do PDF."""
    if not pdf_text:
        return ""
    patterns = [
        r"(?i)valor\s+(?:da\s+)?(?:causa|a[çc][ãa]o)[:\s]+R?\$?\s*([\d\.,]+)",
        r"(?i)R\$\s*([\d\.,]+)",
        r"(?i)(?:pedido|indeniza[çc][ãa]o|d[ií]vida)[:\s]+R?\$?\s*([\d\.,]+)",
    ]
    for pat in patterns:
        m = re.search(pat, pdf_text)
        if m:
            valor = m.group(1).strip()
            valor = re.sub(r"[^\d,]", "", valor)
            if valor:
                return valor
    return ""

def extract_instancia_from_pdf(pdf_text: str, dropdown_options: List[str]) -> Optional[str]:
    """
    Extrai instância do PDF usando sistema principal de extração + fuzzy matching.
    
    ✅ FIX ARQUITETURAL: Delega para extractors.regex_utils.detect_orgao_origem_instancia()
    e então usa _best_match() com detecção de ordinais. Single source of truth.
    """
    if not pdf_text or not dropdown_options:
        return None
    
    # Usa sistema principal de extração (já corrigido para detectar 1ª vs 2ª instância)
    try:
        from extractors.regex_utils import detect_orgao_origem_instancia
        _, _, instancia_extraida = detect_orgao_origem_instancia(pdf_text)
        
        if instancia_extraida:
            # Usa fuzzy matching com detecção de ordinais
            match = _best_match(dropdown_options, instancia_extraida, threshold=20)
            if match:
                return match
    except Exception as e:
        log(f"[extract_instancia_from_pdf][WARN] Erro ao usar sistema principal: {e}")
    
    # Fallback: detecção manual simples baseada em sinais MUITO fortes
    pdf_norm = norm(pdf_text)
    
    # Sinais MUITO FORTES que definem instância
    has_vara = "vara" in pdf_norm
    has_recurso_header = "recurso" in pdf_norm[:300] and "trt" in pdf_norm[:500]
    
    # ✅ FIX: Verifica se "acordao" existe (find != -1) ANTES de comparar posição
    acordao_pos = pdf_norm.find("acordao")
    has_acordao_header = acordao_pos != -1 and acordao_pos < 500
    
    if has_vara and not has_recurso_header and not has_acordao_header:
        # Vara sem recurso/acórdão no header = 1ª instância
        for opt in dropdown_options:
            if _detect_ordinal(opt) == 1:
                return opt
    elif has_recurso_header or has_acordao_header:
        # Recurso ou acórdão no header = 2ª instância
        for opt in dropdown_options:
            if _detect_ordinal(opt) == 2:
                return opt
    
    return None

def extract_cliente_grupo_from_pdf(pdf_text: str, dropdown_options: List[str]) -> Optional[str]:
    """
    Extrai Cliente/Grupo do PDF comparando com opções do dropdown do eLaw.
    
    IMPORTANTE: Usa brand_map.py para identificar clientes cadastrados e
    então busca a melhor correspondência nas opções do dropdown.
    """
    if not pdf_text or not dropdown_options:
        return None
    
    # 1. Extrai partes do PDF
    pdf_roles = parse_roles_from_pdf(pdf_text)
    reclamado = pdf_roles.get("reclamado", "").strip()
    
    if not reclamado:
        return None
    
    # 2. Usa brand_map para normalizar (agora com accent-insensitive matching!)
    from extractors.brand_map import normalize_cliente
    cliente_normalizado = normalize_cliente(reclamado)
    
    if not cliente_normalizado or cliente_normalizado == reclamado.strip().title():
        # normalize_cliente não encontrou no banco, tenta fuzzy direto com dropdown
        log(f"[CLIENTE_GRUPO] Cliente não identificado no banco: {reclamado[:50]}, tentando fuzzy com dropdown")
        match = _best_match(dropdown_options, reclamado, threshold=15, prefer_words=["pao", "acucar", "gpa", "cbd", "sendas"])
        if match:
            log(f"[CLIENTE_GRUPO] ✅ Match fuzzy com dropdown: {match}")
            return match
    else:
        # Cliente identificado, busca no dropdown
        log(f"[CLIENTE_GRUPO] Cliente identificado: {reclamado[:50]} → {cliente_normalizado}")
        match = _best_match(dropdown_options, cliente_normalizado, threshold=12)
        if match:
            log(f"[CLIENTE_GRUPO] ✅ Match no dropdown: {match}")
            return match
        
        # Fallback: tenta nome do grupo também
        for grupo in ["Grupo Pão de Açúcar", "GPA", "Pro Pharma", "CSN", "HAZTEC"]:
            if grupo.lower() in cliente_normalizado.lower():
                match = _best_match(dropdown_options, grupo, threshold=10)
                if match:
                    log(f"[CLIENTE_GRUPO] ✅ Match por grupo: {match}")
                    return match
    
    return None

def extract_assunto_from_pdf(pdf_text: str, dropdown_options: List[str]) -> Optional[str]:
    """Extrai assunto/tema do PDF e compara com opções do dropdown."""
    if not pdf_text or not dropdown_options:
        return None
    
    pdf_norm = norm(pdf_text)
    
    # Padrões comuns de assunto trabalhista
    keywords = [
        ("reclamação trabalhista", ["reclamacao", "trabalhista"]),
        ("ação trabalhista", ["acao", "trabalhista"]),
        ("horas extras", ["horas", "extras"]),
        ("verbas rescisórias", ["verbas", "rescisoria"]),
        ("fgts", ["fgts"]),
        ("adicional noturno", ["adicional", "noturno"]),
        ("diferenças salariais", ["diferencas", "salariais"]),
    ]
    
    for label, terms in keywords:
        if all(term in pdf_norm for term in terms):
            match = _best_match(dropdown_options, label, threshold=12)
            if match:
                return match
    
    # Fallback: tenta "Reclamação Trabalhista"
    return _best_match(dropdown_options, "Reclamação Trabalhista", threshold=10)

def extract_foro_from_pdf(pdf_text: str, dropdown_options: List[str]) -> Optional[str]:
    """Extrai foro/juizado do PDF e compara com opções do dropdown."""
    if not pdf_text or not dropdown_options:
        return None
    
    # Busca padrões de foro
    patterns = [
        r"(?i)foro\s+(?:de\s+)?([A-ZÇÃÁÉÍÓÚÂÊÔÀÜ][a-zçãáéíóúâêôàü\s]+)",
        r"(?i)juizado\s+(?:de\s+)?([A-ZÇÃÁÉÍÓÚÂÊÔÀÜ][a-zçãáéíóúâêôàü\s]+)",
        r"(?i)comarca\s+(?:de\s+)?([A-ZÇÃÁÉÍÓÚÂÊÔÀÜ][a-zçãáéíóúâêôàü\s]+)",
    ]
    
    for pat in patterns:
        m = re.search(pat, pdf_text)
        if m:
            foro_extraido = m.group(1).strip()
            match = _best_match(dropdown_options, foro_extraido, threshold=10)
            if match:
                return match
    
    return None

def extract_parte_interessada_from_pdf(pdf_text: str, dropdown_options: List[str]) -> Optional[str]:
    """
    Extrai parte interessada do PDF e compara com opções do dropdown.
    Usa a lógica de infer_cliente_grupo_and_parte para identificar corretamente.
    """
    if not pdf_text or not dropdown_options:
        return None
    
    # Usa lógica existente para inferir
    pdf_roles = parse_roles_from_pdf(pdf_text)
    reclamado = pdf_roles.get("reclamado", "").strip()
    reclamante = pdf_roles.get("reclamante", "").strip()
    
    # Identifica qual é PJ (normalmente o cliente)
    parte_interessada = reclamado if is_probably_pj(reclamado) else reclamante
    
    if not parte_interessada:
        return None
    
    # Tenta match com dropdown (autocomplete pode ter sugestões)
    match = _best_match(dropdown_options, parte_interessada, threshold=15)
    return match if match else parte_interessada

def extract_npc_from_pdf(pdf_text: str) -> str:
    """Extrai NPC do PDF."""
    if not pdf_text:
        return ""
    
    patterns = [
        r"(?i)\bNPC\b\s*[:\-–]\s*([A-Z0-9][A-Z0-9\/\.\-\s]{2,60})",
        r"(?i)n[úu]mero\s+(?:do\s+)?processo\s+(?:antigo|anterior)[:\s]+([A-Z0-9][A-Z0-9\/\.\-\s]{2,60})",
    ]
    
    for pat in patterns:
        m = re.search(pat, pdf_text)
        if m:
            return m.group(1).strip()
    
    return ""

def extract_field_with_full_fallback(
    field_name: str,
    data: Dict[str, Any],
    pdf_text: str,
    dropdown_options: Optional[List[str]] = None,
    pdf_extractor: Optional[callable] = None,
    data_keys: Optional[List[str]] = None,
    threshold: int = 10
) -> Optional[str]:
    """
    Sistema universal de fallback para TODOS os campos do RPA.
    
    HIERARQUIA DE FALLBACK:
    1. Sistema Jurídico (data dict) - fonte mais confiável
    2. Banco de dados (já incluído em data)
    3. Extração do PDF usando regex customizado
    4. Comparação com opções do dropdown (se aplicável)
    
    Args:
        field_name: Nome do campo principal a extrair (ex: "numero_orgao")
        data: Dicionário com dados do processo (sistema + banco)
        pdf_text: Texto completo do PDF
        dropdown_options: Lista de opções disponíveis no dropdown do eLaw (opcional)
        pdf_extractor: Função customizada para extrair do PDF (opcional)
        data_keys: Lista alternativa de chaves para buscar no data (opcional)
        threshold: Score mínimo para fuzzy matching (default: 10)
        
    Returns:
        Valor extraído ou None
        
    Examples:
        # Campo de input (sem dropdown)
        num_orgao = extract_field_with_full_fallback(
            "numero_orgao",
            data,
            pdf_text,
            pdf_extractor=extract_numero_orgao_from_pdf,
            data_keys=["numero_orgao", "numero_jurisdicao", "orgao_numero"]
        )
        
        # Campo dropdown
        instancia = extract_field_with_full_fallback(
            "instancia",
            data,
            pdf_text,
            dropdown_options=inst_opts,
            pdf_extractor=extract_instancia_from_pdf
        )
    """
    # 1. PRIORIDADE MÁXIMA: Dados do sistema/banco
    keys_to_try = data_keys or [field_name]
    for key in keys_to_try:
        value = (data.get(key) or "").strip()
        if value:
            log(f"[FALLBACK][{field_name}] ✅ Usando valor do SISTEMA/BANCO: {value}")
            # Se tem dropdown, valida se valor existe nas opções
            if dropdown_options:
                match = _best_match(dropdown_options, value, threshold=threshold)
                if match:
                    log(f"[FALLBACK][{field_name}] ✅ Valor validado no dropdown: {match}")
                    return match
            return value
    
    # 2. FALLBACK: Extração do PDF
    if pdf_extractor and pdf_text:
        try:
            if dropdown_options:
                # Se tem dropdown, passa as opções para o extractor
                pdf_value = pdf_extractor(pdf_text, dropdown_options)
            else:
                pdf_value = pdf_extractor(pdf_text)
            
            if pdf_value:
                log(f"[FALLBACK][{field_name}] ✅ Extraído do PDF: {pdf_value}")
                # Se tem dropdown, valida fuzzy matching
                if dropdown_options:
                    match = _best_match(dropdown_options, pdf_value, threshold=threshold)
                    if match:
                        log(f"[FALLBACK][{field_name}] ✅ Matched com dropdown: {match}")
                        return match
                return pdf_value
        except Exception as e:
            log(f"[FALLBACK][{field_name}][WARN] Erro ao extrair do PDF: {e}")
    
    # 3. ÚLTIMO RECURSO: Primeira opção do dropdown
    if dropdown_options and len(dropdown_options) > 0:
        log(f"[FALLBACK][{field_name}] ⚠️  Usando primeira opção do dropdown: {dropdown_options[0]}")
        return dropdown_options[0]
    
    log(f"[FALLBACK][{field_name}] ❌ Nenhum valor encontrado")
    return None

def infer_cliente_grupo_and_parte(pdf_text: str, data: Dict[str, Any]) -> Dict[str, str]:
    """
    PRIORIZA dados do BANCO DE DADOS, só usa PDF para campos vazios.
    Isso evita sobrescrever dados corretos do banco com dados errados de PDFs misturados.
    """
    # PRIORIDADE 1: Dados do banco de dados (fonte canônica)
    db_recl = str(data.get("parte_reclamante") or data.get("reclamante") or "").strip()
    db_reld = str(data.get("parte_reclamado") or data.get("reclamado") or "").strip()
    db_parte_adversa = str(data.get("parte_adversa_nome") or "").strip()
    
    # PRIORIDADE 2: Só extrai do PDF se campos estiverem vazios
    pdf_roles = parse_roles_from_pdf(pdf_text or "") if pdf_text else {}
    pdf_recl = pdf_roles.get("reclamante", "").strip()
    pdf_reld = pdf_roles.get("reclamado", "").strip()
    
    # Usa dados do banco primeiro, PDF só como fallback
    recl = db_recl or pdf_recl
    reld = db_reld or pdf_reld
    
    # Log divergências para auditoria
    if db_recl and pdf_recl and db_recl != pdf_recl:
        log(f"[INFER][WARN] Reclamante divergente - DB: '{db_recl}' vs PDF: '{pdf_recl}' - usando DB!")
    if db_reld and pdf_reld and db_reld != pdf_reld:
        log(f"[INFER][WARN] Reclamado divergente - DB: '{db_reld}' vs PDF: '{pdf_reld}' - usando DB!")

    cliente_grupo = data.get("grupo") or data.get("cliente") or ""
    
    # PRIORIDADE 1: Usa posicao_parte_interessada do banco se existir E for específica
    db_posicao = str(data.get("posicao_parte_interessada") or "").strip()
    
    # 🔒 CRITICAL: Ignora placeholders genéricos/inválidos do banco (dados antigos corrompidos)
    # "PARTES" é um label genérico do eLaw (ID 63) que não especifica QUEM é a parte
    INVALID_POSICOES = {"PARTES", "PARTE", ""}  # Placeholders que devem ser re-inferidos
    
    if db_posicao and db_posicao.upper() not in INVALID_POSICOES:
        log(f"[INFER] Usando posicao_parte_interessada do BANCO: {db_posicao}")
        posicao = normalize_posicao(db_posicao)  # ✅ PRIORIZA BANCO (valores válidos)
    else:
        if db_posicao and db_posicao.upper() in INVALID_POSICOES:
            log(f"[INFER][WARN] Posição genérica/inválida no banco ('{db_posicao}') - forçando re-inferência do PDF")
        # Infere baseado na lógica de PJ/PF
        if is_probably_pj(reld):
            posicao = normalize_posicao("RECLAMADO")
        else:
            posicao = normalize_posicao("RECLAMANTE")
        log(f"[INFER] Posição INFERIDA (banco vazio ou inválido): {posicao}")
    
    # Se parte_adversa_nome já está no banco, usa direto (fonte mais confiável)
    if db_parte_adversa:
        log(f"[INFER] Usando parte_adversa_nome do BANCO: {db_parte_adversa}")
        parte_adversa_nome = db_parte_adversa
        # Determina parte_interessada_nome baseado na posição
        if posicao == "RECLAMADO":
            parte_interessada_nome = reld
        else:
            parte_interessada_nome = recl
    else:
        # Inferir pela lógica antiga se não tiver no banco
        if is_probably_pj(reld):
            parte_interessada_nome = reld
            parte_adversa_nome = recl
        else:
            parte_interessada_nome = recl
            parte_adversa_nome = reld

    parte_adversa_tipo = "FISICA" if is_probably_pf(parte_adversa_nome) else "JURIDICA"

    if not cliente_grupo and is_probably_pj(parte_interessada_nome):
        cliente_grupo = "Grupo Pão de Açúcar"

    return {
        "cliente_grupo": (cliente_grupo or "Grupo Pão de Açúcar").strip(),
        "parte_interessada_nome": parte_interessada_nome.strip(),
        "posicao_parte_interessada": posicao,
        "parte_adversa_nome": parte_adversa_nome.strip(),
        "parte_adversa_tipo": parte_adversa_tipo,
    }

# IDs pós-Tipo de Ação
GRUPO_CLIENTE_SELECT_ID = "GrupoClienteId"
POSICAO_CLIENTE_SELECT_ID = "PosicaoClienteId"
PARTE_INTERESSADA_SELECT_ID = "ClienteId"
PARTE_ADVERSA_TIPO_NAME = "TipoPessoaAdverso"   # 1=Física, 2=Jurídica
PARTE_ADVERSA_NOME_INPUT_ID = "AdversoAutoComplete"
VALOR_CAUSA_INPUT_ID = "ValorCausa"
ESTRATEGIA_SELECT_ID = "EstrategiaId"

async def fill_new_process_form(page, data: Dict[str, Any], process_id: int):  # 2025-11-21: process_id OBRIGATÓRIO
    update_status("navegando_formulario", "Navegando para formulário de novo processo...", process_id=process_id)
    log("[FORM] aguardando /Processo/form")
    try:
        await page.wait_for_url(re.compile(r"/Processo/form"), timeout=NAV_TIMEOUT_MS)
    except Exception:
        pass
    await _settle(page, "form aberto")
    update_status("formulario_aberto", "Formulário aberto - iniciando preenchimento", process_id=process_id)

    # 🔧 DEBUGGING: Logar process_id e dados críticos do banco
    # 2025-12-02: Logs expandidos para diagnosticar data bleeding entre workers
    log(f"[FORM][DEBUG] ═══ PROCESSO #{process_id} ═══")
    log(f"[FORM][DEBUG] data['numero_processo'] = {data.get('numero_processo')}")
    log(f"[FORM][DEBUG] data['cnj'] = {data.get('cnj')}")
    log(f"[FORM][DEBUG] data['parte_adversa_nome'] = {data.get('parte_adversa_nome')}")
    log(f"[FORM][DEBUG] data['cargo_funcao'] = {data.get('cargo_funcao')}")
    log(f"[FORM][DEBUG] data['cargo'] = {data.get('cargo')}")
    log(f"[FORM][DEBUG] data['pis'] = {data.get('pis')}")
    log(f"[FORM][DEBUG] data['ctps'] = {data.get('ctps')}")
    log(f"[FORM][DEBUG] data['salario'] = {data.get('salario')}")
    log(f"[FORM][DEBUG] data['data_admissao'] = {data.get('data_admissao')}")
    log(f"[FORM][DEBUG] data['data_demissao'] = {data.get('data_demissao')}")
    log(f"[FORM][DEBUG] len(data) = {len(data)} campos")
    log(f"[FORM][DEBUG] ═══ FIM DEBUG #{process_id} ═══")

    # ORDEM CORRETA: Tipo → CNJ → Número
    # O campo #ProtocoloInicial é CRIADO pelo eLaw quando marcamos Tipo=Eletrônico
    import re as regex_module  # Force reimport to avoid async scope issues
    cnj = extract_cnj_from_anywhere(data)
    cnj_digits = regex_module.sub(r'\D', '', cnj or '')
    log(f"[CNJ][DEBUG] ✅ CNJ extraído para processo #{process_id}: '{cnj}' ({len(cnj_digits)} dígitos)")
    
    if RPA_EXPECT_CNJ:
        exp = re.sub(r"\D", "", RPA_EXPECT_CNJ)
        got = re.sub(r"\D", "", cnj)
        if exp and got and exp != got:
            log(f"[CNJ][WARN] detectado '{cnj}' difere de RPA_EXPECT_CNJ='{RPA_EXPECT_CNJ}' — usando o EXPECT")
            cnj = _cnj_normalize(RPA_EXPECT_CNJ)
    _must(bool(cnj), "numero_processo vazio")

    # 1) Tipo do processo = Eletrônico
    update_status("tipo_processo", "Selecionando tipo: Eletrônico", process_id=process_id)
    _ = await set_tipo_processo_virtual(page, want_virtual=True)
    await _settle(page, "radio:tipo")
    
    # 2) CNJ
    update_status("preenchendo_cnj", f"Preenchendo número do processo (CNJ, process_id=process_id): {cnj}")
    log(f"[CNJ] ═══ INICIANDO PREENCHIMENTO CNJ PARA PROCESSO #{process_id} ═══")
    await ensure_cnj_flag_on(page)
    
    # 🔧 FIX CRÍTICO: Aguardar campo CNJ aparecer no DOM após AJAX do tipo Eletrônico
    await _settle(page, "cnj_flag_settle")  # Espera adicional após marcar flag CNJ
    _must(await wait_for_cnj_container(page), "Campo CNJ não apareceu no DOM após AJAX")
    
    _must(await set_cnj_value(page, cnj), "Número do Processo (CNJ)")
    await _settle(page, "input:cnj")
    await ensure_cnj_still_present(page, cnj)
    log(f"✅ [FORM] CNJ preenchido: {cnj}")
    update_status("cnj_preenchido", f"CNJ preenchido com sucesso: {cnj}", process_id=process_id)
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # FIM DO FLUXO CNJ (2 etapas concluídas: Radio Sim + Textbox preenchido)
    # ═══════════════════════════════════════════════════════════════════════════════
    
    # 🔧 2025-11-27: AGUARDAR AUTOFILL - O eLaw preenche Estado/Comarca automaticamente via AJAX
    # Isso substitui o wait fixo de 2.5s por uma espera inteligente que verifica os campos
    autofill_ok = await wait_for_cnj_autofill(page, timeout_ms=12000, process_id=process_id)
    if not autofill_ok:
        log(f"[FORM][WARN] Campos automáticos não preenchidos - continuando mesmo assim...")
        # Espera adicional como fallback se autofill não detectou nada
        await page.wait_for_timeout(2000)

    # 3) Sistema Eletrônico
    update_status("aguardando_sistema_eletronico", "Aguardando dropdown Sistema Eletrônico ficar pronto...", process_id=process_id)
    await wait_for_select_ready(page, "SistemaEletronicoId", 1, 15000)  # Aumentado de 7s para 15s
    update_status("abrindo_sistema_eletronico", "Abrindo dropdown Sistema Eletrônico...", process_id=process_id)
    btn, cont = await _open_bs_and_get_container(page, "SistemaEletronicoId")
    sys_options = _clean_choices(await _collect_options_from_container(cont)) if cont else []
    if btn:
        try:
            await btn.press("Escape")
        except Exception:
            pass
    wanted_sys = (
        _best_match(sys_options, resolve_sistema_eletronico(data), prefer_words=["pje", "juizo 100", "e-proc", "projudi"], threshold=10)
        if sys_options
        else resolve_sistema_eletronico(data)
    )
    _must(
        await set_select_fuzzy_any(
            page,
            "SistemaEletronicoId",
            wanted_sys,
            fallbacks=sys_options[:6] if sys_options else ["PJE", "Juízo 100% Digital - PJE", "E-PROC", "PROJUDI"],
            prefer_words=["pje", "juizo", "e-proc", "projudi"],
        ),
        "Sistema Eletrônico",
    )
    await _settle(page, "select:sistema")
    await ensure_cnj_still_present(page, cnj)
    update_field_status("sistema_eletronico", "Sistema Eletrônico", wanted_sys)

    # 4) Número Processo Antigo
    old_num = (data.get("numero_processo_antigo") or "").strip()
    if old_num:
        await set_input_by_id(page, "NumeroProcessoAntigo", old_num, "Número Processo Antigo")
        await _settle(page, "input:cnj_old")
        update_field_status("numero_processo_antigo", "Número Processo Antigo", old_num)

    # 5) Área do Direito
    update_status("area_direito", "Preenchendo Área do Direito...", process_id=process_id)
    await wait_for_select_ready(page, "AreaDireitoId", 1, 15000)
    btn, cont = await _open_bs_and_get_container(page, "AreaDireitoId")
    area_options = _clean_choices(await _collect_options_from_container(cont)) if cont else []
    if btn:
        try:
            await btn.press("Escape")
        except Exception:
            pass
    wanted_area = _best_match(area_options, resolve_area_direito(data), threshold=10) if area_options else resolve_area_direito(data)
    _must(
        await set_select_fuzzy_any(page, "AreaDireitoId", wanted_area, fallbacks=(area_options[:8] if area_options else AREA_LIST)),
        "Área do Direito",
    )
    await _settle(page, "select:area")
    await ensure_cnj_still_present(page, cnj)
    update_field_status("area_direito", "Área do Direito", wanted_area)

    # 6) Estado/Comarca (auto OU manual se autofill falhou)
    # 🔧 2025-11-27: Refatorado para usar helper robusto force_select_bootstrap_by_text
    estado = ""
    comarca = ""
    
    # Primeiro, verificar se o autofill do eLaw preencheu Estado/Comarca
    if await wait_for_select_ready(page, "EstadoId", 1, 9000):
        estado = await _get_selected_text(page, "EstadoId")
        log(f"[FORM] Estado (autofill): '{estado}'")
    
    if await wait_for_select_ready(page, "CidadeId", 1, 5000):
        comarca = await _get_selected_text(page, "CidadeId")
        log(f"[FORM] Comarca (autofill): '{comarca}'")
    
    # Se autofill não preencheu, usar helper manual robusto
    estado_vazio = not estado or estado.lower() in ["selecione", "--", "---", ""]
    comarca_vazia = not comarca or comarca.lower() in ["selecione", "--", "---", ""]
    
    if estado_vazio or comarca_vazia:
        log(f"[FORM] Autofill falhou (Estado vazio: {estado_vazio}, Comarca vazia: {comarca_vazia}) - usando preenchimento manual...")
        estado_manual, comarca_manual = await select_estado_comarca_manual(page, cnj, data, process_id)
        
        # Usar valores manuais se autofill estava vazio
        if estado_vazio and estado_manual:
            estado = estado_manual
        if comarca_vazia and comarca_manual:
            comarca = comarca_manual
    
    # Log e status final
    if estado and comarca:
        update_status("localizacao_preenchida", f"✅ Localização: {estado} - {comarca}", process_id=process_id)
        update_field_status("estado", "Estado", estado)
        update_field_status("comarca", "Comarca", comarca)
    elif estado:
        update_status("localizacao_parcial", f"⚠️ Estado: {estado} (Comarca não preenchida)", process_id=process_id)
        update_field_status("estado", "Estado", estado)
    else:
        log(f"[FORM][WARN] Estado e Comarca não foram preenchidos - possível problema com o CNJ")

    # 7) Origem
    try:
        await wait_for_select_ready(page, "OrigemId", 1, 7000)
        btn, cont = await _open_bs_and_get_container(page, "OrigemId")
        origem_opts = _clean_choices(await _collect_options_from_container(cont)) if cont else []
        if btn:
            try:
                await btn.press("Escape")
            except Exception:
                pass
        area_sel = (await _get_selected_text(page, "AreaDireitoId")) or ""
        pdf_text = data.get("_pdf_text", "")
        heur = resolve_origem_final(data, area_sel, pdf_text)
        wanted = (
            _best_match(origem_opts, heur, prefer_words=(["trt"] if heur == "TRT" else ["tst"]), threshold=10) if origem_opts else heur
        )
        _must(
            await set_select_fuzzy_any(
                page,
                "OrigemId",
                wanted,
                fallbacks=(origem_opts[:8] if origem_opts else ["TRT", "TST", "TRF", "JF", "PROCON", "PREFEITURA", "RECEITA FEDERAL", "STJ", "STF", "ÓRGÃO ADMINISTRATIVO"]),
            ),
            "Origem",
        )
        await _settle(page, "select:origem")
    except Exception as e:
        log(f"[Origem][WARN] {e}")
    await ensure_cnj_still_present(page, cnj)

    # 8) Número do Órgão - COM FALLBACK COMPLETO
    try:
        pdf_text = data.get("_pdf_text", "")
        
        # 🔧 NOVO: Sistema universal de fallback (data → PDF extraction)
        num_orgao_raw = extract_field_with_full_fallback(
            field_name="numero_orgao",
            data=data,
            pdf_text=pdf_text,
            pdf_extractor=extract_numero_orgao_from_pdf,
            data_keys=["numero_orgao", "numero_jurisdicao", "orgao_numero"]
        )
        
        num_orgao = _coerce_numero_orgao(num_orgao_raw) if num_orgao_raw else ""
        
        if num_orgao:
            _must(await set_input_by_id(page, "NumeroJurisdicao", num_orgao, "Número do Órgão"), "Número do Órgão")
            try:
                await page.locator("#NumeroJurisdicao").press("Enter")
            except Exception:
                await page.evaluate(
                    """() => {
                  const el=document.getElementById('NumeroJurisdicao'); if(!el) return;
                  el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
                  el.dispatchEvent(new KeyboardEvent('keyup',{key:'Enter',bubbles:true}));
                  el.dispatchEvent(new Event('change',{bubbles:true}));
                  el.dispatchEvent(new Event('input',{bubbles:true}));
                }"""
                )
            await _settle(page, "input:num_orgao")
        else:
            log("[NumÓrgão][WARN] Número do órgão não encontrado em data nem PDF")
    except Exception as e:
        log(f"[NumÓrgão][WARN] {e}")

    # 9) Órgão (NaturezaId)
    try:
        await wait_for_select_ready(page, "NaturezaId", 1, 7000)
        btn, cont = await _open_bs_and_get_container(page, "NaturezaId")
        org_opts = _clean_choices(await _collect_options_from_container(cont)) if cont else []
        if btn:
            try:
                await btn.press("Escape")
            except Exception:
                pass
        origem_sel = (await _get_selected_text(page, "OrigemId")) or ""
        prefer = (
            ["Vara do Trabalho", "CEJUSC", "Núcleo de Justiça 4.0", "Turma"]
            if "trt" in norm(origem_sel)
            else ["Turma", "SDI", "Órgão Especial", "Tribunal Pleno", "Presidência"]
        )
        wanted = None
        for p in prefer:
            m = _best_match(org_opts, p, threshold=10)
            if m:
                wanted = m
                break
        if not wanted:
            wanted = org_opts[0] if org_opts else "Vara do Trabalho"
        _must(
            await set_select_fuzzy_any(page, "NaturezaId", wanted, fallbacks=org_opts[:8] if org_opts else None),
            "Órgão (NaturezaId)",
        )
        await _settle(page, "select:orgao")
    except Exception as e:
        log(f"[Órgão][WARN] {e}")

    # 10) Célula (EscritorioId)
    try:
        await wait_for_select_ready(page, "EscritorioId", 1, 8000)
        btn, cont = await _open_bs_and_get_container(page, "EscritorioId")
        cel_opts = _clean_choices(await _collect_options_from_container(cont)) if cont else []
        if btn:
            try:
                await btn.press("Escape")
            except Exception:
                pass

        pdf_text = data.get("_pdf_text", "")
        wanted, why = decide_celula_from_sources(data, pdf_text, cel_opts)
        if not cel_opts:
            try:
                btn2, cont2 = await _open_bs_and_get_container(page, "EscritorioId")
                cel_opts = _clean_choices(await _collect_options_from_container(cont2)) if cont2 else []
                if btn2:
                    try:
                        await btn2.press("Escape")
                    except Exception:
                        pass
                if cel_opts:
                    wanted, why = decide_celula_from_sources(data, pdf_text, cel_opts)
            except Exception:
                pass
        ok = await set_select_fuzzy_any(
            page,
            "EscritorioId",
            wanted,
            fallbacks=cel_opts[:8] if cel_opts else ["Trabalhista GPA", "Trabalhista Outros Clientes", "Em Segredo"],
        )
        _must(ok, "Célula")
        await _settle(page, "select:celula")
        sel_final = (await _get_selected_text(page, "EscritorioId") or "").strip()
        log(f"[CÉLULA] alvo='{wanted}' | selecionada='{sel_final}' | motivo: {why}")
    except Exception as e:
        log(f"[Célula][WARN] {e}")

    # 11) Foro (JuizadoId) - COM FALLBACK COMPLETO
    try:
        await wait_for_select_ready(page, "JuizadoId", 1, 7000)
        raw_opts = await page.evaluate(
            """sid=>{
          const el=document.getElementById(sid); if(!el||!el.options) return [];
          return [...el.options].map(o=>({text:(o.textContent||'').trim(), value:(o.value||'').trim()}));
        }""",
            "JuizadoId",
        ) or []
    except Exception:
        raw_opts = []
    
    try:
        foro_opts = _clean_choices([o["text"] for o in raw_opts])
        comarca = (await _get_selected_text(page, "CidadeId")) or ""
        
        # 🔧 NOVO: Sistema universal de fallback (data → PDF → dropdown options)
        pdf_text = data.get("_pdf_text", "")
        wanted = extract_field_with_full_fallback(
            field_name="foro",
            data=data,
            pdf_text=pdf_text,
            dropdown_options=foro_opts,
            pdf_extractor=extract_foro_from_pdf,
            data_keys=["foro", "juizado"],
            threshold=10
        )
        
        # Fallback: usa comarca se disponível
        if not wanted and comarca:
            wanted = _best_match(foro_opts, comarca, threshold=5) or comarca
            log(f"[Foro] Usando comarca: {wanted}")
        
        # Último fallback
        if not wanted:
            wanted = foro_opts[0] if foro_opts else "Foro Central"
            log(f"[Foro] Usando fallback final: {wanted}")
        
        # Preencher dropdown normalmente
        ok = await set_select_fuzzy_any(page, "JuizadoId", wanted, fallbacks=foro_opts[:8] if foro_opts else [comarca, "Foro Central"])
        if not ok:
            ok = await _set_native_select_fuzzy(page, "JuizadoId", wanted)
        _must(ok, "Foro (JuizadoId)")
        await _settle(page, "select:foro")
        
    except Exception as e:
        log(f"[Foro][WARN] {e}")

    # 12) Assunto (AreaProcessoId) - COM FALLBACK COMPLETO E GARANTIA DE PREENCHIMENTO
    assunto_preenchido = False
    assunto_wanted = None
    try:
        log(f"[Assunto] Iniciando preenchimento do Assunto (AreaProcessoId)...")
        await wait_for_select_ready(page, "AreaProcessoId", 1, 7000)
        btn, cont = await _open_bs_and_get_container(page, "AreaProcessoId")
        assunto_opts = _clean_choices(await _collect_options_from_container(cont)) if cont else []
        log(f"[Assunto] Opções do dropdown: {len(assunto_opts)} itens")
        if btn:
            try:
                await btn.press("Escape")
            except Exception:
                pass
        
        pdf_text = data.get("_pdf_text", "")
        assunto_wanted = extract_field_with_full_fallback(
            field_name="assunto",
            data=data,
            pdf_text=pdf_text,
            dropdown_options=assunto_opts,
            pdf_extractor=extract_assunto_from_pdf,
            data_keys=["assunto", "tema", "materia", "assunto_processo"],
            threshold=14
        )
        
        if not assunto_wanted and assunto_opts:
            assunto_wanted = _best_match(assunto_opts, "Reclamação Trabalhista", threshold=10) or assunto_opts[0]
            log(f"[Assunto] Usando fallback final: {assunto_wanted}")
        
        if assunto_wanted:
            ok = await set_select_fuzzy_any(page, "AreaProcessoId", assunto_wanted, fallbacks=assunto_opts[:8] if assunto_opts else None)
            if ok:
                await _settle(page, "select:assunto")
                update_field_status("assunto", "Assunto", assunto_wanted)
                assunto_preenchido = True
                log(f"[Assunto] ✅ Preenchido: {assunto_wanted}")
            else:
                log(f"[Assunto][WARN] Falha ao preencher com set_select_fuzzy_any")
    except Exception as e:
        log(f"[Assunto][WARN] Erro durante preenchimento: {e}")
    
    if not assunto_preenchido:
        log(f"[Assunto][RETRY] Tentando preenchimento de emergência...")
        try:
            await wait_for_select_ready(page, "AreaProcessoId", 1, 5000)
            fallback_assunto = data.get("assunto") or "Reclamação Trabalhista"
            ok = await set_select_fuzzy_any(page, "AreaProcessoId", fallback_assunto, 
                fallbacks=["Reclamação Trabalhista No Rito Sumaríssimo", "Reclamação Trabalhista", "Ação Trabalhista"])
            if ok:
                await _settle(page, "select:assunto")
                update_field_status("assunto", "Assunto", fallback_assunto)
                assunto_preenchido = True
                log(f"[Assunto] ✅ Preenchido via emergência: {fallback_assunto}")
        except Exception as e2:
            log(f"[Assunto][ERROR] Falha total: {e2}")

    # 13) Instância - COM FALLBACK COMPLETO
    try:
        ready = await wait_for_select_ready(page, INSTANCIA_SELECT_ID, 1, 9000)
        if not ready:
            try:
                await page.evaluate(
                    """(sid)=>{
                  const btn = document.querySelector(`button.btn.dropdown-toggle[data-id="${sid}"]`);
                  if(!btn) return;
                  const root = btn.closest('.input-group') || btn.closest('.bootstrap-select')?.parentElement;
                  const refresh = root && root.querySelector('.input-group-append .btn, .fa-sync, .fa-refresh, .fa-rotate-right');
                  if(refresh) (refresh.closest('.btn')||refresh).click();
                }""",
                    INSTANCIA_SELECT_ID,
                )
            except Exception:
                pass
            ready = await wait_for_select_ready(page, INSTANCIA_SELECT_ID, 1, 4000)

        inst_opts = []
        if ready:
            btn, cont = await _open_bs_and_get_container(page, INSTANCIA_SELECT_ID)
            inst_opts = _clean_choices(await _collect_options_from_container(cont)) if cont else []
            if btn:
                try:
                    await btn.press("Escape")
                except Exception:
                    pass
        
        # 🔧 NOVO: Sistema universal de fallback (data → PDF → dropdown options)
        pdf_text = data.get("_pdf_text", "")
        pick = extract_field_with_full_fallback(
            field_name="instancia",
            data=data,
            pdf_text=pdf_text,
            dropdown_options=inst_opts,
            pdf_extractor=extract_instancia_from_pdf,
            data_keys=["instancia", "grau"],
            threshold=10
        )

        # Fallback antigo: usar prefer list se nada funcionou
        if not pick and inst_opts:
            prefs = ["Primeira Instância", "1º Grau", "Primeiro Grau", "2º Grau", "Segunda Instância", "Superior"]
            for ptxt in prefs:
                m = _best_match(inst_opts, ptxt, threshold=10)
                if m:
                    pick = m
                    log(f"[Instância] Usando prefer list: {pick}")
                    break

        if not pick:
            for label_txt in ("Instância", "Instancia"):
                ok = await set_select_by_label_contains(page, label_txt, "Primeira Instância", fallbacks=["Primeira Instância", "1º Grau", "Primeiro Grau"])
                if ok:
                    pick = "Primeira Instância"
                    break

        if pick:
            _must(
                await set_select_fuzzy_any(page, INSTANCIA_SELECT_ID, pick, fallbacks=["Primeira Instância", "1º Grau", "Primeiro Grau", "2º Grau"]),
                "Instância",
            )
            await _settle(page, "select:instancia")
            update_field_status("instancia", "Instância", pick)
        else:
            log("[Instância][WARN] não foi possível determinar; mantendo em branco")
    except Exception as e:
        log(f"[Instância][WARN] {e}")

    # 14) NPC (opcional) - COM FALLBACK COMPLETO
    try:
        pdf_text = data.get("_pdf_text", "")
        
        # 🔧 NOVO: Sistema universal de fallback (data → PDF extraction)
        npc = extract_field_with_full_fallback(
            field_name="npc",
            data=data,
            pdf_text=pdf_text,
            pdf_extractor=extract_npc_from_pdf,
            data_keys=["npc", "numero_processo_antigo"]
        )
        
        if npc:
            await set_input_by_id(page, "NPC", npc, "NPC")
            await _settle(page, "input:npc")
    except Exception as e:
        log(f"[NPC][WARN] {e}")

    # 15) Classe/Objeto (quando houver campo à parte)
    try:
        ready = await wait_for_select_ready(page, "ClasseId", 1, 7000)
        if ready:
            btn, cont = await _open_bs_and_get_container(page, "ClasseId")
            obj_opts = _clean_choices(await _collect_options_from_container(cont)) if cont else []
            if btn:
                try:
                    await btn.press("Escape")
                except Exception:
                    pass
            wanted = None
            for k in ["objeto", "classe", "classe_processual"]:
                v = (data.get(k) or "").strip()
                if not v:
                    continue
                m = _best_match(obj_opts, v, threshold=15)
                if m:
                    wanted = m
                    break
            if wanted:
                ok = await set_select_fuzzy_any(page, "ClasseId", wanted, fallbacks=obj_opts[:6] if obj_opts else None)
                if ok:
                    await _settle(page, "select:classe")
    except Exception as e:
        log(f"[Classe][WARN] {e}")

    # 16) Tipo de Ação (⭐ garante execução) - COM GARANTIA DE PREENCHIMENTO
    tipo_acao_preenchido = False
    try:
        log(f"[TipoAção] Iniciando preenchimento do Tipo de Ação (TipoAcaoId)...")
        await wait_for_select_ready(page, "TipoAcaoId", 1, 9000)
        btn, cont = await _open_bs_and_get_container(page, "TipoAcaoId")
        tp_opts = _clean_choices(await _collect_options_from_container(cont)) if cont else []
        log(f"[TipoAção] Opções do dropdown: {len(tp_opts)} itens")
        if not tp_opts:
            try:
                await page.evaluate(
                    """(sid)=>{
                  const btn = document.querySelector(`button.btn.dropdown-toggle[data-id="${sid}"]`);
                  if(!btn) return;
                  const root = btn.closest('.input-group') || btn.closest('.bootstrap-select')?.parentElement;
                  const refresh = root && root.querySelector('.input-group-append .btn, .fa-sync, .fa-refresh, .fa-rotate-right');
                  if(refresh) (refresh.closest('.btn')||refresh).click();
                }""",
                    "TipoAcaoId",
                )
            except Exception:
                pass
            btn, cont = await _open_bs_and_get_container(page, "TipoAcaoId")
            tp_opts = _clean_choices(await _collect_options_from_container(cont)) if cont else []
            log(f"[TipoAção] Após refresh: {len(tp_opts)} itens")
        if btn:
            try:
                await btn.press("Escape")
            except Exception:
                pass

        assunto_sel = (await _get_selected_text(page, "AreaProcessoId")) or ""
        pdf_text = data.get("_pdf_text", "")
        wanted = pick_tipo_acao_smart(tp_opts, data, pdf_text, assunto_sel) if tp_opts else None
        if not wanted and tp_opts:
            wanted = tp_opts[0]
        if not wanted:
            wanted = data.get("sub_area_direito") or "Ação Trabalhista - Rito Sumaríssimo"

        if wanted:
            ok = await set_select_fuzzy_any(page, "TipoAcaoId", wanted, fallbacks=tp_opts[:10] if tp_opts else None)
            if ok:
                await _settle(page, "select:tipo_acao")
                update_field_status("tipo_acao", "Tipo de Ação", wanted)
                tipo_acao_preenchido = True
                log(f"[TipoAção] ✅ Preenchido: {wanted}")
            else:
                log(f"[TipoAção][WARN] Falha ao preencher com set_select_fuzzy_any")
        else:
            log("[TipoAção][WARN] não foi possível determinar valor")
    except Exception as e:
        log(f"[TipoAção][WARN] Erro durante preenchimento: {e}")
    
    if not tipo_acao_preenchido:
        log(f"[TipoAção][RETRY] Tentando preenchimento de emergência...")
        try:
            await wait_for_select_ready(page, "TipoAcaoId", 1, 5000)
            fallback_tipo = data.get("sub_area_direito") or "Ação Trabalhista - Rito Sumaríssimo"
            ok = await set_select_fuzzy_any(page, "TipoAcaoId", fallback_tipo,
                fallbacks=["Ação Trabalhista - Rito Sumaríssimo", "Ação Trabalhista", "Reclamação Trabalhista"])
            if ok:
                await _settle(page, "select:tipo_acao")
                update_field_status("tipo_acao", "Tipo de Ação", fallback_tipo)
                tipo_acao_preenchido = True
                log(f"[TipoAção] ✅ Preenchido via emergência: {fallback_tipo}")
        except Exception as e2:
            log(f"[TipoAção][ERROR] Falha total: {e2}")

    # 16.b) Objeto/Classe correlata (ObjetoId/Objeto/ClasseId) - COM GARANTIA DE PREENCHIMENTO
    objeto_preenchido = False
    try:
        log(f"[Objeto] Iniciando preenchimento do Objeto...")
        candidatos = ["ObjetoId", "Objeto", "ClasseId"]
        alvo_id = ""
        opts = []
        for sid in candidatos:
            if await wait_for_select_ready(page, sid, 1, 3000):
                btn, cont = await _open_bs_and_get_container(page, sid)
                opts = _clean_choices(await _collect_options_from_container(cont)) if cont else []
                if btn:
                    try:
                        await btn.press("Escape")
                    except Exception:
                        pass
                if opts:
                    alvo_id = sid
                    log(f"[Objeto] Encontrado campo: {alvo_id} com {len(opts)} opções")
                    break
        if alvo_id:
            assunto_sel = (await _get_selected_text(page, "AreaProcessoId")) or ""
            tipo_sel = (await _get_selected_text(page, "TipoAcaoId")) or ""
            pdf_text = data.get("_pdf_text", "")
            wanted = pick_objeto_smart(opts, data, pdf_text, assunto_sel, tipo_sel)
            if not wanted:
                wanted = data.get("objeto") or "Verbas rescisórias"
            ok = await set_select_fuzzy_any(page, alvo_id, wanted, fallbacks=opts[:10] if opts else None)
            if ok:
                await _settle(page, "select:objeto")
                update_field_status("objeto", "Objeto", wanted)
                objeto_preenchido = True
                log(f"[Objeto] ✅ Preenchido: {wanted}")
            else:
                log(f"[Objeto][WARN] Falha ao preencher com set_select_fuzzy_any")
        else:
            log(f"[Objeto][INFO] Nenhum campo de objeto encontrado (pode não existir neste formulário)")
    except Exception as e:
        log(f"[Objeto][WARN] Erro durante preenchimento: {e}")
    
    if not objeto_preenchido and alvo_id:
        log(f"[Objeto][RETRY] Tentando preenchimento de emergência...")
        try:
            fallback_objeto = data.get("objeto") or "Verbas rescisórias"
            ok = await set_select_fuzzy_any(page, alvo_id, fallback_objeto,
                fallbacks=["Verbas rescisórias", "Verbas Salariais", "Verbas Rescisórias e Salariais"])
            if ok:
                await _settle(page, "select:objeto")
                update_field_status("objeto", "Objeto", fallback_objeto)
                objeto_preenchido = True
                log(f"[Objeto] ✅ Preenchido via emergência: {fallback_objeto}")
        except Exception as e2:
            log(f"[Objeto][ERROR] Falha total: {e2}")

    # 17) A PARTIR DAQUI: ordem pedida (cliente→parte etc.)
    pdf_text = data.get("_pdf_text", "")
    inferred = infer_cliente_grupo_and_parte(pdf_text, data)

    # 17.1) Cliente (GrupoClienteId) - COM FALLBACK COMPLETO
    try:
        await wait_for_select_ready(page, GRUPO_CLIENTE_SELECT_ID, 1, 7000)
        btn, cont = await _open_bs_and_get_container(page, GRUPO_CLIENTE_SELECT_ID)
        grp_opts = _clean_choices(await _collect_options_from_container(cont)) if cont else []
        if btn:
            try:
                await btn.press("Escape")
            except Exception:
                pass
        
        # 🔧 NOVO: Sistema universal de fallback (data → PDF → dropdown options)
        target_group = extract_field_with_full_fallback(
            field_name="cliente",
            data=data,
            pdf_text=pdf_text,
            dropdown_options=grp_opts,
            pdf_extractor=extract_cliente_grupo_from_pdf,
            data_keys=["grupo", "cliente", "empresa"],
            threshold=12
        )
        
        # Fallback final se nada funcionou
        if not target_group:
            target_group = inferred.get("cliente_grupo") or "Grupo Pão de Açúcar"
            log(f"[CLIENTE] Usando fallback final: {target_group}")
        
        ok = await set_select_fuzzy_any(
            page, GRUPO_CLIENTE_SELECT_ID, target_group, fallbacks=grp_opts[:6] if grp_opts else ["Grupo Pão de Açúcar"]
        )
        if ok:
            log(f"✅ [CLIENTE] {GRUPO_CLIENTE_SELECT_ID}: '{target_group}'")
            update_status("cliente_preenchido", f"✅ Cliente preenchido: {target_group}", process_id=process_id)
            update_field_status("cliente", "Cliente/Grupo", target_group)
        await _settle(page, "select:grupo_cliente")
    except Exception as e:
        log(f"[Cliente (Grupo)][WARN] {e}")

    # 17.2) Parte Adversa (Tipo) — radio (1=Física, 2=Jurídica)
    try:
        tipo = inferred["parte_adversa_tipo"]
        val = "1" if norm(tipo) == "fisica" else "2"
        await set_radio_by_name(page, PARTE_ADVERSA_TIPO_NAME, val, "Parte Adversa (Tipo)")
        await _settle(page, "radio:adverso_tipo")
        update_field_status("parte_adversa_tipo", "Tipo Parte Adversa", tipo)
    except Exception as e:
        log(f"[Adverso Tipo][WARN] {e}")

    # 17.3) Posição Parte Interessada (PosicaoClienteId)
    try:
        await wait_for_select_ready(page, POSICAO_CLIENTE_SELECT_ID, 1, 7000)
        
        # 🔒 CRITICAL: Filtra placeholders inválidos ANTES de usar fallback do banco
        # Sincronizado com filtro em infer_cliente_grupo_and_parte() (linhas 2821-2836)
        INVALID_POSICOES = {"PARTES", "PARTE", ""}
        
        # Obtém posição inferida primeiro
        pos_from_inferred = inferred.get("posicao_parte_interessada", "")
        
        # Sanitiza fallback do banco (remove placeholders genéricos)
        pos_from_db = str(data.get("posicao_parte_interessada") or "").strip()
        if pos_from_db.upper() in INVALID_POSICOES:
            log(f"[POSIÇÃO][WARN] Placeholder genérico/inválido no banco ('{pos_from_db}') - ignorando fallback do DB")
            pos_from_db = ""  # ❌ Ignora "PARTES" do banco
        
        # Usa inferência → banco sanitizado → fallback "RECLAMADO"
        pos_raw = pos_from_inferred or pos_from_db or "RECLAMADO"
        
        # Normaliza para o label oficial do eLaw
        pos_target = normalize_posicao(pos_raw)
        log(f"[POSIÇÃO] Raw: '{pos_raw}' -> Normalizado: '{pos_target}'")
        
        # Tenta obter o ID do eLaw diretamente do mapeamento
        pos_id = get_posicao_id(pos_target)
        
        if pos_id:
            # Se temos o ID, tenta selecionar diretamente
            log(f"[POSIÇÃO] Usando ID do mapeamento: {pos_id} ({pos_target})")
            try:
                sel = f"#{POSICAO_CLIENTE_SELECT_ID}"
                await page.select_option(sel, value=pos_id, timeout=SHORT_TIMEOUT_MS)
                log(f"✅ [POSIÇÃO] Selecionado diretamente: ID={pos_id} ({pos_target})")
                update_field_status("posicao", "Posição Cliente", pos_target)
                ok = True
            except Exception as e:
                log(f"[POSIÇÃO][WARN] Falha ao selecionar por ID, tentando fuzzy: {e}")
                ok = False
        else:
            ok = False
        
        # Fallback: usa fuzzy matching se não conseguiu por ID
        if not ok:
            btn, cont = await _open_bs_and_get_container(page, POSICAO_CLIENTE_SELECT_ID)
            pos_opts = _clean_choices(await _collect_options_from_container(cont)) if cont else []
            if btn:
                try:
                    await btn.press("Escape")
                except Exception:
                    pass
            ok = await set_select_fuzzy_any(
                page, POSICAO_CLIENTE_SELECT_ID, pos_target, 
                fallbacks=pos_opts[:8] if pos_opts else ["RECLAMADO", "RECLAMANTE"]
            )
            if ok:
                log(f"[POSIÇÃO] Posição Parte Interessada (fuzzy): '{pos_target}'")
                update_field_status("posicao", "Posição Cliente", pos_target)
        
        await _settle(page, "select:posicao_cliente")
    except Exception as e:
        log(f"[Posição Interessada][WARN] {e}")

    # 17.4) Parte Adversa (Nome)
    try:
        adv_nome = inferred["parte_adversa_nome"] or data.get("parte_adversa_nome") or ""
        if adv_nome:
            await set_input_by_id(page, PARTE_ADVERSA_NOME_INPUT_ID, adv_nome, "Parte Adversa (Nome)")
            log(f"✅ [PARTE ADVERSA] Nome preenchido: {adv_nome}")
            update_status("parte_adversa_preenchida", f"✅ Parte adversa preenchida: {adv_nome}", process_id=process_id)
            update_field_status("parte_adversa_nome", "Nome Parte Adversa", adv_nome)
            await _settle(page, "input:adverso_nome")
    except Exception as e:
        log(f"[Adverso Nome][WARN] {e}")

    # 17.4.1) UF OAB Advogado Adverso (dropdown)
    try:
        # Mapeamento de UFs para valores do dropdown AdvogadoAdversoEstadoOAB
        UF_OAB_MAP = {
            "AC": "27", "ACRE": "27",
            "AL": "23", "ALAGOAS": "23",
            "AP": "26", "AMAPA": "26", "AMAPÁ": "26",
            "AM": "21", "AMAZONAS": "21",
            "BA": "8", "BAHIA": "8",
            "CE": "19", "CEARA": "19", "CEARÁ": "19",
            "DF": "12", "DISTRITO FEDERAL": "12",
            "ES": "20", "ESPIRITO SANTO": "20", "ESPÍRITO SANTO": "20",
            "GO": "15", "GOIAS": "15", "GOIÁS": "15",
            "MA": "25", "MARANHAO": "25", "MARANHÃO": "25",
            "MT": "14", "MATO GROSSO": "14",
            "MS": "13", "MATO GROSSO DO SUL": "13",
            "MG": "7", "MINAS GERAIS": "7",
            "PA": "22", "PARA": "22", "PARÁ": "22",
            "PB": "10", "PARAIBA": "10", "PARAÍBA": "10",
            "PR": "5", "PARANA": "5", "PARANÁ": "5",
            "PE": "9", "PERNAMBUCO": "9",
            "PI": "24", "PIAUI": "24", "PIAUÍ": "24",
            "RJ": "3", "RIO DE JANEIRO": "3",
            "RN": "11", "RIO GRANDE DO NORTE": "11",
            "RS": "4", "RIO GRANDE DO SUL": "4",
            "RO": "16", "RONDONIA": "16", "RONDÔNIA": "16",
            "RR": "17", "RORAIMA": "17",
            "SC": "6", "SANTA CATARINA": "6",
            "SP": "18", "SAO PAULO": "18", "SÃO PAULO": "18",
            "SE": "28", "SERGIPE": "28",
            "TO": "2", "TOCANTINS": "2",
        }
        
        # Tentar obter UF do advogado adverso do data (se extraído)
        uf_adv = data.get("advogado_adverso_uf", "") or data.get("uf_oab_adverso", "")
        
        # Se não tiver UF específica, usar a UF do processo como fallback
        if not uf_adv:
            uf_processo = data.get("uf", "") or data.get("estado", "")
            if uf_processo:
                uf_adv = uf_processo.upper().strip()
                log(f"[UF OAB Adverso] Usando UF do processo como fallback: {uf_adv}")
        
        # Se ainda não tiver, usar SP como default (mais comum em trabalhistas)
        if not uf_adv:
            uf_adv = "SP"
            log("[UF OAB Adverso] Usando SP como default")
        
        uf_adv_upper = uf_adv.upper().strip()
        uf_value = UF_OAB_MAP.get(uf_adv_upper, "18")  # Default SP (18)
        
        log(f"[UF OAB Adverso] Selecionando UF: {uf_adv_upper} (value={uf_value})")
        
        # Tentar selecionar por value no dropdown bootstrap-select
        try:
            dropdown_id = "AdvogadoAdversoEstadoOAB"
            
            # Primeiro, clicar para abrir o dropdown
            btn_selector = f"button[data-id='{dropdown_id}'], .bootstrap-select[data-id='{dropdown_id}'] button"
            btn = page.locator(btn_selector).first
            if await btn.count() > 0 and await btn.is_visible(timeout=2000):
                await btn.click()
                await short_sleep_ms(500)
                
                # Procurar pela opção com o texto da UF
                uf_text = uf_adv_upper if len(uf_adv_upper) == 2 else uf_adv_upper
                # Tentar encontrar por texto parcial
                for search_text in [uf_text, "SÃO PAULO", "SAO PAULO", "SP"]:
                    opt_selector = f".dropdown-menu li a:has-text('{search_text}')"
                    opt = page.locator(opt_selector).first
                    if await opt.count() > 0 and await opt.is_visible(timeout=500):
                        await opt.click()
                        log(f"[UF OAB Adverso] ✅ Selecionado: {search_text}")
                        update_field_status("uf_oab_adverso", "UF OAB Advogado Adverso", search_text)
                        break
                else:
                    # Fallback: tentar selecionar por value diretamente
                    await page.evaluate(f"""
                        const sel = document.getElementById('{dropdown_id}');
                        if (sel) {{
                            sel.value = '{uf_value}';
                            sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                        }}
                    """)
                    log(f"[UF OAB Adverso] ✅ Selecionado por value: {uf_value}")
                    update_field_status("uf_oab_adverso", "UF OAB Advogado Adverso", uf_adv_upper)
            else:
                # Fallback direto via JavaScript
                await page.evaluate(f"""
                    const sel = document.getElementById('{dropdown_id}');
                    if (sel) {{
                        sel.value = '{uf_value}';
                        sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                        // Atualizar bootstrap-select se disponível
                        if (window.jQuery && window.jQuery.fn.selectpicker) {{
                            window.jQuery(sel).selectpicker('refresh');
                        }}
                    }}
                """)
                log(f"[UF OAB Adverso] ✅ Selecionado via JS: {uf_value} ({uf_adv_upper})")
                update_field_status("uf_oab_adverso", "UF OAB Advogado Adverso", uf_adv_upper)
            
            await _settle(page, f"#{dropdown_id}")
        except Exception as e:
            log(f"[UF OAB Adverso][WARN] Erro ao selecionar dropdown: {e}")
    except Exception as e:
        log(f"[UF OAB Adverso][WARN] {e}")

    # 17.5) Parte Interessada (ClienteId) - COM FALLBACK COMPLETO
    try:
        await wait_for_select_ready(page, PARTE_INTERESSADA_SELECT_ID, 1, 8000)
        btn, cont = await _open_bs_and_get_container(page, PARTE_INTERESSADA_SELECT_ID)
        cli_opts = _clean_choices(await _collect_options_from_container(cont)) if cont else []
        if btn:
            try:
                await btn.press("Escape")
            except Exception:
                pass
        
        # 🔧 NOVO: Sistema universal de fallback (data → PDF → dropdown options)
        pick = extract_field_with_full_fallback(
            field_name="parte_interessada",
            data=data,
            pdf_text=pdf_text,
            dropdown_options=cli_opts,
            pdf_extractor=extract_parte_interessada_from_pdf,
            data_keys=["parte_interessada", "cliente", "empresa"],
            threshold=15
        )
        
        # Fallback: usa inferred se nada funcionou
        if not pick:
            pit = inferred.get("parte_interessada_nome", "")
            if pit:
                pick = _best_match(cli_opts, pit, threshold=10) or pit
                log(f"[Parte Interessada] Usando inferred: {pick}")
        
        # Último fallback: cliente_grupo ou primeira opção
        if not pick and cli_opts:
            g = inferred.get("cliente_grupo", "")
            if g:
                pick = _best_match(cli_opts, g, threshold=10)
                log(f"[Parte Interessada] Tentando cliente_grupo: {pick}")
            if not pick:
                pick = cli_opts[0]
                log(f"[Parte Interessada] Usando primeira opção: {pick}")
        
        if pick:
            ok = await set_select_fuzzy_any(
                page, PARTE_INTERESSADA_SELECT_ID, pick, fallbacks=cli_opts[:10] if cli_opts else None
            )
            if ok:
                log(f"[PARTE INTERESSADA] {PARTE_INTERESSADA_SELECT_ID}: '{pick}'")
                update_field_status("parte_interessada", "Parte Interessada", pick)
        await _settle(page, "select:parte_interessada")
    except Exception as e:
        log(f"[Parte Interessada][WARN] {e}")

    # 17.5.1) Data de Distribuição
    try:
        data_distribuicao = data.get("data_distribuicao", "")
        log(f"[DEBUG][Data Distribuição] Valor recebido: {repr(data_distribuicao)} (tipo: {type(data_distribuicao).__name__})")
        if data_distribuicao and isinstance(data_distribuicao, str) and data_distribuicao.strip():
            log(f"[DEBUG][Data Distribuição] Tentando preencher...")
            # Tentar múltiplos IDs possíveis para o campo de data de distribuição
            date_field_ids = ["DataDistribuicao", "DataRecebimento", "DataAuditoria", "DataCadastro"]
            filled = False
            for field_id in date_field_ids:
                try:
                    field = page.locator(f"#{field_id}").first
                    if await field.count() > 0:
                        ok = await set_date_field_by_id(page, field_id, data_distribuicao, f"Data de Distribuição ({field_id})")
                        if ok:
                            update_field_status("data_distribuicao", "Data de Distribuição", data_distribuicao)
                            filled = True
                            log(f"[Data Distribuição] ✅ Preenchido em #{field_id}: {data_distribuicao}")
                            await _settle(page, f"#{field_id}")
                            break
                except Exception as e:
                    log(f"[Data Distribuição][WARN] Tentativa {field_id}: {e}")
                    continue
            
            if not filled:
                log(f"[Data Distribuição][WARN] Nenhum campo de data de distribuição encontrado")
        else:
            log(f"[DEBUG][Data Distribuição] Pulado - validação falhou")
    except Exception as e:
        log(f"[Data Distribuição][WARN] {e}")

    # 17.6) Campos Trabalhistas
    update_status("preenchendo_dados_trabalhistas", "Preenchendo dados trabalhistas...", process_id=process_id)
    
    # 17.6.1) Data de Admissão
    try:
        data_admissao = data.get("data_admissao", "")
        log(f"[DEBUG][Data Admissão] Valor recebido: {repr(data_admissao)} (tipo: {type(data_admissao).__name__})")
        if data_admissao and isinstance(data_admissao, str) and data_admissao.strip():
            log(f"[DEBUG][Data Admissão] Tentando preencher...")
            ok = await set_date_field_by_id(page, "DataAdmissao", data_admissao, "Data de Admissão")
            if ok:
                update_field_status("data_admissao", "Data de Admissão", data_admissao)
            await _settle(page, "#DataAdmissao")
        else:
            log(f"[DEBUG][Data Admissão] Pulado - validação falhou")
    except Exception as e:
        log(f"[Data Admissão][WARN] {e}")
    
    # 17.6.2) Data de Demissão
    try:
        data_demissao = data.get("data_demissao", "")
        log(f"[DEBUG][Data Demissão] Valor recebido: {repr(data_demissao)} (tipo: {type(data_demissao).__name__})")
        if data_demissao and isinstance(data_demissao, str) and data_demissao.strip():
            log(f"[DEBUG][Data Demissão] Tentando preencher...")
            ok = await set_date_field_by_id(page, "DataDemissao", data_demissao, "Data de Demissão")
            if ok:
                update_field_status("data_demissao", "Data de Demissão", data_demissao)
            await _settle(page, "#DataDemissao")
        else:
            log(f"[DEBUG][Data Demissão] Pulado - validação falhou")
    except Exception as e:
        log(f"[Data Demissão][WARN] {e}")
    
    # 17.6.3) Motivo de Demissão
    try:
        motivo_demissao = data.get("motivo_demissao", "")
        if motivo_demissao and isinstance(motivo_demissao, str) and motivo_demissao.strip():
            ok = await set_text_field_by_id(page, "MotivoDemissao", motivo_demissao, "Motivo de Demissão")
            if ok:
                update_field_status("motivo_demissao", "Motivo de Demissão", motivo_demissao)
            await _settle(page, "#MotivoDemissao")
    except Exception as e:
        log(f"[Motivo Demissão][WARN] {e}")
    
    # 17.6.4) Salário (opcional - usa função de texto direto sem conversão para inteiro)
    try:
        salario = data.get("salario", "")
        log(f"[DEBUG][Salário] Valor recebido: {repr(salario)} (tipo: {type(salario).__name__})")
        # Validar se é string não vazia antes de tentar preencher
        if salario and isinstance(salario, str) and salario.strip() and not salario.strip().startswith('{'):
            # Normalizar: remover "R$" e espaços, manter formato brasileiro "1.516,00"
            salario_clean = salario.strip().replace("R$", "").replace(" ", "").strip()
            log(f"[DEBUG][Salário] Valor normalizado: {repr(salario_clean)}")
            
            # Usar função de texto direto (NÃO usar set_valor_causa_any que converte para dígitos)
            ok = await set_text_field_by_id(page, "Salario", salario_clean, "Salário")
            
            if ok:
                # Verificar se valor foi preenchido corretamente (não zerado)
                try:
                    el = page.locator("#Salario").first
                    if await el.count() > 0:
                        valor_preenchido = await el.input_value()
                        log(f"[Salário] Valor preenchido no campo: {repr(valor_preenchido)}")
                        
                        # Warning se foi zerado incorretamente
                        if valor_preenchido and valor_preenchido.strip() in ("0", "0,00", "0.00") and re.search(r"[1-9]", salario_clean):
                            log(f"[Salário][WARN] ⚠️ Campo foi zerado! Esperado: {salario_clean}, Obtido: {valor_preenchido}")
                except Exception as e:
                    log(f"[Salário][WARN] Erro ao verificar valor preenchido: {e}")
                
                update_field_status("salario", "Salário", salario_clean)
            await _settle(page, "#Salario")
        else:
            log("[Salário] Campo vazio ou inválido - pulando")
    except Exception as e:
        log(f"[Salário][WARN] {e}")
    
    # 17.6.5) Cargo
    try:
        cargo = data.get("cargo_funcao", "") or data.get("cargo", "")
        log(f"[DEBUG][Cargo] Valor recebido: {repr(cargo)} (tipo: {type(cargo).__name__})")
        if cargo and isinstance(cargo, str) and cargo.strip():
            log(f"[DEBUG][Cargo] Tentando preencher...")
            ok = await set_text_field_by_id(page, "Cargo", cargo, "Cargo")
            if ok:
                update_field_status("cargo", "Cargo", cargo)
            await _settle(page, "#Cargo")
        else:
            log(f"[DEBUG][Cargo] Pulado - validação falhou")
    except Exception as e:
        log(f"[Cargo][WARN] {e}")
    
    # 17.6.6) Empregador
    try:
        empregador = data.get("empregador", "")
        if empregador and isinstance(empregador, str) and empregador.strip():
            ok = await set_text_field_by_id(page, "Empregador", empregador, "Empregador")
            if ok:
                update_field_status("empregador", "Empregador", empregador)
            await _settle(page, "#Empregador")
    except Exception as e:
        log(f"[Empregador][WARN] {e}")
    
    # 17.6.7) Local de Prestação de Serviço
    try:
        local_trabalho = data.get("local_trabalho", "")
        if local_trabalho and isinstance(local_trabalho, str) and local_trabalho.strip():
            ok = await set_text_field_by_id(page, "LocalPrestacaoServico", local_trabalho, "Local de Prestação de Serviço")
            if ok:
                update_field_status("local_trabalho", "Local de Prestação de Serviço", local_trabalho)
            await _settle(page, "#LocalPrestacaoServico")
    except Exception as e:
        log(f"[Local Trabalho][WARN] {e}")
    
    # 17.6.8) PIS
    try:
        pis = data.get("pis", "")
        if pis and isinstance(pis, str) and pis.strip():
            ok = await set_text_field_by_id(page, "Pis", pis, "PIS")
            if ok:
                update_field_status("pis", "PIS", pis)
            await _settle(page, "#Pis")
    except Exception as e:
        log(f"[PIS][WARN] {e}")
    
    # 17.6.9) CTPS
    try:
        ctps = data.get("ctps", "")
        if ctps and isinstance(ctps, str) and ctps.strip():
            ok = await set_text_field_by_id(page, "Cts", ctps, "CTPS")
            if ok:
                update_field_status("ctps", "CTPS", ctps)
            await _settle(page, "#Cts")
    except Exception as e:
        log(f"[CTPS][WARN] {e}")

    # 17.7) Valor da Causa - COM FALLBACK COMPLETO
    try:
        # 🔧 NOVO: Sistema universal de fallback (data → PDF extraction)
        valor = extract_field_with_full_fallback(
            field_name="valor_causa",
            data=data,
            pdf_text=pdf_text,
            pdf_extractor=extract_valor_causa_from_pdf,
            data_keys=["valor_causa", "valor"]
        )
        
        # Fallback final: valor padrão do ambiente
        if not valor:
            valor = os.getenv("RPA_VALOR_CAUSA_DEFAULT", "1.000,00")
            log(f"[Valor Causa] Usando valor padrão do ambiente: {valor}")
        
        if valor:
            ok_vc = await set_valor_causa_any(page, valor)
            if not ok_vc:
                await set_input_by_id(page, VALOR_CAUSA_INPUT_ID, valor, "Valor da Causa")
            await _settle(page, "input:valor_causa")
            update_field_status("valor_causa", "Valor da Causa", valor)
    except Exception as e:
        log(f"[Valor Causa][WARN] {e}")

    # 17.8) Cadastro de Primeira Audiência (condicional)
    try:
        cadastrar_audiencia = data.get("cadastrar_primeira_audiencia", False)
        audiencia_inicial = data.get("audiencia_inicial", "")
        
        # Debug: Log dos valores recebidos
        log(f"[AUDIÊNCIA][DEBUG] cadastrar_primeira_audiencia={cadastrar_audiencia} (type: {type(cadastrar_audiencia).__name__})")
        log(f"[AUDIÊNCIA][DEBUG] audiencia_inicial='{audiencia_inicial}' (type: {type(audiencia_inicial).__name__})")
        
        if cadastrar_audiencia and audiencia_inicial:
            update_status("cadastrando_audiencia", "Cadastrando primeira audiência...", process_id=process_id)
            log(f"[AUDIÊNCIA] Cadastrando primeira audiência: {audiencia_inicial}")
            
            # Marcar rádio "Sim" para "Deseja cadastrar a primeira Audiência?"
            # IMPORTANTE: O formulário usa iCheck, que esconde o input real (opacity: 0)
            # Precisamos clicar no label ou forçar click no input
            try:
                # Estratégia 1: Tentar clicar no label que contém o input "Sim"
                label_selector = 'label.radio-inline:has(input[name="IsDesejaCadastrarPrimeiraAudiencia"][value="True"])'
                
                try:
                    await page.wait_for_selector(label_selector, state="visible", timeout=5000)
                    await page.locator(label_selector).click(timeout=3000)
                    log("[AUDIÊNCIA] Rádio 'Sim' marcado via label")
                except Exception as e_label:
                    # Estratégia 2: Forçar click no input invisível do iCheck
                    log(f"[AUDIÊNCIA][WARN] Falha ao clicar no label: {e_label}, tentando force click...")
                    input_selector = 'input[name="IsDesejaCadastrarPrimeiraAudiencia"][value="True"]'
                    await page.locator(input_selector).click(force=True, timeout=3000)
                    log("[AUDIÊNCIA] Rádio 'Sim' marcado via force click")
                
                update_field_status("cadastrar_audiencia", "Deseja cadastrar primeira audiência?", "Sim")
                
                # Aguardar campos de data e hora aparecerem (aumentado para 1.5 segundos)
                await page.wait_for_timeout(1500)  # 1.5s para garantir que iCheck processe e mostre os campos
                
                # Extrair data e hora da audiência inicial (formato esperado: "15/02/2025 às 10:00h" ou "15/02/2025 10:00")
                import re
                audiencia_str = str(audiencia_inicial)
                
                # Regex para extrair data (DD/MM/YYYY)
                match_data = re.search(r'(\d{1,2}[/-]\d{1,2}[/-]\d{4})', audiencia_str)
                data_audiencia = match_data.group(1).replace('-', '/') if match_data else ""
                
                # Regex para extrair hora (HH:MM)
                match_hora = re.search(r'(\d{1,2}):(\d{2})', audiencia_str)
                hora_audiencia = f"{match_hora.group(1)}:{match_hora.group(2)}" if match_hora else ""
                
                # Preencher campo Data da Audiência usando calendário
                if data_audiencia:
                    data_input_id = "DataPrimeiraAudiencia"
                    
                    # Extrair dia, mês e ano da data (formato DD/MM/YYYY)
                    parts = data_audiencia.split('/')
                    if len(parts) == 3:
                        dia = int(parts[0])
                        mes = int(parts[1])
                        ano = int(parts[2])
                        
                        # Clicar no campo para abrir o calendário
                        await page.locator(f"#{data_input_id}").click(timeout=3000)
                        log(f"[AUDIÊNCIA] Calendário aberto para data: {data_audiencia}")
                        await page.wait_for_timeout(500)  # Aguardar calendário aparecer
                        
                        # Mapear número do mês para nome em português
                        meses_pt = {
                            1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril",
                            5: "Maio", 6: "Junho", 7: "Julho", 8: "Agosto",
                            9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro"
                        }
                        mes_nome = meses_pt.get(mes, "")
                        
                        # Tentar navegar até o mês/ano correto no calendário
                        # Verifica se o cabeçalho do calendário mostra o mês/ano correto
                        max_tentativas = 12
                        for tentativa in range(max_tentativas):
                            try:
                                # Verifica o mês/ano atual mostrado no calendário
                                # O formato pode ser "Novembro 2025", "Dezembro 2025", etc.
                                calendario_header = await page.locator(".datepicker-days th.datepicker-switch").text_content(timeout=2000)
                                
                                if calendario_header and mes_nome in calendario_header and str(ano) in calendario_header:
                                    # Mês/ano correto, podemos clicar no dia
                                    log(f"[AUDIÊNCIA] Calendário no mês correto: {calendario_header}")
                                    break
                                else:
                                    # Precisa navegar - clica na seta direita para avançar mês
                                    await page.locator(".datepicker-days th.next").click(timeout=2000)
                                    await page.wait_for_timeout(200)
                            except Exception:
                                break  # Se falhar, tenta clicar no dia mesmo assim
                        
                        # Clicar no dia específico no calendário
                        # O calendário usa <td class="day"> com o número do dia
                        dia_selector = f".datepicker-days td.day:has-text(\"{dia}\"):not(.old):not(.new)"
                        try:
                            await page.locator(dia_selector).first.click(timeout=3000)
                            log(f"[AUDIÊNCIA] Data selecionada no calendário: {dia}/{mes}/{ano}")
                            update_field_status("data_audiencia", "Data da Audiência", data_audiencia)
                            await page.wait_for_timeout(300)
                        except Exception as e_dia:
                            log(f"[AUDIÊNCIA][WARN] Erro ao clicar no dia {dia}: {e_dia}")
                            # Fallback: tentar preencher diretamente o campo
                            await page.locator(f"#{data_input_id}").fill(data_audiencia)
                            await page.locator(f"#{data_input_id}").press("Tab")
                            log(f"[AUDIÊNCIA] Data preenchida via fallback: {data_audiencia}")
                            update_field_status("data_audiencia", "Data da Audiência", data_audiencia)
                    else:
                        log(f"[AUDIÊNCIA][WARN] Formato de data inválido: {data_audiencia}")
                        update_field_status("data_audiencia", "Data da Audiência", f"ERRO: formato inválido {data_audiencia}")
                
                # Preencher campo Hora da Audiência
                if hora_audiencia:
                    hora_input_id = "HoraPrimeiraAudiencia"
                    await page.locator(f"#{hora_input_id}").click(timeout=3000)
                    await page.locator(f"#{hora_input_id}").fill(hora_audiencia)
                    log(f"[AUDIÊNCIA] Hora preenchida: {hora_audiencia}")
                    update_field_status("hora_audiencia", "Hora da Audiência", hora_audiencia)
                    
                    await page.wait_for_timeout(200)
                
                # ✅ Preencher Link da Audiência (Zoom/Meet/Teams)
                link_audiencia_val = data.get("link_audiencia", "")
                if link_audiencia_val:
                    try:
                        link_input_id = "LinkAudiencia"
                        await page.locator(f"#{link_input_id}").click(timeout=3000)
                        await page.locator(f"#{link_input_id}").fill(link_audiencia_val)
                        log(f"[AUDIÊNCIA] Link preenchido: {link_audiencia_val[:60]}...")
                        update_field_status("link_audiencia", "Link da Audiência", "✓")
                        await page.wait_for_timeout(200)
                    except Exception as e_link:
                        log(f"[AUDIÊNCIA][WARN] Erro ao preencher link: {e_link}")
                
                # ✅ Selecionar SubTipo da Audiência (dropdown) - OBRIGATÓRIO no eLaw
                subtipo_audiencia_val = data.get("subtipo_audiencia", "")
                
                # Se não tiver subtipo extraído, usar valor default (mais comum em processos trabalhistas)
                if not subtipo_audiencia_val:
                    subtipo_audiencia_val = "Audiência Inicial Una (IU)"
                    log(f"[AUDIÊNCIA] Subtipo não extraído do PDF - usando default: {subtipo_audiencia_val}")
                
                try:
                    await select_from_bootstrap_dropdown(
                        page,
                        select_id="SubTipoPrimeiraAudienciaId",
                        search_text=subtipo_audiencia_val,
                        field_label="SubTipo da Audiência"
                    )
                    log(f"[AUDIÊNCIA] Subtipo selecionado: {subtipo_audiencia_val}")
                    update_field_status("subtipo_audiencia", "SubTipo da Audiência", subtipo_audiencia_val)
                except Exception as e_subtipo:
                    log(f"[AUDIÊNCIA][WARN] Erro ao selecionar subtipo: {e_subtipo}")
                
                # ✅ Selecionar Envolvidos da Audiência (dropdown)
                envolvido_audiencia_val = data.get("envolvido_audiencia", "")
                if envolvido_audiencia_val:
                    try:
                        await select_from_bootstrap_dropdown(
                            page,
                            select_id="EnvolvidoPrimeiraAudienciaId",
                            search_text=envolvido_audiencia_val,
                            field_label="Envolvidos da Audiência"
                        )
                        log(f"[AUDIÊNCIA] Envolvidos selecionado: {envolvido_audiencia_val}")
                        update_field_status("envolvido_audiencia", "Envolvidos da Audiência", envolvido_audiencia_val)
                    except Exception as e_envolvido:
                        log(f"[AUDIÊNCIA][WARN] Erro ao selecionar envolvido: {e_envolvido}")
                
                log("[AUDIÊNCIA] Primeira audiência cadastrada com sucesso")
                
            except Exception as e_radio:
                log(f"[AUDIÊNCIA][WARN] Erro ao marcar rádio ou preencher campos: {e_radio}")
                update_field_status("cadastrar_audiencia", "Deseja cadastrar primeira audiência?", f"ERRO: {e_radio}")
        else:
            # Se não há audiência para cadastrar, deixar "Não" marcado (padrão)
            log("[AUDIÊNCIA] Sem audiência inicial para cadastrar - mantendo 'Não' marcado")
            
    except Exception as e:
        log(f"[AUDIÊNCIA][WARN] {e}")

    # 18) Estratégia (opcional)
    try:
        if await page.locator(f"button.btn.dropdown-toggle[data-id='{ESTRATEGIA_SELECT_ID}']").count() > 0:
            btn, cont = await _open_bs_and_get_container(page, ESTRATEGIA_SELECT_ID)
            est_opts = _clean_choices(await _collect_options_from_container(cont)) if cont else []
            if btn:
                try:
                    await btn.press("Escape")
                except Exception:
                    pass
            est_txt = (data.get("estrategia") or "")
            if not est_txt and pdf_text:
                m = re.search(r"(?i)\bestratégia\b[:\-–]\s*([^\n]{3,80})", pdf_text)
                if m:
                    est_txt = m.group(1)
            if est_txt:
                await set_select_fuzzy_any(page, ESTRATEGIA_SELECT_ID, est_txt, fallbacks=est_opts[:6] if est_opts else None)
                await _settle(page, "select:estrategia")
    except Exception as e:
        log(f"[Estratégia][WARN] {e}")

    # snapshot parcial
    try:
        png = SCREENSHOT_DIR / "form_parcial_preenchido.png"
        await page.screenshot(path=str(png), full_page=True)
        log(f"[SHOT] parcial: {png}")
    except Exception:
        pass

    # sanity final: CNJ ligado + presente
    try:
        await ensure_cnj_flag_on(page)
        await ensure_cnj_still_present(page, cnj)
    except Exception:
        pass

    # Screenshot ANTES de salvar para preservar formulário preenchido
    screenshot_before_path = None
    try:
        update_status("capturando_screenshot_before", "Capturando screenshot do formulário preenchido...", process_id=process_id)
        if process_id:
            # 🔧 2025-11-27: Aguardar página estabilizar ANTES de capturar screenshot
            try:
                await page.wait_for_function("document.readyState === 'complete'", timeout=5000)
                await page.wait_for_timeout(500)  # Extra buffer para elementos visuais
            except Exception:
                pass  # Continuar mesmo se timeout
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_filename = f"process_{process_id}_{timestamp}_before.png"
            
            # Salvar em static/rpa_screenshots para servir via Flask
            screenshot_static_dir = Path("static") / "rpa_screenshots"
            screenshot_static_dir.mkdir(parents=True, exist_ok=True)
            screenshot_full_path = screenshot_static_dir / screenshot_filename
            
            await page.screenshot(path=str(screenshot_full_path), full_page=True)
            log(f"[SCREENSHOT BEFORE] Formulário preenchido salvo em: {screenshot_full_path}")
            send_screenshot_to_monitor(screenshot_full_path, region="FORMULARIO_ANTES")
            
            # Enviar screenshot diretamente via rpa_log
            if RPA_LOG_AVAILABLE and rpa_log:
                try:
                    rpa_log.screenshot(
                        filename=f"screen_{int(time.time())}.png",
                        regiao="screenshot_periodica",
                    )
                    log(f"[SCREENSHOT BEFORE] Screenshot enviado via rpa_log.screenshot()")
                except Exception as e:
                    log(f"[SCREENSHOT BEFORE][WARN] Erro ao enviar via rpa_log: {e}")
            
            # Salvar caminho no banco de dados (caminho relativo a static/)
            if not flask_app:
                error_msg = "[SCREENSHOT BEFORE] ❌ CRITICAL: flask_app é None! Caller deve configurar rpa.flask_app ANTES de executar RPA!"
                log(error_msg)
                raise RuntimeError(error_msg)
            
            try:
                from models import Process, db
                with flask_app.app_context():
                    proc = Process.query.get(process_id)
                    if proc:
                        # ✅ FIX: Salvar APENAS o nome do arquivo. O prefixo é adicionado na rota/template.
                        proc.elaw_screenshot_before_path = screenshot_filename
                        proc.elaw_screenshot_path = screenshot_filename  # backwards compat
                        log(f"[SCREENSHOT BEFORE] ANTES de commit: {proc.elaw_screenshot_before_path}")
                        db.session.commit()
                        log(f"[SCREENSHOT BEFORE] ✅ Caminho commitado no banco: {proc.elaw_screenshot_before_path}")
                        screenshot_before_path = proc.elaw_screenshot_before_path
                    else:
                        log(f"[SCREENSHOT BEFORE][ERRO] Processo #{process_id} não encontrado no banco!")
            except Exception as e:
                log(f"[SCREENSHOT BEFORE][ERRO] Erro ao salvar caminho no banco: {e}")
            
            update_status("screenshot_before_captured", f"Screenshot do formulário preenchido salvo ✓", process_id=process_id)
    except Exception as e:
        log(f"[SCREENSHOT BEFORE][WARN] Erro ao capturar screenshot: {e}")

    if RPA_SKIP_SAVE:
        try:
            png = _get_screenshot_path("form_parcial_preenchido.png", process_id=process_id)  # 2025-11-21: Corrigido
            await page.screenshot(path=str(png), full_page=True)
            log(f"[SHOT] parcial: {png}")
        except Exception:
            pass
        log(f"[FLOW] SKIP SAVE (RPA_SKIP_SAVE=1) — somente pré-visualização.")
        if RPA_PREVIEW_SECONDS > 0:
            await asyncio.sleep(RPA_PREVIEW_SECONDS)
        return
    else:
        # Clicar em Salvar e capturar resultado
        update_status("salvando_processo", "Clicando em Salvar...", process_id=process_id)
        save_result = await click_salvar_and_wait(page, cnj_expected=cnj)
        
        # ✅ SCREENSHOT IMEDIATO: Capturar toast ANTES de esperar (toasts somem em 3-5s!)
        # Screenshot DEPOIS de salvar (sucesso ou erro)
        screenshot_after_path = None
        try:
            if process_id:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                screenshot_filename = f"process_{process_id}_{timestamp}_after.png"
                
                screenshot_static_dir = Path("static") / "rpa_screenshots"
                screenshot_static_dir.mkdir(parents=True, exist_ok=True)
                screenshot_full_path = screenshot_static_dir / screenshot_filename
                
                await page.screenshot(path=str(screenshot_full_path), full_page=True)
                
                status_label = "SUCESSO" if save_result['success'] else "ERRO"
                log(f"[SCREENSHOT AFTER] Screenshot pós-save ({status_label}) salvo em: {screenshot_full_path}")
                send_screenshot_to_monitor(screenshot_full_path, region=f"FORMULARIO_DEPOIS_{status_label}")
                
                # Enviar screenshot diretamente via rpa_log
                if RPA_LOG_AVAILABLE and rpa_log:
                    try:
                        rpa_log.screenshot(
                            filename=f"screen_{int(time.time())}.png",
                            regiao="screenshot_periodica",
                        )
                        log(f"[SCREENSHOT AFTER] Screenshot enviado via rpa_log.screenshot()")
                    except Exception as e:
                        log(f"[SCREENSHOT AFTER][WARN] Erro ao enviar via rpa_log: {e}")
                
                # Salvar caminho no banco de dados
                if not flask_app:
                    error_msg = "[SCREENSHOT AFTER] ❌ CRITICAL: flask_app é None! Caller deve configurar rpa.flask_app ANTES de executar RPA!"
                    log(error_msg)
                    raise RuntimeError(error_msg)
                
                try:
                    from models import Process, db
                    with flask_app.app_context():
                        proc = Process.query.get(process_id)
                        if proc:
                            # ✅ FIX: Salvar APENAS o nome do arquivo.
                            proc.elaw_screenshot_after_path = screenshot_filename
                            
                            # ⚠️ NÃO definir 'success' aqui - ainda falta inserir reclamadas, marcações e pedidos
                            # O status 'success' será definido no FINAL do fluxo completo
                            if proc.elaw_status not in ('success', 'error'):
                                if save_result['success']:
                                    # Status intermediário: formulário salvo, mas fluxo ainda não concluído
                                    proc.elaw_status = 'processing'  # Status intermediário
                                    proc.status = 'processing'
                                    proc.elaw_filled_at = datetime.now()
                                    proc.elaw_error_message = None
                                    
                                    # ✅ MÚLTIPLAS RECLAMADAS: Salvar URL de detalhes do processo
                                    # Esta URL é necessária para o fluxo de adicionar reclamadas extras
                                    url_after = save_result.get('url_after', '')
                                    if url_after and ('detail' in url_after.lower() or 'id=' in url_after.lower()):
                                        proc.elaw_detail_url = url_after
                                        log(f"[RECLAMADAS] URL de detalhes salva: {url_after}")
                                else:
                                    proc.elaw_status = 'error'
                                    proc.status = 'error'
                                    proc.elaw_error_message = save_result['message']
                                log(f"[STATUS] Processo marcado como: {proc.elaw_status} / {proc.status}")
                            else:
                                log(f"[STATUS] Processo já tinha status: {proc.elaw_status} - mantendo")
                            
                            log(f"[SCREENSHOT AFTER] ANTES de commit: {proc.elaw_screenshot_after_path}")
                            db.session.commit()
                            log(f"[SCREENSHOT AFTER] ✅ Caminho commitado no banco: {proc.elaw_screenshot_after_path}")
                            screenshot_after_path = proc.elaw_screenshot_after_path
                        else:
                            log(f"[SCREENSHOT AFTER][ERRO] Processo #{process_id} não encontrado no banco!")
                except Exception as e:
                    log(f"[SCREENSHOT AFTER][ERRO] Erro ao salvar caminho no banco: {e}")
                
                update_status("screenshot_after_captured", f"Screenshot pós-save ({status_label}, process_id=process_id) capturado ✓")
        except Exception as e:
            log(f"[SCREENSHOT AFTER][WARN] Erro ao capturar screenshot: {e}")
        
        # Log final do resultado
        if save_result['success']:
            log(f"[FLOW] ✅ Formulário salvo com sucesso! URL: {save_result['url_after']}")
            update_status("processo_salvo", f"✅ Processo salvo com sucesso no eLaw!", process_id=process_id)
            
            # ✅ MÚLTIPLAS RECLAMADAS: Verificar se há reclamadas extras para adicionar
            reclamadas = data.get("reclamadas", [])
            # 🔧 DEBUG 2025-12-02: Log detalhado para verificar reclamadas
            log(f"[RECLAMADAS][DEBUG] process_id={process_id}")
            log(f"[RECLAMADAS][DEBUG] Total reclamadas: {len(reclamadas)}")
            for idx, rec in enumerate(reclamadas):
                log(f"[RECLAMADAS][DEBUG]   [{idx}] nome={rec.get('nome', 'N/A')[:50]}, posicao={rec.get('posicao', 'N/A')}")
            if len(reclamadas) > 1:
                log(f"[RECLAMADAS] Detectadas {len(reclamadas)} reclamadas - iniciando inserção de extras")
                
                # Verificar se temos URL de detalhes disponível
                has_detail_url = False
                current_url = page.url
                
                # Caso 1: Já está na tela de detalhes
                if 'detail' in current_url.lower() or 'id=' in current_url.lower():
                    log("[RECLAMADAS] Já na tela de detalhes")
                    has_detail_url = True
                else:
                    # Caso 2: Temos URL de detalhes no resultado do save (via url_after ou modal)
                    url_after = save_result.get('url_after', '')
                    detail_url_from_modal = save_result.get('detail_url', '')
                    
                    # Priorizar URL capturada do modal "já existe" (mais confiável)
                    if detail_url_from_modal:
                        log(f"[RECLAMADAS] Usando URL de detalhes do modal: {detail_url_from_modal}")
                        await page.goto(detail_url_from_modal, wait_until="load", timeout=NAV_TIMEOUT_MS)
                        await short_sleep_ms(1000)
                        has_detail_url = True
                    elif url_after and ('detail' in url_after.lower() or 'id=' in url_after.lower()):
                        log(f"[RECLAMADAS] Navegando para tela de detalhes: {url_after}")
                        await page.goto(url_after, wait_until="load", timeout=NAV_TIMEOUT_MS)
                        await short_sleep_ms(1000)
                        has_detail_url = True
                    else:
                        # Caso 3: FALLBACK - Processo já existia mas não temos URL
                        # Buscar via Relatório de Andamentos
                        log("[RECLAMADAS][FALLBACK] URL de detalhes não disponível - acionando busca via Relatório")
                        fallback_ok = await ensure_elaw_detail_url_via_relatorio(page, process_id, data)
                        if fallback_ok:
                            log("[RECLAMADAS][FALLBACK] ✅ URL encontrada via relatório - continuando com reclamadas extras")
                            has_detail_url = True
                        else:
                            log("[RECLAMADAS][FALLBACK] ❌ Falha ao obter URL de detalhes - pulando inserção de reclamadas extras")
                
                # Só tenta adicionar reclamadas extras se temos a URL de detalhes
                if has_detail_url:
                    try:
                        await handle_extra_reclamadas(page, data, process_id)
                    except Exception as e:
                        log(f"[RECLAMADAS][WARN] Erro ao adicionar reclamadas extras (processo principal OK): {e}")
                else:
                    log("[RECLAMADAS][SKIP] Sem URL de detalhes - reclamadas extras não serão adicionadas")
            else:
                log("[RECLAMADAS] Apenas 1 reclamada - nenhuma extra para adicionar")
            
            # ✅ NOVO: Verificar e processar pedidos para TODOS os processos (com ou sem reclamadas extras)
            pedidos = data.get("pedidos_json", []) or data.get("pedidos", [])
            # 🔧 DEBUG 2025-12-02: Log detalhado para identificar por que pedidos não estão sendo inseridos
            log(f"[PEDIDOS][DEBUG] process_id={process_id}")
            log(f"[PEDIDOS][DEBUG] data.get('pedidos_json')={type(data.get('pedidos_json')).__name__}: {data.get('pedidos_json', 'N/A')[:200] if isinstance(data.get('pedidos_json'), str) else data.get('pedidos_json')}")
            log(f"[PEDIDOS][DEBUG] data.get('pedidos')={type(data.get('pedidos')).__name__}: {len(data.get('pedidos', [])) if isinstance(data.get('pedidos'), list) else data.get('pedidos')}")
            log(f"[PEDIDOS][DEBUG] pedidos final={type(pedidos).__name__}: {len(pedidos) if isinstance(pedidos, list) else pedidos}")
            if pedidos:
                log(f"[PEDIDOS] Detectados {len(pedidos)} pedidos para inserir")
                
                # Garantir que estamos na tela de detalhes antes de inserir pedidos
                current_url = page.url
                if 'detail' not in current_url.lower() and 'id=' not in current_url.lower():
                    # Precisamos navegar para a tela de detalhes
                    log("[PEDIDOS] Não está na tela de detalhes - verificando URL...")
                    
                    # Tentar obter URL de detalhes do banco
                    detail_url = None
                    if flask_app:
                        from models import Process, db
                        with flask_app.app_context():
                            proc = Process.query.get(process_id)
                            if proc and proc.elaw_detail_url:
                                detail_url = proc.elaw_detail_url
                    
                    if detail_url:
                        log(f"[PEDIDOS] Navegando para tela de detalhes: {detail_url}")
                        await page.goto(detail_url, wait_until="load", timeout=NAV_TIMEOUT_MS)
                        await short_sleep_ms(1000)
                    else:
                        # Fallback: buscar via relatório
                        log("[PEDIDOS][FALLBACK] URL de detalhes não disponível - acionando busca via Relatório")
                        fallback_ok = await ensure_elaw_detail_url_via_relatorio(page, process_id, data)
                        if not fallback_ok:
                            log("[PEDIDOS][SKIP] Não foi possível obter URL de detalhes - pulando pedidos")
                            pedidos = []  # Limpar para pular o fluxo de pedidos
                
                # Processar marcações e pedidos se estamos na tela de detalhes
                if pedidos:
                    # 1. Editar marcações baseado no cliente/reclamada
                    try:
                        log("[PEDIDOS][FLOW] Iniciando fluxo de marcações...")
                        await handle_marcacoes(page, data, process_id)
                    except Exception as e:
                        log(f"[PEDIDOS][FLOW][WARN] Erro ao editar marcações: {e}")
                    
                    # 2. Adicionar pedidos
                    try:
                        log("[PEDIDOS][FLOW] Iniciando inserção de pedidos...")
                        await handle_novo_pedido(page, data, process_id)
                    except Exception as e:
                        log(f"[PEDIDOS][FLOW][WARN] Erro ao adicionar pedidos: {e}")
            else:
                log("[PEDIDOS] Nenhum pedido detectado para este processo")
            
            # Navegar de volta ao dashboard
            try:
                dashboard_url = BASE_URL.rstrip("/") + "/Home/Index"
                log(f"[FLOW] Navegando de volta ao dashboard: {dashboard_url}")
                update_status("voltando_dashboard", "Retornando ao dashboard...", process_id=process_id)
                await page.goto(dashboard_url, wait_until="domcontentloaded", timeout=30000)  # 30s - tolerante a navegação lenta
                await asyncio.sleep(0.5)  # Otimizado: 1s→0.5s (economia 0.5s)
                log(f"[FLOW] ✅ Retornado ao dashboard com sucesso")
                update_status("dashboard_ok", "✅ Retornado ao dashboard", process_id=process_id)
            except Exception as e:
                log(f"[FLOW][WARN] Erro ao retornar ao dashboard: {e}")
        else:
            log(f"[FLOW] ❌ Erro ao salvar: {save_result['message']}")
            update_status("erro_ao_salvar", f"❌ Erro: {save_result['message']}", status="error", process_id=process_id)


# ============================================================================
# FALLBACK: Buscar URL de detalhes via Relatório de Andamentos
# ============================================================================

async def ensure_elaw_detail_url_via_relatorio(page, process_id: int, data: dict) -> bool:
    """
    Fallback para buscar a URL de detalhes do processo via Relatório de Andamentos.
    
    Usado quando:
    - O eLaw mostra modal "processo já existe"
    - Mas não temos a URL de detalhes gravada no banco
    
    Args:
        page: Playwright Page object já logado no eLaw
        process_id: ID do processo no banco de dados
        data: Dicionário com dados extraídos do PDF (para pegar o número do processo)
        
    Returns:
        bool: True se a URL foi encontrada e salva com sucesso
    """
    log(f"[FALLBACK_URL][INICIO] process_id={process_id}")
    
    # 1. Verificar se já existe URL gravada
    if flask_app:
        from models import Process, db
        with flask_app.app_context():
            proc = Process.query.get(process_id)
            if proc and proc.elaw_detail_url:
                log(f"[FALLBACK_URL][SKIP] URL já existe: {proc.elaw_detail_url}")
                return True
    
    # 2. Determinar o número do processo a pesquisar
    numero_processo = extract_cnj_from_anywhere(data)
    if not numero_processo:
        log("[FALLBACK_URL][SEM_NUMERO] Número do processo não encontrado no data")
        return False
    
    log(f"[FALLBACK_URL][NUMERO] Buscando processo: {numero_processo}")
    update_status("fallback_url", f"Buscando detalhes do processo {numero_processo}...", process_id=process_id)
    
    try:
        # 3. Navegar para a tela de Relatório de Andamentos
        relatorio_url = BASE_URL.rstrip("/") + "/processo/ListRelatorio?cache=false"
        log(f"[FALLBACK_URL][NAV] Navegando para: {relatorio_url}")
        await page.goto(relatorio_url, wait_until="load", timeout=NAV_TIMEOUT_MS)
        await short_sleep_ms(1000)
        
        # Verificar se chegou na página correta
        title = await page.title()
        if "Relatório" not in title and "Relatorio" not in title:
            log(f"[FALLBACK_URL][WARN] Título inesperado: {title}")
        
        # 4. Preencher o filtro "Número Processo"
        log(f"[FALLBACK_URL][FILTER] Preenchendo filtro com: {numero_processo}")
        
        # Lista de seletores possíveis para o campo de número do processo
        numero_selectors = [
            '#Filters_Protocolo',
            '#Filters_NumeroProcesso', 
            'input[name="Filters.Protocolo"]',
            'input[name="Filters.NumeroProcesso"]',
            'input[placeholder*="N"]',  # Campo com placeholder contendo "N" (Número)
        ]
        
        # Tentar buscar com número completo e também só com dígitos
        numeros_para_tentar = [
            numero_processo,  # Formato completo: 0101569-94.2025.5.01.0202
            re.sub(r'\D', '', numero_processo)  # Só dígitos: 01015699420255010202
        ]
        
        for tentativa_numero in numeros_para_tentar:
            log(f"[FALLBACK_URL][TENTATIVA] Buscando com: {tentativa_numero}")
            
            filter_filled = False
            for sel in numero_selectors:
                try:
                    elem = page.locator(sel).first
                    if await elem.is_visible(timeout=1000):
                        await elem.clear()
                        await elem.fill(tentativa_numero)
                        await short_sleep_ms(300)
                        filter_filled = True
                        log(f"[FALLBACK_URL][FILTER] Preenchido via: {sel}")
                        break
                except Exception:
                    continue
            
            if not filter_filled:
                log("[FALLBACK_URL][ERRO] Não encontrou campo de filtro para número do processo")
                continue
            
            # 5. Clicar no botão "Localizar"
            log("[FALLBACK_URL][CLICK] Clicando em Localizar...")
            
            # Lista de seletores possíveis para o botão
            button_selectors = [
                '#buttonSubmit',
                '#btnSearch',
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Localizar")',
                'button:has-text("Pesquisar")',
                'button:has-text("Buscar")',
            ]
            
            button_clicked = False
            for sel in button_selectors:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=1000):
                        await btn.click()
                        button_clicked = True
                        log(f"[FALLBACK_URL][CLICK] Clicado via: {sel}")
                        break
                except Exception:
                    continue
            
            if not button_clicked:
                log("[FALLBACK_URL][ERRO] Não encontrou botão de Localizar")
                continue
            
            # 6. Aguardar o carregamento dos resultados (AJAX)
            log("[FALLBACK_URL][WAIT] Aguardando resultados...")
            await short_sleep_ms(2000)  # Esperar AJAX carregar
            
            try:
                await page.wait_for_selector('#processoRelatorioList', state='visible', timeout=20000)  # 20s - tolerante a tabela lenta
            except Exception:
                log("[FALLBACK_URL][ERRO] Container de resultados não ficou visível")
                continue
            
            # 7. Verificar se há resultados
            try:
                html = await page.inner_text('#processoRelatorioList')
                if "Nenhum registro localizado" in html:
                    log(f"[FALLBACK_URL][NENHUM_REGISTRO] Não encontrado com: {tentativa_numero}")
                    continue  # Tentar próximo formato
                log("[FALLBACK_URL][TABELA_OK] Resultados encontrados")
                break  # Sucesso! Sair do loop
            except Exception as e:
                log(f"[FALLBACK_URL][ERRO] Falha ao ler resultados: {e}")
                continue
        else:
            # Nenhuma tentativa funcionou
            log(f"[FALLBACK_URL][NENHUM_REGISTRO] Processo {numero_processo} não encontrado no relatório (nenhum formato)")
            return False
        
        # 8. Encontrar o link do número do processo e clicar
        log(f"[FALLBACK_URL][CLICK_NUMERO] Buscando link para: {numero_processo}")
        numero_digits = re.sub(r'\D', '', numero_processo)
        
        try:
            # Tentar múltiplas estratégias para encontrar o link
            link_found = False
            
            # Estratégia 1: Buscar link na primeira coluna da tabela (td:first-child a)
            try:
                log("[FALLBACK_URL][CLICK_NUMERO] Tentando Estratégia 1: primeira coluna da tabela")
                first_col_links = page.locator('#processoRelatorioList tbody tr td:first-child a')
                count = await first_col_links.count()
                log(f"[FALLBACK_URL][CLICK_NUMERO] Encontrados {count} links na primeira coluna")
                
                for i in range(min(count, 20)):
                    link = first_col_links.nth(i)
                    text = await link.text_content()
                    if text:
                        text_digits = re.sub(r'\D', '', text)
                        if text_digits == numero_digits:
                            log(f"[FALLBACK_URL][CLICK_NUMERO] Match na coluna 1: '{text}'")
                            await link.click()
                            link_found = True
                            break
            except Exception as e:
                log(f"[FALLBACK_URL][WARN] Estratégia 1 falhou: {e}")
            
            # Estratégia 2: Buscar pelo texto exato em qualquer lugar
            if not link_found:
                try:
                    log("[FALLBACK_URL][CLICK_NUMERO] Tentando Estratégia 2: texto exato")
                    link_locator = page.locator(f'a:has-text("{numero_processo}")')
                    count = await link_locator.count()
                    if count > 0:
                        log(f"[FALLBACK_URL][CLICK_NUMERO] Encontrado(s) {count} link(s) com texto exato")
                        await link_locator.first.click()
                        link_found = True
                except Exception as e:
                    log(f"[FALLBACK_URL][WARN] Estratégia 2 falhou: {e}")
            
            # Estratégia 3: Buscar por link com texto contendo dígitos similares
            if not link_found:
                try:
                    log("[FALLBACK_URL][CLICK_NUMERO] Tentando Estratégia 3: dígitos similares")
                    links = page.locator('#processoRelatorioList a')
                    links_count = await links.count()
                    log(f"[FALLBACK_URL][CLICK_NUMERO] Verificando {links_count} links na tabela")
                    
                    for i in range(min(links_count, 30)):
                        link = links.nth(i)
                        text = await link.text_content()
                        if text:
                            text_digits = re.sub(r'\D', '', text)
                            if text_digits == numero_digits:
                                log(f"[FALLBACK_URL][CLICK_NUMERO] Match por dígitos: '{text}'")
                                await link.click()
                                link_found = True
                                break
                except Exception as e:
                    log(f"[FALLBACK_URL][WARN] Estratégia 3 falhou: {e}")
            
            # Estratégia 4: Clicar diretamente na primeira linha se só houver 1 resultado
            if not link_found:
                try:
                    log("[FALLBACK_URL][CLICK_NUMERO] Tentando Estratégia 4: primeiro link da tabela")
                    first_link = page.locator('#processoRelatorioList tbody tr:first-child a').first
                    if await first_link.is_visible(timeout=1000):
                        text = await first_link.text_content()
                        log(f"[FALLBACK_URL][CLICK_NUMERO] Clicando no primeiro link: '{text}'")
                        await first_link.click()
                        link_found = True
                except Exception as e:
                    log(f"[FALLBACK_URL][WARN] Estratégia 4 falhou: {e}")
            
            if not link_found:
                log(f"[FALLBACK_URL][ERRO] Link para processo {numero_processo} não encontrado na tabela")
                return False
                
        except Exception as e:
            log(f"[FALLBACK_URL][ERRO] Falha ao clicar no link do processo: {e}")
            return False
        
        # 9. Aguardar navegação para a tela de detalhes
        log("[FALLBACK_URL][WAIT_NAV] Aguardando tela de detalhes...")
        await short_sleep_ms(2000)
        
        try:
            await page.wait_for_load_state('load', timeout=15000)
            title = await page.title()
            
            if "Detalhes do Processo" not in title:
                log(f"[FALLBACK_URL][WARN] Título não contém 'Detalhes do Processo': {title}")
                # Tentar continuar mesmo assim se a URL parece correta
                current_url = page.url
                if 'detail' not in current_url.lower() and 'id=' not in current_url.lower():
                    log(f"[FALLBACK_URL][ERRO] URL não parece ser de detalhes: {current_url}")
                    return False
        except Exception as e:
            log(f"[FALLBACK_URL][ERRO] Falha ao aguardar navegação: {e}")
            return False
        
        # 10. Gravar a URL de detalhes no banco
        detail_url = page.url
        log(f"[FALLBACK_URL][DETALHES_OK] title={title}, url={detail_url}")
        
        if flask_app:
            from models import Process, db
            with flask_app.app_context():
                proc = Process.query.get(process_id)
                if proc:
                    proc.elaw_detail_url = detail_url
                    db.session.commit()
                    log(f"[FALLBACK_URL][SUCESSO] process_id={process_id}, numero={numero_processo}, url={detail_url}")
                    update_status("fallback_url_ok", f"URL de detalhes encontrada!", process_id=process_id)
                    return True
                else:
                    log(f"[FALLBACK_URL][ERRO] Processo {process_id} não encontrado no banco")
                    return False
        
        return False
        
    except Exception as e:
        log(f"[FALLBACK_URL][ERRO] Erro inesperado: {e}")
        return False


# ============================================================================
# MÚLTIPLAS RECLAMADAS - Adicionar partes extras na aba "Partes e Advogados"
# ============================================================================

async def handle_extra_reclamadas(page, data: dict, process_id: int) -> bool:
    """
    Adiciona reclamadas extras na aba "Partes e Advogados" do eLaw.
    
    Prerequisito: Deve estar na tela de detalhes do processo (após cadastro com sucesso).
    
    Args:
        page: Playwright Page object
        data: Dicionário com dados extraídos do PDF (deve conter 'reclamadas')
        process_id: ID do processo no banco de dados
        
    Returns:
        bool: True se todas as reclamadas extras foram adicionadas com sucesso
    """
    reclamadas = data.get("reclamadas", [])
    
    # Se tem 1 ou menos reclamada, não há extras para adicionar
    if len(reclamadas) <= 1:
        log(f"[RECLAMADAS][RPA] Nenhuma reclamada extra para adicionar (total: {len(reclamadas)})")
        return True
    
    extras = reclamadas[1:]  # Pula a primeira (já foi cadastrada como principal)
    log(f"[RECLAMADAS][RPA][ENTROU_HANDLE] Iniciando inserção de {len(extras)} reclamadas extras")
    update_status("reclamadas_extras", f"Adicionando {len(extras)} reclamadas extras...", process_id=process_id)
    
    try:
        # Garantir que está na tela de detalhes
        title = await page.title()
        if "Detalhes do Processo" not in title:
            log(f"[RECLAMADAS][RPA] Não está na tela de detalhes (título: {title})")
            
            # Tentar navegar para a URL de detalhes salva no banco
            if flask_app:
                from models import Process, db
                with flask_app.app_context():
                    proc = Process.query.get(process_id)
                    if proc and proc.elaw_detail_url:
                        log(f"[RECLAMADAS][RPA] Navegando para URL de detalhes: {proc.elaw_detail_url}")
                        await page.goto(proc.elaw_detail_url, wait_until="load", timeout=NAV_TIMEOUT_MS)
                        await short_sleep_ms(1000)
                    else:
                        log(f"[RECLAMADAS][RPA][ERRO] URL de detalhes não disponível")
                        return False
        
        # Abrir aba "Partes e Advogados"
        log("[RECLAMADAS][RPA] Abrindo aba 'Partes e Advogados'...")
        update_status("reclamadas_aba", "Abrindo aba Partes e Advogados...", process_id=process_id)
        
        # Lista de seletores possíveis para a aba (o eLaw pode usar diferentes estruturas)
        tab_selectors = [
            'a[href="#box-outraspartes"]',
            'a[data-toggle="tab"][href="#box-outraspartes"]',
            'a:has-text("Partes e Advogados")',
            'li a:has-text("Partes")',
            '.nav-tabs a:has-text("Partes")',
            '#tabs-processo a:has-text("Partes")',
            'ul.nav-tabs li a:has-text("Partes")',
        ]
        
        tab_clicked = False
        for tab_sel in tab_selectors:
            try:
                log(f"[RECLAMADAS][RPA] Tentando seletor de aba: {tab_sel}")
                tab_elem = page.locator(tab_sel).first
                if await tab_elem.is_visible(timeout=1000):
                    await tab_elem.click()
                    await short_sleep_ms(1500)
                    tab_clicked = True
                    log(f"[RECLAMADAS][RPA] ✅ Clicou na aba com: {tab_sel}")
                    break
            except Exception as e:
                log(f"[RECLAMADAS][RPA] Seletor {tab_sel} não funcionou: {e}")
                continue
        
        if not tab_clicked:
            log("[RECLAMADAS][RPA][ERRO] Não encontrou a aba 'Partes e Advogados'")
            return False
        
        try:
            # Aguardar container ficar visível
            container_selectors = ['#box-outraspartes', '#partes-advogados', '.tab-pane#box-outraspartes']
            container_visible = False
            for cont_sel in container_selectors:
                try:
                    await page.wait_for_selector(cont_sel, state='visible', timeout=3000)
                    log(f"[RECLAMADAS][RPA] Container {cont_sel} visível")
                    container_visible = True
                    break
                except:
                    continue
            
            if not container_visible:
                log("[RECLAMADAS][RPA][WARN] Container da aba não ficou visível, tentando continuar...")
            
            # Esperar um pouco mais para o conteúdo da aba carregar
            await short_sleep_ms(2000)
            
            # O botão "Nova Parte" está dentro de um dropdown "Ações"
            # O dropdown tem 2 botões: um com texto "Ações" e outro com seta (dropdown-toggle)
            # Precisamos clicar no botão com dropdown-toggle para abrir o menu
            dropdown_selectors = [
                '#box-outraspartes button.dropdown-toggle',
                '#box-outraspartes button[data-toggle="dropdown"]',
                '#box-outraspartes .btn-group button.dropdown-toggle',
                '#box-outraspartes .btn-group button[data-toggle="dropdown"]',
                '.btn-group button.dropdown-toggle',
                'button[data-toggle="dropdown"].btn-acoes',
            ]
            
            dropdown_found = None
            for dd_sel in dropdown_selectors:
                try:
                    dd = page.locator(dd_sel).first
                    if await dd.count() > 0 and await dd.is_visible(timeout=1000):
                        log(f"[RECLAMADAS][RPA] ✅ Dropdown toggle encontrado: {dd_sel}")
                        dropdown_found = dd_sel
                        break
                except:
                    continue
            
            if not dropdown_found:
                log("[RECLAMADAS][RPA][WARN] Dropdown toggle não encontrado, usando fallback...")
                dropdown_found = '#box-outraspartes .btn-group button:nth-child(2)'
            else:
                log(f"[RECLAMADAS][RPA] Dropdown toggle encontrado, será usado para cada inserção")
                
            log("[RECLAMADAS][RPA] ✅ Aba 'Partes e Advogados' aberta com sucesso")
        except Exception as e:
            log(f"[RECLAMADAS][RPA][ERRO] Falha ao abrir aba Partes e Advogados: {e}")
            return False
        
        # Guardar o seletor do dropdown para usar no loop
        acoes_dropdown_selector = dropdown_found
        
        # ✅ VERIFICAR DUPLICATAS: Ler partes já existentes na tabela "Outras Partes"
        existing_names = set()
        try:
            log("[RECLAMADAS][RPA] Verificando partes já existentes para evitar duplicatas...")
            rows = await page.locator('#box-outraspartes table tbody tr').all()
            for row in rows:
                try:
                    # Pegar o nome da parte (geralmente na 3ª coluna - "Nome")
                    nome_cell = row.locator('td:nth-child(3), td:nth-child(4)')
                    nome_text = await nome_cell.text_content() if await nome_cell.count() > 0 else ""
                    if nome_text:
                        # Normalizar para comparação
                        nome_norm = nome_text.strip().upper()
                        if nome_norm and len(nome_norm) > 3:
                            existing_names.add(nome_norm)
                except:
                    continue
            log(f"[RECLAMADAS][RPA] Partes já cadastradas: {len(existing_names)} - {list(existing_names)[:5]}")
        except Exception as e:
            log(f"[RECLAMADAS][RPA][WARN] Erro ao ler partes existentes: {e}")
        
        # Loop para cada reclamada extra
        success_count = 0
        skipped_count = 0
        for idx, reclamada in enumerate(extras):
            # ✅ VERIFICAR DUPLICATA antes de inserir
            nome_reclamada = reclamada.get("nome", "").strip().upper()
            is_duplicate = False
            for existing in existing_names:
                # Verificar se o nome é igual ou muito similar (um contém o outro)
                if nome_reclamada == existing or nome_reclamada in existing or existing in nome_reclamada:
                    log(f"[RECLAMADAS][RPA][SKIP] Reclamada '{reclamada['nome']}' já existe (similar a '{existing}')")
                    is_duplicate = True
                    skipped_count += 1
                    break
            
            if is_duplicate:
                continue
            try:
                log(f"[RECLAMADAS][RPA] ═══ RECLAMADA EXTRA {idx + 1}/{len(extras)} ═══")
                update_status("reclamadas_inserindo", f"Inserindo reclamada {idx + 1}/{len(extras)}: {reclamada['nome'][:30]}...", process_id=process_id)
                
                # Abrir dropdown "Ações" e clicar em "Nova Parte"
                if acoes_dropdown_selector:
                    log(f"[RECLAMADAS][RPA] Abrindo dropdown 'Ações' ({acoes_dropdown_selector})...")
                    await page.click(acoes_dropdown_selector)
                    await short_sleep_ms(1200)  # Esperar mais para o dropdown abrir
                    
                    # Aguardar o menu dropdown ficar visível - usar seletor mais específico
                    try:
                        await page.wait_for_selector('#box-outraspartes .dropdown-menu.show, #box-outraspartes .open .dropdown-menu, #box-outraspartes .dropdown-menu[style*="display: block"]', state='visible', timeout=2000)
                        log("[RECLAMADAS][RPA] Menu dropdown aberto dentro de #box-outraspartes")
                    except:
                        # Fallback: tentar qualquer dropdown visível
                        try:
                            await page.wait_for_selector('.dropdown-menu.show, .open .dropdown-menu', state='visible', timeout=1000)
                            log("[RECLAMADAS][RPA][WARN] Menu dropdown aberto (fallback genérico)")
                        except:
                            log("[RECLAMADAS][RPA][WARN] Menu dropdown pode não ter aberto corretamente")
                    
                    # Debug: listar itens do dropdown - APENAS dentro de #box-outraspartes
                    try:
                        menu_items = await page.locator('#box-outraspartes .dropdown-menu a').all_text_contents()
                        log(f"[RECLAMADAS][RPA][DEBUG] Itens no dropdown #box-outraspartes: {menu_items[:5]}")
                    except Exception as e:
                        log(f"[RECLAMADAS][RPA][DEBUG] Erro ao listar menu: {e}")
                    
                    # Clicar em "Nova Parte" - usar ID específico primeiro
                    nova_parte_selectors = [
                        '#buttonNewParte',
                        'a#buttonNewParte',
                        '#box-outraspartes #buttonNewParte',
                        '.dropdown-menu #buttonNewParte',
                        'ul.dropdown-menu li a#buttonNewParte',
                        '.dropdown-menu a[title="Nova Parte"]',
                        '#box-outraspartes .dropdown-menu a:has-text("Nova Parte")',
                        '.dropdown-menu a:has-text("Nova Parte")',
                    ]
                    
                    clicked = False
                    for np_sel in nova_parte_selectors:
                        try:
                            np_btn = page.locator(np_sel).first
                            if await np_btn.count() > 0 and await np_btn.is_visible(timeout=500):
                                log(f"[RECLAMADAS][RPA] ✅ Clicando em 'Nova Parte' ({np_sel})...")
                                await np_btn.click()
                                clicked = True
                                break
                        except Exception as e:
                            log(f"[RECLAMADAS][RPA][DEBUG] Tentativa {np_sel}: {e}")
                            continue
                    
                    if not clicked:
                        # Fallback: tentar via JavaScript
                        log("[RECLAMADAS][RPA][WARN] Seletores falharam, tentando via JavaScript...")
                        try:
                            result = await page.evaluate("""
                                () => {
                                    const links = document.querySelectorAll('.dropdown-menu a, ul.dropdown-menu li a');
                                    for (const link of links) {
                                        if (link.textContent && link.textContent.includes('Nova Parte')) {
                                            link.click();
                                            return 'clicked';
                                        }
                                    }
                                    return 'not_found';
                                }
                            """)
                            if result == 'clicked':
                                log("[RECLAMADAS][RPA] ✅ Clicado via JavaScript!")
                                clicked = True
                            else:
                                log("[RECLAMADAS][RPA][WARN] JavaScript não encontrou 'Nova Parte'")
                        except Exception as js_err:
                            log(f"[RECLAMADAS][RPA][WARN] Erro JavaScript: {js_err}")
                    
                    if not clicked:
                        log("[RECLAMADAS][RPA][WARN] Não encontrou 'Nova Parte' no dropdown - pulando reclamada")
                        continue
                else:
                    # Fallback: tentar clicar diretamente no botão
                    log("[RECLAMADAS][RPA] Tentando clicar diretamente em 'Nova Parte'...")
                    await page.click('#buttonNewParte')
                
                await short_sleep_ms(1500)
                
                # Aguardar modal abrir
                modal_selectors = ['#dialog-modal.show', '#dialog-modal.in', '.modal.show', '.modal.in']
                modal_visible = False
                for sel in modal_selectors:
                    try:
                        await page.wait_for_selector(sel, state='visible', timeout=3000)
                        modal_visible = True
                        break
                    except:
                        pass
                
                if not modal_visible:
                    log(f"[RECLAMADAS][RPA][WARN] Modal não detectado para reclamada {idx + 1}")
                    continue
                
                # Aguardar select de Posição
                await page.wait_for_selector('#PosicaoParteId', state='attached', timeout=3000)
                log("[RECLAMADAS][RPA] Modal 'Nova Parte' aberto")
                
                # Selecionar Posição da Parte (RECLAMADO ou REU)
                posicao = reclamada.get("posicao", "RECLAMADO")
                log(f"[RECLAMADAS][RPA][POSICAO] Selecionando posição: {posicao}")
                
                # Mapear posição para value do select
                posicao_value = await _map_posicao_to_select_value(page, posicao)
                if posicao_value:
                    await page.select_option('#PosicaoParteId', value=posicao_value)
                    await short_sleep_ms(500)
                else:
                    # Fallback: tentar selecionar por texto visível
                    try:
                        await page.select_option('#PosicaoParteId', label=posicao)
                    except:
                        log(f"[RECLAMADAS][RPA][WARN] Não encontrou posição '{posicao}', usando RECLAMADO")
                        await page.select_option('#PosicaoParteId', value='52')  # 52 = RECLAMADO (valor comum no eLaw)
                
                await short_sleep_ms(500)
                
                # Preencher Nome da Parte
                nome = reclamada.get("nome", "")
                log(f"[RECLAMADAS][RPA][NOME_PARTE] Preenchendo nome: {nome}")
                
                # Verificar se campo é input ou select (depende da posição selecionada)
                nome_input = page.locator('#NomeParte')
                nome_select = page.locator('#IdParteList')
                
                try:
                    if await nome_input.is_visible(timeout=1000):
                        await nome_input.fill(nome)
                        log("[RECLAMADAS][RPA] Nome preenchido via input")
                    elif await nome_select.is_visible(timeout=1000):
                        # Select - tentar encontrar opção que contenha o nome
                        log("[RECLAMADAS][RPA] Nome via select - buscando opção...")
                        # Tentar selecionar por texto parcial
                        options = await page.query_selector_all('#IdParteList option')
                        for opt in options:
                            opt_text = await opt.text_content()
                            if nome.upper() in (opt_text or "").upper():
                                opt_value = await opt.get_attribute('value')
                                await page.select_option('#IdParteList', value=opt_value)
                                log(f"[RECLAMADAS][RPA] Selecionado: {opt_text}")
                                break
                except Exception as e:
                    log(f"[RECLAMADAS][RPA][WARN] Erro ao preencher nome: {e}")
                
                await short_sleep_ms(300)
                
                # Selecionar Tipo Pessoa (Física ou Jurídica)
                # NOTA: Usamos JavaScript porque o iCheck coloca overlay <ins> que intercepta cliques
                tipo = reclamada.get("tipo_pessoa", "")
                log(f"[RECLAMADAS][RPA][TIPO_PESSOA] Selecionando tipo: {tipo}")
                
                tipo_value = "2" if tipo == "juridica" else "1"
                try:
                    # Primeiro tentar via JavaScript (contorna overlay do iCheck)
                    await page.evaluate(f'''
                        const radio = document.querySelector('input[name="TipoPessoa"][value="{tipo_value}"]');
                        if (radio) {{
                            radio.checked = true;
                            radio.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            // Trigger iCheck update se existir
                            if (typeof $.fn.iCheck !== 'undefined') {{
                                $(radio).iCheck('update');
                            }}
                        }}
                    ''')
                    log(f"[RECLAMADAS][RPA][TIPO_PESSOA] ✅ Selecionado via JavaScript: {tipo}")
                except Exception as e:
                    log(f"[RECLAMADAS][RPA][TIPO_PESSOA][WARN] Fallback para click: {e}")
                    # Fallback: clicar no label/container do iCheck
                    try:
                        container = page.locator(f'input[name="TipoPessoa"][value="{tipo_value}"]').locator('..')
                        await container.click(force=True)
                    except:
                        pass
                
                await short_sleep_ms(300)
                
                # Clicar em Salvar do modal
                log("[RECLAMADAS][RPA] Clicando em Salvar...")
                save_btn = page.locator('#dialog-modal button.btn.btn-primary.salvar, #dialog-modal button[type="submit"]')
                await save_btn.click()
                await short_sleep_ms(2000)
                
                # Aguardar modal fechar
                try:
                    await page.wait_for_selector('#dialog-modal', state='hidden', timeout=5000)
                    log(f"[RECLAMADAS][RPA] ✅ Reclamada {idx + 1} inserida com sucesso!")
                    success_count += 1
                    # Adicionar ao set para evitar duplicatas no mesmo loop
                    existing_names.add(nome_reclamada)
                except:
                    # Modal pode não ter fechado, mas não necessariamente erro
                    log(f"[RECLAMADAS][RPA][WARN] Modal pode não ter fechado após reclamada {idx + 1}")
                
            except Exception as e:
                log(f"[RECLAMADAS][RPA][ERRO] Falha ao inserir reclamada {idx + 1}: {e}")
                # Continuar para as próximas
        
        log(f"[RECLAMADAS][RPA] Inseridas {success_count}/{len(extras)} reclamadas extras (puladas: {skipped_count} duplicatas)")
        
        # Screenshot da aba "Partes e Advogados" após inserir todas
        if success_count > 0:
            await _take_reclamadas_screenshot(page, process_id)
        
        # ⚠️ NOTA: Marcações e pedidos agora são tratados FORA desta função
        # para permitir que processos sem reclamadas extras também tenham pedidos
        # Ver handle_marcacoes_e_pedidos_pos_save()
        
        return success_count == len(extras)
        
    except Exception as e:
        log(f"[RECLAMADAS][RPA][ERRO] Erro geral no handle_extra_reclamadas: {e}")
        return False


async def _map_posicao_to_select_value(page, posicao: str) -> str:
    """
    Mapeia a posição extraída do PDF para o value do select no eLaw.
    
    Args:
        page: Playwright page
        posicao: Posição do PDF (RECLAMADO, REU, etc)
        
    Returns:
        str: Value do option correspondente, ou None se não encontrar
    """
    posicao_upper = posicao.upper().strip()
    
    # Mapeamento de posições comuns para values do eLaw
    # Esses values são os mais comuns no eLaw, mas podem variar por instalação
    POSICAO_MAP = {
        "RECLAMADO": "52",
        "RECLAMADA": "52",
        "REU": "2",
        "RÉU": "2",
        "RÉ": "2",
        "APELADO": "7",
        "AGRAVADO": "12",
        "EXECUTADO": "21",
        "REQUERIDO": "35",
    }
    
    # Tentar mapeamento direto
    if posicao_upper in POSICAO_MAP:
        return POSICAO_MAP[posicao_upper]
    
    # Fallback: buscar no select da página
    try:
        options = await page.query_selector_all('#PosicaoParteId option')
        for opt in options:
            opt_text = await opt.text_content()
            if posicao_upper in (opt_text or "").upper():
                return await opt.get_attribute('value')
    except Exception:
        pass
    
    return None


async def _take_reclamadas_screenshot(page, process_id: int):
    """
    Captura screenshot da aba "Partes e Advogados" após inserir reclamadas extras.
    Salva o caminho em Process.elaw_screenshot_reclamadas_path.
    """
    log("[RECLAMADAS][RPA][SHOT] Capturando screenshot da aba Partes e Advogados...")
    update_status("reclamadas_screenshot", "Capturando screenshot das reclamadas...", process_id=process_id)
    
    try:
        # Garantir que aba está visível
        await page.click('a[href="#box-outraspartes"]')
        await short_sleep_ms(500)
        
        # Gerar nome do arquivo
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_filename = f"process_{process_id}_{timestamp}_reclamadas.png"
        
        screenshot_dir = Path("static") / "rpa_screenshots"
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = screenshot_dir / screenshot_filename
        
        await page.screenshot(path=str(screenshot_path), full_page=True)
        log(f"[RECLAMADAS][RPA][SHOT] Screenshot salvo: {screenshot_path}")
        
        # Salvar no banco de dados
        if flask_app:
            from models import Process, db
            with flask_app.app_context():
                proc = Process.query.get(process_id)
                if proc:
                    proc.elaw_screenshot_reclamadas_path = screenshot_filename
                    db.session.commit()
                    log(f"[RECLAMADAS][RPA][SHOT] ✅ Caminho salvo no banco: {screenshot_filename}")
        
        send_screenshot_to_monitor(screenshot_path, region="RECLAMADAS_EXTRAS")
        
    except Exception as e:
        log(f"[RECLAMADAS][RPA][SHOT][ERRO] Falha ao capturar screenshot: {e}")


# =========================
# MARCAÇÕES - Editar marcações do processo
# =========================
async def handle_marcacoes(page, data: dict, process_id: int) -> bool:
    """
    Edita marcações do processo na aba Geral.
    
    Fluxo:
    1. Clica na aba "Geral" na sidebar
    2. Abre dropdown "Ações"
    3. Clica em "Editar Marcações"
    4. No modal, marca checkboxes baseados na reclamada (cliente)
    5. Clica em "Salvar"
    
    Args:
        page: Playwright Page object
        data: Dicionário com dados do processo (deve conter 'cliente' ou 'reclamada_principal')
        process_id: ID do processo no banco
        
    Returns:
        bool: True se marcações foram editadas com sucesso
    """
    log("[MARCACOES][RPA] Iniciando edição de marcações...")
    update_status("marcacoes_inicio", "Abrindo aba Geral para editar marcações...", process_id=process_id)
    
    try:
        # 1. Clicar na aba "Geral" na sidebar
        log("[MARCACOES][RPA] Clicando na aba Geral...")
        geral_tab = page.locator('a[href="#box-dadosprincipais"]')
        await geral_tab.click(timeout=5000)
        await short_sleep_ms(1000)
        
        # GUARD: Verificar se a seção está visível com timeout curto
        try:
            await page.wait_for_selector('#box-dadosprincipais', state='visible', timeout=5000)
            log("[MARCACOES][RPA] Aba Geral visível, continuando...")
        except Exception:
            log("[MARCACOES][RPA][SKIP] Seção Geral não visível - pulando marcações")
            update_status("marcacoes_skip", "Seção de marcações não disponível", process_id=process_id)
            return False
        
        # 2. Abrir dropdown "Ações" - com timeout curto para evitar loop infinito
        log("[MARCACOES][RPA] Abrindo dropdown Ações...")
        acoes_dropdown = page.locator('.btn-group .btn-acoes.dropdown-toggle').first
        try:
            await acoes_dropdown.click(timeout=5000)
        except Exception as e:
            log(f"[MARCACOES][RPA][SKIP] Dropdown Ações não encontrado: {e}")
            return False
        await short_sleep_ms(500)
        
        # 3. Clicar em "Editar Marcações" - com timeout curto
        log("[MARCACOES][RPA] Clicando em Editar Marcações...")
        editar_marcacoes = page.locator('#buttonMarcacoesProcesso')
        try:
            await editar_marcacoes.click(timeout=5000)
        except Exception as e:
            log(f"[MARCACOES][RPA][SKIP] Botão Editar Marcações não encontrado: {e}")
            return False
        await short_sleep_ms(1500)
        
        # 4. Aguardar modal abrir
        update_status("marcacoes_modal", "Modal de marcações aberto, identificando opções...", process_id=process_id)
        await page.wait_for_selector('.modal.in, .modal.show, #dialog-modal', state='visible', timeout=5000)
        
        # 5. Identificar o cliente/reclamada para marcar
        cliente = data.get("cliente", "") or data.get("reclamada_principal", "") or data.get("parte_interessada", "")
        log(f"[MARCACOES][RPA] Cliente para marcação: {cliente}")
        
        if cliente:
            # Procurar checkboxes no modal que contenham o nome do cliente
            # iCheck usa estrutura: <div class="icheckbox_..."><input type="checkbox"><ins class="iCheck-helper"></div>
            
            # Buscar todos os labels/textos de checkbox no modal
            checkboxes = await page.query_selector_all('.modal input[type="checkbox"], .modal input[type="radio"]')
            
            for checkbox in checkboxes:
                try:
                    # Pegar o texto associado (label ou parent text)
                    parent = await checkbox.evaluate_handle('el => el.closest("label") || el.parentElement')
                    label_text = await parent.text_content() if parent else ""
                    
                    # Verificar se o texto contém o cliente
                    cliente_upper = cliente.upper()
                    label_upper = (label_text or "").upper()
                    
                    # Matching parcial - CSN, CBSI, ORIZON, etc.
                    if any(parte in label_upper for parte in cliente_upper.split()):
                        log(f"[MARCACOES][RPA] ✅ Encontrada marcação para: {label_text}")
                        
                        # Marcar via JavaScript (contorna iCheck overlay)
                        await checkbox.evaluate('''el => {
                            el.checked = true;
                            el.dispatchEvent(new Event("change", {bubbles: true}));
                            // Trigger iCheck update se existir
                            if (typeof $ !== "undefined" && typeof $.fn.iCheck !== "undefined") {
                                $(el).iCheck("check");
                            }
                        }''')
                        
                        update_status("marcacoes_check", f"Marcado: {label_text}", process_id=process_id)
                        await short_sleep_ms(300)
                except Exception as e:
                    log(f"[MARCACOES][RPA][WARN] Erro ao processar checkbox: {e}")
        
        # 6. Clicar em Salvar (usar .first para evitar ambiguidade com "Salvar e Fechar")
        log("[MARCACOES][RPA] Clicando em Salvar...")
        save_btn = page.locator('.modal button.btn-primary.salvar:not(.salvarfechar)').first
        await save_btn.click()
        await short_sleep_ms(2000)
        
        # 7. Aguardar modal fechar
        try:
            await page.wait_for_selector('.modal.in, .modal.show', state='hidden', timeout=5000)
            log("[MARCACOES][RPA] ✅ Marcações salvas com sucesso!")
            update_status("marcacoes_ok", "Marcações salvas com sucesso", process_id=process_id)
            return True
        except:
            log("[MARCACOES][RPA][WARN] Modal pode não ter fechado")
            return True  # Consideramos sucesso mesmo assim
            
    except Exception as e:
        log(f"[MARCACOES][RPA][ERRO] Falha ao editar marcações: {e}")
        update_status("marcacoes_erro", f"Erro nas marcações: {e}", process_id=process_id)
        return False


# =========================
# PEDIDOS - Adicionar novos pedidos (múltiplos) - OTIMIZADO 2025-11-27
# =========================

# ✅ Configurações de otimização de pedidos
PEDIDOS_STATUS_UPDATE_INTERVAL = 5  # Atualizar status a cada N pedidos
PEDIDOS_FAST_WAIT_MS = 200  # Wait mínimo após ações rápidas
PEDIDOS_MODAL_WAIT_MS = 500  # Wait após abrir/fechar modal
PEDIDOS_SAVE_WAIT_MS = 800  # Wait após salvar (reduzido de 2000)

async def handle_novo_pedido(page, data: dict, process_id: int) -> bool:
    """
    Adiciona MÚLTIPLOS pedidos na aba Pedidos.
    
    🚀 OTIMIZADO 2025-11-27 - Plano Batman:
    - Waits inteligentes ao invés de fixos
    - Atualização de status agrupada (a cada 5 pedidos)
    - Verificação de duplicatas antes de inserir
    - Aba mantida ativa sem reabrir a cada iteração
    
    Fluxo otimizado:
    1. Abre aba "Pedidos" UMA VEZ
    2. Lê pedidos existentes para evitar duplicatas
    3. Para cada pedido novo:
       - Abre dropdown → Novo Pedido → Seleciona tipo → Preenche valor → Salva
    4. Atualiza status a cada 5 pedidos
    5. Captura screenshot final
    
    Args:
        page: Playwright Page object
        data: Dicionário com dados do processo (deve conter 'tipos_pedidos' ou 'pedidos')
        process_id: ID do processo no banco
        
    Returns:
        bool: True se pelo menos um pedido foi adicionado
    """
    import time
    start_time = time.time()
    
    log("[PEDIDOS][RPA] Iniciando adição de pedidos (OTIMIZADO)...")
    update_status("pedidos_inicio", "Abrindo aba Pedidos...", process_id=process_id)
    
    try:
        # 1. Clicar na aba "Pedidos" na sidebar (OTIMIZADO - wait inteligente)
        log("[PEDIDOS][RPA] Clicando na aba Pedidos...")
        pedidos_tab = page.locator('a[href="#box-pedidos"][data-toggle="tab"]')
        
        try:
            # Primeira tentativa: clique normal
            await pedidos_tab.click(timeout=5000)
            
            # ✅ OTIMIZADO: Wait inteligente - esperar aba aparecer ao invés de sleep fixo
            try:
                await page.locator('#box-pedidos').wait_for(state='visible', timeout=3000)
                log("[PEDIDOS][RPA] ✅ Aba Pedidos visível (wait inteligente)")
            except:
                # Fallback: usar JavaScript para ativar a aba Bootstrap
                log("[PEDIDOS][RPA] Clique normal não ativou aba - tentando via JavaScript...")
                await page.evaluate("""() => {
                    document.querySelectorAll('.nav-tabs li').forEach(li => li.classList.remove('active'));
                    document.querySelectorAll('.tab-content .tab-pane').forEach(pane => pane.classList.remove('active', 'in'));
                    const pedidosLink = document.querySelector('a[href="#box-pedidos"]');
                    if (pedidosLink) pedidosLink.parentElement.classList.add('active');
                    const pedidosPane = document.querySelector('#box-pedidos');
                    if (pedidosPane) pedidosPane.classList.add('active', 'in');
                }""")
                await page.locator('#box-pedidos').wait_for(state='visible', timeout=3000)
            
            # ✅ OTIMIZADO: Aguardar dropdown de ações ficar visível (wait inteligente)
            try:
                await page.locator('#box-pedidos .btn-group button.btn-acoes.dropdown-toggle').first.wait_for(state='visible', timeout=3000)
                log("[PEDIDOS][RPA] ✅ Dropdown Ações visível")
            except:
                log("[PEDIDOS][RPA] Dropdown não visível - tentando via JavaScript...")
                await page.evaluate("""() => {
                    const tab = document.querySelector('a[href="#box-pedidos"]');
                    if (tab) tab.click();
                    document.querySelectorAll('.nav-tabs li').forEach(li => li.classList.remove('active'));
                    document.querySelectorAll('.tab-content .tab-pane').forEach(pane => pane.classList.remove('active', 'in'));
                    const pedidosTab = document.querySelector('a[href="#box-pedidos"]');
                    if (pedidosTab && pedidosTab.parentElement) pedidosTab.parentElement.classList.add('active');
                    const pedidosPane = document.querySelector('#box-pedidos');
                    if (pedidosPane) { pedidosPane.classList.add('active', 'in'); pedidosPane.style.display = 'block'; }
                }""")
                await short_sleep_ms(PEDIDOS_MODAL_WAIT_MS)
                
        except Exception as e:
            log(f"[PEDIDOS][RPA][SKIP] Aba Pedidos não disponível: {e}")
            update_status("pedidos_skip", "Aba de Pedidos não disponível", process_id=process_id)
            return False
        
        # Identificar tipos de pedidos a adicionar
        tipos_pedidos = data.get("tipos_pedidos", [])
        pedidos_list = data.get("pedidos", [])
        objeto = data.get("objeto", "") or data.get("sub_objeto", "")
        
        # Se não tiver tipos_pedidos extraídos, tentar mapear dos pedidos
        if not tipos_pedidos and pedidos_list:
            tipos_pedidos = await _map_pedidos_to_tipos(page, pedidos_list, objeto)
        
        # Se ainda não tiver tipos, usar pedidos padrão baseado no tipo de ação
        if not tipos_pedidos:
            log("[PEDIDOS][RPA] Nenhum tipo extraído - abrindo modal para buscar tipos padrão")
            # CORRIGIDO: Abrir o modal primeiro, pois #TipoPedidoId só existe dentro dele
            try:
                await short_sleep_ms(500)
                
                # Abrir dropdown Ações (botão com classe .btn-acoes.dropdown-toggle)
                acoes_dropdown = page.locator('#box-pedidos .btn-group button.btn-acoes.dropdown-toggle').first
                log("[PEDIDOS][RPA] Clicando no dropdown Ações...")
                await acoes_dropdown.click(timeout=5000)
                await short_sleep_ms(500)
                
                # Clicar em Novo Pedido para abrir modal (id=buttonNewPedido)
                novo_pedido = page.locator('#buttonNewPedido')
                log("[PEDIDOS][RPA] Clicando em Novo Pedido...")
                await novo_pedido.click(timeout=5000)
                await short_sleep_ms(1000)
                
                # Aguardar modal abrir
                await page.wait_for_selector('.modal.in, .modal.show, #dialog-modal', state='visible', timeout=5000)
                
                # ✅ AGUARDAR opções do select carregarem (AJAX demora!)
                await short_sleep_ms(1500)  # Dar tempo para AJAX carregar
                
                # Aguardar que o select tenha opções (além de "Selecione")
                try:
                    await page.wait_for_function("""() => {
                        const sel = document.querySelector('#TipoPedidoId');
                        return sel && sel.options && sel.options.length > 1;
                    }""", timeout=5000)
                    log("[PEDIDOS][RPA] Select de tipos carregado com opções")
                except:
                    log("[PEDIDOS][RPA] Aguardando opções do select (timeout)...")
                    await short_sleep_ms(1000)  # Delay extra
                
                # Agora sim, buscar opções do select que está no modal
                tipo_select = page.locator('#TipoPedidoId')
                options = await tipo_select.locator('option').all()
                log(f"[PEDIDOS][RPA] Total de opções no select: {len(options)}")
                
                for opt in options[:4]:  # Pegar até 3 opções (pulando a primeira que é "Selecione")
                    opt_value = await opt.get_attribute('value')
                    opt_text = await opt.text_content()
                    if opt_value and opt_value.strip() and opt_text and "selecione" not in opt_text.lower():
                        tipos_pedidos.append({"value": opt_value, "text": opt_text.strip()})
                        log(f"[PEDIDOS][RPA][FALLBACK] Tipo padrão encontrado: {opt_text.strip()}")
                        if len(tipos_pedidos) >= 3:
                            break
                
                log(f"[PEDIDOS][RPA] Usando {len(tipos_pedidos)} tipos padrão do dropdown")
                
                # Fechar modal após capturar opções (vai reabrir para cada pedido no loop)
                try:
                    close_btn = page.locator('#dialog-modal button.close, .modal.in button.close').first
                    await close_btn.click(timeout=3000)
                    await short_sleep_ms(500)
                except:
                    # Tentar via JavaScript
                    await page.evaluate("document.querySelector('#dialog-modal')?.querySelector('button.close')?.click()")
                    await short_sleep_ms(500)
                    
            except Exception as e:
                log(f"[PEDIDOS][RPA][WARN] Erro ao buscar tipos padrão: {e}")
                # Tentar fechar modal se estiver aberto
                try:
                    await page.evaluate("document.querySelector('#dialog-modal')?.querySelector('button.close')?.click()")
                    await short_sleep_ms(300)
                except:
                    pass
        
        if not tipos_pedidos:
            log("[PEDIDOS][RPA][WARN] Nenhum tipo de pedido disponível - pulando")
            return False
        
        # ✅ CRÍTICO: Aplicar limite máximo de pedidos ANTES do loop
        original_count = len(tipos_pedidos)
        if original_count > MAX_PEDIDOS_PARA_INSERIR:
            log(f"[PEDIDOS][RPA] ⚠️ LIMITE: Reduzindo de {original_count} para {MAX_PEDIDOS_PARA_INSERIR} pedidos")
            tipos_pedidos = tipos_pedidos[:MAX_PEDIDOS_PARA_INSERIR]
        
        log(f"[PEDIDOS][RPA] Tipos de pedidos a adicionar: {len(tipos_pedidos)}")
        
        # ✅ VERIFICAR DUPLICATAS: Ler pedidos já existentes na tabela
        existing_pedidos = set()
        try:
            log("[PEDIDOS][RPA] Verificando pedidos já existentes para evitar duplicatas...")
            await short_sleep_ms(500)
            rows = await page.locator('#box-pedidos table tbody tr').all()
            for row in rows:
                try:
                    # Pegar o tipo do pedido (geralmente na 2ª ou 3ª coluna)
                    tipo_cell = row.locator('td:nth-child(2), td:nth-child(3)')
                    tipo_text = await tipo_cell.text_content() if await tipo_cell.count() > 0 else ""
                    if tipo_text:
                        tipo_norm = tipo_text.strip().upper()
                        if tipo_norm and len(tipo_norm) > 2:
                            existing_pedidos.add(tipo_norm)
                except:
                    continue
            log(f"[PEDIDOS][RPA] Pedidos já cadastrados: {len(existing_pedidos)} - {list(existing_pedidos)[:5]}")
        except Exception as e:
            log(f"[PEDIDOS][RPA][WARN] Erro ao ler pedidos existentes: {e}")
        
        success_count = 0
        skipped_count = 0
        last_status_update = 0  # ✅ Para atualização agrupada de status
        
        log(f"[PEDIDOS][RPA] ⚡ Iniciando loop OTIMIZADO para {len(tipos_pedidos)} pedidos")
        
        # Loop para adicionar cada pedido (OTIMIZADO)
        for idx, tipo_info in enumerate(tipos_pedidos):
            # ✅ GUARD: Parar se atingir limite máximo
            if success_count >= MAX_PEDIDOS_PARA_INSERIR:
                log(f"[PEDIDOS][RPA] ⚠️ LIMITE atingido: {success_count} pedidos inseridos - parando loop")
                break
            
            try:
                tipo_value = tipo_info.get("value") if isinstance(tipo_info, dict) else tipo_info
                tipo_text = tipo_info.get("text", tipo_value) if isinstance(tipo_info, dict) else tipo_value
                
                # ✅ VERIFICAR DUPLICATA antes de inserir
                tipo_norm = (tipo_text or str(tipo_value)).strip().upper()
                is_duplicate = False
                for existing in existing_pedidos:
                    if tipo_norm == existing or tipo_norm in existing or existing in tipo_norm:
                        log(f"[PEDIDOS][RPA][SKIP] Pedido '{tipo_text}' já existe")
                        is_duplicate = True
                        skipped_count += 1
                        break
                
                if is_duplicate:
                    continue
                
                # ✅ OTIMIZADO: Atualizar status a cada N pedidos (não a cada 1)
                if (idx + 1) - last_status_update >= PEDIDOS_STATUS_UPDATE_INTERVAL:
                    update_status("pedidos_adicionando", f"Adicionando pedidos {idx + 1}/{len(tipos_pedidos)}...", process_id=process_id)
                    last_status_update = idx + 1
                
                # ✅ OTIMIZADO: Verificar modal apenas se necessário (não sempre)
                try:
                    modal_visible = await page.locator('.modal.in, .modal.show').first.is_visible()
                    if modal_visible:
                        await page.evaluate("""() => {
                            document.querySelectorAll('.modal.in button.close, .modal.show button.close').forEach(btn => btn.click());
                            document.querySelectorAll('.modal.in, .modal.show').forEach(m => { m.classList.remove('in', 'show'); m.style.display = 'none'; });
                            document.querySelectorAll('.modal-backdrop').forEach(el => el.remove());
                        }""")
                        await page.locator('.modal.in, .modal.show').wait_for(state='hidden', timeout=1500)
                except:
                    pass
                
                # ✅ OTIMIZADO: Só reativar aba se perdeu foco (não sempre)
                try:
                    box_visible = await page.locator('#box-pedidos').is_visible()
                    if not box_visible:
                        await page.evaluate("document.querySelector('a[href=\"#box-pedidos\"]')?.click()")
                        await page.locator('#box-pedidos').wait_for(state='visible', timeout=2000)
                except:
                    pass
                
                # 2. Abrir dropdown "Ações" e clicar em Novo Pedido (OTIMIZADO - sequência rápida)
                acoes_dropdown = page.locator('#box-pedidos .btn-group button.btn-acoes.dropdown-toggle').first
                await acoes_dropdown.click(timeout=3000)
                await short_sleep_ms(PEDIDOS_FAST_WAIT_MS)  # 200ms ao invés de 500ms
                
                novo_pedido = page.locator('#buttonNewPedido')
                await novo_pedido.click()
                
                # ✅ OTIMIZADO: Wait inteligente para modal + select carregar
                try:
                    await page.wait_for_selector('.modal.in #TipoPedidoId, .modal.show #TipoPedidoId, #dialog-modal #TipoPedidoId', state='visible', timeout=3000)
                    await page.wait_for_function("document.querySelector('#TipoPedidoId')?.options?.length > 1", timeout=3000)
                except:
                    await short_sleep_ms(PEDIDOS_MODAL_WAIT_MS)  # Fallback com sleep curto
                
                # ✅ VERIFICAR se o value existe no select
                value_exists = await page.evaluate(f"""() => {{
                    const select = document.querySelector('#TipoPedidoId');
                    if (!select) return false;
                    for (let i = 0; i < select.options.length; i++) {{
                        if (select.options[i].value === '{tipo_value}') return true;
                    }}
                    return false;
                }}""")
                
                if not value_exists:
                    log(f"[PEDIDOS][RPA][SKIP] Tipo '{tipo_text}' não encontrado no select")
                    await page.evaluate("document.querySelector('#dialog-modal button.close, .modal.in button.close')?.click()")
                    await short_sleep_ms(PEDIDOS_FAST_WAIT_MS)
                    continue
                
                # 5. Selecionar Tipo do Pedido (OTIMIZADO - direto sem verificação extra)
                await page.locator('#TipoPedidoId').select_option(value=str(tipo_value))
                
                log(f"[PEDIDOS][RPA] Tipo selecionado: {tipo_text}")
                
                # 6. Preencher Valor Pedido com 0,00 (OTIMIZADO - sequência rápida)
                valor_input = page.locator('#Valor')
                await valor_input.click()
                current_value = await valor_input.input_value()
                if not current_value or current_value.strip() == "":
                    await valor_input.fill("0,00")
                
                # Disparar eventos (compactado)
                await valor_input.evaluate("el => ['input','change','blur'].forEach(ev => el.dispatchEvent(new Event(ev,{bubbles:true})))")
                
                # 7. Salvar (OTIMIZADO - wait inteligente)
                save_btn = page.locator('.modal button.btn-primary.salvar:not(.salvarfechar)').first
                await save_btn.click()
                
                # ✅ OTIMIZADO: Wait inteligente para modal fechar
                try:
                    await page.locator('.modal.in, .modal.show').wait_for(state='hidden', timeout=3000)
                    success_count += 1
                    existing_pedidos.add(tipo_norm)
                    # Log a cada 5 pedidos para não poluir
                    if success_count % 5 == 0:
                        log(f"[PEDIDOS][RPA] ⚡ {success_count} pedidos adicionados...")
                except:
                    # Modal não fechou = erro de validação
                    try:
                        error_msg = await page.locator('.modal .validation-summary-errors, .modal .field-validation-error').first.text_content()
                        if error_msg:
                            log(f"[PEDIDOS][RPA][WARN] Erro validação: {error_msg.strip()[:50]}")
                    except:
                        pass
                    
                    # Fechar modal rapidamente
                    await page.evaluate("""() => {
                        document.querySelectorAll('.modal.in button.close, .modal.show button.close').forEach(btn => btn.click());
                        document.querySelectorAll('.modal.in, .modal.show').forEach(m => { m.classList.remove('in', 'show'); m.style.display = 'none'; });
                        document.querySelectorAll('.modal-backdrop').forEach(el => el.remove());
                    }""")
                    await short_sleep_ms(PEDIDOS_FAST_WAIT_MS)
                
            except Exception as e:
                log(f"[PEDIDOS][RPA][ERRO] Pedido {idx + 1}: {str(e)[:50]}")
                try:
                    await page.evaluate("document.querySelector('.modal.in button.close, #dialog-modal button.close')?.click()")
                    await short_sleep_ms(PEDIDOS_FAST_WAIT_MS)
                except:
                    pass
        
        # ✅ Log de tempo total
        elapsed = time.time() - start_time
        log(f"[PEDIDOS][RPA] ⚡ Adicionados {success_count}/{len(tipos_pedidos)} pedidos em {elapsed:.1f}s (pulados: {skipped_count} duplicatas)")
        
        # 9. Capturar screenshot da aba Pedidos (com timeout dinâmico baseado na quantidade)
        if success_count > 0:
            await _take_pedidos_screenshot(page, process_id, success_count=success_count)
            update_status("pedidos_ok", f"✅ {success_count} pedido(s) adicionado(s)", process_id=process_id)
        
        return success_count > 0
            
    except Exception as e:
        log(f"[PEDIDOS][RPA][ERRO] Falha ao adicionar pedidos: {e}")
        update_status("pedidos_erro", f"Erro ao adicionar pedidos: {e}", process_id=process_id)
        return False


# ✅ CATÁLOGO ESTÁTICO DE TIPOS DE PEDIDOS DO ELAW (481 tipos)
# Gerado em 2025-11-27 a partir do dropdown #TipoPedidoId
# Usado para mapeamento inteligente sem precisar abrir modal

def _load_elaw_tipos_catalogo() -> list:
    """Carrega catálogo de tipos de pedidos do eLaw do arquivo JSON."""
    import json
    import os
    try:
        catalog_path = os.path.join(os.path.dirname(__file__), 'data', 'elaw_tipos_pedidos.json')
        with open(catalog_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        log(f"[PEDIDOS][CATALOG] Erro ao carregar catálogo: {e}")
        return []

# Catálogo em memória (carregado uma vez)
_ELAW_TIPOS_CACHE = None

def _get_elaw_tipos_catalogo() -> list:
    """Retorna catálogo de tipos de pedidos (com cache)."""
    global _ELAW_TIPOS_CACHE
    if _ELAW_TIPOS_CACHE is None:
        _ELAW_TIPOS_CACHE = _load_elaw_tipos_catalogo()
    return _ELAW_TIPOS_CACHE

# ============================================================================
# 🎯 SISTEMA INTELIGENTE DE PRIORIZAÇÃO DE PEDIDOS
# ============================================================================
# Estratégia: Pedidos são categorizados por importância e priorizados
# quando o total excede o limite, garantindo que os mais relevantes
# sejam sempre inseridos primeiro.
# ============================================================================

# ✅ LIMITE MÁXIMO DE PEDIDOS POR BATCH
# Este limite protege a estabilidade do RPA e evita sobrecarga do sistema eLaw.
# Pode ser ajustado conforme necessidade operacional.
MAX_PEDIDOS_PARA_INSERIR = 30  # Aumentado para 30 (limite seguro testado)

# ✅ CATEGORIAS DE PRIORIDADE (peso 1-5, maior = mais importante)
# Verbas rescisórias são obrigatórias em qualquer ação trabalhista
CATEGORIA_PRIORIDADE = {
    # PRIORIDADE 5 - VERBAS RESCISÓRIAS ESSENCIAIS (sempre incluir)
    "rescisorias_essenciais": 5,
    # PRIORIDADE 4 - VERBAS SALARIAIS BÁSICAS
    "salariais_basicas": 4,
    # PRIORIDADE 3 - ADICIONAIS E EXTRAS
    "adicionais": 3,
    # PRIORIDADE 2 - INDENIZAÇÕES E DANOS
    "indenizatorios": 2,
    # PRIORIDADE 1 - MULTAS E ACESSÓRIOS
    "acessorios": 1,
}

# ✅ KEYWORDS TRABALHISTAS COM CATEGORIAS E PRIORIDADES
# Cada keyword tem: [lista_de_termos, categoria, prioridade]
PEDIDO_KEYWORDS_PRIORITARIOS = {
    # === PRIORIDADE 5: RESCISÓRIAS ESSENCIAIS ===
    "saldo de salário": {
        "termos": ["saldo de salário", "saldo salário", "saldo de salario"],
        "categoria": "rescisorias_essenciais",
        "prioridade": 5
    },
    "aviso prévio": {
        "termos": ["aviso prévio", "aviso-prévio", "aviso previo"],
        "categoria": "rescisorias_essenciais",
        "prioridade": 5
    },
    "13º salário": {
        "termos": ["13º", "décimo terceiro", "13 salário", "gratificação natalina"],
        "categoria": "rescisorias_essenciais",
        "prioridade": 5
    },
    "férias": {
        "termos": ["férias", "ferias", "terço de férias", "1/3 de férias"],
        "categoria": "rescisorias_essenciais",
        "prioridade": 5
    },
    "fgts": {
        "termos": ["fgts", "fundo de garantia", "multa 40%", "multa do fgts"],
        "categoria": "rescisorias_essenciais",
        "prioridade": 5
    },
    "seguro desemprego": {
        "termos": ["seguro desemprego", "seguro-desemprego", "guias sd"],
        "categoria": "rescisorias_essenciais",
        "prioridade": 5
    },
    
    # === PRIORIDADE 4: SALARIAIS BÁSICAS ===
    "horas extras": {
        "termos": ["hora extra", "horas extras", "sobrejornada", "jornada extraordin"],
        "categoria": "salariais_basicas",
        "prioridade": 4
    },
    "adicional noturno": {
        "termos": ["adicional noturno", "horário noturno"],
        "categoria": "salariais_basicas",
        "prioridade": 4
    },
    "equiparação": {
        "termos": ["equiparação salarial", "diferença salarial"],
        "categoria": "salariais_basicas",
        "prioridade": 4
    },
    "vínculo": {
        "termos": ["vínculo", "reconhecimento de vínculo", "anotação ctps"],
        "categoria": "salariais_basicas",
        "prioridade": 4
    },
    
    # === PRIORIDADE 3: ADICIONAIS ===
    "insalubridade": {
        "termos": ["insalubridade", "insalubre", "adicional de insalubridade"],
        "categoria": "adicionais",
        "prioridade": 3
    },
    "periculosidade": {
        "termos": ["periculosidade", "periculoso", "adicional de periculosidade"],
        "categoria": "adicionais",
        "prioridade": 3
    },
    "acúmulo de função": {
        "termos": ["acúmulo de função", "acumulo de funcao", "desvio de função"],
        "categoria": "adicionais",
        "prioridade": 3
    },
    
    # === PRIORIDADE 2: INDENIZATÓRIOS ===
    "danos morais": {
        "termos": ["dano moral", "danos morais", "indenização moral", "assédio moral"],
        "categoria": "indenizatorios",
        "prioridade": 2
    },
    "danos materiais": {
        "termos": ["dano material", "danos materiais", "indenização material"],
        "categoria": "indenizatorios",
        "prioridade": 2
    },
    
    # === PRIORIDADE 1: ACESSÓRIOS ===
    "multa art 477": {
        "termos": ["multa 477", "art. 477", "artigo 477", "atraso rescisão"],
        "categoria": "acessorios",
        "prioridade": 1
    },
    "multa art 467": {
        "termos": ["multa 467", "art. 467", "artigo 467", "verbas incontroversas"],
        "categoria": "acessorios",
        "prioridade": 1
    },
    "honorários": {
        "termos": ["honorários", "honorarios", "sucumbência"],
        "categoria": "acessorios",
        "prioridade": 1
    },
    "justiça gratuita": {
        "termos": ["justiça gratuita", "gratuidade", "assistência judiciária"],
        "categoria": "acessorios",
        "prioridade": 1
    },
}

def _get_keyword_termos(keyword_name: str) -> list:
    """Retorna lista de termos para uma keyword (compatibilidade)."""
    info = PEDIDO_KEYWORDS_PRIORITARIOS.get(keyword_name, {})
    return info.get("termos", []) if isinstance(info, dict) else info

def _get_keyword_prioridade(keyword_name: str) -> int:
    """Retorna prioridade de uma keyword (1-5)."""
    info = PEDIDO_KEYWORDS_PRIORITARIOS.get(keyword_name, {})
    return info.get("prioridade", 1) if isinstance(info, dict) else 1

def _map_pedidos_with_catalog(pedidos_list: list, objeto: str = "") -> list:
    """
    Mapeia pedidos extraídos do PDF para tipos do catálogo eLaw usando keywords.
    
    🚀 OTIMIZADO 2025-11-27:
    - Usa catálogo estático (481 tipos) sem precisar abrir modal
    - Matching por keywords prioritárias trabalhistas COM PRIORIZAÇÃO
    - Limite máximo de MAX_PEDIDOS_PARA_INSERIR (30)
    - Ordena por prioridade (rescisórias primeiro)
    - Log de pedidos omitidos quando excede limite
    
    Args:
        pedidos_list: Lista de pedidos extraídos do PDF
        objeto: Objeto/assunto do processo
        
    Returns:
        Lista de dicts com {value, text, prioridade} dos tipos a selecionar
    """
    from rapidfuzz import fuzz
    
    catalogo = _get_elaw_tipos_catalogo()
    if not catalogo:
        log("[PEDIDOS][MAP] Catálogo vazio - usando fallback")
        return []
    
    texto_busca = " ".join(pedidos_list) + " " + objeto
    texto_busca_lower = texto_busca.lower()
    
    tipos_encontrados = []
    tipos_values_usados = set()
    
    log(f"[PEDIDOS][MAP] Mapeando {len(pedidos_list)} pedidos com catálogo de {len(catalogo)} tipos")
    
    # 1. FASE 1: Matching por keywords prioritárias (com priorização)
    keywords_usadas = set()
    
    for tipo in catalogo:
        tipo_text_lower = tipo.get('text_lower', tipo.get('text', '').lower())
        tipo_value = tipo.get('value', '')
        tipo_text = tipo.get('text', '')
        
        if not tipo_value or tipo_value in tipos_values_usados:
            continue
        
        # Verificar match com keywords prioritárias
        for keyword_name, keyword_info in PEDIDO_KEYWORDS_PRIORITARIOS.items():
            if keyword_name in keywords_usadas:
                continue
            
            # Obter termos e prioridade da keyword
            termos = keyword_info.get("termos", []) if isinstance(keyword_info, dict) else keyword_info
            prioridade = keyword_info.get("prioridade", 1) if isinstance(keyword_info, dict) else 1
            categoria = keyword_info.get("categoria", "outros") if isinstance(keyword_info, dict) else "outros"
            
            keyword_found_in_pedidos = any(kw in texto_busca_lower for kw in termos)
            keyword_found_in_tipo = any(kw in tipo_text_lower for kw in termos)
            
            if keyword_found_in_pedidos and keyword_found_in_tipo:
                tipos_encontrados.append({
                    "value": tipo_value,
                    "text": tipo_text,
                    "prioridade": prioridade,
                    "categoria": categoria,
                    "score": prioridade * 20,  # Score baseado em prioridade
                    "match_type": "keyword",
                    "keyword": keyword_name
                })
                tipos_values_usados.add(tipo_value)
                keywords_usadas.add(keyword_name)
                log(f"[PEDIDOS][MAP] ✅ P{prioridade} [{categoria}] '{keyword_name}' -> {tipo_text}")
                break
    
    # 2. FASE 2: Se poucos matches, usar fuzzy matching
    if len(tipos_encontrados) < 5 and pedidos_list:
        log(f"[PEDIDOS][MAP] Poucos matches ({len(tipos_encontrados)}) - usando fuzzy matching")
        
        for pedido in pedidos_list[:10]:
            pedido_lower = pedido.lower()[:100]
            
            for tipo in catalogo:
                tipo_value = tipo.get('value', '')
                tipo_text = tipo.get('text', '')
                tipo_text_lower = tipo.get('text_lower', tipo_text.lower())
                
                if tipo_value in tipos_values_usados:
                    continue
                
                score = fuzz.partial_ratio(pedido_lower, tipo_text_lower)
                
                if score >= 75:
                    tipos_encontrados.append({
                        "value": tipo_value,
                        "text": tipo_text,
                        "prioridade": 0,  # Fuzzy tem menor prioridade
                        "categoria": "fuzzy",
                        "score": score,
                        "match_type": "fuzzy"
                    })
                    tipos_values_usados.add(tipo_value)
                    log(f"[PEDIDOS][MAP] ✅ P0 [fuzzy] ({score}%): {tipo_text[:50]}")
                    break
    
    # 3. ORDENAR POR PRIORIDADE (maior primeiro) e depois por score
    tipos_encontrados.sort(key=lambda x: (x.get('prioridade', 0), x.get('score', 0)), reverse=True)
    
    total_encontrado = len(tipos_encontrados)
    
    # 4. APLICAR LIMITE e LOGAR OMITIDOS
    if total_encontrado > MAX_PEDIDOS_PARA_INSERIR:
        tipos_omitidos = tipos_encontrados[MAX_PEDIDOS_PARA_INSERIR:]
        tipos_final = tipos_encontrados[:MAX_PEDIDOS_PARA_INSERIR]
        
        # ⚠️ LOG IMPORTANTE: Pedidos omitidos por exceder limite
        log(f"[PEDIDOS][MAP] ⚠️ LIMITE ATINGIDO: {total_encontrado} encontrados, inserindo {MAX_PEDIDOS_PARA_INSERIR}")
        log(f"[PEDIDOS][MAP] ⚠️ PEDIDOS OMITIDOS ({len(tipos_omitidos)}):")
        for omitido in tipos_omitidos:
            log(f"[PEDIDOS][MAP]    - P{omitido.get('prioridade', '?')} [{omitido.get('categoria', '?')}] {omitido.get('text', '?')}")
    else:
        tipos_final = tipos_encontrados
    
    # 5. Resumo por categoria
    categorias_count = {}
    for t in tipos_final:
        cat = t.get('categoria', 'outros')
        categorias_count[cat] = categorias_count.get(cat, 0) + 1
    
    log(f"[PEDIDOS][MAP] Total mapeado: {len(tipos_final)} tipos (máx {MAX_PEDIDOS_PARA_INSERIR})")
    log(f"[PEDIDOS][MAP] Por categoria: {categorias_count}")
    
    return tipos_final

async def _map_pedidos_to_tipos(page, pedidos_list: list, objeto: str) -> list:
    """
    Mapeia pedidos extraídos do PDF para os tipos do select do eLaw.
    
    🚀 OTIMIZADO 2025-11-27:
    - Primeiro tenta usar catálogo estático (rápido, sem abrir modal)
    - Fallback para leitura do select se catálogo falhar
    - Limite máximo de 25 pedidos
    
    Args:
        page: Playwright page
        pedidos_list: Lista de pedidos extraídos
        objeto: Objeto/assunto do processo
        
    Returns:
        Lista de dicts com {value, text} dos tipos a selecionar
    """
    # ✅ OTIMIZADO: Tentar usar catálogo estático primeiro (muito mais rápido)
    tipos_from_catalog = _map_pedidos_with_catalog(pedidos_list, objeto)
    
    if tipos_from_catalog:
        log(f"[PEDIDOS][MAP] ✅ Usando {len(tipos_from_catalog)} tipos do catálogo estático")
        return tipos_from_catalog
    
    # ❌ FALLBACK: Se catálogo falhou, ler do select (mais lento)
    log("[PEDIDOS][MAP] Catálogo falhou - lendo tipos do select...")
    
    tipos_encontrados = []
    tipos_values_usados = set()
    keywords_usadas = set()  # ✅ Deduplicação por keyword também no fallback
    
    texto_busca = " ".join(pedidos_list) + " " + objeto
    texto_busca_lower = texto_busca.lower()
    
    try:
        # Abrir modal para ter acesso ao select
        acoes_dropdown = page.locator('#box-pedidos .btn-group button.btn-acoes.dropdown-toggle').first
        await acoes_dropdown.click(timeout=5000)
        await short_sleep_ms(500)
        
        novo_pedido = page.locator('#buttonNewPedido')
        await novo_pedido.click(timeout=5000)
        
        await page.wait_for_selector('.modal.in #TipoPedidoId, .modal.show #TipoPedidoId', state='visible', timeout=5000)
        await page.wait_for_function("document.querySelector('#TipoPedidoId')?.options?.length > 1", timeout=5000)
        
        tipo_select = page.locator('#TipoPedidoId')
        options = await tipo_select.locator('option').all()
        log(f"[PEDIDOS][MAP] Total de opções no select: {len(options)}")
        
        for option in options:
            option_text = await option.text_content()
            option_value = await option.get_attribute('value')
            
            if not option_value or option_value == "":
                continue
                
            option_lower = (option_text or "").lower()
            
            # Match por keywords prioritárias (com deduplicação)
            for keyword_name, keywords in PEDIDO_KEYWORDS_PRIORITARIOS.items():
                # ✅ Pular se já usamos essa keyword
                if keyword_name in keywords_usadas:
                    continue
                    
                if any(kw in texto_busca_lower for kw in keywords):
                    if any(kw in option_lower for kw in keywords):
                        if option_value not in tipos_values_usados:
                            tipos_encontrados.append({
                                "value": option_value,
                                "text": option_text
                            })
                            tipos_values_usados.add(option_value)
                            keywords_usadas.add(keyword_name)  # ✅ Marcar keyword
                            log(f"[PEDIDOS][MAP] ✅ Match: {keyword_name} -> {option_text}")
                            break  # ✅ Sair do loop de keywords após match
            
            if len(tipos_encontrados) >= MAX_PEDIDOS_PARA_INSERIR:
                break
        
        # Fechar modal
        try:
            await page.evaluate("document.querySelector('#dialog-modal button.close, .modal.in button.close')?.click()")
            await short_sleep_ms(300)
        except:
            pass
        
    except Exception as e:
        log(f"[PEDIDOS][MAP][WARN] Erro ao mapear tipos: {e}")
        try:
            await page.evaluate("document.querySelector('#dialog-modal button.close')?.click()")
        except:
            pass
    
    log(f"[PEDIDOS][MAP] Mapeados {len(tipos_encontrados)} tipos (máx {MAX_PEDIDOS_PARA_INSERIR})")
    return tipos_encontrados[:MAX_PEDIDOS_PARA_INSERIR]


def _compute_pedidos_wait_ms(success_count: int) -> int:
    """
    Calcula timeout dinâmico baseado na quantidade de pedidos.
    Fórmula: base_ms + (per_item_ms * max(0, count - base_count))
    Clamped entre min_ms e max_ms.
    """
    base_ms = 1500      # Tempo base para poucos pedidos
    per_item_ms = 200   # 200ms extra por pedido acima do base
    base_count = 3      # Abaixo disso, usa apenas o base
    min_ms = 1500       # Mínimo de 1.5s
    max_ms = 15000      # Máximo de 15s (para ~60+ pedidos)
    
    extra_items = max(0, success_count - base_count)
    computed = base_ms + (per_item_ms * extra_items)
    result = max(min_ms, min(computed, max_ms))
    
    return result


async def _take_pedidos_screenshot(page, process_id: int, success_count: int = 1):
    """
    Captura screenshot da aba "Pedidos" após inserir pedidos.
    Salva o caminho em Process.elaw_screenshot_pedidos_path.
    
    Args:
        page: Playwright page
        process_id: ID do processo
        success_count: Quantidade de pedidos inseridos (para timeout dinâmico)
    """
    # ✅ DYNAMIC TIMEOUT: Calcular timeout baseado na quantidade de pedidos
    dynamic_wait_ms = _compute_pedidos_wait_ms(success_count)
    log(f"[PEDIDOS][RPA][SHOT] Capturando screenshot da aba Pedidos (pedidos={success_count}, timeout={dynamic_wait_ms}ms)...")
    update_status("pedidos_screenshot", f"Capturando screenshot dos pedidos ({success_count} itens)...", process_id=process_id)
    
    try:
        # ✅ MODAL CHECK: Garantir que nenhum modal está aberto antes de continuar
        for modal_attempt in range(3):
            try:
                modal_visible = await page.locator('.modal.in, .modal.show, #dialog-modal.in').first.is_visible()
                if modal_visible:
                    log(f"[PEDIDOS][RPA][SHOT] Modal detectado! Fechando (tentativa {modal_attempt + 1})...")
                    await page.evaluate("""() => {
                        // Fechar todos os modals abertos
                        document.querySelectorAll('.modal.in button.close, .modal.show button.close, #dialog-modal button.close').forEach(btn => btn.click());
                        // Remover backdrop também
                        document.querySelectorAll('.modal-backdrop').forEach(el => el.remove());
                    }""")
                    await short_sleep_ms(1000)
                else:
                    log("[PEDIDOS][RPA][SHOT] ✅ Nenhum modal aberto")
                    break
            except:
                break
        
        # Aguardar modals desaparecerem completamente
        try:
            await page.wait_for_selector('.modal.in, .modal.show', state='hidden', timeout=3000)
        except:
            pass
        
        # Garantir que aba está visível (com timeout curto e fallback)
        try:
            await page.click('a[href="#box-pedidos"]', timeout=5000)
        except:
            log("[PEDIDOS][RPA][SHOT] Clique timeout - usando JavaScript...")
            await page.evaluate("""() => {
                const tab = document.querySelector('a[href="#box-pedidos"]');
                if (tab) tab.click();
            }""")
        await short_sleep_ms(1000)
        
        # ✅ WAIT FOR ROWS: Aguardar até que a tabela tenha as linhas esperadas
        try:
            await page.wait_for_function(
                """(expectedCount) => {
                    const tbody = document.querySelector('#box-pedidos table tbody');
                    if (!tbody) return false;
                    const rows = tbody.querySelectorAll('tr');
                    return rows.length >= expectedCount;
                }""",
                arg=success_count,
                timeout=dynamic_wait_ms
            )
            log(f"[PEDIDOS][RPA][SHOT] ✅ Tabela tem >= {success_count} linhas")
        except Exception as wait_ex:
            log(f"[PEDIDOS][RPA][SHOT] Timeout aguardando {success_count} linhas, continuando mesmo assim...")
        
        # ✅ HEIGHT STABILIZATION: Aguardar estabilização da altura da tabela
        try:
            await page.wait_for_function(
                """() => {
                    const container = document.querySelector('#box-pedidos');
                    if (!container) return true;
                    if (!window._lastHeight) {
                        window._lastHeight = container.scrollHeight;
                        return false;
                    }
                    if (container.scrollHeight !== window._lastHeight) {
                        window._lastHeight = container.scrollHeight;
                        return false;
                    }
                    return true;
                }""",
                timeout=3000
            )
            log("[PEDIDOS][RPA][SHOT] ✅ Altura da tabela estabilizada")
        except:
            log("[PEDIDOS][RPA][SHOT] Height stabilization timeout, continuando...")
        
        # Pequeno delay final para garantir renderização completa
        await short_sleep_ms(500)
        
        # Gerar nome do arquivo
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_filename = f"process_{process_id}_{timestamp}_pedidos.png"
        
        screenshot_dir = Path("static") / "rpa_screenshots"
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = screenshot_dir / screenshot_filename
        
        await page.screenshot(path=str(screenshot_path), full_page=True)
        log(f"[PEDIDOS][RPA][SHOT] Screenshot salvo: {screenshot_path}")
        
        # Salvar no banco de dados
        if flask_app:
            from models import Process, db
            with flask_app.app_context():
                proc = Process.query.get(process_id)
                if proc:
                    proc.elaw_screenshot_pedidos_path = screenshot_filename
                    db.session.commit()
                    log(f"[PEDIDOS][RPA][SHOT] ✅ Caminho salvo no banco: {screenshot_filename}")
        
        send_screenshot_to_monitor(screenshot_path, region="PEDIDOS")
        
    except Exception as e:
        log(f"[PEDIDOS][RPA][SHOT][ERRO] Falha ao capturar screenshot: {e}")


# =========================
# Pós-login / Orquestração
# =========================
async def ensure_on_new_process_form(page, process_id: int = None):
    """
    🔧 BATCH FIX: Garante que estamos no formulário 'Novo Processo' vazio.
    Critical para batch processing onde o browser é reutilizado entre processos.
    Usa navegação para about:blank para limpar TOTALMENTE o estado JS/DOM.
    """
    current_url = page.url
    form_url_pattern = r"/Processo/form"
    
    log("[BATCH] 🧹 Limpando estado do navegador para novo processo...")
    if process_id:
        update_status("limpando_estado", "Limpando estado do navegador...", process_id=process_id)
    
    # 1. Navegar para about:blank para matar qualquer JS/AJAX pendente
    try:
        log("[BATCH][DEBUG] Passo 1: about:blank...")
        await page.goto("about:blank")
        await short_sleep_ms(200)
        log("[BATCH][DEBUG] Passo 1: OK")
    except Exception as e:
        log(f"[BATCH][DEBUG] Passo 1: falhou (ignorando): {e}")
        pass
    
    # 2. Voltar ao dashboard para garantir sessão logada (com detecção de redirect para login)
    if process_id:
        update_status("verificando_sessao", "Verificando sessão logada...", process_id=process_id)
    max_dashboard_attempts = 3  # Aumentado de 2 para 3 tentativas
    for dashboard_attempt in range(max_dashboard_attempts):
        try:
            dashboard_url = BASE_URL.rstrip("/") + "/Home/Index"
            log(f"[BATCH] Navegando para dashboard (tentativa {dashboard_attempt + 1}/{max_dashboard_attempts}): {dashboard_url}")
            log(f"[BATCH][DEBUG] URL atual antes da navegação: {page.url}")
            if process_id:
                update_status("navegando_dashboard", f"Acessando dashboard ({dashboard_attempt + 1}/{max_dashboard_attempts})...", process_id=process_id)
            await page.goto(dashboard_url, wait_until="domcontentloaded", timeout=180000)  # 180s (3 min) - igual ao login para produção
            
            # Verificar se fomos redirecionados para login
            current_url = page.url
            log(f"[BATCH][DEBUG] URL após navegação dashboard: {current_url}")
            if "/Account/Login" in current_url or "/Login" in current_url:
                log(f"[BATCH][WARN] Sessão expirada - redirecionado para login. Tentando relogar...")
                # Não temos credenciais aqui - precisamos que o caller tenha feito login
                # Lançar erro para que o fluxo superior trate
                raise RuntimeError("Sessão expirada - necessário fazer login novamente")
            
            # Verificar se navegação foi bem-sucedida
            await page.wait_for_url(re.compile(r"/Home/Index"), timeout=5000)
            await short_sleep_ms(500)
            log(f"[BATCH] ✅ Dashboard carregado com sucesso")
            break  # Sucesso - sair do loop
        except Exception as e:
            log(f"[BATCH][WARN] Tentativa {dashboard_attempt + 1} falhou: {e}")
            log(f"[BATCH][WARN] URL atual: {page.url}")
            
            if dashboard_attempt == max_dashboard_attempts - 1:
                # Última tentativa falhou - levantar erro
                log(f"[BATCH][ERROR] Todas as tentativas de navegar para o dashboard falharam")
                log(f"[BATCH][ERROR] Último erro: {str(e)}")
                log(f"[BATCH][ERROR] URL final: {page.url}")
                raise RuntimeError(f"Falha ao garantir sessão logada após {max_dashboard_attempts} tentativas: Page.goto: Timeout - verifique conexão com eLaw")
            
            # Tentar novamente após pausa maior (produção pode precisar de mais tempo)
            wait_ms = 2000 * (dashboard_attempt + 1)  # 2s, 4s, 6s...
            log(f"[BATCH] Aguardando {wait_ms}ms antes de retry...")
            await short_sleep_ms(wait_ms)
    
    # 3. Navegar limpo para o formulário
    target = BASE_URL.rstrip("/") + "/Processo/form"
    log(f"[BATCH] Navegando para formulário: {target}")
    if process_id:
        update_status("abrindo_formulario", "Abrindo formulário de novo processo...", process_id=process_id)
    
    try:
        await goto_with_retries(page, target, attempts=3, nav_timeout_ms=90000)  # 90s por tentativa, 3 tentativas
        log(f"[BATCH][DEBUG] Navegação para form concluída, aguardando URL...")
        await page.wait_for_url(re.compile(form_url_pattern), timeout=60000)  # 60s - tolerante a redirecionamento lento
        log(f"[BATCH][DEBUG] URL do form confirmada, ajustando zoom...")
        await ensure_zoom_100(page, "form_batch")
        log("[BATCH] ✅ Formulário 'Novo Processo' aberto e limpo")
        if process_id:
            update_status("formulario_pronto", "Formulário pronto para preenchimento", process_id=process_id)
    except Exception as e:
        log(f"[BATCH][ERROR] Falha ao abrir formulário: {e}")
        raise

async def after_login_flow(page, process_id: int):
    """2025-11-21: process_id OBRIGATÓRIO (ZERO shared state)"""
    update_status("navegacao", "Acessando formulário de cadastro", process_id=process_id)
    log(f"[FLOW][DEBUG] after_login_flow iniciado para processo #{process_id}")
    try:
        await page.wait_for_selector("nav, header, .navbar, .navbar-fixed-top", timeout=2500)
        log(f"[FLOW][DEBUG] Navbar detectada - sessão parece ativa")
    except Exception:
        log(f"[FLOW][DEBUG] Navbar não encontrada - continuando mesmo assim")
        pass
    
    # 🔧 BATCH FIX: Garantir que estamos no formulário vazio
    update_status("navegacao_form", "Preparando formulário limpo...", process_id=process_id)
    log(f"[FLOW][DEBUG] Chamando ensure_on_new_process_form...")
    await ensure_on_new_process_form(page, process_id=process_id)
    log(f"[FLOW][DEBUG] ensure_on_new_process_form concluído")

    # Catálogos removidos para otimização - não são necessários para preenchimento
    log("[CATALOG] Snapshot de catálogos desabilitado para otimização de velocidade")

    data = load_process_data_for_fill(process_id)
    if not data:
        log("[FORM][WARN] sem dados — encerrando após abrir formulário")
        return

    # Carregar PDF ESPECÍFICO do processo (evita mistura de dados entre processos)
    # Try/except permite execução com apenas dados do banco se PDF não disponível
    try:
        pdf_text = get_process_pdf_text(data, process_id=process_id)
        log(f"[PDF] ✅ Texto do PDF carregado ({len(pdf_text)} chars)")
        data["_pdf_text"] = pdf_text
    except (ValueError, FileNotFoundError) as e:
        log(f"[PDF][WARN] PDF não disponível: {e}")
        log(f"[PDF][WARN] Continuando apenas com dados do banco de dados...")
        data["_pdf_text"] = ""  # Vazio - funções vão usar apenas dados do banco

    await fill_new_process_form(page, data, process_id=process_id)

# =========================
# Runner
# =========================
async def perform_login(page, user: str, pwd: str, process_id: int):
    """2025-11-21: process_id OBRIGATÓRIO"""
    ok = await login_elaw(page, user, pwd, BASE_URL)
    if not ok:
        try:
            png = _get_screenshot_path("elaw_login_form_nao_encontrado.png", process_id=process_id)
            await page.screenshot(path=str(png), full_page=True)
            log(f"[SHOT] login_falha: {png}")
        except Exception:
            pass
        raise RuntimeError("Não foi possível efetuar o login no eLaw.")

async def run_elaw_login_once(process_id: int):
    """
    2025-11-21: process_id agora OBRIGATÓRIO (ZERO shared state)
    
    🔧 2025-12-02: Seta contextvar dentro do async para garantir propagação correta
    em execuções paralelas via asyncio.run()
    """
    # ✅ CRÍTICO: Setar contexto DENTRO da função async para garantir propagação
    # asyncio.run() cria novo event loop, contextvars não propagam automaticamente
    ctx = RPAExecutionContext(process_id=process_id)
    ctx_token = set_rpa_context(ctx)
    log(f"[RPA][ASYNC] Contexto setado dentro de run_elaw_login_once para processo #{process_id}")
    
    try:
        validate_env()
        _init_status(process_id)  # Inicializa sistema de status
        update_status("abrindo_navegador", "Abrindo navegador Chromium em modo headless...", process_id=process_id)
    
        # 🔒 CRITICAL: try/finally garante que status NUNCA fica 'running' após RPA terminar
        async with launch_browser() as page:
            try:
                update_status("fazendo_login", "Fazendo login no eLaw...", process_id=process_id)
                await perform_login(page, ELAW_USER, ELAW_PASS, process_id)
                update_status("login_sucesso", "Login realizado com sucesso!", process_id=process_id)
                await after_login_flow(page, process_id=process_id)
                if KEEP_OPEN_AFTER_LOGIN_SECONDS > 0:
                    log(f"Janela aberta por {KEEP_OPEN_AFTER_LOGIN_SECONDS:.1f}s p/ inspeção…")
                    await asyncio.sleep(KEEP_OPEN_AFTER_LOGIN_SECONDS)
            except Exception as e:
                error_msg = f"Erro durante execução do RPA: {str(e)}"
                update_status("erro", error_msg, status="error", process_id=process_id)
                
                # Enviar erro para monitor remoto
                log_error_to_monitor(error_msg, exc=e)
                
                # 🔧 FIX PROBLEMA 2: Capturar screenshot de erro E salvar em Process.elaw_screenshot_after_path
                screenshot_filename = None
                try:
                    png = _get_screenshot_path("elaw_flow_error.png", process_id=process_id)
                    await page.screenshot(path=str(png), full_page=True)
                    screenshot_filename = png.name  # Apenas nome do arquivo, sem path
                    log(f"[SHOT][ERRO] {png}")
                    send_screenshot_to_monitor(png, region="RPA_ERROR")
                except Exception as screenshot_ex:
                    log(f"[SHOT][ERRO] Falha ao capturar screenshot: {screenshot_ex}")
                
                # Atualizar status de erro no banco de dados + screenshot
                if process_id and flask_app:
                    try:
                        from models import Process, db
                        with flask_app.app_context():
                            proc = Process.query.get(process_id)
                            if proc:
                                proc.elaw_status = 'error'
                                proc.elaw_error_message = error_msg
                                
                                # 🎯 CRITICAL: Salvar screenshot de erro para UI exibir botão
                                # ✅ FIX: Salvar APENAS o nome do arquivo.
                                if screenshot_filename:
                                    proc.elaw_screenshot_after_path = screenshot_filename
                                    log(f"[SCREENSHOT ERROR] ✅ Caminho salvo no banco: {proc.elaw_screenshot_after_path}")
                                
                                db.session.commit()
                                log(f"[RPA][ERROR] Processo #{process_id} marcado como 'error': {error_msg}")
                    except Exception as ex:
                        log(f"[RPA][WARN] Não foi possível atualizar status de erro no banco: {ex}")
                if not HEADLESS:
                    log("[ERRO] Mantendo navegador aberto por 120s para inspeção…")
                    try:
                        await asyncio.sleep(120)
                    except Exception:
                        pass
                raise
            
            # Sucesso - atualiza status final (APENAS se não foi marcado como erro antes)
            update_status("concluido", "Processo preenchido com sucesso no eLaw!", status="completed", process_id=process_id)
            
            # Atualizar status do processo no banco de dados - FINAL DO FLUXO COMPLETO
            if process_id and flask_app:
                try:
                    from models import Process, db
                    from datetime import datetime
                    with flask_app.app_context():
                        proc = Process.query.get(process_id)
                        if proc:
                            # ✅ AGORA é o momento certo de definir 'success'
                            # Status válidos para atualização: running, processing (intermediário)
                            if proc.elaw_status in ('running', 'processing'):
                                proc.elaw_status = 'success'
                                proc.status = 'completed'
                                proc.elaw_filled_at = datetime.utcnow()
                                proc.elaw_error_message = None
                                db.session.commit()
                                log(f"[RPA][SUCCESS] ✅ Processo #{process_id} marcado como 'success' / 'completed' (fluxo completo)")
                            elif proc.elaw_status == 'error':
                                log(f"[RPA][SUCCESS] Processo #{process_id} já tinha status 'error' - mantendo")
                            else:
                                log(f"[RPA][SUCCESS] Processo #{process_id} já tinha status: {proc.elaw_status} - mantendo")
                except Exception as e:
                    log(f"[RPA][WARN] Não foi possível atualizar status no banco: {e}")
    
    finally:
        # 🔒 WATCHDOG FINAL: Garante que status NUNCA fica 'running' ou 'processing' se RPA terminou
        # Isso captura crashes, timeouts, SIGKILL, browser crash, etc
        if process_id and flask_app:
            try:
                from models import Process, db
                with flask_app.app_context():
                    proc = Process.query.get(process_id)
                    if proc and proc.elaw_status in ('running', 'processing'):
                        # Se chegou aqui com status intermediário, algo deu errado
                        proc.elaw_status = 'error'
                        proc.elaw_error_message = proc.elaw_error_message or 'RPA finalizou inesperadamente sem atualizar status'
                        db.session.commit()
                        log(f"[RPA][WATCHDOG] ⚠️ Processo #{process_id} estava '{proc.elaw_status}' ao finalizar - marcado como 'error'")
            except Exception as ex:
                log(f"[RPA][WATCHDOG][ERROR] Não foi possível executar watchdog final: {ex}")
        
        # ✅ NOTA: NÃO fazemos cleanup manual de contextvars aqui!
        # O próprio asyncio.run() descarta automaticamente o contexto ao finalizar,
        # garantindo isolamento completo entre processos do batch sem race conditions.
        # Cleanup manual causaria que watchdogs/error handlers perdessem o contexto.

def run_elaw_login_sync(process_id: int):
    """
    2025-11-21: process_id OBRIGATÓRIO
    Opção B: Seta _current_process_id + usa mutex para prevenir concorrência
    """
    # 🔒 MUTEX: Prevenir execuções concorrentes (CLI pode rodar junto com web/batch)
    with _execute_rpa_lock:
        global _current_process_id
        _current_process_id = process_id
        log(f"[RPA] Lock adquirido + _current_process_id setado para #{process_id} (entry point: run_elaw_login_sync)")
        
        try:
            asyncio.run(run_elaw_login_once(process_id))
        finally:
            _current_process_id = None
            log(f"[RPA] _current_process_id limpo após run_elaw_login_sync")


def execute_rpa(process_id: int) -> dict:
    """
    Função reutilizável para executar RPA completo em um processo.
    Pode ser chamada por routes.py (interface web) ou celery_tasks.py (batch).
    
    THREAD-SAFE: Usa _execute_rpa_lock para prevenir execuções concorrentes (web + batch)
    
    Args:
        process_id: ID do processo no banco de dados
        
    Returns:
        dict com {
            'status': 'success' | 'error',
            'process_id': int,
            'message': str,
            'error': str (opcional)
        }
    """
    # 🔒 MUTEX: Prevenir execuções concorrentes (web + batch simultâneos)
    with _execute_rpa_lock:
        log(f"[EXECUTE_RPA] Lock adquirido para processo #{process_id}")
        
        # ✅ OPÇÃO B: Setar global no INÍCIO (isolamento sequencial)
        global _current_process_id
        _current_process_id = process_id
        log(f"[EXECUTE_RPA] _current_process_id setado para #{process_id}")
        
        try:
            # Inicializar RPA Monitor (se habilitado)
            _init_rpa_monitor()
            
            log(f"[EXECUTE_RPA] Iniciando RPA para processo #{process_id} (Opção B: global sequencial)")
            
            # ⚠️ CRÍTICO: Chamar asyncio.run diretamente (NÃO run_elaw_login_sync) para evitar deadlock de mutex recursivo
            asyncio.run(run_elaw_login_once(process_id))
            
            # Verificar status REAL do processo no banco após execução
            if not flask_app:
                raise Exception("Flask app não disponível - não é possível verificar status")
            
            from models import Process, db
            
            with flask_app.app_context():
                proc = Process.query.get(process_id)
                if not proc:
                    raise Exception(f"Processo #{process_id} não encontrado após RPA")
                
                # Retornar baseado no status REAL gravado pelo RPA
                if proc.elaw_status == 'success':
                    return {
                        'status': 'success',
                        'process_id': process_id,
                        'message': f'Processo #{process_id} preenchido com sucesso no eLaw'
                    }
                elif proc.elaw_status == 'error':
                    return {
                        'status': 'error',
                        'process_id': process_id,
                        'message': proc.elaw_error_message or 'Erro durante execução do RPA',
                        'error': proc.elaw_error_message
                    }
                else:
                    # Status inesperado (running, pending, etc)
                    log(f"[EXECUTE_RPA][WARNING] Status inesperado após RPA: {proc.elaw_status}")
                    return {
                        'status': 'error',
                        'process_id': process_id,
                        'message': f'RPA finalizou com status inesperado: {proc.elaw_status}',
                        'error': f'Status final: {proc.elaw_status}'
                    }
        
        except Exception as e:
            error_msg = f"Erro ao executar RPA para processo #{process_id}: {str(e)}"
            log(f"[EXECUTE_RPA][ERROR] {error_msg}")
            log_error_to_monitor(error_msg, exc=e)
            
            # 🔒 CRITICAL: Atualizar status para error (para não ficar "running")
            # Usa registry lookup para garantir que funciona mesmo fora de async context
            update_status("erro_fatal", f"Erro fatal durante execução: {str(e)[:200]}", status="error", process_id=process_id)
            
            # Também atualizar no banco
            if flask_app:
                try:
                    from models import Process, db
                    with flask_app.app_context():
                        proc = Process.query.get(process_id)
                        if proc and proc.elaw_status == 'running':
                            proc.elaw_status = 'error'
                            proc.elaw_error_message = str(e)[:500]
                            db.session.commit()
                            log(f"[EXECUTE_RPA] Processo #{process_id} marcado como 'error' no banco")
                except Exception as ex:
                    log(f"[EXECUTE_RPA][WARN] Erro ao atualizar status no banco: {ex}")
            
            return {
                'status': 'error',
                'process_id': process_id,
                'message': 'Erro durante execução do RPA',
                'error': str(e)
            }
        
        finally:
            # ✅ OPÇÃO B: Limpar global após execução (isolamento sequencial)
            _current_process_id = None
            log(f"[EXECUTE_RPA] Finalizado para processo #{process_id}, _current_process_id limpo")


def execute_rpa_parallel(process_id: int, worker_id: Optional[int] = None) -> dict:
    """
    🆕 2025-11-27: Função para execução PARALELA de RPA.
    
    Diferente de execute_rpa(), esta função:
    1. Usa contextvars (thread-local) em vez de globals
    2. Usa semáforo em vez de mutex (permite N execuções simultâneas)
    3. Cada worker tem seu próprio browser isolado
    4. Screenshots usam prefixo único por worker
    
    Pode ser chamada por múltiplas threads simultaneamente de forma segura.
    
    Args:
        process_id: ID do processo no banco de dados
        worker_id: ID opcional do worker (para logs e screenshots)
        
    Returns:
        dict com {
            'status': 'success' | 'error',
            'process_id': int,
            'worker_id': int | None,
            'message': str,
            'error': str (opcional)
        }
    """
    # Criar contexto thread-local para esta execução
    ctx = RPAExecutionContext(
        process_id=process_id,
        worker_id=worker_id
    )
    
    # Setar contexto na thread atual
    ctx_token = set_rpa_context(ctx)
    
    log(f"[EXECUTE_RPA_PARALLEL] Worker {worker_id} iniciando processo #{process_id}")
    
    # 🔒 SEMÁFORO: Permite até MAX_RPA_WORKERS execuções simultâneas
    acquired = _execute_rpa_semaphore.acquire(blocking=True, timeout=300)  # 5 min timeout
    if not acquired:
        log(f"[EXECUTE_RPA_PARALLEL] Timeout ao aguardar semáforo para processo #{process_id}")
        reset_rpa_context(ctx_token)
        return {
            'status': 'error',
            'process_id': process_id,
            'worker_id': worker_id,
            'message': 'Timeout aguardando slot disponível para RPA',
            'error': 'Semaphore timeout'
        }
    
    log(f"[EXECUTE_RPA_PARALLEL] Worker {worker_id} adquiriu semáforo para processo #{process_id}")
    
    try:
        # Inicializar RPA Monitor (se habilitado)
        _init_rpa_monitor()
        
        log(f"[EXECUTE_RPA_PARALLEL] Iniciando RPA paralelo para processo #{process_id} (worker={worker_id})")
        
        # Executar RPA - cada thread tem seu próprio event loop via asyncio.run()
        asyncio.run(run_elaw_login_once(process_id))
        
        # Verificar status REAL do processo no banco após execução
        if not flask_app:
            raise Exception("Flask app não disponível - não é possível verificar status")
        
        from models import Process, db
        
        with flask_app.app_context():
            proc = Process.query.get(process_id)
            if not proc:
                # Limpar sessão antes de lançar exceção
                db.session.remove()
                raise Exception(f"Processo #{process_id} não encontrado após RPA")
            
            # Capturar dados antes de limpar sessão
            elaw_status = proc.elaw_status
            elaw_error_message = proc.elaw_error_message
            
            # ✅ CRÍTICO: Limpar sessão IMEDIATAMENTE após leitura, ANTES do return
            db.session.remove()
        
        # Retornar baseado no status REAL gravado pelo RPA (fora do app_context)
        if elaw_status == 'success':
            return {
                'status': 'success',
                'process_id': process_id,
                'worker_id': worker_id,
                'message': f'Processo #{process_id} preenchido com sucesso no eLaw'
            }
        elif elaw_status == 'error':
            return {
                'status': 'error',
                'process_id': process_id,
                'worker_id': worker_id,
                'message': elaw_error_message or 'Erro durante execução do RPA',
                'error': elaw_error_message
            }
        else:
            # Status inesperado (running, pending, etc)
            log(f"[EXECUTE_RPA_PARALLEL][WARNING] Status inesperado após RPA: {elaw_status}")
            return {
                'status': 'error',
                'process_id': process_id,
                'worker_id': worker_id,
                'message': f'RPA finalizou com status inesperado: {elaw_status}',
                'error': f'Status final: {elaw_status}'
            }
    
    except Exception as e:
        error_msg = f"Erro ao executar RPA paralelo para processo #{process_id}: {str(e)}"
        log(f"[EXECUTE_RPA_PARALLEL][ERROR] {error_msg}")
        log_error_to_monitor(error_msg, exc=e)
        
        # 🔒 CRITICAL: Atualizar status para error (para não ficar "running")
        update_status("erro_fatal", f"Erro fatal durante execução: {str(e)[:200]}", status="error", process_id=process_id)
        
        # Também atualizar no banco
        if flask_app:
            try:
                from models import Process, db
                with flask_app.app_context():
                    proc = Process.query.get(process_id)
                    if proc and proc.elaw_status == 'running':
                        proc.elaw_status = 'error'
                        proc.elaw_error_message = str(e)[:500]
                        db.session.commit()
                        log(f"[EXECUTE_RPA_PARALLEL] Processo #{process_id} marcado como 'error' no banco")
                    # ✅ CRÍTICO: Limpar sessão após uso
                    db.session.remove()
            except Exception as ex:
                log(f"[EXECUTE_RPA_PARALLEL][WARN] Erro ao atualizar status no banco: {ex}")
        
        return {
            'status': 'error',
            'process_id': process_id,
            'worker_id': worker_id,
            'message': 'Erro durante execução do RPA',
            'error': str(e)
        }
    
    finally:
        # ✅ CRÍTICO: Garantir limpeza da sessão DB em TODOS os casos
        # Isso é uma salvaguarda - as funções acima já limpam, mas
        # se houver exceção inesperada entre a limpeza e o return, isso protege
        if flask_app:
            try:
                from models import db
                with flask_app.app_context():
                    db.session.remove()
            except Exception:
                pass  # Ignorar erros de limpeza no finally
        
        # ✅ Liberar semáforo
        _execute_rpa_semaphore.release()
        log(f"[EXECUTE_RPA_PARALLEL] Worker {worker_id} liberou semáforo para processo #{process_id}")
        
        # ✅ Resetar contexto thread-local
        reset_rpa_context(ctx_token)
        log(f"[EXECUTE_RPA_PARALLEL] Contexto resetado para processo #{process_id}")


if __name__ == "__main__":
    try:
        pid = int(ENV_PROCESS_ID) if ENV_PROCESS_ID.isdigit() else None
        log(f"[RPA][INÍCIO] Iniciando RPA com process_id={pid}, headless={HEADLESS}")
        log(f"[RPA][CONFIG] Timeouts: DEFAULT={DEFAULT_TIMEOUT_MS}ms, NAV={NAV_TIMEOUT_MS}ms")
        run_elaw_login_sync(pid)
    except Exception as e:
        log(f"[RPA][ERRO] {e}")
        import traceback
        log(f"[RPA][TRACEBACK] {traceback.format_exc()}")
