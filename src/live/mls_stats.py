"""Official MLS (Sportec/StatsPerform) per-match stats ingestion.

The goals-only model (mls-2026-v0) can't see HOW a result was earned.
The public MLS stats API carries the provider's own expected goals and
full shot volume per team, per match — the signal that stabilizes a
half-season of noisy scorelines — plus per-match player rows (xG,
minutes, goalkeeper flag) for later player/GK features.

This module is the ingestion source, mirroring ingest.py's discipline:
  - raw responses are content-hashed into SourceObservation
    (source='mls_stats') — the bottom of the evidence chain;
  - a stats match attaches to OUR fixture by the two clubs' resolved
    team ids + kickoff date (Sportec three_letter_code == our abbrev,
    verified 1:1); a match we can't map is skipped, never guessed;
  - per-(fixture, side) team stats and per-(fixture, player) rows are
    UPSERTED — a re-ingest refreshes, never duplicates;
  - the provider is throttled and only completed matches are fetched.

PURELY ADDITIVE: writing these rows changes no model output. A feature
built on them ships only after it is MEASURED to beat the current model
(model_eval ladder). Money stays LOCKED.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

import requests

from src.live import identity
import config
from src.live.db import get_session, plane_ready
from src.live.models import (Fixture, MlsPlayerMatchStat, MlsTeamMatchStat,
                             SourceObservation)

BASE = "https://stats-api.mlssoccer.com"
SEASON_ID = "MLS-SEA-0001KA"          # 2026 regular season
COMPETITION_ID = "MLS-COM-000001"
# a courteous request identity + throttle: this is the API the MLS site
# itself serves, fine for personal shadow reads, but we don't hammer it
_HEADERS = {"Referer": "https://www.mlssoccer.com/",
            "User-Agent": "wc26-shadow-research/1.0 (personal; throttled)"}
THROTTLE_SECONDS = 0.4
COMPLETED_STATUSES = {"finalwhistle", "fulltime", "afterextratime",
                      "afterpenalties", "final"}


def _now():
    return datetime.now(timezone.utc)


def _parse_dt(iso: str | None):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _get(path: str, params: dict | None = None,
         quiet_404: bool = False) -> dict | None:
    try:
        r = requests.get(f"{BASE}/{path}", params=params or {},
                         headers=_HEADERS, timeout=20)
        # the match-list endpoint 404s on a window with NO matches (a
        # fixture gap / international break) — benign, not a failure
        if quiet_404 and r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[mls_stats] GET {path} failed: {exc}")
        return None


def _observe(s, endpoint: str, params: dict, payload) -> int:
    raw = json.dumps(payload, sort_keys=True)
    from src.live.evidence import pack_payload
    obs = SourceObservation(
        source="mls_stats", endpoint=endpoint,
        params_json=json.dumps(params, sort_keys=True),
        observed_at=_now(), **pack_payload(raw))
    s.add(obs)
    s.flush()
    return obs.id


def _is_completed(status: str | None) -> bool:
    return bool(status) and status.replace(" ", "").lower() in \
        COMPLETED_STATUSES


# --- match list -----------------------------------------------------------

def season_matches(gte: str, lte: str,
                   per_page: int = 500) -> list[dict]:
    """Completed matches in [gte, lte] (YYYY-MM-DD), each normalized to the
    fields ingestion needs. Paginates defensively via next_page_token."""
    out: list[dict] = []
    token = None
    for _ in range(20):                      # hard page cap
        params = {"competition_id": COMPETITION_ID, "per_page": per_page,
                  "match_date[gte]": gte, "match_date[lte]": lte}
        if token:
            params["page_token"] = token
        payload = _get(f"matches/seasons/{SEASON_ID}", params,
                       quiet_404=True)
        if not payload:
            break
        for m in payload.get("schedule") or []:
            if not _is_completed(m.get("match_status")):
                continue
            out.append({
                "match_id": m.get("match_id"),
                "kickoff": _parse_dt(m.get("planned_kickoff_time")
                                     or m.get("kick_off")),
                "home_code": m.get("home_team_three_letter_code"),
                "away_code": m.get("away_team_three_letter_code"),
                "home_cid": m.get("home_team_id"),
                "away_cid": m.get("away_team_id"),
                "home_name": m.get("home_team_name"),
                "away_name": m.get("away_team_name"),
                "home_goals": m.get("home_team_goals"),
                "away_goals": m.get("away_team_goals"),
            })
        token = payload.get("next_page_token")
        if not token:
            break
        time.sleep(THROTTLE_SECONDS)
    return out


def observed_clubs(matches: list[dict]) -> list[dict]:
    """Distinct clubs seen in a match list, for provenance alias seeding."""
    seen: dict[str, dict] = {}
    for m in matches:
        for pre in ("home", "away"):
            code, cid, name = (m[f"{pre}_code"], m[f"{pre}_cid"],
                               m[f"{pre}_name"])
            if cid and cid not in seen:
                seen[cid] = {"sportec_id": cid, "code": code, "name": name}
    return list(seen.values())


# --- fixture matching -----------------------------------------------------

def _find_fixture(s, ta_id: int, tb_id: int, kickoff):
    """Our fixture between these two teams (either orientation), nearest by
    kickoff. The team PAIR plus a date within 3 days uniquely identifies a
    meeting — the two season meetings are months apart."""
    from sqlalchemy import and_, or_
    cands = (s.query(Fixture)
             .filter_by(competition_slug="mls-2026")
             .filter(or_(
                 and_(Fixture.home_team_id == ta_id,
                      Fixture.away_team_id == tb_id),
                 and_(Fixture.home_team_id == tb_id,
                      Fixture.away_team_id == ta_id))).all())
    if not cands:
        return None
    if kickoff is None:
        return cands[0] if len(cands) == 1 else None

    def diff(f):
        k = f.current_kickoff_utc
        if k is None:
            return 10 ** 9
        if k.tzinfo is None:
            k = k.replace(tzinfo=timezone.utc)
        return abs((k - kickoff).total_seconds())

    best = min(cands, key=diff)
    return best if diff(best) < 3 * 86400 else None


# --- per-match stat parsing ----------------------------------------------

def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _team_fields(t: dict) -> dict:
    """Extract the model-relevant fields from one Sportec team_statistics
    object (defensive to field-name drift on the pass totals)."""
    passes_ok = t.get("passes_and_crosses_successful_sum")
    passes_tot = t.get("passes_and_crosses_sum")
    return {
        "code": t.get("team_three_letter_code"),
        "sportec_club_id": t.get("team_id"),
        "role": (t.get("team_role") or "").lower() or None,
        "goals": _int(t.get("goals")),
        "goals_conceded": _int(t.get("goals_conceded")),
        "xg": _float(t.get("xG")),
        "shots_total": _int(t.get("shots_at_goal_sum")),
        "shots_inside_box": _int(t.get("shots_at_goal_inside_box")),
        "shots_outside_box": _int(t.get("shots_at_goal_outside_box")),
        "shots_on_target": _int(t.get("shots_on_target")),
        "corners": _int(t.get("corner_kicks_sum")),
        "passes_successful": _int(passes_ok),
        "passes_total": _int(passes_tot),
    }


def _upsert_team_stat(s, fixture, side: str, team_id: int, match_id: str,
                      f: dict, opp: dict, obs_id: int, now) -> bool:
    """Insert or refresh one MlsTeamMatchStat row. Returns created?"""
    row = (s.query(MlsTeamMatchStat)
           .filter_by(fixture_id=fixture.id, side=side).first())
    created = row is None
    if created:
        row = MlsTeamMatchStat(fixture_id=fixture.id, side=side)
        s.add(row)
    row.team_id = team_id
    row.sportec_match_id = match_id
    row.sportec_club_id = f["sportec_club_id"]
    row.goals = f["goals"]
    row.goals_conceded = (f["goals_conceded"] if f["goals_conceded"]
                          is not None else opp.get("goals"))
    row.xg = f["xg"]
    row.xg_against = opp.get("xg")
    row.shots_total = f["shots_total"]
    row.shots_inside_box = f["shots_inside_box"]
    row.shots_outside_box = f["shots_outside_box"]
    row.shots_on_target = f["shots_on_target"]
    row.corners = f["corners"]
    row.passes_successful = f["passes_successful"]
    row.passes_total = f["passes_total"]
    row.source_observation_id = obs_id
    row.observed_at = now
    return created


def _upsert_player_stat(s, fixture, side, team_id, match_id, club_id,
                        p: dict, obs_id: int, now) -> bool:
    pid = p.get("player_id")
    if not pid:
        return False
    row = (s.query(MlsPlayerMatchStat)
           .filter_by(fixture_id=fixture.id, sportec_player_id=pid).first())
    created = row is None
    if created:
        row = MlsPlayerMatchStat(fixture_id=fixture.id,
                                 sportec_player_id=pid)
        s.add(row)
    name = " ".join(x for x in (p.get("player_first_name"),
                                p.get("player_last_name")) if x) \
        or p.get("player_alias")
    row.team_id = team_id
    row.side = side
    row.sportec_match_id = match_id
    row.sportec_club_id = club_id
    row.player_name = (name or "")[:96]
    row.is_goalkeeper = bool(p.get("goal_keeper"))
    row.minutes = _float(p.get("normalized_player_minutes"))
    row.goals = _int(p.get("goals"))
    row.assists = _int(p.get("assists"))
    row.xg = _float(p.get("xG"))
    row.shots_total = _int(p.get("shots_at_goal_sum"))
    row.shots_on_target = _int(p.get("shots_on_target"))
    row.shots_faced = _int(p.get("shots_on_goal_suffered"))
    row.source_observation_id = obs_id
    row.observed_at = now
    return created


def _ingest_one(s, m: dict, with_players: bool,
                skip_existing: bool = False) -> dict:
    """Ingest one match's team (+ optional player) stats onto our fixture.
    Returns a small status dict; never raises (caller commits per match).
    skip_existing avoids the network entirely for a fixture already
    ingested — so a re-boot's full-season pass only fetches the gaps."""
    ta = identity.resolve_mls_club(m["home_code"], m["home_name"])
    tb = identity.resolve_mls_club(m["away_code"], m["away_name"])
    if ta is None or tb is None:
        return {"status": "unmapped_club",
                "match": m["match_id"], "home": m["home_code"],
                "away": m["away_code"]}
    fixture = _find_fixture(s, ta.id, tb.id, m["kickoff"])
    if fixture is None:
        return {"status": "no_fixture", "match": m["match_id"]}
    if skip_existing:
        have_team = s.query(MlsTeamMatchStat).filter_by(
            fixture_id=fixture.id).count() >= 2
        # player-aware: a match with team stats but no players is NOT done
        # when players are wanted — so a player backfill still fetches them
        have_players = (not with_players) or s.query(
            MlsPlayerMatchStat).filter_by(fixture_id=fixture.id).count() > 0
        if have_team and have_players:
            return {"status": "skipped_existing", "match": m["match_id"]}

    payload = _get(f"statistics/clubs/matches/{m['match_id']}")
    if not payload:
        return {"status": "fetch_failed", "match": m["match_id"]}
    try:
        teams = payload["match_statistics_list"][0][
            "match_statistics"]["team_statistics"]
    except (KeyError, IndexError, TypeError):
        return {"status": "bad_payload", "match": m["match_id"]}
    if len(teams) != 2:
        return {"status": "expected_2_teams", "match": m["match_id"]}
    obs_id = _observe(s, f"statistics/clubs/matches/{m['match_id']}",
                      {"match_id": m["match_id"]}, payload)
    parsed = [_team_fields(t) for t in teams]
    # map each Sportec team to a side of OUR fixture (orientation is taken
    # from our fixture, not assumed to match the provider's home/away)
    by_team_id: dict[int, dict] = {}
    for pf in parsed:
        club = identity.resolve_mls_club(pf["code"])
        if club is not None:
            by_team_id[club.id] = pf
    if fixture.home_team_id not in by_team_id \
            or fixture.away_team_id not in by_team_id:
        return {"status": "orientation_mismatch", "match": m["match_id"]}
    now = _now()
    hf, af = by_team_id[fixture.home_team_id], by_team_id[fixture.away_team_id]
    created = 0
    created += _upsert_team_stat(s, fixture, "home", fixture.home_team_id,
                                 m["match_id"], hf, af, obs_id, now)
    created += _upsert_team_stat(s, fixture, "away", fixture.away_team_id,
                                 m["match_id"], af, hf, obs_id, now)

    players_written = 0
    if with_players:
        time.sleep(THROTTLE_SECONDS)
        pp = _get(f"statistics/players/matches/{m['match_id']}",
                  {"per_page": 100})
        if pp:
            plist = (pp.get("match_statistics") or {}).get(
                "player_statistics") or []
            pobs = _observe(
                s, f"statistics/players/matches/{m['match_id']}",
                {"match_id": m["match_id"]}, pp)
            for p in plist:
                club = identity.resolve_mls_club(
                    p.get("team_three_letter_code"))
                if club is None:
                    continue
                side = ("home" if club.id == fixture.home_team_id
                        else "away")
                _upsert_player_stat(s, fixture, side, club.id, m["match_id"],
                                    p.get("team_id"), p, pobs, now)
                players_written += 1
    return {"status": "ok", "match": m["match_id"], "fixture_id": fixture.id,
            "team_rows_created": created, "players": players_written}


