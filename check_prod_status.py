#!/usr/bin/env python3
import requests
import urllib3
urllib3.disable_warnings()

PROD_URL = "https://fg-bularmaci-processos.replit.app"
s = requests.Session()

# Verificar se aplicação está rodando
try:
    r = s.get(PROD_URL, timeout=10, verify=False)
    print(f"✅ Aplicação em produção está ONLINE (status: {r.status_code})")
    
    if "Login" in r.text or "login" in r.text:
        print("   → Página de login detectada")
    if "Dashboard" in r.text:
        print("   → Dashboard detectado")
        
    # Verificar se tem batches
    r2 = s.get(f"{PROD_URL}/processos/batch", verify=False, allow_redirects=False)
    print(f"\n📦 Rota /processos/batch: status {r2.status_code}")
    
    if r2.status_code == 302:
        print(f"   → Redirecionado para: {r2.headers.get('Location')}")
        print("   → Requer autenticação")
    
except Exception as e:
    print(f"❌ Erro ao acessar produção: {e}")
