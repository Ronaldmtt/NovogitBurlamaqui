# -*- coding: utf-8 -*-
# extractors/brand_map.py
import unicodedata
import re
import json
import os
from typing import Optional, Dict, List
from rapidfuzz import fuzz, process

# Cache do banco de dados de clientes
_CLIENTES_DB: Optional[Dict] = None

def _load_clientes_database() -> Dict:
    """Carrega o banco de dados de clientes do JSON."""
    global _CLIENTES_DB
    
    if _CLIENTES_DB is not None:
        return _CLIENTES_DB
    
    # Caminho para o JSON
    json_path = os.path.join(os.path.dirname(__file__), "..", "data", "clientes_database.json")
    
    if not os.path.exists(json_path):
        # Fallback: retorna estrutura vazia se JSON não existir
        _CLIENTES_DB = {"clientes": {}, "partes_interessadas": []}
        return _CLIENTES_DB
    
    with open(json_path, "r", encoding="utf-8") as f:
        loaded_data = json.load(f)
        # Garante que sempre retornamos um Dict válido
        _CLIENTES_DB = loaded_data if isinstance(loaded_data, dict) else {"clientes": {}, "partes_interessadas": []}
    
    return _CLIENTES_DB

def _norm(s: str) -> str:
    """Uppercase + remove acentos + colapsa espaços."""
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.upper()
    s = re.sub(r"\s+", " ", s).strip()
    return s

def find_cliente_by_parte_interessada(nome_parte: str, threshold: int = 85) -> Optional[str]:
    """
    Busca o cliente correspondente a uma parte interessada usando fuzzy matching.
    
    Args:
        nome_parte: Nome da parte interessada (empresa) extraída do PDF
        threshold: Score mínimo de similaridade (0-100). Default: 85
        
    Returns:
        Nome do cliente encontrado ou None
        
    Examples:
        find_cliente_by_parte_interessada("PROFARMA DISTRIBUIDORA DE PRODUTOS FARMACEUTICOS SA")
        → "Profarma"
        
        find_cliente_by_parte_interessada("ORIZON MEIO AMBIENTE S.A.")
        → "HAZTEC"
    """
    if not nome_parte:
        return None
    
    db = _load_clientes_database()
    partes = db.get("partes_interessadas", [])
    
    if not partes:
        return None
    
    # Normaliza o nome da parte para busca
    nome_norm = _norm(nome_parte)
    
    # ✅ BUSCA EXATA PRIMEIRO (mais rápida)
    for item in partes:
        if _norm(item["nome"]) == nome_norm:
            return item["cliente"]
    
    # ✅ FUZZY MATCHING: Encontra a melhor correspondência
    # 🔧 FIX: Normaliza AMBOS os lados antes do fuzzy matching para ignorar acentos
    nomes_norm_map = {_norm(item["nome"]): item for item in partes}
    nomes_norm = list(nomes_norm_map.keys())
    
    # Usa token_set_ratio para lidar com variações de ordem e abreviações
    result = process.extractOne(
        nome_norm,
        nomes_norm,
        scorer=fuzz.token_set_ratio,
        score_cutoff=threshold
    )
    
    if result:
        nome_encontrado_norm, score, idx = result
        # Encontra o cliente correspondente usando o nome normalizado
        item = nomes_norm_map.get(nome_encontrado_norm)
        if item:
            return item["cliente"]
    
    return None

def detect_grupo(nome_ou_texto: str) -> Optional[str]:
    """
    Detecta se o texto contém qualquer sinônimo do GPA.
    Retorna 'Grupo Pão de Açúcar' se detectado, None caso contrário.
    
    IMPORTANTE: Esta função é mantida por compatibilidade retroativa.
    Use normalize_cliente() para normalizar qualquer cliente.
    """
    # Usa o novo sistema baseado em JSON
    cliente = find_cliente_by_parte_interessada(nome_ou_texto, threshold=80)
    if cliente and "Pão de Açúcar" in cliente:
        return "Grupo Pão de Açúcar"
    
    # Fallback: busca hardcoded para GPA
    _GPA_ALIASES = [
        "GRUPO PÃO DE AÇÚCAR", "GRUPO PAO DE ACUCAR",
        "PÃO DE AÇÚCAR", "PAO DE ACUCAR",
        "GPA", "CBD", "SENDAS", "SENDAS DISTRIBUIDORA",
        "COMPANHIA BRASILEIRA DE DISTRIBUICAO",
        "COMPANHIA BRASILEIRA DE DISTRIBUIÇÃO",
        "EXTRA", "ASSAÍ", "ASSAI",
    ]
    
    t = _norm(nome_ou_texto)
    if not t:
        return None
    
    for alias in _GPA_ALIASES:
        if _norm(alias) in t:
            return "Grupo Pão de Açúcar"
    
    return None

def normalize_cliente(nome_cliente: str) -> str:
    """
    Normaliza o nome do cliente usando o banco de dados JSON com fuzzy matching.
    
    ✅ NOVO: Usa clientes_database.json para reconhecer QUALQUER cliente cadastrado,
    não apenas aliases hardcoded.
    
    Args:
        nome_cliente: Nome do cliente/reclamado extraído do PDF
        
    Returns:
        Nome canônico do cliente do banco de dados ou o nome original se não encontrado
        
    Examples:
        normalize_cliente("PROFARMA DISTRIBUIDORA DE PRODUTOS FARMACEUTICOS SA")
        → "Profarma"
        
        normalize_cliente("ORIZON MEIO AMBIENTE S.A.")
        → "HAZTEC"
        
        normalize_cliente("CBSI COMPANHIA BRASILEIRA")
        → "CSN"
    """
    if not nome_cliente:
        return nome_cliente
    
    # ✅ BUSCA NO BANCO DE DADOS JSON com threshold reduzido (80) para melhor recall
    # 🔧 FIX: Agora ignora acentos no fuzzy matching (DISTRIBUICAO = DISTRIBUIÇÃO)
    cliente_encontrado = find_cliente_by_parte_interessada(nome_cliente, threshold=80)
    
    if cliente_encontrado:
        return cliente_encontrado
    
    # ✅ FALLBACK: Retorna o nome original capitalizado
    return nome_cliente.strip().title() if nome_cliente else nome_cliente

def get_all_clientes() -> List[str]:
    """
    Retorna lista de todos os clientes cadastrados no banco de dados.
    
    Returns:
        Lista com nomes de todos os clientes
    """
    db = _load_clientes_database()
    return list(db.get("clientes", {}).keys())

def get_partes_by_cliente(nome_cliente: str) -> List[str]:
    """
    Retorna todas as partes interessadas de um cliente específico.
    
    Args:
        nome_cliente: Nome do cliente
        
    Returns:
        Lista de partes interessadas do cliente
    """
    db = _load_clientes_database()
    clientes = db.get("clientes", {})
    
    if nome_cliente in clientes:
        return clientes[nome_cliente].get("partes", [])
    
    return []
