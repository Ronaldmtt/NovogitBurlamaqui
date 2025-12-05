#!/usr/bin/env python3
"""
Script de backfill para popular a tabela AnnexLocation com dados históricos.

Analisa os PDFs já processados e extrai as localizações de bookmarks/TOC
para criar estatísticas que serão usadas na inferência de novos PDFs.

Uso:
    python scripts/backfill_annex_locations.py
"""
import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
app = create_app()

from models import db, Process, AnnexLocation
from extractors.ocr_utils import (
    extract_pdf_bookmarks, 
    parse_toc_from_pdf, 
    get_pdf_total_pages,
    record_annex_location
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def find_pdf_path(pdf_filename):
    """Procura o PDF em vários caminhos possíveis."""
    if not pdf_filename:
        return None
    
    filename = os.path.basename(pdf_filename)
    
    possible_paths = [
        pdf_filename,
        os.path.join('uploads', pdf_filename),
        os.path.join('uploads', filename),
    ]
    
    for batch_dir in os.listdir('uploads') if os.path.exists('uploads') else []:
        batch_path = os.path.join('uploads', batch_dir)
        if os.path.isdir(batch_path):
            for sub in os.listdir(batch_path):
                sub_path = os.path.join(batch_path, sub)
                if os.path.isdir(sub_path):
                    candidate = os.path.join(sub_path, filename)
                    if os.path.exists(candidate):
                        return candidate
                elif sub == filename:
                    return os.path.join(batch_path, sub)
    
    for path in possible_paths:
        if os.path.exists(path):
            return path
    
    return None


def backfill_annex_locations():
    """Processa PDFs existentes e popula a tabela AnnexLocation."""
    
    with app.app_context():
        processes = Process.query.filter(
            Process.pdf_filename.isnot(None),
            Process.pdf_filename != ''
        ).all()
        
        logger.info(f"Encontrados {len(processes)} processos com PDF")
        
        stats = {
            'total': len(processes),
            'processados': 0,
            'bookmarks_encontrados': 0,
            'toc_encontrados': 0,
            'erros': 0
        }
        
        for proc in processes:
            try:
                pdf_path = find_pdf_path(proc.pdf_filename)
                
                if not pdf_path:
                    logger.debug(f"PDF não encontrado: {proc.pdf_filename}")
                    continue
                
                total_pages = get_pdf_total_pages(pdf_path)
                if total_pages < 10:
                    continue
                
                proc.pdf_total_pages = total_pages
                
                bookmarks = extract_pdf_bookmarks(pdf_path)
                if bookmarks:
                    stats['bookmarks_encontrados'] += 1
                    for doc_type, page_num in bookmarks.items():
                        record_annex_location(
                            process_id=proc.id,
                            doc_type=doc_type,
                            page_number=page_num,
                            total_pages=total_pages,
                            source='bookmark'
                        )
                else:
                    toc_pages = parse_toc_from_pdf(pdf_path)
                    if any(toc_pages.values()):
                        stats['toc_encontrados'] += 1
                        for doc_type, pages in toc_pages.items():
                            if pages:
                                record_annex_location(
                                    process_id=proc.id,
                                    doc_type=doc_type,
                                    page_number=pages[0],
                                    total_pages=total_pages,
                                    source='toc'
                                )
                
                stats['processados'] += 1
                
                if stats['processados'] % 10 == 0:
                    logger.info(f"Progresso: {stats['processados']}/{stats['total']}")
                    db.session.commit()
                    
            except Exception as e:
                logger.error(f"Erro ao processar {proc.numero_processo}: {e}")
                stats['erros'] += 1
        
        db.session.commit()
        
        logger.info("=" * 50)
        logger.info("BACKFILL CONCLUÍDO")
        logger.info(f"Total processados: {stats['processados']}/{stats['total']}")
        logger.info(f"Com bookmarks: {stats['bookmarks_encontrados']}")
        logger.info(f"Com TOC: {stats['toc_encontrados']}")
        logger.info(f"Erros: {stats['erros']}")
        
        locations = AnnexLocation.query.all()
        logger.info(f"Total de localizações registradas: {len(locations)}")
        
        for doc_type in ['ctps', 'trct', 'contracheque']:
            locs = AnnexLocation.query.filter_by(doc_type=doc_type).all()
            if locs:
                avg_ratio = sum(l.page_ratio for l in locs) / len(locs)
                logger.info(f"  {doc_type.upper()}: {len(locs)} amostras, média {avg_ratio:.0%} do PDF")


if __name__ == '__main__':
    backfill_annex_locations()
