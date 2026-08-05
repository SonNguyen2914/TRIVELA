"""Partial unique index on fixture (competition_slug, provider_fixture_id).

The viewer-competition journal path (#72) get-or-creates fixtures by
this pair at APPLICATION level — racy by construction, flagged in its
own review as "acceptable for one operator, worth queuing rather than
forgetting". This is the queue item landing: the database now says no.

Partial (provider_fixture_id IS NOT NULL) because the MLS plane's own
fixtures key on espn_event_id and may carry NULL provider ids forever.

PRE-CHECK, inside the migration: if duplicate pairs already exist the
index cannot be created, and creating it half-heartedly or
deduplicating rows SILENTLY would destroy journal-linked fixtures. So
it raises with the offending pairs named — the live plane goes dark
with migrations_current=false and a readable reason, which is the
fail-closed behaviour every plane here is built around. Verified before
shipping: the write path has been single-operator since it existed, so
the expected duplicate count is zero.

Revision ID: c4d5e6f7a8b9
Revises: b7d3e91c05af
"""
from alembic import op
import sqlalchemy as sa

revision = "c4d5e6f7a8b9"
down_revision = "b7d3e91c05af"
branch_labels = None
depends_on = None

INDEX = "uq_fixture_slug_provider_fixture"


def upgrade() -> None:
    conn = op.get_bind()
    dupes = conn.execute(sa.text(
        "SELECT competition_slug, provider_fixture_id, COUNT(*) AS n "
        "FROM fixture WHERE provider_fixture_id IS NOT NULL "
        "GROUP BY competition_slug, provider_fixture_id "
        "HAVING COUNT(*) > 1")).fetchall()
    if dupes:
        raise RuntimeError(
            f"cannot add {INDEX}: {len(dupes)} duplicate "
            f"(competition_slug, provider_fixture_id) pair(s) exist — "
            f"first few: {[tuple(d) for d in dupes[:5]]}. Resolve by "
            f"hand; journal rows may reference either copy, so no "
            f"automatic dedup is safe.")
    op.create_index(
        INDEX, "fixture",
        ["competition_slug", "provider_fixture_id"],
        unique=True,
        postgresql_where=sa.text("provider_fixture_id IS NOT NULL"),
        sqlite_where=sa.text("provider_fixture_id IS NOT NULL"))


def downgrade() -> None:
    op.drop_index(INDEX, table_name="fixture")
