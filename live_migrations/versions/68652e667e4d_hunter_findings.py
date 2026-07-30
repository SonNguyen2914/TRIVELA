"""Kalshi market hunter: findings + cycle heartbeat

Three additive tables for the observational soccer-market scanner:

  hunter_finding  append-only findings (structural arbitrage, crossed
                  books, post-certainty, liquidity context, model-edge
                  readouts) with the full arithmetic and OUR capture
                  clocks. Expiry is a status flip + second timestamp,
                  never a delete. Two CHECK constraints make the clock
                  rules database invariants: last_seen_at never regresses
                  behind first_captured_at, and a row carrying an ESPN
                  finality clock must have captured its quote at or after
                  it (a pre-final price plus a settled outcome
                  manufactures a margin that never existed).
  hunter_cycle    one row per scan cycle — the heartbeat and the
                  denominator (cycles run / markets scanned) every
                  findings count is reported against.
  hunter_observation_watermark
                  the newest observation clock applied to each
                  finding_key. Railway overlaps containers deliberately,
                  so an OLDER scan can commit after a newer one; the
                  watermark is advanced by an atomic
                  UPDATE ... WHERE observed_through < :clock, which lets
                  the DATABASE reject the stale write instead of letting
                  it re-open an expired finding and alert on it.

This revision is EXTENDED rather than superseded: it has never been
applied to any environment (the branch is unmerged), so a single
additive revision remains the honest description of the change and
keeps the re-parenting note below a one-line concern.

MERGE-ORDER NOTE: parented on d4e5f6a7b8c9, the head of origin/main at
branch time. Three other in-flight branches carry their own migrations
off the same head; whichever lands later must re-parent (down_revision)
onto the new head. This migration is single and additive precisely so
that re-parenting is a one-line change.

Revision ID: 68652e667e4d
Revises: d4e5f6a7b8c9
Create Date: 2026-07-28
"""
from alembic import op
import sqlalchemy as sa


revision = '68652e667e4d'
down_revision = '7f31c9b8ad42'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'hunter_finding',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('competition_slug', sa.String(32), nullable=True),
        sa.Column('series', sa.String(64), nullable=False),
        sa.Column('event_ticker', sa.String(128), nullable=True),
        sa.Column('market_ticker', sa.String(128), nullable=True),
        sa.Column('finding_type', sa.String(32), nullable=False),
        # provider-stable identity (type|series|event|market); the
        # partial unique index below enforces one OPEN row per key at
        # the database, across overlapping Railway containers
        sa.Column('finding_key', sa.String(384), nullable=False),
        sa.Column('is_context', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column('legs_json', sa.Text(), nullable=False),
        sa.Column('net_margin_dollars', sa.String(24), nullable=True),
        sa.Column('fee_policy_version', sa.String(64), nullable=True),
        sa.Column('model_qualifier_json', sa.Text(), nullable=True),
        sa.Column('fixture_id', sa.Integer(),
                  sa.ForeignKey('fixture.id'), nullable=True),
        # the per-series Kalshi fetch-completion clock (the only timing
        # evidence a quote gets), plus the SEPARATE detection and
        # persistence clocks — never conflated
        sa.Column('first_captured_at', sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column('observed_cycles', sa.Integer(), nullable=True),
        sa.Column('detected_at', sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column('persisted_at', sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column('espn_captured_at', sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column('status', sa.String(16), nullable=False,
                  server_default='open'),
        sa.Column('expired_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('alerted_at', sa.DateTime(timezone=True), nullable=True),
        # durable, leased alert claim + retained per-transport results
        sa.Column('alert_claimed_at', sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column('alert_results_json', sa.Text(), nullable=True),
        # clock invariants enforced by the DATABASE, not by the writer
        sa.CheckConstraint(
            'last_seen_at IS NULL OR last_seen_at >= first_captured_at',
            name='ck_hunter_finding_clock_monotonic'),
        sa.CheckConstraint(
            'espn_captured_at IS NULL OR '
            'first_captured_at >= espn_captured_at',
            name='ck_hunter_finding_quote_after_finality'),
    )
    op.create_index('ix_hunter_finding_status_type', 'hunter_finding',
                    ['status', 'finding_type'])
    op.create_index('uq_hunter_finding_open_key', 'hunter_finding',
                    ['finding_key'], unique=True,
                    postgresql_where=sa.text("status = 'open'"),
                    sqlite_where=sa.text("status = 'open'"))
    op.create_table(
        'hunter_cycle',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('started_at', sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True),
                  nullable=True),
        # running | complete | degraded | failed
        sa.Column('status', sa.String(16), nullable=False,
                  server_default='running'),
        sa.Column('series_scanned', sa.Integer(), nullable=True),
        sa.Column('series_failed', sa.Integer(), nullable=True),
        sa.Column('series_outcomes_json', sa.Text(), nullable=True),
        sa.Column('events_scanned', sa.Integer(), nullable=True),
        sa.Column('markets_scanned', sa.Integer(), nullable=True),
        sa.Column('findings_new', sa.Integer(), nullable=True),
        sa.Column('findings_expired', sa.Integer(), nullable=True),
        sa.Column('stale_writes_rejected', sa.Integer(), nullable=True),
        sa.Column('request_count', sa.Integer(), nullable=True),
        sa.Column('roster_size', sa.Integer(), nullable=True),
        sa.Column('active_series', sa.Integer(), nullable=True),
        sa.Column('discovery_at', sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column('discovery_error', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
    )
    # chronological reconciliation across overlapping containers: the
    # newest observation clock per finding_key, advanced by a conditional
    # UPDATE so a stale writer loses at the DATABASE
    op.create_table(
        'hunter_observation_watermark',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('finding_key', sa.String(384), nullable=False,
                  unique=True),
        sa.Column('observed_through', sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('hunter_observation_watermark')
    op.drop_table('hunter_cycle')
    op.drop_index('uq_hunter_finding_open_key',
                  table_name='hunter_finding')
    op.drop_index('ix_hunter_finding_status_type',
                  table_name='hunter_finding')
    op.drop_table('hunter_finding')
