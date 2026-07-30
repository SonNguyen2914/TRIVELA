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
import math
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


def _poisson_sf(k: int, lam: float) -> float:
    """P(N >= k) for Poisson(lam) — the chance a shot model expecting `lam`
    goals produced at least the `k` that were actually scored. Exact sum,
    not an approximation: k is a match's goal count, so it is tiny."""
    if k <= 0:
        return 1.0
    if lam <= 0:
        # zero expected goals cannot produce a goal. Not "very unlikely" —
        # impossible, and the only value that says so is 0.
        return 0.0
    term = math.exp(-lam)
    cdf = term
    for i in range(1, k):
        term *= lam / i
        cdf += term
    return max(0.0, min(1.0, 1.0 - cdf))


def xg_plausibility(sides: list[dict]) -> dict:
    """Is the xG in this payload consistent with the shot and goal counts in
    the SAME payload? Returns {"quarantined", "reason", "checks"}.

    WHY THIS IS DERIVED AND NOT READ. Sportec's own xG collapsed on the
    2026-07-22/23 MLS restart slates while its own shot volume held: total
    match xG per own shot on target ran 0.328-0.334 Feb-May, then 0.246 in
    July. Yet `data_status` is "postmatch" — the provider's own claim that
    the row is FINAL — on 253/253 matches measured, the seven broken ones
    included. There is no provider flag to reject them by, so the only
    available evidence is the payload contradicting itself.

    TWO INDEPENDENT CHECKS, both on match totals:

      ratio    total xG per own shot on target. Catches "the shot model
               went quiet while the shots kept coming".
      goals    P(N >= goals scored | total xG), Poisson. Catches "four-three
               on 1.37 expected goals" — a scoreline the xG cannot explain.

    The ratio check alone flags all seven July matches, so `goals` is
    currently redundant. It is kept because it fails on a DIFFERENT input:
    if the shot counts were ever corrupted alongside the xG, the ratio
    would look normal and only the scoreline would disagree. Redundancy
    between two signals is the point, not an accident — and each check
    records itself, so which one is doing the work stays visible.

    THRESHOLDS ARE NOT TUNED TO THE ANOMALY. Both sit below the clean
    Feb-May window's empirical extreme over 218 matches (ratio min 0.1443,
    tail min 0.0228). Measured: 0/218 clean flagged, 7/35 July flagged, all
    seven on 2026-07-22 and 2026-07-23. The three slates around them
    (07-16/17/18 before, 07-25/26 after) are clean, which is what makes
    this a bounded provider incident rather than a drifting screen.

    A verdict of False means "screened and passed", which is not the same
    claim as a NULL column ("never screened") — never blur the two.
    """
    checks: dict = {}
    xgs = [s.get("xg") for s in sides]
    if len(sides) != 2 or any(x is None for x in xgs):
        # nothing to screen: without both sides' xG the fixture already
        # contributes nothing to the xG rating (fit() requires xg AND
        # xg_against), so this is not a pass and not a failure
        return {"quarantined": False, "reason": None,
                "checks": {"applicable": False, "why": "xg_absent"}}

    total_xg = float(sum(xgs))
    sots = [s.get("shots_on_target") for s in sides]
    total_sot = sum(v for v in sots if v is not None)
    goals = [s.get("goals") for s in sides]
    total_goals = (sum(goals) if all(g is not None for g in goals) else None)

    failed: list[str] = []

    # --- check 1: xG against the provider's own shot volume ---------------
    if total_sot >= config.MLS_XG_GUARD_MIN_SOT:
        ratio = total_xg / total_sot
        thr = config.MLS_XG_GUARD_MIN_XG_PER_SOT
        checks["ratio"] = {"xg_per_own_sot": round(ratio, 4),
                           "threshold": thr, "total_xg": round(total_xg, 4),
                           "total_own_sot": total_sot, "passed": ratio >= thr}
        if ratio < thr:
            failed.append(f"xg_per_own_sot={ratio:.4f}<{thr} "
                          f"(xg={total_xg:.3f} on {total_sot} SoT)")
    else:
        # a sparse match carries little evidence either way; the clean
        # window's sparse matches have HIGH ratios, not low ones
        checks["ratio"] = {"applicable": False, "total_own_sot": total_sot,
                           "min_sot": config.MLS_XG_GUARD_MIN_SOT}

    # --- check 2: xG against the scoreline in the same payload ------------
    if total_goals is not None:
        p = _poisson_sf(total_goals, total_xg)
        thr_p = config.MLS_XG_GUARD_MIN_GOALS_TAIL_P
        checks["goals"] = {"tail_p": round(p, 6), "threshold": thr_p,
                           "total_goals": total_goals,
                           "total_xg": round(total_xg, 4),
                           "passed": p >= thr_p}
        if p < thr_p:
            failed.append(f"goals_tail_p={p:.5f}<{thr_p} "
                          f"({total_goals} goals on xg={total_xg:.3f})")
    else:
        checks["goals"] = {"applicable": False, "why": "goals_absent"}

    if not failed:
        return {"quarantined": False, "reason": None, "checks": checks}
    if not config.MLS_XG_GUARD_ENABLED:
        # when the guard is disabled the checks are still COMPUTED and the
        # failure is still RECORDED — an off switch that also stops
        # measuring would hide the next incident the way the last one hid
        # behind {"created": 0}. Only the quarantine verdict is withheld.
        return {"quarantined": False,
                "reason": ("guard_disabled: " + "; ".join(failed))[:160],
                "checks": checks}
    return {"quarantined": True, "reason": "; ".join(failed)[:160],
            "checks": checks}


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
                      f: dict, opp: dict, obs_id: int, now,
                      meta: dict | None = None,
                      verdict: dict | None = None) -> bool:
    """Insert or refresh one MlsTeamMatchStat row. Returns created?

    The provider's raw xG is stored VERBATIM even when quarantined — the
    verdict is recorded beside it, never substituted for it. Historical
    evidence does not get rewritten to improve a result (AGENTS.md §3), and
    a later threshold change has to be re-derivable from what was received.
    """
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
    row.data_status = (meta or {}).get("data_status")
    row.scope = (meta or {}).get("scope")
    # no verdict means nothing screened this row, and the column must stay
    # NULL to say so. Writing False would claim "screened and passed" —
    # missing evidence quietly promoted to confidence (AGENTS.md §3).
    row.xg_quarantined = (None if verdict is None
                          else bool(verdict.get("quarantined")))
    row.xg_quarantine_reason = (verdict or {}).get("reason")
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


