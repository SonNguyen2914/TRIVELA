"""Journal P0 round 3 — verified dispatch origin, quote age, payload hashes

Schema teeth for the three round-3 P0 findings, plus the correction-chain
linearity race.

  `broadcast_log.dispatch_capability_id` (P0-G) — `source` records what
      the caller CLAIMED; this records the interactive-session
      capability the server actually verified before dispatching. The
      stack-frame check added in round 2 binds in-process callers only:
      over HTTP the stack contains no guarded frame, so any holder of
      the generic operator token could claim source="session" and reach
      Discord/ntfy. NULL on rows written before the gate existed —
      absence is "unverified", never "verified".

  `personal_bet.quote_age_seconds` / `quote_max_age_seconds` /
  `quote_rejection_reason` (P0-H) — Kalshi publishes NO quote timestamp,
      so our capture clock is the only evidence of when a book was
      observed, and an arbitrarily old same-contract quote could be
      promoted into `observed_quote`. The accepted age and the ceiling
      in force are stored with the row so a later config change cannot
      silently reclassify it, and a REFUSED citation is named rather
      than left as an absent field.

  `personal_bet_execution.fill_quote_age_seconds` /
  `fill_quote_max_age_seconds` (P0-H) — the same, for the fill book the
      decomposed execution-fidelity metrics rest on.

  `personal_bet_execution.execution_payload_hash` /
  `settlement_payload_hash` / `reconciliation_payload_hash` (P0-I) —
      idempotency was keyed on (exchange_order_id, status) alone, so the
      same key carrying DIFFERENT economics returned the earlier row as
      though it had confirmed the new payload. Each write path now
      stores a canonical hash of what it accepted; a repeat that hashes
      differently is a conflict and writes nothing.

  partial unique index `uq_personal_bet_correction_head` on
  `personal_bet.corrects_bet_id` (P1-G) — correction chains must stay
      linear, and the handler's read-then-insert check loses the race:
      two concurrent corrections of the same entry both read "no head"
      and both commit. Partial so ordinary rows (NULL) are
      unconstrained. Explicit per-dialect WHERE text, for the reason the
      canonical-lock index carries it: a clause built from typeless
      detached Columns rendered `canonical IS 1`, which SQLite accepts
      and PostgreSQL rejects.

No backfill. Every column is nullable and every existing row keeps NULL,
which reads as "this predates the check" — the honest value. Backfilling
a capability id, a quote age or a payload hash would manufacture
evidence that the server never verified.

Revision ID: 7f31c9b8ad42
Revises: 8c40640a120f
Create Date: 2026-07-29

(Revision id is random, not the repo's historical rotating-hex pattern:
two agents continuing that pattern from the same head deterministically
mint the SAME id, which collides at merge.)
"""
from alembic import op
import sqlalchemy as sa


revision = '7f31c9b8ad42'
down_revision = '8c40640a120f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('broadcast_log',
                  sa.Column('dispatch_capability_id', sa.String(64),
                            nullable=True))
    with op.batch_alter_table('personal_bet') as batch:
        batch.add_column(sa.Column('quote_age_seconds', sa.Integer(),
                                   nullable=True))
        batch.add_column(sa.Column('quote_max_age_seconds', sa.Integer(),
                                   nullable=True))
        batch.add_column(sa.Column('quote_rejection_reason',
                                   sa.String(24), nullable=True))
    with op.batch_alter_table('personal_bet_execution') as batch:
        batch.add_column(sa.Column('fill_quote_age_seconds',
                                   sa.Integer(), nullable=True))
        batch.add_column(sa.Column('fill_quote_max_age_seconds',
                                   sa.Integer(), nullable=True))
        batch.add_column(sa.Column('execution_payload_hash',
                                   sa.String(64), nullable=True))
        batch.add_column(sa.Column('settlement_payload_hash',
                                   sa.String(64), nullable=True))
        batch.add_column(sa.Column('reconciliation_payload_hash',
                                   sa.String(64), nullable=True))
    # partial unique index — created outside batch mode so the dialect's
    # own WHERE clause is emitted verbatim
    dialect = op.get_bind().dialect.name
    where = sa.text("corrects_bet_id IS NOT NULL")
    kwargs = ({"postgresql_where": where} if dialect == "postgresql"
              else {"sqlite_where": where})
    op.create_index('uq_personal_bet_correction_head', 'personal_bet',
                    ['corrects_bet_id'], unique=True, **kwargs)


def downgrade() -> None:
    op.drop_index('uq_personal_bet_correction_head',
                  table_name='personal_bet')
    with op.batch_alter_table('personal_bet_execution') as batch:
        batch.drop_column('reconciliation_payload_hash')
        batch.drop_column('settlement_payload_hash')
        batch.drop_column('execution_payload_hash')
        batch.drop_column('fill_quote_max_age_seconds')
        batch.drop_column('fill_quote_age_seconds')
    with op.batch_alter_table('personal_bet') as batch:
        batch.drop_column('quote_rejection_reason')
        batch.drop_column('quote_max_age_seconds')
        batch.drop_column('quote_age_seconds')
    op.drop_column('broadcast_log', 'dispatch_capability_id')
