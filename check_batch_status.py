#!/usr/bin/env python3
import requests
import re
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PROD_URL = "https://fg-bularmaci-processos.replit.app"
session = requests.Session()

# Login
login_page = session.get(f"{PROD_URL}/login", verify=False)
csrf_token = re.search(r'csrf_token.*?value="([^"]+)"', login_page.text).group(1)
session.post(f"{PROD_URL}/login", data={
    "csrf_token": csrf_token,
    "username": "admin",
    "password": "admin123"
}, verify=False)

# Verificar batch #14
batch_resp = session.get(f"{PROD_URL}/processos/batch/14", verify=False)

# Extrair processos e status
print("=" * 80)
print("BATCH #14 - STATUS")
print("=" * 80)

# Status geral
status_match = re.search(r'Status:.*?<[^>]*>([^<]+)', batch_resp.text)
if status_match:
    print(f"Status do Batch: {status_match.group(1).strip()}")

# Extrair tabela de processos
tables = re.findall(r'<table[^>]*>(.*?)</table>', batch_resp.text, re.DOTALL)
for table in tables:
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table, re.DOTALL)
    for row in rows[:10]:  # Primeiros 10 items
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) >= 3:
            # Extrair info de cada célula
            process_id = re.search(r'/process/(\d+)', cells[0])
            filename = re.sub(r'<[^>]+>', '', cells[0]).strip()
            status = re.sub(r'<[^>]+>', '', cells[1]).strip() if len(cells) > 1 else ''
            rpa_status = re.sub(r'<[^>]+>', '', cells[2]).strip() if len(cells) > 2 else ''
            
            if filename and filename != 'Arquivo':
                print(f"\nProcesso: {filename[:40]}")
                if process_id:
                    print(f"  ID: {process_id.group(1)}")
                print(f"  Status: {status}")
                print(f"  RPA: {rpa_status}")

# Buscar mensagens de erro
errors = re.findall(r'Erro[^<]*', batch_resp.text)
for error in errors[:5]:
    print(f"\n⚠️  {error}")
