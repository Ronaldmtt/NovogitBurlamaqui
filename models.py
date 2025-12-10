# models.py
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func, event
from flask_login import UserMixin, current_user

from extensions import db


# ---------------------------------------------------------------------
# Usuário
# ---------------------------------------------------------------------
class User(UserMixin, db.Model):
    __tablename__ = "user"  # se sua tabela for "users", troque aqui e nos ForeignKeys abaixo

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), unique=True, index=True, nullable=False)
    email = db.Column(db.String(255), unique=True, index=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)

    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # relacionamento principal (dono do processo)
    processes = db.relationship(
        "Process",
        back_populates="owner",
        foreign_keys="Process.owner_id",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    # relacionamentos auxiliares (opcionais)
    created_processes = db.relationship(
        "Process",
        foreign_keys="Process.created_by",
        backref="creator",
        lazy="dynamic",
    )
    updated_processes = db.relationship(
        "Process",
        foreign_keys="Process.updated_by",
        backref="updater",
        lazy="dynamic",
    )

    # Helpers de senha
    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def __repr__(self) -> str:
        return f"<User {self.username}>"


# ---------------------------------------------------------------------
# Processo
# ---------------------------------------------------------------------
class Process(db.Model):
    __tablename__ = "process"

    id = db.Column(db.Integer, primary_key=True)

    # 🔑 Dono / responsável (com FKs)
    owner_id   = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    updated_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    # lado inverso do relacionamento principal
    owner = db.relationship("User", back_populates="processes", foreign_keys=[owner_id])

    # Campos do cabeçalho
    cnj = db.Column(db.String(3))                 # "Sim" / "Não" (ou bool, se preferir)
    tipo_processo = db.Column(db.String(20))      # "Eletrônico" / "Físico"

    numero_processo = db.Column(db.String(50), index=True)
    numero_processo_antigo = db.Column(db.String(50))
    sistema_eletronico = db.Column(db.String(60))

    area_direito = db.Column(db.String(60))
    sub_area_direito = db.Column(db.String(120))

    estado = db.Column(db.String(2))
    comarca = db.Column(db.String(120))
    numero_orgao = db.Column(db.String(20))
    origem = db.Column(db.String(60))
    orgao = db.Column(db.String(160))
    vara = db.Column(db.String(160))
    celula = db.Column(db.String(160))
    foro = db.Column(db.String(160))
    instancia = db.Column(db.String(60))

    assunto = db.Column(db.String(255))
    npc = db.Column(db.String(60))
    objeto = db.Column(db.Text)
    sub_objeto = db.Column(db.Text)

    # Strings para evitar ALTER TYPE no SQLite
    audiencia_inicial = db.Column(db.String(25))            # "YYYY-MM-DD HH:MM:SS"
    link_audiencia = db.Column(db.String(500))               # Link Zoom/Meet/Teams para audiência telepresencial
    subtipo_audiencia = db.Column(db.String(120))            # "Una", "Não-Una", "Tentativa Conciliação", etc
    envolvido_audiencia = db.Column(db.String(80))           # "Advogado", "Preposto", "Advogado e Preposto", etc
    data_hora_cadastro_manual = db.Column(db.String(25))    # "YYYY-MM-DD HH:MM:SS"

    # Complementares / RAG
    cliente_parte = db.Column(db.Text)
    advogado_autor = db.Column(db.String(255))
    advogado_reu = db.Column(db.String(255))
    prazo = db.Column(db.String(120))
    tipo_notificacao = db.Column(db.String(255))
    resultado_audiencia = db.Column(db.Text)
    prazos_derivados_audiencia = db.Column(db.Text)
    decisao_tipo = db.Column(db.String(255))
    decisao_resultado = db.Column(db.String(255))
    decisao_fundamentacao_resumida = db.Column(db.Text)

    id_interno_hilo = db.Column(db.String(120))

    estrategia = db.Column(db.String(120))
    indice_atualizacao = db.Column(db.String(120))

    # Parte interessada / parte adversa
    posicao_parte_interessada = db.Column(db.String(60))     # "AUTOR..." / "RÉU..."
    parte_interessada = db.Column(db.String(200))

    parte_adversa_tipo = db.Column(db.String(50))            # "FISICA" / "JURIDICA" / "PESSOA FISICA" / "PESSOA JURIDICA"
    parte_adversa_nome = db.Column(db.String(200))
    escritorio_parte_adversa = db.Column(db.String(200))
    uf_oab_advogado_adverso = db.Column(db.String(5))
    cpf_cnpj_parte_adversa = db.Column(db.String(30))
    telefone_parte_adversa = db.Column(db.String(50))
    email_parte_adversa = db.Column(db.String(200))
    endereco_parte_adversa = db.Column(db.String(300))

    data_distribuicao = db.Column(db.String(50))
    data_citacao = db.Column(db.String(50))
    risco = db.Column(db.String(50))
    valor_causa = db.Column(db.String(30))
    rito = db.Column(db.String(120))
    observacao = db.Column(db.String(300))

    cadastrar_primeira_audiencia = db.Column(db.Boolean, default=False, nullable=False)
    
    # Campos adicionais
    cliente = db.Column(db.String(200))
    outra_reclamada_cliente = db.Column(db.String(200), nullable=True)  # Para casos de múltiplos clientes no polo (ex: CBSI + CSN onde CBSI é principal)
    parte = db.Column(db.String(200))
    pdf_filename = db.Column(db.String(255))
    
    # Status do preenchimento no eLaw
    elaw_status = db.Column(db.String(20), default='pending', nullable=False)  # pending, running, success, error
    elaw_filled_at = db.Column(db.DateTime, nullable=True)  # Quando foi preenchido com sucesso
    elaw_error_message = db.Column(db.Text, nullable=True)  # Mensagem de erro se falhou
    
    # Screenshots do RPA (antes e depois de salvar)
    elaw_screenshot_before_path = db.Column(db.String(500), nullable=True)  # Screenshot do formulário preenchido (ANTES de clicar Salvar)
    elaw_screenshot_after_path = db.Column(db.String(500), nullable=True)   # Screenshot após salvar (sucesso ou erro)
    elaw_screenshot_path = db.Column(db.String(500), nullable=True)  # DEPRECATED: usar elaw_screenshot_before_path
    
    # ✅ MÚLTIPLAS RECLAMADAS - URLs e screenshots
    elaw_detail_url = db.Column(db.String(500), nullable=True)  # URL da tela de detalhes do processo no eLaw
    elaw_screenshot_reclamadas_path = db.Column(db.String(500), nullable=True)  # Screenshot da aba "Partes e Advogados" após inserir reclamadas extras
    elaw_screenshot_pedidos_path = db.Column(db.String(500), nullable=True)  # Screenshot da aba "Pedidos" após inserir pedidos
    
    # ✅ DADOS TRABALHISTAS - Informações específicas de processos trabalhistas
    data_admissao = db.Column(db.String(50), nullable=True)          # Data de admissão do trabalhador (DD/MM/AAAA)
    data_demissao = db.Column(db.String(50), nullable=True)          # Data de demissão/dispensa (DD/MM/AAAA)
    salario = db.Column(db.String(50), nullable=True)                # Salário do trabalhador (R$ X.XXX,XX)
    cargo_funcao = db.Column(db.String(200), nullable=True)          # Cargo/Função exercida
    empregador = db.Column(db.String(300), nullable=True)            # Nome da empresa empregadora
    pis = db.Column(db.String(20), nullable=True)                    # Número do PIS (XXX.XXXXX.XX-X)
    ctps = db.Column(db.String(50), nullable=True)                   # CTPS (número e série)
    local_trabalho = db.Column(db.String(300), nullable=True)        # Local de trabalho/prestação de serviços
    motivo_demissao = db.Column(db.String(100), nullable=True)       # Motivo da demissão (Sem Justa Causa, Rescisão Indireta, etc)
    
    # ✅ PEDIDOS - Lista de pedidos extraídos do PDF (JSON)
    pedidos_json = db.Column(db.Text, nullable=True)                 # JSON com lista de pedidos extraídos
    
    # ✅ METADADOS DO PDF - Para mapeamento inteligente de anexos
    pdf_total_pages = db.Column(db.Integer, nullable=True)           # Total de páginas do PDF original

    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    def __repr__(self) -> str:
        return f"<Process {self.numero_processo or self.id}>"

    # Campos críticos que devem estar preenchidos para RPA (todos os campos do fluxo eLaw)
    CRITICAL_FIELDS = [
        # Identificação do processo
        'numero_processo',
        'area_direito',
        # Localização
        'estado',
        'comarca',
        'numero_orgao',
        'orgao',
        'celula',
        'foro',
        'instancia',
        # Assunto/Objeto
        'assunto',
        # Partes
        'cliente',
        'posicao_parte_interessada',
        'parte_interessada',
        'parte_adversa_tipo',
        'parte_adversa_nome',
        # Datas
        'data_distribuicao',
        'data_admissao',
        'data_demissao',
        # Dados trabalhistas
        'salario',
        'cargo_funcao',
        'empregador',
        'local_trabalho',
        'motivo_demissao',
        'pis',
        'ctps',
        # Valor
        'valor_causa',
    ]
    
    CRITICAL_FIELD_LABELS = {
        # Identificação do processo
        'numero_processo': 'Número CNJ',
        'area_direito': 'Área do Direito',
        # Localização
        'estado': 'Estado',
        'comarca': 'Comarca',
        'numero_orgao': 'Nº Órgão',
        'orgao': 'Órgão',
        'celula': 'Célula',
        'foro': 'Foro',
        'instancia': 'Instância',
        # Assunto/Objeto
        'assunto': 'Assunto',
        # Partes
        'cliente': 'Cliente',
        'posicao_parte_interessada': 'Posição',
        'parte_interessada': 'Parte Interessada',
        'parte_adversa_tipo': 'Tipo Parte Adversa',
        'parte_adversa_nome': 'Nome Parte Adversa',
        # Datas
        'data_distribuicao': 'Data Distribuição',
        'data_admissao': 'Data Admissão',
        'data_demissao': 'Data Demissão',
        # Dados trabalhistas
        'salario': 'Salário',
        'cargo_funcao': 'Cargo',
        'empregador': 'Empregador',
        'local_trabalho': 'Local Trabalho',
        'motivo_demissao': 'Motivo Demissão',
        'pis': 'PIS',
        'ctps': 'CTPS',
        # Valor
        'valor_causa': 'Valor da Causa',
    }

    def get_missing_critical_fields(self) -> list:
        """Retorna lista de campos críticos que estão vazios."""
        missing = []
        for field in self.CRITICAL_FIELDS:
            value = getattr(self, field, None)
            if not value or (isinstance(value, str) and not value.strip()):
                missing.append(self.CRITICAL_FIELD_LABELS.get(field, field))
        return missing

    def has_missing_critical_fields(self) -> bool:
        """Verifica se há campos críticos faltando."""
        return len(self.get_missing_critical_fields()) > 0

    @property
    def critical_fields_complete(self) -> bool:
        """Retorna True se todos os campos críticos estão preenchidos."""
        return not self.has_missing_critical_fields()

    # Helper opcional para preencher a partir do WTForm
    def fill_from_form(self, form) -> None:
        for field in (
            "cnj", "tipo_processo", "numero_processo", "numero_processo_antigo",
            "sistema_eletronico", "area_direito", "sub_area_direito", "estado",
            "comarca", "numero_orgao", "origem", "orgao", "vara", "celula", "foro",
            "instancia", "assunto", "npc", "objeto", "sub_objeto", "audiencia_inicial",
            "cliente_parte", "advogado_autor", "advogado_reu", "prazo",
            "tipo_notificacao", "resultado_audiencia", "prazos_derivados_audiencia",
            "decisao_tipo", "decisao_resultado", "decisao_fundamentacao_resumida",
            "id_interno_hilo", "data_hora_cadastro_manual", "estrategia",
            "indice_atualizacao", "posicao_parte_interessada", "parte_interessada",
            "parte_adversa_tipo", "parte_adversa_nome", "escritorio_parte_adversa",
            "uf_oab_advogado_adverso", "cpf_cnpj_parte_adversa", "telefone_parte_adversa",
            "email_parte_adversa", "endereco_parte_adversa", "data_distribuicao",
            "data_citacao", "risco", "valor_causa", "rito", "observacao",
            "cliente", "outra_reclamada_cliente", "parte",
            # Dados trabalhistas
            "data_admissao", "data_demissao", "salario", "cargo_funcao", "empregador",
            "pis", "ctps", "local_trabalho", "motivo_demissao",
        ):
            if hasattr(form, field):
                setattr(self, field, getattr(form, field).data)

        if hasattr(form, "cadastrar_primeira_audiencia"):
            self.cadastrar_primeira_audiencia = (
                getattr(form, "cadastrar_primeira_audiencia").data == "Sim"
            )


# ---------------------------------------------------------------------
# Utilitário opcional para “seed” do admin no primeiro run
# ---------------------------------------------------------------------
def ensure_admin_user():
    """Cria um admin padrão usando credenciais dos secrets."""
    import os
    admin_username = os.environ.get("ADMIN_USERNAME", "admin")
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")
    
    if not User.query.filter_by(username=admin_username).first():
        u = User(username=admin_username, email=f"{admin_username}@local", is_admin=True)
        u.set_password(admin_password)
        db.session.add(u)
        db.session.commit()


# ---------------------------------------------------------------------
# Eventos para preencher created_by / updated_by automaticamente
# ---------------------------------------------------------------------
@event.listens_for(Process, "before_insert")
def _set_created_by(mapper, connection, target: Process):
    # tenta o usuário logado; se não houver, use o owner_id
    try:
        uid = current_user.id if current_user and not current_user.is_anonymous else target.owner_id
    except Exception:
        uid = target.owner_id
    if not target.created_by:
        target.created_by = uid
    target.updated_by = uid  # opcional: iguala no insert


@event.listens_for(Process, "before_update")
def _set_updated_by(mapper, connection, target: Process):
    try:
        if current_user and not current_user.is_anonymous:
            target.updated_by = current_user.id
    except Exception:
        pass


# ---------------------------------------------------------------------
# Status do RPA (progresso em tempo real)
# ---------------------------------------------------------------------
class RPAStatus(db.Model):
    __tablename__ = "rpa_status"
    
    id = db.Column(db.Integer, primary_key=True)
    process_id = db.Column(db.Integer, db.ForeignKey("process.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # Status geral: starting, running, completed, error
    status = db.Column(db.String(20), nullable=False, default="starting")
    
    # Etapa atual (ex: "login", "preenchendo_cnj", "salvando")
    current_step = db.Column(db.String(100))
    
    # Mensagem descritiva
    message = db.Column(db.Text)
    
    # Dados adicionais (JSON serializado)
    data_json = db.Column(db.Text)  # JSON com dados do campo preenchido
    
    # Histórico de steps (JSON serializado - array de {step, message, timestamp, data})
    history_json = db.Column(db.Text)
    
    # Timestamps
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Relacionamento com processo
    process = db.relationship("Process", backref=db.backref("rpa_statuses", cascade="all, delete-orphan"))
    
    def __repr__(self) -> str:
        return f"<RPAStatus process_id={self.process_id} status={self.status!r} step={self.current_step!r}>"


# ---------------------------------------------------------------------
# Batch Upload (processamento em lote)
# ---------------------------------------------------------------------
class BatchUpload(db.Model):
    __tablename__ = "batch_upload"
    
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    
    # Status: pending, extracting, ready, running, completed, partial_completed, error
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    
    # Contadores
    total_count = db.Column(db.Integer, nullable=False, default=0)
    processed_count = db.Column(db.Integer, nullable=False, default=0)
    
    # Lock para processamento (task ID do Celery)
    lock_owner = db.Column(db.String(100), nullable=True)
    
    # Timestamps
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    
    # Relacionamentos
    owner = db.relationship("User", foreign_keys=[owner_id])
    items = db.relationship("BatchItem", back_populates="batch", cascade="all, delete-orphan", lazy="dynamic")
    
    def __repr__(self) -> str:
        return f"<BatchUpload id={self.id} status={self.status!r} {self.processed_count}/{self.total_count}>"


# ---------------------------------------------------------------------
# Localização de Anexos (Inteligência para OCR)
# ---------------------------------------------------------------------
class AnnexLocation(db.Model):
    """
    Armazena onde cada tipo de documento (CTPS, TRCT, Contracheque) foi 
    encontrado em PDFs processados. Usado para inferir localizações em 
    novos PDFs sem bookmarks.
    
    Exemplo: Em um PDF de 100 páginas, a CTPS foi encontrada na página 85.
    page_ratio = 85/100 = 0.85 (85% do PDF)
    
    Estatísticas agregadas são calculadas por doc_type para inferência.
    """
    __tablename__ = "annex_location"
    
    id = db.Column(db.Integer, primary_key=True)
    process_id = db.Column(db.Integer, db.ForeignKey("process.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # Tipo de documento: ctps, trct, contracheque
    doc_type = db.Column(db.String(20), nullable=False, index=True)
    
    # Página onde foi encontrado (1-indexed)
    page_number = db.Column(db.Integer, nullable=False)
    
    # Total de páginas do PDF (para calcular ratio)
    total_pages = db.Column(db.Integer, nullable=False)
    
    # Ratio = page_number / total_pages (ex: 0.85 = 85% do PDF)
    page_ratio = db.Column(db.Float, nullable=False)
    
    # Fonte da informação: bookmark, toc, ocr_found
    source = db.Column(db.String(20), nullable=False, default="ocr_found")
    
    # Confiança: 1.0 = bookmark, 0.9 = toc, 0.7 = ocr
    confidence = db.Column(db.Float, nullable=False, default=0.7)
    
    # Timestamps
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    
    # Relacionamento
    process = db.relationship("Process", backref=db.backref("annex_locations", cascade="all, delete-orphan"))
    
    def __repr__(self) -> str:
        return f"<AnnexLocation {self.doc_type} page={self.page_number}/{self.total_pages} ({self.page_ratio:.0%})>"


class BatchItem(db.Model):
    __tablename__ = "batch_item"
    
    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batch_upload.id", ondelete="CASCADE"), nullable=False, index=True)
    process_id = db.Column(db.Integer, db.ForeignKey("process.id", ondelete="SET NULL"), nullable=True, index=True)
    
    # Arquivo fonte
    source_filename = db.Column(db.String(255), nullable=False)
    upload_path = db.Column(db.String(500), nullable=False)
    file_size = db.Column(db.BigInteger, nullable=True, index=True)  # Tamanho em bytes (para ordenação)
    
    # Status: pending, extracting, ready, running, success, error
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    
    # Controle de retry
    attempt_count = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.Text, nullable=True)
    
    # Timestamps
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Relacionamentos (especificar foreign_keys para evitar ambiguidade)
    batch = db.relationship("BatchUpload", back_populates="items")
    process = db.relationship("Process", foreign_keys=[process_id])
    
    def __repr__(self) -> str:
        return f"<BatchItem id={self.id} batch_id={self.batch_id} status={self.status!r}>"
