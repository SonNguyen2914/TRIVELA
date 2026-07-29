"""Real-PostgreSQL integration tests (V8.1 evaluation Phase 4 / step 5).

Four clean production migrations are good operational evidence, but not
a substitute for automated validation. SQLite (the fast unit suite)
cannot prove: partial unique indexes, the exact migration DDL,
transaction isolation, concurrent canonical-lock creation, or psycopg
driver behavior. These do — against a real server.

Gated on PG_TEST_URL so the default suite stays SQLite-fast; CI sets it
to a postgres service container. Each test works in its own schema so
they neither collide nor need teardown between runs.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

PG_URL = os.getenv("PG_TEST_URL")
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="PG_TEST_URL not set (real-PostgreSQL tests)")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _normalize(url: str) -> str:
    import config
    return config._normalize_pg_url(url)


class _Scoped:
    """A per-test schema handle: its engine plus the scoped URL string,
    so a test can spin up FRESH engines (simulating restarts and
    concurrent workers) that all land in the same schema."""
    def __init__(self, engine, url):
        self.engine = engine
        self.url = url

    def new_engine(self):
        return create_engine(self.url, future=True)


@pytest.fixture()
def pg_schema():
    """A throwaway schema per test on the shared PG server, with the
    full Alembic migration chain applied inside it."""
    schema = "t_" + uuid.uuid4().hex[:12]
    url = _normalize(PG_URL)
    eng = create_engine(url, future=True)
    with eng.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    # migrate INSIDE the schema (search_path via the connection URL option)
    scoped = url + (
        "&" if "?" in url else "?") + f"options=-csearch_path%3D{schema}"
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"url={scoped}",
         "upgrade", "head"],
        cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    seng = create_engine(scoped, future=True)
    yield _Scoped(seng, scoped)
    seng.dispose()
    with eng.begin() as c:
        c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    eng.dispose()


def _session(engine):
    return sessionmaker(bind=engine, future=True)()


# 1 + 2. migration from empty produces the full schema at head
def test_migration_from_empty_creates_all_tables(pg_schema):
    with pg_schema.engine.connect() as c:
        head = c.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one()
        tables = {r[0] for r in c.execute(text(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = current_schema()"))}
    assert head  # at some head
    assert {"prediction_run", "market_quote", "market_snapshot",
            "model_input_artifact", "team_alias"} <= tables


# 5 + the reason migration #1 died: the PARTIAL unique index must
# actually enforce one canonical complete t10 per fixture ON POSTGRESQL
def test_partial_unique_index_enforced_on_postgres(pg_schema):
    from src.live.models import Competition, Fixture, PredictionRun
    s = _session(pg_schema.engine)
    try:
        s.add(Competition(slug="mls-2026", name="MLS", season=2026))
        s.add(Fixture(id=10, competition_slug="mls-2026",
                      espn_event_id="900"))
        s.commit()
        s.add(PredictionRun(id="r1", fixture_id=10, run_type="t10",
                            status="complete", canonical=True))
        s.commit()
        # non-canonical / non-complete siblings are free
        s.add(PredictionRun(id="r2", fixture_id=10, run_type="t10",
                            status="complete", canonical=False))
        s.add(PredictionRun(id="r3", fixture_id=10, run_type="t10",
                            status="failed", canonical=True))
        s.commit()
        # a SECOND canonical complete t10 must be rejected BY POSTGRES
        s.add(PredictionRun(id="r4", fixture_id=10, run_type="t10",
                            status="complete", canonical=True))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
    finally:
        s.close()


# 7. outcome-key uniqueness (the NULL-contract gap fix) on PostgreSQL
def test_outcome_key_uniqueness_on_postgres(pg_schema):
    from src.live.models import (Competition, Fixture, PredictionContract,
                                 PredictionRun)
    s = _session(pg_schema.engine)
    try:
        s.add(Competition(slug="mls-2026", name="MLS", season=2026))
        s.add(Fixture(id=11, competition_slug="mls-2026",
                      espn_event_id="901"))
        s.add(PredictionRun(id="run", fixture_id=11,
                            run_type="scheduled", status="complete"))
        s.commit()
        # two null-contract rows with the SAME outcome key — the old
        # UNIQUE(run, market_contract_id) let this through (NULLs
        # distinct); UNIQUE(run, outcome_key) must reject it
        s.add(PredictionContract(prediction_run_id="run",
                                 market_contract_id=None,
                                 outcome_key="home_win",
                                 raw_probability=0.5))
        s.commit()
        s.add(PredictionContract(prediction_run_id="run",
                                 market_contract_id=None,
                                 outcome_key="home_win",
                                 raw_probability=0.4))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
    finally:
        s.close()


# 6 + 9 + 10. snapshot-gated lock writes and reads back across a fresh
# engine (a "process restart")
def test_lock_evidence_roundtrips_across_restart(pg_schema):
    from src.live.models import (Competition, Fixture, MarketSnapshot,
                                 ModelInputArtifact, PredictionContract,
                                 PredictionRun)
    from datetime import datetime, timezone
    s = _session(pg_schema.engine)
    try:
        s.add(Competition(slug="mls-2026", name="MLS", season=2026))
        s.add(Fixture(id=12, competition_slug="mls-2026",
                      espn_event_id="902"))
        snap = MarketSnapshot(id=1, fixture_id=12,
                              captured_at=datetime.now(timezone.utc),
                              status="complete", policy_version="mls-lock-v1")
        art = ModelInputArtifact(id=1, schema_version="model-input-v1",
                                 content_hash="deadbeef",
                                 document_json='{"schema_version":"x"}')
        s.add_all([snap, art])
        s.commit()
        s.add(PredictionRun(id="lock", fixture_id=12, run_type="t10",
                            status="complete", canonical=True,
                            market_snapshot_id=1, model_input_artifact_id=1,
                            model_approved_at_run=True))
        s.flush()                       # parent before child, as _write_run does
        s.add(PredictionContract(prediction_run_id="lock",
                                 outcome_key="home_win",
                                 raw_probability=0.5))
        s.commit()
    finally:
        s.close()
    # a NEW engine = a restarted process reading the durable rows
    fresh = pg_schema.new_engine()
    s2 = _session(fresh)
    try:
        run = s2.get(PredictionRun, "lock")
        assert run.canonical and run.status == "complete"
        assert run.market_snapshot_id == 1
        assert run.model_input_artifact_id == 1
        assert run.model_approved_at_run is True
    finally:
        s2.close()
        fresh.dispose()


# 6 (concurrency). two concurrent transactions both attempting the
# canonical lock — exactly one may win on PostgreSQL
def test_concurrent_canonical_lock_only_one_wins(pg_schema):
    from src.live.models import Competition, Fixture, PredictionRun
    setup = _session(pg_schema.engine)
    try:
        setup.add(Competition(slug="mls-2026", name="MLS", season=2026))
        setup.add(Fixture(id=13, competition_slug="mls-2026",
                          espn_event_id="903"))
        setup.commit()
    finally:
        setup.close()
    e1 = pg_schema.new_engine()
    e2 = pg_schema.new_engine()
    s1, s2 = _session(e1), _session(e2)
    try:
        # worker 1 inserts and HOLDS the transaction open (the unique
        # index slot is now taken but uncommitted)
        s1.add(PredictionRun(id="c1", fixture_id=13, run_type="t10",
                             status="complete", canonical=True))
        s1.flush()
        # worker 2 tries the same slot. Postgres BLOCKS it (correct
        # semantics — it waits to see if worker 1 commits). A short
        # lock_timeout turns that block into an observable error instead
        # of a hang; without one this would deadlock the test forever.
        s2.execute(text("SET lock_timeout = '2s'"))
        s2.add(PredictionRun(id="c2", fixture_id=13, run_type="t10",
                             status="complete", canonical=True))
        with pytest.raises(Exception):  # blocked -> timeout, then abort
            s2.flush()
        s2.rollback()
        # worker 1 wins
        s1.commit()
        chk = _session(e1)
        n = (chk.query(PredictionRun)
             .filter_by(fixture_id=13, canonical=True,
                        status="complete").count())
        chk.close()
        assert n == 1
    finally:
        s1.close()
        s2.close()
        e1.dispose()
        e2.dispose()


# 7. the defect that destroyed the first prospective slate's fill record.
# SQLite ignores VARCHAR(n); PostgreSQL raises StringDataRightTruncation.
# FEE_POLICY["version"] reached 26 chars against a String(24) column, so
# EVERY paper_fill INSERT failed in production while the suite stayed
# green. Only a real PostgreSQL write can prove the fix.
def test_paper_fill_accepts_the_real_policy_constants(pg_schema):
    from src.live.models import (Competition, Fixture, PaperFill,
                                 PaperSignal, PredictionRun)
    from src.live.paper import EXEC_POLICY, FEE_POLICY
    s = _session(pg_schema.engine)
    try:
        # the ledger's FKs are real (V9 eval F5), so the row needs a run
        s.add(Competition(slug="mls-2026", name="MLS", season=2026))
        s.add(Fixture(id=71, competition_slug="mls-2026",
                      espn_event_id="971"))
        s.add(PredictionRun(id="r1", fixture_id=71, run_type="t10",
                            status="complete"))
        s.flush()
        s.add(PaperSignal(id=1, prediction_run_id="r1",
                          policy_version=EXEC_POLICY["version"],
                          decision="fill"))
        s.flush()
        s.add(PaperFill(paper_signal_id=1, requested_contracts=100,
                        filled_contracts=100,
                        execution_class="top_of_book_estimate",
                        fee_policy_version=FEE_POLICY["version"],
                        status="open"))
        s.commit()               # this is the statement that used to die
        row = s.query(PaperFill).one()
        # stored WHOLE, not silently truncated to the old cap
        assert row.fee_policy_version == FEE_POLICY["version"]
        assert row.execution_class == "top_of_book_estimate"
    finally:
        s.close()


# and the general invariant, enforced by the database itself: every
# versioned constant must fit the column it is persisted in
def test_every_versioned_constant_round_trips_on_postgres(pg_schema):
    from sqlalchemy import String

    from src.live import corpus, lineups, markets, model_eval, model_mls
    from src.live import paper, risk
    consts = [paper.FEE_POLICY["version"], paper.EXEC_POLICY["version"],
              markets.LOCK_POLICY_VERSION, markets.PROVIDER_SCHEMA_VERSION,
              model_mls.INPUT_ARTIFACT_SCHEMA, corpus.CORPUS_SCHEMA,
              model_eval.EVAL_VERSION, model_eval.APPROVAL_POLICY_VERSION,
              lineups.PARSER_VERSION, risk.RISK_POLICY["version"]]
    longest = max(len(c) for c in consts)
    with pg_schema.engine.connect() as c:
        rows = c.execute(text(
            "SELECT table_name, column_name, character_maximum_length "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND (column_name LIKE '%%version' OR column_name LIKE '%%schema')"
        )).all()
    tight = [(t, col, n) for t, col, n in rows
             if n is not None and n < longest]
    assert not tight, (
        f"columns narrower than the longest versioned constant "
        f"({longest} chars): {tight}")


# 8. hunter tables: created by the migration chain, and a finding row
# with REAL provider tickers + the real fee-policy string round-trips
# under PostgreSQL's VARCHAR enforcement (the truncation class that
# silently erased the first slate's fills).
def test_hunter_tables_round_trip_on_postgres(pg_schema):
    from datetime import datetime, timezone

    from src.live.models import HunterCycle, HunterFinding
    from src.live.paper import FEE_POLICY
    with pg_schema.engine.connect() as c:
        tables = {r[0] for r in c.execute(text(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = current_schema()"))}
    assert {"hunter_finding", "hunter_cycle"} <= tables
    now = datetime.now(timezone.utc)
    s = _session(pg_schema.engine)
    try:
        s.add(HunterFinding(
            series="KXCONMEBOLLIBGAME",
            event_ticker="KXCONMEBOLLIBGAME-26JUL30CARSFE",
            market_ticker="KXCONMEBOLLIBGAME-26JUL30CARSFE-CAR",
            finding_type="SUM_BELOW_ONE", is_context=False,
            finding_key="SUM_BELOW_ONE|KXCONMEBOLLIBGAME|"
                        "KXCONMEBOLLIBGAME-26JUL30CARSFE|"
                        "KXCONMEBOLLIBGAME-26JUL30CARSFE-CAR",
            legs_json="{}", net_margin_dollars="0.0353",
            fee_policy_version=FEE_POLICY["version"],
            first_captured_at=now, last_seen_at=now, status="open"))
        s.add(HunterCycle(started_at=now, completed_at=now,
                          status="complete", markets_scanned=3,
                          series_scanned=1, series_failed=0,
                          series_outcomes_json='{"KXCONMEBOLLIBGAME": '
                                               '{"outcome": "SUCCESS"}}'))
        s.commit()
        row = s.query(HunterFinding).one()
        assert row.fee_policy_version == FEE_POLICY["version"]
        assert row.net_margin_dollars == "0.0353"
    finally:
        s.close()


# 9. P1-5: cross-process idempotency under a REAL race. Railway keeps
# the old container serving while the new one boots, so two hunter
# processes legitimately overlap. Barriered double-insert of the same
# open finding_key must yield ONE open row (partial unique index), and
# the barriered alert claim must have at most ONE winner (atomic
# UPDATE ... WHERE on the row).
def test_hunter_concurrent_insert_and_alert_claim(pg_schema):
    import threading
    from datetime import datetime, timezone

    from src.live import hunter
    from src.live.models import HunterFinding

    now = datetime.now(timezone.utc)
    f = {"finding_type": "SUM_BELOW_ONE", "series": "KXUCLGAME",
         "event_ticker": "KXUCLGAME-26JUL28AAABBB",
         "market_ticker": None, "is_context": False,
         "net_margin_dollars": "0.0353", "captured_at": now,
         "detected_at": now, "legs": {"rule": "race-test"}}
    key = hunter._finding_key(f)

    engines = [pg_schema.new_engine() for _ in range(2)]
    barrier = threading.Barrier(2, timeout=20)
    results: dict = {}

    def worker(i):
        from sqlalchemy.exc import IntegrityError
        s = _session(engines[i])
        try:
            barrier.wait()
            try:
                hunter._insert_finding_row(s, f, key, now)
                s.commit()      # loser BLOCKS here until the winner
                #                 commits, then gets the unique violation
                results[f"insert{i}"] = True
            except IntegrityError:
                s.rollback()    # the retry-pass contract: adopt, don't
                #                 create
                results[f"insert{i}"] = False
            barrier.wait()
            rid = (s.query(HunterFinding)
                   .filter_by(finding_key=key, status="open")
                   .one().id)
            barrier.wait()
            results[f"claim{i}"] = hunter._try_claim_alert(s, rid, now)
        except Exception as exc:            # surface, never hang
            results[f"error{i}"] = repr(exc)
        finally:
            s.close()

    threads = [threading.Thread(target=worker, args=(i,))
               for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    for e in engines:
        e.dispose()
    assert not [k for k in results if k.startswith("error")], results
    # exactly one insert won; exactly one open row exists
    assert sorted([results["insert0"], results["insert1"]]) \
        == [False, True]
    chk = _session(pg_schema.engine)
    try:
        assert (chk.query(HunterFinding)
                .filter_by(finding_key=key, status="open").count() == 1)
    finally:
        chk.close()
    # at most one alert claim won (exactly one, since none existed)
    assert sorted([results["claim0"], results["claim1"]]) \
        == [False, True]
