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

from sqlalchemy import (Boolean, CheckConstraint, Column, DateTime, Float,
                        ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, text)
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
    # the git revision this decision was computed under, so a later boot
    # can rehash the engine signature the way replay already does
    # (model_mls.engine_matches) and tell "the repo moved" apart from
    # "the engine changed". NOT covered by content_hash and NOT in
    # decision_document: it is provenance ABOUT the decision, not part of
    # what was decided — putting it in the hash would make two decisions
    # identical in every approved fact hash differently merely because
    # they shipped from different commits. NULL on rows written before
    # this column existed, which the second arm refuses (fail closed).
    code_revision = Column(String(40))
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
    # V9.5 eval: the COMPLETE raw order-book response these levels were
    # parsed from. Only the retained best-N levels were stored, so
    # omitted depth could not be reconstructed, a corrected parser could
    # not be re-run against the original book, and best-N selection was
    # not independently auditable from published bytes.
    book_observation_id = Column(Integer,
                                 ForeignKey("source_observation.id"))


class PersonalBet(LiveBase):
    """A bet Son FORMED A VIEW ON — taken or passed. Documentation, and
    the input side of the execution-fidelity pilot.

    This is NOT research evidence and must never touch one. The paper
    ledger is mechanical: every eligible leg, fixed policy, frozen book,
    rejections retained. These rows are human-selected, so they carry
    selection bias by construction and can establish execution fidelity
    but never edge. No join to PaperSignal/PaperFill exists or may be
    added, and nothing here reaches model_eval or an approval decision.

    `status` deliberately includes `passed`. A journal holding only bets
    that were taken has exactly the survivorship problem the paper
    ledger's retained rejections exist to prevent — it cannot separate
    "the model is good" from "I remember my winners". A view is recorded
    when it FORMS (`considered`), then resolves to `taken` or `passed`.

    Multi-league is schema-only (platform audit A2): `competition_slug`
    is carried, but no second competition is wired end to end.
    """
    __tablename__ = "personal_bet"
    id = Column(Integer, primary_key=True)
    competition_slug = Column(String(32), ForeignKey("competition.slug"))
    fixture_id = Column(Integer, ForeignKey("fixture.id"), nullable=False)
    # the raw ticker is ALWAYS recorded; the FK may be absent for a
    # market we have not mapped
    market_ticker = Column(String(128), nullable=False)
    market_contract_id = Column(Integer, ForeignKey("market_contract.id"))
    outcome_key = Column(String(32))
    # considered | taken | passed | void
    status = Column(String(16), nullable=False, default="considered")
    stated_price_dollars = Column(String(16))
    stated_size = Column(String(24))
    # observed_quote | stated_only — see the falsifiability rule. A
    # stated_only entry is recorded honestly and counted nowhere.
    price_basis = Column(String(16), nullable=False, default="stated_only")
    market_quote_id = Column(Integer, ForeignKey("market_quote.id"))
    quote_observed_at = Column(DateTime(timezone=True))
    recorded_at = Column(DateTime(timezone=True), nullable=False)
    resolved_at = Column(DateTime(timezone=True))   # taken/passed moment
    rationale = Column(Text)
    # the shadow model's probability FROZEN at record time, with the run
    # it came from — a later re-run must not change what was recorded
    model_probability = Column(Float)
    prediction_run_id = Column(String(36),
                               ForeignKey("prediction_run.id"))
    settled_outcome = Column(String(32))
    settled_at = Column(DateTime(timezone=True))
    content_hash = Column(String(64))
    # a resolution is IMMUTABLE once set (journal-P0 F5). A mistaken
    # entry is corrected by a NEW row citing the old one here — the
    # record of the mistake is part of the record, never rewritten.
    corrects_bet_id = Column(Integer, ForeignKey("personal_bet.id"))
    # journal-P0-H: Kalshi publishes NO quote timestamp, so our capture
    # clock is the only evidence of when a book was observed. The age of
    # the cited quote AT RECORD TIME, and the ceiling that was in force
    # then, are stored so the row stays readable after the config moves —
    # reclassifying an old row under a new ceiling would rewrite history.
    quote_age_seconds = Column(Integer)
    quote_max_age_seconds = Column(Integer)
    # why a CITED quote was refused as the price basis: stale_capture |
    # future_capture | not_found | no_capture_time. Absent when no quote
    # was cited or the citation was accepted — the difference between
    # "no quote offered" and "a quote refused" is never left implicit.
    quote_rejection_reason = Column(String(24))

    __table_args__ = (
        # journal-P1-G: correction chains stay LINEAR under CONCURRENCY.
        # The handler's read-then-insert check loses the race — two
        # simultaneous corrections of the same entry both read "no head"
        # and both commit, producing two heads and an ambiguous
        # effective observation. The database is the only place this can
        # be settled. Partial so ordinary rows (corrects_bet_id NULL)
        # are unconstrained; explicit per-dialect WHERE text for the
        # same reason the canonical-lock index carries it.
        Index("uq_personal_bet_correction_head", "corrects_bet_id",
              unique=True,
              postgresql_where=text("corrects_bet_id IS NOT NULL"),
              sqlite_where=text("corrects_bet_id IS NOT NULL")),
    )


