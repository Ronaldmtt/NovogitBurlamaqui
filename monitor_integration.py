"""
Módulo de integração global do RPA Monitor Client
Permite enviar logs, erros e screenshots para monitoramento remoto em tempo real
"""

import logging
import os
from pathlib import Path
from typing import Optional

LOG = logging.getLogger(__name__)

# Tentar importar cliente do monitor
try:
    from rpa_monitor_client import setup_rpa_monitor, auto_setup_rpa_monitor, rpa_log, rpa
    MONITOR_AVAILABLE = True
except ImportError as e:
    LOG.warning(f"[MONITOR] rpa-monitor-client não disponível: {e}")
    MONITOR_AVAILABLE = False
    rpa_log = None
    rpa = None

# Estado de inicialização do monitor
_monitor_initialized = False


def init_monitor(rpa_id: Optional[str] = None) -> bool:
    """
    Inicializa o RPA Monitor usando variáveis de ambiente
    Conecta imediatamente ao servidor para aparecer como "ativo"
    
    Args:
        rpa_id: ID do RPA (opcional, usa RPA_MONITOR_ID do .env se não especificado)
    
    Returns:
        True se inicializado com sucesso, False caso contrário
    """
    global _monitor_initialized
    
    LOG.info("[MONITOR] Iniciando integração do RPA Monitor...")
    
    if not MONITOR_AVAILABLE:
        LOG.warning("[MONITOR] rpa-monitor-client não disponível")
        return False
    
    # Verificar se está habilitado
    enabled = os.getenv("RPA_MONITOR_ENABLED", "false").lower() == "true"
    monitor_id = rpa_id or os.getenv("RPA_MONITOR_ID", "")
    monitor_host = os.getenv("RPA_MONITOR_HOST", "")
    monitor_region = os.getenv("RPA_MONITOR_REGION", "Sistema Juridico")
    monitor_transport = os.getenv("RPA_MONITOR_TRANSPORT", "ws")
    
    LOG.debug(f"[MONITOR] Configuração: enabled={enabled}, id={monitor_id}, region={monitor_region}")
    
    if not enabled:
        LOG.info("[MONITOR] Monitor desabilitado via RPA_MONITOR_ENABLED")
        return False
    
    if not monitor_host:
        LOG.warning("[MONITOR] RPA_MONITOR_HOST não configurado")
        return False
    
    if not monitor_id:
        LOG.warning("[MONITOR] RPA_MONITOR_ID não configurado")
        return False
    
    try:
        # Configurar RPA_MONITOR_ID se fornecido
        if rpa_id:
            os.environ["RPA_MONITOR_ID"] = rpa_id
        
        LOG.info(f"[MONITOR] Conectando ao servidor de monitoramento: {monitor_host[:60]}...")
        
        # Inicializar usando auto_setup (lê variáveis de ambiente)
        auto_setup_rpa_monitor()
        _monitor_initialized = True
        
        LOG.info(f"[MONITOR] ✅ Conectado com sucesso: {monitor_id} @ {monitor_region}")
        
        # Enviar log inicial para confirmar conexão
        try:
            rpa_log.info(f"Sistema {monitor_id} iniciado e conectado ao monitor")
        except Exception as e:
            LOG.debug(f"[MONITOR] Não foi possível enviar log inicial: {e}")
        
        return True
        
    except Exception as e:
        LOG.error(f"[MONITOR] Erro ao inicializar: {e}", exc_info=True)
        _monitor_initialized = False
        return False


def log_info(message: str, region: str = "SYSTEM"):
    """
    Envia log de informação para o monitor
    
    Args:
        message: Mensagem de log
        region: Região/módulo que originou o log
    """
    if _monitor_initialized and rpa_log:
        try:
            rpa_log.info(f"[{region}] {message}")
        except Exception:
            pass


def log_warning(message: str, region: str = "SYSTEM"):
    """
    Envia log de warning para o monitor
    
    Args:
        message: Mensagem de warning
        region: Região/módulo que originou o warning
    """
    if _monitor_initialized and rpa_log:
        try:
            rpa_log.warn(f"[{region}] {message}")
        except Exception:
            pass


def log_error(message: str, exc: Optional[Exception] = None, region: str = "SYSTEM", screenshot_path: Optional[str] = None):
    """
    Envia log de erro para o monitor COM screenshot obrigatório
    
    Args:
        message: Mensagem de erro
        exc: Exceção capturada (opcional)
        region: Região/módulo que originou o erro
        screenshot_path: Caminho do screenshot a enviar junto (obrigatório para erros)
    """
    if _monitor_initialized and rpa_log:
        try:
            full_message = f"[{region}] {message}"
            if exc:
                rpa_log.error(full_message, exc=exc, regiao=region)
            else:
                rpa_log.error(full_message, regiao=region)
            
            # Se tiver screenshot, enviar também
            if screenshot_path:
                send_screenshot(screenshot_path, region=region)
                
        except Exception:
            pass


def send_screenshot(screenshot_path, region: str = "SYSTEM"):
    """
    Envia screenshot PNG para o monitor
    
    Args:
        screenshot_path: Caminho do arquivo PNG existente
        region: Região/módulo que gerou o screenshot
    """
    if not _monitor_initialized or not rpa_log:
        return
    
    try:
        # Converter para Path se necessário
        if isinstance(screenshot_path, str):
            screenshot_path = Path(screenshot_path)
        
        # Verificar se arquivo existe
        if not screenshot_path.exists():
            LOG.warning(f"[MONITOR] Screenshot não existe: {screenshot_path}")
            return
        
        # Usar a API de screenshot do rpa_log
        rpa_log.screenshot(
            filename=screenshot_path.name,
            regiao=region
        )
        
        LOG.debug(f"[MONITOR] 📸 Screenshot enviado: {screenshot_path.name}")
        
    except Exception as e:
        LOG.warning(f"[MONITOR] Erro ao enviar screenshot: {e}")


def is_initialized() -> bool:
    """Retorna True se o monitor foi inicializado com sucesso"""
    return _monitor_initialized


def get_rpa_log():
    """Retorna a instância do rpa_log para uso direto"""
    if _monitor_initialized and rpa_log:
        return rpa_log
    return None
