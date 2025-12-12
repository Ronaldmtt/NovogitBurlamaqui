## Overview
This project is a Flask-based legal process management system designed to streamline the handling and tracking of legal cases. It offers user registration, robust authentication with role-based access control, and comprehensive metadata management for legal processes. A key feature is the automated classification and data extraction from PDF legal documents, supporting various Brazilian legal terminologies. The system prioritizes database values over PDF extraction for critical data. It also integrates an RPA automation module for interacting with external legal systems like eLaw, providing real-time progress tracking and verification through screenshots. The primary goal is to enhance efficiency in legal process management through automation and structured data handling, ultimately improving the legal process workflow and improving case management.

## User Preferences
Preferred communication style: Simple, everyday language.
Deploy workflow: Sempre limpar arquivos pesados do Git (.venv/, attached_assets/, *.backup.db, bundle.txt) antes de preparar para republish, para garantir deploys rápidos (2-3 min ao invés de 6-10 min).
Database migrations: SEMPRE atualizar os DOIS bancos (desenvolvimento e produção) quando houver qualquer alteração de tabela/coluna.

## Database Configuration
- **Desenvolvimento (Replit):** ep-rough-frog-aet7l8az.c-2.us-east-2.aws.neon.tech (banco integrado)
- **Produção (Google Cloud):** ep-odd-river-ah3ax0po.c-3.us-east-1.aws.neon.tech (banco separado)

## System Architecture

### UI/UX
The application uses server-side rendering with Jinja2 templates and Bootstrap for a responsive and consistent user interface. It employs template inheritance for a unified layout, and forms incorporate both client and server-side validation. The RPA progress screen features a modern UI with gradient backgrounds, animated progress bars, and real-time status updates.

### Technical Implementation & Feature Specifications
The system is built on the Flask web framework, utilizing SQLAlchemy for ORM and Flask-Login for user authentication and session management.

**Key Features:**

*   **Multi-Client Support:** Supports 24+ clients with fuzzy matching for name variations, prioritizing legal entities, and easily maintainable via a JSON database.
*   **Legal Nomenclature Support:** Recognizes and processes all 61 Brazilian legal terminologies from the eLaw system, adapting to document types to correctly identify parties without role inversion.
*   **Document Classification & Extraction:** Automatically classifies legal documents and extracts over 30 universal and document-specific fields. It intelligently adapts extraction to document type, prioritizes legal entities, includes multi-tier state detection, and extracts distribution dates and telepresential hearing links. Implements a 3-tier fallback system (Regex → LLM → OCR) for robust data extraction from various document types, including scanned PDFs. It includes data quality enhancements like auto-correction of inverted dates and enhanced entity extraction with blacklists and verb filters.
    *   **Extraction Optimization:** Shared normalization utilities ensure consistency. Optimized Regex for salary, dates, job title, PIS, CTPS. Context validation prevents false positives. Selective OCR via bookmarks and historical inference optimizes processing for embedded images in PDFs (e.g., TRCT, Contracheques, CTPS). Asynchronous OCR queue limits simultaneous OCR processes, allowing other tasks to proceed. OCR timeouts are increased, and performance is boosted through reduced DPI, image pre-processing, and optimized PSM settings per document type.
    *   **"ZERO ERRORS" Policy:** Extraction functions never fabricate data; strict validation for dates and times.
    *   **Expanded Zone Extraction:** Extracts from 35 initial pages to capture "DOS PEDIDOS" sections.
*   **RPA Automation (eLaw Integration):** Headless RPA execution using Playwright for external system interaction, providing real-time, step-by-step progress tracking with screenshots for verification. Features robust status management, history logging, and graceful error handling. Prioritizes database values over PDF extraction for critical fields and automatically registers first hearings. Supports parallel execution with isolated browser instances.
    *   **Intelligent Multiple Defendant Handling:** Detects registered clients among extra defendants for correct selection in eLaw.
    *   **Data Isolation Fix:** Corrected "data bleeding" during parallel RPA execution using contextvars.
    *   **Production Optimizations:** Fixed parallel worker counts, increased timeouts, and added browser launch retries for stability.
    *   **Global Batch Queue System:** Sequential batch processing queue that allows multiple batches to be queued and processed automatically one after another. Uses PostgreSQL advisory locks to ensure only one runner executes across all Gunicorn workers. Features: batch queue management (add/remove/reorder), automatic transition to next batch upon completion, real-time progress tracking via polling, 5 parallel workers per batch, start/stop controls. Accessible via `/processos/batch/queue` route.
    *   **Real-Time Monitoring (RPA Monitor Client):** Sistema de monitoramento remoto via WebSocket para observar toda a atividade do sistema em tempo real. Envia logs INFO/WARNING/ERROR para servidor central, com screenshots obrigatórios em todos os erros. Integrado em: routes.py, routes_batch.py, rpa.py, batch_queue_runner.py, pdf_rag_service.py, e extractors/. Configuração via secrets: RPA_MONITOR_ID, RPA_MONITOR_HOST, RPA_MONITOR_REGION, RPA_MONITOR_TRANSPORT, RPA_MONITOR_ENABLED.
*   **Intelligent Request Prioritization System:** Uses 5 priority categories (P5-P1) to ensure essential severance payments (notice, vacation, FGTS, 13th salary) are inserted first, followed by basic wages, additional, indemnification, and accessory claims. Configurable limit of 30 requests with detailed omission logging.
*   **Parallel PDF Extraction:** Processes multiple PDFs concurrently using a ThreadPoolExecutor, ensuring isolated database sessions and robust error handling.
    *   **Size-based Ordering:** Smaller PDFs are processed first in batch extractions for faster visual progress.
    *   **Data-Weight RPA Ordering:** RPA prioritizes processes with fewer fields to fill for quicker completion, applying to both batch start and selective reprocessing.
*   **Selective Reprocessing:** Allows users to select and reprocess specific PDFs from a batch, either re-extracting data or re-running RPA, with asynchronous background processing.
*   **Automatic Screenshot Cleanup:** Automatically removes old RPA screenshots (after 2 days) and completed RPA statuses (after 7 days) to free up disk space.
*   **Optimized LLM Fallback:** Utilizes LLM functions for advanced extraction of requests, identification of defendants, data validation, and document classification when regex fails.

### System Design Choices

*   **Modular Application Structure:** Uses Flask's Blueprint pattern for scalability and maintainability.
*   **Database Design:** Uses PostgreSQL (Neon) for both development and production, with a user model linked to a comprehensive process model in a one-to-many relationship, including cascade deletes and tracking of secondary interested parties.
*   **Authentication & Authorization:** Implements role-based access control, secure password hashing, and protected routes.
*   **File Management:** Supports secure PDF document uploads with increased size limits and configurable storage.
*   **Configuration:** Environment-based configuration with sensible defaults.

## External Dependencies

*   **Web Framework:** Flask
*   **Database ORM:** Flask-SQLAlchemy
*   **Authentication & Session Management:** Flask-Login, Werkzeug
*   **Forms:** Flask-WTF, WTForms
*   **Frontend Libraries:** Bootstrap, Font Awesome
*   **Databases:** PostgreSQL (Neon)
*   **RPA Browser Automation:** Playwright
*   **RPA Monitoring:** rpa-monitor-client
*   **External Legal Systems:** eLaw
*   **Document Processing:** python-docx, Tesseract OCR, pdf2image
*   **Fuzzy Matching:** rapidfuzz