class PersonalBetExecution(LiveBase):
    """A REAL fill (or a real failure to fill) by a consenting third
    party against a PersonalBet. Real money, reconcilable against the
    exchange.

    `status` includes `not_filled` for the same reason PersonalBet
    includes `passed`: a bet the friend could not get at an acceptable
    price is evidence about LIQUIDITY, which is half of what an
    execution-fidelity pilot measures. Recording only successful fills
    reproduces, one level down, the bias the pilot exists to detect.

    Idempotency (journal-P0 F5): (exchange_order_id, status) is unique,
    so a retried POST is a no-op instead of a duplicate fill.

    That key alone is NOT the idempotency contract (journal-P0-I). The
    same key carrying different economics is a CONFLICT, not a retry —
    returning the earlier row for it silently accepts contradictory
    evidence about a third party's money as though the earlier row had
    confirmed the new payload. Each write path therefore stores a
    canonical hash of the payload it accepted, and a repeat whose hash
    differs is refused.
    """
    __tablename__ = "personal_bet_execution"
    __table_args__ = (
        UniqueConstraint("exchange_order_id", "status",
                         name="uq_personal_bet_execution_order_status"),
    )
    id = Column(Integer, primary_key=True)
    personal_bet_id = Column(Integer, ForeignKey("personal_bet.id"),
                             nullable=False)
    account_label = Column(String(64), nullable=False)
    # non-null required to accept a row: this is someone else's money
    consent_recorded_at = Column(DateTime(timezone=True), nullable=False)
    # filled | partial | not_filled
    status = Column(String(16), nullable=False, default="filled")
    not_filled_reason = Column(String(32))
    best_available_price_dollars = Column(String(16))
    fill_price_dollars = Column(String(16))
    filled_contracts = Column(String(24))
    fee_paid_dollars = Column(String(16))
    filled_at = Column(DateTime(timezone=True))
    # the book AT FILL TIME, so slippage decomposes into market drift
    # vs execution cost rather than staying one uninterpretable number
    market_quote_id_at_fill = Column(Integer,
                                     ForeignKey("market_quote.id"))
    exchange_order_id = Column(String(64))
    settlement_credit_dollars = Column(String(16))
    settled_at = Column(DateTime(timezone=True))
    reconciled = Column(Boolean, default=False)
    reconciliation_note = Column(Text)
    # publication gate (journal-P0 F2): the corpus is public bytes, and
    # this row is a third party's financial record. Private fields
    # (account label, order id, fill economics, consent provenance) are
    # exported ONLY when this explicit consent is set. Default false —
    # absence of consent is never publication.
    publication_consent = Column(Boolean, default=False)
    # journal-P0-H: the fill book's capture age relative to `filled_at`,
    # and the ceiling in force when it was accepted. Stored so the
    # decomposed metrics can say what evidence they rest on, and so a
    # later config change cannot silently reclassify an accepted row.
    fill_quote_age_seconds = Column(Integer)
    fill_quote_max_age_seconds = Column(Integer)
    # journal-P0-I: canonical hash of the payload each write path
    # accepted. A retry hashing the same is a no-op; a retry hashing
    # differently is a conflict and writes nothing.
    execution_payload_hash = Column(String(64))
    settlement_payload_hash = Column(String(64))
    reconciliation_payload_hash = Column(String(64))


class BroadcastLog(LiveBase):
    """Every message a live session pushed to Discord/ntfy.

    The session is the analyser; the alert channels are its megaphone.
    What was said live, and when, is documentation in the same sense the
    journal is — and it lets a session that dropped mid-match pick up
    the thread instead of contradicting itself.

    `source` separates a computed rule from an agent's judgement. A
    measurement and an opinion must never look alike in the channel.
    """
    __tablename__ = "broadcast_log"
    id = Column(Integer, primary_key=True)
    fixture_id = Column(Integer, ForeignKey("fixture.id"))
    channel = Column(String(16), nullable=False)      # action | detail
    source = Column(String(16), nullable=False)       # session | computed
    session_label = Column(String(64))
    message = Column(Text, nullable=False)
    # the structured values behind the prose, so a broadcast can be
    # checked against what the API actually returned that turn
    claims_json = Column(Text)
    delivered = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True))
    # journal-P0-A: the EXACT payload handed to the transports (prefix +
    # possibly-truncated prose + qualifier), its hash, and each
    # transport's own acceptance — so the log describes what was
    # actually SENT, not what the caller composed. `message` above stays
    # the operator's full prose; this is the wire record.
    dispatched_body = Column(Text)
    dispatched_sha256 = Column(String(64))
    transports_json = Column(Text)
    # journal-P0-G: the id of the interactive-session capability the
    # server VERIFIED before dispatching. `source` records what the
    # caller claimed; this records what the server checked. A row
    # without it predates the capability gate.
    dispatch_capability_id = Column(String(64))


