"""
Sistema de status compartilhado para acompanhar progresso do RPA em tempo real.
Usa banco de dados SQLite para persistência robusta e evitar race conditions.
"""
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Any


class RPAStatusManager:
    """Gerencia o status de um processo RPA usando banco de dados"""
    
    def __init__(self, process_id: int):
        self.process_id = process_id
        
    def update(self, step: str, message: str, status: str = "running", data: Optional[Dict[str, Any]] = None):
        """
        Atualiza o status do RPA no banco de dados.
        
        Args:
            step: Nome da etapa (ex: "login", "preenchendo_cnj", "salvando")
            message: Mensagem descritiva (ex: "Fazendo login no eLaw")
            status: Status geral (starting, running, completed, error)
            data: Dados adicionais (valor preenchido, etc)
        """
        # Importação local para evitar circular imports
        from extensions import db
        from models import RPAStatus
        from flask import has_app_context
        
        def _do_update():
            # Busca registro existente ou cria novo
            rpa_status = RPAStatus.query.filter_by(process_id=self.process_id).first()
            
            if not rpa_status:
                rpa_status = RPAStatus(process_id=self.process_id)
                db.session.add(rpa_status)
            
            # Atualiza campos principais
            rpa_status.status = status
            rpa_status.current_step = step
            rpa_status.message = message
            rpa_status.data_json = json.dumps(data or {}, ensure_ascii=False)
            
            # Atualiza histórico
            history = []
            if rpa_status.history_json:
                try:
                    history = json.loads(rpa_status.history_json)
                except Exception:
                    history = []
            
            history.append({
                "step": step,
                "message": message,
                "timestamp": datetime.now().isoformat(),
                "data": data or {}
            })
            
            rpa_status.history_json = json.dumps(history, ensure_ascii=False)
            
            # Commit no banco
            db.session.commit()
            
            # Log no console
            print(f"✅ [RPA] {step}: {message}")
            if data:
                print(f"   📊 {json.dumps(data, ensure_ascii=False)}")
        
        try:
            # Se já estiver em um contexto Flask, executa diretamente
            if has_app_context():
                _do_update()
            else:
                # Cria contexto do Flask para threads isoladas
                from main import app
                with app.app_context():
                    _do_update()
                
        except Exception as e:
            print(f"❌ [RPA STATUS ERROR] Falha ao atualizar status: {e}")
            # Não falha o RPA se não conseguir atualizar status
            try:
                db.session.rollback()
            except Exception:
                pass
    
    def get_status(self) -> Optional[Dict[str, Any]]:
        """Retorna o status atual do RPA do banco de dados"""
        from models import RPAStatus, Process
        from flask import has_app_context
        
        def _do_get():
            rpa_status = RPAStatus.query.filter_by(process_id=self.process_id).first()
            
            if not rpa_status:
                return None
            
            # Converte para dict
            history = []
            if rpa_status.history_json:
                try:
                    history = json.loads(rpa_status.history_json)
                except Exception:
                    pass
            
            data = {}
            if rpa_status.data_json:
                try:
                    data = json.loads(rpa_status.data_json)
                except Exception:
                    pass
            
            # Busca dados do Process para incluir screenshots
            process = Process.query.get(self.process_id)
            
            # ✅ PRIORIZAR status do Process quando RPA já terminou
            final_status = rpa_status.status
            final_message = rpa_status.message
            
            if process:
                if process.elaw_status == 'success':
                    final_status = 'completed'
                    final_message = 'Concluído com sucesso'
                elif process.elaw_status == 'error':
                    final_status = 'error'
                    final_message = process.elaw_error_message or 'Erro no preenchimento'
                elif process.elaw_status == 'processing':
                    final_status = 'running'
                    final_message = rpa_status.message or 'Preenchendo reclamadas e pedidos...'
            
            result = {
                "process_id": rpa_status.process_id,
                "status": final_status,
                "current_step": rpa_status.current_step,
                "message": final_message,
                "data": data,
                "history": history,
                "timestamp": rpa_status.updated_at.isoformat()
            }
            
            if process:
                result["elaw_screenshot_path"] = process.elaw_screenshot_path
                result["elaw_screenshot_before_path"] = process.elaw_screenshot_before_path
                result["elaw_screenshot_after_path"] = process.elaw_screenshot_after_path
                result["elaw_screenshot_reclamadas_path"] = process.elaw_screenshot_reclamadas_path
                result["elaw_screenshot_pedidos_path"] = process.elaw_screenshot_pedidos_path
                result["elaw_detail_url"] = process.elaw_detail_url
            
            return result
        
        try:
            if has_app_context():
                return _do_get()
            else:
                from main import app
                with app.app_context():
                    return _do_get()
        except Exception as e:
            print(f"❌ [RPA STATUS ERROR] Falha ao buscar status: {e}")
            return None
    
    def clear(self):
        """Remove o registro de status do banco"""
        from extensions import db
        from models import RPAStatus
        from flask import has_app_context
        
        def _do_clear():
            rpa_status = RPAStatus.query.filter_by(process_id=self.process_id).first()
            if rpa_status:
                db.session.delete(rpa_status)
                db.session.commit()
        
        try:
            if has_app_context():
                _do_clear()
            else:
                from main import app
                with app.app_context():
                    _do_clear()
        except Exception as e:
            print(f"❌ [RPA STATUS ERROR] Falha ao limpar status: {e}")
            try:
                db.session.rollback()
            except Exception:
                pass


