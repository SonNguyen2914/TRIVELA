"""Journal P0 remediations — lifecycle, idempotency, publication consent

Three schema teeth for the personal-bet-journal P0 findings:

  `personal_bet.corrects_bet_id` (F5) — a resolution is immutable once
      set. A mistake is corrected by a NEW row citing the old one here;
      the record of the mistake stays part of the record.

  unique (exchange_order_id, status) on personal_bet_execution (F5) —
      a retried POST for the same exchange order and status is a no-op,
      enforced by the database rather than promised by the handler.
      NULL order ids never conflict (both PostgreSQL and SQLite treat
      NULLs as distinct), so executions without an exchange id — e.g.
      not_filled attempts — are unaffected.

  `personal_bet_execution.publication_consent` (F2) — the corpus is
      public bytes and an execution row is a third party's financial
      record. Private fields (account label, order id, fill economics,
      consent provenance) export ONLY when this explicit flag is set.
      Server default false: absence of consent is never publication.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-28
"""
from alembic import op
import sqlalchemy as sa


revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('personal_bet',
                  sa.Column('corrects_bet_id', sa.Integer(),
                            nullable=True))
    op.create_foreign_key('fk_personal_bet_corrects', 'personal_bet',
                          'personal_bet', ['corrects_bet_id'], ['id'])
    op.add_column('personal_bet_execution',
                  sa.Column('publication_consent', sa.Boolean(),
                            server_default=sa.false(), nullable=True))
    op.create_unique_constraint(
        'uq_personal_bet_execution_order_status',
        'personal_bet_execution', ['exchange_order_id', 'status'])


def downgrade() -> None:
    op.drop_constraint('uq_personal_bet_execution_order_status',
                       'personal_bet_execution', type_='unique')
    op.drop_column('personal_bet_execution', 'publication_consent')
    op.drop_constraint('fk_personal_bet_corrects', 'personal_bet',
                       type_='foreignkey')
    op.drop_column('personal_bet', 'corrects_bet_id')
