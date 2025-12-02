#!/usr/bin/env python3
"""
Script de teste para executar RPA em um único processo
"""
import sys

from app import create_app, db
from models import Process
from rpa import execute_rpa

def test_single_process(process_id: int):
    """Testa RPA em um único processo"""
    app = create_app()
    
    with app.app_context():
        proc = Process.query.get(process_id)
        if not proc:
            print(f"❌ Processo {process_id} não encontrado")
            return False
        
        print(f"✅ Processo encontrado: ID={proc.id}")
        print(f"   CNJ: {proc.cnj}")
        print(f"   Número do processo: {proc.numero_processo}")
        print(f"   Parte adversa: {proc.parte_adversa_nome}")
        print(f"   PDF: {proc.pdf_filename}")
        print()
        print("🚀 Iniciando RPA...")
        print("=" * 80)
        
        try:
            result = execute_rpa(process_id)
            print("=" * 80)
            print(f"Resultado: {result}")
            
            if result.get("status") == "success":
                print("✅ RPA concluído com sucesso!")
                return True
            else:
                print(f"❌ RPA falhou: {result.get('message')}")
                if result.get("error"):
                    print(f"   Erro: {result.get('error')}")
                return False
        except Exception as e:
            print(f"❌ Erro durante execução do RPA: {e}")
            import traceback
            traceback.print_exc()
            return False

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python test_single_rpa.py <process_id>")
        print("Exemplo: python test_single_rpa.py 318")
        sys.exit(1)
    
    process_id = int(sys.argv[1])
    result = test_single_process(process_id)
    sys.exit(0 if result else 1)