def ingest_match_stats(gte: str | None = None, lte: str | None = None,
                       days_back: int | None = None,
                       with_players: bool = True,
                       max_matches: int | None = None,
                       skip_existing: bool = False,
                       seed_aliases: bool = True) -> dict:
    """Ingest official per-match team (+ player) stats for completed
    matches in a date range. Defaults to the full season; pass days_back
    for the cheap rolling refresh (recently completed matches only), or
    skip_existing to only fill gaps (the boot backfill)."""
    if not plane_ready():
        return {"skipped": "dormant"}
    today = _now().date()
    if days_back is not None:
        gte = (today - timedelta(days=days_back)).isoformat()
        lte = (today + timedelta(days=1)).isoformat()
    gte = gte or "2026-01-01"
    lte = lte or (today + timedelta(days=1)).isoformat()

    matches = season_matches(gte, lte)
    if max_matches:
        matches = matches[:max_matches]
    if seed_aliases and matches:
        identity.seed_mls_club_aliases(observed_clubs(matches))

    counts: dict[str, int] = {}
    ok = teamrows = players = 0
    s = get_session()
    try:
        for m in matches:
            try:
                res = _ingest_one(s, m, with_players,
                                  skip_existing=skip_existing)
                s.commit()
            except Exception as exc:            # isolate a bad match
                s.rollback()
                res = {"status": "error", "match": m.get("match_id"),
                       "error": str(exc)[:160]}
            st = res.get("status", "error")
            counts[st] = counts.get(st, 0) + 1
            if st == "ok":
                ok += 1
                teamrows += res.get("team_rows_created", 0)
                players += res.get("players", 0)
            time.sleep(THROTTLE_SECONDS)
        total = s.query(MlsTeamMatchStat).count()
        return {"matches_seen": len(matches), "ingested": ok,
                "team_rows_created": teamrows, "player_rows": players,
                "team_stat_rows_total": total, "by_status": counts,
                "range": [gte, lte]}
    finally:
        s.close()


