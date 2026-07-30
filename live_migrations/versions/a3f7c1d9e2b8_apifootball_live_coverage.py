"""API-Football live in-play coverage: measured, dated, one row per competition

ONE additive table, `apifootball_live_coverage`. It records what the
provider was MEASURED to serve in-play for each competition, so the
hunter's conditioning inference never rests on an assumption about
coverage.

Why the columns are shaped this way:

  - `league_id` is the PROVIDER's own id and is UNIQUE — a provider-stable
    identity, never a competition name. One row per competition, updated
    in place, so growth is bounded by the number of competitions rather
    than by the number of readings. Per-reading payloads with no reader
    are exactly what filled the production volume on 2026-07-25.

  - statistics coverage and EVENTS coverage are separate columns rather
    than one "has live data" flag. Measured 2026-07-29: Copa Colombia
    served 3 goal events with ZERO statistic types, and USL League One
    served 5 events including cards with zero statistic types. Event
    coverage is strictly broader, and collapsing the two would have made
    the broader signal invisible.

  - `fixtures_probed` is the denominator and is stored beside the verdict,
    because a verdict from one fixture at 20' and one from ten fixtures at
    70' are not the same evidence.

  - `too_early_reads` is counted separately from the verdict. A reading
    taken before a match had time to generate statistics is uninformative,
    and it must never downgrade an already-measured PRESENT verdict —
    doing so would convert missing evidence into a conclusion.

  - `last_present_at` is kept apart from `last_measured_at` so a long run
    of absences cannot make a stale presence look current.

NOT A MODEL INPUT. Nothing in this table may reach a T-10 lock, a
PredictionRun, a paper signal, or any feature the model is fitted or
scored on. Live xG carries information from after a lock; this is the
hunter's observational surface only, and
tests/test_hunter_live_stats.py enforces that boundary.

No existing table is altered and no data is backfilled, so downgrade is a
clean drop.

MERGE-ORDER NOTE: parented on 68652e667e4d, the head of
origin/feat-market-hunter at branch time. Several branches are in flight
with their own migrations; whichever lands later re-parents
`down_revision`. This revision is single and additive precisely so that
re-parenting stays a one-line change.

Revision ID: a3f7c1d9e2b8
Revises: 68652e667e4d
Create Date: 2026-07-29
"""
from alembic import op
import sqlalchemy as sa


revision = 'a3f7c1d9e2b8'
down_revision = '68652e667e4d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'apifootball_live_coverage',
        sa.Column('id', sa.Integer(), primary_key=True),
        # the provider's own league id — provider-stable identity
        sa.Column('league_id', sa.Integer(), nullable=False),
        sa.Column('league_name', sa.String(128), nullable=True),
        sa.Column('league_country', sa.String(64), nullable=True),
        # LIVE_STATS_PRESENT | LIVE_STATS_ABSENT | TOO_EARLY_TO_CONCLUDE
        sa.Column('verdict', sa.String(32), nullable=True),
        sa.Column('statistic_type_count', sa.Integer(), nullable=True),
        sa.Column('statistic_types_json', sa.Text(), nullable=True),
        # presence means a PARSEABLE number for both teams; a present-but-
        # null xG (several leagues serve one) is ABSENT, never zero
        sa.Column('xg_present', sa.Boolean(), nullable=True),
        sa.Column('xg_type_offered', sa.Boolean(), nullable=True),
        # tracked separately from statistics: events coverage is broader
        sa.Column('events_present', sa.Boolean(), nullable=True),
        sa.Column('event_count_last', sa.Integer(), nullable=True),
        sa.Column('fixtures_probed', sa.Integer(), nullable=True),
        sa.Column('too_early_reads', sa.Integer(), nullable=True),
        sa.Column('last_elapsed_observed', sa.Integer(), nullable=True),
        sa.Column('first_measured_at', sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column('last_measured_at', sa.DateTime(timezone=True),
                  nullable=True),
        sa.Column('last_present_at', sa.DateTime(timezone=True),
                  nullable=True),
        # One row per competition is the growth bound, so it is a DATABASE
        # invariant rather than an upsert convention.
        #
        # Declared INLINE rather than through a follow-up
        # create_unique_constraint: SQLite has no ALTER for constraints, so
        # the add-after form raises NotImplementedError and the constraint
        # would have existed on PostgreSQL only. The test suite runs on
        # SQLite, so that shape means the suite cannot enforce what
        # production enforces — the test-DB parity rule. Inline works on
        # both dialects.
        sa.UniqueConstraint('league_id',
                            name='uq_apifootball_live_coverage_league'),
    )

    # The second provider's spend is counted on the cycle heartbeat, in its
    # OWN column rather than folded into request_count: one combined number
    # would hide which provider a budget problem came from, and Kalshi and
    # API-Football have different limits and different failure modes.
    # live_stats_json records what the pass DID — including, when it spent
    # nothing, why it spent nothing.
    op.add_column('hunter_cycle',
                  sa.Column('live_stats_request_count', sa.Integer(),
                            nullable=True))
    op.add_column('hunter_cycle',
                  sa.Column('live_stats_json', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('hunter_cycle', 'live_stats_json')
    op.drop_column('hunter_cycle', 'live_stats_request_count')
    # the unique constraint is inline in the table, so dropping the table
    # takes it with it — an explicit drop_constraint here would fail on
    # SQLite for the same reason the inline declaration exists
    op.drop_table('apifootball_live_coverage')
