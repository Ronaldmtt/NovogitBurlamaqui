# Banco de Dados de Clientes - Guia de Manutenção

## 📋 Visão Geral

O sistema usa um arquivo JSON (`data/clientes_database.json`) como fonte centralizada de dados para detecção inteligente de clientes em documentos jurídicos. Este arquivo foi extraído do arquivo DOCX "CLIENTE X CÉLULA x PARTE INTERESSADA" fornecido pelo cliente.

## 🗂️ Estrutura do JSON

```json
{
  "clientes": {
    "Nome do Cliente": {
      "nome_cliente": "Nome do Cliente",
      "partes": ["Parte Interessada 1", "Parte Interessada 2", ...],
      "celula": "Nome da Célula"
    }
  },
  "partes_interessadas": [
    {
      "nome": "Nome da Parte Interessada",
      "cliente": "Nome do Cliente",
      "celula": "Nome da Célula"
    }
  ]
}
```

## 📊 Estatísticas Atuais

- **Total de clientes únicos:** 24
- **Total de partes interessadas:** 137
- **Principais clientes:**
  - Casas Bahia (16 empresas)
  - HAZTEC (13 empresas)
  - GRUPO KPFR (9 empresas)
  - Grupo EBX (9 empresas)
  - CSN (7 empresas)
  - CNS (6 empresas)
  - E mais...

## ➕ Como Adicionar Novos Clientes

### Opção 1: Atualização Manual do JSON

1. Abra `data/clientes_database.json`
2. Adicione o novo cliente em `clientes`:
   ```json
   "Novo Cliente": {
     "nome_cliente": "Novo Cliente",
     "partes": ["Empresa A Ltda", "Empresa B S.A."],
     "celula": "Trabalhistas Outros clientes"
   }
   ```
3. Adicione cada parte interessada em `partes_interessadas`:
   ```json
   {
     "nome": "Empresa A Ltda",
     "cliente": "Novo Cliente",
     "celula": "Trabalhistas Outros clientes"
   }
   ```

### Opção 2: Reprocessar o DOCX

Se o arquivo DOCX foi atualizado:

```bash
cd /home/runner/workspace
python3 << 'EOF'
from docx import Document
import json
import os

# Carrega o documento DOCX
doc = Document("attached_assets/CLIENTE X CÉLULA_1762959455538.docx")

# Extrai dados da tabela
data = {
    "clientes": {},
    "partes_interessadas": []
}

for table in doc.tables:
    for row in table.rows:
        cells = [cell.text.strip() for cell in row.cells]
        
        if len(cells) >= 3 and cells[0] not in ["CÉLULA", ""]:
            celula = cells[0]
            cliente = cells[1]
            parte = cells[2]
            
            if cliente and parte:
                cliente_norm = cliente.strip()
                parte_norm = parte.strip()
                
                data["partes_interessadas"].append({
                    "nome": parte_norm,
                    "cliente": cliente_norm,
                    "celula": celula
                })
                
                if cliente_norm not in data["clientes"]:
                    data["clientes"][cliente_norm] = {
                        "nome_cliente": cliente_norm,
                        "partes": [],
                        "celula": celula
                    }
                
                if parte_norm not in data["clientes"][cliente_norm]["partes"]:
                    data["clientes"][cliente_norm]["partes"].append(parte_norm)

# Salva JSON
os.makedirs("data", exist_ok=True)
with open("data/clientes_database.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print("✅ JSON atualizado com sucesso!")
EOF
```

## 🔍 Como Funciona a Detecção

O sistema usa **fuzzy matching** (correspondência aproximada) para reconhecer variações de nomes:

1. **Busca Exata:** Primeiro tenta encontrar correspondência exata (case-insensitive, sem acentos)
2. **Fuzzy Matching:** Se não encontrar exata, usa algoritmo `token_set_ratio` com threshold de 85%
3. **Normalização:** Remove acentos, normaliza espaços, ignora maiúsculas/minúsculas

### Exemplos de Variações Reconhecidas

| Nome no PDF | Cliente Detectado |
|------------|-------------------|
| `PROFARMA DISTRIBUIDORA DE PRODUTOS FARMACEUTICOS SA` | Profarma |
| `ORIZON MEIO AMBIENTE S.A.` | HAZTEC |
| `CBSI COMPANHIA BRASILEIRA DE SERVICOS` | CSN |
| `BANQI INSTITUICAO DE PAGAMENTO LTDA` | Casas Bahia |
| `GRUPO CASAS BAHIA S.A` | Casas Bahia |

## 🧪 Testando Alterações

Após atualizar o JSON, teste com:

```python
from extractors.brand_map import normalize_cliente, find_cliente_by_parte_interessada

# Teste 1: Normalização direta
cliente = normalize_cliente("NOME DA EMPRESA S.A.")
print(f"Cliente detectado: {cliente}")

# Teste 2: Busca de cliente por parte interessada
cliente = find_cliente_by_parte_interessada("NOME DA EMPRESA S.A.", threshold=85)
print(f"Cliente encontrado: {cliente}")
```

## 🔧 Funções Disponíveis

### `normalize_cliente(nome_cliente: str) -> str`
Normaliza o nome do cliente usando o banco de dados JSON.

### `find_cliente_by_parte_interessada(nome_parte: str, threshold: int = 85) -> Optional[str]`
Busca o cliente correspondente a uma parte interessada.

### `get_all_clientes() -> List[str]`
Retorna lista de todos os clientes cadastrados.

### `get_partes_by_cliente(nome_cliente: str) -> List[str]`
Retorna todas as partes interessadas de um cliente específico.

## ⚠️ Importante

- O arquivo JSON é **cacheado em memória** na primeira leitura
- Após alterar o JSON, **reinicie o servidor** para ver as mudanças
- Mantenha a estrutura do JSON consistente
- Use **UTF-8** para acentuação correta
- **Threshold de 85%** funciona bem na maioria dos casos

## 📚 Referências

- Código: `extractors/brand_map.py`
- Dados: `data/clientes_database.json`
- Documentação original: `attached_assets/CLIENTE X CÉLULA_1762959455538.docx`
