"""Broadcast wire record — the payload actually sent, per transport

journal-P0-A: the transports truncate at TRANSPORT_MESSAGE_LIMIT, so
"what the operator composed" and "what reached Discord/ntfy" can
differ — and on a long action message the difference was exactly the
appended standing-edge qualifier. The composer now reserves space for
the qualifier and truncates the PROSE (marked), and these columns
persist the exact final payload, its sha256, and each transport's own
acceptance, so the broadcast log describes what was actually sent.

Revision ID: 8c40640a120f
Revises: de771533ac03
Create Date: 2026-07-28

(Revision id is random, not the repo's historical rotating-hex pattern
— parallel agents continuing that pattern mint colliding ids.)
"""
from alembic import op
import sqlalchemy as sa


revision = '8c40640a120f'
down_revision = 'de771533ac03'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('broadcast_log',
                  sa.Column('dispatched_body', sa.Text(), nullable=True))
    op.add_column('broadcast_log',
                  sa.Column('dispatched_sha256', sa.String(length=64),
                            nullable=True))
    op.add_column('broadcast_log',
                  sa.Column('transports_json', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('broadcast_log', 'transports_json')
    op.drop_column('broadcast_log', 'dispatched_sha256')
    op.drop_column('broadcast_log', 'dispatched_body')