class PaperEvaluationContext(LiveBase):
    """The complete paper/risk state FROZEN at lock time.

    V9.5 evaluation, critical finding 1: `paper_trade_lock` read LIVE
    kill switches, LIVE open exposure and the LIVE approved-model state,
    so a recovery run was never a pure replay — the same frozen lock
    could legitimately yield a different decision later (the evaluator
    demonstrated it by flipping a kill switch). The release nonetheless
    called the backfill "faithful by construction", which was true only
    of the market inputs.

    Every non-market input a paper decision depends on is captured here
    at lock time, so evaluation becomes a pure function of

        frozen lock evidence  +  this row

    A reconstruction that can load this row reproduces the lock-time
    decision exactly. One without it is explicitly degraded — see
    PaperSignal.evaluation_mode."""
    __tablename__ = "paper_evaluation_context"
    id = Column(Integer, primary_key=True)
    prediction_run_id = Column(String(36),
                               ForeignKey("prediction_run.id"),
                               nullable=False, unique=True)
    captured_at = Column(DateTime(timezone=True))
    exec_policy_version = Column(String(64))
    exec_policy_json = Column(Text)
    fee_policy_version = Column(String(64))
    fee_policy_json = Column(Text)
    risk_policy_version = Column(String(64))
    risk_policy_json = Column(Text)
    # the gates that read mutable state — frozen here
    kill_switches_json = Column(Text)      # [] when nothing was tripped
    exposure_json = Column(Text)           # open exposure, exact dollars
    model_approved = Column(Boolean)
    model_approval_decision_id = Column(
        Integer, ForeignKey("model_approval_decision.id"))
    engine_signature_hash = Column(String(64))
    # sha256 over the canonical document above, so a context cannot be
    # edited after the fact without detection
    content_hash = Column(String(64))


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
    # V9.5 eval C1 — provenance of the DECISION, not just its timing:
    #   contemporaneous  evaluated at lock time against live state
    #   reconstructed    replayed later. Pure when it carries a frozen
    #                    context; DEGRADED when that FK is NULL, because
    #                    then recovery-time risk state was read instead.
    # Only contemporaneous rows may feed headline execution metrics.
    evaluation_mode = Column(String(20), default="contemporaneous")
    paper_evaluation_context_id = Column(
        Integer, ForeignKey("paper_evaluation_context.id"))
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
    cap, so completeness is first-class and auditable.

    V9.5 eval, critical finding 2: completeness was decided by pagination
    truncation ALONE. A provider request that failed outright was logged
    and skipped, so a sweep that reached nothing at all still persisted
    complete=true and satisfied the canonical lock's 'recent complete
    registry' prerequisite. `family_outcomes_json` now records a stage
    outcome per required family and `complete` is the AND of them."""
    __tablename__ = "registry_discovery"
    id = Column(Integer, primary_key=True)
    competition_slug = Column(String(32))
    provider = Column(String(24))
    complete = Column(Boolean, nullable=False)
    truncated_series_json = Column(Text)     # series that hit the page cap
    # {series: SUCCESS|REQUEST_FAILED|PAGINATION_CAP|PARSE_FAILED|
    #          CONTRACT_DISCOVERY_FAILED}
    family_outcomes_json = Column(Text)
    incomplete_reasons_json = Column(Text)
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
    # the provider's OWN claim about this payload, recorded so it is
    # auditable rather than assumed: data_status is "postmatch" (final) on
    # every match measured — including the seven whose xG is provably wrong
    # — which is exactly why the plausibility guard has to be DERIVED from
    # the payload's internals instead of read off a provider flag.
    # (Declared after observed_at to match the order migration
    # c4e8a91f27b6 produces by appending them to an existing table.)
    data_status = Column(String(24))                  # postmatch | ...
    scope = Column(String(24))                        # match | ...
    # verdict of that derived guard (mls_stats.xg_plausibility): the xG on
    # this row is contradicted by the shot/goal counts in the SAME payload.
    # Quarantined rows are still STORED — never rewritten, never deleted;
    # historical evidence is not edited to improve a result (AGENTS.md §3).
    # NULL means "never screened", which is NOT the same claim as False
    # ("screened and passed") — quarantine_report keeps the two apart.
    xg_quarantined = Column(Boolean)
    xg_quarantine_reason = Column(String(160))        # which check + measured
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


class HunterFinding(LiveBase):
    """One observational market-structure finding from the Kalshi soccer
    hunter. APPEND-ONLY: a later cycle that no longer sees the anomaly
    marks the row expired with a second capture timestamp — it never
    deletes or rewrites the original observation.

    Every price in legs_json carries OUR capture clock; Kalshi publishes
    no quote timestamp (updated_time is the definition clock, ~30h stale
    on active markets — see markets.QUOTE_FRESHNESS_NOTE). Margins are
    exact Decimal net of the exact versioned fee policy, serialized as
    strings. Provider-controlled strings (series/event/market tickers)
    get generous widths — a 26-char fee-policy string in a 24-char
    column once silently destroyed a night of production fills.

    Cross-process idempotency: finding_key is the provider-stable
    identity (type|series|event|market) and a PARTIAL unique index
    enforces at most ONE OPEN row per key at the database — Railway
    keeps the old container serving while the new one boots, so two
    hunter processes legitimately overlap and an in-Python dedup check
    alone is a race. Alert delivery is a durable claim on the row
    (alert_claimed_at, leased) taken BEFORE any bytes leave the process;
    alerted_at is set only on a confirmed transport acceptance and
    alert_results_json retains the per-transport outcome either way."""
    __tablename__ = "hunter_finding"
    id = Column(Integer, primary_key=True)
    # competition slug when the event maps to a known fixture (mls-2026);
    # otherwise NULL and the series ticker is the grouping key
    competition_slug = Column(String(32))
    series = Column(String(64), nullable=False)          # KXUCLGAME ...
    event_ticker = Column(String(128))
    market_ticker = Column(String(128))    # market-level findings only
    finding_type = Column(String(32), nullable=False)
    # provider-stable identity: finding_type|series|event|market. The
    # partial unique index below makes "one open finding per key" a
    # database invariant, not a process-local one.
    finding_key = Column(String(384), nullable=False)
    # SUM_BELOW_ONE | CROSSED_BOOK | POST_CERTAINTY |
    # WIDE_SPREAD | THIN_BOOK | IN_PLAY_OVERREACTION | MODEL_EDGE
    # liquidity/repricing flags are CONTEXT, never wins: labelled so,
    # never alerted
    is_context = Column(Boolean, nullable=False, default=False)
    # the full arithmetic: legs, exact prices, exact fee per leg, margin
    legs_json = Column(Text, nullable=False)
    net_margin_dollars = Column(String(24))       # exact Decimal, string
    fee_policy_version = Column(String(64))
    # MODEL_EDGE only: the standing approval qualifier (edge, n, CI,
    # significance) copied verbatim from the active approval decision
    model_qualifier_json = Column(Text)
    fixture_id = Column(Integer, ForeignKey("fixture.id"))
    # the per-series Kalshi fetch-COMPLETION clock — the only timing
    # evidence a quote gets (Kalshi publishes no quote timestamp), so it
    # is preserved verbatim, never replaced by a later cycle-end clock
    first_captured_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True))
    observed_cycles = Column(Integer, default=1)
    # the separate clocks, kept apart on purpose: when the detector
    # evaluated, and when the row reached the persistence phase
    detected_at = Column(DateTime(timezone=True))
    persisted_at = Column(DateTime(timezone=True))
    # POST_CERTAINTY only: when the ESPN state was (re-)read — always at
    # detection time, never cached from a previous cycle
    espn_captured_at = Column(DateTime(timezone=True))
    status = Column(String(16), nullable=False, default="open")
    # open | expired
    expired_at = Column(DateTime(timezone=True))
    alerted_at = Column(DateTime(timezone=True))
    # durable alert claim (leased) + retained per-transport outcomes
    alert_claimed_at = Column(DateTime(timezone=True))
    alert_results_json = Column(Text)
    __table_args__ = (
        Index("ix_hunter_finding_status_type", "status", "finding_type"),
        # at most ONE open row per provider-stable key, enforced by the
        # DATABASE on both dialects (same pattern as the canonical-lock
        # partial unique index — the real cross-container guard)
        Index("uq_hunter_finding_open_key", "finding_key", unique=True,
              postgresql_where=text("status = 'open'"),
              sqlite_where=text("status = 'open'")),
        # observation clocks may never regress on a row: last_seen_at is
        # a LATER observation of the same anomaly, never an earlier one.
        # Overlapping Railway containers commit out of order, so this is
        # a database invariant, not a process-local one (P0-3).
        CheckConstraint("last_seen_at IS NULL OR "
                        "last_seen_at >= first_captured_at",
                        name="ck_hunter_finding_clock_monotonic"),
        # POST_CERTAINTY only: the quote must have been captured AFTER
        # finality was observed. A pre-final price paired with a settled
        # outcome manufactures a margin that never existed, so the
        # ordering is enforced by the TABLE and not merely by the
        # detector that happens to write it (P0-2).
        CheckConstraint("espn_captured_at IS NULL OR "
                        "first_captured_at >= espn_captured_at",
                        name="ck_hunter_finding_quote_after_finality"),
    )


class HunterObservationWatermark(LiveBase):
    """The newest observation clock any process has applied to one
    hunter finding_key — the cross-process chronological reconciliation
    point (P0-3).

    Railway deploys overlap old and new containers deliberately
    (teardown is OFF), so an OLDER container's scan can legitimately
    commit AFTER a newer one's. Without a watermark that straggler
    re-opens a finding a newer clean scan already expired, and can send
    an alert about a book that no longer exists. Advancing this row is
    an atomic `UPDATE ... WHERE observed_through < :clock`: the DATABASE
    decides which write is newest — exactly like the leased alert claim
    — and a losing (stale) write is ignored rather than applied.

    Keyed by finding_key rather than by finding id on purpose: the
    watermark has to OUTLIVE the row it guards, or the straggler would
    simply insert a fresh open row after the expiry.

    Growth: one row per DISTINCT finding key ever observed (type x
    series x event x market) — the same order as hunter_finding itself
    and far smaller per row, written only when a key's observation
    clock actually advances, never once per cycle."""
    __tablename__ = "hunter_observation_watermark"
    id = Column(Integer, primary_key=True)
    finding_key = Column(String(384), nullable=False, unique=True)
    # the newest per-series capture clock (OURS) applied to this key
    observed_through = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True))


class HunterCycle(LiveBase):
    """One hunter scan cycle — the heartbeat AND the denominator. A count
    of findings without cycles-run/markets-scanned is a defect in this
    repo, and a scanner that dies silently must be visible as dead, not
    as a quiet market. One row per cycle, including failed ones.

    Completeness is fail-closed: series_outcomes_json records the
    per-series outcome (SUCCESS / REQUEST_FAILED / PAGINATION_CAP /
    DETECTION_FAILED with detail), and any non-SUCCESS outcome makes the
    cycle 'degraded' — "didn't look" is never recordable as "no
    anomaly", and only a SUCCESS pass for a series may expire that
    series' open findings."""
    __tablename__ = "hunter_cycle"
    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True))
    status = Column(String(16), nullable=False, default="running")
    # running | complete | degraded | failed
    series_scanned = Column(Integer)
    # series whose scan was NOT a complete success this cycle
    series_failed = Column(Integer)
    # per-series outcome map: {ticker: {outcome, detail?}}
    series_outcomes_json = Column(Text)
    events_scanned = Column(Integer)
    markets_scanned = Column(Integer)
    findings_new = Column(Integer)
    findings_expired = Column(Integer)
    # writes this cycle produced that a NEWER process had already
    # superseded — ignored at the watermark rather than applied. A
    # silent rejection would be its own kind of missing evidence, so
    # the count rides on the heartbeat.
    stale_writes_rejected = Column(Integer)
    request_count = Column(Integer)
    # the SECOND provider's spend (API-Football live in-play statistics),
    # counted apart from Kalshi's. One combined number would hide which
    # provider a budget problem came from, and the two have different
    # limits and different failure modes.
    live_stats_request_count = Column(Integer)
    # what the live-evidence pass did this cycle: whether it ran at all,
    # how many overreaction candidates it saw, which joins resolved or
    # refused, the conditioning-state histogram, and the budget block. A
    # cycle that spent nothing records WHY it spent nothing.
    live_stats_json = Column(Text)
    roster_size = Column(Integer)          # discovered soccer GAME series
    active_series = Column(Integer)        # series with open markets
    # when the roster was last (re-)discovered, and whether a DUE
    # discovery failed this cycle (stale roster scanned instead)
    discovery_at = Column(DateTime(timezone=True))
    discovery_error = Column(Text)
    error = Column(Text)


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


