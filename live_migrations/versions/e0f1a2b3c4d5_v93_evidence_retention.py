"""V9.3 eval F11 + F12 — complete raw payloads and the active price grid

F11: SourceObservation retains the COMPLETE body gzip+base64 beside the
     truncated preview, with the full byte count and encoding, so a hash
     can be independently verified and a corrected parser can replay it.
F12: MarketQuote freezes the market's active price grid at capture, so a
     historical price can be interpreted against the grid that was valid.

Additive nullable columns — plain DDL, valid on SQLite and PostgreSQL.

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
Create Date: 2026-07-25
"""
from alembic import op
import sqlalchemy as sa

revision = 'e0f1a2b3c4d5'
down_revision = 'd9e0f1a2b3c4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('source_observation',
                  sa.Column('payload_compressed', sa.Text(), nullable=True))
    op.add_column('source_observation',
                  sa.Column('payload_bytes', sa.Integer(), nullable=True))
    op.add_column('source_observation',
                  sa.Column('payload_encoding', sa.String(length=16),
                            nullable=True))
    op.add_column('market_quote',
                  sa.Column('price_level_structure', sa.String(length=32),
                            nullable=True))
    op.add_column('market_quote',
                  sa.Column('price_ranges_json', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('market_quote', 'price_ranges_json')
    op.drop_column('market_quote', 'price_level_structure')
    op.drop_column('source_observation', 'payload_encoding')
    op.drop_column('source_observation', 'payload_bytes')
    op.drop_column('source_observation', 'payload_compressed')
