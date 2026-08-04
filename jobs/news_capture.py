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

# BOOK-KEYED WATCHING. A match with a live Kalshi book is watched from
# the moment the book lists — days out — because news moves the price
# whenever it lands, not only near kickoff. But information density and
# provider budget both scale with proximity, so the cadence tiers:
#   > 24h to kickoff : swept every 6h
#   3h - 24h         : swept every 2h
#   < 3h             : every run (20 min), the original window
_LAST_SWEPT: dict[str, float] = {}
_ALERTED: set[tuple] = set()


def _tier_seconds(hours_to_ko: float) -> float:
    if hours_to_ko > 24: return 6 * 3600
    if hours_to_ko > 3: return 2 * 3600
    return 0.0


def _due(ref: str, hours_to_ko: float, now_mono: float) -> bool:
    gap = _tier_seconds(hours_to_ko)
    last = _LAST_SWEPT.get(ref)
    if last is not None and now_mono - last < gap:
        return False
    _LAST_SWEPT[ref] = now_mono
    return True


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
    # BOOKED fixtures are watched from listing, tiered by proximity —
    # a Kalshi book means money is moving on this match, so news for it
    # matters whenever it lands, not only inside the kickoff window.
    import time as _time
    booked: set[str] = set()
    try:
        from src import competitions
        from datetime import datetime as _dt
        mono = _time.monotonic()
        for key in competitions.VIEWERS:
            d = competitions.fixtures(key, days=8) or {}
            for r in d.get("fixtures") or []:
                fid = r.get("fixture_id")
                ko = r.get("kickoff_utc") or ""
                if not (fid and ko and r.get("kalshi_event")):
                    continue
                kod = _dt.fromisoformat(ko)
                if kod < now:
                    continue
                hrs = (kod - now).total_seconds() / 3600
                if _due(str(fid), hrs, mono):
                    refs.append((str(fid), None))
                    booked.add(str(fid))
    except Exception as exc:
        print(f"[news-capture] comp sweep failed: {exc}")

    # dedup by provider id: each capture spends a budgeted request, and
    # the same fixture must never be swept twice in one window
    seen_ids = set()
    refs = [r for r in refs
            if r[0] not in seen_ids and not seen_ids.add(r[0])]
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
    _alert_new_absences(booked)


def _alert_new_absences(booked: set[str]) -> None:
    """The intern yelling: a NEW absence on a match with a live book.

    Alerted once per (fixture, player), never re-alerted — repetition
    trains deafness. Reads first_seen_at, so an absence merely re-seen
    on a later sweep stays silent.
    """
    if not booked:
        return
    from datetime import datetime, timedelta, timezone
    try:
        from src.live.db import get_session
        from src.live.models import TeamAbsence
        cut = datetime.now(timezone.utc) - timedelta(minutes=30)
        s = get_session()
        try:
            rows = (s.query(TeamAbsence)
                    .filter(TeamAbsence.subject_ref.in_(booked),
                            TeamAbsence.first_seen_at >= cut).all())
            fresh = [(r.subject_ref, r.player_key, r.player_name,
                      r.provider_type, r.provider_reason) for r in rows]
        finally:
            s.close()
    except Exception as exc:
        print(f"[news-capture] absence read failed: {exc}")
        return
    new = [f for f in fresh if (f[0], f[1]) not in _ALERTED]
    _ALERTED.update((f[0], f[1]) for f in new)
    if not new:
        return
    try:
        from src import alerts
        from src.alerts import OPERATIONAL
        lines = [f"- {n or k}: {t or '?'} ({r or 'no reason given'}) "
                 f"[fixture {ref}]" for ref, k, n, t, r in new[:12]]
        alerts.send_alert(
            "NEW absence(s) on booked matches - the market reprices on "
            "these. Type and reason are the provider own words, stored "
            "verbatim, not our claim:\n" + "\n".join(lines),
            title="team news / booked match", kind="action",
            dispatch_class=OPERATIONAL)
        print(f"[news-capture] alerted {len(new)} new absence(s)")
    except Exception as exc:
        print(f"[news-capture] alert failed: {exc}")