# --- league-derived xG (API-Football) -------------------------------------
#
# These three tables are PROVIDER-NATIVE: keyed by API-Football's own league,
# season, fixture and team ids rather than by rows in `fixture` / `team`.
# That is deliberate. Friendlies span the global club universe, so the vast
# majority of the clubs these ratings describe have no row in `team` and never
# will — keying to our own fixtures would make the store unable to hold the
# very data it exists for. The optional bridge to our fixtures lives in
# `apifootball.bridge_fixture_xg`, which is a READ over these rows.
#
# No SourceObservation payload is written per fixture. That is a deliberate
# departure from `mls_stats`, and the reason is the 2026-07-25 DiskFull
# incident: `source_observation` payloads with no reader were one of the two
# growth drivers that filled the volume and made every prediction write fail
# silently. A full-season xG ingest is ~380 statistics responses per league,
# which is exactly that shape. What is retained instead is the parsed value
# PLUS the provider's raw string, which is what an audit actually needs.


class ApiFootballLeagueCoverage(LiveBase):
    """MEASURED xG coverage for one (league, season), and the date measured.

    API-Football's documentation is 403-walled, so no published coverage list
    exists — this table IS the coverage documentation. It records an empirical
    probe, never a provider claim.

    The measurement that matters is the VALUE, not the type. The
    expected-goals statistic TYPE is present in `fixtures/statistics`
    responses even where every value is null, so a type-presence check
    succeeds while carrying no data — the same shallow-success shape as this
    repo's `results: 0` and `{"created": 0}` incidents.
    `type_without_value_seen` records that the hollow form was observed, so
    the distinction stays on the record rather than being inferred.

    `verdict` is one of xg_available (every sampled fixture had a numeric
    value for both teams), xg_partial (some did), xg_absent (none did), or
    indeterminate_no_completed_fixture (the probe could not run, which is
    never read as clean)."""
    __tablename__ = "apifootball_league_coverage"
    id = Column(Integer, primary_key=True)
    league_id = Column(Integer, nullable=False)
    season = Column(Integer, nullable=False)
    league_name = Column(String(96))
    country = Column(String(64))
    verdict = Column(String(40), nullable=False)
    measured_rate = Column(Float)                 # fraction of samples with
    # completed fixtures the provider showed for this season. Load-bearing for
    # SEASON CHOICE, not decoration: on 2026-07-29 the provider's 'current'
    # season for the Premier League was 2026 (barely started, a handful of
    # completed fixtures) while the xG history sat in 2025 (380), and J1's
    # 'current' was 2027 against 200 completed in 2026 — the J-League moved to
    # an autumn-spring calendar. A rating fitted on the 'current' season would
    # have been fitted on almost nothing, so the season with the data wins.
    completed_fixtures_visible = Column(Integer)
    samples_taken = Column(Integer, nullable=False, default=0)
    samples_with_xg = Column(Integer, nullable=False, default=0)
    type_without_value_seen = Column(Boolean, default=False, nullable=False)
    # rounds whose sample carried no value — the attributable form of the
    # rate. End-of-season promotion/relegation ties were MEASURED to be the
    # miss on three leagues that a most-recent-N sample called 'partial'.
    miss_rounds_json = Column(Text)
    sampling_method = Column(String(40))          # spread_across_season
    measured_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (UniqueConstraint("league_id", "season"),)


