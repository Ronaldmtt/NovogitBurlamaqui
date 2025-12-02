"""add_elaw_screenshot_before_after_paths

Revision ID: 5c3dd8521bc9
Revises: 07e5716f0046
Create Date: 2025-11-11 16:44:04.901409

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5c3dd8521bc9'
down_revision = '07e5716f0046'
branch_labels = None
depends_on = None


def upgrade():
    # Adicionar novos campos de screenshot
    with op.batch_alter_table('process', schema=None) as batch_op:
        batch_op.add_column(sa.Column('elaw_screenshot_before_path', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('elaw_screenshot_after_path', sa.String(length=500), nullable=True))
    
    # Migrar dados existentes: copiar elaw_screenshot_path -> elaw_screenshot_before_path
    op.execute("""
        UPDATE process 
        SET elaw_screenshot_before_path = elaw_screenshot_path 
        WHERE elaw_screenshot_path IS NOT NULL
    """)


def downgrade():
    # Remover colunas adicionadas
    with op.batch_alter_table('process', schema=None) as batch_op:
        batch_op.drop_column('elaw_screenshot_after_path')
        batch_op.drop_column('elaw_screenshot_before_path')
