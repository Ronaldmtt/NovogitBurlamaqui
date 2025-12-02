"""add_elaw_detail_url_and_screenshot_reclamadas

Revision ID: 06a4ed414aee
Revises: 5c3dd8521bc9
Create Date: 2025-11-26 15:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '06a4ed414aee'
down_revision = '5c3dd8521bc9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('process', schema=None) as batch_op:
        batch_op.add_column(sa.Column('elaw_detail_url', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('elaw_screenshot_reclamadas_path', sa.String(length=500), nullable=True))


def downgrade():
    with op.batch_alter_table('process', schema=None) as batch_op:
        batch_op.drop_column('elaw_screenshot_reclamadas_path')
        batch_op.drop_column('elaw_detail_url')
