"""V9.3 eval P0s — execution-fidelity columns

Additive columns supporting the V9.3 evaluation's P0 findings:
  F2  per-level fill allocations + the fee policy they were charged under
  F3  the edge the fill ACTUALLY realised, beside the quoted edge
  F4  whether a fill walked captured depth or is a top-of-book estimate

New nullable columns only — plain DDL, valid on SQLite and PostgreSQL.

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-07-25
"""
from alembic import op
import sqlalchemy as sa


revision = 'd9e0f1a2b3c4'
down_revision = 'c8d9e0f1a2b3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('paper_signal',
                  sa.Column('realized_net_edge', sa.Float(), nullable=True))
    op.add_column('paper_fill',
                  sa.Column('execution_class', sa.String(length=24),
                            nullable=True))
    op.add_column('paper_fill',
                  sa.Column('allocations_json', sa.Text(), nullable=True))
    op.add_column('paper_fill',
                  sa.Column('fee_policy_version', sa.String(length=24),
                            nullable=True))


def downgrade() -> None:
    op.drop_column('paper_fill', 'fee_policy_version')
    op.drop_column('paper_fill', 'allocations_json')
    op.drop_column('paper_fill', 'execution_class')
    op.drop_column('paper_signal', 'realized_net_edge')
