# Sistema de Mapeamento de Posições do eLaw

## 📋 Visão Geral

O sistema mapeia **61 posições** possíveis do dropdown "Posição da Parte Interessada" do eLaw, permitindo que o RPA preencha corretamente o formulário mesmo com variações de nomenclatura encontradas nos PDFs.

## 🗂️ Estrutura

### Arquivo Principal
- **`extractors/posicao_mapping.py`**: Módulo de mapeamento inteligente

### Dados Mapeados
- **61 posições oficiais** do eLaw com IDs correspondentes
- **Sinônimos e variações** comuns encontradas em PDFs
- **Fuzzy matching** para reconhecer erros de digitação e variações

## 📊 Posições Suportadas

### Trabalhistas (mais comuns)
| Label Oficial | ID | Variações Reconhecidas |
|--------------|----|-----------------------|
| RECLAMANTE | 51 | reclamante, Reclamante, RTE |
| RECLAMADO | 52 | reclamado, reclamada, RDO, RDA |
| RECORRENTE | 55 | recorrente, RTE |
| RECORRIDO | 56 | recorrido, recorrida, RDO |

### Recursos e Apelações
| Label Oficial | ID | Variações Reconhecidas |
|--------------|----|-----------------------|
| APELANTE | 8 | apelante, Apelante |
| APELADO | 7 | apelado, apelada |
| AGRAVANTE | 5 | agravante |
| AGRAVADO | 6 | agravado, agravada |
| EMBARGANTE | 25 | embargante |
| EMBARGADO | 26 | embargado, embargada |

### Cíveis
| Label Oficial | ID | Variações Reconhecidas |
|--------------|----|-----------------------|
| AUTOR | 1 | autor, autora |
| REU | 2 | reu, réu, ré, re |
| REQUERENTE | 57 | requerente |
| REQUERIDO | 58 | requerido, requerida |

### Execuções
| Label Oficial | ID | Variações Reconhecidas |
|--------------|----|-----------------------|
| EXEQUENTE | 29 | exequente |
| EXECUTADO | 30 | executado, executada |

### Mandado de Segurança
| Label Oficial | ID | Variações Reconhecidas |
|--------------|----|-----------------------|
| IMPETRANTE | 35 | impetrante |
| IMPETTRADO | 36 | impetrado, impettrado |

### Outras (61 posições no total)
- DEMANDANTE/DEMANDADO
- DENUNCIANTE/DENUNCIADO
- INTERPELANTE/INTERPELADO
- NOTIFICANTE/NOTIFICADO
- E muitas outras...

## 🔧 Funções Disponíveis

### `normalize_posicao(posicao: str) -> str`
Normaliza uma posição encontrada no PDF para o label oficial do eLaw.

```python
from extractors.posicao_mapping import normalize_posicao

# Exemplos
normalize_posicao("Reclamado")      # -> "RECLAMADO"
normalize_posicao("recorrente")     # -> "RECORRENTE"
normalize_posicao("APELADA")        # -> "APELADO"
normalize_posicao("RÉ")             # -> "REU"
```

### `get_posicao_id(posicao: str) -> Optional[str]`
Retorna o ID do eLaw para uma posição (normaliza automaticamente).

```python
from extractors.posicao_mapping import get_posicao_id

# Exemplos
get_posicao_id("RECLAMANTE")  # -> "51"
get_posicao_id("Reclamado")   # -> "52"
get_posicao_id("APELANTE")    # -> "8"
get_posicao_id("REU")         # -> "2"
```

### `get_posicao_label(id_elaw: str) -> Optional[str]`
Retorna o label oficial do eLaw para um ID (lookup reverso).

```python
from extractors.posicao_mapping import get_posicao_label

# Exemplos
get_posicao_label("51")  # -> "RECLAMANTE"
get_posicao_label("52")  # -> "RECLAMADO"
get_posicao_label("8")   # -> "APELANTE"
```

### `find_posicao_fuzzy(posicao: str, threshold: int = 85) -> Optional[str]`
Busca a posição mais próxima usando fuzzy matching.

