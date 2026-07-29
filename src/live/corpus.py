"""Prospective research corpus exporter (V8.1 evaluation Phase 3).

Produces a SELF-CONTAINED snapshot of the MLS shadow evidence — every
entity a researcher needs to regenerate forecast, market-comparison,
reproducibility, and audit results WITHOUT access to the production
database. Includes failures (missed locks, failed snapshots) so the
corpus is free of survivorship bias.

The manifest carries per-file record counts and content hashes plus an
overall hash over the DATA (not the wall-clock timestamps), so the same
database state exports to the same manifest_hash. Published corpus
versions are immutable — bump the version, never overwrite.

Scope note: quotes/depth are the LOCK-SNAPSHOT evidence (the frozen
T-10 books), not the routine observation stream — the research-relevant
set, and bounded. This is stated in the manifest.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

from src.live import audit as live_audit
from src.live.journal import public_bet_projection
from src.live.db import get_session, plane_ready
from src.live.models import (Competition, CorpusExport, Fixture,
                             LineupEntry, LineupSnapshot, MarketContract,
                             MarketDepthLevel, MarketEvent, MarketQuote,
                             MarketSnapshot, MlsPlayerMatchStat,
                             MlsTeamMatchStat, ModelApprovalDecision,
                             ModelInputArtifact, ModelVersion,
                             PaperEvaluationContext, PaperFill,
                             PersonalBet, PersonalBetExecution,
                             PaperSignal, Player, PredictionContract,
                             PredictionRun, RegistryDiscovery,
                             SourceObservation, Team, TeamAlias)

# v2 (V9.3 eval F10): adds the RESEARCH plane — approval decisions,
# registry sweeps, official per-match team/player stats and the exact
# selected model parameters — so the corpus can regenerate the
# model-DEVELOPMENT result, not only replay a final run.
#
# v3 (journal-P0 F2/F9): documents and disciplines the two journal
# sections that entered under the v2 label without a bump:
#
#   personal_journal.json            Son's recorded views (taken AND
#                                    passed) — evidence class
#                                    `personal_journal`, execution-
#                                    fidelity documentation, NEVER edge
#                                    evidence, never summed with the
#                                    paper ledger. `rationale` is
#                                    private prose and never exports.
#   personal_journal_executions.json The pilot's real executions.
#                                    Private fields (account label,
#                                    order id, fill economics, consent
#                                    provenance, reconciliation note)
#                                    export ONLY under the row's
#                                    explicit publication_consent.
#
# Both sections are scoped to the corpus's competition, exactly like
# fixtures/runs/markets — a second league's journal must not leak into
# an MLS corpus.
#
# v4 (journal-P1-F): journal entries carry an explicit `superseded`
# flag and counts_toward_aggregate honours it — a corrected row stays
# exported (immutable audit) but is labelled as contributing no
# effective observation; manifest counts carry raw vs effective.
# Corrections are scope-checked at write time (same competition/
# fixture/market/outcome), so a competition-scoped corpus cannot
# contain a dangling corrects_bet_id.
#
# v5 (journal-P0-H + closure): the journal sections are REFERENTIALLY
# CLOSED. `market_quotes.json` was scoped to lock-snapshot quotes only,
# but a journal entry may cite any persisted observation — so an
# exported entry's `market_quote_id` (and an exported execution's
# `market_quote_id_at_fill`) could name a quote no file in the bundle
# contained. The corpus claims to be self-contained; a reader could not
# check the one price the entry exists to make checkable. Journal-
# referenced quotes are now included, scoped to what actually exports:
# entry quotes always, fill-book quotes only for executions that carry
# publication consent. Entries additionally carry the quote's capture
# age, the ceiling applied and any refusal reason, so a reader can
# verify the falsifiability claim rather than take it on trust.
CORPUS_SCHEMA = "corpus-v5"
_GIT_REV = os.getenv("RAILWAY_GIT_COMMIT_SHA", "")[:40]


def _now():
    return datetime.now(timezone.utc)


def _model_parameters() -> dict:
    """The exact deployed model configuration + what each constant was
    SELECTED AGAINST (V9.3 eval F7/F10). A corpus that omits this cannot
    regenerate the model-development result, and an approval that omits it
    cannot say what it approved."""
    import config

    from src.live import model_mls
    return {
        "model_version": model_mls.MODEL_NAME,
        "engine_signature": model_mls.engine_signature()["signature_hash"],
        "artifact_schema": model_mls.INPUT_ARTIFACT_SCHEMA,
        "parameters": {
            "xg_rating_alpha": config.MLS_XG_RATING_ALPHA,
            "xg_shrink_games": model_mls.XG_SHRINK_GAMES,
            "goals_shrink_games": model_mls.SHRINK_GAMES,
            "half_life_days": model_mls.HALF_LIFE_DAYS,
            "min_games": model_mls.MIN_GAMES,
            "calibration_alpha": config.MLS_CALIBRATION_ALPHA,
            "mls_goal_dispersion_cv": config.MLS_GOAL_DISPERSION_CV,
            "wc26_goal_dispersion_cv": config.GOAL_DISPERSION_CV,
            "n_simulations": config.N_SIMULATIONS,
        },
        "selection_protocol": {
            "method": "rolling-origin walk-forward, match-cluster bootstrap",
            "sample": "the season's completed top-division fixtures",
            "primary_metric": "3-way log loss vs the M0 league/venue baseline",
            "limitation": ("hyperparameters were swept on THIS sample, so "
                           "the reported interval is conditional on the "
                           "selected model and excludes model-selection "
                           "uncertainty (V9.3 eval F8)"),
        },
    }


def _dump(obj) -> dict:
    """Generic column -> JSON-safe dict for any live-plane row."""
    from sqlalchemy import inspect as _inspect
    out = {}
    for c in _inspect(obj).mapper.column_attrs:
        v = getattr(obj, c.key)
        if isinstance(v, datetime):
            v = (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).isoformat()
        out[c.key] = v
    return out


# journal-P0 F2 + P0-C: the corpus is PUBLIC bytes. Journal entries
# export through THE single public projection defined in
# src.live.journal (one field list for briefing, journal and corpus —
# never two drifting copies). Executions are a third party's financial
# record: a row exports ONLY under its explicit publication_consent
# (default false), and then in full; absent consent even its
# occurrence/timeline stays out of public bytes — the manifest carries
# aggregate counts so the omission is explicit, never silent.


def _book_observations(s, depth) -> list:
    """The raw order-book observations the exported depth rows point at.

    Scoped to referenced books rather than the whole observation stream:
    the corpus is lock evidence, and an unbounded dump of every routine
    poll would dwarf it without adding auditability."""
    ids = {d.book_observation_id for d in depth
           if getattr(d, "book_observation_id", None)}
    if not ids:
        return []
    return (s.query(SourceObservation)
            .filter(SourceObservation.id.in_(ids)).all())


def build_corpus(version: str = "mls-shadow-2026-v1") -> dict:
    """Read the live plane into an in-memory, self-contained bundle +
    manifest. Deterministic for a given DB state (manifest_hash covers
    data, not timestamps)."""
    if not plane_ready():
        return {"skipped": "dormant"}
    s = get_session()
    try:
        comp = "mls-2026"
        fixtures = s.query(Fixture).filter_by(
            competition_slug=comp).all()
        fixture_ids = {f.id for f in fixtures}
        runs = s.query(PredictionRun).filter(
            PredictionRun.fixture_id.in_(fixture_ids)).all()
        run_ids = {r.id for r in runs}
        artifact_ids = {r.model_input_artifact_id for r in runs
                        if r.model_input_artifact_id}
        contracts = [c for c in s.query(PredictionContract).all()
                     if c.prediction_run_id in run_ids]
        events = s.query(MarketEvent).filter_by(
            competition_slug=comp).all()
        event_ids = {e.id for e in events}
        mcontracts = [c for c in s.query(MarketContract).all()
                      if c.market_event_id in event_ids]
        snapshots = [sn for sn in s.query(MarketSnapshot).all()
                     if sn.fixture_id in fixture_ids]
        snap_ids = {sn.id for sn in snapshots}
        # RESEARCH scope: quotes frozen into a lock snapshot (+ depth)
        quotes = [q for q in s.query(MarketQuote).all()
                  if q.market_snapshot_id in snap_ids]
        quote_ids = {q.id for q in quotes}
        depth = [d for d in s.query(MarketDepthLevel).all()
                 if d.market_quote_id in quote_ids]
        # journal quote evidence — resolved below, once the journal rows
        # are known, so the bundle closes over what it actually exports
        lineups = [ln for ln in s.query(LineupSnapshot).all()
                   if ln.fixture_id in fixture_ids]
        lineup_ids = {ln.id for ln in lineups}
        lineup_entries = [le for le in s.query(LineupEntry).all()
                          if le.lineup_snapshot_id in lineup_ids]
        # journal-P1 F9: the journal scopes to THIS competition like
        # everything else — a second league's entries stay out
        journal_bets = (s.query(PersonalBet)
                        .filter_by(competition_slug=comp).all())
        journal_bet_ids = {b.id for b in journal_bets}
        journal_execs = [e for e in s.query(PersonalBetExecution).all()
                         if e.personal_bet_id in journal_bet_ids]
        # journal-P1-F: corrected rows export as immutable audit,
        # labelled superseded — one effective observation per chain
        journal_superseded = {b.corrects_bet_id for b in journal_bets
                              if b.corrects_bet_id is not None}
        # corpus-v5: close the bundle over the quotes the journal
        # sections REFER TO. Entry quotes always (every entry exports);
        # fill-book quotes only for executions that actually export,
        # i.e. those carrying publication consent — pulling in a quote
        # referenced solely by a withheld execution would leak which
        # book that execution used.
        published_execs = [e for e in journal_execs
                           if e.publication_consent]
        journal_quote_ids = {b.market_quote_id for b in journal_bets
                             if b.market_quote_id is not None}
        journal_quote_ids |= {e.market_quote_id_at_fill
                              for e in published_execs
                              if e.market_quote_id_at_fill is not None}
        missing_quote_ids = journal_quote_ids - quote_ids
        if missing_quote_ids:
            extra = (s.query(MarketQuote)
                     .filter(MarketQuote.id.in_(missing_quote_ids)).all())
            quotes = quotes + extra
            quote_ids = quote_ids | {q.id for q in extra}

        sections = {
            "competitions.json": [_dump(x) for x in
                                  s.query(Competition).all()],
            "teams.json": [_dump(x) for x in s.query(Team).filter_by(
                competition_slug=comp).all()],
            "team_aliases.json": [_dump(x) for x in
                                  s.query(TeamAlias).all()],
            "fixtures.json": [_dump(x) for x in fixtures],
            "model_versions.json": [_dump(x) for x in
                                    s.query(ModelVersion).all()],
            "model_input_artifacts.json": [
                _dump(x) for x in s.query(ModelInputArtifact).all()
                if x.id in artifact_ids],
            "prediction_runs.json": [_dump(x) for x in runs],
            "prediction_contracts.json": [_dump(x) for x in contracts],
            "market_events.json": [_dump(x) for x in events],
            "market_contracts.json": [_dump(x) for x in mcontracts],
            "market_snapshots.json": [_dump(x) for x in snapshots],
            "market_quotes.json": [_dump(x) for x in quotes],
            "market_depth_levels.json": [_dump(x) for x in depth],
            # V9.5 eval: the COMPLETE raw order books the depth rows were
            # parsed from. Without these the corpus could not support
            # re-parsing an original book or auditing best-N selection —
            # `SourceObservation` was not exported at all.
            "source_observations.json": [
                _dump(x) for x in _book_observations(s, depth)],
            # The personal journal and the pilot's real executions.
            # Exported so the execution-fidelity evidence travels with
            # the corpus — but they are a DIFFERENT evidence class from
            # everything above and must never be folded into the
            # forecast or market reports. Human-selected bets cannot
            # measure edge; they measure whether execution behaves as
            # modelled. Entries go through THE public projection
            # (journal-P0-C); executions export only under explicit
            # publication_consent, then in full. Both sections scope to
            # THIS corpus's competition (journal-P1 F9).
            "personal_journal.json": [
                public_bet_projection(
                    x, superseded=x.id in journal_superseded)
                for x in journal_bets],
            "personal_journal_executions.json": [
                _dump(x) for x in published_execs],
            # V9.5 eval C1: the frozen paper/risk state each lock was
            # evaluated against, so a reader can verify that a paper
            # decision was a pure function of frozen inputs
            "paper_evaluation_contexts.json": [
                _dump(x) for x in s.query(PaperEvaluationContext).all()],
            "players.json": [_dump(x) for x in s.query(Player).all()],
            "lineup_snapshots.json": [_dump(x) for x in lineups],
            "lineup_entries.json": [_dump(x) for x in lineup_entries],
            # paper trading — signals (incl. rejections) + fills, so the
            # execution-strategy metrics reproduce from the corpus too
            "paper_signals.json": [_dump(x) for x in
                                   s.query(PaperSignal).all()],
            "paper_fills.json": [_dump(x) for x in
                                 s.query(PaperFill).all()],
            # audit carries missed_locks + failed_snapshots = the
            # anti-survivorship-bias record
            "audit.json": live_audit.lock_audit(),
            # --- RESEARCH plane (V9.3 eval F10) ---------------------------
            # The run-replay sections above let a reader reproduce a FINAL
            # run. They do not let anyone regenerate the model-DEVELOPMENT
            # result: the ladder, the parameter sweeps, or the approval.
            # These objects close that gap, so the corpus is self-contained
            # for the research claim and not just the prediction claim.
            "model_approval_decisions.json": [
                _dump(x) for x in s.query(ModelApprovalDecision).all()],
            "registry_discovery.json": [
                _dump(x) for x in s.query(RegistryDiscovery).all()],
            "mls_team_match_stats.json": [
                _dump(x) for x in s.query(MlsTeamMatchStat).all()],
            "mls_player_match_stats.json": [
                _dump(x) for x in s.query(MlsPlayerMatchStat).all()],
            # the exact parameters the deployed model was selected with,
            # so a reader can re-run the sweeps rather than trust them
            "model_parameters.json": _model_parameters(),
        }
        files = {}
        for name, data in sections.items():
            body = json.dumps(data, sort_keys=True, ensure_ascii=False)
            files[name] = {
                "records": len(data) if isinstance(data, list) else 1,
                "sha256": hashlib.sha256(body.encode()).hexdigest(),
            }
        audit_summary = (sections["audit.json"].get("summary")
                         if isinstance(sections["audit.json"], dict)
                         else {})
        counts = {
            "fixtures": len(fixtures),
            "completed_fixtures": sum(1 for f in fixtures
                                      if f.status == "post"),
            "prediction_runs": len(runs),
            "canonical_locks": sum(1 for r in runs
                                   if r.run_type == "t10"
                                   and r.canonical
                                   and r.status == "complete"),
            "input_artifacts": len(artifact_ids),
            "lock_snapshots": len(snapshots),
            "frozen_quotes": len(quotes),
            # corpus-v5: how many of the exported quotes are there only
            # because a journal row cites them. Stated, so `quote_scope`
            # cannot be read as "lock snapshots only" when it is not.
            "journal_referenced_quotes": len(missing_quote_ids),
            "depth_rows": len(depth),
            "missed_locks": audit_summary.get("missed_locks", 0),
            "failed_snapshots": audit_summary.get("failed_snapshots", 0),
            "lineup_snapshots": len(lineups),
            "players": len(sections["players.json"]),
            "paper_signals": len(sections["paper_signals.json"]),
            "paper_fills": len(sections["paper_fills.json"]),
            # journal-P0-C: consent-gated omissions are explicit — the
            # difference between total and published is the number of
            # executions withheld from public bytes
            "journal_entries": len(journal_bets),
            # journal-P1-F: raw vs effective is explicit — the
            # difference is the number of corrected (superseded) rows
            "journal_entries_effective": (len(journal_bets)
                                          - len(journal_superseded)),
            "journal_entries_superseded": len(journal_superseded),
            "journal_executions_total": len(journal_execs),
            "journal_executions_published": len(
                sections["personal_journal_executions.json"]),
        }
        manifest = {
            "corpus_version": version,
            "schema_version": CORPUS_SCHEMA,
            "created_at": _now().isoformat(),
            "db_cutoff": _now().isoformat(),
            "backend_revision": _GIT_REV,
            "model_versions": [m["name"] for m in
                               sections["model_versions.json"]],
            "quote_scope": "lock_snapshot_plus_journal_referenced",
            "files": files,
            "counts": counts,
        }
        # hash over DATA (file hashes + counts), NOT the timestamps
        core = json.dumps({"files": files, "counts": counts,
                           "corpus_version": version,
                           "schema_version": CORPUS_SCHEMA},
                          sort_keys=True)
        manifest["manifest_hash"] = hashlib.sha256(
            core.encode()).hexdigest()
        return {"manifest": manifest, "sections": sections}
    finally:
        s.close()


def publish_corpus(version: str) -> dict:
    """Freeze the current corpus as an IMMUTABLE published version (V9
    eval F3). build_corpus reads live state, so its bytes drift as the
    database grows — meaning the same version LABEL rebuilt on each call
    is NOT immutable. Publishing stores one version's bytes + manifest in
    corpus_export; get_published then serves FROM that row, never a
    rebuild. Re-publishing an existing version is ALWAYS refused — there is
    no overwrite path (V9.1 eval F12); bump the version instead. A mistaken
    publish is corrected by a deliberate migration, not a runtime flag."""
    if not plane_ready():
        return {"skipped": "dormant"}
    bundle = build_corpus(version)
    if "manifest" not in bundle:
        return bundle
    manifest = bundle["manifest"]
    body = json.dumps(bundle, sort_keys=True, ensure_ascii=False)
    s = get_session()
    try:
        existing = s.query(CorpusExport).filter_by(version=version).first()
        if existing is not None:
            return {"error": "version already published — corpus versions "
                             "are immutable; bump the version",
                    "version": version,
                    "manifest_hash": existing.manifest_hash}
        row = CorpusExport(
            version=version,
            schema_version=manifest.get("schema_version"),
            manifest_hash=manifest["manifest_hash"],
            manifest_json=json.dumps(manifest, sort_keys=True,
                                     ensure_ascii=False),
            bundle_json=body, backend_revision=_GIT_REV,
            size_bytes=len(body.encode()), published_at=_now())
        s.add(row)
        s.commit()
        return {"published": version,
                "manifest_hash": manifest["manifest_hash"],
                "size_bytes": row.size_bytes}
    finally:
        s.close()


def list_published() -> list[dict]:
    """The published (immutable) corpus versions, newest first."""
    if not plane_ready():
        return []
    s = get_session()
    try:
        return [{"version": r.version, "manifest_hash": r.manifest_hash,
                 "schema_version": r.schema_version,
                 "size_bytes": r.size_bytes,
                 "published_at": (r.published_at.isoformat()
                                  if r.published_at else None)}
                for r in s.query(CorpusExport)
                .order_by(CorpusExport.id.desc()).all()]
    finally:
        s.close()


def latest_published_version() -> str | None:
    """The newest published corpus version, or None when none exists.

    Exists so an approval decision can BIND to a published corpus without
    an operator having to carry the version string around. Boot called
    ensure_approval_decision() with no corpus, so every decision recorded
    corpus_version=null — the binding the evidence contract asks for was
    available all along and simply never wired up."""
    pub = list_published()
    return pub[0]["version"] if pub else None


def get_published(version: str, full: bool = False) -> dict | None:
    """Serve a PUBLISHED version FROM its stored immutable bytes (V9 eval
    F3) — never a rebuild from current state. Manifest by default, the
    whole self-contained bundle when full=True."""
    if not plane_ready():
        return None
    s = get_session()
    try:
        row = s.query(CorpusExport).filter_by(version=version).first()
        if row is None:
            return None
        return json.loads(row.bundle_json if full else row.manifest_json)
    finally:
        s.close()


def export_corpus(out_dir: str,
                  version: str = "mls-shadow-2026-v1") -> dict:
    """Write the corpus to a directory (one JSON file per section +
    manifest.json). Refuses to overwrite an existing directory —
    published versions are immutable. Returns the manifest."""
    bundle = build_corpus(version)
    if "manifest" not in bundle:
        return bundle
    if os.path.exists(out_dir) and os.listdir(out_dir):
        raise FileExistsError(
            f"{out_dir} is not empty — corpus versions are immutable, "
            f"bump the version instead of overwriting")
    os.makedirs(out_dir, exist_ok=True)
    for name, data in bundle["sections"].items():
        with open(os.path.join(out_dir, name), "w",
                  encoding="utf-8") as fh:
            json.dump(data, fh, sort_keys=True, ensure_ascii=False,
                      indent=1)
    with open(os.path.join(out_dir, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(bundle["manifest"], fh, sort_keys=True, indent=1)
    return bundle["manifest"]
