"""League-derived xG ratings from API-Football (provider-native store)

Three additive tables. Nothing existing is altered, so this migration cannot
change any current model output, any lock, or any approval:

  apifootball_league_coverage   MEASURED xG coverage per (league, season).
                                API-Football's docs are 403-walled, so this
                                table IS the coverage documentation. The
                                expected-goals statistic TYPE is present even
                                where every VALUE is null, so coverage is
                                measured on values and the hollow form is
                                recorded explicitly.
  apifootball_fixture_xg        One team's xG in one fixture AS READ AT ONE
                                INSTANT. Append-only: the unique key carries
                                a value_hash, so a re-read with the same value
                                writes nothing and a re-read with a DIFFERENT
                                value inserts a second row — a retroactive
                                provider correction becomes visible and
                                countable instead of silently overwriting the
                                evidence.
  apifootball_club_league       A club's domestic league, provider-derived and
                                archived, with the ambiguous and unresolved
                                cases stored as such rather than guessed.

Provider-native keys (league/season/fixture/team ids from API-Football) rather
than FKs onto `fixture` / `team`: these ratings describe the global club
universe that friendlies draw on, and almost none of those clubs have a row in
`team`.

No source_observation payload is written per fixture — deliberately. Payloads
with no reader were one of the two growth drivers behind the 2026-07-25
DiskFull incident, and a full-season ingest is ~380 of them per league. The
parsed value plus the provider's raw string is retained instead.

Revision ID: 3e686ff73620
Revises: d4e5f6a7b8c9
Create Date: 2026-07-29
"""
from alembic import op
import sqlalchemy as sa


revision = '3e686ff73620'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'apifootball_league_coverage',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('league_id', sa.Integer(), nullable=False),
        sa.Column('season', sa.Integer(), nullable=False),
        sa.Column('league_name', sa.String(length=96), nullable=True),
        sa.Column('country', sa.String(length=64), nullable=True),
        sa.Column('verdict', sa.String(length=40), nullable=False),
        sa.Column('measured_rate', sa.Float(), nullable=True),
        sa.Column('samples_taken', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('samples_with_xg', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('type_without_value_seen', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column('miss_rounds_json', sa.Text(), nullable=True),
        sa.Column('sampling_method', sa.String(length=40), nullable=True),
        sa.Column('measured_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('league_id', 'season',
                            name='uq_apif_coverage_league_season'),
    )
    op.create_table(
        'apifootball_fixture_xg',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('provider_fixture_id', sa.Integer(), nullable=False),
        sa.Column('side', sa.String(length=8), nullable=False),
        sa.Column('league_id', sa.Integer(), nullable=False),
        sa.Column('season', sa.Integer(), nullable=False),
        sa.Column('provider_team_id', sa.Integer(), nullable=False),
        sa.Column('team_name', sa.String(length=96), nullable=True),
        sa.Column('opponent_team_id', sa.Integer(), nullable=True),
        sa.Column('xg', sa.Float(), nullable=True),
        sa.Column('xg_raw', sa.String(length=32), nullable=True),
        sa.Column('xg_against', sa.Float(), nullable=True),
        sa.Column('goals', sa.Integer(), nullable=True),
        sa.Column('goals_conceded', sa.Integer(), nullable=True),
        sa.Column('kickoff_utc', sa.DateTime(timezone=True), nullable=True),
        sa.Column('round_label', sa.String(length=64), nullable=True),
        sa.Column('value_hash', sa.String(length=64), nullable=False),
        sa.Column('read_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider_fixture_id', 'side', 'value_hash',
                            name='uq_apif_xg_fixture_side_value'),
    )
    op.create_index('ix_apif_xg_league_season', 'apifootball_fixture_xg',
                    ['league_id', 'season'])
    op.create_index('ix_apif_xg_team', 'apifootball_fixture_xg',
                    ['provider_team_id'])
    op.create_table(
        'apifootball_club_league',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('query_name', sa.String(length=96), nullable=False),
        sa.Column('resolution', sa.String(length=24), nullable=False),
        sa.Column('provider_team_id', sa.Integer(), nullable=True),
        sa.Column('provider_team_name', sa.String(length=96), nullable=True),
        sa.Column('country', sa.String(length=64), nullable=True),
        sa.Column('league_id', sa.Integer(), nullable=True),
        sa.Column('season', sa.Integer(), nullable=True),
        sa.Column('league_name', sa.String(length=96), nullable=True),
        sa.Column('league_country', sa.String(length=64), nullable=True),
        sa.Column('match_tier', sa.String(length=24), nullable=True),
        sa.Column('candidates_json', sa.Text(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('query_name', name='uq_apif_club_league_query'),
    )


def downgrade() -> None:
    op.drop_table('apifootball_club_league')
    op.drop_index('ix_apif_xg_team', table_name='apifootball_fixture_xg')
    op.drop_index('ix_apif_xg_league_season',
                  table_name='apifootball_fixture_xg')
    op.drop_table('apifootball_fixture_xg')
    op.drop_table('apifootball_league_coverage')