class ApiFootballFixtureXg(LiveBase):
    """One team's provider xG in one league fixture, AS READ AT ONE INSTANT.

    APPEND-ONLY BY CONSTRUCTION. API-Football's documentation is 403-walled
    and Sportmonks-style retroactive correction of historical values cannot
    be ruled out, so a stored xG is evidence of what the provider said WHEN WE
    ASKED — never a cell to be refetched and overwritten. The unique key
    includes `value_hash`, so:

      - a re-read returning the SAME value collides and writes nothing
        (the ingest is idempotent and cheap to resume);
      - a re-read returning a DIFFERENT value inserts a SECOND row, which
        makes a retroactive correction VISIBLE and countable instead of
        silently replacing the evidence.

    `xg` is NULL where the provider offered no usable value; `xg_raw` keeps
    the provider's own string so the parse can be re-audited. A null, empty,
    '-' or exactly-zero value is ABSENCE, never zero: the provider uses null
    and 0 interchangeably as placeholders and the two cannot be told apart,
    so 0 is never read as data (the pre-registered trial criteria took the
    same line)."""
    __tablename__ = "apifootball_fixture_xg"
    id = Column(Integer, primary_key=True)
    provider_fixture_id = Column(Integer, nullable=False)
    side = Column(String(8), nullable=False)          # home | away
    league_id = Column(Integer, nullable=False)
    season = Column(Integer, nullable=False)
    provider_team_id = Column(Integer, nullable=False)
    team_name = Column(String(96))
    opponent_team_id = Column(Integer)
    xg = Column(Float)                                # None == absent
    xg_raw = Column(String(32))                       # provider's own string
    xg_against = Column(Float)                        # opponent's xg
    goals = Column(Integer)
    goals_conceded = Column(Integer)
    kickoff_utc = Column(DateTime(timezone=True))
    round_label = Column(String(64))
    # sha256 over the canonical (xg_raw, xg_against_raw, goals) triple — the
    # discriminator that turns an overwrite into a new row
    value_hash = Column(String(64), nullable=False)
    read_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("provider_fixture_id", "side", "value_hash"),
        Index("ix_apif_xg_league_season", "league_id", "season"),
        Index("ix_apif_xg_team", "provider_team_id"),
    )


class ApiFootballClubLeague(LiveBase):
    """A club's DOMESTIC league, derived from provider data and archived.

    A friendly's clubs must be resolved to the league whose fixtures the
    club's rating is fitted on — Son's rule: a club's xG rating comes from
    the league that club plays in. The resolution is provider-derived
    (`/teams?search=` then `/leagues?team=`), never guessed from a name.

    Resolution is authoritative against LEAGUE ROSTERS, not against the
    provider's own club->league answer. That answer is structurally ambiguous:
    `/leagues?team=` mixes the real league with pre-season friendly
    tournaments ('Premier League - Summer Series') and years-stale lower-tier
    registrations, with no field distinguishing them. Membership of a league
    roster has no such problem — a club belongs to exactly one domestic league
    roster — and one request buys a whole league.

    `resolution` is one of resolved (exactly one roster club at the strongest
    matching tier), ambiguous_team (several at that tier — never guessed), or
    league_not_indexed (no roster we hold contains this club, which is a
    statement about OUR coverage and is worded as one, never as 'this club has
    no league'). Only `resolved` may be used to pick a rating; every other
    value is shown in words.

    `match_tier` records HOW it matched, because the tiers are not equally
    strong: exact and token_set are safe alone, while unique_containment
    ('Newcastle' for 'Newcastle United') is directional — the roster name's
    tokens must be a strict subset of the query's. The reverse direction is
    what makes 'Real Madrid' match 'Real Madrid Castilla'."""
    __tablename__ = "apifootball_club_league"
    id = Column(Integer, primary_key=True)
    # the name we asked about (an ESPN displayName on the friendlies surface)
    query_name = Column(String(96), nullable=False, unique=True)
    resolution = Column(String(24), nullable=False)
    provider_team_id = Column(Integer)
    provider_team_name = Column(String(96))
    country = Column(String(64))
    league_id = Column(Integer)
    season = Column(Integer)
    league_name = Column(String(96))
    league_country = Column(String(64))
    match_tier = Column(String(24))       # exact | token_set | alias
    candidates_json = Column(Text)        # what was refused, and why
    resolved_at = Column(DateTime(timezone=True), nullable=False)

