#!/usr/bin/env python3
import requests
import json
import time
import urllib3
import re

# Desabilitar avisos de SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PROD_URL = "https://fg-bularmaci-processos.replit.app"

session = requests.Session()

# Pegar CSRF token
print("🔐 Obtendo CSRF token...")
login_page = session.get(f"{PROD_URL}/login", verify=False)
csrf_match = re.search(r'name="csrf_token".*?value="([^"]+)"', login_page.text)
csrf_token = csrf_match.group(1) if csrf_match else None
print(f"CSRF Token: {csrf_token[:50]}..." if csrf_token else "❌ CSRF token não encontrado")

# Login
print("\n🔐 Fazendo login em produção...")
login_resp = session.post(f"{PROD_URL}/login", data={
    "csrf_token": csrf_token,
    "username": "admin",
    "password": "admin123",
    "next": "/dashboard"
}, allow_redirects=True, verify=False)

if "Dashboard" in login_resp.text or "Lotes" in login_resp.text:
    print("✅ Login bem-sucedido!")
else:
    print("❌ Login falhou")
    print(login_resp.text[:500])
    exit(1)

# Listar batches
print("\n📦 Buscando batches disponíveis...")
batches_resp = session.get(f"{PROD_URL}/processos/batch/list", verify=False)
print(f"Status: {batches_resp.status_code}")

# Extrair IDs de batches da página
batch_ids = re.findall(r'href="/processos/batch/(\d+)"', batches_resp.text)
print(f"Batches encontrados: {batch_ids}")

if not batch_ids:
    print("❌ Nenhum batch encontrado")
    # Tentar listar processos
    processes_resp = session.get(f"{PROD_URL}/processes", verify=False)
    process_ids = re.findall(r'href="/process/(\d+)"', processes_resp.text)
    print(f"Processos encontrados: {process_ids[:10]}")
    exit(0)

# Pegar o último batch
batch_id = batch_ids[0]
print(f"\n🎯 Acessando batch #{batch_id}...")

batch_resp = session.get(f"{PROD_URL}/processos/batch/{batch_id}", verify=False)
print(f"Status: {batch_resp.status_code}")

# Procurar processos no batch
process_ids = re.findall(r'href="/process/(\d+)"', batch_resp.text)
print(f"Processos no batch: {process_ids}")

# Verificar status RPA
rpa_status = re.findall(r'Status RPA:\s*([^<]+)', batch_resp.text)
print(f"Status RPA encontrados: {rpa_status}")

# Tentar executar RPA batch
print(f"\n🤖 Tentando executar RPA do batch #{batch_id}...")
rpa_resp = session.post(f"{PROD_URL}/processos/batch/{batch_id}/start", allow_redirects=False, verify=False)
print(f"Status: {rpa_resp.status_code}")
print(f"Response: {rpa_resp.text[:500]}")

# Aguardar e verificar progresso
print("\n⏳ Aguardando execução (30s)...")
time.sleep(30)

# Verificar status final
batch_final = session.get(f"{PROD_URL}/processos/batch/{batch_id}", verify=False)
print(f"\n📊 Status final do batch:")
print(batch_final.text[:1000])
