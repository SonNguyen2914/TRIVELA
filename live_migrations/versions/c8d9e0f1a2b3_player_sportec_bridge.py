"""player.sportec_id — the ESPN<->Sportec identity bridge

One nullable, indexed column on the existing player table. Additive,
plain DDL valid on SQLite and PostgreSQL. Populated by player_bridge from
per-match participant matching (validated 99.5% on starters).

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-07-24
"""
from alembic import op
import sqlalchemy as sa


revision = 'c8d9e0f1a2b3'
down_revision = 'b7c8d9e0f1a2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('player',
                  sa.Column('sportec_id', sa.String(length=32),
                            nullable=True))
    op.create_index('ix_player_sportec_id', 'player', ['sportec_id'])


def downgrade() -> None:
    op.drop_index('ix_player_sportec_id', table_name='player')
    op.drop_column('player', 'sportec_id')