class ApiFootballLiveCoverage(LiveBase):
    """Per-competition verdict on whether API-Football serves LIVE in-play
    data — MEASURED, dated, and one row per competition.

    The hunter's in-play conditioning inference is only as honest as its
    knowledge of what the provider actually covers, and coverage is uneven
    and undocumented: measured 2026-07-29, CONMEBOL Sudamericana served 18
    statistic types at 90' while Copa Colombia served ZERO at 90'. So the
    verdict is measured from readings the scanner actually took, never
    assumed and never read off the provider's own coverage flag (which the
    trial harness found declaring `False` for two leagues whose payloads
    carried xG).

    STATISTICS AND EVENTS ARE SEPARATE COLUMNS, not one "has live data"
    flag, because event coverage is strictly BROADER: competitions with
    zero statistic types still served goal events. Collapsing them would
    have made the broader signal invisible.

    `fixtures_probed` is the DENOMINATOR and travels with the verdict: a
    verdict from one fixture at 20' and one from ten fixtures at 70' are
    not the same evidence. `too_early_reads` counts readings taken before
    a match had time to generate statistics — those never downgrade an
    already-measured PRESENT verdict, because letting one early read erase
    a measured presence would convert missing evidence into a conclusion.

    Growth is bounded by the number of competitions, updated IN PLACE —
    deliberately not one row per reading. Per-reading payloads with no
    reader are what filled the production volume on 2026-07-25.

    NOT A MODEL INPUT. Nothing here may reach a T-10 lock, a
    PredictionRun, a paper signal, or a fitted/scored feature; live xG
    carries information from after a lock. Enforced by
    tests/test_hunter_live_stats.py."""
    __tablename__ = "apifootball_live_coverage"
    id = Column(Integer, primary_key=True)
    # the PROVIDER's own league id — a provider-stable identity, never a
    # competition name
    league_id = Column(Integer, nullable=False, unique=True)
    league_name = Column(String(128))
    league_country = Column(String(64))
    # LIVE_STATS_PRESENT | LIVE_STATS_ABSENT | TOO_EARLY_TO_CONCLUDE
    verdict = Column(String(32))
    statistic_type_count = Column(Integer)
    statistic_types_json = Column(Text)
    # xG presence means a PARSEABLE number for both teams; a present-but-
    # null xG (several leagues serve one) is ABSENT, never zero
    xg_present = Column(Boolean, default=False)
    xg_type_offered = Column(Boolean, default=False)
    # tracked separately from statistics on purpose — see the class note
    events_present = Column(Boolean, default=False)
    event_count_last = Column(Integer)
    fixtures_probed = Column(Integer, default=0)
    too_early_reads = Column(Integer, default=0)
    last_elapsed_observed = Column(Integer)
    first_measured_at = Column(DateTime(timezone=True))
    last_measured_at = Column(DateTime(timezone=True))
    # when a PRESENT verdict was last actually observed, kept apart from
    # last_measured_at so a long run of absences cannot make a stale
    # presence look current
    last_present_at = Column(DateTime(timezone=True))


# ===========================================================================
# Team news (2026-07-30). Injuries/absences, lineup RELEASE state and in-play
# events, per provider, with our capture clock on every row.
#
# THE EPISTEMIC LINE, built into the schema rather than asserted in prose:
#
#   - Nothing here is a model feature. No PredictionRun, no canonical T-10
#     lock, no paper signal and no engine-signature module reads any of
#     these tables, and tests/test_team_news_isolation.py fails if one
#     starts to. The platform MEASURED lineup and key-attacker features as
#     negative-or-marginal (key-attacker +0.0034, not significant) and
#     switched them off; this is reader context, not a revived feature.
#   - There is deliberately NO aggregate column anywhere below — no
#     "absences_count" on a fixture, no severity weight, no team-strength
#     impact. An aggregate is a model wearing a news label.
#   - The provider's typing is stored VERBATIM (`provider_type`,
#     `provider_reason`). "Missing Fixture / Knee Injury" is API-Football's
#     claim about a player, not ours, and re-wording it into our vocabulary
#     would launder a third party's guess into our voice.
#   - `record_count` is NULLABLE ON PURPOSE and is NULL whenever the state
#     is `unavailable`. Zero means "the provider showed us an empty list";
#     NULL means "we never saw a list". This repo has twice read a zero as
#     an answer (`results: 0` on a live match; `{"created": 0}` over a full
#     volume), so the difference is structural here, not a convention.
#   - `subject_ref` is a STRING provider reference, and `fixture_id` is
#     NULLABLE, so club friendlies — which have no live-plane fixture row
#     and by design get none (src/friendlies.py) — can carry news without
#     being handed the evidence machinery they are excluded from.
#   - Raw provider bodies are NOT stored. Only a content hash and a byte
#     length live here; the bytes live in research_archive/. A per-fixture
#     payload column was the growth driver behind the 2026-07-25 DiskFull
#     incident, and re-adding one to carry news would repeat it.
# ===========================================================================

TEAM_NEWS_FEEDS = ("absences", "lineup", "events")
TEAM_NEWS_STATES = ("ok", "empty", "unavailable")


