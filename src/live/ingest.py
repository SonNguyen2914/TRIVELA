"""Fixture + result ingestion for the live plane (launch decision O4).

Sources: ESPN keyless (the season's full schedule via per-team schedule
endpoints; the rolling window via dated scoreboards). Every ingest run:
  - upserts Fixture rows keyed by (competition, espn_event_id);
  - records kickoff/status changes as FixtureChange HISTORY rows
    (reschedules never silently overwrite provenance);
  - stores content-hashed SourceObservation rows for raw payloads;
  - fills final scores when a fixture completes (the settlement +
    backtest substrate).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import requests

import config
from src.live.db import get_session, plane_ready
from src.live import identity
from src.live.models import Fixture, FixtureChange, SourceObservation

SCHEDULE_URL = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
                "{league}/teams/{espn_id}/schedule")
SCOREBOARD_URL = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
                  "{league}/scoreboard")
# competition-keyed since the EPL build (2026-07-28): every function
# below takes (competition_slug, espn_league) with MLS defaults, so the
# MLS call sites and their behaviour are unchanged.


def _now():
    return datetime.now(timezone.utc)


def _parse_dt(iso: str | None):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def _observe(s, endpoint: str, payload: dict) -> int:
    raw = json.dumps(payload, sort_keys=True)
    from src.live.evidence import pack_payload
    obs = SourceObservation(
        source="espn", endpoint=endpoint,
        observed_at=_now(), **pack_payload(raw))
    s.add(obs)
    s.flush()
    return obs.id


def _event_to_fields(ev: dict) -> dict | None:
    """One ESPN schedule/scoreboard event -> fixture field dict."""
    comp = (ev.get("competitions") or [{}])[0]
    sides = {}
    for c in comp.get("competitors") or []:
        team_name = (c.get("team") or {}).get("displayName")
        score = c.get("score")
        if isinstance(score, dict):          # schedule endpoint shape
            score = score.get("value")
        sides[c.get("homeAway")] = (team_name, score)
    if "home" not in sides or "away" not in sides:
        return None
    status = (((ev.get("status") or comp.get("status")) or {})
              .get("type") or {})
    state = status.get("state")
    home_goals = away_goals = None
    if state == "post":
        try:
            home_goals = int(float(sides["home"][1]))
            away_goals = int(float(sides["away"][1]))
        except (TypeError, ValueError):
            pass
    return {
        "espn_event_id": str(ev.get("id")),
        "kickoff": _parse_dt(ev.get("date")),
        "home_name": sides["home"][0],
        "away_name": sides["away"][0],
        "status": state,
        "home_goals": home_goals,
        "away_goals": away_goals,
        "venue": ((comp.get("venue") or {}).get("fullName")
                  if comp.get("venue") else None),
    }


def _upsert_fixture(s, f: dict, observed_at,
                    competition_slug: str = "mls-2026") -> tuple[bool, bool]:
    """Insert or update one fixture; returns (created, changed)."""
    row = (s.query(Fixture)
           .filter_by(competition_slug=competition_slug,
                      espn_event_id=f["espn_event_id"]).first())
    home = (identity.resolve_espn_name(f["home_name"],
                                       competition_slug=competition_slug)
            if f["home_name"] else None)
    away = (identity.resolve_espn_name(f["away_name"],
                                       competition_slug=competition_slug)
            if f["away_name"] else None)
    if row is None:
        s.add(Fixture(
            competition_slug=competition_slug,
            espn_event_id=f["espn_event_id"],
            home_team_id=home.id if home else None,
            away_team_id=away.id if away else None,
            original_kickoff_utc=f["kickoff"],
            current_kickoff_utc=f["kickoff"],
            venue=f["venue"], status=f["status"],
            home_goals=f["home_goals"], away_goals=f["away_goals"],
            observed_at=observed_at))
        return True, False
    changed = False
    # reschedule/status transitions create HISTORY, then update
    if f["kickoff"] and row.current_kickoff_utc:
        old_k = row.current_kickoff_utc
        if old_k.tzinfo is None:
            old_k = old_k.replace(tzinfo=timezone.utc)
        if abs((old_k - f["kickoff"]).total_seconds()) > 60:
            s.add(FixtureChange(fixture_id=row.id, field="kickoff",
                                old_value=old_k.isoformat(),
                                new_value=f["kickoff"].isoformat(),
                                observed_at=observed_at))
            row.current_kickoff_utc = f["kickoff"]
            changed = True
    if f["status"] and f["status"] != row.status:
        s.add(FixtureChange(fixture_id=row.id, field="status",
                            old_value=row.status, new_value=f["status"],
                            observed_at=observed_at))
        row.status = f["status"]
        changed = True
    if f["home_goals"] is not None and row.home_goals is None:
        row.home_goals = f["home_goals"]
        row.away_goals = f["away_goals"]
        changed = True
    if home and row.home_team_id is None:
        row.home_team_id = home.id
    if away and row.away_team_id is None:
        row.away_team_id = away.id
    row.observed_at = observed_at
    return False, changed


def _event_season_year(ev: dict) -> int | None:
    """The season an ESPN schedule EVENT belongs to.

    Verified 2026-07-29 against eng.1 team 359
    (research_archive/epl/espn_team_schedule_359_*.json): the request's
    `season` parameter IS honoured for the events, but the payload's
    TOP-LEVEL `season` block reports the CURRENT season regardless —
    `?season=2025` returned 2025-26 events under a top-level block still
    saying "2026-27 English Premier League". So the per-EVENT block is
    the only field that means what a season assertion needs it to mean.
    """
    y = ((ev.get("season") or {}).get("year"))
    try:
        return int(y)
    except (TypeError, ValueError):
        return None


def ingest_season_schedules(competition_slug: str = "mls-2026",
                            espn_league: str = "usa.1",
                            expected_season_year: int | None = None,
                            expected_season_label: str | None = None
                            ) -> dict:
    """Full-season ingest via each club's schedule endpoint (one call per
    club) — fixtures past AND future, with final scores. The history
    substrate for ratings, backtests, and settlement.

    When `expected_season_year` is given the request is PINNED to it and
    every event is VALIDATED against it: an event from another season is
    refused, counted, and reported as an explicit `error`, never
    ingested. Without the pin the provider's idea of "current" silently
    decides which season the ratings are fitted on — and ESPN's own
    top-level season block cannot be used for the check (see
    `_event_season_year`)."""
    if not plane_ready():
        return {"skipped": "dormant"}
    from src.live.models import Team
    s = get_session()
    created = updated = 0
    rejected: dict[str, int] = {}
    try:
        teams = s.query(Team).filter_by(
            competition_slug=competition_slug).all()
        for t in teams:
            if not t.espn_id:
                continue
            # ESPN splits the season: the bare endpoint returns PLAYED
            # games only; fixture=true returns the UPCOMING ones
            # (verified live Jul 23 — 16 played + 18 upcoming for CLB).
            # Both halves are needed: history feeds the model, the
            # future feeds the slate.
            for params in ({}, {"fixture": "true"}):
                if expected_season_year is not None:
                    params = dict(params, season=str(expected_season_year))
                try:
                    r = requests.get(
                        SCHEDULE_URL.format(league=espn_league,
                                            espn_id=t.espn_id),
                        params=params, timeout=15)
                    r.raise_for_status()
                    payload = r.json()
                except requests.RequestException as exc:
                    print(f"[ingest] schedule {t.canonical_name}: {exc}")
                    continue
                now = _now()
                _observe(s, f"teams/{t.espn_id}/schedule"
                            + ("?fixture=true" if params.get("fixture")
                               else "")
                            + (f"&season={expected_season_year}"
                               if expected_season_year is not None else ""),
                         payload)
                for ev in payload.get("events") or []:
                    if expected_season_year is not None:
                        got = _event_season_year(ev)
                        if got != expected_season_year:
                            key = ("season_unknown" if got is None
                                   else f"season_{got}")
                            rejected[key] = rejected.get(key, 0) + 1
                            continue
                        label = (ev.get("season") or {}).get("displayName")
                        if (expected_season_label and label
                                and expected_season_label not in label):
                            rejected["season_label_mismatch"] = rejected.get(
                                "season_label_mismatch", 0) + 1
                            continue
                    f = _event_to_fields(ev)
                    if not f:
                        continue
                    c, ch = _upsert_fixture(s, f, now,
                                            competition_slug=competition_slug)
                    created += int(c)
                    updated += int(ch)
                s.commit()
        total = s.query(Fixture).filter_by(
            competition_slug=competition_slug).count()
        out = {"fixtures": total, "created": created, "updated": updated,
               "season_pinned": expected_season_year,
               "season_label": expected_season_label}
        if rejected:
            # an explicit FAILURE, not a footnote: fixtures from another
            # season would silently become training data for this one
            out["season_rejected"] = rejected
            out["error"] = (
                f"provider season mismatch for {competition_slug}: "
                f"expected {expected_season_label or expected_season_year}, "
                f"refused {sum(rejected.values())} event(s) {rejected}")
            print(f"[ingest] {out['error']}")
        return out
    except Exception as exc:
        s.rollback()
        print(f"[ingest] season ingest failed: {exc}")
        return {"error": str(exc)[:200]}
    finally:
        s.close()


def refresh_window(competition_slug: str = "mls-2026",
                   espn_league: str = "usa.1") -> dict:
    """Rolling-window refresh (past 7d + next 14d) via dated scoreboards
    — cheap, run on a schedule; catches reschedules, statuses, scores."""
    if not plane_ready():
        return {"skipped": "dormant"}
    from datetime import timedelta
    s = get_session()
    created = updated = 0
    try:
        for delta in range(-7, 15):
            day = (_now() + timedelta(days=delta)).strftime("%Y%m%d")
            try:
                r = requests.get(
                    SCOREBOARD_URL.format(league=espn_league),
                    params={"dates": day}, timeout=15)
                r.raise_for_status()
                payload = r.json()
            except requests.RequestException:
                continue
            now = _now()
            for ev in payload.get("events") or []:
                f = _event_to_fields(ev)
                if not f:
                    continue
                c, ch = _upsert_fixture(s, f, now,
                                        competition_slug=competition_slug)
                created += int(c)
                updated += int(ch)
            s.commit()
        return {"created": created, "updated": updated}
    except Exception as exc:
        s.rollback()
        print(f"[ingest] window refresh failed: {exc}")
        return {"error": str(exc)[:200]}
    finally:
        s.close()
