"""Paper-coverage provenance — distinguish inline signals from backfills

The first prospective slate (2026-07-25) locked 15 fixtures, each with
all three game legs carrying a market contract AND a frozen quote — so
45 legs were eligible for paper evaluation. The ledger held 27 signals.
18 eligible legs produced nothing, and NO metric surfaced the shortfall:
`paper_summary` reported a numerator with no denominator, so "27 signals,
all rejected" read as a complete examination of the slate.

Recovering those legs is deterministic (every input is frozen on the
snapshot, including the quote-age the gate tests), but a signal written
today is not the same evidence as one written at the lock. This column
keeps them distinguishable forever: NULL means generated inline at lock
time, a timestamp means recomputed afterwards from the frozen book.

Revision ID: f1a2b3c4d5e6
Revises: e0f1a2b3c4d5
Create Date: 2026-07-26
"""
from alembic import op
import sqlalchemy as sa


revision = 'f1a2b3c4d5e6'
down_revision = 'e0f1a2b3c4d5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('paper_signal',
                  sa.Column('backfilled_at', sa.DateTime(timezone=True),
                            nullable=True))


def downgrade() -> None:
    op.drop_column('paper_signal', 'backfilled_at')
