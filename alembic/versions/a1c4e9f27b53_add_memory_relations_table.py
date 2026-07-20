"""add_memory_relations_table

Revision ID: a1c4e9f27b53
Revises: d7e1f3b2c804
Create Date: 2026-07-17 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1c4e9f27b53'
down_revision: str | None = 'd7e1f3b2c804'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('memory_relations',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('user_id', sa.String(), nullable=False),
    sa.Column('source_item_id', sa.String(), nullable=False),
    sa.Column('target_item_id', sa.String(), nullable=False),
    sa.Column('relation_type', sa.String(), nullable=False),
    sa.Column('project', sa.String(), nullable=True),
    sa.Column('metadata', JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
              nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'source_item_id', 'target_item_id', 'relation_type',
                        name='uq_memory_relations_edge')
    )
    op.create_index(op.f('ix_memory_relations_user_id'), 'memory_relations',
                    ['user_id'], unique=False)
    op.create_index(op.f('ix_memory_relations_source_item_id'), 'memory_relations',
                    ['source_item_id'], unique=False)
    op.create_index(op.f('ix_memory_relations_target_item_id'), 'memory_relations',
                    ['target_item_id'], unique=False)
    op.create_index(op.f('ix_memory_relations_project'), 'memory_relations',
                    ['project'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_memory_relations_project'), table_name='memory_relations')
    op.drop_index(op.f('ix_memory_relations_target_item_id'), table_name='memory_relations')
    op.drop_index(op.f('ix_memory_relations_source_item_id'), table_name='memory_relations')
    op.drop_index(op.f('ix_memory_relations_user_id'), table_name='memory_relations')
    op.drop_table('memory_relations')
