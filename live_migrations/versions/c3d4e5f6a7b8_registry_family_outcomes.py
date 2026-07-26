"""Registry completeness needs per-family outcomes (V9.5 eval, critical 2)

`discover_and_map()` decided completeness from pagination truncation
alone. A provider request that failed outright was logged and skipped
without touching that flag, so the evaluator forced every required
family request to fail and still got:

    {"events_seen": 0, "discovery_complete": true, "truncated_series": []}

...persisted as a complete RegistryDiscovery row, which then satisfies a
canonical lock's "recent complete registry sweep" prerequisite. An
incomplete local universe could define its own expected universe.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-26
"""
from alembic import op
import sqlalchemy as sa


revision = 'c3d4e5f6a7b8'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('registry_discovery',
                  sa.Column('family_outcomes_json', sa.Text(),
                            nullable=True))
    op.add_column('registry_discovery',
                  sa.Column('incomplete_reasons_json', sa.Text(),
                            nullable=True))


def downgrade() -> None:
    op.drop_column('registry_discovery', 'incomplete_reasons_json')
    op.drop_column('registry_discovery', 'family_outcomes_json')