class TeamNewsCapture(LiveBase):
    """The freshness ledger: one upserted row per (provider, subject, feed)
    recording what the LAST fetch attempt actually saw.

    This table is what makes "stale", "unavailable" and "empty"
    distinguishable instead of collapsing into an absent section. It is
    upserted rather than appended so growth stays bounded by
    fixtures x feeds; the one transition worth keeping as history — a
    lineup being released — is kept on TeamNewsLineup.first_released_at.
    """
    __tablename__ = "team_news_capture"
    id = Column(Integer, primary_key=True)
    provider = Column(String(16), nullable=False)      # apifootball|espn
    subject_ref = Column(String(32), nullable=False)   # provider fixture ref
    fixture_id = Column(Integer, ForeignKey("fixture.id"))   # nullable
    competition = Column(String(32))
    feed = Column(String(16), nullable=False)          # TEAM_NEWS_FEEDS
    state = Column(String(16), nullable=False)         # TEAM_NEWS_STATES
    # NULL when state='unavailable' — see the header note. Never defaulted
    # to 0, because 0 is a measurement and NULL is the absence of one.
    record_count = Column(Integer)
    # the provider's own count, kept only to record a DISAGREEMENT with the
    # array it shipped. The array always wins; this is evidence, not input.
    provider_declared_count = Column(Integer)
    captured_at = Column(DateTime(timezone=True), nullable=False)
    first_captured_at = Column(DateTime(timezone=True))
    attempts = Column(Integer, nullable=False, default=1)
    # an error CLASS or a short provider note. Never a payload, never a
    # credential — the ingestion redacts before anything reaches here.
    note = Column(String(300))
    content_hash = Column(String(64))     # sha256 of the raw body
    payload_bytes = Column(Integer)       # length of the body we hashed
    __table_args__ = (
        UniqueConstraint("provider", "subject_ref", "feed",
                         name="uq_team_news_capture_subject_feed"),
        Index("ix_team_news_capture_fixture", "fixture_id"),
    )


class TeamAbsence(LiveBase):
    """One provider-reported absence for one fixture, typed IN THE
    PROVIDER'S OWN WORDS.

    Injury feeds are stale and wrong often enough that a record is only
    worth having if a human can discount it, so every row carries its
    provider, its capture clock, and when it was first and last seen. A
    row whose `last_seen_at` predates the newest successful capture for
    its fixture is no longer being reported by the provider — which is
    surfaced, not deleted, because a retraction is information too.
    """
    __tablename__ = "team_absence"
    id = Column(Integer, primary_key=True)
    provider = Column(String(16), nullable=False)
    subject_ref = Column(String(32), nullable=False)
    fixture_id = Column(Integer, ForeignKey("fixture.id"))   # nullable
    competition = Column(String(32))
    # provider player id when present, else a normalised name. NOT NULL so
    # the uniqueness below is enforced identically on PostgreSQL and
    # SQLite: PG treats NULLs as distinct in a unique constraint, so a
    # nullable key would silently admit duplicates in production while the
    # SQLite suite stayed green.
    player_key = Column(String(96), nullable=False)
    provider_player_id = Column(String(24))
    player_name = Column(String(96))
    provider_team_id = Column(String(24))
    team_name = Column(String(96))
    # VERBATIM provider fields. Do not normalise, map or re-word these.
    provider_type = Column(String(64))       # e.g. "Missing Fixture"
    provider_reason = Column(String(96))     # e.g. "Knee Injury"
    captured_at = Column(DateTime(timezone=True), nullable=False)
    first_seen_at = Column(DateTime(timezone=True))
    last_seen_at = Column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("provider", "subject_ref", "player_key",
                         name="uq_team_absence_subject_player"),
        Index("ix_team_absence_fixture", "fixture_id"),
    )


class TeamNewsLineup(LiveBase):
    """Per-side lineup RELEASE state, per provider, plus the release
    transition.

    `provider` is part of the identity on purpose: ESPN carries friendly
    XIs and API-Football carries league XIs, and pooling them into an
    undifferentiated "lineup" with the source lost would make a coverage
    number unreadable.

    THE THREE STATES THAT MUST NOT COLLAPSE:
      container_present=False                 -> no coverage for this fixture
      container_present=True, named_count=0   -> NOT RELEASED
      released=True                           -> a full XI exists

    The middle case is real and measured: API-Football returns friendly
    lineup arrays whose side objects carry formation=None and startXI=0,
    and ESPN returned rosters=2 with named=0 on a COMPLETED fixture
    (Atletico v Getafe, 2026-07-30). Counting containers reports coverage
    that does not exist, so release is decided by counting NAMES.

    `first_released_at` is the information event: on a friendly, where the
    XI is arbitrary rather than predictable, the moment it lands is the
    largest genuine information event on the fixture.
    """
    __tablename__ = "team_news_lineup"
    id = Column(Integer, primary_key=True)
    provider = Column(String(16), nullable=False)      # espn|apifootball
    subject_ref = Column(String(32), nullable=False)
    fixture_id = Column(Integer, ForeignKey("fixture.id"))   # nullable
    competition = Column(String(32))
    side = Column(String(8), nullable=False)           # home|away
    team_name = Column(String(96))
    formation = Column(String(16))
    named_count = Column(Integer)        # NAMES, never containers
    starter_count = Column(Integer)
    released = Column(Boolean, nullable=False, default=False)
    container_present = Column(Boolean, nullable=False, default=False)
    captured_at = Column(DateTime(timezone=True), nullable=False)
    first_seen_at = Column(DateTime(timezone=True))
    # The transition. Set ONCE, on the first capture that sees a full XI,
    # and never rewritten — a later re-release must not move the clock on
    # when the news landed.
    #
    # READ IT AS AN UPPER BOUND, NOT A PUBLICATION TIME. This records when
    # WE FIRST OBSERVED the XI, not when the provider published it.
    # Neither ESPN nor API-Football timestamps a lineup, so the
    # publication moment is unavailable at ANY polling rate, and the
    # observation clock is the only honest thing to store. The error is
    # bounded by the polling interval and always in one direction: a
    # sparse poll makes a release look LATER than it was. Nothing
    # downstream may therefore treat this as the instant the market
    # learned anything.
    first_released_at = Column(DateTime(timezone=True))
    kickoff_utc = Column(DateTime(timezone=True))
    # computed AT THE MOMENT OF OBSERVATION and frozen, so a later
    # reschedule cannot retroactively change how early the news landed.
    # Carries the same upper-bound caveat as first_released_at.
    released_minutes_before_kickoff = Column(Integer)
    __table_args__ = (
        UniqueConstraint("provider", "subject_ref", "side",
                         name="uq_team_news_lineup_subject_side"),
        Index("ix_team_news_lineup_fixture", "fixture_id"),
    )