# --- model feature substrate ---------------------------------------------

def team_xg_map() -> dict[int, dict]:
    """{fixture_id: {'home': {...}, 'away': {...}}} of the stored per-match
    team stats, for the model's xG-based ratings. Read once per fit; the
    walk-forward slices it by fixture. A fixture with no stats simply isn't
    in the map — the model falls back to goals for it."""
    if not plane_ready():
        return {}
    s = get_session()
    try:
        out: dict[int, dict] = {}
        for r in s.query(MlsTeamMatchStat).all():
            out.setdefault(r.fixture_id, {})[r.side] = {
                "xg": r.xg, "xg_against": r.xg_against,
                "goals": r.goals, "goals_conceded": r.goals_conceded,
                "shots_total": r.shots_total,
                "shots_inside_box": r.shots_inside_box,
                "shots_outside_box": r.shots_outside_box,
            }
        return out
    finally:
        s.close()


def coverage() -> dict:
    """How much of the season's completed matches we actually hold stats
    for — the answer to 'do we have all the team + player stats?'. Reports
    completed fixtures vs. matches with team stats (and with provider xG)
    and with player rows, so any gap is visible, not assumed."""
    if not plane_ready():
        return {"dormant": True}
    from sqlalchemy import distinct, func
    s = get_session()
    try:
        completed = (s.query(Fixture)
                     .filter_by(competition_slug="mls-2026", status="post")
                     .filter(Fixture.home_goals.isnot(None),
                             Fixture.home_team_id.isnot(None),
                             Fixture.away_team_id.isnot(None)).count())
        team_matches = s.query(
            func.count(distinct(MlsTeamMatchStat.fixture_id))).scalar() or 0
        team_xg_matches = (s.query(
            func.count(distinct(MlsTeamMatchStat.fixture_id)))
            .filter(MlsTeamMatchStat.xg.isnot(None)).scalar() or 0)
        player_matches = s.query(
            func.count(distinct(MlsPlayerMatchStat.fixture_id))).scalar() or 0
        return {
            "completed_fixtures": completed,
            "team_stats": {
                "matches_covered": team_matches,
                "matches_with_xg": team_xg_matches,
                "rows": s.query(MlsTeamMatchStat).count(),
                "complete": team_matches >= completed and completed > 0},
            "player_stats": {
                "matches_covered": player_matches,
                "rows": s.query(MlsPlayerMatchStat).count(),
                "goalkeeper_rows": s.query(MlsPlayerMatchStat)
                .filter_by(is_goalkeeper=True).count(),
                "complete": player_matches >= completed and completed > 0},
            "backfill": dict(_backfill_state),
        }
    finally:
        s.close()


# one-shot operator backfill runs in a daemon thread so a full-season
# player pass (minutes of throttled fetches) doesn't block the request
_backfill_state: dict = {"running": False, "started_at": None, "last": None}


def start_backfill(with_players: bool = True,
                   skip_existing: bool = True) -> dict:
    """Kick off a full-season stats ingest in the background (operator
    action). skip_existing makes it resumable and cheap on re-runs; the
    default fills the player-history gap the team-only boot leaves."""
    import threading
    if not plane_ready():
        return {"skipped": "dormant"}
    if _backfill_state["running"]:
        return {"status": "already_running",
                "started_at": _backfill_state["started_at"]}

    def _run():
        _backfill_state["running"] = True
        _backfill_state["started_at"] = _now().isoformat()
        try:
            _backfill_state["last"] = ingest_match_stats(
                with_players=with_players, skip_existing=skip_existing)
        except Exception as exc:                       # never leave stuck
            _backfill_state["last"] = {"error": str(exc)[:200]}
        finally:
            _backfill_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started", "with_players": with_players,
            "skip_existing": skip_existing}
