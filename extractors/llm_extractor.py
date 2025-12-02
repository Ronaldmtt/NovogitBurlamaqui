# extractors/llm_extractor.py
import json
import os
import re
from typing import Dict

# Integração com monitor remoto
try:
    from monitor_integration import log_info, log_error
    MONITOR_AVAILABLE = True
except ImportError:
    MONITOR_AVAILABLE = False
    def log_info(msg, region=""): pass
    def log_error(msg, exc=None, region=""): pass

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # troque se quiser
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY_2")

SCHEMA = {
    "type": "object",
    "properties": {
        "numero_processo": {"type": "string"},
        "sistema_eletronico": {"type": "string"},
        "area_direito": {"type": "string"},
        "sub_area_direito": {"type": "string"},
        "estado": {"type": "string"},
        "comarca": {"type": "string"},
        "orgao": {"type": "string"},
        "vara": {"type": "string"},
        "foro": {"type": "string"},
        "instancia": {"type": "string"},
        "assunto": {"type": "string"},
        "objeto": {"type": "string"},
        "sub_objeto": {"type": "string"},
        "distribuido_em": {"type": "string"},
        "audiencia_tipo": {"type": "string"},
        "audiencia_data_hora": {"type": "string"},
        "audiencia_local": {"type": "string"},
        "posicao_parte_interessada": {"type": "string"},
        "parte_interessada": {"type": "string"},
        "parte_adversa_tipo": {"type": "string"},
        "parte_adversa_nome": {"type": "string"}
    },
    "required": ["numero_processo"]
}

INSTRUCTIONS = """Você é um extrator de dados jurídicos.
Retorne APENAS um JSON válido que respeite o schema a seguir.

INSTRUÇÕES IMPORTANTES:
- Campo 'sistema_eletronico' use 'PJe', 'eproc', 'Projudi' ou vazio se não achar.
- 'estado' deve ser a sigla UF (ex.: 'RJ').
- Datas no formato dd/mm/aaaa hh:mm quando houver hora, senão dd/mm/aaaa.
- Não invente dados: deixe vazio se não tiver certeza.

ATENÇÃO CRÍTICA - DIFERENÇA ENTRE DATAS:

1. 'distribuido_em': É a DATA DE DISTRIBUIÇÃO do processo ao juízo
   - Procure por: "Distribuído em DD/MM/AAAA" no cabeçalho do documento
   - Exemplo: "Distribuído em 25/09/2025 12:16:24"
   - Esta data aparece NO CABEÇALHO/INÍCIO do documento
   - NÃO confundir com data de audiência!

2. 'audiencia_data_hora': É a DATA E HORA DA AUDIÊNCIA INICIAL quando agendada
   - Procure por: "audiência INICIAL", "audiência inicial", "Determino a audiência INICIAL"
   - Exemplo: "audiência INICIAL TELEPRESENCIAL... : 12/12/2025 09:10"
   - Esta data aparece no CORPO do documento, geralmente após o texto de decisão
   - Só preencher se houver EXPLICITAMENTE menção a "audiência INICIAL" ou "audiência inicial"
   - Se não encontrar "audiência inicial", deixe VAZIO

REGRA DE OURO: 
- Data de Distribuição = cabeçalho ("Distribuído em...")
- Audiência Inicial = corpo do documento ("audiência INICIAL...")
- São datas DIFERENTES e não devem ser confundidas!

Schema:
{}
""".format(json.dumps(SCHEMA, ensure_ascii=False))

def _call_openai_json(texto: str) -> Dict:
    """
    Chama a IA. Se não houver chave, retorna {}.
    Saída sempre é um dict (vazio em falha).
    """
    if not OPENAI_API_KEY:
        return {}

    try:
        # Cliente oficial (>= 1.0). Ajuste conforme seu SDK.
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

        prompt = INSTRUCTIONS + "\n\n---\nTEXTO DO PDF:\n" + texto[:35000]  # OTIMIZADO: texto suficiente para campos essenciais

        # Usamos "responses" (novo) ou "chat.completions" (legado).
        # Aqui opto por "chat.completions" para compatibilidade ampla.
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "Você extrai dados para preencher um formulário jurídico."},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
        )

        content = resp.choices[0].message.content
        if not content:
            return {}
        content = content.strip()
        # Tenta achar um bloco JSON mesmo que a IA envolva em texto
        m = re.search(r"\{.*\}", content, flags=re.S)
        if m:
            content = m.group(0)
        data = json.loads(content)
        if isinstance(data, dict):
            return data
    except Exception:
        return {}
    return {}

