"""Live-plane schema, tranche 1: identity + the evidence chain
(launch decision Jul 23, minimum-schema section).

Design rules carried from the decision doc:
  - prediction batches have explicit UUIDs and status gating — readers
    only ever see status='complete'; NO time-window reconstruction;
  - exactly one canonical complete T-10 run per fixture, enforced by a
    PARTIAL UNIQUE INDEX (postgresql_where + sqlite_where so the test
    suite enforces the same invariant the production database does);
  - market prices are integer CENTS (fixed point), both sides with
    sizes, plus depth levels;
  - fixture rescheduling creates history rows, never silent overwrite;
  - fuzzy matching may PROPOSE an identity mapping; only an APPROVED
    alias row may attach a market to a fixture.

Tranche 2 (with the ingestion build): player, player_team_membership,
team/player/availability/lineup snapshots, signal, paper_position,
paper_fill, settlement.
"""
from __future__ import annotations

import uuid

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Index,
                        Integer, String, Text, UniqueConstraint, text)
from sqlalchemy.orm import declarative_base

LiveBase = declarative_base()


def _uuid() -> str:
    return str(uuid.uuid4())


class Competition(LiveBase):
    __tablename__ = "competition"
    slug = Column(String(32), primary_key=True)        # mls-2026
    name = Column(String(64), nullable=False)
    provider_league_id = Column(Integer)               # API-Football id
    season = Column(Integer, nullable=False)
    timezone = Column(String(32), default="UTC")
    match_duration_minutes = Column(Integer, default=90)
    supports_draw = Column(Boolean, default=True)
    regular_time_only = Column(Boolean, default=True)
    has_group_stage = Column(Boolean, default=False)
    has_knockout_stage = Column(Boolean, default=False)
    model_version = Column(String(48))


class Team(LiveBase):
    __tablename__ = "team"
    id = Column(Integer, primary_key=True)
    competition_slug = Column(String(32),
                              ForeignKey("competition.slug"),
                              nullable=False)
    canonical_name = Column(String(80), nullable=False)
    abbrev = Column(String(8))
    espn_id = Column(String(16))
    api_football_id = Column(Integer)
    kalshi_name = Column(String(80))
    __table_args__ = (
        UniqueConstraint("competition_slug", "canonical_name"),
    )


class TeamAlias(LiveBase):
    """Identity bridge. Fuzzy matching can only PROPOSE (approved=False);
    market attachment requires approved=True."""
    __tablename__ = "team_alias"
    id = Column(Integer, primary_key=True)
    team_id = Column(Integer, ForeignKey("team.id"), nullable=False)
    alias = Column(String(80), nullable=False)
    source = Column(String(24), nullable=False)   # kalshi|espn|apifootball
    approved = Column(Boolean, default=False, nullable=False)
    __table_args__ = (UniqueConstraint("source", "alias"),)


class Fixture(LiveBase):
    __tablename__ = "fixture"
    id = Column(Integer, primary_key=True)
    competition_slug = Column(String(32),
                              ForeignKey("competition.slug"),
                              nullable=False)
    provider_fixture_id = Column(String(32))
    espn_event_id = Column(String(16))
    home_team_id = Column(Integer, ForeignKey("team.id"))
    away_team_id = Column(Integer, ForeignKey("team.id"))
    original_kickoff_utc = Column(DateTime(timezone=True))
    current_kickoff_utc = Column(DateTime(timezone=True))
    venue = Column(String(96))
    status = Column(String(16))
    home_goals = Column(Integer)          # final score once status=post
    away_goals = Column(Integer)
    round = Column(String(32))
    observed_at = Column(DateTime(timezone=True))
    provider_updated_at = Column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("competition_slug", "espn_event_id"),
    )


class FixtureChange(LiveBase):
    """Reschedules create history, never silent overwrite."""
    __tablename__ = "fixture_change"
    id = Column(Integer, primary_key=True)
    fixture_id = Column(Integer, ForeignKey("fixture.id"), nullable=False)
    field = Column(String(32), nullable=False)
    old_value = Column(String(96))
    new_value = Column(String(96))
    observed_at = Column(DateTime(timezone=True), nullable=False)


