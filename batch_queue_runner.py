"""
Global Batch Queue Runner - Coordenador de Fila de Batches RPA

Este módulo gerencia a execução sequencial de múltiplos batches,
processando um batch por vez com 5 workers paralelos por batch.

Funcionalidades:
- Enfileirar batches para execução automática
- Executar batches em ordem (FIFO por queue_position)
- Quando um batch terminar, ir automaticamente para o próximo
- Manter 5 workers paralelos processando os itens de cada batch
- Status em tempo real da fila global

IMPORTANTE: Usa PostgreSQL advisory locks para garantir que apenas 
um runner execute por vez, mesmo com múltiplos workers Gunicorn.
"""

import threading
import time
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from contextlib import contextmanager

logger = logging.getLogger(__name__)

QUEUE_RUNNER_LOCK_ID = 999999

try:
    from logging_config import log_start, log_end, log_success, log_err, log_event
except ImportError:
    def log_start(op, msg, **kw): pass
    def log_end(op, msg, **kw): pass
    def log_success(op, msg, **kw): pass
    def log_err(op, msg, **kw): pass
    def log_event(op, msg, **kw): pass

# Integração com RPA Monitor Client
try:
    from monitor_integration import log_info as monitor_log_info, log_warning as monitor_log_warning, log_error as monitor_log_error
    MONITOR_AVAILABLE = True
except ImportError:
    MONITOR_AVAILABLE = False
    def monitor_log_info(msg, region=""): pass
    def monitor_log_warning(msg, region=""): pass
    def monitor_log_error(msg, exc=None, region=""): pass


