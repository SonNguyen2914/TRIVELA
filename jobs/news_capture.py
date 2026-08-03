"""The team-news capture job — deliberately OUTSIDE jobs/scheduler.py.

WHY THIS MODULE EXISTS. `capture_absences` sat uncalled for weeks (zero
announced XI across all 15 slate fixtures on 2026-07-31) because the
first wiring attempt (#53) imported team_news inside jobs/scheduler.py —
and scheduler.py is in LOCK_PATH_MODULES: nothing that writes forecast
evidence may NAME team news, because lineup features were measured
negative-or-marginal and switched off. The guard was right; the wiring
was wrong.

THE BOUNDARY, STATED PRECISELY. The isolation exists to keep team news
OUT OF FORECAST EVIDENCE, not out of the database. This module:
  - imports team_news and writes ONLY its display tables;
  - imports NOTHING from the model, runs, paper, simulator or eval
    modules — pinned by test, both directions;
  - is registered on the scheduler as an opaque callable, so
    scheduler.py names `news_capture`, never `team_news`.
An unattended module that routed captures INTO a lock would be the
violation; one that fills a display surface is the reason the capture
code was written.
"""
from __future__ import annotations

TEAM_NEWS_WINDOW_HOURS = 3.0


def capture_window_job() -> None:
    """Capture absences for fixtures kicking off soon.

    Windowed, not a full sweep: absences matter close to kickoff, each
    capture spends a budgeted provider request, and a fixture is seen ~9
    times as its news firms up — an XI at T-60 replaces a projection
    made at T-180.
    """
    from datetime import datetime, timedelta, timezone

    try:
        from src.live import team_news
        from src.live.db import get_session
        from src.live.models import Fixture
    except Exception as exc:
        print(f"[news-capture] import failed: {exc}")
        return
    try:
        if not team_news.plane_ready():
            print("[news-capture] plane dormant, skipping")
            return
        now = datetime.now(timezone.utc)
        cut = now + timedelta(hours=TEAM_NEWS_WINDOW_HOURS)
        s = get_session()
        try:
            rows = (s.query(Fixture)
                    .filter(Fixture.current_kickoff_utc >= now,
                            Fixture.current_kickoff_utc <= cut).all())
            # the API-Football id, NOT ESPN's: the injuries endpoint is
            # queried by `fixture`, so an ESPN id returns nothing forever
            refs = [(r.provider_fixture_id, r.id) for r in rows
                    if r.provider_fixture_id]
        finally:
            s.close()
    except Exception as exc:
        print(f"[news-capture] fixture read failed: {exc}")
        return
    if not refs:
        print("[news-capture] no fixtures in the window")
        return
    ok = failed = 0
    for ref, fid in refs:
        try:
            r = team_news.capture_absences(ref, fixture_id=fid)
            # dormant/empty is a FAILURE, not a capture — a dead feed
            # reporting healthy is how this went unnoticed for weeks
            good = (r or {}).get("stored") is not None
            ok += 1 if good else 0
            failed += 0 if good else 1
        except Exception as exc:
            failed += 1
            print(f"[news-capture] {ref}: {type(exc).__name__}: {str(exc)[:90]}")
    print(f"[news-capture] window {TEAM_NEWS_WINDOW_HOURS}h: "
          f"{len(refs)} fixtures, {ok} captured, {failed} failed")