class SourceObservation(LiveBase):
    """Raw provider responses, content-hashed — the bottom of every
    evidence chain."""
    __tablename__ = "source_observation"
    id = Column(Integer, primary_key=True)
    source = Column(String(24), nullable=False)    # espn|kalshi|apifootball
    endpoint = Column(String(160), nullable=False)
    params_json = Column(Text)
    content_hash = Column(String(64), nullable=False)
    # a truncated, human-readable PREVIEW only — never the evidence
    payload_json = Column(Text)
    # the COMPLETE raw body, gzip+base64 (V9.3 eval F11). A content hash
    # without the bytes cannot be independently verified or replayed
    # through a corrected parser, which is exactly what the truncated
    # preview cost us. Compression makes the full body ~29% of raw — i.e.
    # SMALLER than the 8 KB stub it replaces — so completeness costs no
    # more volume than the truncation did.
    payload_compressed = Column(Text)
    payload_bytes = Column(Integer)            # full, uncompressed length
    payload_encoding = Column(String(16))      # gzip+base64
    observed_at = Column(DateTime(timezone=True), nullable=False)
    provider_timestamp = Column(DateTime(timezone=True))


class ModelVersion(LiveBase):
    __tablename__ = "model_version"
    id = Column(Integer, primary_key=True)
    name = Column(String(48), unique=True, nullable=False)  # mls-2026-v0
    description = Column(Text)
    approved_for_shadow = Column(Boolean, default=False, nullable=False)
    approved_for_real_money = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True))


class ModelApprovalDecision(LiveBase):
    """The immutable model-approval DECISION a run is authorized under
    (V9 eval F1/F10). Boot no longer sets approved_for_shadow from a bare
    Monte-Carlo point estimate: it runs the confidence-interval evaluator
    (model_eval.evaluate_ladder), records the whole decision here — the
    M2-vs-baseline edge WITH its 95% CI, the metrics, the limitations, the
    eval/policy/corpus versions — content-hashes it, and only then flips
    the flag. Deduped by content_hash: an unchanged evaluation reuses one
    row, a changed one writes a new, never-overwritten record. Shadow
    approval means 'safe to collect prospective evidence', NEVER 'edge
    established' — approved_mode is capped at 'shadow' and there is no
    real-money setter anywhere."""
    __tablename__ = "model_approval_decision"
    id = Column(Integer, primary_key=True)
    model_version_id = Column(Integer, ForeignKey("model_version.id"),
                              nullable=False)
    model_version_name = Column(String(48))
    eval_version = Column(String(64))
    policy_version = Column(String(64))
    corpus_version = Column(String(48))
    approved_mode = Column(String(16), nullable=False)      # shadow
    approved = Column(Boolean, nullable=False)
    n_scored = Column(Integer)
    metrics_json = Column(Text)          # log_loss / brier / rps / n
    edge_json = Column(Text)             # M2_vs_M0 point + ci95 + significant
    limitations_json = Column(Text)
    report_json = Column(Text)           # the full evaluate_ladder report
    # the EXACT canonical bytes content_hash covers (V9.1 eval F4): the
    # audit recomputes sha256(decision_document) and compares, so a lock's
    # approval hash is independently verifiable, not merely present
    decision_document = Column(Text)
    approved_by = Column(String(32))
    content_hash = Column(String(64), unique=True, nullable=False)
    created_at = Column(DateTime(timezone=True))


class Player(LiveBase):
    """Player identity (V8.1 evaluation Phase 5). Keyed by provider id;
    team membership + availability live in the snapshot tables so a
    provider correction never overwrites what was true at T-10."""
    __tablename__ = "player"
    id = Column(Integer, primary_key=True)
    competition_slug = Column(String(32), ForeignKey("competition.slug"))
    espn_id = Column(String(16), unique=True)
    name = Column(String(96))
    position = Column(String(8))
    # the ESPN<->Sportec identity bridge (validated 99.5% on starters by
    # matching per-match participant lists). Lets a released ESPN lineup be
    # priced against a player's Sportec strength history. Indexed, not
    # unique-constrained: one-to-one is enforced in the builder so a bad
    # match can be corrected without a migration.
    sportec_id = Column(String(32), index=True)


