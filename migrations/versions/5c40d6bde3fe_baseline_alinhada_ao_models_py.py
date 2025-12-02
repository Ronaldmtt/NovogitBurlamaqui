"""baseline alinhada ao models.py

Revision ID: 5c40d6bde3fe
Revises:
Create Date: 2025-11-03 15:58:46.195143

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5c40d6bde3fe'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) DROP TABLEs antigos com segurança (IF EXISTS)
    # Em SQLite, Alembic não tem "if_exists" nativo em op.drop_table,
    # então usamos SQL direto:
    op.execute("DROP TABLE IF EXISTS pdf_chunk")
    op.execute("DROP TABLE IF EXISTS process_analysis")

    # 2) Alterações na tabela process
    # IMPORTANTE:
    # - Remova QUALQUER linha "batch_op.drop_constraint(..., type_='foreignkey')" sem nome.
    # - Se sua migration tinha esse drop de FK antiga (created_by), APAGUE essa linha.
    with op.batch_alter_table('process', recreate='always') as batch_op:
        # Exemplo de alterações (adicione/ajuste aqui o que seu arquivo mostrou no autogenerate):
        # Se no seu autogenerate apareceu "add_column" / "alter_column" / "create_index",
        # coloque-os aqui; abaixo são exemplos típicos com base no seu log:

        # Adições:
        batch_op.add_column(sa.Column('owner_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('advogado_autor', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('advogado_reu', sa.String(length=120), nullable=True))

        # Alterações (ajuste nullable/length conforme seu autogenerate):
        batch_op.alter_column('cnj', type_=sa.String(length=3), existing_type=sa.String(length=100), existing_nullable=True)
        batch_op.alter_column('tipo_processo', type_=sa.String(length=20), existing_type=sa.String(length=100), existing_nullable=True)
        batch_op.alter_column('numero_processo', type_=sa.String(length=50), existing_type=sa.String(length=100), existing_nullable=True)
        batch_op.alter_column('numero_processo_antigo', type_=sa.String(length=50), existing_type=sa.String(length=100), existing_nullable=True)
        batch_op.alter_column('sistema_eletronico', type_=sa.String(length=60), existing_type=sa.String(length=100), existing_nullable=True)
        batch_op.alter_column('area_direito', type_=sa.String(length=60), existing_type=sa.String(length=100), existing_nullable=True)
        batch_op.alter_column('sub_area_direito', type_=sa.String(length=120), existing_type=sa.String(length=100), existing_nullable=True)
        batch_op.alter_column('estado', type_=sa.String(length=2), existing_type=sa.String(length=50), existing_nullable=True)
        batch_op.alter_column('comarca', type_=sa.String(length=120), existing_type=sa.String(length=100), existing_nullable=True)
        batch_op.alter_column('numero_orgao', type_=sa.String(length=20), existing_type=sa.String(length=50), existing_nullable=True)
        batch_op.alter_column('origem', type_=sa.String(length=60), existing_type=sa.String(length=100), existing_nullable=True)
        batch_op.alter_column('orgao', type_=sa.String(length=160), existing_type=sa.String(length=100), existing_nullable=True)
        batch_op.alter_column('vara', type_=sa.String(length=160), existing_type=sa.String(length=100), existing_nullable=True)
        batch_op.alter_column('celula', type_=sa.String(length=160), existing_type=sa.String(length=100), existing_nullable=True)
        batch_op.alter_column('foro', type_=sa.String(length=160), existing_type=sa.String(length=100), existing_nullable=True)
        batch_op.alter_column('instancia', type_=sa.String(length=60), existing_type=sa.String(length=100), existing_nullable=True)
        batch_op.alter_column('assunto', type_=sa.String(length=255), existing_type=sa.Text(), existing_nullable=True)
        batch_op.alter_column('npc', type_=sa.String(length=60), existing_type=sa.String(length=100), existing_nullable=True)
        batch_op.alter_column('objeto', type_=sa.String(length=255), existing_type=sa.Text(), existing_nullable=True)
        batch_op.alter_column('audiencia_inicial', type_=sa.String(length=25), existing_type=sa.DateTime(), existing_nullable=True)
        batch_op.alter_column('data_hora_cadastro_manual', type_=sa.String(length=25), existing_type=sa.DateTime(), existing_nullable=True)
        batch_op.alter_column('cliente_parte', type_=sa.Text(), existing_type=sa.String(length=300), existing_nullable=True)
        batch_op.alter_column('prazo', type_=sa.String(length=120), existing_type=sa.String(length=120), existing_nullable=True)
        batch_op.alter_column('tipo_notificacao', type_=sa.String(length=255), existing_type=sa.String(length=120), existing_nullable=True)
        batch_op.alter_column('resultado_audiencia', type_=sa.Text(), existing_type=sa.String(length=300), existing_nullable=True)
        batch_op.alter_column('prazos_derivados_audiencia', type_=sa.Text(), existing_type=sa.String(length=500), existing_nullable=True)
        batch_op.alter_column('decisao_tipo', type_=sa.String(length=255), existing_type=sa.String(length=120), existing_nullable=True)
        batch_op.alter_column('decisao_resultado', type_=sa.String(length=255), existing_type=sa.String(length=500), existing_nullable=True)
        batch_op.alter_column('estrategia', type_=sa.String(length=120), existing_type=sa.String(length=500), existing_nullable=True)
        batch_op.alter_column('posicao_parte_interessada', type_=sa.String(length=60), existing_type=sa.String(length=120), existing_nullable=True)
        batch_op.alter_column('parte_interessada', type_=sa.String(length=200), existing_type=sa.String(length=300), existing_nullable=True)
        batch_op.alter_column('parte_adversa_tipo', type_=sa.String(length=10), existing_type=sa.String(length=120), existing_nullable=True)
        batch_op.alter_column('parte_adversa_nome', type_=sa.String(length=200), existing_type=sa.String(length=300), existing_nullable=True)
        batch_op.alter_column('escritorio_parte_adversa', type_=sa.String(length=200), existing_type=sa.String(length=300), existing_nullable=True)
        batch_op.alter_column('valor_causa', type_=sa.String(length=30), existing_type=sa.String(length=50), existing_nullable=True)
        batch_op.alter_column('observacao', type_=sa.String(length=300), existing_type=sa.Text(), existing_nullable=True)

        # Remoções apontadas pelo autogenerate (se constarem na sua migration):
        # batch_op.drop_column('pdf_filename')
        # batch_op.drop_column('cliente')
        # batch_op.drop_column('created_by')
        # batch_op.drop_column('parte')

        # Índices novos (conforme autogenerate):
        batch_op.create_index('ix_process_numero_processo', ['numero_processo'], unique=False)
        batch_op.create_index('ix_process_owner_id', ['owner_id'], unique=False)

        # FK nova (se constar no autogenerate):
        batch_op.create_foreign_key(
            'fk_process_owner_id_user',
            'user',
            ['owner_id'],
            ['id'],
        )

    # 3) Alterações na tabela user
    with op.batch_alter_table('user', recreate='always') as batch_op:
        # Exemplo com base no seu log:
        batch_op.add_column(sa.Column('updated_at', sa.DateTime(), nullable=True))
        # is_admin virou NOT NULL com default => em SQLite, o "server_default" pode ser recriado:
        batch_op.alter_column('is_admin', existing_type=sa.Boolean(), nullable=False, server_default=sa.text('0'))
        batch_op.alter_column('created_at', existing_type=sa.DateTime(), nullable=False, server_default=sa.text("(DATETIME('now'))"))
        batch_op.create_index('ix_user_email', ['email'], unique=True)
        batch_op.create_index('ix_user_username', ['username'], unique=True)


def downgrade() -> None:
    # Faça o inverso, também com segurança:
    # Reverter índices
    with op.batch_alter_table('user', recreate='always') as batch_op:
        batch_op.drop_index('ix_user_username')
        batch_op.drop_index('ix_user_email')
        batch_op.alter_column('created_at', existing_type=sa.DateTime(), nullable=True, server_default=None)
        batch_op.alter_column('is_admin', existing_type=sa.Boolean(), nullable=True, server_default=None)
        batch_op.drop_column('updated_at')

    with op.batch_alter_table('process', recreate='always') as batch_op:
        batch_op.drop_constraint('fk_process_owner_id_user', type_='foreignkey')
        batch_op.drop_index('ix_process_owner_id')
        batch_op.drop_index('ix_process_numero_processo')

        # Reverta aqui os tipos/colunas conforme necessário (espelhe o que foi feito no upgrade):
        batch_op.drop_column('advogado_reu')
        batch_op.drop_column('advogado_autor')
        batch_op.drop_column('owner_id')

    # Se você quiser recriar as tabelas removidas no downgrade:
    op.execute("""
        CREATE TABLE IF NOT EXISTS pdf_chunk (
            id INTEGER PRIMARY KEY,
            -- defina colunas mínimas se precisar realmente reverter
            dummy TEXT
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS process_analysis (
            id INTEGER PRIMARY KEY,
            -- defina colunas mínimas se precisar realmente reverter
            dummy TEXT
        )
    """)

