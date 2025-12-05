## Overview
This project is a Flask-based legal process management system designed to streamline the handling and tracking of legal cases. It provides user registration, robust authentication with role-based access control, and comprehensive metadata management for legal processes. A core feature is the automated classification and data extraction from PDF legal documents, supporting various Brazilian legal terminologies. The system prioritizes database values over PDF extraction for critical data. It also integrates an RPA automation module for interacting with external legal systems like eLaw, offering real-time progress tracking and verification through screenshots. The primary goal is to enhance efficiency in legal process management through automation and structured data handling, ultimately improving the legal process workflow.

## User Preferences
Preferred communication style: Simple, everyday language.
Deploy workflow: Sempre limpar arquivos pesados do Git (.venv/, attached_assets/, *.backup.db, bundle.txt) antes de preparar para republish, para garantir deploys rápidos (2-3 min ao invés de 6-10 min).

## System Architecture

### UI/UX
The application uses server-side rendering with Jinja2 templates and Bootstrap for a responsive and consistent user interface. It employs template inheritance for a unified layout, and forms incorporate both client and server-side validation. The RPA progress screen features a modern UI with gradient backgrounds, animated progress bars, and real-time status updates.

### Technical Implementation & Feature Specifications
The system is built on the Flask web framework, utilizing SQLAlchemy for ORM and Flask-Login for user authentication and session management.

**Key Features:**

*   **Multi-Client Support:** Supports 24+ clients with fuzzy matching for name variations, prioritizing legal entities over individuals, and easily maintainable via a JSON database. Includes intelligent client prioritization and storage of secondary parties.
*   **Legal Nomenclature Support:** Recognizes and processes all 61 Brazilian legal terminologies from the eLaw system, adapting to document types to correctly identify parties without role inversion.
*   **Document Classification & Extraction:** Automatically classifies legal documents and extracts over 30 universal and document-specific fields. It intelligently adapts extraction to document type, prioritizes legal entities, includes multi-tier state detection, and extracts distribution dates and telepresential hearing links. Implements a 3-tier fallback system (Regex → LLM → OCR) for robust data extraction from various document types, including scanned PDFs. It includes data quality enhancements like auto-correction of inverted dates and enhanced entity extraction with blacklists and verb filters.
*   **Otimização de Extração (2025-12-01):** Utilitários compartilhados de normalização (normalize_text, normalize_monetary, MESES_MAP, is_valid_brazilian_date, is_invalid_date_context) garantem consistência em todas as funções de extração. Regex otimizados para: salário (TRCT, contracheques, verbos de recebimento), datas (admissão, demissão, distribuição, audiência), cargo/função, PIS, CTPS. Validação de contexto para evitar falsos positivos.
*   **Correção de Meses Quebrados por PDF (2025-12-05):** Parser de sumário (parse_toc_from_pdf) agora verifica tanto o início quanto o fim do PDF para encontrar índices do PJe. Adicionada função `_fix_broken_months()` que corrige palavras quebradas por PyPDF2 (ex: "ou tubro" → "outubro", "de zembro" → "dezembro"). Melhora significativa na extração de datas de demissão em documentos PJe.
*   **OCR Seletivo via Bookmarks (2025-12-05):** PDFs do PJe contêm anexos (TRCT, Contracheques, Ficha CTPS) como imagens embutidas que requerem OCR. Lógica SIMPLIFICADA - OCR por DOCUMENTO:
    - `extract_pdf_bookmarks()`: Extrai mapeamento dos bookmarks (CTPS→pág.19, TRCT→pág.21, Contracheque→pág.29)
    - Identifica quais DOCUMENTOS são necessários (não campos): CTPS, TRCT ou Contracheque
    - OCR 1x por documento → extrai TODOS os campos daquele documento em uma passada
    - Fallback hierárquico: Bookmarks → Sumário textual → Inferência Histórica → Heurística limitada
    - Economia de ~90 segundos por PDF (2 páginas ao invés de 5)
    - Tesseract instalado com português para extração
*   **Inteligência de Localização de Anexos (2025-12-05):** Sistema de aprendizado que mapeia onde documentos (CTPS, TRCT, Contracheque) aparecem em PDFs processados:
    - Tabela `AnnexLocation`: Armazena page_number, page_ratio, doc_type, confidence para cada documento encontrado
    - `infer_annex_pages_from_history()`: Consulta estatísticas históricas por faixa de tamanho do PDF
    - Padrões default quando sem histórico: CTPS ~82%, TRCT ~87%, Contracheque ~77% do total de páginas
    - Prioridade 2.5 no pipeline OCR (entre TOC e heurística)
    - Script de backfill: `python scripts/backfill_annex_locations.py`
