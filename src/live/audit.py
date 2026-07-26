"""Lock acceptance audit (V8.1 evaluation): a machine-readable check
of every T-10 lock's integrity invariants, plus RETAINED evidence of
what went wrong — missed locks and failed snapshots are data, not
noise. This is INTEGRITY auditing (does the observation hold up?), not
forecast scoring (is the model any good?) — the latter is a separate
prospective study that needs settled results.

Scope = fixtures the shadow pipeline has actually touched (any
prediction run), so historical pre-shadow fixtures never count as
"missed". The output carries a content hash so an exported copy can be
proven to match the database it came from.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from src.live.db import get_session, plane_ready
from src.live.models import (Fixture, MarketSnapshot,
                             ModelApprovalDecision, ModelInputArtifact,
                             PredictionContract, PredictionRun)

AUDIT_VERSION = "mls-lock-audit-v1"
THREE_WAY = ("home_win", "draw", "away_win")


def _now():
    return datetime.now(timezone.utc)


def _utc(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _paper_coverage_summary() -> dict:
    """How much of the paper evidence the locks were ELIGIBLE for
    actually exists. Reported at summary level (see the call site) and
    kept small so it stays inside the audit's content hash without
    dominating it."""
    try:
        from src.live import paper
        cov = paper.paper_coverage()
        return {k: cov.get(k) for k in
                ("locks_eligible", "locks_uncovered", "legs_eligible",
                 "legs_signalled", "legs_missing", "coverage_pct",
                 "complete", "backfilled_signals")}
    except Exception as exc:
        return {"error": str(exc)[:120]}


def _lock_checks(s, f, lock, n_locks, current_engine=None) -> dict:
    """Every invariant from the evaluation's acceptance table, as a flat
    dict of booleans. all_pass is their AND."""
    ko = _utc(f.current_kickoff_utc)
    contracts = (s.query(PredictionContract)
                 .filter_by(prediction_run_id=lock.id).all())
    okeys = [c.outcome_key for c in contracts]
    three = {c.outcome_key: c.raw_probability
             for c in contracts if c.outcome_key in THREE_WAY}
    # a market-priced contract is one attached to a market contract; it
    # must carry the frozen quote link
    priced = [c for c in contracts if c.market_contract_id is not None]
    snap = (s.get(MarketSnapshot, lock.market_snapshot_id)
            if lock.market_snapshot_id else None)
    # the immutable approval decision the run was authorized under
    # (V9 eval F1/F10) — a REQUIRED reference, not just the boolean
    approval = (s.get(ModelApprovalDecision, lock.model_approval_decision_id)
                if lock.model_approval_decision_id else None)
    # the engine signature frozen into the input artifact (V9 eval F4) —
    # presence only, cheap: no full replay is run during the audit
    art = (s.get(ModelInputArtifact, lock.model_input_artifact_id)
           if lock.model_input_artifact_id else None)
    engine_sig = None
    if art and art.document_json:
        try:
            engine_sig = ((json.loads(art.document_json).get("engine") or {})
                          .get("signature_hash"))
        except (ValueError, TypeError):
            engine_sig = None
    # V9.1 eval F4: VALIDATE, don't just check presence. Recompute the
    # approval-decision hash from its stored canonical bytes, and compare
    # the lock's frozen engine signature against the CURRENT engine.
    approval_hash_valid = bool(
        approval and approval.decision_document and approval.content_hash
        and hashlib.sha256(approval.decision_document.encode()).hexdigest()
        == approval.content_hash)
    from src.live import model_mls
    if current_engine is None:
        current_engine = model_mls.engine_signature()["signature_hash"]
    # The signature includes the git revision, so any deploy — a docs
    # commit, a migration — flips it. Accept a stored signature that
    # reproduces exactly when recomputed under the revision the run
    # RECORDED: that proves only the revision moved. A real engine change
    # still fails. Without this, every lock goes red on the next deploy
    # and a permanently-red audit is one nobody reads.
    engine_matches_current, engine_rev_drift = model_mls.engine_matches(
        engine_sig, lock.git_revision)
    # a later COMPLETE run must not exist for this fixture (F9)
    later = (s.query(PredictionRun)
             .filter_by(fixture_id=f.id, status="complete")
             .filter(PredictionRun.captured_at > lock.captured_at)
             .filter(PredictionRun.id != lock.id).count())
    checks = {
        "exactly_one_canonical_lock": n_locks == 1,
        "lock_before_kickoff": bool(
            ko and lock.captured_at and _utc(lock.captured_at) < ko),
        "lock_inside_window": (lock.seconds_before_kickoff is not None
                               and 0 < lock.seconds_before_kickoff
                               <= 11 * 60),
        "snapshot_present": snap is not None,
        "snapshot_complete": bool(snap and snap.status == "complete"),
        "snapshot_policy_version": bool(snap and snap.policy_version),
        "required_families_complete": bool(
            snap and snap.required_families_complete),
        "contracts_unique_by_outcome": len(okeys) == len(set(okeys)),
        # V9.3 eval F5: `priced` filters to contracts that already have a
        # market_contract_id, so all([]) made this VACUOUSLY TRUE — a
        # canonical lock with ZERO market links passed the whole audit
        # while proving no model-to-market join at all. The check now
        # requires it to be non-empty AND demands all three game legs be
        # mapped to a contract and a FROZEN quote by name.
        "priced_contracts_quote_linked": bool(priced) and all(
            c.market_quote_id is not None for c in priced),
        "three_way_market_linked": all(
            any(c.outcome_key == k
                and c.market_contract_id is not None
                and c.market_quote_id is not None
                for c in contracts)
            for k in ("home_win", "draw", "away_win")),
        "model_version_present": lock.model_version_id is not None,
        "model_approved_at_run": bool(lock.model_approved_at_run),
        # the run must reference the EXACT immutable approval decision that
        # authorized it (V9 eval F1/F10) — required, not informational
        "approval_decision_referenced": (
            lock.model_approval_decision_id is not None),
        "approval_decision_exists": approval is not None,
        "approval_decision_model_matches": bool(
            approval and approval.model_version_id == lock.model_version_id),
        "approval_decision_is_shadow": bool(
            approval and approval.approved
            and approval.approved_mode == "shadow"),
        "approval_decision_precedes_run": bool(
            approval and approval.created_at and lock.captured_at
            and _utc(approval.created_at) <= _utc(lock.captured_at)),
        "approval_decision_hash_present": bool(
            approval and approval.content_hash),
        # the approval hash must RECOMPUTE from its stored bytes (V9.1 F4)
        "approval_decision_hash_valid": approval_hash_valid,
        "input_hash_present": bool(lock.input_snapshot_hash),
        "input_artifact_retained": lock.model_input_artifact_id is not None,
        # the frozen engine signature must be present AND match the current
        # engine (V9.1 eval F4) — presence alone is engine-blind
        "engine_signature_present": bool(engine_sig),
        "engine_signature_matches_current": engine_matches_current,
        # a lineup snapshot must be REFERENCED (Phase 5) — whether or not
        # the lineup was confirmed. Its absence is a provenance gap; a
        # PENDING lineup inside it is honest data, not a failure.
        "lineup_snapshot_referenced": lock.lineup_snapshot_id is not None,
        "input_quality_recorded": bool(lock.input_quality_json),
        "seed_present": lock.simulation_seed is not None,
        "three_way_present": set(three) == set(THREE_WAY),
        "three_way_sums_to_one": abs(sum(three.values()) - 1.0) < 0.02
        if len(three) == 3 else False,
        "no_post_kickoff_replacement": later == 0,
    }
    row = {
        "espn_event_id": f.espn_event_id,
        "kickoff": ko.isoformat() if ko else None,
        "lock_run_id": lock.id,
        "captured_at": (_utc(lock.captured_at).isoformat()
                        if lock.captured_at else None),
        "seconds_before_kickoff": lock.seconds_before_kickoff,
        "market_snapshot_id": lock.market_snapshot_id,
        "approval_decision_id": lock.model_approval_decision_id,
        "approval_decision_hash": approval.content_hash if approval else None,
        "approval_hash_recomputed_ok": approval_hash_valid,
        "engine_signature_hash": engine_sig,
        "current_engine_signature_hash": current_engine,
        "engine_matches_current": engine_matches_current,
        # true when the engine is provably identical and ONLY the build
        # revision differs — informational, never a failure
        "engine_revision_only_drift": engine_rev_drift,
        "run_git_revision": lock.git_revision,
        "contracts": len(contracts),
        "priced_contracts": len(priced),
        "input_quality": (json.loads(lock.input_quality_json)
                          if lock.input_quality_json else None),
        "checks": checks,
        "all_pass": all(checks.values()),
    }
    if snap is not None:
        row["snapshot"] = {
            "status": snap.status,
            "policy_version": snap.policy_version,
            "provider_schema_version": snap.provider_schema_version,
            "events_expected": snap.events_expected,
            "events_captured": snap.events_captured,
            "contracts_expected": snap.contracts_expected,
            "quotes_written": snap.quotes_written,
            "quotes_with_prices": snap.quotes_with_prices,
            "quotes_without_prices": snap.quotes_without_prices,
            "depth_rows": snap.depth_rows_written,
            "oldest_quote_age_seconds": snap.oldest_quote_age_seconds,
            "execution_ready": snap.execution_ready,
        }
    return row


def verify_replay(run_id: str, tol: float = 1e-6) -> dict:
    """Independent-reproducibility check (Phase 2 acceptance): replay a
    run FROM ITS STORED INPUT ARTIFACT ALONE and confirm the outcomes
    match the stored contracts. This is the proof behind the
    'independently model-reproducible' claim, so it reads only the
    artifact bytes — never the live ratings — to reconstruct."""
    import json as _json

    from src.live import model_mls
    if not plane_ready():
        return {"skipped": "dormant"}
    s = get_session()
    try:
        run = s.get(PredictionRun, run_id)
        if run is None or run.model_input_artifact_id is None:
            return {"run_id": run_id, "replayable": False,
                    "reason": "no input artifact"}
        art = s.get(ModelInputArtifact, run.model_input_artifact_id)
        doc = _json.loads(art.document_json)
        # the frozen (stored) engine signature vs the current engine
        # (V9 eval F4). Surfaced in EVERY return path so the reproducibility
        # claim is engine-matched and independently visible, never blind.
        stored_sig = (doc.get("engine") or {}).get("signature_hash")
        current_sig = model_mls.engine_signature()["signature_hash"]
        if stored_sig:
            engine_match, rev_only = model_mls.engine_matches(
                stored_sig, run.git_revision)
        else:
            engine_match, rev_only = None, False
        base = {
            "run_id": run_id,
            "artifact_hash": art.content_hash,
            "artifact_schema": art.schema_version,
            "stored_engine_signature_hash": stored_sig,
            "current_engine_signature_hash": current_sig,
            "engine_match": engine_match,
            # the engine is identical; only the build revision moved
            "engine_revision_only_drift": rev_only,
            "run_git_revision": run.git_revision,
        }
        # engine-drift guard: a v2+ artifact whose signature no longer
        # matches the current engine is REFUSED — the numbers would
        # silently diverge. "Replayable under the matching engine", never
        # "bit-identical from the bytes alone across versions".
        if stored_sig is not None and not engine_match:
            return {**base, "replayable": False,
                    "reason": "engine signature mismatch — refusing to "
                              "replay under a different engine"}
        replayed = model_mls.replay_from_artifact(doc)
        if replayed is None:
            return {**base, "replayable": False,
                    "reason": "artifact missing ratings"}
        stored = {c.outcome_key: c.raw_probability
                  for c in s.query(PredictionContract)
                  .filter_by(prediction_run_id=run_id).all()
                  if c.outcome_key in THREE_WAY}
        deltas = {k: abs(replayed.get(k, 0.0) - stored.get(k, 0.0))
                  for k in THREE_WAY}
        return {
            **base,
            "replayable": all(d <= tol for d in deltas.values()),
            "max_delta": max(deltas.values()) if deltas else None,
        }
    finally:
        s.close()


def lock_audit() -> dict:
    """Integrity audit over every shadow-touched fixture."""
    if not plane_ready():
        return {"skipped": "dormant"}
    s = get_session()
    try:
        # compute the current engine signature ONCE for the whole audit
        # (V9.1 eval F4) rather than per-lock — it hashes source files
        from src.live import model_mls
        current_engine = model_mls.engine_signature()["signature_hash"]
        touched_ids = {r[0] for r in s.query(
            PredictionRun.fixture_id).distinct().all()}
        fixtures = [f for f in s.query(Fixture).filter_by(
            competition_slug="mls-2026").all() if f.id in touched_ids]
        locks_out, missed = [], []
        for f in fixtures:
            ko = _utc(f.current_kickoff_utc)
            kicked_off = ko is not None and ko < _now()
            canon = (s.query(PredictionRun)
                     .filter_by(fixture_id=f.id, run_type="t10",
                                canonical=True, status="complete")
                     .order_by(PredictionRun.captured_at.desc()).all())
            if not canon:
                # a fixture that has kicked off with no lock is retained
                # evidence, not silence (the evaluation's instruction)
                if kicked_off:
                    fails = (s.query(MarketSnapshot)
                             .filter_by(fixture_id=f.id, status="failed")
                             .count())
                    missed.append({
                        "espn_event_id": f.espn_event_id,
                        "kickoff": ko.isoformat() if ko else None,
                        "failed_snapshot_attempts": fails,
                    })
                continue
            for lock in canon:
                locks_out.append(_lock_checks(s, f, lock, len(canon),
                                              current_engine))
        failed_snaps = [{
            "market_snapshot_id": sn.id,
            "fixture_id": sn.fixture_id,
            "captured_at": (_utc(sn.captured_at).isoformat()
                            if sn.captured_at else None),
            "failure_reason": sn.failure_reason,
        } for sn in s.query(MarketSnapshot).filter_by(
            status="failed").all()]
        body = {
            "audit_version": AUDIT_VERSION,
            "generated_at": _now().isoformat(),
            "locks": locks_out,
            "missed_locks": missed,
            "failed_snapshots": failed_snaps,
            "summary": {
                "shadow_touched_fixtures": len(fixtures),
                "canonical_locks": len(locks_out),
                "locks_all_pass": sum(1 for r in locks_out
                                      if r["all_pass"]),
                "locks_with_failures": sum(1 for r in locks_out
                                           if not r["all_pass"]),
                "missed_locks": len(missed),
                "failed_snapshots": len(failed_snaps),
                # Ledger completeness is reported BESIDE lock integrity,
                # never inside a lock's `checks`. A lock whose paper
                # engine never ran is still a perfectly valid lock — it
                # is the PAPER EVIDENCE that is missing, and conflating
                # the two would either excuse the gap or falsely condemn
                # 15 clean locks. `clean` stays a statement about locks.
                "paper_coverage": _paper_coverage_summary(),
                "clean": (all(r["all_pass"] for r in locks_out)
                          and not missed),
            },
        }
        # content hash over canonical serialization EXCLUDING the
        # wall-clock generated_at, so the same DB state hashes identically
        core = {k: v for k, v in body.items() if k != "generated_at"}
        body["content_hash"] = hashlib.sha256(
            json.dumps(core, sort_keys=True).encode()).hexdigest()
        return body
    finally:
        s.close()