class LineupSnapshot(LiveBase):
    """As-of team-selection state for a fixture, with full provenance.
    A T-10 run references the EXACT snapshot it saw — missing/unconfirmed
    lineups are recorded as such, never silently treated as confidence."""
    __tablename__ = "lineup_snapshot"
    id = Column(Integer, primary_key=True)
    fixture_id = Column(Integer, ForeignKey("fixture.id"), nullable=False)
    captured_at = Column(DateTime(timezone=True), nullable=False)
    observed_at = Column(DateTime(timezone=True))
    provider = Column(String(24))
    parser_version = Column(String(32))
    source_observation_id = Column(
        Integer, ForeignKey("source_observation.id"))
    status = Column(String(16))              # confirmed | partial | pending
    home_confirmed = Column(Boolean)
    away_confirmed = Column(Boolean)
    home_formation = Column(String(16))
    away_formation = Column(String(16))
    home_gk_player_id = Column(Integer, ForeignKey("player.id"))
    away_gk_player_id = Column(Integer, ForeignKey("player.id"))


class LineupEntry(LiveBase):
    """One player's selection state within a lineup snapshot."""
    __tablename__ = "lineup_entry"
    id = Column(Integer, primary_key=True)
    lineup_snapshot_id = Column(
        Integer, ForeignKey("lineup_snapshot.id"), nullable=False)
    side = Column(String(8), nullable=False)   # home | away
    player_id = Column(Integer, ForeignKey("player.id"))
    starter = Column(Boolean)
    is_goalkeeper = Column(Boolean)
    position = Column(String(8))
    jersey = Column(String(8))


class ModelInputArtifact(LiveBase):
    """The exact, retrievable input DOCUMENT a run simulated from
    (V8.1 evaluation Phase 2 / qualification #1). input_snapshot_hash
    proves integrity; this stores the BYTES so another machine can
    replay the run and get the same probabilities. Deduped by
    content_hash — identical inputs share one artifact."""
    __tablename__ = "model_input_artifact"
    id = Column(Integer, primary_key=True)
    schema_version = Column(String(64), nullable=False)
    content_hash = Column(String(64), unique=True, nullable=False)
    size_bytes = Column(Integer)
    document_json = Column(Text, nullable=False)   # canonical serialization
    created_at = Column(DateTime(timezone=True))


class PredictionRun(LiveBase):
    __tablename__ = "prediction_run"
    id = Column(String(36), primary_key=True, default=_uuid)
    fixture_id = Column(Integer, ForeignKey("fixture.id"), nullable=False)
    run_type = Column(String(16), nullable=False)  # scheduled|t60|t10|live
    scheduled_for = Column(DateTime(timezone=True))
    captured_at = Column(DateTime(timezone=True))
    seconds_before_kickoff = Column(Integer)
    status = Column(String(12), nullable=False, default="writing")
    canonical = Column(Boolean, nullable=False, default=False)
    model_version_id = Column(Integer, ForeignKey("model_version.id"))
    # the immutable approval decision this run was authorized under
    # (V9 eval F1/F10) — the CI-based record, not just a boolean
    model_approval_decision_id = Column(
        Integer, ForeignKey("model_approval_decision.id"))
    git_revision = Column(String(40))
    simulation_seed = Column(Integer)
    simulation_count = Column(Integer)
    input_snapshot_hash = Column(String(64))
    model_input_artifact_id = Column(
        Integer, ForeignKey("model_input_artifact.id"))
    # tranche-2 provenance entities not yet built (no team/player/
    # availability snapshot tables exist). RESERVED, never populated —
    # V9 eval F14 removed the earlier dishonest conflation that wrote the
    # lineup id into availability_snapshot_id. No FK: no table to point at.
    team_snapshot_id = Column(Integer)
    player_snapshot_id = Column(Integer)
    availability_snapshot_id = Column(Integer)
    # real provenance links, now enforced as foreign keys (V9 eval F5)
    lineup_snapshot_id = Column(Integer, ForeignKey("lineup_snapshot.id"))
    market_snapshot_id = Column(Integer, ForeignKey("market_snapshot.id"))
    created_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    failure_reason = Column(Text)
    # display extras (xg, scorelines, props, basis) frozen WITH the run —
    # recomputing later against refreshed ratings would silently diverge
    # from the stored contracts
    payload_json = Column(Text)
    # immutable approval-decision record: whether the model version was
    # approved for shadow AT CAPTURE TIME (V8.1 eval — flipping the
    # ModelVersion flag later must not retroactively re-authorize an old
    # run). Frozen True here because the F3 gate refuses to run otherwise.
    model_approved_at_run = Column(Boolean)
    # input-quality states frozen with the run (V8.1 eval Phase 5):
    # TEAM_DATA_FRESH / PLAYER_DATA_FRESH / AVAILABILITY_COMPLETE /
    # LINEUP_CONFIRMED / GOALKEEPER_CONFIRMED. Missing data is recorded
    # as false, never absorbed into the model as confidence.
    input_quality_json = Column(Text)
    __table_args__ = (
        # ONE canonical complete T-10 per fixture — the same partial
        # unique invariant on SQLite (tests) and PostgreSQL (production).
        # Explicit per-dialect WHERE text: building this from detached
        # typeless Column() objects rendered `canonical IS 1`, which
        # SQLite accepts and PostgreSQL rejects — the first live-plane
        # migration died on it (Jul 23). A compilation test now pins
        # both dialects' DDL.
        Index("uq_fixture_canonical_t10", "fixture_id",
              unique=True,
              postgresql_where=text(
                  "run_type = 't10' AND canonical AND "
                  "status = 'complete'"),
              sqlite_where=text(
                  "run_type = 't10' AND canonical = 1 AND "
                  "status = 'complete'")),
    )


