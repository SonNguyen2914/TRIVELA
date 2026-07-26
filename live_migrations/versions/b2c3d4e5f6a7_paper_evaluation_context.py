"""Freeze the paper/risk state at lock time (V9.5 eval, critical 1)

`paper_trade_lock` read LIVE kill switches, LIVE open exposure and the
LIVE approved-model state. So a recovery run was never a pure replay:
the same frozen lock could yield a different decision later, which the
evaluator demonstrated by flipping a kill switch. Every fill in the
first prospective slate came from such a recovery, which makes them
reconstructions under recovery-time state rather than contemporaneous
execution evidence.

This adds the missing half of the input. With a frozen context, paper
evaluation is a pure function of (frozen lock evidence + context), and
signals record which of the two they are.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-26
"""
from alembic import op
import sqlalchemy as sa


revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'paper_evaluation_context',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('prediction_run_id', sa.String(length=36), nullable=False),
        sa.Column('captured_at', sa.DateTime(timezone=True)),
        sa.Column('exec_policy_version', sa.String(length=64)),
        sa.Column('exec_policy_json', sa.Text()),
        sa.Column('fee_policy_version', sa.String(length=64)),
        sa.Column('fee_policy_json', sa.Text()),
        sa.Column('risk_policy_version', sa.String(length=64)),
        sa.Column('risk_policy_json', sa.Text()),
        sa.Column('kill_switches_json', sa.Text()),
        sa.Column('exposure_json', sa.Text()),
        sa.Column('model_approved', sa.Boolean()),
        sa.Column('model_approval_decision_id', sa.Integer()),
        sa.Column('engine_signature_hash', sa.String(length=64)),
        sa.Column('content_hash', sa.String(length=64)),
        sa.ForeignKeyConstraint(['prediction_run_id'],
                                ['prediction_run.id']),
        sa.ForeignKeyConstraint(['model_approval_decision_id'],
                                ['model_approval_decision.id']),
        sa.UniqueConstraint('prediction_run_id',
                            name='uq_paper_eval_ctx_run'),
    )
    op.add_column('paper_signal',
                  sa.Column('evaluation_mode', sa.String(length=20),
                            nullable=True))
    op.add_column('paper_signal',
                  sa.Column('paper_evaluation_context_id', sa.Integer(),
                            nullable=True))
    with op.batch_alter_table('paper_signal') as batch:
        batch.create_foreign_key('fk_paper_signal_eval_ctx',
                                 'paper_evaluation_context',
                                 ['paper_evaluation_context_id'], ['id'])
    # Existing rows are honest about what they are. The 18 recovered
    # signals of the first slate were evaluated under recovery-time risk
    # state with no frozen context, so they are reconstructed-degraded;
    # the 27 written inline at the lock are contemporaneous.
    op.execute("UPDATE paper_signal SET evaluation_mode = 'reconstructed' "
               "WHERE backfilled_at IS NOT NULL")
    op.execute("UPDATE paper_signal SET evaluation_mode = 'contemporaneous' "
               "WHERE backfilled_at IS NULL")


def downgrade() -> None:
    with op.batch_alter_table('paper_signal') as batch:
        batch.drop_constraint('fk_paper_signal_eval_ctx',
                              type_='foreignkey')
    op.drop_column('paper_signal', 'paper_evaluation_context_id')
    op.drop_column('paper_signal', 'evaluation_mode')
    op.drop_table('paper_evaluation_context')