class GlobalBatchQueueRunner:
    """
    Singleton que coordena a execução de múltiplos batches em fila.
    
    Usa um lock global para garantir que apenas uma instância do runner
    esteja ativa por vez (mesmo com múltiplos workers gunicorn).
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        monitor_log_info("QueueRunner: __init__() iniciada", region="QUEUE")
        
        self._initialized = True
        self._running = False
        self._runner_thread: Optional[threading.Thread] = None
        self._current_batch_id: Optional[int] = None
        self._stop_requested = False
        self._status_lock = threading.Lock()
        self._flask_app = None
        
        self._stats = {
            'total_batches_queued': 0,
            'batches_completed': 0,
            'batches_failed': 0,
            'processes_completed': 0,
            'processes_failed': 0,
            'started_at': None,
            'last_update': None
        }
        
        logger.info("[QUEUE_RUNNER] GlobalBatchQueueRunner inicializado (singleton)")
        monitor_log_info("QueueRunner: __init__() concluída - singleton inicializado", region="QUEUE")
    
    def set_flask_app(self, app):
        """Define o Flask app para usar no contexto de banco de dados."""
        monitor_log_info("QueueRunner: set_flask_app() iniciada", region="QUEUE")
        self._flask_app = app
        monitor_log_info("QueueRunner: set_flask_app() concluída - Flask app configurado", region="QUEUE")
    
    def _acquire_db_lock(self) -> bool:
        """
        Tenta adquirir um advisory lock no PostgreSQL.
        Garante que apenas um runner execute por vez, mesmo com múltiplos workers.
        
        Returns:
            True se o lock foi adquirido, False se outro processo já tem o lock.
        """
        monitor_log_info("QueueRunner: _acquire_db_lock() iniciada - tentando adquirir lock", region="QUEUE")
        
        if not self._flask_app:
            monitor_log_warning("QueueRunner: _acquire_db_lock() - Flask app não configurado", region="QUEUE")
            return False
        
        try:
            with self._flask_app.app_context():
                from extensions import db
                from sqlalchemy import text
                
                result = db.session.execute(
                    text(f"SELECT pg_try_advisory_lock({QUEUE_RUNNER_LOCK_ID})")
                ).scalar()
                
                if result:
                    logger.info("[QUEUE_RUNNER] Advisory lock adquirido com sucesso")
                    monitor_log_info("QueueRunner: _acquire_db_lock() concluída - lock ADQUIRIDO", region="QUEUE")
                else:
                    logger.warning("[QUEUE_RUNNER] Outro processo já possui o advisory lock")
                    monitor_log_warning("QueueRunner: _acquire_db_lock() concluída - lock NÃO adquirido (outro processo)", region="QUEUE")
                
                return bool(result)
                
        except Exception as e:
            logger.error(f"[QUEUE_RUNNER] Erro ao adquirir advisory lock: {e}")
            monitor_log_error(f"QueueRunner: _acquire_db_lock() - ERRO: {e}", exc=e, region="QUEUE")
            return False
    
    def _release_db_lock(self):
        """Libera o advisory lock no PostgreSQL."""
        monitor_log_info("QueueRunner: _release_db_lock() iniciada", region="QUEUE")
        
        if not self._flask_app:
            monitor_log_warning("QueueRunner: _release_db_lock() - Flask app não configurado", region="QUEUE")
            return
        
        try:
            with self._flask_app.app_context():
                from extensions import db
                from sqlalchemy import text
                
                db.session.execute(
                    text(f"SELECT pg_advisory_unlock({QUEUE_RUNNER_LOCK_ID})")
                )
                db.session.commit()
                logger.info("[QUEUE_RUNNER] Advisory lock liberado")
                monitor_log_info("QueueRunner: _release_db_lock() concluída - lock liberado", region="QUEUE")
                
        except Exception as e:
            logger.error(f"[QUEUE_RUNNER] Erro ao liberar advisory lock: {e}")
            monitor_log_error(f"QueueRunner: _release_db_lock() - ERRO: {e}", exc=e, region="QUEUE")
    
    @property
    def is_running(self) -> bool:
        """Retorna True se o runner está em execução."""
        return self._running
    
    @property
    def current_batch_id(self) -> Optional[int]:
        """Retorna o ID do batch sendo processado atualmente."""
        return self._current_batch_id
    
    def get_status(self) -> Dict[str, Any]:
        """Retorna status atual da fila global."""
        with self._status_lock:
            if not self._flask_app:
                return {
                    'running': self._running,
                    'error': 'Flask app não configurado'
                }
            
            try:
                with self._flask_app.app_context():
                    from models import BatchUpload, BatchItem, db
                    
                    queued_batches = BatchUpload.query.filter(
                        BatchUpload.queue_position.isnot(None)
                    ).order_by(BatchUpload.queue_position.asc()).all()
                    
                    batches_info = []
                    total_pending_items = 0
                    
                    for batch in queued_batches:
                        ready_items = BatchItem.query.filter_by(
                            batch_id=batch.id, 
                            status='ready'
                        ).count()
                        
                        running_items = BatchItem.query.filter_by(
                            batch_id=batch.id, 
                            status='running'
                        ).count()
                        
                        completed_items = BatchItem.query.filter(
                            BatchItem.batch_id == batch.id,
                            BatchItem.status.in_(['completed', 'success'])
                        ).count()
                        
                        error_items = BatchItem.query.filter(
                            BatchItem.batch_id == batch.id,
                            BatchItem.status.in_(['error', 'failed'])
                        ).count()
                        
                        batches_info.append({
                            'id': batch.id,
                            'queue_position': batch.queue_position,
                            'status': batch.status,
                            'total': batch.total_count,
                            'ready': ready_items,
                            'running': running_items,
                            'completed': completed_items,
                            'error': error_items,
                            'is_current': batch.id == self._current_batch_id
                        })
                        
                        if batch.status in ('queued', 'ready'):
                            total_pending_items += ready_items
                    
                    db.session.remove()
                    
                    return {
                        'running': self._running,
                        'stop_requested': self._stop_requested,
                        'current_batch_id': self._current_batch_id,
                        'queued_batches': batches_info,
                        'total_queued': len(queued_batches),
                        'total_pending_items': total_pending_items,
                        'stats': self._stats.copy()
                    }
                    
            except Exception as e:
                logger.error(f"[QUEUE_RUNNER] Erro ao obter status: {e}")
                monitor_log_error(f"Erro ao obter status: {e}", exc=e, region="QUEUE")
                return {
                    'running': self._running,
                    'error': str(e)
                }
    
    def add_to_queue(self, batch_id: int, user_id: int) -> Dict[str, Any]:
        """
        Adiciona um batch à fila global.
        
        Args:
            batch_id: ID do batch a enfileirar
            user_id: ID do usuário que está enfileirando
            
        Returns:
            Dict com status da operação
        """
        monitor_log_info(f"QueueRunner: add_to_queue() iniciada - batch_id={batch_id}, user_id={user_id}", region="QUEUE")
        
        if not self._flask_app:
            monitor_log_warning("QueueRunner: add_to_queue() - Flask app não configurado", region="QUEUE")
            return {'success': False, 'error': 'Flask app não configurado'}
        
        try:
            with self._flask_app.app_context():
                from models import BatchUpload, BatchItem, db
                
                batch = BatchUpload.query.get(batch_id)
                if not batch:
                    return {'success': False, 'error': 'Batch não encontrado'}
                
                if batch.queue_position is not None:
                    return {'success': False, 'error': f'Batch já está na fila (posição {batch.queue_position})'}
                
                ready_items = BatchItem.query.filter_by(
                    batch_id=batch_id, 
                    status='ready'
                ).count()
                
                if ready_items == 0:
                    return {'success': False, 'error': 'Batch não possui itens prontos para RPA'}
                
                max_position = db.session.query(db.func.max(BatchUpload.queue_position)).scalar() or 0
                new_position = max_position + 1
                
                batch.queue_position = new_position
                batch.queued_at = datetime.utcnow()
                batch.queued_by = user_id
                batch.status = 'queued'
                
                db.session.commit()
                
                log_event("QUEUE_ADD", f"Batch adicionado à fila", 
                         batch_id=batch_id, position=new_position, user_id=user_id)
                
                logger.info(f"[QUEUE_RUNNER] Batch {batch_id} adicionado à fila (posição {new_position})")
                monitor_log_info(f"QueueRunner: add_to_queue() concluída - batch {batch_id} na posição {new_position}", region="QUEUE")
                
                return {
                    'success': True, 
                    'message': f'Batch adicionado à fila na posição {new_position}',
                    'queue_position': new_position
                }
                
        except Exception as e:
            logger.error(f"[QUEUE_RUNNER] Erro ao adicionar batch {batch_id} à fila: {e}")
            monitor_log_error(f"QueueRunner: add_to_queue() - ERRO: {e}", exc=e, region="QUEUE")
            return {'success': False, 'error': str(e)}
    
    def remove_from_queue(self, batch_id: int) -> Dict[str, Any]:
        """Remove um batch da fila."""
        monitor_log_info(f"QueueRunner: remove_from_queue() iniciada - batch_id={batch_id}", region="QUEUE")
        
        if not self._flask_app:
            monitor_log_warning("QueueRunner: remove_from_queue() - Flask app não configurado", region="QUEUE")
            return {'success': False, 'error': 'Flask app não configurado'}
        
        try:
            with self._flask_app.app_context():
                from models import BatchUpload, db
                
                batch = BatchUpload.query.get(batch_id)
                if not batch:
                    return {'success': False, 'error': 'Batch não encontrado'}
                
                if batch.queue_position is None:
                    return {'success': False, 'error': 'Batch não está na fila'}
                
                if batch.id == self._current_batch_id and self._running:
                    return {'success': False, 'error': 'Não é possível remover batch em execução'}
                
                old_position = batch.queue_position
                batch.queue_position = None
                batch.queued_at = None
                batch.queued_by = None
                batch.status = 'ready'
                
                BatchUpload.query.filter(
                    BatchUpload.queue_position > old_position
                ).update({
                    BatchUpload.queue_position: BatchUpload.queue_position - 1
                })
                
                db.session.commit()
                
                log_event("QUEUE_REMOVE", f"Batch removido da fila", 
                         batch_id=batch_id, old_position=old_position)
                
                monitor_log_info(f"QueueRunner: remove_from_queue() concluída - batch {batch_id} removido", region="QUEUE")
                return {'success': True, 'message': 'Batch removido da fila'}
                
        except Exception as e:
            logger.error(f"[QUEUE_RUNNER] Erro ao remover batch {batch_id} da fila: {e}")
            monitor_log_error(f"QueueRunner: remove_from_queue() - ERRO: {e}", exc=e, region="QUEUE")
            return {'success': False, 'error': str(e)}
    
    def start_queue_processing(self, user_id: int) -> Dict[str, Any]:
        """
        Inicia o processamento da fila global.
        
        Usa PostgreSQL advisory lock para garantir que apenas um runner
        execute por vez, mesmo com múltiplos workers Gunicorn.
        
        Args:
            user_id: ID do usuário que está iniciando
            
        Returns:
            Dict com status da operação
        """
        monitor_log_info(f"QueueRunner: start_queue_processing() iniciada - user_id={user_id}", region="QUEUE")
        
        if self._running:
            monitor_log_warning("QueueRunner: start_queue_processing() - fila já em execução", region="QUEUE")
            return {'success': False, 'error': 'Fila já está em execução neste worker'}
        
        if not self._flask_app:
            monitor_log_warning("QueueRunner: start_queue_processing() - Flask app não configurado", region="QUEUE")
            return {'success': False, 'error': 'Flask app não configurado'}
        
        if not self._acquire_db_lock():
            return {
                'success': False, 
                'error': 'Fila já está sendo executada por outro worker. Aguarde ou tente novamente.'
            }
        
        try:
            with self._flask_app.app_context():
                from models import BatchUpload, db
                
                queued_count = BatchUpload.query.filter(
                    BatchUpload.queue_position.isnot(None),
                    BatchUpload.status.in_(['queued', 'ready'])
                ).count()
                
                if queued_count == 0:
                    self._release_db_lock()
                    return {'success': False, 'error': 'Nenhum batch na fila para processar'}
                
                db.session.remove()
            
            self._stop_requested = False
            self._running = True
            self._stats['started_at'] = datetime.utcnow().isoformat()
            self._stats['batches_completed'] = 0
            self._stats['batches_failed'] = 0
            self._stats['processes_completed'] = 0
            self._stats['processes_failed'] = 0
            
            self._runner_thread = threading.Thread(
                target=self._run_queue_loop,
                daemon=True
            )
            self._runner_thread.start()
            
            log_start("QUEUE_PROCESS", f"Iniciando processamento da fila global", 
                     user_id=user_id, queued_batches=queued_count)
            
            logger.info(f"[QUEUE_RUNNER] Processamento da fila iniciado ({queued_count} batches)")
            monitor_log_info(f"QueueRunner: start_queue_processing() concluída - {queued_count} batches na fila", region="QUEUE")
            
            return {
                'success': True,
                'message': f'Processamento da fila iniciado ({queued_count} batches)',
                'queued_count': queued_count
            }
            
        except Exception as e:
            self._running = False
            self._release_db_lock()
            logger.error(f"[QUEUE_RUNNER] Erro ao iniciar fila: {e}")
            monitor_log_error(f"QueueRunner: start_queue_processing() - ERRO: {e}", exc=e, region="QUEUE")
            return {'success': False, 'error': str(e)}
    
    def stop_queue_processing(self) -> Dict[str, Any]:
        """Para o processamento da fila após o batch atual terminar."""
        monitor_log_info("QueueRunner: stop_queue_processing() iniciada", region="QUEUE")
        
        if not self._running:
            monitor_log_warning("QueueRunner: stop_queue_processing() - fila não está em execução", region="QUEUE")
            return {'success': False, 'error': 'Fila não está em execução'}
        
        self._stop_requested = True
        
        log_event("QUEUE_STOP", f"Parada da fila solicitada")
        logger.info("[QUEUE_RUNNER] Parada da fila solicitada (aguardando batch atual terminar)")
        monitor_log_info(f"QueueRunner: stop_queue_processing() concluída - parada solicitada, batch atual={self._current_batch_id}", region="QUEUE")
        
        return {
            'success': True,
            'message': 'Parada solicitada. A fila será interrompida após o batch atual terminar.',
            'current_batch_id': self._current_batch_id
        }
    
    def _run_queue_loop(self):
        """
        Loop principal que processa batches em sequência.
        Executa em thread separada.
        """
        monitor_log_info("QueueRunner: _run_queue_loop() iniciada", region="QUEUE")
        log_start("QUEUE_LOOP", f"Loop da fila iniciado")
        logger.info("[QUEUE_RUNNER] Loop da fila iniciado")
        
        try:
            while not self._stop_requested:
                next_batch = self._get_next_batch()
                
                if next_batch is None:
                    logger.info("[QUEUE_RUNNER] Fila vazia, encerrando loop")
                    monitor_log_info("Fila vazia, encerrando loop", region="QUEUE")
                    break
                
                batch_id = next_batch['id']
                self._current_batch_id = batch_id
                
                log_event("QUEUE_BATCH_START", f"Iniciando processamento de batch da fila",
                         batch_id=batch_id, position=next_batch['queue_position'])
                logger.info(f"[QUEUE_RUNNER] Processando batch {batch_id} (posição {next_batch['queue_position']})")
                monitor_log_info(f"Processando batch {batch_id} (posição {next_batch['queue_position']})", region="QUEUE")
                
                try:
                    result = self._process_single_batch(batch_id)
                    
                    if result['success']:
                        self._stats['batches_completed'] += 1
                        self._stats['processes_completed'] += result.get('success_count', 0)
                        self._stats['processes_failed'] += result.get('error_count', 0)
                        
                        log_success("QUEUE_BATCH", f"Batch processado com sucesso",
                                   batch_id=batch_id, 
                                   success=result.get('success_count', 0),
                                   errors=result.get('error_count', 0))
                    else:
                        self._stats['batches_failed'] += 1
                        
                        log_err("QUEUE_BATCH", f"Falha ao processar batch",
                               batch_id=batch_id, error=result.get('error'))
                    
                except Exception as e:
                    self._stats['batches_failed'] += 1
                    logger.error(f"[QUEUE_RUNNER] Erro ao processar batch {batch_id}: {e}")
                    monitor_log_error(f"Erro ao processar batch {batch_id}: {e}", exc=e, region="QUEUE")
                    log_err("QUEUE_BATCH", f"Exceção ao processar batch",
                           batch_id=batch_id, error=str(e))
                
                finally:
                    self._remove_from_queue_after_processing(batch_id)
                    self._current_batch_id = None
                    self._stats['last_update'] = datetime.utcnow().isoformat()
                
                time.sleep(1)
        
        except Exception as e:
            logger.error(f"[QUEUE_RUNNER] Erro fatal no loop da fila: {e}")
            monitor_log_error(f"Erro fatal no loop da fila: {e}", exc=e, region="QUEUE")
            log_err("QUEUE_LOOP", f"Erro fatal no loop", error=str(e))
        
        finally:
            self._running = False
            self._current_batch_id = None
            self._stop_requested = False
            
            self._release_db_lock()
            
            log_end("QUEUE_LOOP", f"Loop da fila encerrado",
                   batches_completed=self._stats['batches_completed'],
                   batches_failed=self._stats['batches_failed'])
            logger.info("[QUEUE_RUNNER] Loop da fila encerrado")
            monitor_log_info(f"QueueRunner: _run_queue_loop() concluída - completed={self._stats['batches_completed']}, failed={self._stats['batches_failed']}", region="QUEUE")
    
    def _get_next_batch(self) -> Optional[Dict[str, Any]]:
        """Retorna o próximo batch da fila para processar."""
        monitor_log_info("QueueRunner: _get_next_batch() iniciada", region="QUEUE")
        try:
            with self._flask_app.app_context():
                from models import BatchUpload, BatchItem, db
                
                next_batch = BatchUpload.query.filter(
                    BatchUpload.queue_position.isnot(None),
                    BatchUpload.status.in_(['queued', 'ready'])
                ).order_by(BatchUpload.queue_position.asc()).first()
                
                if not next_batch:
                    monitor_log_info("QueueRunner: _get_next_batch() concluída - fila vazia", region="QUEUE")
                    return None
                
                ready_items = BatchItem.query.filter_by(
                    batch_id=next_batch.id,
                    status='ready'
                ).count()
                
                result = {
                    'id': next_batch.id,
                    'queue_position': next_batch.queue_position,
                    'ready_items': ready_items
                }
                
                db.session.remove()
                monitor_log_info(f"QueueRunner: _get_next_batch() concluída - próximo batch_id={next_batch.id}, pos={next_batch.queue_position}", region="QUEUE")
                return result
                
        except Exception as e:
            logger.error(f"[QUEUE_RUNNER] Erro ao obter próximo batch: {e}")
            monitor_log_error(f"QueueRunner: _get_next_batch() - ERRO: {e}", exc=e, region="QUEUE")
            return None
    
    def _process_single_batch(self, batch_id: int) -> Dict[str, Any]:
        """
        Processa um único batch usando o sistema existente de RPA paralelo.
        
        Reutiliza a lógica existente em routes_batch.py mas de forma síncrona.
        """
        monitor_log_info(f"QueueRunner: _process_single_batch() iniciada - batch_id={batch_id}", region="QUEUE")
        
        import rpa
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        MAX_RPA_WORKERS = 5
        
        try:
            with self._flask_app.app_context():
                from models import BatchUpload, BatchItem, Process, db
                
                rpa.flask_app = self._flask_app
                
                batch = BatchUpload.query.get(batch_id)
                if not batch:
                    return {'success': False, 'error': 'Batch não encontrado'}
                
                # Garantir que apenas este batch esteja como 'running'
                BatchUpload.query.filter(
                    BatchUpload.status == 'running',
                    BatchUpload.id != batch_id
                ).update({'status': 'queued'})
                
                batch.status = 'running'
                batch.started_at = datetime.utcnow()
                batch.processed_count = 0
                db.session.commit()
                
                items = BatchItem.query.filter_by(batch_id=batch_id, status='ready').all()
                total_items = len(items)
                
                if total_items == 0:
                    batch.status = 'completed'
                    batch.finished_at = datetime.utcnow()
                    db.session.commit()
                    return {'success': True, 'success_count': 0, 'error_count': 0}
                
                items_data = []
                for item in items:
                    if item.process_id:
                        items_data.append({
                            'item_id': item.id,
                            'process_id': item.process_id
                        })
                    else:
                        item.status = 'error'
                        item.last_error = 'Processo não encontrado'
                
                db.session.commit()
                db.session.remove()
                
                success_count = 0
                error_count = total_items - len(items_data)
                
                logger.info(f"[QUEUE_RUNNER] Processando {len(items_data)} itens do batch {batch_id}")
                monitor_log_info(f"Processando {len(items_data)} itens do batch {batch_id}", region="QUEUE")
                
                def execute_single_rpa(item_id: int, process_id: int, worker_id: int):
                    try:
                        with self._flask_app.app_context():
                            from models import BatchItem, db
                            
                            item = BatchItem.query.get(item_id)
                            if item:
                                item.status = 'running'
                                item.started_at = datetime.utcnow()
                                db.session.commit()
                            
                            result = rpa.execute_rpa_parallel(process_id, worker_id=worker_id)
                            
                            item = BatchItem.query.get(item_id)
                            if item:
                                if result['status'] == 'success':
                                    item.status = 'completed'
                                else:
                                    item.status = 'error'
                                    item.last_error = result.get('error', 'Erro desconhecido')
                                item.finished_at = datetime.utcnow()
                                db.session.commit()
                            
                            db.session.remove()
                            
                            return {
                                'success': result['status'] == 'success',
                                'item_id': item_id,
                                'process_id': process_id,
                                'error': result.get('error')
                            }
                            
                    except Exception as e:
                        logger.error(f"[QUEUE_RUNNER] Erro ao executar RPA para item {item_id}: {e}")
                        monitor_log_error(f"Erro ao executar RPA para item {item_id}: {e}", exc=e, region="QUEUE")
                        try:
                            with self._flask_app.app_context():
                                from models import BatchItem, db
                                item = BatchItem.query.get(item_id)
                                if item:
                                    item.status = 'error'
                                    item.last_error = str(e)[:500]
                                    item.finished_at = datetime.utcnow()
                                    db.session.commit()
                                db.session.remove()
                        except:
                            pass
                        
                        return {
                            'success': False,
                            'item_id': item_id,
                            'process_id': process_id,
                            'error': str(e)
                        }
                
                with ThreadPoolExecutor(max_workers=MAX_RPA_WORKERS) as executor:
                    future_to_item = {}
                    for idx, item_data in enumerate(items_data):
                        worker_id = idx % MAX_RPA_WORKERS
                        future = executor.submit(
                            execute_single_rpa,
                            item_data['item_id'],
                            item_data['process_id'],
                            worker_id
                        )
                        future_to_item[future] = item_data
                    
                    for future in as_completed(future_to_item):
                        try:
                            result = future.result()
                            if result['success']:
                                success_count += 1
                            else:
                                error_count += 1
                            
                            with self._flask_app.app_context():
                                from models import BatchUpload, db
                                batch = BatchUpload.query.get(batch_id)
                                if batch:
                                    batch.processed_count = success_count + error_count
                                    db.session.commit()
                                db.session.remove()
                                
                        except Exception as e:
                            error_count += 1
                            logger.error(f"[QUEUE_RUNNER] Erro no future: {e}")
                            monitor_log_error(f"Erro no future: {e}", exc=e, region="QUEUE")
                
                with self._flask_app.app_context():
                    from models import BatchUpload, db
                    batch = BatchUpload.query.get(batch_id)
                    if batch:
                        batch.status = 'completed' if error_count == 0 else 'partial_completed'
                        batch.processed_count = success_count + error_count
                        batch.finished_at = datetime.utcnow()
                        db.session.commit()
                    db.session.remove()
                
                logger.info(f"[QUEUE_RUNNER] Batch {batch_id} concluído: {success_count} sucesso, {error_count} erros")
                monitor_log_info(f"QueueRunner: _process_single_batch() concluída - batch {batch_id}: {success_count} sucesso, {error_count} erros", region="QUEUE")
                
                return {
                    'success': True,
                    'success_count': success_count,
                    'error_count': error_count
                }
                
        except Exception as e:
            logger.error(f"[QUEUE_RUNNER] Erro ao processar batch {batch_id}: {e}")
            monitor_log_error(f"QueueRunner: _process_single_batch() - ERRO: {e}", exc=e, region="QUEUE")
            
            try:
                with self._flask_app.app_context():
                    from models import BatchUpload, db
                    batch = BatchUpload.query.get(batch_id)
                    if batch:
                        batch.status = 'error'
                        batch.finished_at = datetime.utcnow()
                        db.session.commit()
                    db.session.remove()
            except:
                pass
            
            return {'success': False, 'error': str(e)}
    
    def _remove_from_queue_after_processing(self, batch_id: int):
        """Remove o batch da fila após processamento."""
        try:
            with self._flask_app.app_context():
                from models import BatchUpload, db
                
                batch = BatchUpload.query.get(batch_id)
                if batch and batch.queue_position is not None:
                    old_position = batch.queue_position
                    batch.queue_position = None
                    batch.queued_at = None
                    
                    BatchUpload.query.filter(
                        BatchUpload.queue_position > old_position
                    ).update({
                        BatchUpload.queue_position: BatchUpload.queue_position - 1
                    })
                    
                    db.session.commit()
                
                db.session.remove()
                
        except Exception as e:
            logger.error(f"[QUEUE_RUNNER] Erro ao remover batch {batch_id} da fila: {e}")
            monitor_log_error(f"Erro ao remover batch {batch_id} da fila: {e}", exc=e, region="QUEUE")


global_queue_runner = GlobalBatchQueueRunner()
