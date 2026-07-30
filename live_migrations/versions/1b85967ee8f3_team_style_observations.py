"""Per-fixture raw playstyle inputs (provider-native, append-only)

ONE additive table, `team_style_observation`. Nothing existing is altered, so
this migration cannot change any current model output, any lock, or any
approval. In particular `apifootball_fixture_xg` is NOT widened, and that is
the point rather than an omission:

  that table's append-only guarantee rests on a value_hash computed over the
  xG tuple ALONE. A style value that changed while the xG stayed the same
  would hash identically, collide with the existing row and be silently
  dropped — reintroducing, through a widened row, exactly the overwrite the
  hash exists to prevent. Style therefore gets its own table with its own
  hash over its own values, and the two evidence trails stay independently
  auditable.

`source` is part of the identity. API-Football and Sportec do not merely
differ in accuracy; they carry DIFFERENT SETS of axes (Sportec publishes no
possession, no offsides and no goalkeeper saves), so pooling them into one
population would silently change which axes exist for which team.

Every raw column is nullable and NULL means ABSENT, never zero — the provider
ships a statistic type with a null value routinely, and a null read as 0 would
make a team look like it never won possession, never drew an offside and never
forced a save.

`provider_fixture_id` is a STRING, not an integer: API-Football's fixture ids
are numeric but Sportec's are not (`MLS-MAT-0009I1`), and a source-agnostic
store that typed this column as an integer would be unable to hold the second
source it exists to accommodate.

No source_observation payload is written per fixture — same reasoning as the
xG store: payloads with no reader were one of the two growth drivers behind
the 2026-07-25 DiskFull incident. The parsed values plus a compact raw_json of
the provider's own strings are retained instead.

Revision ID: 1b85967ee8f3
Revises: 37a567d88d57
Create Date: 2026-07-29
"""
from alembic import op
import sqlalchemy as sa

revision = "1b85967ee8f3"
# Re-parented at merge time from 3e686ff73620 onto the team-news head:
# both branches forked off 3e686ff73620 and each added a table, so
# keeping the original parent left the live plane with TWO heads.
down_revision = "37a567d88d57"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "team_style_observation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("league_id", sa.Integer(), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("provider_fixture_id", sa.String(length=48),
                  nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("provider_team_id", sa.Integer(), nullable=False),
        sa.Column("team_name", sa.String(length=96)),
        sa.Column("opponent_team_id", sa.Integer()),
        sa.Column("kickoff_utc", sa.DateTime(timezone=True)),
        sa.Column("round_label", sa.String(length=64)),
        # raw axis inputs — NULL == ABSENT, never zero
        sa.Column("possession_pct", sa.Float()),
        sa.Column("shots_total", sa.Integer()),
        sa.Column("shots_inside_box", sa.Integer()),
        sa.Column("xg", sa.Float()),
        sa.Column("offsides_drawn", sa.Integer()),
        sa.Column("offsides_own", sa.Integer()),
        sa.Column("gk_saves", sa.Integer()),
        sa.Column("raw_json", sa.Text()),
        sa.Column("value_hash", sa.String(length=64), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "provider_fixture_id", "side",
                            "value_hash",
                            name="uq_style_obs_source_fixture_side_hash"),
    )
    op.create_index("ix_style_scope", "team_style_observation",
                    ["source", "league_id", "season"])
    op.create_index("ix_style_team", "team_style_observation",
                    ["source", "provider_team_id"])


def downgrade() -> None:
    op.drop_index("ix_style_team", table_name="team_style_observation")
    op.drop_index("ix_style_scope", table_name="team_style_observation")
    op.drop_table("team_style_observation")
