#!/usr/bin/env python3
"""Testa RPA em batch existente em produção"""
import requests
import time
import re
import urllib3
import json

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PROD_URL = "https://fg-bularmaci-processos.replit.app"

session = requests.Session()

# Login
print("🔐 Login...")
login_page = session.get(f"{PROD_URL}/login", verify=False)
csrf = re.search(r'csrf_token.*?value="([^"]+)"', login_page.text).group(1)
session.post(f"{PROD_URL}/login", data={
    "csrf_token": csrf,
    "username": "admin",
    "password": "admin123"
}, verify=False)

# Listar batches disponíveis
print("\n📦 Buscando batches disponíveis...")
batches_page = session.get(f"{PROD_URL}/processos/batch", verify=False)

# Extrair IDs de batches
batch_links = re.findall(r'/processos/batch/(\d+)', batches_page.text)
batch_ids = sorted(set(int(bid) for bid in batch_links), reverse=True)

print(f"Encontrados {len(batch_ids)} batches: {batch_ids[:10]}")

# Pegar o batch mais recente com status 'completed' ou 'ready'
for batch_id in batch_ids[:5]:
    batch_page = session.get(f"{PROD_URL}/processos/batch/{batch_id}", verify=False)
    
    # Verificar status do batch
    status_match = re.search(r'Status:</strong>\s*<span[^>]*>([^<]+)</span>', batch_page.text)
    if status_match:
        status = status_match.group(1).strip()
        
        # Contar processos
        proc_count = len(re.findall(r'<tr[^>]*data-process-id', batch_page.text))
        
        print(f"\nBatch #{batch_id}: Status={status}, Processos={proc_count}")
        
        if status in ['Concluído', 'Completo', 'Pronto para RPA'] and proc_count >= 2:
            print(f"\n✅ Usando batch #{batch_id} para teste!")
            
            # Iniciar RPA
            print(f"\n🤖 Iniciando RPA batch #{batch_id}...")
            start_resp = session.post(f"{PROD_URL}/processos/batch/{batch_id}/start", verify=False)
            print(f"   Status: {start_resp.status_code}")
            
            # Monitorar por 5 minutos
            print("\n👀 Monitorando progresso (5 min máximo)...")
            for i in range(60):  # 60 x 5s = 5 minutos
                time.sleep(5)
                
                batch_page = session.get(f"{PROD_URL}/processos/batch/{batch_id}", verify=False)
                
                # Contar badges de status RPA
                success = batch_page.text.count('badge bg-success')
                errors = batch_page.text.count('badge bg-danger')
                pending = batch_page.text.count('badge bg-warning')
                processing = batch_page.text.count('badge bg-info')
                
                elapsed = (i + 1) * 5
                print(f"[{elapsed}s] ✅ {success} | ❌ {errors} | ⏳ {pending} | 🔄 {processing}")
                
                # Se todos finalizaram (sucesso ou erro), parar
                if processing == 0 and pending == 0 and (success + errors) >= proc_count:
                    print(f"\n{'='*60}")
                    print("RESULTADO FINAL:")
                    print(f"✅ Sucessos: {success}")
                    print(f"❌ Erros: {errors}")
                    print(f"{'='*60}")
                    
                    if errors > 0:
                        # Extrair mensagens de erro
                        print("\n🔍 ANALISANDO ERROS...")
                        
                        # Buscar por tooltips ou mensagens de erro
                        error_details = re.findall(r'data-error="([^"]+)"', batch_page.text)
                        if error_details:
                            for j, err in enumerate(error_details[:5], 1):
                                print(f"\n   Erro {j}: {err[:200]}")
                        
                        # Buscar em células da tabela
                        error_cells = re.findall(r'<td[^>]*>([^<]*Erro[^<]*)</td>', batch_page.text)
                        if error_cells:
                            for j, cell in enumerate(error_cells[:5], 1):
                                print(f"   Célula {j}: {cell.strip()}")
                    
                    break
            
            break

print("\n✅ Teste concluído!")