def get_rpa_status(process_id: int) -> Optional[Dict[str, Any]]:
    """Função helper para buscar status de um processo"""
    manager = RPAStatusManager(process_id)
    return manager.get_status()


def update_status(step: str, message: str, status: str = "running", process_id: int = None, data: Optional[Dict[str, Any]] = None):
    """
    Função utilitária para atualizar status do RPA sem precisar instanciar o manager.
    Compatível com chamadas legadas que usam process_id como kwarg.
    
    Args:
        step: Nome da etapa (ex: "login", "preenchendo_cnj")
        message: Mensagem descritiva
        status: Status geral (starting, running, completed, error)
        process_id: ID do processo (opcional - se None, apenas loga)
        data: Dados adicionais opcionais
    """
    if process_id:
        manager = RPAStatusManager(process_id)
        manager.update(step, message, status, data)
    else:
        # Se não tem process_id, apenas loga no console
        print(f"✅ [RPA] {step}: {message}")


def cleanup_old_statuses(days_old: int = 7):
    """Remove status RPA antigos (concluídos há mais de X dias)"""
    from extensions import db
    from models import RPAStatus
    
    try:
        cutoff = datetime.now() - timedelta(days=days_old)
        old_statuses = RPAStatus.query.filter(
            RPAStatus.status.in_(["completed", "error"]),
            RPAStatus.updated_at < cutoff
        ).all()
        
        for status in old_statuses:
            db.session.delete(status)
        
        db.session.commit()
        print(f"🧹 Limpeza: removidos {len(old_statuses)} status antigos")
    except Exception as e:
        print(f"❌ Erro na limpeza de status: {e}")
        try:
            db.session.rollback()
        except Exception:
            pass


def cleanup_old_screenshots(days_old: int = 2):
    """
    Remove screenshots de RPA antigos (mais de X dias após processamento).
    
    Esta função:
    1. Busca processos com RPA concluído há mais de X dias
    2. Remove os arquivos de screenshot do disco
    3. Limpa os paths no banco de dados
    
    Args:
        days_old: Número de dias após o qual os screenshots são deletados (padrão: 2)
    
    Returns:
        Tuple[int, int]: (arquivos_deletados, processos_atualizados)
    """
    from extensions import db
    from models import Process
    from pathlib import Path
    import os
    
    files_deleted = 0
    processes_updated = 0
    
    try:
        cutoff = datetime.now() - timedelta(days=days_old)
        
        processes_with_old_screenshots = Process.query.filter(
            Process.elaw_status.in_(["success", "error", "completed"]),
            Process.elaw_filled_at < cutoff,
            db.or_(
                Process.elaw_screenshot_path.isnot(None),
                Process.elaw_screenshot_before_path.isnot(None),
                Process.elaw_screenshot_after_path.isnot(None)
            )
        ).all()
        
        if not processes_with_old_screenshots:
            print(f"🧹 [CLEANUP] Nenhum screenshot antigo para limpar")
            return (0, 0)
        
        print(f"🧹 [CLEANUP] Encontrados {len(processes_with_old_screenshots)} processos com screenshots > {days_old} dias")
        
        screenshot_dirs = [
            Path('rpa_screenshots'),
            Path('static/rpa_screenshots'),
            Path('/home/runner/workspace/rpa_screenshots')
        ]
        
        for process in processes_with_old_screenshots:
            screenshot_paths = [
                process.elaw_screenshot_path,
                process.elaw_screenshot_before_path,
                process.elaw_screenshot_after_path
            ]
            
            any_deleted = False
            for screenshot_path in screenshot_paths:
                if not screenshot_path:
                    continue
                
                filename = Path(screenshot_path).name
                
                for screenshot_dir in screenshot_dirs:
                    full_path = screenshot_dir / filename
                    if full_path.exists():
                        try:
                            os.remove(str(full_path))
                            files_deleted += 1
                            any_deleted = True
                            print(f"   🗑️ Deletado: {full_path}")
                        except Exception as e:
                            print(f"   ⚠️ Erro ao deletar {full_path}: {e}")
            
            if any_deleted:
                process.elaw_screenshot_path = None
                process.elaw_screenshot_before_path = None
                process.elaw_screenshot_after_path = None
                processes_updated += 1
        
        db.session.commit()
        print(f"🧹 [CLEANUP] ✅ Limpeza concluída: {files_deleted} arquivos deletados, {processes_updated} processos atualizados")
        
        return (files_deleted, processes_updated)
        
    except Exception as e:
        print(f"❌ [CLEANUP] Erro na limpeza de screenshots: {e}")
        try:
            db.session.rollback()
        except Exception:
            pass
        return (0, 0)


def run_all_cleanup(screenshot_days: int = 2, status_days: int = 7):
    """
    Executa todas as rotinas de limpeza automática.
    
    Args:
        screenshot_days: Dias após os quais screenshots são deletados (padrão: 2)
        status_days: Dias após os quais status RPA são deletados (padrão: 7)
    """
    print("=" * 60)
    print("🧹 INICIANDO LIMPEZA AUTOMÁTICA")
    print("=" * 60)
    
    cleanup_old_screenshots(days_old=screenshot_days)
    
    cleanup_old_statuses(days_old=status_days)
    
    print("=" * 60)
    print("🧹 LIMPEZA AUTOMÁTICA CONCLUÍDA")
    print("=" * 60)
