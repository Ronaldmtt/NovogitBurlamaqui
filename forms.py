from __future__ import annotations

from flask_wtf import FlaskForm
from wtforms import (
    StringField,
    PasswordField,
    SubmitField,
    TextAreaField,
    SelectField,
    RadioField,
)
from wtforms.validators import DataRequired, Email, Length, Optional, EqualTo

__all__ = ["LoginForm", "CreateUserForm", "ProcessForm", "SearchForm"]

# =========================
# Autenticação
# =========================
class LoginForm(FlaskForm):
    username = StringField("Usuário ou E-mail", validators=[DataRequired(), Length(max=255)])
    password = PasswordField("Senha", validators=[DataRequired(), Length(max=255)])
    submit = SubmitField("Entrar")


class CreateUserForm(FlaskForm):
    name = StringField("Nome", validators=[DataRequired(), Length(max=255)])
    email = StringField("E-mail", validators=[DataRequired(), Email(), Length(max=255)])
    password = PasswordField("Senha", validators=[DataRequired(), Length(min=6, max=255)])
    confirm_password = PasswordField(
        "Confirmar Senha",
        validators=[DataRequired(), EqualTo("password", message="As senhas devem coincidir.")]
    )
    role = SelectField(
        "Papel",
        choices=[("user", "Usuário"), ("admin", "Administrador")],
        validators=[Optional()]
    )
    submit = SubmitField("Criar Usuário")


# =========================
# Processos
# =========================
_UF_CHOICES = [
    ("", ""),
    ("AC", "AC"), ("AL", "AL"), ("AP", "AP"), ("AM", "AM"), ("BA", "BA"),
    ("CE", "CE"), ("DF", "DF"), ("ES", "ES"), ("GO", "GO"), ("MA", "MA"),
    ("MT", "MT"), ("MS", "MS"), ("MG", "MG"), ("PA", "PA"), ("PB", "PB"),
    ("PR", "PR"), ("PE", "PE"), ("PI", "PI"), ("RJ", "RJ"), ("RN", "RN"),
    ("RS", "RS"), ("RO", "RO"), ("RR", "RR"), ("SC", "SC"), ("SP", "SP"),
    ("SE", "SE"), ("TO", "TO"),
]

_AREA_CHOICES = [
    ("", ""),
    ("Trabalhista", "Trabalhista"),
    ("Cível", "Cível"),
    ("Criminal", "Criminal"),
    ("Previdenciário", "Previdenciário"),
    ("Administrativo", "Administrativo"),
    ("Tributário", "Tributário"),
    ("Outros", "Outros"),
]

_INSTANCIA_CHOICES = [
    ("", ""),
    ("Primeira Instância", "Primeira Instância"),
    ("Segunda Instância", "Segunda Instância"),
    ("Superior", "Superior"),
]

_RISCO_CHOICES = [("", ""), ("Baixo", "Baixo"), ("Médio", "Médio"), ("Alto", "Alto")]
_RITO_CHOICES = [("", ""), ("Ordinário", "Ordinário"), ("Sumaríssimo", "Sumaríssimo"), ("Sumário", "Sumário")]
_INDICE_CHOICES = [("", ""), ("IPCA-E", "IPCA-E"), ("TR", "TR"), ("SELIC", "SELIC")]
_ESTRATEGIA_CHOICES = [("", ""), ("Defensiva", "Defensiva"), ("Negocial", "Negocial"), ("Ativa", "Ativa")]
_POSICAO_CHOICES = [("", ""), ("AUTOR", "AUTOR / RECLAMANTE"), ("REU", "RÉU / RECLAMADO")]