class PredictionContract(LiveBase):
    __tablename__ = "prediction_contract"
    id = Column(Integer, primary_key=True)
    prediction_run_id = Column(String(36),
                               ForeignKey("prediction_run.id"),
                               nullable=False)
    market_contract_id = Column(Integer,
                                ForeignKey("market_contract.id"))
    outcome_key = Column(String(32), nullable=False)
    raw_probability = Column(Float, nullable=False)
    anchored_probability = Column(Float)
    market_quote_id = Column(Integer, ForeignKey("market_quote.id"))
    __table_args__ = (
        UniqueConstraint("prediction_run_id", "market_contract_id"),
        # SQL NULLs are pairwise-distinct, so the constraint above never
        # fired for unmapped contracts (V8 evaluation): the outcome key
        # itself must be unique per run
        UniqueConstraint("prediction_run_id", "outcome_key"),
    )


class MarketEvent(LiveBase):
    __tablename__ = "market_event"
    id = Column(Integer, primary_key=True)
    competition_slug = Column(String(32),
                              ForeignKey("competition.slug"))
    kalshi_event_ticker = Column(String(64), unique=True, nullable=False)
    series = Column(String(24))                     # KXMLSGAME | KXMLSCUP
    title = Column(String(120))
    fixture_id = Column(Integer, ForeignKey("fixture.id"))
    settlement_scope = Column(String(24))           # regular_time | ...
    mapped_via = Column(String(24))                 # alias | manual
    mapping_approved = Column(Boolean, default=False, nullable=False)


class MarketContract(LiveBase):
    __tablename__ = "market_contract"
    id = Column(Integer, primary_key=True)
    market_event_id = Column(Integer, ForeignKey("market_event.id"),
                             nullable=False)
    ticker = Column(String(80), unique=True, nullable=False)
    side_label = Column(String(64))
    outcome_key = Column(String(32))                # home_win|draw|away_win


class MarketSnapshot(LiveBase):
    """The atomic evidence header a T-10 lock points at (V8 evaluation
    F1): one row per capture attempt, with expected-vs-actual coverage
    counts and a status gate. A run may only become canonical against a
    snapshot whose status is 'complete' — a zero-quote or partial
    capture stays 'failed' and the lock visibly does not happen."""
    __tablename__ = "market_snapshot"
    id = Column(Integer, primary_key=True)
    fixture_id = Column(Integer, ForeignKey("fixture.id"), nullable=False)
    captured_at = Column(DateTime(timezone=True), nullable=False)
    # `status` is CAPTURE-completeness only (all expected records
    # observed or explicitly recorded absent). Tradeability is a
    # SEPARATE concept — `execution_ready` — because a complete
    # capture can legitimately contain no-bid contracts (V8.1 eval
    # qualification #2). The lock predicate itself is versioned so
    # "full book" cannot change meaning silently (qualification #3).
    status = Column(String(12), nullable=False, default="writing")
    policy_version = Column(String(64))
    provider_schema_version = Column(String(32))
    events_expected = Column(Integer)
    events_captured = Column(Integer)
    contracts_expected = Column(Integer)
    quotes_written = Column(Integer)
    quotes_with_prices = Column(Integer)
    quotes_without_prices = Column(Integer)
    depth_rows_written = Column(Integer)
    oldest_quote_age_seconds = Column(Integer)          # over ALL quotes
    # freshness computed specifically over the REQUIRED game quotes, with
    # an explicit basis (V9 eval F9): a missing provider timestamp must
    # not read as age zero / "fresh". basis is 'provider' when every game
    # quote carried a provider timestamp, 'capture_time' when we fell back
    # to our own capture clock, 'none' when no game quote was priced.
    game_oldest_quote_age_seconds = Column(Integer)
    freshness_basis = Column(String(16))
    required_families_complete = Column(Boolean)
    execution_ready = Column(Boolean)
    failure_reason = Column(Text)


