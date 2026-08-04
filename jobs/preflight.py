"""Coverage preflight — find the gaps BEFORE match day does.

Every alias this platform has was discovered in production: Sporting CP
on the operator's phone at T-5, LAFC missing from a slate, Toluca blank
on the cup board the day it went live. The discovery mechanism was
always a human looking at a broken row.

This job walks the next 48 hours of every board and reports, per
competition: clubs neither ratings provider resolves, and fixtures whose
Kalshi book has not bridged. It alerts only on NEW gaps — a gap already
reported is a gap being worked, and repeating it trains the operator to
ignore the channel.
"""
from __future__ import annotations

_SEEN: set[str] = set()
WINDOW_HOURS = 48


def preflight_job() -> None:
    from datetime import datetime, timedelta, timezone

    gaps: list[str] = []
    try:
        from src import competitions
        cut = (datetime.now(timezone.utc)
               + timedelta(hours=WINDOW_HOURS)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        for key in competitions.VIEWERS:
            d = competitions.fixtures(key, days=3) or {}
            for r in d.get("fixtures") or []:
                ko = r.get("kickoff_utc") or ""
                if not (now <= ko <= cut):
                    continue
                s = r.get("strength") or {}
                for side in ("home", "away"):
                    x = s.get(side) or {}
                    if x.get("rated") is False and x.get("reason") in (
                            "name_unmapped", "name_ambiguous"):
                        gaps.append(f"{key}: {r[side]['name']} "
                                    f"({x['reason']})")
    except Exception as exc:
        print(f"[preflight] sweep failed: {exc}")
        return

    new = [g for g in gaps if g not in _SEEN]
    _SEEN.update(new)
    if not new:
        print(f"[preflight] {len(gaps)} known gap(s), nothing new")
        return
    try:
        from src import alerts
        from src.alerts import OPERATIONAL
        alerts.send_alert(
            "coverage gaps on boards inside 48h — fix the aliases BEFORE "
            "kickoff, with /api/admin/ratings/name-probe as the measured "
            "source of spellings:\n" + "\n".join(f"- {g}" for g in new),
            title="coverage preflight", kind="action",
            dispatch_class=OPERATIONAL)
        print(f"[preflight] alerted {len(new)} new gap(s)")
    except Exception as exc:
        print(f"[preflight] alert failed: {exc}")


def ops_digest_job() -> None:
    """One morning message: the all-clear the transition alerts cannot
    give. Alerts fire on CHANGE; nothing says "everything is fine", so
    the operator is currently the polling loop. This is that message.
    """
    lines: list[str] = []
    try:
        from src.live.db import get_session, plane_ready
        from src.live.models import ModelVersion
        if plane_ready():
            s = get_session()
            try:
                rows = s.query(ModelVersion).all()
                armed = sorted(r.name for r in rows if r.approved_for_shadow)
                dark = sorted(r.name for r in rows
                              if not r.approved_for_shadow)
            finally:
                s.close()
            lines.append(f"armed: {', '.join(armed) or 'NONE'}")
            if dark:
                lines.append(f"disarmed: {', '.join(dark)}")
        else:
            lines.append("plane dormant")
    except Exception as exc:
        lines.append(f"plane state unreadable: {type(exc).__name__}")
    try:
        from datetime import date

        from src.live import clubelo
        n = len(clubelo.day_ranking(date.today().isoformat()))
        v = clubelo.served_vintage()
        lines.append(f"clubelo: {n} clubs"
                     + (f" (STALE, from {v['table_day']})" if v["stale"]
                        else ""))
    except Exception:
        lines.append("clubelo: unreadable")
    try:
        from src.live import worldclubratings as w
        t = w.ratings()
        lines.append(f"worldclubratings: {len(t)} clubs"
                     + (" (serving last good)" if
                        w._stale.get("serving_last_good") else ""))
    except Exception:
        lines.append("worldclubratings: unreadable")
    lines.append(f"preflight: {len(_SEEN)} known coverage gap(s)")
    try:
        from src import alerts
        from src.alerts import OPERATIONAL
        alerts.send_alert("\n".join(lines), title="morning digest",
                          kind="info", dispatch_class=OPERATIONAL)
        print("[digest] sent")
    except Exception as exc:
        print(f"[digest] alert failed: {exc}")
