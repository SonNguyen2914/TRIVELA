"""Retain the raw order book behind every depth row (V9.5 eval)

Capture fetched each market's order book, parsed the best-N levels, and
kept only those. The complete response was discarded, so:

  - omitted depth could not be reconstructed later;
  - a corrected parser could not be re-run against the original book;
  - best-N selection could not be independently audited from published
    corpus bytes.

The body is stored gzip+base64 through evidence.pack_payload (a real
response compresses to ~29%, and the content hash is always taken over
the FULL body), and each depth level points at the observation it came
from.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-26
"""
from alembic import op
import sqlalchemy as sa


revision = 'd4e5f6a7b8c9'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('market_depth_level',
                  sa.Column('book_observation_id', sa.Integer(),
                            nullable=True))
    with op.batch_alter_table('market_depth_level') as batch:
        batch.create_foreign_key('fk_depth_book_observation',
                                 'source_observation',
                                 ['book_observation_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('market_depth_level') as batch:
        batch.drop_constraint('fk_depth_book_observation',
                              type_='foreignkey')
    op.drop_column('market_depth_level', 'book_observation_id')