class ProcessForm(FlaskForm):
    # Campos principais
    cnj = RadioField(
        "CNJ",
        choices=[("Sim", "Sim"), ("Não", "Não")],
        default="Sim",
        validators=[DataRequired()]
    )
    tipo_processo = RadioField(
        "Tipo de Processo",
        choices=[("Eletrônico", "Eletrônico"), ("Físico", "Físico")],
        default="Eletrônico",
        validators=[DataRequired()]
    )

    numero_processo = StringField("Número do Processo", validators=[Optional(), Length(max=50)])
    numero_processo_antigo = StringField("Número do Processo (Antigo)", validators=[Optional(), Length(max=50)])
    sistema_eletronico = StringField("Sistema Eletrônico", validators=[Optional(), Length(max=60)])

    area_direito = SelectField("Área do Direito", choices=_AREA_CHOICES, validators=[Optional()])
    sub_area_direito = StringField("Sub-área do Direito", validators=[Optional(), Length(max=120)])

    estado = SelectField("Estado", choices=_UF_CHOICES, validators=[Optional()])
    comarca = StringField("Comarca", validators=[Optional(), Length(max=120)])
    numero_orgao = StringField("Número do Órgão/Unidade", validators=[Optional(), Length(max=20)])
    origem = StringField("Origem (ex.: TRT, TJ, JF)", validators=[Optional(), Length(max=60)])
    orgao = StringField("Órgão (ex.: Vara do Trabalho, Vara Cível)", validators=[Optional(), Length(max=160)])
    vara = StringField("Vara", validators=[Optional(), Length(max=160)])
    celula = StringField("Célula (se aplicável)", validators=[Optional(), Length(max=160)])
    foro = StringField("Foro", validators=[Optional(), Length(max=160)])
    instancia = SelectField("Instância", choices=_INSTANCIA_CHOICES, validators=[Optional()])

    assunto = StringField("Assunto", validators=[Optional(), Length(max=255)])
    npc = StringField("NPC", validators=[Optional(), Length(max=60)])
    objeto = TextAreaField("Objeto", validators=[Optional(), Length(max=4000)])
    sub_objeto = TextAreaField("Sub-objeto", validators=[Optional(), Length(max=4000)])

    audiencia_inicial = StringField(
        "Audiência Inicial",
        validators=[Optional(), Length(max=25)],
        render_kw={"placeholder": "YYYY-MM-DD HH:MM:SS"}
    )

    # Complementares (RAG)
    cliente_parte = TextAreaField("Cliente/Parte (JSON)", validators=[Optional()])
    advogado_autor = StringField("Advogado(a) do Autor", validators=[Optional(), Length(max=255)])
    advogado_reu = StringField("Advogado(a) do Réu", validators=[Optional(), Length(max=255)])
    prazo = StringField("Prazo", validators=[Optional(), Length(max=120)])
    tipo_notificacao = StringField("Tipo de Notificação", validators=[Optional(), Length(max=255)])
    resultado_audiencia = TextAreaField("Resultado da Audiência", validators=[Optional()])
    prazos_derivados_audiencia = TextAreaField("Prazos Derivados da Audiência", validators=[Optional()])
    decisao_tipo = StringField("Tipo da Decisão", validators=[Optional(), Length(max=255)])
    decisao_resultado = StringField("Resultado da Decisão", validators=[Optional(), Length(max=255)])
    decisao_fundamentacao_resumida = TextAreaField("Fundamentação Resumida", validators=[Optional()])

    id_interno_hilo = StringField("ID Interno Hilo", validators=[Optional(), Length(max=120)])
    data_hora_cadastro_manual = StringField(
        "Data/Hora Cadastro Manual",
        validators=[Optional(), Length(max=25)],
        render_kw={"placeholder": "YYYY-MM-DD HH:MM:SS"}
    )

    # Estratégia / Índice
    estrategia = SelectField("Estratégia", choices=_ESTRATEGIA_CHOICES, validators=[Optional()])
    indice_atualizacao = SelectField("Índice Atualização Monetária", choices=_INDICE_CHOICES, validators=[Optional()])

    # Dados do Cliente e Parte Adversa
    posicao_parte_interessada = SelectField(
        "Posição Parte Interessada*",
        choices=_POSICAO_CHOICES,
        validators=[Optional()]
    )
    parte_interessada = StringField("Parte Interessada*", validators=[Optional(), Length(max=200)])

    parte_adversa_tipo = RadioField(
        "Parte Adversa (Tipo)*",
        choices=[("FISICA", "Física"), ("JURIDICA", "Jurídica")],
        validators=[Optional()]
    )
    parte_adversa_nome = StringField("Parte Adversa (Nome)*", validators=[Optional(), Length(max=200)])
    escritorio_parte_adversa = StringField("Escritório parte adversa", validators=[Optional(), Length(max=200)])
    uf_oab_advogado_adverso = SelectField("UF OAB Advogado Adverso", choices=_UF_CHOICES, validators=[Optional()])
    cpf_cnpj_parte_adversa = StringField("CPF/CNPJ - Parte Adversa", validators=[Optional(), Length(max=25)])
    telefone_parte_adversa = StringField("Telefone - Parte Adversa", validators=[Optional(), Length(max=30)])
    email_parte_adversa = StringField("Email Parte Adversa", validators=[Optional(), Email(), Length(max=120)])
    endereco_parte_adversa = StringField("Endereço - Parte Adversa", validators=[Optional(), Length(max=300)])

    # Outras Informações
    data_distribuicao = StringField("Data de Distribuição", validators=[Optional(), Length(max=10)])
    data_citacao = StringField("Data da Citação", validators=[Optional(), Length(max=10)])
    risco = SelectField("Risco", choices=_RISCO_CHOICES, validators=[Optional()])
    valor_causa = StringField("Valor da Causa*", validators=[Optional(), Length(max=30)])
    rito = SelectField("Rito", choices=_RITO_CHOICES, validators=[Optional()])
    observacao = TextAreaField("Observação (Breve Relato)", validators=[Optional(), Length(max=300)])
    cadastrar_primeira_audiencia = RadioField(
        "Deseja cadastrar a primeira Audiência?",
        choices=[("Sim", "Sim"), ("Não", "Não")],
        default="Não",
        validators=[Optional()]
    )

    submit = SubmitField("Salvar")


# =========================
# Busca
# =========================
class SearchForm(FlaskForm):
    q = StringField("Buscar", validators=[Optional(), Length(max=255)])
    submit = SubmitField("Pesquisar")
