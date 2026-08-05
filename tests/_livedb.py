"""Engine parameterization for the live-plane test fixtures (TASK-11).

Eight fixtures across the plane suites point config.LIVE_DATABASE_URL
at a throwaway sqlite file. Setting PG_TEST_URL alone would NOT move
them to PostgreSQL — they monkeypatch the URL after config loads, so an
env-only change false-greens: CI would say "postgres" while every test
quietly kept running SQLite. That refusal (TASK-5) is why this module
exists.

`provision()` is the one place the choice happens:

  - PG_TEST_URL set   -> a throwaway schema on that server, per test,
                         dropped afterwards. The REAL engine enforces
                         what SQLite only simulates.
  - PG_TEST_URL unset -> the sqlite file, exactly as before.

Two commitments the rest of the suite can rely on:

  VISIBLE LEGS. Every provision is counted by engine, and conftest
  prints the tally at the end of the run — a PG run that silently fell
  back to SQLite would show `postgresql=0`, which is the opposite of a
  silent skip.

  THE VARCHAR SIMULATION STAYS SQLITE-ONLY. The suites attach a
  before_flush guard that imitates PostgreSQL's
  StringDataRightTruncation because SQLite ignores String lengths. On
  the PG leg the imitation must NOT run: the point of the leg is that
  the real engine enforces the real constraint, and a simulation layered
  on top would mask a divergence between the two. Fixtures gate the
  listener on SIMULATE_VARCHAR.
"""
from __future__ import annotations

import os
import uuid

PG_URL = os.getenv("PG_TEST_URL")

# The sqlite-only varchar imitation runs exactly when the real engine
# will not enforce the real thing.
SIMULATE_VARCHAR = not PG_URL

# engine name -> provisions this run; printed by conftest at the end.
LEGS = {"sqlite": 0, "postgresql": 0}


def _pg_scoped_url() -> tuple[str, str]:
    """(scoped_url, schema). A fresh schema on the PG_TEST_URL server,
    selected via search_path in the URL options — the same mechanism
    tests/test_postgres_integration.py has used against CI's service
    since V8.1."""
    import config
    from sqlalchemy import create_engine, text
    schema = "lv_" + uuid.uuid4().hex[:12]
    base = config._normalize_pg_url(PG_URL)
    eng = create_engine(base, future=True)
    with eng.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    eng.dispose()
    scoped = base + ("&" if "?" in base else "?") \
        + f"options=-csearch_path%3D{schema}"
    return scoped, schema


def provision(tmp_path, monkeypatch):
    """Point the live plane at a throwaway database and return
    (url, teardown). The fixture calls teardown() AFTER closing its
    session — on the PG leg the schema is dropped, and an engine still
    holding connections would make that drop hang, so teardown disposes
    the plane's engine first."""
    import config
    from src.live import db as live_db

    if PG_URL:
        url, schema = _pg_scoped_url()
        LEGS["postgresql"] += 1
    else:
        url, schema = f"sqlite:///{tmp_path}/live.db", None
        LEGS["sqlite"] += 1

    monkeypatch.setattr(config, "LIVE_DATABASE_URL", url)
    monkeypatch.setattr(live_db, "_engine", None)
    monkeypatch.setattr(live_db, "_Session", None)
    monkeypatch.setattr(live_db, "LIVE_BOOT_ERROR", None)

    def teardown():
        eng = live_db._engine
        if eng is not None:
            eng.dispose()
        if schema is not None:
            from sqlalchemy import create_engine, text
            base = config._normalize_pg_url(PG_URL)
            e = create_engine(base, future=True)
            # Restart-simulating tests build their OWN engines from the
            # URL; disposing the plane's engine cannot reach those, and
            # one idle-in-transaction connection blocks DROP SCHEMA
            # forever (observed: a 15-minute hang on the first full
            # leg). The database is a throwaway test server, so the
            # honest fix is: bounded wait, then terminate the stragglers
            # by name and retry once. lock_timeout makes the wait
            # visible instead of eternal.
            try:
                with e.begin() as c:
                    c.execute(text("SET lock_timeout = '3s'"))
                    c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            except Exception:
                with e.begin() as c:
                    c.execute(text(
                        "SELECT pg_terminate_backend(pid) "
                        "FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid() "
                        "AND state LIKE 'idle in transaction%'"))
                with e.begin() as c:
                    c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            e.dispose()

    return url, teardown
