"""Widen version/policy columns — a 26-char constant in a 24-char column
silently destroyed every paper fill of the first prospective slate

FEE_POLICY["version"] became "kalshi-fee-2026-07-general" (26 chars).
`paper_fill.fee_policy_version` was String(24). PostgreSQL raises
StringDataRightTruncation; SQLite — which the whole test suite runs on —
ignores VARCHAR length entirely. So the suite stayed green while every
paper_fill INSERT died in production.

The damage was not merely lost rows, it was BIASED loss. A lock that
found no tradeable edge inserted no fill and committed its rejection
signals normally. A lock that DID find one inserted a fill, hit the
truncation, and rolled the whole transaction back — taking its signals
with it. The ledger therefore retained 100% of rejections and 0% of
fills, and reported "27 signals, 0 fills, all rejected NET_EDGE_TOO_LOW"
— the exact inverse of what happened. Six of fifteen locks were lost
this way, and they were precisely the interesting ones.

Widening every version/policy column removes the trap. The matching
structural fix lives in the test suite, which now enforces VARCHAR
lengths on SQLite so this class cannot pass a local run again.

Revision ID: a1b2c3d4e5f6
Revises: f1a2b3c4d5e6
Create Date: 2026-07-26
"""
from alembic import op
import sqlalchemy as sa


revision = 'a1b2c3d4e5f6'
down_revision = 'f1a2b3c4d5e6'
branch_labels = None
depends_on = None

# (table, column, old_len, new_len, nullable). Every column that holds a
# versioned constant we control, plus the two that hold PROVIDER-supplied
# strings (fee_schedule_version, parser_version) where the length is not
# ours to guarantee at all.
_WIDEN = [
    ("paper_fill", "fee_policy_version", 24, 64, True),
    ("paper_fill", "execution_class", 24, 32, True),
    ("paper_signal", "policy_version", 24, 64, True),
    ("market_snapshot", "policy_version", 24, 64, True),
    ("market_quote", "fee_schedule_version", 16, 64, True),
    ("lineup_snapshot", "parser_version", 16, 32, True),
    ("model_input_artifact", "schema_version", 24, 64, False),
    ("corpus_export", "schema_version", 24, 64, True),
    ("model_approval_decision", "eval_version", 24, 64, True),
    ("model_approval_decision", "policy_version", 24, 64, True),
]


def _retype(table, col, frm, to, nullable) -> None:
    """Batch mode so the chain replays on SQLite too — the live plane is
    PostgreSQL, but the migration chain is also exercised against SQLite
    (which has no ALTER COLUMN ... TYPE) as a schema smoke test."""
    with op.batch_alter_table(table) as batch:
        batch.alter_column(col, type_=sa.String(length=to),
                           existing_type=sa.String(length=frm),
                           existing_nullable=nullable)


def upgrade() -> None:
    for table, col, old, new, nullable in _WIDEN:
        _retype(table, col, old, new, nullable)


def downgrade() -> None:
    # Narrowing can fail on rows that legitimately exceed the old cap.
    # That is the point of the migration, so the downgrade is only
    # meaningful on data written before the widening.
    for table, col, old, new, nullable in _WIDEN:
        _retype(table, col, new, old, nullable)
