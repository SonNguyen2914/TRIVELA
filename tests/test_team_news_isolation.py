"""The epistemic line: team news must never become a model input.

The platform already ran this experiment. Walk-forward tests measured an
XI-strength adjustment as NOT beating team-xG, and the one real signal —
key-attacker availability, +0.0034 — as not significant
(research_archive/mls_targeted_lineup_features_2026-07-24.json). Both were
switched off. Ingesting injuries and lineups under the word "news" creates
exactly the conditions for that settled experiment to creep back in
without being re-measured, so the boundary is mechanical here rather than
a promise in a docstring.

WHAT IS PROVEN, AND HOW

  1. A STATIC REFERENCE SCAN over every module that writes a PredictionRun,
     a canonical T-10 lock, a paper signal, or the engine signature. None
     of them may name the team-news module, its ORM classes or its tables.

     The scanner is proven NON-VACUOUS inside this suite: one test plants a
     violating file in a temp directory and asserts the scanner flags it.
     A guard that has never caught anything is not evidence, and a scan
     whose file list silently resolves to nothing passes for the wrong
     reason — so the list is asserted non-empty and every path is asserted
     to exist.

  2. A BEHAVIOURAL TEST that the model's own input bytes are unchanged by
     the presence of absence records. The input artifact is content-hashed,
     so this is an equality of hashes and not an inspection of fields:
     if an absence could reach the model, the hash would move.

  3. A LOCK-PATH TEST that runs the real T-10 lock with team news present
     for the fixture and asserts the committed artifact and input-quality
     record contain no trace of it.
"""
from __future__ import annotations

import ast
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import config
from src.live import model_mls, runs, team_news
from src.live.models import (Competition, Fixture, Team, TeamAbsence,
                             TeamNewsCapture, TeamNewsEvent, TeamNewsLineup)
# the live-plane fixture lives in the suite that owns it. Reuse rather than
# build a second, subtly different one — including its VARCHAR-length
# enforcement, which is what makes SQLite refuse what PostgreSQL would.
from tests.test_mls_shadow import live_session  # noqa: F401

UTC = timezone.utc
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every module that writes forecast evidence or computes the engine
# signature. If team news reaches a lock, it reaches it through one of
# these.
LOCK_PATH_MODULES = (
    "src/live/runs.py",          # PredictionRun + the canonical T-10 lock
    "src/live/model_mls.py",     # the model + engine_signature
    "src/live/paper.py",         # paper signals and fills
    "src/live/slate.py",         # slate assembly
    "src/live/audit.py",         # lock audit / replay
    "src/live/corpus.py",        # the published corpus
    "src/live/model_eval.py",    # the evaluation ladder
    "src/live/risk.py",
    "src/models/simulator.py",   # engine-signature modules
    "src/models/xg_model.py",
    "src/models/features.py",
    "jobs/scheduler.py",         # what runs unattended
)

# Naming any of these inside a lock-path module is the coupling this file
# exists to prevent — the module, its ORM classes, and its tables.
FORBIDDEN_ON_THE_LOCK_PATH = (
    "team_news",
    "TeamAbsence", "TeamNewsCapture", "TeamNewsLineup", "TeamNewsEvent",
    "team_absence", "team_news_capture", "team_news_lineup",
    "team_news_event",
)


def scan_for_forbidden(paths, tokens=FORBIDDEN_ON_THE_LOCK_PATH):
    """-> [f"{path}: {token}"] for every token named in every path.

    Deliberately a TEXT scan, not an import graph: a string literal
    ("team_absence" in raw SQL) couples just as hard as an import, and an
    import-graph check would miss it.
    """
    offenders = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for token in tokens:
            if token in text:
                offenders.append(f"{os.path.relpath(path, REPO_ROOT)}: {token}")
    return offenders


