"""add updated_by to process

Revision ID: 07e5716f0046
Revises: 5c40d6bde3fe
Create Date: 2025-11-04 19:01:39.866351
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '07e5716f0046'
down_revision = '5c40d6bde3fe'
branch_labels = None
depends_on = None


FK_NAME = "fk_process_updated_by_user_id"


def upgrade():
    # Use batch_alter_table p/ compatibilidade com SQLite
    with op.batch_alter_table('process', schema=None) as batch_op:
        # 1) Add column nullable primeiro
        batch_op.add_column(sa.Column('updated_by', sa.Integer(), nullable=True))

    # 2) Backfill: copie de owner_id (ajuste se a sua regra for outra)
    # Obs: use schema-qualified se precisar.
    conn = op.get_bind()
    conn.execute(sa.text("""
        UPDATE process
        SET updated_by = owner_id
        WHERE updated_by IS NULL
    """))

    # 3) Crie a FK com nome explícito
    with op.batch_alter_table('process', schema=None) as batch_op:
        batch_op.create_foreign_key(
            FK_NAME,
            'user',  # tabela referenciada
            ['updated_by'], ['id'],
            onupdate='CASCADE',
            ondelete='SET NULL'  # troque para 'RESTRICT' / 'CASCADE' se preferir
        )

    # 4) (Opcional) Torne NOT NULL se a regra de negócio exigir
    #    Só faça isso se o backfill cobriu todos os registros.
    #    Se você quer permitir NULL (ex.: registros antigos), remova este bloco.
    make_not_null = True  # mude para False se não quiser NOT NULL
    if make_not_null:
        with op.batch_alter_table('process', schema=None) as batch_op:
            batch_op.alter_column(
                'updated_by',
                existing_type=sa.Integer(),
                nullable=False
            )


def downgrade():
    # Reverte NOT NULL, remove FK, dropa coluna
    with op.batch_alter_table('process', schema=None) as batch_op:
        # Se ficou NOT NULL no upgrade, volte a permitir NULL antes de dropar FK/coluna
        try:
            batch_op.alter_column(
                'updated_by',
                existing_type=sa.Integer(),
                nullable=True
            )
        except Exception:
            # Alguns bancos permitem dropar direto; ignore se não aplicável
            pass

        # Drop FK pelo nome
        batch_op.drop_constraint(FK_NAME, type_='foreignkey')

        # Drop column
        batch_op.drop_column('updated_by')