def extract_fields_with_llm(texto: str) -> Dict:
    """
    API pública: retorna um dict com os campos do schema extraídos pela IA.
    """
    data = _call_openai_json(texto)
    # Sanitização mínima
    if not isinstance(data, dict):
        return {}
    for k, v in list(data.items()):
        if isinstance(v, str):
            data[k] = re.sub(r"\s+", " ", v).strip()
    return data


# ========== FALLBACK LLM PARA CAMPOS TRABALHISTAS ==========

LABOR_SCHEMA = {
    "type": "object",
    "properties": {
        "motivo_demissao": {
            "type": "string",
            "enum": ["sem_justa_causa", "justa_causa", "term_exp", "rescisao_indireta", "pedido_demissao", ""]
        },
        "local_trabalho": {"type": "string"},
        "empregador": {"type": "string"},
        "salario": {"type": "string"},
        "pis": {"type": "string"},
        "ctps_numero": {"type": "string"},
        "ctps_serie_uf": {"type": "string"}
    }
}

LABOR_INSTRUCTIONS = """Você é um extrator especializado em dados trabalhistas de PDFs jurídicos brasileiros.
Retorne APENAS um JSON válido com os campos solicitados.

PRIORIZAÇÃO DE FONTES (use nesta ordem):
1. TRCT (Termo de Rescisão do Contrato de Trabalho) - prioridade MÁXIMA
2. CTPS (Carteira de Trabalho) - prioridade ALTA
3. Petição inicial / narrativa - prioridade BAIXA

INSTRUÇÕES POR CAMPO:

1. motivo_demissao:
   - TRCT: procure "Causa da rescisão" ou "Tipo de desligamento"
   - Narrativa: procure "sem justa causa", "rescisão indireta", "término de contrato/experiência", "pedido de demissão"
   - Valores aceitos: "sem_justa_causa", "justa_causa", "term_exp", "rescisao_indireta", "pedido_demissao"
   - Se não encontrar, retorne vazio ""

2. local_trabalho:
   - Procure: "Último local de trabalho", "Endereço do estabelecimento", "Local de trabalho"
   - TRCT: "Estabelecimento/Unidade/Endereço"
   - Deve ser endereço completo com rua/avenida, bairro ou cidade
   - Se não encontrar endereço real, retorne vazio ""

3. empregador:
   - TRCT: "IDENTIFICAÇÃO DO EMPREGADOR" (razão social)
   - CTPS: razão social ou nome fantasia da empresa
   - Narrativa: qualificação da ré ("em face de...", "RECLAMADA:")
   - Retorne a razão social/nome da empresa

4. salario:
   - TRCT: campo "Última remuneração" ou "Remuneração" (mais confiável)
   - CTPS: campo de salário na anotação
   - Narrativa: procure "percebia", "recebia", "ganhava", "último salário", "remuneração"
   - Formato: "R$ X.XXX,XX" (ex: "R$ 1.200,00", "R$ 10.500,50")
   - NÃO confundir com: piso salarial, "deveria receber", valores totais de rescisão
   - Priorize SALÁRIO EFETIVAMENTE RECEBIDO, não valores rescisórios
   - Se não encontrar, retorne vazio ""

5. pis:
   - TRCT: campo "PIS/PASEP" no bloco do trabalhador
   - Formato: 11 dígitos (ex: "204.05911.17-8")
   - Se encontrar, mantenha a formatação encontrada
   - Se não encontrar, retorne vazio ""

5. ctps_numero e ctps_serie_uf:
   - CTPS: procure "CTPS", "Carteira de Trabalho", "série", "UF"
   - Separe número (ex: "1210996") e série/UF (ex: "2780/RJ")
   - Se estiverem juntos: "CTPS nº 123456, série 149/RJ" → numero="123456", serie_uf="149/RJ"
   - Se não encontrar, retorne vazio ""

REGRAS CRÍTICAS:
- NÃO invente dados: se não encontrar, deixe campo vazio ""
- Priorize informações de TRCT sobre outras fontes
- Para endereços, exija que tenha indicadores geográficos reais (rua, avenida, bairro)
- Para PIS: exatamente 11 dígitos
- Para CTPS série: deve ter UF (2 letras maiúsculas) ou ser numérica

Schema:
{}
""".format(json.dumps(LABOR_SCHEMA, ensure_ascii=False))


