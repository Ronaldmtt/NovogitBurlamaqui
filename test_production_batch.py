#!/usr/bin/env python3
"""
Script de teste para batch RPA em PRODUÇÃO
Testa o fluxo completo: login → upload batch → executar RPA → verificar resultados
"""
import requests
import time
import re
import urllib3
from pathlib import Path

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PROD_URL = "https://fg-bularmaci-processos.replit.app"
USERNAME = "admin"
PASSWORD = "admin123"

def login(session):
    """Faz login e retorna CSRF token"""
    print("\n🔐 Fazendo login em produção...")
    login_page = session.get(f"{PROD_URL}/login", verify=False)
    csrf_match = re.search(r'csrf_token.*?value="([^"]+)"', login_page.text)
    if not csrf_match:
        print("❌ CSRF token não encontrado!")
        return None
    
    csrf_token = csrf_match.group(1)
    resp = session.post(f"{PROD_URL}/login", data={
        "csrf_token": csrf_token,
        "username": USERNAME,
        "password": PASSWORD
    }, verify=False, allow_redirects=True)
    
    if "Sair" in resp.text or "Dashboard" in resp.text:
        print("✅ Login bem-sucedido!")
        return csrf_token
    else:
        print(f"❌ Falha no login! Status: {resp.status_code}")
        return None

def get_csrf_from_page(session, url):
    """Obtém CSRF token de uma página específica"""
    page = session.get(url, verify=False)
    csrf_match = re.search(r'csrf_token.*?value="([^"]+)"', page.text)
    return csrf_match.group(1) if csrf_match else None

def create_batch_upload(session):
    """Cria um novo batch upload e retorna o ID"""
    print("\n📦 Criando novo batch upload...")
    
    # Obter CSRF da página de batch
    csrf = get_csrf_from_page(session, f"{PROD_URL}/processos/batch/new")
    if not csrf:
        print("❌ Não conseguiu obter CSRF token!")
        return None
    
    # Upload de múltiplos PDFs (simulando - vamos usar arquivos de teste)
    files_data = {
        'csrf_token': (None, csrf),
    }
    
    # Buscar PDFs de exemplo no diretório uploads/ local (se existir)
    upload_dir = Path('uploads')
    pdf_files = []
    
    if upload_dir.exists():
        pdf_files = list(upload_dir.glob('*.pdf'))[:4]  # Máximo 4 PDFs
    
    if not pdf_files:
        print("⚠️ Nenhum PDF encontrado em uploads/ - não posso criar batch de teste")
        print("   Por favor, coloque alguns PDFs em uploads/ e tente novamente")
        return None
    
    print(f"📄 Encontrados {len(pdf_files)} PDFs para upload:")
    for pdf in pdf_files:
        print(f"   - {pdf.name}")
    
    # Preparar upload
    files = []
    for pdf in pdf_files:
        files.append(('files', (pdf.name, open(pdf, 'rb'), 'application/pdf')))
    
    print("\n⬆️ Fazendo upload dos PDFs...")
    resp = session.post(
        f"{PROD_URL}/processos/batch/upload",
        data=files_data,
        files=files,
        verify=False,
        allow_redirects=True
    )
    
    # Fechar arquivos
    for _, (_, file_obj, _) in files:
        file_obj.close()
    
    # Extrair ID do batch da URL de redirecionamento
    batch_id_match = re.search(r'/batch/(\d+)', resp.url)
    if batch_id_match:
        batch_id = int(batch_id_match.group(1))
        print(f"✅ Batch #{batch_id} criado com sucesso!")
        return batch_id
    else:
        print(f"❌ Falha ao criar batch! Status: {resp.status_code}")
        print(f"   URL final: {resp.url}")
        return None

def start_batch_rpa(session, batch_id):
    """Inicia execução RPA do batch"""
    print(f"\n🤖 Iniciando RPA para batch #{batch_id}...")
    
    resp = session.post(
        f"{PROD_URL}/processos/batch/{batch_id}/start",
        verify=False,
        allow_redirects=False
    )
    
    if resp.status_code in [200, 302]:
        print("✅ RPA iniciado com sucesso!")
        return True
    else:
        print(f"❌ Falha ao iniciar RPA! Status: {resp.status_code}")
        return False