def lock_path_files() -> list[str]:
    out = []
    for rel in LOCK_PATH_MODULES:
        p = os.path.join(REPO_ROOT, rel)
        # a renamed module must break this test loudly rather than shrink
        # the scan to nothing and pass
        assert os.path.exists(p), (
            f"{rel} is in LOCK_PATH_MODULES but does not exist. Update the "
            f"list deliberately — do not let the scan quietly cover less.")
        out.append(p)
    assert out, "the lock-path file list resolved to nothing"
    return out


class TestTheScannerActuallyCatchesThings:
    """A guard that has never caught a violation is not evidence."""

    def test_the_scanner_flags_a_planted_import(self, tmp_path):
        planted = tmp_path / "pretend_runs.py"
        planted.write_text(
            "from src.live import team_news\n"
            "def lock():\n"
            "    return team_news.fixture_news('1')\n", encoding="utf-8")
        offenders = scan_for_forbidden([str(planted)])
        assert offenders, (
            "the scanner passed a file that imports team_news — it would "
            "pass the real lock path too, and prove nothing")
        assert any("team_news" in o for o in offenders)

    def test_the_scanner_flags_a_planted_orm_reference(self, tmp_path):
        planted = tmp_path / "pretend_model.py"
        planted.write_text(
            "from src.live.models import TeamAbsence\n"
            "def feature(s, fid):\n"
            "    return s.query(TeamAbsence).filter_by(fixture_id=fid).count()\n",
            encoding="utf-8")
        offenders = scan_for_forbidden([str(planted)])
        assert any("TeamAbsence" in o for o in offenders)

    def test_the_scanner_flags_a_raw_table_name_in_sql(self, tmp_path):
        """The case an import-graph check would miss entirely."""
        planted = tmp_path / "pretend_sql.py"
        planted.write_text(
            'QUERY = "SELECT count(*) FROM team_absence WHERE fixture_id = :f"\n',
            encoding="utf-8")
        offenders = scan_for_forbidden([str(planted)])
        assert any("team_absence" in o for o in offenders)

    def test_a_clean_file_produces_no_offenders(self, tmp_path):
        """CONTROL. Passes both before and after the guard exists; stated
        as a control so it is never offered as evidence of the boundary."""
        clean = tmp_path / "clean.py"
        clean.write_text("def predict(x):\n    return x * 2\n",
                         encoding="utf-8")
        assert scan_for_forbidden([str(clean)]) == []