def extract_labor_fields_with_llm(texto: str) -> Dict:
    """
    Extrai campos trabalhistas usando LLM como fallback.
    Retorna dict com: motivo_demissao, local_trabalho, empregador, salario, pis, ctps_numero, ctps_serie_uf
    """
    log_info("Iniciando extração LLM (GPT-4o-mini) para campos trabalhistas", region="LLM_EXTRACTOR")
    
    if not OPENAI_API_KEY:
        return {}

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

        prompt = LABOR_INSTRUCTIONS + "\n\n---\nTEXTO DO PDF:\n" + texto[:35000]  # OTIMIZADO: texto suficiente para campos trabalhistas

        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "Você é um extrator especializado em dados trabalhistas."},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
        )

        content = resp.choices[0].message.content
        if not content:
            return {}
        content = content.strip()
        m = re.search(r"\{.*\}", content, flags=re.S)
        if m:
            content = m.group(0)
        data = json.loads(content)
        
        if isinstance(data, dict):
            # Sanitizar e validar
            result = {}
            if data.get("motivo_demissao") and data["motivo_demissao"].strip():
                result["motivo_demissao"] = data["motivo_demissao"].strip()
            if data.get("local_trabalho") and data["local_trabalho"].strip():
                result["local_trabalho"] = re.sub(r"\s+", " ", data["local_trabalho"]).strip()
            if data.get("empregador") and data["empregador"].strip():
                result["empregador"] = re.sub(r"\s+", " ", data["empregador"]).strip()
            if data.get("salario") and data["salario"].strip():
                result["salario"] = data["salario"].strip()
            if data.get("pis") and data["pis"].strip():
                result["pis"] = data["pis"].strip()
            if data.get("ctps_numero") and data["ctps_numero"].strip():
                result["ctps_numero"] = data["ctps_numero"].strip()
            if data.get("ctps_serie_uf") and data["ctps_serie_uf"].strip():
                result["ctps_serie_uf"] = data["ctps_serie_uf"].strip()
            
            extracted_fields = list(result.keys())
            log_info(f"LLM extraiu {len(extracted_fields)} campos: {', '.join(extracted_fields)}", region="LLM_EXTRACTOR")
            return result
    except Exception as e:
        log_error(f"Erro ao extrair com LLM: {e}", exc=e, region="LLM_EXTRACTOR")
        return {}
    return {}


# ========== FALLBACK LLM - QUERIES ESPECÍFICAS POR CAMPO ==========
# 2025-11-28: Baseado na arquitetura RAG do prompt de extração otimizado
# Usa perguntas direcionadas quando regex + LLM geral falham

def _query_llm_single_field(texto: str, query: str, field_name: str) -> str | None:
    """
    Faz uma query específica ao LLM para um único campo.
    Retorna apenas o valor extraído ou None.
    """
    if not OPENAI_API_KEY:
        return None
    
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        
        prompt = f"""Analise o texto de um processo trabalhista e responda APENAS com o valor solicitado.
NÃO inclua explicações, apenas o valor.
Se não encontrar, responda apenas: NAO_ENCONTRADO

PERGUNTA: {query}

---
TEXTO DO PROCESSO:
{texto[:100000]}
"""
        
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "Você é um extrator de dados jurídicos. Responda APENAS com o valor solicitado, sem explicações."},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
            max_tokens=100
        )
        
        content = resp.choices[0].message.content
        if not content:
            return None
        
        content = content.strip()
        
        if "NAO_ENCONTRADO" in content.upper() or "NÃO ENCONTRADO" in content.upper():
            return None
        
        if content and len(content) < 200:
            log_info(f"[{field_name}] Query LLM específica retornou: {content[:50]}", region="LLM_QUERY")
            return content
        
        return None
        
    except Exception as e:
        log_error(f"Erro em query LLM específica para {field_name}: {e}", exc=e, region="LLM_QUERY")
        return None