class MarketQuote(LiveBase):
    """Full-book quote in integer CENTS (fixed point, never binary
    float). YES ask derives from NO bid (1 - no_bid) and vice versa —
    both stored as captured."""
    __tablename__ = "market_quote"
    id = Column(Integer, primary_key=True)
    market_contract_id = Column(Integer,
                                ForeignKey("market_contract.id"),
                                nullable=False)
    market_snapshot_id = Column(Integer,
                                ForeignKey("market_snapshot.id"))
    captured_at = Column(DateTime(timezone=True), nullable=False)
    provider_timestamp = Column(DateTime(timezone=True))
    yes_bid_c = Column(Integer)
    yes_bid_size = Column(Integer)
    yes_ask_c = Column(Integer)
    yes_ask_size = Column(Integer)
    no_bid_c = Column(Integer)
    no_bid_size = Column(Integer)
    no_ask_c = Column(Integer)
    no_ask_size = Column(Integer)
    last_trade_c = Column(Integer)
    last_trade_at = Column(DateTime(timezone=True))
    volume = Column(Integer)
    open_interest = Column(Integer)
    status = Column(String(16))
    rules_hash = Column(String(64))
    fee_schedule_version = Column(String(64))
    source_observation_id = Column(Integer,
                                   ForeignKey("source_observation.id"))
    # EXACT provider fixed-point values retained beside the derived integer
    # cents (V9 eval F7): subpenny dollar-string prices and fractional
    # *_fp sizes are evidence and must not be rounded away at ingest. The
    # integer-cent columns above stay the executable comparator; these are
    # the lossless record. provider_precision names the schema they came
    # from so a later reader knows how to interpret them.
    yes_bid_dollars = Column(String(16))
    yes_ask_dollars = Column(String(16))
    no_bid_dollars = Column(String(16))
    no_ask_dollars = Column(String(16))
    sizes_fp_json = Column(Text)         # exact *_fp size strings, by field
    provider_precision = Column(String(24))
    # the ACTIVE price grid at capture (V9.3 eval F12). Kalshi can change a
    # market's price structure during its lifecycle, so a historical reader
    # needs the grid that was valid when this quote was frozen — otherwise
    # a subpenny-era price cannot be interpreted against a cent-era book.
    price_level_structure = Column(String(32))   # e.g. linear_cent
    price_ranges_json = Column(Text)             # [{start,end,step}, ...]


class MarketDepthLevel(LiveBase):
    __tablename__ = "market_depth_level"
    id = Column(Integer, primary_key=True)
    market_quote_id = Column(Integer, ForeignKey("market_quote.id"),
                             nullable=False)
    side = Column(String(8), nullable=False)        # yes | no
    price_c = Column(Integer, nullable=False)       # derived (rounded)
    size = Column(Integer, nullable=False)          # derived (truncated)
    # exact provider values (V9 eval F7): a large paper order walks depth,
    # so subpenny prices and fractional sizes at each level are material.
    price_dollars = Column(String(16))
    size_fp = Column(String(24))


