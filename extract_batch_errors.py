#!/usr/bin/env python3
import requests
import re
import urllib3
from html.parser import HTMLParser

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_table = False
        self.in_row = False
        self.in_cell = False
        self.current_row = []
        self.rows = []
        self.current_text = []
        
    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.in_table = True
        elif tag == 'tr' and self.in_table:
            self.in_row = True
            self.current_row = []
        elif tag == 'td' and self.in_row:
            self.in_cell = True
            self.current_text = []
            
    def handle_endtag(self, tag):
        if tag == 'table':
            self.in_table = False
        elif tag == 'tr':
            if self.current_row:
                self.rows.append(self.current_row)
            self.in_row = False
        elif tag == 'td':
            self.in_cell = False
            self.current_row.append(' '.join(self.current_text).strip())
            
    def handle_data(self, data):
        if self.in_cell:
            text = data.strip()
            if text:
                self.current_text.append(text)

PROD_URL = "https://fg-bularmaci-processos.replit.app"
session = requests.Session()

# Login
print("🔐 Login em produção...")
login_page = session.get(f"{PROD_URL}/login", verify=False)
csrf_token = re.search(r'csrf_token.*?value="([^"]+)"', login_page.text).group(1)
session.post(f"{PROD_URL}/login", data={
    "csrf_token": csrf_token,
    "username": "admin",
    "password": "admin123"
}, verify=False)

# Verificar batch #14
print(f"\n📦 Buscando detalhes do batch #14...")
batch_resp = session.get(f"{PROD_URL}/processos/batch/14", verify=False)

# Parse tabela
parser = TableParser()
parser.feed(batch_resp.text)

print("\n" + "="*80)
print("BATCH #14 - PROCESSOS E ERROS")
print("="*80)

for i, row in enumerate(parser.rows):
    if len(row) >= 3 and i > 0:  # Skip header
        print(f"\n📄 Item #{i}:")
        print(f"  Arquivo: {row[0][:60]}")
        print(f"  Status: {row[1]}")
        if len(row) > 2:
            print(f"  Status RPA: {row[2]}")
        if len(row) > 3:
            print(f"  Mensagem/Erro: {row[3][:200]}")

# Tentar extrair mensagens de erro do JavaScript ou JSON embutido
json_data = re.search(r'var\s+batchData\s*=\s*(\{.*?\});', batch_resp.text, re.DOTALL)
if json_data:
    import json
    try:
        data = json.loads(json_data.group(1))
        print(f"\n📊 Dados JSON encontrados: {json.dumps(data, indent=2)}")
    except:
        pass

# Buscar por tooltips ou popups com mensagens de erro
tooltips = re.findall(r'data-bs-content="([^"]+)"', batch_resp.text)
for tooltip in tooltips[:5]:
    print(f"\n💬 Tooltip: {tooltip[:200]}")