def extract_pis_with_llm_query(texto: str) -> str | None:
    """Extrai PIS usando query LLM específica (fallback final)."""
    query = "Qual é o número de PIS/PASEP do reclamante/trabalhador? Responda APENAS com o número no formato XXX.XXXXX.XX-X ou apenas dígitos."
    result = _query_llm_single_field(texto, query, "PIS")
    
    if result:
        digits = re.sub(r'[^\d]', '', result)
        if len(digits) == 11:
            return f"{digits[:3]}.{digits[3:8]}.{digits[8:10]}-{digits[10]}"
        elif 10 <= len(digits) <= 12:
            return result
    return None


def extract_ctps_with_llm_query(texto: str) -> str | None:
    """Extrai CTPS usando query LLM específica (fallback final)."""
    query = "Qual é o número da CTPS (Carteira de Trabalho) do reclamante? Responda no formato: NUMERO série SERIE/UF (ex: 123456 série 789/RJ)"
    return _query_llm_single_field(texto, query, "CTPS")


def extract_data_admissao_with_llm_query(texto: str) -> str | None:
    """Extrai data de admissão usando query LLM específica (fallback final)."""
    query = "Qual é a data de admissão/contratação do trabalhador reclamante? Responda APENAS com a data no formato dd/mm/aaaa."
    result = _query_llm_single_field(texto, query, "DATA_ADMISSAO")
    
    if result:
        date_match = re.search(r'(\d{2}[/.\-]\d{2}[/.\-]\d{4})', result)
        if date_match:
            return date_match.group(1).replace('.', '/').replace('-', '/')
    return None


def extract_data_demissao_with_llm_query(texto: str) -> str | None:
    """Extrai data de demissão usando query LLM específica (fallback final)."""
    query = "Qual é a data de demissão/dispensa/término do contrato do trabalhador? Responda APENAS com a data no formato dd/mm/aaaa."
    result = _query_llm_single_field(texto, query, "DATA_DEMISSAO")
    
    if result:
        date_match = re.search(r'(\d{2}[/.\-]\d{2}[/.\-]\d{4})', result)
        if date_match:
            return date_match.group(1).replace('.', '/').replace('-', '/')
    return None


def extract_salario_with_llm_query(texto: str) -> str | None:
    """Extrai salário usando query LLM específica (fallback final)."""
    query = "Qual é o último salário mensal do trabalhador reclamante (não valor de rescisão)? Responda APENAS com o valor no formato R$ X.XXX,XX."
    result = _query_llm_single_field(texto, query, "SALARIO")
    
    if result:
        money_match = re.search(r'R\$\s*[\d\.\s,]+', result)
        if money_match:
            value = money_match.group().strip()
            value = re.sub(r'\s+', '', value)
            return value
    return None


def extract_cargo_with_llm_query(texto: str) -> str | None:
    """Extrai cargo/função usando query LLM específica (fallback final)."""
    query = "Qual é o cargo ou função exercida pelo trabalhador reclamante? Responda APENAS com o nome do cargo."
    result = _query_llm_single_field(texto, query, "CARGO")
    
    if result and len(result) <= 100:
        result = result.upper()
        result = re.sub(r'\s+', ' ', result).strip()
        return result
    return None


def extract_data_audiencia_with_llm_query(texto: str) -> str | None:
    """Extrai data de audiência usando query LLM específica (fallback final)."""
    query = "Qual é a data e hora da audiência designada neste processo? Responda no formato dd/mm/aaaa HH:MM."
    result = _query_llm_single_field(texto, query, "DATA_AUDIENCIA")
    
    if result:
        datetime_match = re.search(r'(\d{2}[/.\-]\d{2}[/.\-]\d{4})\s*(?:às?\s*)?(\d{1,2}:\d{2})?', result)
        if datetime_match:
            date_part = datetime_match.group(1).replace('.', '/').replace('-', '/')
            time_part = datetime_match.group(2) or ""
            if time_part:
                return f"{date_part} {time_part}"
            return date_part
    return None


# ========== OTIMIZAÇÕES LLM - PEDIDOS, RECLAMADAS, VALIDAÇÃO ==========
# 2025-12-02: Funções avançadas para melhorar precisão da extração

from typing import List, Optional