# process-local ledger of quarantines, so a run's outcome is visible in the
# response and the logs and not only by querying the table afterwards. The
# DURABLE record is the row itself (xg_quarantined / _reason) plus this
# printed line — a counter alone dies with the process.
_guard_state: dict = {"screened": 0, "quarantined": 0, "recent": []}
_GUARD_RECENT_MAX = 40


def _note_quarantine(m: dict, fixture, verdict: dict, meta: dict) -> None:
    """Record and PRINT one quarantine. A guard that rejects silently is
    indistinguishable from a provider that went quiet, and "the number just
    got smaller" is the shape of every incident this project has paid for."""
    entry = {
        "match": m.get("match_id"), "fixture_id": getattr(fixture, "id", None),
        "kickoff": (m["kickoff"].isoformat() if m.get("kickoff") else None),
        "home": m.get("home_code"), "away": m.get("away_code"),
        "goals": [m.get("home_goals"), m.get("away_goals")],
        "reason": verdict.get("reason"),
        "provider_data_status": meta.get("data_status"),
        "at": _now().isoformat(),
    }
    _guard_state["quarantined"] += 1
    _guard_state["recent"].append(entry)
    del _guard_state["recent"][:-_GUARD_RECENT_MAX]
    print(f"[mls_stats] xG QUARANTINE {entry['match']} "
          f"{entry['home']}-{entry['away']} "
          f"{entry['goals'][0]}-{entry['goals'][1]}: {entry['reason']} "
          f"(provider data_status={entry['provider_data_status']})")


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
        block = payload["match_statistics_list"][0]["match_statistics"]
        teams = block["team_statistics"]
    except (KeyError, IndexError, TypeError):
        return {"status": "bad_payload", "match": m["match_id"]}
    if len(teams) != 2:
        return {"status": "expected_2_teams", "match": m["match_id"]}
    # the provider's own claim about this payload, recorded rather than
    # trusted: it reads "postmatch" even on the matches whose xG is wrong
    meta = {"data_status": block.get("data_status"),
            "scope": block.get("scope")}
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
    # one verdict per MATCH, applied to both rows: the checks are on match
    # totals, and fit() needs both sides' xG to use either, so a per-side
    # verdict could not be acted on coherently anyway
    verdict = xg_plausibility([hf, af])
    _guard_state["screened"] += 1
    if verdict["quarantined"]:
        _note_quarantine(m, fixture, verdict, meta)
    created = 0
    created += _upsert_team_stat(s, fixture, "home", fixture.home_team_id,
                                 m["match_id"], hf, af, obs_id, now,
                                 meta=meta, verdict=verdict)
    created += _upsert_team_stat(s, fixture, "away", fixture.away_team_id,
                                 m["match_id"], af, hf, obs_id, now,
                                 meta=meta, verdict=verdict)

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
            "team_rows_created": created, "players": players_written,
            "xg_quarantined": verdict["quarantined"],
            "xg_quarantine_reason": verdict["reason"]}


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
    ok = teamrows = players = quarantined = 0
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
                if res.get("xg_quarantined"):
                    quarantined += 1
            time.sleep(THROTTLE_SECONDS)
        total = s.query(MlsTeamMatchStat).count()
        # xg_quarantined is reported unconditionally, including as 0 — an
        # absent key reads as "the guard did not run", which is a different
        # claim and the one that hides a regression
        return {"matches_seen": len(matches), "ingested": ok,
                "team_rows_created": teamrows, "player_rows": players,
                "team_stat_rows_total": total, "by_status": counts,
                "xg_quarantined": quarantined,
                "xg_quarantine_excluded_from_ratings":
                    config.MLS_XG_QUARANTINE_EXCLUDE,
                "range": [gte, lte]}
    finally:
        s.close()