class TestNoLockPathModuleTouchesTeamNews:

    def test_the_lock_path_never_names_team_news(self):
        offenders = scan_for_forbidden(lock_path_files())
        assert offenders == [], (
            "these modules write forecast evidence or the engine signature "
            "and must not read team news — lineup/key-attacker features "
            "were measured negative-or-marginal and switched off: "
            + "; ".join(offenders))

    def test_team_news_does_not_import_the_model_or_the_lock(self):
        """The other direction. A news module that imports the engine is
        one refactor away from feeding it."""
        path = os.path.join(REPO_ROOT, "src", "live", "team_news.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        bad = []
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                mods = [f"{base}.{a.name}" for a in node.names] + [base]
            for m in mods:
                if any(k in m for k in ("model_mls", "runs", "paper",
                                        "simulator", "xg_model", "model_eval",
                                        "hunter", "scheduler")):
                    bad.append(m)
        assert bad == [], f"team_news imports engine/lock/hunter modules: {bad}"

    def test_team_news_builds_no_alerting(self):
        """No dispatch is built here at all. The money lock governs
        dispatch and "player X is injured" on the act-now channel is a
        market-relevant claim; widening today's narrow concession is not
        an implementer's decision."""
        path = os.path.join(REPO_ROOT, "src", "live", "team_news.py")
        text = open(path, encoding="utf-8").read()
        for token in ("send_alert", "send_discord", "send_ntfy",
                      "_post_discord", "_post_ntfy", "NTFY_TOPIC",
                      "WEBHOOK"):
            assert token not in text, (
                f"team_news names {token}: no alerting may be built here")

    def test_team_news_never_aggregates_absences_into_a_number(self):
        """An aggregate of absences is a model wearing a news label. The
        schema carries no such column and the module computes no such
        value."""
        path = os.path.join(REPO_ROOT, "src", "live", "team_news.py")
        text = open(path, encoding="utf-8").read().lower()
        for token in ("strength_impact", "team_strength", "absence_score",
                      "availability_index", "severity_weight",
                      "impact_score"):
            assert token not in text, f"team_news computes {token}"
        for col in (TeamAbsence.__table__.columns):
            assert col.name not in ("impact", "severity", "weight", "score"), \
                f"team_absence carries an aggregate column: {col.name}"


# ---------------------------------------------------------------------------
# behavioural: the model's input bytes do not move
# ---------------------------------------------------------------------------

def _seed_fixture(s, kickoff_minutes=9):
    comp = s.query(Competition).filter_by(slug="mls-2026").first()
    assert comp is not None
    h = Team(competition_slug="mls-2026", canonical_name="Alpha FC",
             abbrev="ALP")
    a = Team(competition_slug="mls-2026", canonical_name="Beta FC",
             abbrev="BET")
    s.add_all([h, a])
    s.flush()
    ko = datetime.now(UTC) + timedelta(minutes=kickoff_minutes)
    fx = Fixture(competition_slug="mls-2026", espn_event_id="991001",
                 home_team_id=h.id, away_team_id=a.id,
                 original_kickoff_utc=ko, current_kickoff_utc=ko,
                 status="pre")
    s.add(fx)
    s.commit()
    return fx


def _absences_for(s, subject_ref, fixture_id):
    now = datetime.now(UTC)
    for i, (name, typ, reason) in enumerate((
            ("Star Striker", "Missing Fixture", "Knee Injury"),
            ("Key Midfielder", "Missing Fixture", "Suspended"),
            ("First Keeper", "Questionable", "Illness"))):
        s.add(TeamAbsence(
            provider="apifootball", subject_ref=str(subject_ref),
            fixture_id=fixture_id, competition="mls-2026",
            player_key=f"p{i}", provider_player_id=str(1000 + i),
            player_name=name, team_name="Alpha FC",
            provider_type=typ, provider_reason=reason,
            captured_at=now, first_seen_at=now, last_seen_at=now))
    s.commit()


class TestAbsencesCannotReachTheModelInput:

    def test_the_input_artifact_hash_is_identical_with_and_without_absences(
            self, live_session):
        """The model input is content-hashed. If an absence could enter it,
        the hash would move — so equality here is a proof about the bytes,
        not an inspection of the fields we remembered to check."""
        from tests.test_mls_shadow import TestPredictionRuns
        fx = TestPredictionRuns()._seed_playable(live_session)

        # A FIXED as_of. `current_model()` fits with as_of=now and the
        # artifact records it as `data_cutoff`, so two calls a millisecond
        # apart already hash differently — comparing those would fail for
        # a reason that has nothing to do with team news, and "fix it by
        # loosening the assertion" would then hide the real property. The
        # cutoff is pinned so the ONLY thing that varies is whether news
        # exists.
        as_of = datetime.now(UTC)

        def refit():
            s = live_session
            rows = model_mls._completed(s)
            return model_mls.fit(rows, as_of)

        model = refit()
        assert model is not None, "the fixture did not produce a fitted model"
        _d1, _c1, hash_before = model_mls.build_input_artifact(
            fx, model, "t10")

        ref = str(fx.espn_event_id)
        _absences_for(live_session, ref, fx.id)
        team_news.capture_events(ref, fixture_id=fx.id,
                                 competition="mls-2026",
                                 items=[{"time": {"elapsed": 61},
                                         "team": {"id": 1, "name": "Alpha FC"},
                                         "player": {"id": 9, "name": "Sent Off"},
                                         "type": "Card",
                                         "detail": "Red Card"}])
        team_news.capture_league_lineup(
            ref, fixture_id=fx.id, competition="mls-2026",
            items=[{"team": {"id": 1, "name": "Alpha FC"},
                    "formation": "4-3-3",
                    "startXI": [{"player": {"id": i, "name": f"P{i}"}}
                                for i in range(11)]}])
        assert live_session.query(TeamAbsence).count() == 3
        assert live_session.query(TeamNewsEvent).count() == 1
        assert live_session.query(TeamNewsLineup).count() >= 1

        # refit from scratch at the SAME cutoff: if any ratings path could
        # see news, a refit after the news landed is where it would show up
        model_after = refit()
        _d2, _c2, hash_after = model_mls.build_input_artifact(
            fx, model_after, "t10")
        assert hash_after == hash_before, (
            "the model's input bytes changed once team news existed — "
            "something on the model path is reading it")
        # and the fitted ratings themselves are unchanged, not merely the
        # serialised document
        assert model_after["ratings"] == model["ratings"]

    def test_the_canonical_lock_carries_no_trace_of_team_news(
            self, live_session, monkeypatch):
        """The real T-10 lock path, with news present for the fixture."""
        from src.live import lineups as lineups_mod
        from src.live import markets
        from src.live.models import ModelInputArtifact, PredictionRun
        # _seed_playable and _fake_snapshot live on TestPredictionRuns.
        # Reuse them rather than rebuild a lockable fixture: a second,
        # subtly different setup could pass while the real path differs.
        from tests.test_mls_shadow import TestPredictionRuns

        monkeypatch.setattr(config, "N_SIMULATIONS", 200)
        helper = TestPredictionRuns()
        up = helper._seed_playable(live_session)
        up.current_kickoff_utc = datetime.now(UTC) + timedelta(minutes=9)
        live_session.commit()

        _absences_for(live_session, str(up.espn_event_id or "991001"), up.id)
        assert live_session.query(TeamAbsence).count() == 3

        snap = helper._fake_snapshot(live_session, up.id)
        monkeypatch.setattr(markets, "capture_lock_snapshot",
                            lambda fixture_id: snap)
        import src.alerts as alerts
        monkeypatch.setattr(alerts, "send_alert", lambda msg, **kw: None)
        monkeypatch.setattr(
            lineups_mod, "capture_lineup",
            lambda fixture_id, **kw: {
                "snapshot_id": 77, "status": "pending",
                "quality": {"LINEUP_CONFIRMED": False,
                            "GOALKEEPER_CONFIRMED": False,
                            "AVAILABILITY_COMPLETE": False,
                            "PLAYER_DATA_FRESH": False}})
        assert runs.t10_locks()["locked"] == 1

        run = (live_session.query(PredictionRun)
               .filter_by(fixture_id=up.id, canonical=True).first())
        assert run is not None
        art = live_session.get(ModelInputArtifact,
                               run.model_input_artifact_id)
        blob = " ".join(str(x) for x in (
            art.document_json if art else "", run.input_quality_json or "",
            run.payload_json or ""))
        for leaked in ("Star Striker", "Key Midfielder", "Knee Injury",
                       "Missing Fixture", "team_absence", "team_news"):
            assert leaked not in blob, (
                f"{leaked!r} reached the canonical lock artifact")
        # and the honest proxy is still false rather than upgraded by news
        q = json.loads(run.input_quality_json)
        assert q["AVAILABILITY_COMPLETE"] is False, (
            "an absence feed must not flip AVAILABILITY_COMPLETE — that "
            "conflation was V9 eval F14")

    def test_no_prediction_run_column_references_a_team_news_table(self):
        """Schema-level: nothing on the run/lock tables points at news."""
        from src.live.models import PaperSignal, PredictionRun
        news_tables = {"team_absence", "team_news_capture",
                       "team_news_lineup", "team_news_event"}
        for model in (PredictionRun, PaperSignal):
            for col in model.__table__.columns:
                for fk in col.foreign_keys:
                    assert fk.column.table.name not in news_tables, (
                        f"{model.__tablename__}.{col.name} references "
                        f"{fk.column.table.name}")
