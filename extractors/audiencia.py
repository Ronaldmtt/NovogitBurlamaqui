import re
from typing import Optional

def parse_audiencia_inicial(texto: str) -> Optional[str]:
    """
    Extrai data/hora de AUDIÊNCIA INICIAL do texto.
    
    IMPORTANTE: Só retorna se encontrar contexto de "AUDIÊNCIA" próximo à data.
    Isso evita capturar erroneamente a data de distribuição do processo.
    
    2025-11-28: CORREÇÃO CRÍTICA - Limitar distância entre "audiência" e a data
    para evitar capturar timestamps de assinatura eletrônica.
    Máximo 200 caracteres entre "audiência" e a data.
    
    TAMBÉM: Verificar se o PDF menciona "não agendada" antes de retornar audiência.
    """
    t = texto or ""
    
    # ✅ VERIFICAÇÃO: "não agendada" - MOVIDA para o final, após tentar todos os padrões
    # 2025-11-28: Corrigido - o texto pode ter "Audiência inicial do processo não agendada automaticamente"
    # MAS também ter uma audiência real agendada. Priorizar a extração da audiência real.
    # A verificação será feita APENAS se nenhum padrão encontrar audiência.
    
    # Padrão 1: "Determino a audiência INICIAL TELEPRESENCIAL... : DD/MM/AAAA HH:MM"
    # 2025-11-28: Limitado a 200 chars entre audiência e data para evitar falsos positivos
    m = re.search(
        r'determino\s+a\s+audi[êe]ncia\s+.{0,100}?inicial.{0,100}?:\s*(\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2})',
        t,
        re.IGNORECASE | re.DOTALL
    )
    if m:
        return f"{m.group(1)} {m.group(2)}"
    
    # Padrão 2: "audiência INICIAL... : DD/MM/AAAA HH:MM" (sem "Determino")
    # 2025-11-28: CORRIGIDO - Limitar a 100 chars entre "audiência" e "inicial"
    # e 100 chars entre "inicial" e a data. Antes era .*? sem limite.
    m = re.search(
        r'audi[êe]ncia\s+.{0,100}?inicial.{0,100}?:\s*(\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2})',
        t,
        re.IGNORECASE | re.DOTALL
    )
    if m:
        return f"{m.group(1)} {m.group(2)}"
    
    # Padrão 3: "Determino a audiência INICIAL... DD/MM/AAAA às HH:MM" (com "às")
    m = re.search(
        r'determino\s+a\s+audi[êe]ncia\s+.{0,100}?inicial.{0,100}?(\d{2}/\d{2}/\d{4})\s+[àa]s\s+(\d{1,2}:\d{2})',
        t,
        re.IGNORECASE | re.DOTALL
    )
    if m:
        return f"{m.group(1)} {m.group(2)}"
    
    # 🆕 Padrão 3b: "AUDIÊNCIA UNA" - comum em varas que unificam audiência inicial e de instrução
    # 2025-11-28: Limitado a 100 chars
    m = re.search(
        r'audi[êe]ncia\s+una.{0,100}?(\d{2}/\d{2}/\d{4})\s+[àa]?s?\s*(\d{1,2}:\d{2})',
        t,
        re.IGNORECASE | re.DOTALL
    )
    if m:
        return f"{m.group(1)} {m.group(2)}"
    
    # 🆕 Padrão 3c: "audiência de conciliação e instrução"
    # 2025-11-28: Limitado a 100 chars
    m = re.search(
        r'audi[êe]ncia\s+de\s+concilia[çc][aã]o\s+e\s+instru[çc][aã]o.{0,100}?(\d{2}/\d{2}/\d{4})\s+[àa]?s?\s*(\d{1,2}:\d{2})',
        t,
        re.IGNORECASE | re.DOTALL
    )
    if m:
        return f"{m.group(1)} {m.group(2)}"
    
    # 🆕 Padrão 3d: "pauta de audiência" com data
    # 2025-11-28: Limitado a 100 chars
    m = re.search(
        r'pauta\s+de\s+audi[êe]ncia.{0,100}?(\d{2}/\d{2}/\d{4})\s+[àa]?s?\s*(\d{1,2}:\d{2})',
        t,
        re.IGNORECASE | re.DOTALL
    )
    if m:
        return f"{m.group(1)} {m.group(2)}"
    
    # Padrão 4: "AUDIÊNCIA... para DD/MM/AAAA HH:MM"
    m = re.search(
        r'audi[êe]ncia.{0,80}?\s+para\s+(\d{2}/\d{2}/\d{4}).{0,20}?(\d{1,2}:\d{2})',
        t,
        re.IGNORECASE
    )
    if m:
        return f"{m.group(1)} {m.group(2)}"
    
    # Padrão 5: "Audiência marcada para DD/MM/AAAA às HH:MM"
    m = re.search(
        r'audi[êe]ncia\s+marcada\s+para\s+(\d{2}/\d{2}/\d{4})\s+[àa]s\s+(\d{1,2}:\d{2})',
        t,
        re.IGNORECASE
    )
    if m:
        return f"{m.group(1)} {m.group(2)}"
    
    # 🆕 Padrão 5b: "Audiência designada para DD/MM/AAAA às HH:MM"
    m = re.search(
        r'audi[êe]ncia\s+designada\s+para\s+(\d{2}/\d{2}/\d{4})\s+[àa]s\s+(\d{1,2}:\d{2})',
        t,
        re.IGNORECASE
    )
    if m:
        return f"{m.group(1)} {m.group(2)}"
    
    # 🆕 Padrão 5c: "Audiência agendada para DD/MM/AAAA às HH:MM"
    m = re.search(
        r'audi[êe]ncia\s+agendada\s+para\s+(\d{2}/\d{2}/\d{4})\s+[àa]s\s+(\d{1,2}:\d{2})',
        t,
        re.IGNORECASE
    )
    if m:
        return f"{m.group(1)} {m.group(2)}"
    
    # 🆕 Padrão 5d: "Fica designada audiência para DD/MM/AAAA HH:MM"
    m = re.search(
        r'fica\s+designada\s+audi[êe]ncia\s+para\s+(\d{2}/\d{2}/\d{4})\s+[àa]?s?\s*(\d{1,2}:\d{2})',
        t,
        re.IGNORECASE
    )
    if m:
        return f"{m.group(1)} {m.group(2)}"
    
    # Padrão 6: "dia DD/MM/AAAA HH:MM horas" (comum em notificações de audiência)
    # Exemplo: "AUDIÊNCIA INICIAL... que se realizará no dia 09/12/2025 08:50 horas"
    # 2025-11-28: Limitado a 150 chars entre audiência e dia
    m = re.search(
        r'audi[êe]ncia.{0,150}?\bdia\s+(\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2})\s+horas',
        t,
        re.IGNORECASE | re.DOTALL
    )
    if m:
        return f"{m.group(1)} {m.group(2)}"
    
    # 🆕 Padrão 7: Formato com hífen na data "DD-MM-AAAA HH:MM"
    # 2025-11-28: Limitado a 100 chars
    m = re.search(
        r'audi[êe]ncia\s+.{0,100}?inicial.{0,100}?:\s*(\d{2}-\d{2}-\d{4})\s+(\d{1,2}:\d{2})',
        t,
        re.IGNORECASE | re.DOTALL
    )
    if m:
        data = m.group(1).replace('-', '/')
        return f"{data} {m.group(2)}"
    
    # 🆕 Padrão 8: "primeira audiência" como sinônimo de inicial
    # 2025-11-28: Limitado a 100 chars
    m = re.search(
        r'primeira\s+audi[êe]ncia.{0,100}?(\d{2}/\d{2}/\d{4})\s+[àa]?s?\s*(\d{1,2}:\d{2})',
        t,
        re.IGNORECASE | re.DOTALL
    )
    if m:
        return f"{m.group(1)} {m.group(2)}"
    
    # 🆕 Padrão 9: "UNA a ser realizada em... TELEPRESENCIAL DD/MM/AAAA HH:MM"
    # Batch 97: "UNA a ser realizada em , modalidade TELEPRESENCIAL. 27/01/2026 08:35"
    m = re.search(
        r'UNA\s+a\s+ser\s+realizada.{0,50}?(?:TELEPRESENCIAL|PRESENCIAL).?\s*(\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2})',
        t,
        re.IGNORECASE | re.DOTALL
    )
    if m:
        return f"{m.group(1)} {m.group(2)}"
    
    # 🆕 Padrão 10: "audiência que se realizará no dia: DD/MM/AAAA HH:MM horas"
    # Batch 97: "comparecer à audiência que se realizará no dia: 02/12/2025 14:10 horas"
    m = re.search(
        r'audi[êe]ncia\s+que\s+se\s+realizar[áa]\s+no\s+dia:?\s*(\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2})',
        t,
        re.IGNORECASE
    )
    if m:
        return f"{m.group(1)} {m.group(2)}"
    
    # 🆕 Padrão 11: "Designo audiência para , UNA telepresencial DD/MM/AAAA HH:MM"
    # Batch 97: "Designo audiência para , UNA telepresencial 25/02/2026 10:45"
    m = re.search(
        r'[Dd]esigno\s+audi[êe]ncia\s+para\s*,?\s*UNA.{0,30}?(\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2})',
        t,
        re.IGNORECASE | re.DOTALL
    )
    if m:
        return f"{m.group(1)} {m.group(2)}"
    
    # 🆕 Padrão 12: "pauta INICIAL PRESENCIAL DD/MM/AAAA"
    # Batch 97: "Processo incluído em pauta INICIAL PRESENCIAL 09/12/2025"
    # Nota: Este padrão geralmente não tem hora, usamos 09:00 como default
    m = re.search(
        r'pauta\s+(?:INICIAL|UNA)\s+(?:TELEPRESENCIAL|PRESENCIAL)\s+(\d{2}/\d{2}/\d{4})',
        t,
        re.IGNORECASE
    )
    if m:
        # Tentar encontrar hora nas proximidades
        hora_match = re.search(
            r'pauta.{0,80}?' + re.escape(m.group(1)) + r'\s+(\d{1,2}:\d{2})',
            t,
            re.IGNORECASE | re.DOTALL
        )
        if hora_match:
            return f"{m.group(1)} {hora_match.group(1)}"
        return f"{m.group(1)} 09:00"  # Hora default
    
    # 🆕 Padrão 13: "AUDIÊNCIA... instrução e julgamento... dia DD/MM/AAAA HH:MM"
    # Para audiências de instrução quando não há inicial
    m = re.search(
        r'audi[êe]ncia\s+de\s+instru[çc][aã]o.{0,80}?dia\s+(\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2})',
        t,
        re.IGNORECASE | re.DOTALL
    )
    if m:
        return f"{m.group(1)} {m.group(2)}"
    
    # 🆕 Padrão 14: Data antes de "horas" com contexto de audiência
    # "audiência... 02/12/2025 14:10 horas" (sem "dia")
    m = re.search(
        r'audi[êe]ncia.{0,100}?(\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2})\s+horas',
        t,
        re.IGNORECASE | re.DOTALL
    )
    if m:
        return f"{m.group(1)} {m.group(2)}"
    
    # 🆕 Padrão 15: Formato genérico "audiência... DD/MM/AAAA HH:MM" (fallback com limite)
    # Captura padrões não cobertos pelos anteriores
    m = re.search(
        r'audi[êe]ncia.{0,80}?(\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2})',
        t,
        re.IGNORECASE | re.DOTALL
    )
    if m:
        # Verificar se não é contexto inválido
        start = max(0, m.start() - 30)
        context = t[start:m.end()].lower()
        invalid = ['distribuição', 'autuação', 'assinado', 'publicação']
        if not any(inv in context for inv in invalid):
            return f"{m.group(1)} {m.group(2)}"
    
    # Se não encontrou NENHUM padrão específico de audiência, retorna None
    # Isso evita capturar datas de distribuição ou outras datas aleatórias
    return None
