"""Team news — absences, lineup release state, in-play events

Four additive tables. Nothing existing is altered, no column is dropped,
and no row is rewritten, so this migration cannot change the meaning of
any evidence already collected.

WHAT THESE TABLES ARE NOT. None of them is a model input. No PredictionRun,
no canonical T-10 lock, no paper signal and no engine-signature module
reads them; `tests/test_team_news_isolation.py` fails if that changes. The
platform already measured lineup and key-attacker features as
negative-or-marginal (key-attacker +0.0034, not significant) and switched
them off. This is reader context with provenance attached.

THREE SCHEMA DECISIONS THAT ENCODE LESSONS THIS REPO PAID FOR:

  `team_news_capture.record_count` IS NULLABLE, and is NULL exactly when
      the state is 'unavailable'. Zero means the provider showed us an
      empty list; NULL means we never saw a list. Collapsing those is the
      failure behind `results: 0` on a live 45th-minute match and behind
      `{"created": 0}` masking every paper fill over a full volume.

  `team_absence.player_key` IS NOT NULL. PostgreSQL treats NULLs as
      distinct inside a unique constraint, so keying absences on a
      nullable provider player id would admit duplicates in production
      while a SQLite suite stayed green — the exact parity trap recorded
      in the test-DB parity rule. The key falls back to a normalised name
      so it is always present.

  NO RAW PAYLOAD COLUMN. Only a content hash and a byte length are stored;
      the bytes live in research_archive/. A per-fixture payload column was
      the growth driver behind the 2026-07-25 DiskFull incident, where
      every prediction write failed silently behind a full volume.

There is also, deliberately, no aggregate anywhere: no absence count on a
fixture, no severity weight, no team-strength impact. An aggregate of
absences is a model wearing a news label.

Revision ID: 37a567d88d57
Revises: 7f31c9b8ad42
Create Date: 2026-07-30
"""
from alembic import op
import sqlalchemy as sa

revision = "37a567d88d57"
down_revision = "a3f7c1d9e2b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "team_news_capture",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("subject_ref", sa.String(length=32), nullable=False),
        sa.Column("fixture_id", sa.Integer(), sa.ForeignKey("fixture.id")),
        sa.Column("competition", sa.String(length=32)),
        sa.Column("feed", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        # nullable ON PURPOSE — see the module docstring
        sa.Column("record_count", sa.Integer()),
        sa.Column("provider_declared_count", sa.Integer()),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_captured_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False,
                  server_default="1"),
        sa.Column("note", sa.String(length=300)),
        sa.Column("content_hash", sa.String(length=64)),
        sa.Column("payload_bytes", sa.Integer()),
        sa.UniqueConstraint("provider", "subject_ref", "feed",
                            name="uq_team_news_capture_subject_feed"),
    )
    op.create_index("ix_team_news_capture_fixture", "team_news_capture",
                    ["fixture_id"])

    op.create_table(
        "team_absence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("subject_ref", sa.String(length=32), nullable=False),
        sa.Column("fixture_id", sa.Integer(), sa.ForeignKey("fixture.id")),
        sa.Column("competition", sa.String(length=32)),
        sa.Column("player_key", sa.String(length=96), nullable=False),
        sa.Column("provider_player_id", sa.String(length=24)),
        sa.Column("player_name", sa.String(length=96)),
        sa.Column("provider_team_id", sa.String(length=24)),
        sa.Column("team_name", sa.String(length=96)),
        # the provider's own words, stored verbatim
        sa.Column("provider_type", sa.String(length=64)),
        sa.Column("provider_reason", sa.String(length=96)),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("provider", "subject_ref", "player_key",
                            name="uq_team_absence_subject_player"),
    )
    op.create_index("ix_team_absence_fixture", "team_absence", ["fixture_id"])

    op.create_table(
        "team_news_lineup",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("subject_ref", sa.String(length=32), nullable=False),
        sa.Column("fixture_id", sa.Integer(), sa.ForeignKey("fixture.id")),
        sa.Column("competition", sa.String(length=32)),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("team_name", sa.String(length=96)),
        sa.Column("formation", sa.String(length=16)),
        sa.Column("named_count", sa.Integer()),
        sa.Column("starter_count", sa.Integer()),
        sa.Column("released", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("container_present", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True)),
        sa.Column("first_released_at", sa.DateTime(timezone=True)),
        sa.Column("kickoff_utc", sa.DateTime(timezone=True)),
        sa.Column("released_minutes_before_kickoff", sa.Integer()),
        sa.UniqueConstraint("provider", "subject_ref", "side",
                            name="uq_team_news_lineup_subject_side"),
    )
    op.create_index("ix_team_news_lineup_fixture", "team_news_lineup",
                    ["fixture_id"])

    op.create_table(
        "team_news_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("subject_ref", sa.String(length=32), nullable=False),
        sa.Column("fixture_id", sa.Integer(), sa.ForeignKey("fixture.id")),
        sa.Column("competition", sa.String(length=32)),
        sa.Column("event_key", sa.String(length=160), nullable=False),
        sa.Column("provider_minute", sa.Integer()),
        sa.Column("provider_minute_extra", sa.Integer()),
        sa.Column("event_type", sa.String(length=32)),
        sa.Column("event_detail", sa.String(length=64)),
        sa.Column("provider_team_id", sa.String(length=24)),
        sa.Column("team_name", sa.String(length=96)),
        sa.Column("provider_player_id", sa.String(length=24)),
        sa.Column("player_name", sa.String(length=96)),
        sa.Column("assist_name", sa.String(length=96)),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("provider", "subject_ref", "event_key",
                            name="uq_team_news_event_subject_key"),
    )
    op.create_index("ix_team_news_event_fixture", "team_news_event",
                    ["fixture_id"])


def downgrade() -> None:
    op.drop_index("ix_team_news_event_fixture", table_name="team_news_event")
    op.drop_table("team_news_event")
    op.drop_index("ix_team_news_lineup_fixture", table_name="team_news_lineup")
    op.drop_table("team_news_lineup")
    op.drop_index("ix_team_absence_fixture", table_name="team_absence")
    op.drop_table("team_absence")
    op.drop_index("ix_team_news_capture_fixture",
                  table_name="team_news_capture")
    op.drop_table("team_news_capture")