# Schema para extração de pedidos
PEDIDOS_SCHEMA = {
    "type": "object",
    "properties": {
        "pedidos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "descricao": {"type": "string"},
                    "categoria": {
                        "type": "string",
                        "enum": [
                            "verbas_rescisorias",      # Aviso prévio, férias, 13º, FGTS+40%
                            "salariais",               # Horas extras, adicional noturno, insalubridade
                            "indenizatorios",          # Danos morais, assédio, acidente
                            "acessorios",              # Honorários, custas, juros
                            "outros"
                        ]
                    },
                    "valor_estimado": {"type": "string"}  # Se mencionado
                }
            }
        },
        "total_pedidos": {"type": "integer"}
    }
}

PEDIDOS_INSTRUCTIONS = """Você é um extrator especializado em pedidos de petições trabalhistas brasileiras.
Analise o texto e extraia TODOS os pedidos feitos pelo reclamante.

LOCALIZAÇÃO DOS PEDIDOS:
1. Seção "DOS PEDIDOS" ou "DOS REQUERIMENTOS"
2. Após "Diante o exposto, requer:"
3. Listados com letras (a, b, c) ou números (1, 2, 3)

CATEGORIAS DE PEDIDOS:
1. verbas_rescisorias: Aviso prévio, férias proporcionais/vencidas, 13º salário, FGTS, multa 40%
2. salariais: Horas extras, adicional noturno, insalubridade, periculosidade, diferenças salariais
3. indenizatorios: Danos morais, danos materiais, assédio, acidente de trabalho
4. acessorios: Honorários advocatícios, custas processuais, juros, correção monetária
5. outros: Qualquer pedido que não se encaixe nas categorias acima

REGRAS:
- Extraia o pedido COMPLETO, não apenas palavras-chave
- Se houver valor estimado no pedido, inclua
- NÃO invente pedidos - extraia apenas os que estão no texto
- Retorne array vazio se não encontrar seção de pedidos

Retorne JSON no formato:
{
    "pedidos": [
        {"descricao": "...", "categoria": "verbas_rescisorias", "valor_estimado": "R$ X.XXX,XX"},
        ...
    ],
    "total_pedidos": N
}
"""


def extract_pedidos_with_llm(texto: str) -> List[dict]:
    """
    Extrai pedidos da petição usando LLM para maior precisão.
    Fallback quando regex não encontra ou encontra poucos pedidos.
    
    Returns:
        Lista de dicts com: descricao, categoria, valor_estimado (opcional)
    """
    log_info("Iniciando extração LLM para pedidos", region="LLM_PEDIDOS")
    
    if not OPENAI_API_KEY:
        log_info("API key não disponível, retornando lista vazia", region="LLM_PEDIDOS")
        return []
    
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        
        # Usar até 50k chars para capturar seção de pedidos completa
        prompt = PEDIDOS_INSTRUCTIONS + "\n\n---\nTEXTO DO PDF:\n" + texto[:50000]
        
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "Você é um extrator especializado em pedidos trabalhistas. Retorne APENAS JSON válido."},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
        )
        
        content = resp.choices[0].message.content
        if not content:
            return []
        
        content = content.strip()
        
        # Extrair JSON do response
        m = re.search(r'\{.*\}', content, flags=re.S)
        if m:
            content = m.group(0)
        
        data = json.loads(content)
        
        if isinstance(data, dict) and "pedidos" in data:
            pedidos = data["pedidos"]
            if isinstance(pedidos, list):
                # Sanitizar e validar cada pedido
                result = []
                for p in pedidos:
                    if isinstance(p, dict) and p.get("descricao"):
                        descricao = re.sub(r'\s+', ' ', p["descricao"]).strip()
                        if len(descricao) > 10:  # Mínimo de caracteres
                            result.append({
                                "descricao": descricao,
                                "categoria": p.get("categoria", "outros"),
                                "valor_estimado": p.get("valor_estimado", "")
                            })
                
                log_info(f"LLM extraiu {len(result)} pedidos", region="LLM_PEDIDOS")
                return result
        
        return []
        
    except Exception as e:
        log_error(f"Erro ao extrair pedidos com LLM: {e}", exc=e, region="LLM_PEDIDOS")
        return []


# Schema para extração de reclamadas
RECLAMADAS_SCHEMA = {
    "type": "object",
    "properties": {
        "reclamadas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nome": {"type": "string"},
                    "cnpj": {"type": "string"},
                    "tipo": {"type": "string", "enum": ["juridica", "fisica"]},
                    "posicao": {"type": "string"}  # RECLAMADO, RÉU, etc.
                }
            }
        }
    }
}