# --- model feature substrate ---------------------------------------------

def team_xg_map() -> dict[int, dict]:
    """{fixture_id: {'home': {...}, 'away': {...}}} of the stored per-match
    team stats, for the model's xG-based ratings. Read once per fit; the
    walk-forward slices it by fixture. A fixture with no stats simply isn't
    in the map — the model falls back to goals for it.

    QUARANTINE. When config.MLS_XG_QUARANTINE_EXCLUDE is true, a fixture
    with any quarantined row is dropped from the map ENTIRELY, so it lands
    in exactly the state fit() already handles — no xG, goals only. Dropping
    one side would not do that: the surviving row's `xg_against` IS the
    other side's rejected number, so the opponent would keep consuming it.

    The default is false, which reproduces byte-for-byte what the deployed
    M3 rung consumes today. Flipping it is a modelling change owed an
    evaluation and sequenced between slates (AGENTS.md §12) — not something
    this function should decide on its own.
    """
    if not plane_ready():
        return {}
    exclude = config.MLS_XG_QUARANTINE_EXCLUDE
    s = get_session()
    try:
        out: dict[int, dict] = {}
        dropped: set[int] = set()
        for r in s.query(MlsTeamMatchStat).all():
            if exclude and r.xg_quarantined:
                dropped.add(r.fixture_id)
            out.setdefault(r.fixture_id, {})[r.side] = {
                "xg": r.xg, "xg_against": r.xg_against,
                "goals": r.goals, "goals_conceded": r.goals_conceded,
                "shots_total": r.shots_total,
                "shots_inside_box": r.shots_inside_box,
                "shots_outside_box": r.shots_outside_box,
            }
        for fid in dropped:
            out.pop(fid, None)
        if dropped:
            print(f"[mls_stats] team_xg_map excluded {len(dropped)} "
                  f"quarantined fixture(s) from the xG ratings")
        return out
    finally:
        s.close()