*   **Suporte a PDFs Grandes (2025-12-05):** Limite de upload aumentado de 16MB para 100MB por arquivo. Limite total de request 2GB para suportar batches grandes.
*   **Política ZERO ERRORS (2025-12-01):** Funções de extração NUNCA fabricam dados. parse_date_extenso() rejeita datas só com mês/ano (não assume dia=01). extract_data_hora_audiencia() só retorna hora com minutos explícitos (não fabrica :00). extract_data_distribuicao() removido fallback perigoso de "primeira data no cabeçalho".
*   **Extração por Zonas Ampliada (2025-12-01):** Pipeline agora extrai 35 páginas iniciais (antes 15) para capturar seção DOS PEDIDOS que normalmente fica entre páginas 15-35 após fundamentação jurídica. Últimas 15 páginas mantidas para TRCT/dados trabalhistas.
*   **RPA Automation (eLaw Integration):** Headless RPA execution using Playwright for external system interaction, providing real-time, step-by-step progress tracking with field-by-field updates. It captures screenshots for verification, features robust status management, history logging, and graceful error handling. The system prioritizes database values over PDF extraction for critical fields and automatically registers first hearings. Supports parallel execution of RPA processes with isolated browser instances. Handles multiple defendants from PDF extraction to eLaw population.
*   **Data Isolation Fix (2025-12-02):** Corrigido bug de "data bleeding" onde dados de um processo vazavam para outro durante execução paralela do RPA. Solução: contextvars setados DENTRO da função async (run_elaw_login_once) para garantir propagação correta quando asyncio.run() cria novo event loop. Debug logging expandido para campos trabalhistas críticos (cargo, pis, ctps, salário, datas). Timeouts de produção aumentados para estabilidade.
*   **Otimizações Produção (2025-12-03):** Correções para estabilidade e desempenho:
    - Workers paralelos fixos em 5: MAX_RPA_WORKERS=5, MAX_EXTRACTION_WORKERS=5, MAX_UPLOAD_WORKERS=5
    - Configuração via variáveis de ambiente para flexibilidade (sobrescrever valores padrão)
    - Timeouts aumentados: NAV_TIMEOUT e BROWSER_LAUNCH de 120s → 180s (3 minutos)
    - Browser launch com retry: 3 tentativas com backoff exponencial (5s, 10s) antes de falhar
    - Parsing de pedidos corrigido: Trata pedidos_json como string JSON e parseia corretamente
    - Google Cloud nginx: Requer client_max_body_size 500M em /etc/nginx/nginx.conf
*   **Sistema Inteligente de Priorização de Pedidos:** 5 categorias de prioridade (P5-P1) garantem que verbas rescisórias essenciais (aviso prévio, férias, FGTS, 13º) sejam sempre inseridas primeiro, seguidas de salariais básicas, adicionais, indenizatórios e acessórios. Limite configurável de 30 pedidos com log detalhado de omissões.
*   **Parallel PDF Extraction:** Processes multiple PDFs concurrently using a ThreadPoolExecutor, ensuring isolated database sessions and robust error handling for each extraction task.
*   **Limpeza Automática de Screenshots (2025-12-03):** Sistema automático que remove screenshots de RPA antigos após 2 dias do processamento, liberando espaço em disco. Executa automaticamente 30 segundos após o servidor iniciar. Também remove status RPA concluídos há mais de 7 dias. Funções disponíveis: `cleanup_old_screenshots(days_old=2)`, `cleanup_old_statuses(days_old=7)`, `run_all_cleanup()`.
*   **LLM Fallback Otimizado (2025-12-02):** Novas funções LLM para extração avançada:
    - `extract_pedidos_with_llm()`: Extrai pedidos com categorização (verbas_rescisorias, salariais, indenizatorios, acessorios) quando regex falha.
    - `extract_reclamadas_with_llm()`: Identifica todas as empresas reclamadas quando regex não encontra.
    - `validate_extracted_data_with_llm()`: Valida consistência de datas, valores e documentos (disponível para uso manual).
    - `classify_document_with_llm()`: Classifica tipo de documento (petição inicial, sentença, etc).
    Pipeline usa fallback apenas quando regex falha (0 resultados) para manter velocidade.

### System Design Choices

*   **Modular Application Structure:** Uses Flask's Blueprint pattern for scalability and maintainability.
*   **Database Design:** Uses SQLite for development and supports PostgreSQL for production, with a user model linked to a comprehensive process model in a one-to-many relationship, including cascade deletes and tracking of secondary interested parties.
*   **Authentication & Authorization:** Implements role-based access control, secure password hashing, and protected routes.
*   **File Management:** Supports secure PDF document uploads with size limits and configurable storage.
*   **Configuration:** Environment-based configuration with sensible defaults.

## External Dependencies

*   **Web Framework:** Flask
*   **Database ORM:** Flask-SQLAlchemy
*   **Authentication & Session Management:** Flask-Login, Werkzeug
*   **Forms:** Flask-WTF, WTForms
*   **Frontend Libraries:** Bootstrap, Font Awesome
*   **Databases:** SQLite, PostgreSQL
*   **RPA Browser Automation:** Playwright
*   **RPA Monitoring:** rpa-monitor-client
*   **External Legal Systems:** eLaw
*   **Document Processing:** python-docx, Tesseract OCR, pdf2image
*   **Fuzzy Matching:** rapidfuzz