RECLAMADAS_INSTRUCTIONS = """Você é um extrator especializado em identificar as partes reclamadas (réus/empregadores) em processos trabalhistas brasileiros.

OBJETIVO: Identificar TODAS as empresas ou pessoas que são RÉS/RECLAMADAS no processo.

ONDE ENCONTRAR:
1. Cabeçalho do processo: "RECLAMADO:", "RÉ:", "RECLAMADA:"
2. Qualificação das partes
3. Corpo da petição: "em face de...", "contra..."
4. Múltiplas rés: "1ª RECLAMADA:", "2ª RECLAMADA:" ou listadas em sequência

IDENTIFICAÇÃO:
- Pessoa Jurídica: tem CNPJ, LTDA, S.A., S/A, EIRELI, ME, EPP, CIA, Companhia
- Pessoa Física: tem CPF, sem sufixos empresariais

REGRAS:
- Extraia o NOME COMPLETO da empresa/pessoa
- Se houver CNPJ, inclua no formato XX.XXX.XXX/XXXX-XX
- NÃO inclua o reclamante (trabalhador/autor)
- NÃO inclua advogados
- Se houver múltiplas empresas do mesmo grupo, liste todas separadamente

Retorne JSON no formato:
{
    "reclamadas": [
        {"nome": "EMPRESA XYZ LTDA", "cnpj": "12.345.678/0001-90", "tipo": "juridica", "posicao": "RECLAMADO"},
        {"nome": "EMPRESA ABC S.A.", "cnpj": "", "tipo": "juridica", "posicao": "2ª RECLAMADA"}
    ]
}
"""


def extract_reclamadas_with_llm(texto: str) -> List[dict]:
    """
    Extrai todas as partes reclamadas usando LLM para maior precisão.
    Útil quando regex não identifica corretamente ou há formatos incomuns.
    
    Returns:
        Lista de dicts com: nome, cnpj, tipo (juridica/fisica), posicao
    """
    log_info("Iniciando extração LLM para reclamadas", region="LLM_RECLAMADAS")
    
    if not OPENAI_API_KEY:
        log_info("API key não disponível, retornando lista vazia", region="LLM_RECLAMADAS")
        return []
    
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        
        # Usar primeiros 30k chars (cabeçalho + qualificação das partes)
        prompt = RECLAMADAS_INSTRUCTIONS + "\n\n---\nTEXTO DO PDF:\n" + texto[:30000]
        
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "Você é um extrator especializado em partes processuais. Retorne APENAS JSON válido."},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
        )
        
        content = resp.choices[0].message.content
        if not content:
            return []
        
        content = content.strip()
        
        # Extrair JSON do response
        m = re.search(r'\{.*\}', content, flags=re.S)
        if m:
            content = m.group(0)
        
        data = json.loads(content)
        
        if isinstance(data, dict) and "reclamadas" in data:
            reclamadas = data["reclamadas"]
            if isinstance(reclamadas, list):
                result = []
                for r in reclamadas:
                    if isinstance(r, dict) and r.get("nome"):
                        nome = re.sub(r'\s+', ' ', r["nome"]).strip().upper()
                        if len(nome) > 3:  # Nome mínimo válido
                            result.append({
                                "nome": nome,
                                "cnpj": r.get("cnpj", ""),
                                "tipo_pessoa": r.get("tipo", "juridica"),
                                "posicao": r.get("posicao", "RECLAMADO")
                            })
                
                log_info(f"LLM extraiu {len(result)} reclamadas", region="LLM_RECLAMADAS")
                return result
        
        return []
        
    except Exception as e:
        log_error(f"Erro ao extrair reclamadas com LLM: {e}", exc=e, region="LLM_RECLAMADAS")
        return []


# Schema para validação de dados
VALIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "is_valid": {"type": "boolean"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "issue": {"type": "string"},
                    "suggestion": {"type": "string"}
                }
            }
        },
        "corrections": {
            "type": "object"  # campo: valor_corrigido
        }
    }
}