def monitor_batch_progress(session, batch_id, timeout=180):
    """Monitora progresso do batch RPA"""
    print(f"\n👀 Monitorando batch #{batch_id} (timeout: {timeout}s)...")
    
    start_time = time.time()
    last_status = None
    
    while time.time() - start_time < timeout:
        resp = session.get(f"{PROD_URL}/processos/batch/{batch_id}", verify=False)
        
        # Extrair status dos processos
        statuses = re.findall(r'<td[^>]*>(Pendente|Processando|Concluído|Erro)</td>', resp.text)
        rpa_statuses = re.findall(r'<span class="badge[^"]*"[^>]*>([^<]+)</span>', resp.text)
        
        current_status = {
            'processos': statuses[:5] if statuses else [],
            'rpa': rpa_statuses[:5] if rpa_statuses else []
        }
        
        if current_status != last_status:
            print(f"\n⏱️ [{int(time.time() - start_time)}s] Status atualizado:")
            for i, (proc, rpa) in enumerate(zip(current_status['processos'], current_status['rpa']), 1):
                print(f"   Processo {i}: {proc} | RPA: {rpa}")
            last_status = current_status
        
        # Verificar se todos concluíram (sucesso ou erro)
        if all(s in ['Concluído', 'Erro'] for s in current_status['processos']):
            print("\n✅ Todos os processos finalizados!")
            return current_status
        
        time.sleep(3)
    
    print(f"\n⏰ Timeout de {timeout}s atingido!")
    return last_status

def analyze_results(session, batch_id):
    """Analisa resultados finais do batch"""
    print(f"\n📊 Analisando resultados do batch #{batch_id}...")
    
    resp = session.get(f"{PROD_URL}/processos/batch/{batch_id}", verify=False)
    
    # Contar sucessos e erros
    success_count = resp.text.count('badge bg-success')
    error_count = resp.text.count('badge bg-danger')
    pending_count = resp.text.count('badge bg-warning')
    
    print(f"\n{'='*60}")
    print(f"RESULTADO FINAL - BATCH #{batch_id}")
    print(f"{'='*60}")
    print(f"✅ Sucessos: {success_count}")
    print(f"❌ Erros: {error_count}")
    print(f"⏳ Pendentes: {pending_count}")
    print(f"{'='*60}")
    
    # Extrair mensagens de erro se houver
    if error_count > 0:
        print("\n🔍 DETALHES DOS ERROS:")
        error_msgs = re.findall(r'<td[^>]*class="[^"]*text-danger[^"]*"[^>]*>([^<]+)</td>', resp.text)
        for i, msg in enumerate(error_msgs[:10], 1):  # Primeiros 10 erros
            print(f"   {i}. {msg.strip()}")
    
    return {
        'success': success_count,
        'errors': error_count,
        'pending': pending_count,
        'all_success': error_count == 0 and pending_count == 0 and success_count > 0
    }

def main():
    """Fluxo principal de teste"""
    print("╔═══════════════════════════════════════════════════════════╗")
    print("║   TESTE AUTOMATIZADO - BATCH RPA EM PRODUÇÃO             ║")
    print("╚═══════════════════════════════════════════════════════════╝")
    
    session = requests.Session()
    
    # 1. Login
    if not login(session):
        return
    
    # 2. Criar batch
    batch_id = create_batch_upload(session)
    if not batch_id:
        return
    
    # 3. Iniciar RPA
    if not start_batch_rpa(session, batch_id):
        return
    
    # 4. Monitorar progresso
    final_status = monitor_batch_progress(session, batch_id, timeout=300)
    
    # 5. Analisar resultados
    results = analyze_results(session, batch_id)
    
    # 6. Conclusão
    print("\n" + "="*60)
    if results['all_success']:
        print("🎉 TESTE PASSOU! Todos os processos foram preenchidos com sucesso!")
    else:
        print("⚠️ TESTE FALHOU! Há processos com erro que precisam ser corrigidos.")
        print(f"\n🔗 Ver detalhes em: {PROD_URL}/processos/batch/{batch_id}")
    print("="*60)

if __name__ == "__main__":
    main()