def quarantine_report(limit: int = 100) -> dict:
    """Which stored matches the plausibility guard rejected, and whether the
    ratings are actually excluding them. Reads the DURABLE record (the rows),
    not the process counters, so a restart does not erase the answer.

    `screened_rows` counts rows with a non-NULL verdict: NULL means the row
    predates the guard and has never been screened, which is not the same as
    passing it. Reporting the two as one number would convert missing
    evidence into confidence (AGENTS.md §3)."""
    if not plane_ready():
        return {"dormant": True}
    from sqlalchemy import func
    s = get_session()
    try:
        rows = (s.query(MlsTeamMatchStat)
                .filter(MlsTeamMatchStat.xg_quarantined.is_(True))
                .order_by(MlsTeamMatchStat.fixture_id.desc())
                .limit(limit).all())
        total = s.query(MlsTeamMatchStat).count()
        screened = (s.query(func.count(MlsTeamMatchStat.id))
                    .filter(MlsTeamMatchStat.xg_quarantined.isnot(None))
                    .scalar() or 0)
        q_rows = (s.query(func.count(MlsTeamMatchStat.id))
                  .filter(MlsTeamMatchStat.xg_quarantined.is_(True))
                  .scalar() or 0)
        q_fixtures = (s.query(
            func.count(func.distinct(MlsTeamMatchStat.fixture_id)))
            .filter(MlsTeamMatchStat.xg_quarantined.is_(True)).scalar() or 0)
        # the provider's own claim on the rejected rows — the whole reason
        # the guard has to be derived rather than read off a flag
        statuses: dict[str, int] = {}
        for r in rows:
            k = r.data_status or "unknown"
            statuses[k] = statuses.get(k, 0) + 1
        return {
            "rows_total": total,
            "rows_screened": screened,
            "rows_never_screened": total - screened,
            "rows_quarantined": q_rows,
            "fixtures_quarantined": q_fixtures,
            "excluded_from_ratings": config.MLS_XG_QUARANTINE_EXCLUDE,
            "guard_enabled": config.MLS_XG_GUARD_ENABLED,
            "thresholds": {
                "min_xg_per_own_sot": config.MLS_XG_GUARD_MIN_XG_PER_SOT,
                "min_goals_tail_p": config.MLS_XG_GUARD_MIN_GOALS_TAIL_P,
                "min_own_sot_for_ratio_check": config.MLS_XG_GUARD_MIN_SOT,
            },
            "provider_data_status_on_quarantined": statuses,
            "quarantined": [{
                "fixture_id": r.fixture_id, "side": r.side,
                "sportec_match_id": r.sportec_match_id,
                "team_id": r.team_id, "xg": r.xg, "xg_against": r.xg_against,
                "goals": r.goals, "goals_conceded": r.goals_conceded,
                "shots_on_target": r.shots_on_target,
                "data_status": r.data_status, "scope": r.scope,
                "reason": r.xg_quarantine_reason,
                "observed_at": (r.observed_at.isoformat()
                                if r.observed_at else None),
            } for r in rows],
            "this_process": dict(_guard_state),
        }
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
        # coverage that counts a provably-wrong xG as covered is a coverage
        # number that lies, so the quarantined subset is reported here too
        q_matches = (s.query(
            func.count(distinct(MlsTeamMatchStat.fixture_id)))
            .filter(MlsTeamMatchStat.xg_quarantined.is_(True)).scalar() or 0)
        player_matches = s.query(
            func.count(distinct(MlsPlayerMatchStat.fixture_id))).scalar() or 0
        return {
            "completed_fixtures": completed,
            "team_stats": {
                "matches_covered": team_matches,
                "matches_with_xg": team_xg_matches,
                "matches_xg_quarantined": q_matches,
                "matches_with_usable_xg": team_xg_matches - q_matches,
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