class TeamNewsEvent(LiveBase):
    """An in-play event (goal, card, substitution) with the PROVIDER'S
    minute and OUR capture time — both, never one.

    A provider minute alone cannot be aged: it says "73'", not "we learned
    this at 20:41Z". Anything downstream that wants to know how fresh an
    event read is needs the capture clock, and anything that wants to know
    where in the match it happened needs the provider minute.

    Deliberately NOT named `fixture_event`: the in-play detector work on
    feat-hunter-live-stats also consumes `fixtures/events`, and a
    namespaced table lets both branches land without a schema collision.
    Nothing in this module reads or writes src/live/hunter.py.
    """
    __tablename__ = "team_news_event"
    id = Column(Integer, primary_key=True)
    provider = Column(String(16), nullable=False)
    subject_ref = Column(String(32), nullable=False)
    fixture_id = Column(Integer, ForeignKey("fixture.id"))   # nullable
    competition = Column(String(32))
    # dedupe identity: minute|extra|type|detail|player|team, so re-reading
    # a live feed refreshes rather than duplicating
    event_key = Column(String(160), nullable=False)
    provider_minute = Column(Integer)
    provider_minute_extra = Column(Integer)
    # VERBATIM provider strings
    event_type = Column(String(32))       # Goal|Card|subst|Var
    event_detail = Column(String(64))     # Red Card|Normal Goal|...
    provider_team_id = Column(String(24))
    team_name = Column(String(96))
    provider_player_id = Column(String(24))
    player_name = Column(String(96))
    assist_name = Column(String(96))
    captured_at = Column(DateTime(timezone=True), nullable=False)
    first_seen_at = Column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("provider", "subject_ref", "event_key",
                         name="uq_team_news_event_subject_key"),
        Index("ix_team_news_event_fixture", "fixture_id"),
    )


class TeamStyleObservation(LiveBase):
    """One team's RAW playstyle inputs in one league fixture, as read at one
    instant. The per-fixture evidence the style vectors are fitted from.

    WHY A SEPARATE TABLE, not columns on `apifootball_fixture_xg`. That table
    is append-only against a `value_hash` computed over the xG tuple ALONE. A
    style value changing while the xG stayed the same would produce an
    identical hash, collide with the existing row, and be silently dropped —
    the exact overwrite the hash exists to prevent, reintroduced through a
    widened row. So style gets its own table with its own hash over its own
    values, and the two evidence trails stay independently auditable.

    APPEND-ONLY, on the same reasoning as the xG store: a stored statistic is
    evidence of what the provider said WHEN WE ASKED. A re-read returning the
    same values collides and writes nothing (idempotent, cheap to resume); a
    re-read returning DIFFERENT values inserts a second row, so a retroactive
    provider correction becomes visible and countable instead of replacing the
    evidence.

    `source` is part of the identity, not a label. API-Football and Sportec
    measure different quantities under similar names and carry DIFFERENT SETS
    of axes at all (Sportec publishes no possession, no offsides and no
    goalkeeper saves), so pooling two sources into one population would both
    mix measurement systems and silently change which axes exist.

    EVERY RAW COLUMN IS NULLABLE AND NULL MEANS ABSENT, NEVER ZERO. The
    provider ships a statistic type with a null value routinely, and a null
    read as 0 would make a team look like it never won possession, never drew
    an offside and never forced a save. `team_style.parse_count` /
    `parse_percent` return None for null, '', '-' and non-numeric alike.

    `offsides_drawn` is the OPPONENT's offside count in this fixture — a high
    defensive line is what forces it, so it belongs to the team that forced it
    rather than to the team that committed it. Both teams' statistics arrive in
    the same `fixtures/statistics` response, so it is derivable without an
    extra request. `offsides_own` is the team's OWN offside count and is a
    DIFFERENT quantity (a verticality proxy); both are stored so the sixth
    candidate axis can be measured rather than assumed."""
    __tablename__ = "team_style_observation"
    id = Column(Integer, primary_key=True)
    source = Column(String(16), nullable=False)       # api-football | sportec
    league_id = Column(Integer, nullable=False)
    season = Column(Integer, nullable=False)
    provider_fixture_id = Column(String(48), nullable=False)
    side = Column(String(8), nullable=False)          # home | away
    provider_team_id = Column(Integer, nullable=False)
    team_name = Column(String(96))
    opponent_team_id = Column(Integer)
    kickoff_utc = Column(DateTime(timezone=True))
    round_label = Column(String(64))
    # --- raw axis inputs: NULL == ABSENT, never zero ---
    possession_pct = Column(Float)          # 0-100, provider's '%' stripped
    shots_total = Column(Integer)
    shots_inside_box = Column(Integer)
    xg = Column(Float)
    offsides_drawn = Column(Integer)        # the OPPONENT's offsides
    offsides_own = Column(Integer)          # this team's own — 6th candidate
    gk_saves = Column(Integer)
    # the provider's own strings, so any parse can be re-audited later
    raw_json = Column(Text)
    # sha256 over the canonical raw tuple — the discriminator that turns a
    # silent overwrite into a new row
    value_hash = Column(String(64), nullable=False)
    read_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("source", "provider_fixture_id", "side",
                         "value_hash"),
        Index("ix_style_scope", "source", "league_id", "season"),
        Index("ix_style_team", "source", "provider_team_id"),
    )
