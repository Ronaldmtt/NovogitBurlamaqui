"""
Configuração de prioridade de PARTES INTERESSADAS para casos onde múltiplas empresas
do mesmo grupo aparecem no polo reclamado.

IMPORTANTE: Este módulo trabalha com PARTES INTERESSADAS, não com clientes diferentes.

Exemplo:
  Cliente: CSN (único)
    └── Partes Interessadas: CBSI, CSN, CSN MINERAÇÃO, etc.
    
Quando um processo tem múltiplas partes do mesmo grupo (ex: CBSI + CSN MINERAÇÃO),
este módulo determina qual deve ser a parte principal (campo parte_interessada)
e qual deve ir para o campo outra_reclamada_cliente.

Regra: CBSI tem prioridade sobre outras partes do grupo CSN.
"""

from typing import List, Dict, Tuple, Optional

# Ordem de prioridade de PARTES INTERESSADAS (do maior para o menor)
# Esta lista define qual parte deve ser a principal quando múltiplas aparecem no processo
PARTE_PRIORITY_ORDER = [
    # === GRUPO CSN ===
    # Prioridade 1: CBSI (maior prioridade dentro do grupo CSN)
    "CBSI",
    
    # Prioridade 2: CSN Siderúrgica Nacional
    "CSN – COMPANHIA SIDERURGICA NACIONAL",
    "CSN",
    
    # Prioridade 3: Outras empresas do grupo CSN
    "CSN CIMENTOS BRASIL S/A",
    "CSN MINERAÇÃO S.A.",
    "COMPANHIA METALURGICA PRADA",
    "SEPETIBA TECON S.A.",
    "FUNDAÇÃO CSN",
    
    # === OUTROS GRUPOS (mantidos para compatibilidade futura) ===
    "Casas Bahia",
    "Profarma",
    "HAZTEC",
    "Grupo Pão de Açúcar",
    "CNS",
    "GRUPO KPFR",
    "Grupo EBX",
]


def get_parte_priority(parte_nome: str) -> int:
    """
    Retorna o índice de prioridade de uma parte interessada (menor = maior prioridade).
    Usa fuzzy matching para encontrar a melhor correspondência.
    
    Args:
        parte_nome: Nome da parte interessada (ex: "CBSI LTDA", "CSN S.A.")
    
    Returns:
        Índice de prioridade (0 = maior prioridade) ou 999 se não encontrado
    """
    from rapidfuzz import fuzz
    
    parte_upper = parte_nome.upper()
    
    # Tenta matching exato primeiro
    for idx, parte_ref in enumerate(PARTE_PRIORITY_ORDER):
        if parte_ref.upper() in parte_upper or parte_upper in parte_ref.upper():
            return idx
    
    # Fuzzy matching como fallback
    melhor_score = 0
    melhor_idx = 999
    
    for idx, parte_ref in enumerate(PARTE_PRIORITY_ORDER):
        score = fuzz.ratio(parte_upper, parte_ref.upper())
        if score > melhor_score and score >= 85:  # threshold de 85%
            melhor_score = score
            melhor_idx = idx
    
    return melhor_idx


def sort_partes_by_priority(partes: List[str]) -> List[str]:
    """
    Ordena uma lista de partes interessadas por prioridade (maior prioridade primeiro).
    
    Args:
        partes: Lista de nomes de partes interessadas
    
    Returns:
        Lista ordenada por prioridade
    
    Example:
        >>> sort_partes_by_priority(["CSN S.A.", "CBSI LTDA", "CSN MINERAÇÃO"])
        ["CBSI LTDA", "CSN S.A.", "CSN MINERAÇÃO"]
    """
    return sorted(partes, key=get_parte_priority)


def assign_primary_secondary_partes(
    partes_encontradas: List[str]
) -> Tuple[Optional[str], Optional[str]]:
    """
    Atribui parte interessada principal e secundária baseado na prioridade.
    
    IMPORTANTE: Esta função trabalha com NOMES DE PARTES INTERESSADAS, não com clientes.
    
    Args:
        partes_encontradas: Lista de nomes de partes interessadas extraídas do PDF
                           Ex: ["CBSI LTDA", "CSN MINERAÇÃO S.A."]
    
    Returns:
        Tupla (parte_principal, parte_secundaria)
        
    Example:
        >>> partes = ["CSN MINERAÇÃO S.A.", "CBSI LTDA"]
        >>> assign_primary_secondary_partes(partes)
        ("CBSI LTDA", "CSN MINERAÇÃO S.A.")
    """
    if not partes_encontradas:
        return (None, None)
    
    # Remove duplicatas mantendo ordem
    partes_unicas = list(dict.fromkeys(partes_encontradas))
    
    if not partes_unicas:
        return (None, None)
    
    # Ordena por prioridade
    partes_ordenadas = sort_partes_by_priority(partes_unicas)
    
    # Primeira é a principal, segunda (se existir) é a secundária
    parte_principal = partes_ordenadas[0] if len(partes_ordenadas) >= 1 else None
    parte_secundaria = partes_ordenadas[1] if len(partes_ordenadas) >= 2 else None
    
    return (parte_principal, parte_secundaria)


