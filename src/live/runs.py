"""Prediction runs + the atomic T-10 lock (launch decision O7).

Every simulation batch is a PredictionRun with an explicit UUID, status
gating (readers only see status='complete'), a stored deterministic
seed, the model version, and the git revision. The T-10 lock follows
the decision's transactional workflow: capture the book, open the run
as 'writing', simulate, write contracts, validate invariants, mark
complete+canonical, COMMIT — and only then alert (PAPER-labeled). A
crash before commit leaves nothing visible; the partial unique index
makes a second canonical lock physically impossible.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import config
from src.live import markets, model_mls
from src.live.db import get_session, plane_ready
from src.live.models import (Fixture, MarketContract, MarketEvent,
                             ModelInputArtifact, ModelVersion,
                             PredictionContract, PredictionRun)

GIT_REV = os.getenv("RAILWAY_GIT_COMMIT_SHA", "")[:40]


# --- competition spec (EPL build, 2026-07-28) ------------------------------
# The run/lock machinery is competition-KEYED, not duplicated: every
# public function takes an optional spec and defaults to MLS, so
# existing MLS call sites and behaviour are unchanged. The model module
# must expose model_mls's surface (MODEL_NAME, current_model,
# predict_fixture, build_input_artifact, INPUT_ARTIFACT_SCHEMA).
class CompetitionRunsSpec:
    def __init__(self, slug: str, model_module, market_spec, label: str,
                 expected_teams: int = 30, enabled_fn=None):
        self.slug = slug
        self.model = model_module
        self.market_spec = market_spec       # markets.CompetitionMarketSpec
        self.label = label                   # alert prefix ("MLS", "EPL")
        self.expected_teams = expected_teams
        # read at call time so an env flip needs no re-import
        self.enabled_fn = enabled_fn or (
            lambda: config.MLS_SHADOW_ENABLED)


MLS_RUNS_SPEC = CompetitionRunsSpec(
    slug="mls-2026", model_module=model_mls,
    market_spec=markets.MLS_SPEC, label="MLS", expected_teams=30)


def empty_board_reason(spec: CompetitionRunsSpec | None = None,
                       horizon_hours: float = 168.0) -> dict:
    """Why THIS competition's odds board is empty, for any competition.

    `approved_no_runs` is several different situations wearing one label,
    and telling them apart IS the diagnosis:

      insufficient_team_history  the sweep runs and `_raw` refuses every
                                 club, because none clears MIN_GAMES.
                                 Liga MX, live on 2026-07-30: approved,
                                 mapped, enabled, 18 clubs known, 0 rated,
                                 max 2 games played — and none of that was
                                 visible from the board.
      no_fixtures_in_horizon     nothing to price. EPL, the same day: its
                                 first fixture is 2026-08-21, 22 days out,
                                 while the sweep looks 168h ahead.
      no_completed_fixtures      nothing fitted at all.
      ok                         history and fixtures both present, so an
                                 empty board is NOT explained by either
                                 and deserves a real look.

    Read-only and cheap: one fit over already-ingested rows plus one
    fixture count. Nothing here writes, prices, or spends provider quota.
    """
    spec = spec or MLS_RUNS_SPEC
    if not plane_ready():
        return {"state": "dormant"}
    min_games = getattr(spec.model, "MIN_GAMES", None)
    out: dict = {"min_games": min_games, "horizon_hours": horizon_hours}
    s = get_session()
    try:
        cutoff = _now() + timedelta(hours=horizon_hours)
        upcoming = 0
        for f in (s.query(Fixture)
                  .filter_by(competition_slug=spec.slug, status="pre").all()):
            if f.current_kickoff_utc is None:
                continue
            ko = _utc(f.current_kickoff_utc)
            if _now() <= ko <= cutoff:
                upcoming += 1
        out["fixtures_in_horizon"] = upcoming
    finally:
        s.close()

    model = spec.model.current_model()
    if model is None:
        out["state"] = "no_completed_fixtures"
        out["clubs_rated"] = 0
        return out
    games = [r["games"] for r in model["ratings"].values()]
    rated = [g for g in games if min_games is None or g >= min_games]
    out.update({"clubs_known": len(games), "clubs_rated": len(rated),
                "max_games_seen": max(games) if games else 0,
                "n_fixtures_fitted": model["n_fixtures"]})
    if not rated:
        out["state"] = "insufficient_team_history"
    elif not out["fixtures_in_horizon"]:
        out["state"] = "no_fixtures_in_horizon"
    else:
        out["state"] = "ok"
    return out


def _now():
    return datetime.now(timezone.utc)


def _utc(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def approved_model_version(s, model_name: str | None = None
                           ) -> ModelVersion | None:
    """The enforcement point for F3: run paths may only publish under a
    ModelVersion row that is approved for shadow. Missing or unapproved
    fails CLOSED (no runs), never open."""
    return (s.query(ModelVersion)
            .filter_by(name=model_name or model_mls.MODEL_NAME,
                       approved_for_shadow=True)
            .first())


def _input_quality(model: dict, lineup: dict | None) -> dict:
    """The five input-quality states frozen on each run (V8.1 eval
    Phase 5). TEAM_DATA_FRESH reflects the model's own rating basis;
    the lineup-derived states come from the captured lineup (all false
    when no lineup was available — missing data is never confidence)."""
    q = {"TEAM_DATA_FRESH": bool(model.get("ratings")),
         "PLAYER_DATA_FRESH": False, "AVAILABILITY_COMPLETE": False,
         "LINEUP_CONFIRMED": False, "GOALKEEPER_CONFIRMED": False}
    if lineup and lineup.get("quality"):
        q.update(lineup["quality"])
    return q


# how recent a COMPLETE registry sweep must be for a canonical lock to
# trust the local contract universe (V9.3 eval F6). Discovery runs every
# 10 minutes, so 6h is generous while still refusing a stale registry.
REGISTRY_MAX_AGE_HOURS = 6


def _write_run(s, fixture, run_type: str, model: dict, mv: ModelVersion,
               canonical: bool = False, snapshot: dict | None = None,
               lineup: dict | None = None,
               spec: CompetitionRunsSpec | None = None
               ) -> PredictionRun | None:
    """The transactional core. Returns the committed run or None.
    For canonical locks the caller supplies a COMPLETE MarketSnapshot
    (capture_lock_snapshot) — its id and ticker->quote map are frozen
    onto the run and its contracts (V8 evaluation F1/F2) — plus the
    lineup snapshot the lock saw (Phase 5)."""
    spec = spec or MLS_RUNS_SPEC
    model_mod = spec.model
    pred = model_mod.predict_fixture(fixture, model, run_type=run_type)
    if pred is None:
        return None
    # the retrievable input artifact (Phase 2): store the exact bytes the
    # run simulated from, deduped by content hash, and link the run to it
    _doc, canon, ihash = model_mod.build_input_artifact(
        fixture, model, run_type)
    artifact = (s.query(ModelInputArtifact)
                .filter_by(content_hash=ihash).first())
    if artifact is None:
        artifact = ModelInputArtifact(
            schema_version=model_mod.INPUT_ARTIFACT_SCHEMA,
            content_hash=ihash, size_bytes=len(canon.encode()),
            document_json=canon, created_at=_now())
        s.add(artifact)
        s.flush()
    # the immutable approval decision authorizing this run (V9 eval F10):
    # not just the boolean below, but the exact CI-based record. Queried
    # on the SAME session so no second connection is opened mid-write.
    from src.live.models import ModelApprovalDecision
    _dec = (s.query(ModelApprovalDecision)
            .filter_by(model_version_name=model_mod.MODEL_NAME,
                       approved=True)
            .order_by(ModelApprovalDecision.id.desc()).first())
    # V9.1 eval F9: never CREATE an audit-invalid canonical lock — a
    # canonical run without an approved decision is structurally refused
    if canonical and _dec is None:
        return None
    ko = _utc(fixture.current_kickoff_utc)
    run = PredictionRun(
        fixture_id=fixture.id, run_type=run_type,
        scheduled_for=ko, captured_at=_now(), created_at=_now(),
        seconds_before_kickoff=int((ko - _now()).total_seconds()),
        status="writing", canonical=False,
        git_revision=GIT_REV,
        model_version_id=mv.id,
        model_approval_decision_id=(_dec.id if _dec else None),
        model_approved_at_run=bool(mv.approved_for_shadow),
        input_snapshot_hash=ihash,
        model_input_artifact_id=artifact.id,
        lineup_snapshot_id=(lineup or {}).get("snapshot_id"),
        # availability_snapshot_id stays NULL: there is no availability
        # feed, and writing the lineup id here was a dishonest conflation
        # (V9 eval F14). AVAILABILITY_COMPLETE below is the honest proxy.
        input_quality_json=json.dumps(_input_quality(model, lineup)),
        market_snapshot_id=(snapshot or {}).get("snapshot_id"),
        simulation_seed=pred["seed"],
        simulation_count=config.N_SIMULATIONS,
        payload_json=json.dumps({
            "xg": pred.get("xg"),
            "scorelines": pred.get("scorelines"),
            "props": pred.get("props"),
            "basis": pred.get("basis"),
        })[:100_000])
    s.add(run)
    s.flush()
    # the model's full probability surface: 3-way outcomes, props
    # (totals/BTTS/margins/first-goal/team totals), and scorelines
    prob_for: dict[str, float] = dict(pred["outcomes"])
    prob_for.update(pred.get("props") or {})
    for sl in pred.get("scorelines") or []:
        h, a = sl["score"].split("-")
        prob_for[f"score_{h}_{a}"] = sl["prob"]
    # contracts: the three-way outcomes are ALWAYS stored; every other
    # mapped market the model prices joins the same batch (full-book
    # locks across all families, launch decision O6/O7). Each contract
    # links its frozen snapshot quote where one exists (F2).
    quote_by_ticker = (snapshot or {}).get("quote_by_ticker") or {}
    mapped: dict[str, int] = {}
    ticker_by_mc: dict[int, str] = {}
    extra: list[tuple[int, str]] = []
    seen_keys: set[str] = set()
    for me in (s.query(MarketEvent)
               .filter_by(fixture_id=fixture.id, mapping_approved=True)
               .all()):
        for mc in s.query(MarketContract).filter_by(
                market_event_id=me.id).all():
            if not mc.outcome_key or mc.outcome_key in seen_keys:
                continue
            ticker_by_mc[mc.id] = mc.ticker
            if mc.outcome_key in ("home_win", "draw", "away_win"):
                mapped[mc.outcome_key] = mc.id
                seen_keys.add(mc.outcome_key)
            elif mc.outcome_key in prob_for:
                extra.append((mc.id, mc.outcome_key))
                seen_keys.add(mc.outcome_key)
    total = 0.0
    for okey in ("home_win", "draw", "away_win"):
        p = pred["outcomes"][okey]
        total += p
        mc_id = mapped.get(okey)
        s.add(PredictionContract(
            prediction_run_id=run.id,
            market_contract_id=mc_id,
            market_quote_id=quote_by_ticker.get(
                ticker_by_mc.get(mc_id, "")),
            outcome_key=okey, raw_probability=p))
    for cid, okey in extra:
        s.add(PredictionContract(
            prediction_run_id=run.id, market_contract_id=cid,
            market_quote_id=quote_by_ticker.get(ticker_by_mc.get(cid, "")),
            outcome_key=okey, raw_probability=prob_for[okey]))
    # integrity invariants BEFORE completion (the decision's checklist)
    if not (0.99 <= total <= 1.01):
        run.status = "failed"
        run.failure_reason = f"probabilities sum {total:.4f}"
        s.commit()
        return None
    # V9.3 eval F6: a canonical lock's completeness was self-referential —
    # "everything known LOCALLY was captured", even if discovery itself was
    # truncated. Require a recent COMPLETE registry sweep so the expected
    # universe is externally established, not defined by whatever the local
    # registry happened to contain.
    if canonical:
        from src.live.models import RegistryDiscovery
        reg = (s.query(RegistryDiscovery)
               .filter_by(competition_slug=spec.slug, provider="kalshi",
                          complete=True)
               .order_by(RegistryDiscovery.id.desc()).first())
        fresh_reg = bool(
            reg and reg.completed_at
            and (_now() - _utc(reg.completed_at))
            <= timedelta(hours=REGISTRY_MAX_AGE_HOURS))
        if not fresh_reg:
            run.status = "failed"
            run.failure_reason = (
                "no complete kalshi registry discovery within "
                f"{REGISTRY_MAX_AGE_HOURS}h (F6)")[:500]
            s.commit()
            return None
    # V9.3 eval F5: a CANONICAL lock claims the model was joined to the
    # frozen market. Refuse completion unless all three game legs actually
    # carry a market contract AND a frozen quote — previously a lock with
    # zero market links completed and then passed the audit vacuously.
    if canonical:
        missing = [k for k in ("home_win", "draw", "away_win")
                   if not (mapped.get(k)
                           and quote_by_ticker.get(
                               ticker_by_mc.get(mapped.get(k), "")))]
        if missing:
            run.status = "failed"
            run.failure_reason = (
                "canonical lock missing market contract/quote for: "
                + ",".join(missing))[:500]
            s.commit()
            return None
    run.status = "complete"
    run.canonical = canonical
    run.completed_at = _now()
    s.commit()
    return run


def scheduled_runs(horizon_hours: float = 168.0,
                   freshness_hours: float = 4.0,
                   spec: CompetitionRunsSpec | None = None) -> dict:
    """Rolling shadow odds: every upcoming fixture inside the horizon
    gets a fresh 'scheduled' run unless one is younger than
    freshness_hours (operator sweeps may pass 0 to force regeneration).
    The default horizon matches the dashboard's seven-day fixture list —
    "odds up and running for all matches" means every visible fixture."""
    spec = spec or MLS_RUNS_SPEC
    if not (plane_ready() and spec.enabled_fn()):
        return {"skipped": "off"}
    model = spec.model.current_model()
    if model is None:
        return {"skipped": "no model (no completed fixtures ingested)"}
    s = get_session()
    created = skipped = 0
    failures: list[str] = []
    no_prediction: list[str] = []
    try:
        mv = approved_model_version(s, spec.model.MODEL_NAME)
        if mv is None:
            return {"skipped": "model not approved for shadow (F3 gate)"}
        cutoff = _now() + timedelta(hours=horizon_hours)
        # pre-match only (a 'scheduled' run must never masquerade as an
        # in-play read), and never after the canonical lock exists — the
        # lock is the fixture's final pre-match word (F9)
        for f in (s.query(Fixture)
                  .filter_by(competition_slug=spec.slug, status="pre")
                  .all()):
            if f.current_kickoff_utc is None:
                continue
            ko = _utc(f.current_kickoff_utc)
            if not (_now() <= ko <= cutoff):
                continue
            if (s.query(PredictionRun)
                    .filter_by(fixture_id=f.id, run_type="t10",
                               canonical=True, status="complete")
                    .first()):
                skipped += 1
                continue
            fresh = (s.query(PredictionRun)
                     .filter_by(fixture_id=f.id, run_type="scheduled",
                                status="complete")
                     .order_by(PredictionRun.captured_at.desc())
                     .first())
            if fresh and (_now() - _utc(fresh.captured_at)
                          ) < timedelta(hours=freshness_hours):
                skipped += 1
                continue
            # one bad fixture must not kill the whole sweep — the prod
            # boot of Jul 23 lost all 15 runs to a single insert error
            try:
                if _write_run(s, f, "scheduled", model, mv, spec=spec):
                    created += 1
                else:
                    # _write_run declines (no prediction) rather than
                    # raising. Reporting it matters: a sweep returning
                    # created=0 with no errors used to be indistinguishable
                    # from "nothing to do" (audit Jul 25).
                    no_prediction.append(str(f.espn_event_id))
            except Exception as exc:
                s.rollback()
                failures.append(f"{f.espn_event_id}: {type(exc).__name__}: "
                                f"{str(exc)[:160]}")
                print(f"[runs] fixture {f.espn_event_id} failed: {exc}")
        out = {"created": created, "fresh_skipped": skipped}
        # surface WHY a sweep produced nothing — silent swallowing hid a
        # prod regeneration failure behind {"created": 0}
        if no_prediction:
            out["no_prediction"] = no_prediction[:10]
        if failures:
            out["failures"] = failures[:5]
            out["failure_count"] = len(failures)
        return out
    except Exception as exc:
        s.rollback()
        print(f"[runs] scheduled failed: {exc}")
        return {"error": str(exc)[:200]}
    finally:
        s.close()


def t10_locks(spec: CompetitionRunsSpec | None = None) -> dict:
    """The lock sweep: fixtures inside the window get a lock-grade
    market snapshot (validated for completeness) and then ONE canonical
    complete t10 run frozen against it. NO complete snapshot -> NO
    canonical lock (V8 evaluation F1) — the sweep retries every tick
    until kickoff, and a fixture that never captures stays visibly
    missing."""
    spec = spec or MLS_RUNS_SPEC
    if not (plane_ready() and spec.enabled_fn()):
        return {"skipped": "off"}
    model = spec.model.current_model()
    if model is None:
        return {"skipped": "no model"}
    s = get_session()
    locked = 0
    try:
        mv = approved_model_version(s, spec.model.MODEL_NAME)
        if mv is None:
            return {"skipped": "model not approved for shadow (F3 gate)"}
        # V9.1 eval F9: a canonical lock MUST reference a valid approved
        # decision — refuse to even attempt one without it, rather than
        # creating an audit-invalid run and detecting it afterward.
        from src.live.models import ModelApprovalDecision
        if (s.query(ModelApprovalDecision)
                .filter_by(model_version_name=spec.model.MODEL_NAME,
                           approved=True).first()) is None:
            return {"skipped": "no approved model-approval decision (F9)"}
        for f in (s.query(Fixture)
                  .filter_by(competition_slug=spec.slug, status="pre")
                  .all()):
            if f.current_kickoff_utc is None:
                continue
            secs = (_utc(f.current_kickoff_utc) - _now()).total_seconds()
            if not (0 < secs <= 11 * 60):
                continue
            if (s.query(PredictionRun)
                    .filter_by(fixture_id=f.id, run_type="t10",
                               canonical=True, status="complete")
                    .first()):
                continue
            # 1. the lock-grade snapshot — completeness-gated; a failed
            # or partial capture records WHY and produces no lock
            snapshot = markets.capture_lock_snapshot(
                f.id, spec=spec.market_spec)
            if snapshot is None:
                continue
            # 1b. the lineup snapshot the lock SAW (Phase 5). A pending
            # lineup is recorded, not a blocker — the model doesn't use
            # it yet, but the shadow record must show what was available
            from src.live import lineups
            lineup = lineups.capture_lineup(f.id)
            # 2-12. the transactional run (isolated per fixture: one
            # failed lock must stay visibly missing, not take the rest
            # of the slate down with it)
            try:
                run = _write_run(s, f, "t10", model, mv, canonical=True,
                                 snapshot=snapshot, lineup=lineup,
                                 spec=spec)
            except Exception as exc:
                s.rollback()
                print(f"[runs] t10 {f.espn_event_id} failed: {exc}")
                continue
            if run:
                locked += 1
                # 13. paper trading against the frozen book — PAPER only,
                # isolated (a paper failure must not undo the lock)
                try:
                    from src.live import paper
                    paper.paper_trade_lock(run.id)
                except Exception as exc:
                    print(f"[runs] t10 paper {f.espn_event_id}: {exc}")
                # 14. lock record only AFTER commit — OPERATIONAL, and it
                # carries the locked H/D/A.
                #
                # The round-4 alert-gate work stripped these numbers,
                # reasoning that per-match probabilities on the act-now
                # channel are a market view reaching a real-money reader.
                # Son restored them on 2026-07-29 on two facts that
                # reasoning lacked: the detail and action channels are
                # configured DISTINCTLY in Railway, and he is
                # currently the channel's ONLY reader — his friend bets on
                # what Son concludes and relays, never on this feed. So
                # this is the operator reading his own instrument.
                #
                # Note what the classification rests on: a fact about the
                # READERSHIP, not about the wording. If the friend is ever
                # given channel access this call site becomes a
                # MODEL_SIGNAL and must be refused while the money lock is
                # on. Recorded so the next reader inherits the condition
                # rather than the conclusion.
                try:
                    from src.alerts import OPERATIONAL, send_alert
                    o = (s.query(PredictionContract)
                         .filter_by(prediction_run_id=run.id).all())
                    probs = {c.outcome_key: c.raw_probability for c in o}
                    send_alert(
                        f"📋 PAPER · {spec.label} T-10 lock — fixture "
                        f"{f.espn_event_id}: "
                        f"H {probs.get('home_win', 0):.0%} / "
                        f"D {probs.get('draw', 0):.0%} / "
                        f"A {probs.get('away_win', 0):.0%} "
                        f"({spec.model.MODEL_NAME}, shadow — not advice)",
                        title=f"{spec.label} shadow lock",
                        dispatch_class=OPERATIONAL)
                except Exception as exc:
                    print(f"[runs] t10 lock record failed: {exc}")
        return {"locked": locked}
    except Exception as exc:
        s.rollback()
        print(f"[runs] t10 failed: {exc}")
        return {"error": str(exc)[:200]}
    finally:
        s.close()


def model_for_event(espn_event_id: str,
                    spec: CompetitionRunsSpec | None = None) -> dict | None:
    """The match hub's model section: this fixture's newest complete run
    with full provenance, plus its canonical T-10 lock if one exists.
    None when the fixture is unknown or no complete run exists yet."""
    spec = spec or MLS_RUNS_SPEC
    if not plane_ready():
        return None
    s = get_session()
    try:
        f = (s.query(Fixture)
             .filter_by(competition_slug=spec.slug,
                        espn_event_id=str(espn_event_id)).first())
        if f is None:
            return None

        THREE_WAY = ("home_win", "draw", "away_win")

        def _payload(run):
            contracts = (s.query(PredictionContract)
                         .filter_by(prediction_run_id=run.id).all())
            # outcome -> Kalshi ticker via the APPROVED mapping chain, so
            # the frontend joins model to book by ticker, never by
            # guessing at side labels. Runs now carry contracts for EVERY
            # priced family; "outcomes" stays strictly the 3-way.
            tickers = {}
            for c in contracts:
                if c.market_contract_id and c.outcome_key in THREE_WAY:
                    mc = s.get(MarketContract, c.market_contract_id)
                    if mc:
                        tickers[c.outcome_key] = mc.ticker
            out = {
                "run_id": run.id, "run_type": run.run_type,
                "captured_at": (_utc(run.captured_at).isoformat()
                                if run.captured_at else None),
                "seed": run.simulation_seed,
                "n_simulations": run.simulation_count,
                "outcomes": {c.outcome_key: round(c.raw_probability, 4)
                             for c in contracts
                             if c.outcome_key in THREE_WAY},
                "tickers": tickers,
                "input_quality": (json.loads(run.input_quality_json)
                                  if run.input_quality_json else None),
            }
            if run.payload_json:
                try:
                    out.update(json.loads(run.payload_json))
                except (ValueError, TypeError):
                    pass
            return out

        latest = (s.query(PredictionRun)
                  .filter_by(fixture_id=f.id, status="complete")
                  .order_by(PredictionRun.captured_at.desc())
                  .first())
        if latest is None:
            return None
        lock = (s.query(PredictionRun)
                .filter_by(fixture_id=f.id, run_type="t10",
                           canonical=True, status="complete")
                .first())
        # display policy (V8 evaluation F9): once the canonical lock
        # exists it IS the fixture's model — a later scheduled run must
        # never silently supersede it. "primary" is what the page shows.
        return {
            "model_version": spec.model.MODEL_NAME,
            "shadow": True,
            "real_money_signals": config.REAL_MONEY_SIGNALS_ENABLED,
            "primary": _payload(lock) if lock else _payload(latest),
            "latest": _payload(latest),
            "t10_lock": _payload(lock) if lock else None,
        }
    finally:
        s.close()


def shadow_counts(spec: CompetitionRunsSpec | None = None) -> dict:
    """Readiness for /api/ready's live section — counts PLUS the
    operating gates (V8 evaluation F12): shadow_ready is true only when
    the pipeline could actually produce a lock right now, and blockers
    names whatever is in the way.

    Counts are competition-scoped since the EPL build: mapped events,
    complete runs and locks join through THIS competition's fixtures,
    and the approval decision is looked up by THIS competition's model
    name. Identical numbers for MLS while it is the only populated
    competition; correct (not shared) numbers once a second one lands."""
    spec = spec or MLS_RUNS_SPEC
    if not plane_ready():
        return {}
    s = get_session()
    try:
        from src.live.models import Team
        teams = s.query(Team).filter_by(
            competition_slug=spec.slug).count()
        mapped = (s.query(MarketEvent)
                  .filter_by(competition_slug=spec.slug,
                             mapping_approved=True).count())
        runs_n = (s.query(PredictionRun).join(
            Fixture, PredictionRun.fixture_id == Fixture.id)
            .filter(Fixture.competition_slug == spec.slug,
                    PredictionRun.status == "complete").count())
        mv = approved_model_version(s, spec.model.MODEL_NAME)
        from src.live.models import ModelApprovalDecision
        approval_decision = (s.query(ModelApprovalDecision)
                             .filter_by(approved=True,
                                        model_version_name=spec.model
                                        .MODEL_NAME)
                             .order_by(ModelApprovalDecision.id.desc())
                             .first())
        # upcoming fixtures (48h) whose teams lack an approved mapped
        # market event — the invariant the decision doc asked for.
        # NON-CLUB fixtures are excluded: ESPN's MLS feed carries the
        # All-Star game (MLS All-Stars vs Liga MX All-Stars — event
        # 401864004, 2026-07-30), whose "teams" are not clubs, so identity
        # correctly refuses to map them and no KXMLSGAME market exists.
        # They are not shadow-predictable by construction (the model
        # already skips them), so counting them as "unmapped" would have
        # raised a FALSE readiness blocker the moment that game entered
        # the 48h window — i.e. Jul 28 (found by audit Jul 24).
        horizon = _now() + timedelta(hours=48)
        upcoming = [f for f in s.query(Fixture)
                    .filter_by(competition_slug=spec.slug, status="pre")
                    .all()
                    if f.current_kickoff_utc
                    and _utc(f.current_kickoff_utc) <= horizon
                    and f.home_team_id is not None
                    and f.away_team_id is not None]
        unmapped_upcoming = [
            f.espn_event_id for f in upcoming
            if not s.query(MarketEvent).filter_by(
                fixture_id=f.id, mapping_approved=True).first()]
        blockers = []
        if teams < spec.expected_teams:
            blockers.append(f"teams {teams}/{spec.expected_teams}")
        if mv is None:
            blockers.append("model not approved for shadow")
        if approval_decision is None:
            # V9 eval F1/F17: shadow readiness now requires the immutable
            # CI-based approval decision, not just the ModelVersion flag
            blockers.append("no approved model-approval decision")
        if runs_n == 0:
            blockers.append("no complete runs")
        if unmapped_upcoming:
            blockers.append(
                f"unmapped upcoming fixtures: {unmapped_upcoming[:5]}")
        return {
            "teams": teams,
            "fixtures": s.query(Fixture).filter_by(
                competition_slug=spec.slug).count(),
            "completed_fixtures": s.query(Fixture).filter_by(
                competition_slug=spec.slug, status="post").count(),
            "complete_runs": runs_n,
            "t10_locks": (s.query(PredictionRun).join(
                Fixture, PredictionRun.fixture_id == Fixture.id)
                .filter(Fixture.competition_slug == spec.slug,
                        PredictionRun.run_type == "t10",
                        PredictionRun.canonical.is_(True),
                        PredictionRun.status == "complete").count()),
            "mapped_events": mapped,
            "model_approved_for_shadow": mv is not None,
            "approval_decision_present": approval_decision is not None,
            "upcoming_48h": len(upcoming),
            "unmapped_upcoming": len(unmapped_upcoming),
            "shadow_ready": not blockers,
            "blockers": blockers,
        }
    except Exception as exc:
        return {"error": str(exc)[:200]}
    finally:
        s.close()


def latest_odds(spec: CompetitionRunsSpec | None = None) -> list[dict]:
    """Every upcoming fixture's newest complete run — the public shadow
    odds board. Reads ONLY status='complete' runs, per the decision."""
    spec = spec or MLS_RUNS_SPEC
    if not plane_ready():
        return []
    s = get_session()
    out = []
    try:
        for f in (s.query(Fixture)
                  .filter_by(competition_slug=spec.slug)
                  .filter(Fixture.status.in_(("pre", "in")))
                  .all()):
            run = (s.query(PredictionRun)
                   .filter_by(fixture_id=f.id, status="complete")
                   .order_by(PredictionRun.captured_at.desc())
                   .first())
            if run is None:
                continue
            contracts = (s.query(PredictionContract)
                         .filter_by(prediction_run_id=run.id).all())
            out.append({
                "espn_event_id": f.espn_event_id,
                "kickoff": (_utc(f.current_kickoff_utc).isoformat()
                            if f.current_kickoff_utc else None),
                "run_type": run.run_type,
                "captured_at": (_utc(run.captured_at).isoformat()
                                if run.captured_at else None),
                "model_version": spec.model.MODEL_NAME,
                "outcomes": {c.outcome_key: round(c.raw_probability, 4)
                             for c in contracts
                             if c.outcome_key in ("home_win", "draw",
                                                  "away_win")},
                "locked": run.run_type == "t10" and run.canonical,
            })
        return out
    finally:
        s.close()
