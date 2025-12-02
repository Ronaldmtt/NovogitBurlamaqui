# pdf_rag_service.py

import os
import re
import json
import time
import logging
import threading
from io import BytesIO
from typing import List, Dict, Any, Optional
from pathlib import Path

import PyPDF2
import tiktoken
from openai import OpenAI

from app import db
from models import Process, PDFChunk, ProcessAnalysis
from extractors import postprocess_extracted_fields
from extractors.regex_utils import extract_audiencia_inicial  # Fallback robusto para data/hora de audiência
from utils.gap_filler import gap_fill

# =========================
# Logging
# =========================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PDFRAGService:
    def __init__(self):
        self.openai_client = OpenAI(
            api_key=os.environ.get("OPENAI_API_KEY"),
            timeout=120.0
        )
        self.tokenizer = tiktoken.get_encoding("cl100k_base")
        self.max_chunk_tokens = 1000
        self.overlap_tokens = 100

    # =========================
    # Util
    # =========================
    def _finalize_result(self, full_text: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Ajustes finais e fallbacks independentes do LLM:
          - Preenche audiencia_inicial via regex quando estiver vazia
          - Garante que certos campos não fiquem None (usa "")
        """
        if not isinstance(data, dict):
            data = {}

        # Audiência: tentar inferir do PDF se vier vazio do LLM
        aud = (data.get("audiencia_inicial") or "").strip()
        if not aud:
            try:
                inferred = extract_audiencia_inicial(full_text or "")
            except Exception:
                inferred = None
            if inferred:
                data["audiencia_inicial"] = inferred
                data["cadastrar_primeira_audiencia"] = True  # ✅ Marcar flag quando audiência for detectada
                logger.info("Audiência inferida do PDF (fallback regex): %s", inferred)
            else:
                data["audiencia_inicial"] = ""

        # Nunca mandar None para esses campos
        sanitize_keys = [
            "celula",
            "numero_processo_antigo",
            "npc",
            "cliente_parte",
            "advogado_autor",
            "advogado_reu",
            "prazo",
            "prazos_derivados_audiencia",
            "resultado_audiencia",
            "decisao_tipo",
            "decisao_resultado",
            "decisao_fundamentacao_resumida",
            "id_interno_hilo",
            "data_hora_cadastro_manual",
            "tipo_notificacao",
            "estrategia", "indice_atualizacao",
            "posicao_parte_interessada", "parte_interessada",
            "parte_adversa_tipo", "parte_adversa_nome", "escritorio_parte_adversa",
            "uf_oab_advogado_adverso", "cpf_cnpj_parte_adversa", "telefone_parte_adversa",
            "email_parte_adversa", "endereco_parte_adversa",
            "data_distribuicao", "data_citacao", "risco", "valor_causa", "rito", "observacao",
        ]
        # normaliza cadastrar_primeira_audiencia para bool
        cpa = data.get("cadastrar_primeira_audiencia")
        if isinstance(cpa, bool):
            # Já é boolean, manter o valor
            data["cadastrar_primeira_audiencia"] = cpa
        elif isinstance(cpa, str):
            # É string, normalizar para bool
            data["cadastrar_primeira_audiencia"] = True if cpa.strip().lower() in ("sim", "true", "1") else False
        else:
            # Qualquer outro tipo (None, int, etc), converter para bool
            data["cadastrar_primeira_audiencia"] = bool(cpa)

        for k in sanitize_keys:
            if data.get(k) is None:
                data[k] = ""

        return data

    @staticmethod
    def _to_rpa_payload(data: Dict[str, Any]) -> Dict[str, Any]:
        def _b(v) -> bool:
            s = str(v or "").strip().lower()
            return s in {"sim", "true", "1", "yes"}

        return {
            "cnj_sim": _b(data.get("cnj", True)),
            "numero_processo": (data.get("numero_processo") or "").strip(),
            "tipo_processo": (data.get("tipo_processo") or "Eletrônico").strip() or "Eletrônico",
            "sistema_eletronico": (data.get("sistema_eletronico") or "PJE").strip() or "PJE",
            "area_direito": (data.get("area_direito") or "").strip(),
            "sub_area_direito": (data.get("sub_area_direito") or "").strip(),
            "origem": (data.get("origem") or "").strip().upper(),
            "numero_orgao": (data.get("numero_orgao") or "").strip(),
            "comarca": (data.get("comarca") or "").strip(),
            "estado": (data.get("estado") or "").strip(),
            "assunto": (data.get("assunto") or "").strip(),
            "objeto": (data.get("objeto") or "").strip(),
            "celula": (data.get("celula") or "").strip(),
            "numero_processo_antigo": (data.get("numero_processo_antigo") or "").strip(),
            "npc": (data.get("npc") or "").strip(),
        }

    @staticmethod
    def _salvar_ultimo_processo(payload: Dict[str, Any]) -> None:
        Path("instance").mkdir(exist_ok=True)
        Path("instance/rpa_current.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        logger.info("[RPA][SAVE] instance/rpa_current.json atualizado")

    # =========================
    # PDF
    # =========================
    def extract_text_from_pdf(self, pdf_file_path: str) -> str:
        """Extrai texto de um PDF inteiro."""
        try:
            with open(pdf_file_path, "rb") as file:
                reader = PyPDF2.PdfReader(file)
                text = ""
                for page_num, page in enumerate(reader.pages):
                    try:
                        page_text = page.extract_text()
                        text += f"\n--- Página {page_num + 1} ---\n{page_text}\n"
                    except Exception as e:
                        logger.warning("Erro ao extrair texto da página %d: %s", page_num + 1, e)
                        continue
                return text.strip()
        except Exception as e:
            logger.error("Erro ao extrair texto do PDF: %s", e)
            raise Exception(f"Falha ao processar PDF: {str(e)}")

    def count_tokens(self, text: str) -> int:
        """Conta tokens de um texto (estimativa via tiktoken)."""
        return len(self.tokenizer.encode(text or ""))

    def create_smart_chunks(self, text: str) -> List[Dict[str, Any]]:
        """
        Cria chunks com sobreposição leve com base em sentenças.
        """
        chunks: List[Dict[str, Any]] = []
        sentences = (text or "").split(". ")
        current_chunk = ""
        chunk_index = 0

        for sentence in sentences:
            sentence = sentence.strip() + ". "
            test_chunk = current_chunk + sentence
            if self.count_tokens(test_chunk) > self.max_chunk_tokens:
                if current_chunk:
                    overlap_text = ""
                    if chunks:
                        prev_sentences = chunks[-1]["content"].split(". ")[-2:]
                        overlap_text = ". ".join(s for s in prev_sentences if s) + ". "
                    final_chunk = (overlap_text + current_chunk).strip()
                    chunks.append(
                        {
                            "index": chunk_index,
                            "content": final_chunk,
                            "token_count": self.count_tokens(final_chunk),
                        }
                    )
                    chunk_index += 1
                    current_chunk = sentence
                else:
                    one = sentence.strip()
                    chunks.append(
                        {
                            "index": chunk_index,
                            "content": one,
                            "token_count": self.count_tokens(one),
                        }
                    )
                    chunk_index += 1
            else:
                current_chunk = test_chunk

        if current_chunk:
            overlap_text = ""
            if chunks:
                prev_sentences = chunks[-1]["content"].split(". ")[-2:]
                overlap_text = ". ".join(s for s in prev_sentences if s) + ". "
            final_chunk = (overlap_text + current_chunk).strip()
            chunks.append(
                {
                    "index": chunk_index,
                    "content": final_chunk,
                    "token_count": self.count_tokens(final_chunk),
                }
            )

        return chunks

    # =========================
    # OpenAI helpers
    # =========================
    def generate_embedding(self, text: str) -> Optional[List[float]]:
        """Gera embedding com OpenAI."""
        try:
            resp = self.openai_client.embeddings.create(
                model="text-embedding-3-small",
                input=text
            )
            return resp.data[0].embedding
        except Exception as e:
            logger.error("Erro ao gerar embedding: %s", e)
            return None

    def analyze_chunk_with_ai(self, chunk_content: str) -> Dict[str, Any]:
        """Resumo/entidades/conceitos do chunk."""
        try:
            prompt = (
                "Analise o seguinte trecho de um documento jurídico e extraia:\n"
                "1. Resumo em 2-3 frases\n"
                "2. Entidades principais (nomes, datas, valores, leis)\n"
                "3. Conceitos jurídicos mencionados\n\n"
                "Responda em formato JSON:\n"
                "{\n"
                '  "summary": "resumo aqui",\n'
                '  "entities": ["entidade1", "entidade2"],\n'
                '  "legal_concepts": ["conceito1", "conceito2"]\n'
                "}\n\n"
                f"Texto: {chunk_content[:1500]}"
            )
            response = self.openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.3,
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.error("Erro na análise AI do chunk: %s", e)
            return {"summary": "Análise não disponível", "entities": [], "legal_concepts": []}

    # =========================================================
    # Baseline determinístico (regex) para campos críticos
    # =========================================================
    def _norm(self, txt: str) -> str:
        return re.sub(r"\s+", " ", txt or "").strip()

    def _rx_valor_causa(self, text: str) -> Optional[str]:
        # "Valor da causa: R$ 117.932,02" (variações com pontuação)
        m = re.search(r"Valor\s+da\s+causa\s*[:\-]?\s*R?\$?\s*([\d\.\,]+)", text, re.IGNORECASE)
        return m.group(1) if m else None

    def _rx_rito(self, text: str) -> Optional[str]:
        # "Ação Trabalhista – Rito Ordinário", "Rito Sumaríssimo", "Rito Sumário"
        # IMPORTANTE: Verificar primeiro SUMARÍSSIMO (mais específico) antes de SUMÁRIO e ORDINÁRIO
        # Procurar no cabeçalho (primeiras 3000 chars) para evitar falsos positivos
        cabecalho = text[:3000]
        
        if re.search(r"Rito\s+Sumarí[sS]+imo|Sumarí[sS]+imo|SUMARÍSSIMO|procedimento\s+sumarí[sS]+imo", cabecalho, re.IGNORECASE):
            return "Sumaríssimo"
        if re.search(r"Rito\s+Ordin[aá]rio|Ordin[aá]rio|ORDINÁRIO|Ação\s+Trabalhista\s+[-–]\s+Rito\s+Ordin[aá]rio", cabecalho, re.IGNORECASE):
            return "Ordinário"
        if re.search(r"Rito\s+Sum[aá]rio(?!\s*[sí])|Sum[aá]rio(?!\s*[sí])|SUMÁRIO(?!SSI)", cabecalho, re.IGNORECASE):
            return "Sumário"
        return None

    def _rx_partes(self, text: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        # RECLAMANTE / RECLAMADO (ou Autor/Réu)
        # Melhorar para capturar nomes completos
        m_rec = re.search(r"RECLAMANTE\s*[:\-]\s*([A-ZÀ-Ý][A-ZÀ-Ý\s\'\-\.]+?)(?=\s*(?:ADVOGADO|CPF|RG|\n|,|$))", text, re.IGNORECASE)
        if m_rec:
            out["parte_interessada"] = self._norm(m_rec.group(1).title())
        
        m_reu = re.search(r"RECLAMAD[OA]\s*[:\-]\s*([A-ZÀ-Ý0-9][A-ZÀ-Ý0-9\s\-\.&/()]+?)(?=\s*(?:CNPJ|CPF|PAGINA|\n|,|inscrit|pessoa\s+jur[ií]dica|$))", text, re.IGNORECASE)
        if m_reu:
            out["parte_adversa_nome"] = self._norm(m_reu.group(1).title())

        # Tipo da parte adversa: heurística por CNPJ/CPF
        if re.search(r"\bCNPJ\b", text, re.IGNORECASE):
            out["parte_adversa_tipo"] = "JURIDICA"
        elif re.search(r"\bCPF\b", text, re.IGNORECASE):
            out["parte_adversa_tipo"] = "FISICA"
        return out

    def _rx_cnpj_reu(self, text: str) -> Optional[str]:
        m = re.search(r"CNPJ\s*(?:n?[ºo]?)?\s*[:\-]?\s*([\d]{2}\.?\d{3}\.?\d{3}/?\d{4}\-?\d{2})", text, re.IGNORECASE)
        return m.group(1) if m else None

    def _rx_contatos_reu(self, text: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        m_email = re.search(r"[\w\.\-\+]+@[\w\.\-]+\.\w+", text)
        if m_email:
            out["email_parte_adversa"] = m_email.group(0)
        tels = re.findall(r"\(?\d{2}\)?\s?\d{4,5}\-?\d{4}", text)
        if tels:
            # remove duplicatas mantendo ordem
            out["telefone_parte_adversa"] = ", ".join(list(dict.fromkeys(tels)))
        return out

    def _rx_comarca(self, text: str) -> Optional[str]:
        """
        Extrai comarca do cabeçalho do processo judicial (NÃO do endereço das partes).
        Prioridade:
        1. "Comarca de X"
        2. "VARA DO TRABALHO DE/DO X" (incluindo "Rio de Janeiro", "São Paulo", etc.)
        3. "Fórum Trabalhista de X"
        """
        # Primeiras 5000 caracteres (cobre página 1 e início da página 2)
        cabecalho = text[:5000]
        # Normalizar quebras de linha para facilitar regex
        cabecalho_norm = re.sub(r'\s+', ' ', cabecalho)
        
        # Padrão 1: "Comarca de X"
        m = re.search(r'Comarca\s+de\s+((?:[A-ZÀ-Ú][a-zà-ú]+\s*){1,5})(?=\s*[-–,]|$)', cabecalho_norm, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        
        # Padrão 2: "X VARA DO TRABALHO DO RIO DE JANEIRO" (padrão mais específico para nomes compostos)
        m = re.search(r'VARA\s+DO\s+TRABALHO\s+D[OE]\s+RIO\s+DE\s+JANEIRO', cabecalho_norm, re.IGNORECASE)
        if m:
            return "Rio de Janeiro"
        
        m = re.search(r'VARA\s+DO\s+TRABALHO\s+D[OE]\s+SÃO\s+PAULO', cabecalho_norm, re.IGNORECASE)
        if m:
            return "São Paulo"
        
        m = re.search(r'VARA\s+DO\s+TRABALHO\s+D[OE]\s+SÃO\s+GONÇALO', cabecalho_norm, re.IGNORECASE)
        if m:
            return "São Gonçalo"
        
        # Padrão 3: "Fórum Trabalhista de X" (ANTES do genérico para não capturar "Fórum")
        m = re.search(r'F[OÓ]RUM\s+TRABALHISTA\s+DE\s+([A-ZÀ-ÚÇa-zà-úç]+)', cabecalho_norm, re.IGNORECASE)
        if m:
            comarca = m.group(1).strip().title()
            # Remover sufixos como " - RJ" que podem vir grudados
            comarca = re.sub(r'\s*[-–]\s*[A-Z]{2}\s*$', '', comarca)
            return comarca
        
        # Padrão 4: "VARA DO TRABALHO DE X" (genérico, para cidades de nome simples)
        m = re.search(r'VARA\s+DO\s+TRABALHO\s+D[OE]\s+([A-ZÇÃÂÉÍÓÚ]+)(?=\s|$)', cabecalho_norm, re.IGNORECASE)
        if m:
            cidade = m.group(1).strip().title()
            # Filtrar estados E a palavra "Fórum" que pode aparecer
            if cidade not in ['Rj', 'Sp', 'Mg', 'Rs', 'Ba', 'Pr', 'Fórum', 'Forum']:
                return cidade
        
        return None

    def _rx_endereco_reu(self, text: str) -> Optional[str]:
        # Heurística baseada em "estabelecida na Av.... – São Paulo/SP – CEP 00000-000"
        m = re.search(
            r"estabelecid[ao]\s+na\s+(.+?)\s*[–\-]\s*([A-Za-zÀ-ÿ ]+\/[A-Z]{2})\s*[–\-]\s*CEP[:\s]*([\d\-\.]{8,9})",
            text, re.IGNORECASE
        )
        if m:
            return f"{self._norm(m.group(1))} – {self._norm(m.group(2))} – CEP {m.group(3)}"
        # fallback mais permissivo
        m2 = re.search(r"(Av\.|Avenida|Rua|Al\.|Alameda)\s+.+?CEP[:\s]*([\d\-\.]{8,9})", text, re.IGNORECASE)
        if m2:
            trecho = text[m2.start(): m2.end()+100]
            linha = " ".join(trecho.splitlines())
            return self._norm(linha)
        return None

    def _rx_data_distribuicao(self, text: str) -> Optional[str]:
        # "Distribuído em 25/09/2025 12:16:24" ou apenas a data
        m = re.search(r"Distribuíd[oa]?\s+em\s+(\d{2}/\d{2}/\d{4})(?:\s+(\d{2}:\d{2}:\d{2}))?", text, re.IGNORECASE)
        if m:
            return f"{m.group(1)} {m.group(2)}".strip() if m.group(2) else m.group(1)
        return None

    def _rx_audiencia(self, text: str) -> Optional[str]:
        """
        Detecta audiência inicial em múltiplos formatos:
        1. "audiência INICIAL... : DD/MM/AAAA HH:MM"
        2. "audiência... para DD/MM/AAAA HH:MM"
        3. "Determino a audiência INICIAL... DD/MM/AAAA HH:MM"
        """
        # Formato 1: "audiência INICIAL TELEPRESENCIAL... : 12/12/2025 09:10"
        m = re.search(r"audiênc[ia]+\s+INICIAL[^:]*:\s*(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})", text, re.IGNORECASE)
        if m:
            return f"{m.group(1)} {m.group(2)}"
        
        # Formato 2: "Determino a audiência INICIAL... 12/12/2025 09:10"
        m = re.search(r"audiênc[ia]+\s+INICIAL[^\.]*?(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})", text, re.IGNORECASE)
        if m:
            return f"{m.group(1)} {m.group(2)}"
        
        # Formato 3 (fallback): "audiência... para 25/02/2026 10:45"
        m = re.search(r"audiênc[ia]?\s.*?\spara\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})", text, re.IGNORECASE)
        if m:
            return f"{m.group(1)} {m.group(2)}"
        
        return None

    def _baseline_regex_fill(self, full_text: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Completa (sem sobrescrever valores já preenchidos) os campos críticos via regex.
        Não interfere em Estratégia/Risco/Índice (regras internas).
        """
        if not isinstance(data, dict):
            data = {}

        text = self._norm(full_text)

        # Partes
        partes = self._rx_partes(text)
        for k, v in partes.items():
            if v and not (data.get(k) or "").strip():
                data[k] = v

        # Documento/contatos parte adversa
        cnpj = self._rx_cnpj_reu(text)
        if cnpj and not (data.get("cpf_cnpj_parte_adversa") or "").strip():
            data["cpf_cnpj_parte_adversa"] = cnpj

        contatos = self._rx_contatos_reu(text)
        if contatos.get("email_parte_adversa") and not (data.get("email_parte_adversa") or "").strip():
            data["email_parte_adversa"] = contatos["email_parte_adversa"]
        if contatos.get("telefone_parte_adversa") and not (data.get("telefone_parte_adversa") or "").strip():
            data["telefone_parte_adversa"] = contatos["telefone_parte_adversa"]

        endereco = self._rx_endereco_reu(text)
        if endereco and not (data.get("endereco_parte_adversa") or "").strip():
            data["endereco_parte_adversa"] = endereco

        # Comarca (do cabeçalho judicial, NÃO do endereço das partes)
        comarca = self._rx_comarca(text)
        if comarca and not (data.get("comarca") or "").strip():
            data["comarca"] = comarca

        # Valor da causa
        vcausa = self._rx_valor_causa(text)
        if vcausa and not (data.get("valor_causa") or "").strip():
            data["valor_causa"] = f"R$ {vcausa}" if not vcausa.strip().startswith("R$") else vcausa

        # Rito
        rito = self._rx_rito(text)
        if rito and not (data.get("rito") or "").strip():
            data["rito"] = rito

        # Datas
        dist = self._rx_data_distribuicao(text)
        if dist and not (data.get("data_distribuicao") or "").strip():
            data["data_distribuicao"] = dist

        aud = self._rx_audiencia(text)
        if aud and not (data.get("audiencia_inicial") or "").strip():
            data["audiencia_inicial"] = aud
            data["cadastrar_primeira_audiencia"] = True

        return data

    # =========================
    # Extração principal
    # =========================
    def extract_process_data_from_pdf(self, text: str) -> Dict[str, Any]:
        """Extrai dados estruturados a partir do texto completo do PDF."""
        try:
            logger.info("Analisando documento completo: %d caracteres", len(text))
            if len(text) > 50000:
                result = self._analyze_large_document(text)
            else:
                result = self._analyze_full_document(text)
            return result
        except Exception as e:
            logger.error("Erro na extração de dados: %s", e)
            return {
                "cnj": "Não",
                "tipo_processo": "Eletrônico",
                "numero_processo": "",
                "numero_processo_antigo": "",
                "sistema_eletronico": "",
                "area_direito": "",
                "sub_area_direito": "",
                "estado": "",
                "comarca": "",
                "numero_orgao": "",
                "origem": "",
                "orgao": "",
                "vara": "",
                "celula": "",
                "foro": "",
                "instancia": "",
                "assunto": "Processo extraído de PDF - verificar manualmente",
                "npc": "",
                "objeto": "Analisar documento PDF anexo",
                "sub_objeto": "",
                "audiencia_inicial": ""
            }

    def _analyze_full_document(self, text: str) -> Dict[str, Any]:
        """Analisa o documento inteiro em uma única chamada ao LLM."""
        try:
            prompt = (
                "ANÁLISE EXAUSTIVA DE DOCUMENTO JURÍDICO BRASILEIRO\n\n"
                "Você DEVE extrair TODAS as informações possíveis deste documento. Leia TODO o texto cuidadosamente.\n\n"
                "CAMPOS OBRIGATÓRIOS (não podem ficar vazios):\n"
                "- cnj, tipo_processo, estado, comarca, origem, orgao, foro, instancia, assunto, objeto\n\n"
                "INSTRUÇÕES DETALHADAS:\n"
                "1. NÚMEROS DE PROCESSO: Procure em TODO formato possível (NNNNNNN-NN.NNNN.N.NN.NNNN, antigos, etc.)\n"
                "2. ÓRGÃOS: Identifique números, nomes completos, abreviações (1ª VT, 2ª VC, etc.)\n"
                "3. LOCALIZAÇÃO: Estado, comarca (do CABEÇALHO judicial, NÃO do endereço das partes!), foro específico\n"
                "4. SISTEMAS: PJE, E-PROC, PROJUDI - procure em todo o documento\n"
                "5. CLASSIFICAÇÕES: NPCs, áreas, sub-áreas específicas\n"
                "6. RITO: Identifique corretamente Ordinário, Sumaríssimo ou Sumário (procure \"Rito\" no cabeçalho)\n\n"
                "*** ATENÇÃO CRÍTICA - DATAS (NÃO CONFUNDIR!):\n\n"
                "A) DATA DE DISTRIBUIÇÃO:\n"
                "   - Procure por 'Distribuído em DD/MM/AAAA' no CABEÇALHO/INÍCIO do documento\n"
                "   - Exemplo: 'Distribuído em 25/09/2025 12:16:24'\n"
                "   - Esta é a data que o processo foi distribuído ao juízo\n"
                "   - Coloque em 'data_distribuicao'\n\n"
                "B) AUDIÊNCIA INICIAL:\n"
                "   - Procure por 'audiência INICIAL', 'Determino a audiência INICIAL' no CORPO do documento\n"
                "   - Exemplo: 'audiência INICIAL TELEPRESENCIAL... : 12/12/2025 09:10'\n"
                "   - Esta é a data/hora AGENDADA para primeira audiência\n"
                "   - Coloque em 'audiencia_inicial' no formato 'YYYY-MM-DD HH:MM:SS'\n"
                "   - Se encontrar audiência inicial, marque 'cadastrar_primeira_audiencia' como 'Sim'\n"
                "   - Se NÃO encontrar 'audiência INICIAL' explicitamente, deixe 'audiencia_inicial' VAZIO\n\n"
                "REGRA: Distribuição ≠ Audiência. São datas DIFERENTES. NÃO confundir!\n\n"
                "7. VARA vs CÉLULA - DIFERENÇA CRÍTICA:\n\n"
                "   VARA (JUDICIAL - TEM JUIZ):\n"
                "   - Órgão jurisdicional que julga processos e profere sentenças\n"
                "   - Exemplos: \"1ª Vara do Trabalho de São Gonçalo\", \"2ª Vara Cível\", \"5ª Vara Federal\"\n\n"
                "   CÉLULA (ADMINISTRATIVA - SEM JUIZ):\n"
                "   - Setor administrativo/unidade interna para apoio (NÃO julga, NÃO profere sentenças)\n"
                "   - Exemplos: \"Célula de Distribuição\", \"Célula de Cumprimento de Mandados\", \"Célula de Protocolo\"\n"
                "   - IMPORTANTE: só preencher se o documento mencionar EXPLICITAMENTE \"Célula de...\"\n"
                "   - Em TRT e Justiça Federal, célula GERALMENTE NÃO EXISTE - deixe em branco\n"
                "   - Se não encontrar \"Célula de...\", deixe VAZIO\n\n"
                "7. ASSUNTO/OBJETO: Capture descrição completa e detalhada\n\n"
                "PROCURE POR PADRÕES COMO:\n"
                "- \"Vara\", \"Tribunal\", \"Foro\", \"Comarca\"\n"
                "- Números como \"1ª\", \"2ª\", \"3ª\"\n"
                "- Estados: \"RJ\", \"SP\", \"Rio de Janeiro\", \"São Paulo\"\n"
                "- Sistemas: \"PJe\", \"PJE\", \"E-PROC\", \"PROJUDI\"\n"
                "- NPCs: códigos numéricos de classificação\n\n"
                "RESPONDA APENAS JSON - TODOS OS CAMPOS PREENCHIDOS:\n"
                "{\n"
                '  "cnj": "Sim ou Não",\n'
                '  "tipo_processo": "Eletrônico ou Físico",\n'
                '  "numero_processo": "número encontrado",\n'
                '  "numero_processo_antigo": "número antigo ou vazio",\n'
                '  "sistema_eletronico": "PJE, E-PROC, PROJUDI, TRT ou inferido",\n'
                '  "area_direito": "Trabalhista, Cível, Criminal, etc",\n'
                '  "sub_area_direito": "especificação detalhada",\n'
                '  "estado": "sigla do estado",\n'
                '  "comarca": "nome da comarca (do CABEÇALHO judicial - ex: Rio de Janeiro, Macaé, São Gonçalo - NÃO do endereço das partes!)",\n'
                '  "numero_orgao": "número da vara",\n'
                '  "origem": "TRT, STF, JF, etc",\n'
                '  "orgao": "nome completo do órgão",\n'
                '  "vara": "nome COMPLETO da vara judicial",\n'
                '  "celula": "apenas se mencionar explicitamente Célula de...",\n'
                '  "foro": "nome do foro",\n'
                '  "instancia": "Primeira, Segunda Instância, etc",\n'
                '  "assunto": "assunto completo do processo",\n'
                '  "npc": "código NPC ou vazio",\n'
                '  "objeto": "finalidade completa",\n'
                '  "sub_objeto": "detalhamento específico",\n'
                '  "audiencia_inicial": "YYYY-MM-DD HH:MM:SS ou vazio",\n'
                '  "estrategia": "Defensiva/Negocial/Ativa ou vazio",\n'
                '  "indice_atualizacao": "IPCA-E/TR/SELIC ou vazio",\n'
                '  "posicao_parte_interessada": "AUTOR ou REU",\n'
                '  "parte_interessada": "nome do cliente",\n'
                '  "parte_adversa_tipo": "FISICA/JURIDICA ou vazio",\n'
                '  "parte_adversa_nome": "nome parte adversa",\n'
                '  "escritorio_parte_adversa": "se houver",\n'
                '  "uf_oab_advogado_adverso": "UF se houver OAB",\n'
                '  "cpf_cnpj_parte_adversa": "documento",\n'
                '  "telefone_parte_adversa": "",\n'
                '  "email_parte_adversa": "",\n'
                '  "endereco_parte_adversa": "",\n'
                '  "data_distribuicao": "DD/MM/AAAA ou vazio",\n'
                '  "data_citacao": "DD/MM/AAAA ou vazio",\n'
                '  "risco": "Baixo/Médio/Alto",\n'
                '  "valor_causa": "R$ 0.000,00",\n'
                '  "rito": "Ordinário/Sumaríssimo/Sumário (procure \'Rito\' no cabeçalho - ATENÇÃO: Sumaríssimo ≠ Ordinário!)",\n'
                '  "observacao": "até 300 chars",\n'
                '  "cadastrar_primeira_audiencia": "Sim/Não"\n'
               "}\n\n"
                f"DOCUMENTO COMPLETO:\n{text}"
            )
            response = self.openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            result = json.loads(response.choices[0].message.content)
            logger.info("Análise completa - Primeira passada concluída.")
            result = self._fill_mandatory_fields(text, result)
            return result
        except Exception as e:
            logger.error("Erro na análise completa: %s", e)
            return self._get_default_values()

    def _analyze_large_document(self, text: str) -> Dict[str, Any]:
        """Divide o documento e faz uma análise progressiva para preencher campos vazios."""
        try:
            chunk_size = 20000
            overlap = 3000
            chunks: List[str] = []
            for i in range(0, len(text), chunk_size - overlap):
                chunks.append(text[i : i + chunk_size])

            logger.info("Documento grande dividido em %d partes.", len(chunks))
            header_result = self._analyze_full_document(chunks[0])

            for i, chunk in enumerate(chunks[1:], 1):
                empty_fields = [
                    k
                    for k, v in header_result.items()
                    if not v or (isinstance(v, str) and v.strip() == "")
                ]
                if empty_fields:
                    logger.info("Analisando chunk %d para campos vazios: %s", i, empty_fields)
                    chunk_result = self._focused_chunk_analysis(chunk, empty_fields)
                    for field, value in chunk_result.items():
                        if field in empty_fields and value and (
                            not isinstance(value, str) or value.strip()
                        ):
                            header_result[field] = value

            return header_result
        except Exception as e:
            logger.error("Erro na análise de documento grande: %s", e)
            return self._get_default_values()

    def _focused_chunk_analysis(self, chunk: str, missing_fields: List[str]) -> Dict[str, Any]:
        """Análise focada em campos específicos que ficaram vazios."""
        try:
            field_descriptions = {
                "numero_processo_antigo": "número do processo no formato antigo",
                "sistema_eletronico": "sistema eletrônico (PJE, E-PROC, PROJUDI)",
                "sub_area_direito": "sub-área específica do direito",
                "numero_orgao": "número da vara ou órgão",
                "vara": "nome completo da vara judicial (órgão que julga)",
                "celula": "APENAS se mencionar explicitamente 'Célula de...' (setor administrativo, não vara)",
                "foro": "nome do foro",
                "npc": "código NPC para classificação",
                "sub_objeto": "detalhamento específico do objeto",
                "audiencia_inicial": "data e hora da audiência no formato DD/MM/AAAA HH:MM",
            }
            relevant_fields = [f for f in missing_fields if f in field_descriptions]
            if not relevant_fields:
                return {}

            prompt = (
                "Procure especificamente por estas informações neste trecho do documento:\n\n"
                "CAMPOS ESPECÍFICOS NECESSÁRIOS:\n"
                + "\n".join([f"- {f}: {field_descriptions[f]}" for f in relevant_fields])
                + "\n\nResponda APENAS com JSON:\n{\n"
                + ", ".join([f'"{f}": "valor encontrado ou vazio"' for f in relevant_fields])
                + "\n}\n\nTRECHO DO DOCUMENTO:\n"
                f"{chunk}"
            )

            response = self.openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.error("Erro na análise focada: %s", e)
            return {}

    def _fill_mandatory_fields(self, text: str, result: Dict[str, Any]) -> Dict[str, Any]:
        """Garante preenchimento dos campos obrigatórios (ou preenchimento inteligente)."""
        mandatory_fields = [
            "cnj",
            "tipo_processo",
            "estado",
            "comarca",
            "origem",
            "orgao",
            "foro",
            "instancia",
            "assunto",
            "objeto",
        ]
        empty_mandatory = [
            f
            for f in mandatory_fields
            if not result.get(f) or (isinstance(result.get(f), str) and result.get(f).strip() == "")
        ]
        if empty_mandatory:
            logger.info("Preenchendo campos obrigatórios vazios: %s", empty_mandatory)
            prompt = (
                "ATENÇÃO: Campos obrigatórios estão vazios. Analise novamente TODO o documento e extraia:\n\n"
                f"CAMPOS OBRIGATÓRIOS VAZIOS: {', '.join(empty_mandatory)}\n\n"
                "DADOS JÁ ENCONTRADOS:\n"
                f"{json.dumps(result, indent=2, ensure_ascii=False)}\n\n"
                "INSTRUÇÕES ESPECÍFICAS:\n"
                "- Se não encontrar explicitamente, INFIRA baseado no contexto\n"
                "- Para estado: procure endereços, cabeçalhos, rodapés\n"
                "- Para comarca: pode estar no cabeçalho ou referências do tribunal\n"
                "- Para órgão/vara: procure por \"Vara\", \"Tribunal\", números ordinais (ex: 1ª Vara)\n"
                "- Para foro: pode estar nas informações do tribunal\n"
                "- Para CNJ: se há número de processo no formato NNNNNNN-NN.NNNN, então CNJ = \"Sim\"\n"
                "- Para tipo_processo: se há sistema eletrônico mencionado, então \"Eletrônico\"\n\n"
                "Responda APENAS JSON com os campos que estavam vazios:\n{\n"
                + ", ".join([f'"{field}": "valor obrigatório"' for field in empty_mandatory])
                + "\n}\n\nDOCUMENTO COMPLETO:\n"
                f"{text[:25000]}"
            )
            try:
                response = self.openai_client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                    temperature=0.2,
                )
                mandatory_result = json.loads(response.choices[0].message.content)
                result.update(mandatory_result)
                logger.info("Campos obrigatórios preenchidos via LLM.")
            except Exception as e:
                logger.error("Erro ao preencher campos obrigatórios: %s", e)
                defaults = {
                    "cnj": "Sim" if result.get("numero_processo") else "Não",
                    "tipo_processo": "Eletrônico" if result.get("sistema_eletronico") else "Físico",
                    "estado": "RJ",
                    "comarca": "Capital",
                    "origem": "TRT" if result.get("area_direito") == "Trabalhista" else "JF",
                    "orgao": "Vara do Trabalho" if result.get("area_direito") == "Trabalhista" else "Vara Cível",
                    "vara": "",
                    "celula": "",
                    "foro": "Foro Central",
                    "instancia": "Primeira Instância",
                    "assunto": "Processo jurídico - verificar documento",
                    "objeto": "Análise de direitos - verificar documento PDF",
                }
                for field in empty_mandatory:
                    if field in defaults:
                        result[field] = defaults[field]
        return result

    def _get_default_values(self) -> Dict[str, Any]:
        """Valores padrão quando a extração falha."""
        return {
            "cnj": "Sim",
            "tipo_processo": "Eletrônico",
            "numero_processo": "",
            "numero_processo_antigo": "",
            "sistema_eletronico": "",
            "area_direito": "",
            "sub_area_direito": "",
            "estado": "RJ",
            "comarca": "Capital",
            "numero_orgao": "",
            "origem": "JF",
            "orgao": "Vara Cível",
            "vara": "",
            "celula": "",
            "foro": "Foro Central",
            "instancia": "Primeira Instância",
            "assunto": "Processo extraído de PDF - verificar manualmente",
            "npc": "",
            "objeto": "Analisar documento PDF anexo",
            "sub_objeto": "",
            "audiencia_inicial": ""
        }

    def _second_pass_extraction(self, text: str, first_result: Dict[str, Any], empty_fields: List[str]) -> Dict[str, Any]:
        """Segunda passada para campos específicos ainda vazios."""
        try:
            field_descriptions = {
                "numero_processo_antigo": "número do processo no formato antigo (diferente do atual)",
                "sistema_eletronico": "sistema eletrônico usado (PJE, E-PROC, PROJUDI, TRT)",
                "sub_area_direito": "sub-área específica do direito (ex: Direito do Consumidor, Acidente de Trabalho)",
                "numero_orgao": "número da vara ou órgão (1ª, 2ª, 3ª, etc)",
                "vara": "nome completo da vara judicial (ex: 1ª Vara do Trabalho de São Gonçalo)",
                "celula": "APENAS se mencionar explicitamente 'Célula de...' (setor administrativo). Em TRT/JF deixe vazio",
                "foro": "nome do foro (ex: Foro Central, Foro Regional)",
                "npc": "código NPC para classificação processual",
                "sub_objeto": "detalhamento específico do objeto do processo",
                "audiencia_inicial": "data e hora da audiência (DD/MM/AAAA HH:MM)",
            }
            fields_to_extract = [f for f in empty_fields if f in field_descriptions]
            if not fields_to_extract:
                return {}

            field_prompts = [f'"{f}": "{field_descriptions[f]}"' for f in fields_to_extract]
            prompt = (
                "Analise novamente este documento jurídico e procure especificamente por estas informações que não foram encontradas na primeira análise:\n\n"
                f"CAMPOS ESPECÍFICOS PARA ENCONTRAR:\n{', '.join(field_prompts)}\n\n"
                "CONTEXTO DA PRIMEIRA ANÁLISE:\n"
                f"- Já encontramos: {first_result.get('numero_processo', 'N/A')}\n"
                f"- Área: {first_result.get('area_direito', 'N/A')}\n"
                f"- Órgão: {first_result.get('orgao', 'N/A')}\n"
                f"- Estado: {first_result.get('estado', 'N/A')}\n\n"
                "INSTRUÇÕES:\n"
                "- Procure em todo o documento por essas informações específicas\n"
                "- Considere abreviações e formatos alternativos\n\n"
                "Responda APENAS com JSON contendo os campos encontrados:\n{\n"
                + ", ".join([f'"{f}": "valor encontrado ou vazio"' for f in fields_to_extract])
                + "\n}\n\nTEXTO DO DOCUMENTO:\n"
                f"{text}"
            )
            response = self.openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.2,
            )
            second_result = json.loads(response.choices[0].message.content)
            logger.info("Segunda passada concluída.")
            return second_result
        except Exception as e:
            logger.error("Erro na segunda passada: %s", e)
            return {}

    def _contextual_extraction(self, text: str, current_result: Dict[str, Any]) -> Dict[str, Any]:
        """Aprimora extração com base na área do direito."""
        try:
            area = current_result.get("area_direito", "")
            if not area:
                return {}

            context_prompts = {
                "Trabalhista": {
                    "vara": "nome completo da vara judicial (ex: 1ª Vara do Trabalho de São Gonçalo)",
                    "celula": 'APENAS se mencionar explicitamente "Célula de..." - em TRT deixe vazio',
                    "sub_area_direito": "área específica trabalhista (ex: Acidente de Trabalho, Rescisão, FGTS)",
                    "numero_orgao": "número da Vara do Trabalho",
                    "sistema_eletronico": "geralmente PJE para trabalhista",
                },
                "Cível": {
                    "vara": "nome completo da vara judicial (ex: 1ª Vara Cível)",
                    "celula": 'APENAS se mencionar explicitamente "Célula de..." (setor administrativo)',
                    "sub_area_direito": "área específica cível (ex: Consumidor, Contratos, Família)",
                    "numero_orgao": "número da Vara Cível",
                    "foro": "foro onde tramita",
                },
                "Criminal": {
                    "vara": "nome completo da vara judicial (ex: 1ª Vara Criminal)",
                    "celula": 'APENAS se mencionar explicitamente "Célula de..." (setor administrativo)',
                    "sub_area_direito": "área específica criminal (ex: Tráfico, Furto, Homicídio)",
                    "numero_orgao": "número da Vara Criminal",
                    "instancia": "instância do processo criminal",
                },
            }
            if area not in context_prompts:
                return {}

            area_context = context_prompts[area]
            fields_to_improve: List[str] = []
            for field, desc in area_context.items():
                val = current_result.get(field)
                if not val or (isinstance(val, str) and val.strip() == ""):
                    fields_to_improve.append((field, desc))

            if not fields_to_improve:
                return {}

            prompt = (
                f"Este é um documento da área {area}. Com base nesse contexto específico, procure as seguintes informações:\n\n"
                f"CAMPOS ESPECÍFICOS PARA {area.upper()}:\n"
                + "\n".join([f"- {f}: {d}" for f, d in fields_to_improve])
                + "\n\nCONTEXTO ATUAL:\n"
                f"- Processo: {current_result.get('numero_processo', 'N/A')}\n"
                f"- Órgão: {current_result.get('orgao', 'N/A')}\n"
                f"- Estado: {current_result.get('estado', 'N/A')}\n\n"
                "Responda APENAS com JSON:\n{\n"
                + ", ".join([f'"{f}": "valor encontrado ou vazio"' for f, _ in fields_to_improve])
                + "\n}\n\nTEXTO:\n"
                f"{text[:10000]}"
            )
            response = self.openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            contextual_result = json.loads(response.choices[0].message.content)
            logger.info("Análise contextual concluída.")
            return contextual_result
        except Exception as e:
            logger.error("Erro na análise contextual: %s", e)
            return {}

    # =========================
    # Pipelines públicas
    # =========================
    def extract_process_from_pdf(self, pdf_file_path: str) -> Dict[str, Any]:
        """Extrai dados do PDF e retorna as informações estruturadas."""
        start_time = time.time()
        try:
            logger.info("Extraindo dados do PDF: %s", pdf_file_path)
            text = self.extract_text_from_pdf(pdf_file_path)
            logger.info("Texto extraído: %d caracteres", len(text))

            process_data = self.extract_process_data_from_pdf(text)

            # >>> NOVO: baseline determinístico por regex antes do pós-processamento
            process_data = self._baseline_regex_fill(text, process_data)

            try:
                process_data = postprocess_extracted_fields(full_text=text, data=process_data)
                logger.info("[RAG] Pós-processamento aplicado.")
            except Exception as e:
                logger.exception("postprocess_extracted_fields falhou: %s", e)
            # [GAP] Completar campos antes de retornar
            try:
                process_data, needs_review, evidence = gap_fill(process_data, text)
                logger.info("[GAP] Campos enriquecidos; pendentes=%s", needs_review)
            except Exception as e:
                logger.exception("[GAP] Falhou: %s", e)

            process_data = self._finalize_result(text, process_data)
            logger.info("[RAG] Campos prontos (amostra): %s", {k: process_data.get(k) for k in list(process_data.keys())[:12]})

            return {
                "success": True,
                "data": process_data,
                "processing_time": time.time() - start_time,
                "extracted_text_length": len(text),
            }

        except Exception as e:
            logger.error("Erro na extração de dados do PDF: %s", e)
            return {"success": False, "error": str(e), "processing_time": time.time() - start_time}

    def process_pdf(self, process_id: int, pdf_file_path: str) -> Dict[str, Any]:
        """Pipeline completo: extrai texto, cria chunks, salva no BD e gera análise."""
        start_time = time.time()
        try:
            logger.info("Iniciando processamento RAG para processo %d", process_id)
            text = self.extract_text_from_pdf(pdf_file_path)
            total_tokens = self.count_tokens(text)
            logger.info("Texto extraído: %d caracteres, %d tokens", len(text), total_tokens)

            logger.info("Extraindo dados estruturados do PDF...")
            process_data = self.extract_process_data_from_pdf(text)

            # >>> NOVO: baseline determinístico por regex antes do pós-processamento
            process_data = self._baseline_regex_fill(text, process_data)

            try:
                process_data = postprocess_extracted_fields(full_text=text, data=process_data)
                logger.info("[RAG] Campos extraídos com sucesso (pós-processamento).")
            except Exception as e:
                logger.exception("Erro no pós-processamento: %s", e)
            # [GAP] Completar campos antes de salvar no BD
            try:
                process_data, needs_review, evidence = gap_fill(process_data, text)
                logger.info("[GAP] Campos enriquecidos; pendentes=%s", needs_review)
            except Exception as e:
                logger.exception("[GAP] Falhou: %s", e)
                print()
            process_data = self._finalize_result(text, process_data)
            # >>> salva payload mínimo para o RPA
            try:
                rpa_payload = self._to_rpa_payload(process_data)
                self._salvar_ultimo_processo(rpa_payload)
            except Exception as e:
                logger.warning("[RPA][SAVE] falhou: %s", e)

            process = Process.query.get(process_id)
            if process and process_data:
                logger.info("Atualizando Process com dados extraídos...")

                audiencia_str = process_data.get("audiencia_inicial")
                if audiencia_str and isinstance(audiencia_str, str):
                    try:
                        from datetime import datetime
                        import re as _re

                        # ISO (YYYY-MM-DD HH:MM:SS)
                        if _re.match(r"\d{4}-\d{2}-\d{2}", audiencia_str):
                            process.audiencia_inicial = datetime.fromisoformat(audiencia_str.replace(" ", "T"))
                        # BR (DD/MM/YYYY HH:MM)
                        elif "/" in audiencia_str:
                            parts = audiencia_str.strip()
                            date_part, time_part = parts.split(" ") if " " in parts else (parts, "00:00")
                            d, m, y = date_part.split("/")
                            hh, mm = time_part.split(":") if ":" in time_part else ("00", "00")
                            process.audiencia_inicial = datetime(int(y), int(m), int(d), int(hh), int(mm))
                        else:
                            logger.warning("Formato de data não reconhecido para audiencia_inicial: %s", audiencia_str)
                            process.audiencia_inicial = None
                    except Exception as e:
                        logger.warning("Erro ao parsear audiencia_inicial '%s': %s", audiencia_str, e)
                        process.audiencia_inicial = None
                else:
                    process.audiencia_inicial = audiencia_str  # mantém vazio se não houver

                # cliente/parte a partir de JSON (se houver)
                cliente_parte_json = process_data.get("cliente_parte")
                if cliente_parte_json:
                    try:
                        if isinstance(cliente_parte_json, str):
                            cliente_parte_dict = json.loads(cliente_parte_json)
                        else:
                            cliente_parte_dict = cliente_parte_json
                        process.cliente = (
                            cliente_parte_dict.get("reclamante")
                            or cliente_parte_dict.get("autor")
                            or process.cliente
                        )
                        process.parte = (
                            cliente_parte_dict.get("reclamado")
                            or cliente_parte_dict.get("reu")
                            or process.parte
                        )
                    except Exception as e:
                        logger.warning("Erro ao extrair cliente/parte de cliente_parte: %s", e)

                # Atualiza campos textuais extras (se existirem no modelo)
                process.cliente_parte = process_data.get("cliente_parte")
                process.advogado_autor = process_data.get("advogado_autor")
                process.advogado_reu = process_data.get("advogado_reu")
                process.prazo = process_data.get("prazo")
                process.tipo_notificacao = process_data.get("tipo_notificacao")
                process.resultado_audiencia = process_data.get("resultado_audiencia")
                process.prazos_derivados_audiencia = process_data.get("prazos_derivados_audiencia")
                process.decisao_tipo = process_data.get("decisao_tipo")
                process.decisao_resultado = process_data.get("decisao_resultado")
                process.decisao_fundamentacao_resumida = process_data.get("decisao_fundamentacao_resumida")
                process.id_interno_hilo = process_data.get("id_interno_hilo")
                process.data_hora_cadastro_manual = process_data.get("data_hora_cadastro_manual")

            # Chunks
            chunk_data = self.create_smart_chunks(text)
            logger.info("Criados %d chunks.", len(chunk_data))

            chunks_created: List[PDFChunk] = []
            for chunk_info in chunk_data:
                embedding = self.generate_embedding(chunk_info["content"])
                ai_analysis = self.analyze_chunk_with_ai(chunk_info["content"])

                chunk = PDFChunk(
                    process_id=process_id,
                    chunk_index=chunk_info["index"],
                    content=chunk_info["content"],
                    token_count=chunk_info["token_count"],
                    embedding=json.dumps(embedding or []),
                    summary=ai_analysis.get("summary"),
                    key_entities=json.dumps(ai_analysis.get("entities", [])),
                    legal_concepts=json.dumps(ai_analysis.get("legal_concepts", [])),
                )
                db.session.add(chunk)
                chunks_created.append(chunk)
                logger.info("Processado chunk %d/%d", chunk_info["index"] + 1, len(chunk_data))

            db.session.flush()  # IDs dos chunks

            # Análise global do documento
            analysis_data = self.generate_document_analysis(process, chunks_created)

            analysis = ProcessAnalysis(
                process_id=process_id,
                document_summary=analysis_data.get("document_summary"),
                key_points=json.dumps(analysis_data.get("key_points", [])),
                legal_issues=json.dumps(analysis_data.get("legal_issues", [])),
                recommendations=analysis_data.get("recommendations"),
                confidence_score=analysis_data.get("confidence_score", 0.0),
                total_chunks=len(chunk_data),
                total_tokens=total_tokens,
                processing_time=time.time() - start_time,
            )
            db.session.add(analysis)
            db.session.commit()

            logger.info("Processamento RAG concluído em %.2f segundos", time.time() - start_time)
            return {
                "success": True,
                "chunks_created": len(chunk_data),
                "total_tokens": total_tokens,
                "processing_time": time.time() - start_time,
                "analysis": analysis_data,
                "extracted_data": process_data,
            }
        except Exception as e:
            db.session.rollback()
            logger.error("Erro no processamento RAG: %s", e)
            return {"success": False, "error": str(e), "processing_time": time.time() - start_time}

    def process_pdf_background(self, process_id: int, pdf_file_path: str) -> None:
        """Dispara o processamento em thread para não travar a requisição HTTP."""
        from app import app

        def background_task():
            with app.app_context():
                try:
                    logger.info("[BACKGROUND] Iniciando processamento para processo %d", process_id)
                    result = self.process_pdf(process_id, pdf_file_path)
                    if result.get("success"):
                        logger.info("[BACKGROUND] Processamento concluído para processo %d", process_id)
                    else:
                        logger.error("[BACKGROUND] Erro no processamento: %s", result.get("error"))
                except Exception as e:
                    logger.error("[BACKGROUND] Exceção no processamento: %s", e)

        thread = threading.Thread(target=background_task, daemon=True)
        thread.start()
        logger.info("Thread de background iniciada para processo %d", process_id)

    def generate_document_analysis(self, process: Optional[Process], chunks: List[PDFChunk]) -> Dict[str, Any]:
        """
        Gera uma análise consolidada do documento com base nos resumos dos chunks.
        """
        try:
            summaries = []
            entities = []
            concepts = []
            for ch in chunks:
                if ch.summary:
                    summaries.append(ch.summary)
                try:
                    if ch.key_entities:
                        entities.extend(json.loads(ch.key_entities))
                except Exception:
                    pass
                try:
                    if ch.legal_concepts:
                        concepts.extend(json.loads(ch.legal_concepts))
                except Exception:
                    pass

            joined_summary = "\n".join(summaries)[:8000]
            joined_entities = ", ".join(list(dict.fromkeys([e for e in entities if isinstance(e, str)])))[:2000]
            joined_concepts = ", ".join(list(dict.fromkeys([c for c in concepts if isinstance(c, str)])))[:2000]

            prompt = (
                "Você é um assistente jurídico. Com base no material abaixo, produza:\n"
                "1) Um resumo executivo objetivo (5-8 frases)\n"
                "2) 5 a 8 pontos-chave em bullet points\n"
                "3) Principais questões jurídicas envolvidas\n"
                "4) Recomendações de próximos passos práticos\n"
                "Responda em JSON com as chaves: document_summary, key_points, legal_issues, recommendations, confidence_score.\n\n"
                f"SUMÁRIOS DOS CHUNKS:\n{joined_summary}\n\n"
                f"ENTIDADES RELEVANTES:\n{joined_entities}\n\n"
                f"CONCEITOS JURÍDICOS:\n{joined_concepts}"
            )

            response = self.openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.2,
            )
            data = json.loads(response.choices[0].message.content)
            if "confidence_score" not in data:
                data["confidence_score"] = 0.75
            return data
        except Exception as e:
            logger.error("Erro ao gerar análise do documento: %s", e)
            return {
                "document_summary": "Análise sintetizada indisponível.",
                "key_points": [],
                "legal_issues": [],
                "recommendations": "Revise os documentos e confirme os dados extraídos.",
                "confidence_score": 0.5,
            }

    def search_similar_content(self, query: str, process_id: Optional[int] = None, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Busca simples por similaridade textual (fallback sem pgvector).
        """
        try:
            _ = self.generate_embedding(query)  # preparado para futura busca vetorial
            q = PDFChunk.query
            if process_id:
                q = q.filter(PDFChunk.process_id == process_id)
            similar = q.filter(PDFChunk.content.ilike(f"%{query}%")).limit(limit).all()

            results: List[Dict[str, Any]] = []
            for ch in similar:
                results.append(
                    {
                        "chunk_id": ch.id,
                        "process_id": ch.process_id,
                        "content": ch.content[:500] + ("..." if len(ch.content) > 500 else ""),
                        "summary": ch.summary,
                        "entities": json.loads(ch.key_entities) if ch.key_entities else [],
                        "legal_concepts": json.loads(ch.legal_concepts) if ch.legal_concepts else [],
                    }
                )
            return results
        except Exception as e:
            logger.error("Erro na busca por similaridade: %s", e)
            return []


# Instância global
pdf_rag_service = PDFRAGService()
