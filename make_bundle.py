#!/usr/bin/env python3
"""
Gera bundle.txt com todo o código fonte do sistema.
"""
import os
from pathlib import Path

# Arquivos principais a incluir (ordem lógica)
MAIN_FILES = [
    # Core
    "replit.md",
    "app.py",
    "main.py",
    "models.py",
    "forms.py",
    "extensions.py",
    "routes.py",
    "routes_batch.py",
    "rpa.py",
    "rpa_status.py",
    "monitor_integration.py",
    "gunicorn.conf.py",
    
    # Extractors
    "extractors/__init__.py",
    "extractors/pipeline.py",
    "extractors/llm_extractor.py",
    "extractors/regex_utils.py",
    "extractors/ocr_utils.py",
    "extractors/header_parser.py",
    "extractors/section_detector.py",
    "extractors/document_classifier.py",
    "extractors/posicao_mapping.py",
    "extractors/client_priority.py",
    "extractors/brand_map.py",
    "extractors/audiencia.py",
    "extractors/cadastro.py",
    "extractors/decisao.py",
    "extractors/dictionary.py",
    "extractors/postprocess.py",
    "extractors/reextract.py",
    
    # Utils
    "utils/normalization.py",
    "utils/cell_inference.py",
    "utils/gap_filler.py",
    "utils/jur_extraction.py",
    "utils/option_catalog.py",
    
    # Data files
    "data/clientes_database.json",
    "data/actors.json",
    "data/elaw_tipos_pedidos.json",
    "data/trt_map.json",
    "config/brand_map.json",
    "config/client_aliases.json",
    "config/client_cell_map.json",
    
    # Templates
    "templates/base.html",
    "templates/login.html",
    "templates/dashboard.html",
    "templates/error.html",
    "templates/admin/users.html",
    "templates/admin/create_user.html",
    "templates/processes/list.html",
    "templates/processes/view.html",
    "templates/processes/create.html",
    "templates/processes/edit.html",
    "templates/processes/analysis.html",
    "templates/processes/extract_from_pdf.html",
    "templates/processes/confirm_extracted.html",
    "templates/processes/reextract_ocr.html",
    "templates/processes/batch_upload.html",
    "templates/processes/batch_list.html",
    "templates/processes/batch_detail.html",
    "templates/processes/batch_progress.html",
    "templates/processes/rpa_progress.html",
    
    # Config
    "requirements.txt",
    "pyproject.toml",
]

def generate_bundle():
    output = []
    output.append("=" * 80)
    output.append("BUNDLE.TXT - Sistema Jurídico Inteligente - Código Fonte Completo")
    output.append("Gerado em: " + __import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    output.append("=" * 80)
    output.append("")
    
    # Índice
    output.append("ÍNDICE DE ARQUIVOS:")
    output.append("-" * 40)
    for i, f in enumerate(MAIN_FILES, 1):
        if os.path.exists(f):
            size = os.path.getsize(f)
            output.append(f"{i:3}. {f} ({size:,} bytes)")
    output.append("-" * 40)
    output.append("")
    
    # Conteúdo
    for filepath in MAIN_FILES:
        if not os.path.exists(filepath):
            continue
        
        output.append("")
        output.append("=" * 80)
        output.append(f"FILE: {filepath}")
        output.append("=" * 80)
        
        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            output.append(content)
        except Exception as e:
            output.append(f"[ERROR reading file: {e}]")
        
        output.append("")
    
    # Escrever bundle
    with open('bundle.txt', 'w', encoding='utf-8') as f:
        f.write('\n'.join(output))
    
    total_size = os.path.getsize('bundle.txt')
    print(f"✅ bundle.txt criado com sucesso!")
    print(f"   Arquivos incluídos: {len([f for f in MAIN_FILES if os.path.exists(f)])}")
    print(f"   Tamanho total: {total_size:,} bytes ({total_size/1024/1024:.2f} MB)")

if __name__ == "__main__":
    generate_bundle()
