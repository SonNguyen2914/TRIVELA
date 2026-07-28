"""Personal bet journal + execution-fidelity pilot

Son records the bets he forms a view on — taken AND passed. A consenting
friend places some of them on the real Kalshi market at much smaller
size, producing actual fills. The gap between the two is the first
measurement this project has of whether its EXECUTION model matches
reality: every fill in the paper ledger is simulated.

This is the V9.5 evaluation's step 21 (a tightly capped manual pilot
with actual exchange confirmations and complete reconciliation).

Three deliberate design points, each preventing a known failure:

  `personal_bet.status` includes `passed`  — a journal of only the bets
      that were taken has exactly the survivorship problem the paper
      ledger's retained rejections exist to prevent.

  `personal_bet_execution.status` includes `not_filled` — a fill the
      friend could not get is evidence about liquidity, which is half of
      what the pilot measures. Recording only successes reproduces the
      same bias one level down.

  `market_quote_id_at_fill` — so slippage decomposes into market drift
      versus execution cost. Conflated, neither question is answerable.

These tables are NOT research evidence. Nothing here may join to
PaperSignal/PaperFill or reach model_eval or an approval decision.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-27
"""
from alembic import op
import sqlalchemy as sa


revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'personal_bet',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('competition_slug', sa.String(length=32)),
        sa.Column('fixture_id', sa.Integer(), nullable=False),
        sa.Column('market_ticker', sa.String(length=128), nullable=False),
        sa.Column('market_contract_id', sa.Integer()),
        sa.Column('outcome_key', sa.String(length=32)),
        sa.Column('status', sa.String(length=16), nullable=False,
                  server_default='considered'),
        sa.Column('stated_price_dollars', sa.String(length=16)),
        sa.Column('stated_size', sa.String(length=24)),
        sa.Column('price_basis', sa.String(length=16), nullable=False,
                  server_default='stated_only'),
        sa.Column('market_quote_id', sa.Integer()),
        sa.Column('quote_observed_at', sa.DateTime(timezone=True)),
        sa.Column('recorded_at', sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True)),
        sa.Column('rationale', sa.Text()),
        sa.Column('model_probability', sa.Float()),
        sa.Column('prediction_run_id', sa.String(length=36)),
        sa.Column('settled_outcome', sa.String(length=32)),
        sa.Column('settled_at', sa.DateTime(timezone=True)),
        sa.Column('content_hash', sa.String(length=64)),
        sa.ForeignKeyConstraint(['competition_slug'], ['competition.slug']),
        sa.ForeignKeyConstraint(['fixture_id'], ['fixture.id']),
        sa.ForeignKeyConstraint(['market_contract_id'],
                                ['market_contract.id']),
        sa.ForeignKeyConstraint(['market_quote_id'], ['market_quote.id']),
        sa.ForeignKeyConstraint(['prediction_run_id'],
                                ['prediction_run.id']),
    )
    op.create_index('ix_personal_bet_fixture', 'personal_bet',
                    ['fixture_id'])

    op.create_table(
        'personal_bet_execution',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('personal_bet_id', sa.Integer(), nullable=False),
        sa.Column('account_label', sa.String(length=64), nullable=False),
        sa.Column('consent_recorded_at', sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False,
                  server_default='filled'),
        sa.Column('not_filled_reason', sa.String(length=32)),
        sa.Column('best_available_price_dollars', sa.String(length=16)),
        sa.Column('fill_price_dollars', sa.String(length=16)),
        sa.Column('filled_contracts', sa.String(length=24)),
        sa.Column('fee_paid_dollars', sa.String(length=16)),
        sa.Column('filled_at', sa.DateTime(timezone=True)),
        sa.Column('market_quote_id_at_fill', sa.Integer()),
        sa.Column('exchange_order_id', sa.String(length=64)),
        sa.Column('settlement_credit_dollars', sa.String(length=16)),
        sa.Column('settled_at', sa.DateTime(timezone=True)),
        sa.Column('reconciled', sa.Boolean(), server_default=sa.false()),
        sa.Column('reconciliation_note', sa.Text()),
        sa.ForeignKeyConstraint(['personal_bet_id'], ['personal_bet.id']),
        sa.ForeignKeyConstraint(['market_quote_id_at_fill'],
                                ['market_quote.id']),
    )
    op.create_index('ix_personal_bet_execution_bet',
                    'personal_bet_execution', ['personal_bet_id'])

    op.create_table(
        'broadcast_log',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('fixture_id', sa.Integer()),
        sa.Column('channel', sa.String(length=16), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('session_label', sa.String(length=64)),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('claims_json', sa.Text()),
        sa.Column('delivered', sa.Boolean(), server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(['fixture_id'], ['fixture.id']),
    )
    op.create_index('ix_broadcast_log_fixture', 'broadcast_log',
                    ['fixture_id'])


def downgrade() -> None:
    op.drop_index('ix_broadcast_log_fixture', 'broadcast_log')
    op.drop_table('broadcast_log')
    op.drop_index('ix_personal_bet_execution_bet',
                  'personal_bet_execution')
    op.drop_table('personal_bet_execution')
    op.drop_index('ix_personal_bet_fixture', 'personal_bet')
    op.drop_table('personal_bet')
