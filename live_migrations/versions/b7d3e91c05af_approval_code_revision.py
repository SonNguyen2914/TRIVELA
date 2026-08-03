"""model_approval_decision: record the code revision the decision was made under

`engine_signature()` hashes the git revision along with the module source,
python and numpy — its own docstring says "EVERY deploy changes it,
including a migration or a docs commit that cannot touch the model". The
replay path already handles that: `engine_matches()` rehashes with the
revision a run RECORDED and reports `revision_only_drift` rather than
refusing.

The APPROVAL path had no such second arm, because a decision stored the
signature HASH and not the revision that produced it — leaving nothing to
rehash with. So `ensure_approval_decision` compared hashes strictly, any
deploy failed the match, and boot fell closed and disarmed the plane. Four
merges on 2026-08-02/03 disarmed MLS four times; one was a docs-only PR.

One nullable column so the second arm has an input.

DELIBERATELY OUTSIDE the content hash. `decision_document` is the exact
canonical bytes `content_hash` covers and the audit recomputes sha256 over
it — adding a field there would not invalidate stored rows, but it would
mean two decisions identical in every approved fact hash differently
because they shipped from different commits, which is the same
revision-sensitivity one layer up. The revision is provenance about the
decision, not part of what was decided.

Existing rows read NULL, which the second arm treats as "no revision
recorded" and refuses — identical to today's behaviour, so this migration
alone changes nothing. The first activation after it stores a revision,
and subsequent deploys stop disarming.

Purely additive: one nullable column, plain DDL valid on SQLite and
PostgreSQL, no backfill, no data rewritten.

Revision ID: b7d3e91c05af
Revises: c4e8a91f27b6
Create Date: 2026-08-03

(Revision id is random, per the note in 8c40640a120f: parallel agents
continuing the repo's historical rotating-hex pattern mint colliding ids.)
"""
import sqlalchemy as sa
from alembic import op

revision = 'b7d3e91c05af'
down_revision = 'c4e8a91f27b6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("model_approval_decision",
                  sa.Column("code_revision", sa.String(40), nullable=True))


def downgrade():
    op.drop_column("model_approval_decision", "code_revision")