# Mantém função legada para compatibilidade (deprecated)
def assign_primary_secondary_clients(
    clientes_encontrados: List[Dict[str, str]]
) -> Tuple[Optional[str], Optional[str]]:
    """
    DEPRECATED: Use assign_primary_secondary_partes() ao invés desta função.
    
    Mantido apenas para compatibilidade com código existente.
    """
    if not clientes_encontrados:
        return (None, None)
    
    # Extrai nomes das partes
    nomes_partes = [c.get("parte", "") for c in clientes_encontrados if c.get("parte")]
    
    return assign_primary_secondary_partes(nomes_partes)


# Testes rápidos
if __name__ == "__main__":
    print("=" * 70)
    print("TESTES DE PRIORIDADE DE PARTES INTERESSADAS (GRUPO CSN)")
    print("=" * 70)
    print()
    
    # Teste 1: Caso CBSI + CSN (cenário real do usuário)
    print("Teste 1: CBSI + CSN (ambas no mesmo processo)")
    partes = [
        "CSN COMPANHIA SIDERURGICA NACIONAL S.A.",
        "CBSI COMPANHIA BRASILEIRA DE SERVIÇOS E INFRAESTRUTURA LTDA"
    ]
    principal, secundaria = assign_primary_secondary_partes(partes)
    print(f"  Partes encontradas: {partes}")
    print(f"  ✅ Principal: {principal}")
    print(f"  ✅ Secundária: {secundaria}")
    print(f"  Esperado: CBSI (principal) e CSN (secundária)")
    # CBSI deve ter prioridade sobre CSN
    assert "CBSI" in (principal or ""), "CBSI deveria ser principal"
    assert "CSN" in (secundaria or "") and "CBSI" not in (secundaria or ""), "CSN deveria ser secundária"
    print("  ✅ PASSOU\n")
    
    # Teste 2: Caso CSN + CSN MINERAÇÃO
    print("Teste 2: CSN + CSN MINERAÇÃO")
    partes = [
        "CSN MINERAÇÃO S.A.",
        "CSN – COMPANHIA SIDERURGICA NACIONAL"
    ]
    principal, secundaria = assign_primary_secondary_partes(partes)
    print(f"  Partes encontradas: {partes}")
    print(f"  ✅ Principal: {principal}")
    print(f"  ✅ Secundária: {secundaria}")
    print(f"  Esperado: CSN SIDERURGICA (principal) e CSN MINERAÇÃO (secundária)")
    assert "SIDERURGICA" in (principal or "").upper(), "CSN Siderúrgica deveria ser principal"
    assert "MINERAÇÃO" in (secundaria or "").upper(), "CSN Mineração deveria ser secundária"
    print("  ✅ PASSOU\n")
    
    # Teste 3: Apenas CBSI (sem outras partes)
    print("Teste 3: Apenas CBSI (sem outras partes)")
    partes = ["CBSI COMPANHIA BRASILEIRA DE SERVIÇOS E INFRAESTRUTURA"]
    principal, secundaria = assign_primary_secondary_partes(partes)
    print(f"  Partes encontradas: {partes}")
    print(f"  ✅ Principal: {principal}")
    print(f"  ✅ Secundária: {secundaria}")
    print(f"  Esperado: CBSI (principal) e None (secundária)")
    assert "CBSI" in (principal or ""), "CBSI deveria ser principal"
    assert secundaria is None, "Não deveria haver parte secundária"
    print("  ✅ PASSOU\n")
    
    # Teste 4: Teste de ordenação múltipla
    print("Teste 4: Ordenação de 3+ partes do grupo CSN")
    partes_desordenadas = [
        "CSN CIMENTOS BRASIL S/A",
        "CBSI LTDA",
        "CSN – COMPANHIA SIDERURGICA NACIONAL",
        "CSN MINERAÇÃO S.A."
    ]
    ordenadas = sort_partes_by_priority(partes_desordenadas)
    print(f"  Original: {[p[:30] + '...' for p in partes_desordenadas]}")
    print(f"  Ordenado: {[p[:30] + '...' for p in ordenadas]}")
    print(f"  Esperado: CBSI primeiro, depois CSN Siderúrgica, depois outras")
    assert "CBSI" in ordenadas[0], "CBSI deve estar primeiro"
    assert "SIDERURGICA" in ordenadas[1].upper(), "CSN Siderúrgica deve estar segundo"
    print("  ✅ PASSOU\n")
    
    print("=" * 70)
    print("TODOS OS TESTES PASSARAM! ✅")
    print("=" * 70)