class PaperSignal(LiveBase):
    """A paper-trading DECISION on one contract of a canonical lock
    (V8.1 evaluation Phase 7). PAPER ONLY — no real order is ever
    placed. Records the model's read and whether the execution gates
    passed; a rejection keeps its reason so the ledger has no
    survivorship bias. One per (run, contract)."""
    __tablename__ = "paper_signal"
    id = Column(Integer, primary_key=True)
    prediction_run_id = Column(String(36),
                               ForeignKey("prediction_run.id"),
                               nullable=False)
    market_contract_id = Column(Integer,
                                ForeignKey("market_contract.id"))
    market_quote_id = Column(Integer, ForeignKey("market_quote.id"))
    fixture_id = Column(Integer, ForeignKey("fixture.id"))
    outcome_key = Column(String(32))
    policy_version = Column(String(64))
    model_probability = Column(Float)
    ask_c = Column(Integer)              # display (rounded)
    fee_c = Column(Integer)              # display (rounded)
    # EXACT provider-precision economics (V9.1 eval F2/F3), stored beside
    # the display cents so paper P&L can be reconciled to the centicent
    ask_dollars = Column(String(16))
    fee_dollars = Column(String(16))
    net_edge = Column(Float)             # model_p - (ask + fee) AT THE QUOTE
    # the edge the fill ACTUALLY achieved: model_p - (avg fill + per-contract
    # fee from the real allocations). V9.3 eval F3 — the quoted edge
    # authorised the order, but the depth walk can pay a worse average, so
    # the policy is re-applied to these economics before the fill stands.
    realized_net_edge = Column(Float)
    decision = Column(String(12))        # fill | reject
    reject_reason = Column(String(48))
    created_at = Column(DateTime(timezone=True))
    # NULL = written inline at the lock. A timestamp = recovered later
    # from the frozen book (deterministic, but not the same evidence as
    # a signal that existed at lock time — see paper.paper_coverage).
    backfilled_at = Column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("prediction_run_id", "market_contract_id",
                         name="uq_paper_signal_run_contract"),
    )


class PaperFill(LiveBase):
    """The simulated execution of a filled PaperSignal against the
    FROZEN lock book — realistic: ask entry walking real depth, fees,
    slippage, partial fills. Settled once the fixture resolves.
    Deterministic from the frozen snapshot (the replay acceptance)."""
    __tablename__ = "paper_fill"
    id = Column(Integer, primary_key=True)
    paper_signal_id = Column(Integer, ForeignKey("paper_signal.id"),
                             nullable=False)
    requested_contracts = Column(Integer)
    filled_contracts = Column(Integer)            # display (int)
    avg_fill_price_c = Column(Integer)            # display (rounded)
    best_ask_c = Column(Integer)
    slippage_c = Column(Integer)         # avg fill - best ask
    fee_c = Column(Integer)
    cost_c = Column(Integer)             # contracts*price + fee
    # EXACT provider-precision economics (V9.1 eval F2/F3): fractional
    # fills, subpenny weighted price, and centicent fees/costs, retained
    # beside the display cents so P&L reconciles exactly
    filled_contracts_fp = Column(String(24))
    avg_fill_price_dollars = Column(String(16))
    fee_dollars = Column(String(16))
    cost_dollars = Column(String(24))
    levels_consumed = Column(Integer)
    # V9.3 eval F4: a fill built from the top quote because no order-book
    # depth was captured is a TOP-OF-BOOK ESTIMATE, not a depth-backed
    # execution. Recorded explicitly so execution-grade metrics can exclude
    # it instead of silently mixing the two.
    execution_class = Column(String(32))   # bounded_depth | top_of_book_estimate
    # the exact per-level allocations the fee was computed from (V9.3 eval
    # F2): [{"seq","price","qty","fee"}]. The general fee is non-linear in
    # price, so one fee at the VWAP is not the sum of the per-fill fees.
    allocations_json = Column(Text)
    fee_policy_version = Column(String(64))
    latency_ms = Column(Integer)         # recorded assumption
    reason = Column(String(48))          # filled | partial | no_depth
    created_at = Column(DateTime(timezone=True))
    # settlement, once the fixture is post
    status = Column(String(12), default="open")   # open | settled
    outcome_hit = Column(Boolean)
    payout_c = Column(Integer)
    pnl_c = Column(Integer)
    payout_dollars = Column(String(24))
    pnl_dollars = Column(String(24))
    settled_at = Column(DateTime(timezone=True))


class RegistryDiscovery(LiveBase):
    """A durable record of a market-discovery sweep's COMPLETENESS (V9.1
    eval F10). The cursor helper can report a page cap, but that state was
    transient — a truncated local registry could silently define an
    incomplete universe as 'expected' for a lock's completeness gate. Each
    sweep now persists whether every series exhausted its cursor or hit the
    cap, so completeness is first-class and auditable."""
    __tablename__ = "registry_discovery"
    id = Column(Integer, primary_key=True)
    competition_slug = Column(String(32))
    provider = Column(String(24))
    complete = Column(Boolean, nullable=False)
    truncated_series_json = Column(Text)     # series that hit the page cap
    events_seen = Column(Integer)
    newly_mapped = Column(Integer)
    unmapped = Column(Integer)
    contracts_filled = Column(Integer)
    completed_at = Column(DateTime(timezone=True))


