"""Live-plane database access. Dormant without LIVE_DATABASE_URL."""
from __future__ import annotations

import config

_engine = None
_Session = None


def live_enabled() -> bool:
    return bool(config.LIVE_DATABASE_URL)


def plane_ready() -> bool:
    """Enabled AND the boot migration/seed didn't fail. Shadow jobs gate
    on this so a boot-failed plane isn't hammered every interval."""
    return live_enabled() and not LIVE_BOOT_ERROR


def get_engine():
    """Lazy engine; never created while the plane is dormant."""
    global _engine
    if not live_enabled():
        return None
    if _engine is None:
        from sqlalchemy import create_engine
        _engine = create_engine(config.LIVE_DATABASE_URL,
                                pool_pre_ping=True, future=True)
    return _engine


def get_session():
    global _Session
    if _Session is None:
        eng = get_engine()
        if eng is None:
            return None
        from sqlalchemy.orm import sessionmaker
        _Session = sessionmaker(bind=eng, future=True)
    return _Session()


def migrations_current() -> bool | None:
    """True/False when the plane is active; None while dormant. The live
    service must FAIL STARTUP when migrations are behind — schema is
    never created or modified ad hoc during request handling."""
    eng = get_engine()
    if eng is None:
        return None
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory
    from sqlalchemy import text
    import os
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__),
                                     "..", "..", "alembic.ini"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    with eng.connect() as conn:
        try:
            row = conn.execute(
                text("SELECT version_num FROM alembic_version")).first()
        except Exception:
            return False                 # no version table = behind
    return bool(row and row[0] == head)


def startup_check() -> None:
    """Raise when the live plane is active but not migrated."""
    state = migrations_current()
    if state is False:
        raise RuntimeError(
            "live database migrations are behind — run "
            "`alembic upgrade head` before serving the live plane")


# Set when the boot-time migration/seed fails: the LIVE plane is then
# disabled for this process (its endpoints and readiness report the
# error), but the ARCHIVE plane keeps serving. The first version raised
# here and took the whole service down — a live-plane failure must
# never be able to kill the archive (plane isolation is the launch
# decision's own first principle).
LIVE_BOOT_ERROR: str | None = None


def migrate_and_seed() -> None:
    """Deploy-time migration + idempotent seed. Called ONCE from run.py
    before the server starts — deliberate and logged, never ad hoc from
    request handling. On failure the live plane DISABLES ITSELF (error
    recorded, /api/ready not-ready, no live serving) while the archive
    stays up."""
    global LIVE_BOOT_ERROR
    if not live_enabled():
        print("[live] LIVE_DATABASE_URL absent — live plane dormant")
        return
    import os
    import subprocess
    import sys
    import traceback
    try:
        root = os.path.join(os.path.dirname(__file__), "..", "..")
        r = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=root, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(
                f"alembic upgrade failed: {(r.stderr or r.stdout)[-800:]}")
        print("[live] migrations at head")
        from src.live.models import Competition
        s = get_session()
        try:
            if s.get(Competition, "mls-2026") is None:
                s.add(Competition(
                    slug="mls-2026", name="Major League Soccer",
                    provider_league_id=253, season=2026, timezone="UTC",
                    match_duration_minutes=90, supports_draw=True,
                    regular_time_only=True, has_group_stage=False,
                    has_knockout_stage=True,    # playoffs, later phase
                    model_version="mls-2026-v0"))
                s.commit()
                print("[live] seeded competition mls-2026")
            # --- EPL seed (additive block, 2026-07-28) ----------------
            if s.get(Competition, "epl-2026") is None:
                s.add(Competition(
                    slug="epl-2026", name="Premier League",
                    provider_league_id=39,      # API-Football's EPL id
                    season=2026, timezone="UTC",
                    match_duration_minutes=90, supports_draw=True,
                    regular_time_only=True, has_group_stage=False,
                    has_knockout_stage=False,   # pure league, no playoffs
                    model_version="epl-2026-v0"))
                s.commit()
                print("[live] seeded competition epl-2026")
            # --- end EPL seed -----------------------------------------
            # --- Liga MX seed (additive block, 2026-07-29) ------------
            # slug spans the ESPN season year (Apertura 2026 + Clausura
            # 2027 + both Liguillas) — the split-season identity
            # decision, documented in research_archive/
            # ligamx_RESEARCH_SUMMARY_2026-07-29.json.
            if s.get(Competition, "liga-mx-2026") is None:
                s.add(Competition(
                    slug="liga-mx-2026", name="Liga MX",
                    provider_league_id=262,     # API-Football's Liga MX id
                    season=2026, timezone="UTC",
                    match_duration_minutes=90, supports_draw=True,
                    regular_time_only=True, has_group_stage=False,
                    has_knockout_stage=True,    # Liguilla, per tournament
                    model_version="liga-mx-2026-v0"))
                s.commit()
                print("[live] seeded competition liga-mx-2026")
            # --- end Liga MX seed -------------------------------------
        finally:
            s.close()
    except Exception as exc:
        LIVE_BOOT_ERROR = f"{type(exc).__name__}: {exc}"
        print(f"[live] BOOT FAILED — live plane disabled, archive "
              f"unaffected:\n{traceback.format_exc()}")


def status() -> dict:
    """Externally-verifiable live-plane state for /api/ready."""
    if not live_enabled():
        return {"enabled": False}
    if LIVE_BOOT_ERROR:
        return {"enabled": True, "boot_failed": True,
                "error": LIVE_BOOT_ERROR[:500]}
    out = {"enabled": True, "connected": False,
           "migrations_current": False, "competition_seeded": False}
    try:
        out["migrations_current"] = bool(migrations_current())
        from src.live.models import Competition
        s = get_session()
        try:
            out["connected"] = True
            out["competition_seeded"] = (
                s.get(Competition, "mls-2026") is not None)
        finally:
            s.close()
    except Exception as exc:
        out["error"] = str(exc)[:200]
    return out