VALIDATION_INSTRUCTIONS = """Você é um validador de dados jurídicos extraídos de processos trabalhistas.
Analise os dados extraídos e verifique consistência e correção.

VALIDAÇÕES A FAZER:

1. DATAS:
   - Data de admissão deve ser ANTERIOR à data de demissão
   - Datas não podem ser futuras (referência: hoje é {today})
   - Formato deve ser dd/mm/aaaa
   - Datas não podem ter ano < 1950 ou > ano atual

2. VALORES MONETÁRIOS:
   - Salário deve estar em formato R$ X.XXX,XX
   - Salário deve ser razoável (> R$ 1.000,00 e < R$ 500.000,00)

3. DOCUMENTOS:
   - PIS: deve ter 11 dígitos
   - CTPS: número + série/UF

4. PARTES:
   - Reclamante e reclamado devem ser diferentes
   - Reclamado deve parecer nome de empresa (para PJ) ou pessoa (para PF)

5. CONSISTÊNCIA GERAL:
   - Se há data de demissão, deve haver data de admissão
   - Se há salário, deve parecer valor mensal (não anual, não total rescisório)

Retorne JSON no formato:
{
    "is_valid": true/false,
    "issues": [
        {"field": "data_admissao", "issue": "Data posterior à demissão", "suggestion": "Verificar datas"}
    ],
    "corrections": {
        "data_admissao": "01/05/2020"  // Se houver correção óbvia
    }
}
"""


def validate_extracted_data_with_llm(data: dict, texto: str = "") -> dict:
    """
    Valida os dados extraídos usando LLM para detectar inconsistências.
    
    Args:
        data: Dict com dados extraídos (data_admissao, data_demissao, salario, etc.)
        texto: Texto do PDF para contexto adicional (opcional)
    
    Returns:
        Dict com: is_valid, issues (lista), corrections (dict campo->valor)
    """
    log_info("Iniciando validação LLM dos dados extraídos", region="LLM_VALIDATION")
    
    if not OPENAI_API_KEY:
        log_info("API key não disponível, assumindo dados válidos", region="LLM_VALIDATION")
        return {"is_valid": True, "issues": [], "corrections": {}}
    
    try:
        from openai import OpenAI
        from datetime import datetime
        
        client = OpenAI(api_key=OPENAI_API_KEY)
        
        today = datetime.now().strftime("%d/%m/%Y")
        instructions = VALIDATION_INSTRUCTIONS.replace("{today}", today)
        
        # Preparar dados para validação
        data_str = json.dumps(data, ensure_ascii=False, indent=2)
        
        prompt = instructions + f"\n\n---\nDADOS EXTRAÍDOS:\n{data_str}"
        
        # Adicionar contexto do PDF se disponível (primeiros 5k chars)
        if texto:
            prompt += f"\n\n---\nCONTEXTO DO PDF (primeiros 5000 chars):\n{texto[:5000]}"
        
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "Você é um validador de dados jurídicos. Retorne APENAS JSON válido."},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
        )
        
        content = resp.choices[0].message.content
        if not content:
            return {"is_valid": True, "issues": [], "corrections": {}}
        
        content = content.strip()
        
        # Extrair JSON do response
        m = re.search(r'\{.*\}', content, flags=re.S)
        if m:
            content = m.group(0)
        
        result = json.loads(content)
        
        if isinstance(result, dict):
            is_valid = result.get("is_valid", True)
            issues = result.get("issues", [])
            corrections = result.get("corrections", {})
            
            if issues:
                log_info(f"Validação encontrou {len(issues)} problemas", region="LLM_VALIDATION")
                for issue in issues[:3]:  # Log primeiros 3
                    log_info(f"  - {issue.get('field', '?')}: {issue.get('issue', '?')}", region="LLM_VALIDATION")
            else:
                log_info("Validação OK - dados consistentes", region="LLM_VALIDATION")
            
            return {
                "is_valid": is_valid,
                "issues": issues if isinstance(issues, list) else [],
                "corrections": corrections if isinstance(corrections, dict) else {}
            }
        
        return {"is_valid": True, "issues": [], "corrections": {}}
        
    except Exception as e:
        log_error(f"Erro na validação LLM: {e}", exc=e, region="LLM_VALIDATION")
        return {"is_valid": True, "issues": [], "corrections": {}}