class MlsTeamMatchStat(LiveBase):
    """Per-match, per-team OFFICIAL MLS (Sportec/StatsPerform) statistics —
    the richer shot/xG signal the goals-only model cannot see. One row per
    (fixture, side). Sourced from stats-api.mlssoccer.com, content-hashed
    into SourceObservation, and attached to OUR fixture by (kickoff date,
    the two clubs' resolved team ids). ADDITIVE EVIDENCE: the model reads
    these when present and falls back to goals when absent — a fixture is
    never dropped for missing stats. `xg` is the provider's own expected
    goals (not our proxy); `xg_against` denormalizes the opponent row's xg
    so a defence rating needs no self-join and survives a one-sided row."""
    __tablename__ = "mls_team_match_stat"
    id = Column(Integer, primary_key=True)
    fixture_id = Column(Integer, ForeignKey("fixture.id"), nullable=False)
    team_id = Column(Integer, ForeignKey("team.id"), nullable=False)
    side = Column(String(8), nullable=False)          # home | away
    sportec_match_id = Column(String(32))             # MLS-MAT-...
    sportec_club_id = Column(String(32))              # MLS-CLU-...
    goals = Column(Integer)
    goals_conceded = Column(Integer)
    xg = Column(Float)                                # provider xG, for
    xg_against = Column(Float)                        # opponent xG (against)
    shots_total = Column(Integer)                     # shots_at_goal_sum
    shots_inside_box = Column(Integer)
    shots_outside_box = Column(Integer)
    shots_on_target = Column(Integer)
    corners = Column(Integer)
    passes_successful = Column(Integer)
    passes_total = Column(Integer)
    source_observation_id = Column(
        Integer, ForeignKey("source_observation.id"))
    observed_at = Column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("fixture_id", "side"),)


class MlsPlayerMatchStat(LiveBase):
    """Per-match player statistics from the official MLS players endpoint —
    the durable substrate for player-strength / goalkeeper features (the
    M4/M5 rungs). Captured now so the history exists; consumed by the model
    only once a feature is MEASURED to help. `xg` and `is_goalkeeper` come
    straight from the provider; `minutes` is normalized_player_minutes."""
    __tablename__ = "mls_player_match_stat"
    id = Column(Integer, primary_key=True)
    fixture_id = Column(Integer, ForeignKey("fixture.id"), nullable=False)
    team_id = Column(Integer, ForeignKey("team.id"))
    side = Column(String(8))                          # home | away
    sportec_match_id = Column(String(32))
    sportec_club_id = Column(String(32))
    sportec_player_id = Column(String(32))
    player_name = Column(String(96))
    is_goalkeeper = Column(Boolean)
    minutes = Column(Float)
    goals = Column(Integer)
    assists = Column(Integer)
    xg = Column(Float)
    shots_total = Column(Integer)
    shots_on_target = Column(Integer)
    shots_faced = Column(Integer)                     # shots_on_goal_suffered
    source_observation_id = Column(
        Integer, ForeignKey("source_observation.id"))
    observed_at = Column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("fixture_id", "sportec_player_id"),
    )


class CorpusExport(LiveBase):
    """An IMMUTABLE published corpus version (V9 eval F3). build_corpus
    reads live state, so its bytes legitimately drift as the database
    grows — meaning the same version LABEL served fresh each call is not
    immutable. Publishing freezes one version's bytes + manifest into this
    row; the public endpoint serves a published version FROM HERE, never a
    rebuild. A version is written once — re-publishing the same label is
    refused. (In-database bytes are the immutable artifact at the current
    corpus size; object storage is the documented scale-up path.)"""
    __tablename__ = "corpus_export"
    id = Column(Integer, primary_key=True)
    version = Column(String(48), unique=True, nullable=False)
    schema_version = Column(String(64))
    manifest_hash = Column(String(64), nullable=False)
    manifest_json = Column(Text, nullable=False)
    bundle_json = Column(Text, nullable=False)     # full self-contained bundle
    backend_revision = Column(String(40))
    size_bytes = Column(Integer)
    published_at = Column(DateTime(timezone=True))
