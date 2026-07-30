"""mls team stat: provider data_status/scope + derived xG quarantine

Sportec's own xG collapsed on the 2026-07-22/23 MLS restart slates while
its own shot volume held. Seven matches carry xG that the SAME payload's
shot and goal counts contradict, and the provider labels all of them
data_status="postmatch" — final, not provisional — so no provider flag
can reject them.

Four nullable columns:
  data_status, scope            the provider's own claim, now auditable
  xg_quarantined, ..._reason    the verdict of the DERIVED guard

Purely additive: nullable columns only, plain DDL valid on SQLite and
PostgreSQL, no backfill and no data rewritten. Existing rows read NULL,
which mls_stats treats as "never screened" — distinct from "screened and
passed" — and a re-ingest sets the verdict. No model output changes here:
whether the ratings EXCLUDE quarantined xG is gated on
config.MLS_XG_QUARANTINE_EXCLUDE, which defaults to false.

Revision ID: c4e8a91f27b6
Revises: 1b85967ee8f3
Create Date: 2026-07-30

(Revision id is random, per the note in 8c40640a120f: parallel agents
continuing the repo's historical rotating-hex pattern mint colliding ids.)
"""
from alembic import op
import sqlalchemy as sa


revision = 'c4e8a91f27b6'
down_revision = '1b85967ee8f3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('mls_team_match_stat',
                  sa.Column('data_status', sa.String(length=24),
                            nullable=True))
    op.add_column('mls_team_match_stat',
                  sa.Column('scope', sa.String(length=24), nullable=True))
    op.add_column('mls_team_match_stat',
                  sa.Column('xg_quarantined', sa.Boolean(), nullable=True))
    op.add_column('mls_team_match_stat',
                  sa.Column('xg_quarantine_reason', sa.String(length=160),
                            nullable=True))


def downgrade() -> None:
    op.drop_column('mls_team_match_stat', 'xg_quarantine_reason')
    op.drop_column('mls_team_match_stat', 'xg_quarantined')
    op.drop_column('mls_team_match_stat', 'scope')
    op.drop_column('mls_team_match_stat', 'data_status')