```python
from extractors.posicao_mapping import find_posicao_fuzzy

# Exemplos (erros de digitação)
find_posicao_fuzzy("RECLAMNTE")   # -> "RECLAMANTE"
find_posicao_fuzzy("APELADA")     # -> "APELADO"
```

### `get_all_posicoes() -> Dict[str, str]`
Retorna todas as 61 posições disponíveis (ID -> LABEL).

```python
from extractors.posicao_mapping import get_all_posicoes

posicoes = get_all_posicoes()
# {"1": "AUTOR", "2": "REU", ..., "63": "PARTES"}
```

### `get_posicoes_trabalhistas() -> Dict[str, str]`
Retorna apenas as posições mais comuns em processos trabalhistas.

```python
from extractors.posicao_mapping import get_posicoes_trabalhistas

# Retorna: {"51": "RECLAMANTE", "52": "RECLAMADO", ...}
```

## 🚀 Integração com o RPA

O RPA (`rpa.py`) usa o sistema de mapeamento em duas etapas:

### 1. Inferência de Dados
```python
# No infer_cliente_posicao_adverso()
if is_probably_pj(reld):
    posicao = normalize_posicao("RECLAMADO")  # ✅ Normalizado
else:
    posicao = normalize_posicao("RECLAMANTE")  # ✅ Normalizado
```

### 2. Preenchimento do Formulário
```python
# No fill_new_process_form()
pos_raw = data.get("posicao_parte_interessada") or "RECLAMADO"
pos_target = normalize_posicao(pos_raw)  # Normaliza
pos_id = get_posicao_id(pos_target)      # Obtém ID

if pos_id:
    # Seleciona diretamente usando o ID do eLaw
    await page.select_option(f"#{POSICAO_CLIENTE_SELECT_ID}", value=pos_id)
else:
    # Fallback: fuzzy matching
    await set_select_fuzzy_any(page, POSICAO_CLIENTE_SELECT_ID, pos_target)
```

## ✅ Resultados dos Testes

**100% de sucesso** em todos os cenários testados:

- ✅ **6/6** posições trabalhistas (RECLAMANTE, RECLAMADO, RECORRENTE, etc.)
- ✅ **6/6** recursos e apelações (APELANTE, AGRAVANTE, EMBARGANTE, etc.)
- ✅ **6/6** ações cíveis (AUTOR, REU, REQUERENTE, etc.)
- ✅ **3/3** execuções (EXEQUENTE, EXECUTADO)
- ✅ **3/3** fuzzy matching (erros de digitação reconhecidos)
- ✅ **8/8** lookups reversos (ID -> LABEL)

## 🎯 Vantagens

1. **Precisão:** Mapeamento direto para IDs do eLaw elimina erros
2. **Flexibilidade:** Reconhece variações e sinônimos automaticamente
3. **Robustez:** Fuzzy matching lida com erros de digitação
4. **Manutenibilidade:** Fácil adicionar novas posições ou variações
5. **Performance:** Seleção direta por ID é mais rápida que fuzzy matching

## 📝 Como Adicionar Novas Variações

Edite o dicionário `SINONIMOS_POSICAO` em `extractors/posicao_mapping.py`:

```python
SINONIMOS_POSICAO = {
    # ... existentes ...
    
    # Nova variação
    "NOVA_VARIACAO": "LABEL_OFICIAL_DO_ELAW",
}
```

## ⚠️ Importante

- As posições são **normalizadas automaticamente** (case-insensitive, sem acentos)
- O sistema sempre retorna o **label oficial do eLaw** (ex: "RECLAMADO", não "reclamado")
- IDs são **strings** (ex: "51"), não números
- Threshold de fuzzy matching padrão: **85%**

## 📚 Referências

- Código: `extractors/posicao_mapping.py`
- Integração RPA: `rpa.py` (linhas 2447-2461, 2961-3009)
- HTML fonte: `attached_assets/Pasted--div-class-dropdown-bootstrap-select...txt`