# Schema para classificação de documento
CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "tipo_documento": {
            "type": "string",
            "enum": [
                "peticao_inicial",
                "contestacao",
                "sentenca",
                "acordao",
                "despacho",
                "decisao_interlocutoria",
                "mandado_citacao",
                "notificacao",
                "trct",
                "ctps",
                "outro"
            ]
        },
        "area_direito": {
            "type": "string",
            "enum": ["trabalhista", "civel", "criminal", "tributario", "outro"]
        },
        "instancia": {
            "type": "string",
            "enum": ["1_grau", "2_grau", "superior", "outro"]
        },
        "confianca": {"type": "number"}  # 0.0 a 1.0
    }
}

CLASSIFICATION_INSTRUCTIONS = """Você é um classificador de documentos jurídicos brasileiros.
Analise o texto e classifique o tipo de documento.

TIPOS DE DOCUMENTO:
- peticao_inicial: Primeira peça do processo, contém qualificação das partes, causa de pedir, pedidos
- contestacao: Resposta do réu à petição inicial
- sentenca: Decisão final de 1º grau que resolve o mérito
- acordao: Decisão colegiada de tribunal (2º grau ou superior)
- despacho: Ato judicial de mero expediente
- decisao_interlocutoria: Decisão que não põe fim ao processo
- mandado_citacao: Documento para citar a parte ré
- notificacao: Comunicação oficial do juízo
- trct: Termo de Rescisão do Contrato de Trabalho
- ctps: Carteira de Trabalho e Previdência Social
- outro: Qualquer outro tipo

INDICADORES:
- Petição inicial: "DOS FATOS", "DOS PEDIDOS", "RECLAMANTE:", "em face de"
- Sentença: "DECIDO", "JULGO", "PROCEDENTE/IMPROCEDENTE", "DISPOSITIVO"
- Acórdão: "ACORDAM", "TURMA", "EMENTA"
- TRCT: "TERMO DE RESCISÃO", "AVISO PRÉVIO", "FGTS"

Retorne JSON no formato:
{
    "tipo_documento": "peticao_inicial",
    "area_direito": "trabalhista",
    "instancia": "1_grau",
    "confianca": 0.95
}
"""


def classify_document_with_llm(texto: str) -> dict:
    """
    Classifica o tipo de documento jurídico usando LLM.
    
    Returns:
        Dict com: tipo_documento, area_direito, instancia, confianca
    """
    log_info("Iniciando classificação LLM do documento", region="LLM_CLASSIFY")
    
    if not OPENAI_API_KEY:
        log_info("API key não disponível, retornando classificação padrão", region="LLM_CLASSIFY")
        return {
            "tipo_documento": "outro",
            "area_direito": "trabalhista",
            "instancia": "1_grau",
            "confianca": 0.0
        }
    
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        
        # Usar primeiros 15k chars para classificação
        prompt = CLASSIFICATION_INSTRUCTIONS + "\n\n---\nTEXTO DO DOCUMENTO:\n" + texto[:15000]
        
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "Você é um classificador de documentos jurídicos. Retorne APENAS JSON válido."},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
        )
        
        content = resp.choices[0].message.content
        if not content:
            return {
                "tipo_documento": "outro",
                "area_direito": "trabalhista",
                "instancia": "1_grau",
                "confianca": 0.0
            }
        
        content = content.strip()
        
        # Extrair JSON do response
        m = re.search(r'\{.*\}', content, flags=re.S)
        if m:
            content = m.group(0)
        
        result = json.loads(content)
        
        if isinstance(result, dict):
            tipo = result.get("tipo_documento", "outro")
            area = result.get("area_direito", "trabalhista")
            instancia = result.get("instancia", "1_grau")
            confianca = result.get("confianca", 0.5)
            
            log_info(f"Documento classificado: {tipo} ({area}, {instancia}) - confiança: {confianca:.0%}", region="LLM_CLASSIFY")
            
            return {
                "tipo_documento": tipo,
                "area_direito": area,
                "instancia": instancia,
                "confianca": float(confianca) if isinstance(confianca, (int, float)) else 0.5
            }
        
        return {
            "tipo_documento": "outro",
            "area_direito": "trabalhista",
            "instancia": "1_grau",
            "confianca": 0.0
        }
        
    except Exception as e:
        log_error(f"Erro na classificação LLM: {e}", exc=e, region="LLM_CLASSIFY")
        return {
            "tipo_documento": "outro",
            "area_direito": "trabalhista",
            "instancia": "1_grau",
            "confianca": 0.0
        }

