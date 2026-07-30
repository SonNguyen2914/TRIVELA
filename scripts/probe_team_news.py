#!/usr/bin/env python3
"""Measure team-news coverage per competition, per source. Read-only.

This exists because coverage is a CLAIM until it is counted. The brief for
the ingestion this probe supports asserted that `/injuries?fixture=` returns
26 records on one MLS fixture; that is one fixture in one league, and a
surface built on it would imply completeness for four leagues it was never
measured on.

THE GOVERNING RULE, inherited from docs/APIFOOTBALL-TRIAL.md §2:

    ASSERT A NON-ZERO COUNT, NEVER A STATUS CODE.
    A 200 WITH NOTHING IN IT IS NOT COVERAGE, AND THIS PROBE SAYS SO.

Nothing here reads `results`, `errors: []` or `status_code` to decide
whether data exists; every conclusion counts the elements of the array the
provider actually shipped. Where the provider's own `results` count
disagrees with its array, the array wins and the disagreement is recorded.

Three states are kept distinct for every (fixture, feed) pair, because
collapsing them is the specific failure this repo has paid for twice
(`results: 0` read as "no match on"; `{"created": 0}` read as "written"):

    ok           the array had elements; the count is reported
    empty        the request succeeded and the array was empty
    unavailable  the request did not succeed — transport or HTTP error

"empty" is NOT "nobody is injured" and it is NOT "no lineup". For a lineup
it means NOT YET RELEASED, which is a statement about the provider's clock,
not about the teams.

The key travels only in a header, is never printed, never placed in a URL
or a query parameter, and every byte bound for stdout or the archive passes
through _redact() first.

Usage:
    python scripts/probe_team_news.py                 # full probe
    python scripts/probe_team_news.py --leagues mls   # one league
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

REDACTED = "<REDACTED>"
KEY_FILE = Path.home() / ".apifootball_key"
APIF_BASE = os.getenv("APIFOOTBALL_BASE", "https://v3.football.api-sports.io")
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

# Frozen league ids, read from scripts/apifootball_espn_roster.json's table on
# feat-apifootball-probe (epl 39 / laliga 140 / ligamx 262 / mls 253) so this
# probe and that harness cannot drift apart on which league is which.
LEAGUES = {
    "mls":    {"apif_id": 253, "espn": "usa.1",  "split_year": False,
               "display": "Major League Soccer"},
    "epl":    {"apif_id": 39,  "espn": "eng.1",  "split_year": True,
               "display": "English Premier League"},
    "laliga": {"apif_id": 140, "espn": "esp.1",  "split_year": True,
               "display": "Spanish LALIGA"},
    "ligamx": {"apif_id": 262, "espn": "mex.1",  "split_year": True,
               "display": "Mexican Liga BBVA MX"},
}

# ESPN's club-friendly slug. `fifa.friendly` is INTERNATIONAL friendlies and
# is deliberately out of scope, matching src/friendlies.py.
ESPN_FRIENDLY_SLUG = "club.friendly"

REQUEST_CAP = 120            # hard ceiling; the probe aborts rather than run on
THROTTLE_SECONDS = 0.35      # 300/min is the plan limit; this is ~170/min


def _redact(text, key: str) -> str:
    out = str(text)
    if key:
        out = out.replace(key, REDACTED)
        stripped = key.strip()
        if stripped and stripped != key:
            out = out.replace(stripped, REDACTED)
    return out


def load_key() -> tuple[str, str, list[str]]:
    """(key, source, problems). Mode-600 enforced on the file route: using a
    world-readable secret would quietly normalise leaving it readable."""
    problems: list[str] = []
    env = os.getenv("APIFOOTBALL_KEY", "").strip()
    if env:
        return env, "APIFOOTBALL_KEY", problems
    if not KEY_FILE.exists():
        return "", "nowhere", problems
    mode = KEY_FILE.stat().st_mode & 0o777
    if mode & 0o077:
        problems.append(f"{KEY_FILE.name} is mode {mode:03o}; must be 600. "
                        f"Refusing to read a group/world-readable secret.")
        return "", KEY_FILE.name, problems
    key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        problems.append(f"{KEY_FILE.name} is empty")
    return key, KEY_FILE.name, problems


def calendar_season(cfg: dict, now: datetime) -> int:
    """Calendar-derived, never provider-declared (TRIAL doc CHECK 2): a plan
    that cannot see the live season could flag an older one `current` and
    pass a check it actually fails."""
    if cfg["split_year"]:
        return now.year if now.month >= 7 else now.year - 1
    return now.year


def response_items(body) -> list:
    """The `response` array, or [] for any shape that is not a list. A dict
    or a string response is NOT one item — it is a shape we did not expect,
    and counting it as one would invent coverage."""
    if not isinstance(body, dict):
        return []
    r = body.get("response")
    return r if isinstance(r, list) else []


class Client:
    """Budgeted API-Football client. Counts every request, throttles, and
    records the redacted outcome of each call."""

    def __init__(self, key: str, cap: int = REQUEST_CAP):
        self.key = key
        self.cap = cap
        self.used = 0
        self.log: list[dict] = []

    def get(self, path: str, params: dict, label: str) -> dict:
        """-> {"state": ok|empty|unavailable, "count", "body", "note"}.

        `state` never comes from the status code. A 200 whose `response`
        array is empty is `empty`; a non-2xx or a transport failure is
        `unavailable`. The two are different answers and are kept apart.
        """
        if self.used >= self.cap:
            rec = {"label": label, "state": "unavailable", "count": 0,
                   "body": None,
                   "note": f"REQUEST CAP {self.cap} REACHED — not attempted"}
            self.log.append({k: rec[k] for k in ("label", "state", "note")})
            return rec
        self.used += 1
        time.sleep(THROTTLE_SECONDS)
        url = f"{APIF_BASE}/{path}"
        try:
            r = requests.get(url, params=params, timeout=25,
                             headers={"x-apisports-key": self.key,
                                      "Accept": "application/json"})
        except requests.RequestException as exc:
            rec = {"label": label, "state": "unavailable", "count": 0,
                   "body": None,
                   "note": f"transport: {_redact(exc, self.key)[:300]}"}
            self.log.append({k: rec[k] for k in ("label", "state", "note")})
            return rec
        if r.status_code // 100 != 2:
            rec = {"label": label, "state": "unavailable", "count": 0,
                   "body": None, "note": f"HTTP {r.status_code}"}
            self.log.append({k: rec[k] for k in ("label", "state", "note")})
            return rec
        try:
            body = r.json()
        except ValueError:
            rec = {"label": label, "state": "unavailable", "count": 0,
                   "body": None, "note": "response was not JSON"}
            self.log.append({k: rec[k] for k in ("label", "state", "note")})
            return rec
        items = response_items(body)
        note = None
        declared = body.get("results") if isinstance(body, dict) else None
        if isinstance(declared, int) and declared != len(items):
            # the array wins, and the disagreement is on the record
            note = (f"provider results={declared} disagrees with array "
                    f"length {len(items)}; THE ARRAY WINS")
        perrs = body.get("errors") if isinstance(body, dict) else None
        if perrs and not (isinstance(perrs, list) and not perrs):
            note = ((note + " | ") if note else "") + \
                f"provider errors={_redact(json.dumps(perrs), self.key)[:200]}"
        rec = {"label": label, "count": len(items),
               "state": "ok" if items else "empty",
               "body": json.loads(_redact(json.dumps(body), self.key)),
               "note": note}
        self.log.append({k: rec[k] for k in ("label", "state", "count", "note")})
        return rec


def espn_get(url: str, params: dict, label: str,
             log: list) -> dict:
    """ESPN is unauthenticated and uncounted against the API-Football plan."""
    try:
        r = requests.get(url, params=params, timeout=25,
                         headers={"User-Agent": "trivela-team-news-probe/1.0"})
    except requests.RequestException as exc:
        rec = {"label": label, "state": "unavailable", "body": None,
               "note": f"transport: {str(exc)[:300]}"}
        log.append({k: rec[k] for k in ("label", "state", "note")})
        return rec
    if r.status_code // 100 != 2:
        rec = {"label": label, "state": "unavailable", "body": None,
               "note": f"HTTP {r.status_code}"}
        log.append({k: rec[k] for k in ("label", "state", "note")})
        return rec
    try:
        body = r.json()
    except ValueError:
        rec = {"label": label, "state": "unavailable", "body": None,
               "note": "response was not JSON"}
        log.append({k: rec[k] for k in ("label", "state", "note")})
        return rec
    rec = {"label": label, "state": "ok", "body": body, "note": None}
    log.append({k: rec[k] for k in ("label", "state", "note")})
    return rec


# ---------------------------------------------------------------------------
# roster reading — shared with the ingestion module's semantics
# ---------------------------------------------------------------------------

def espn_roster_state(summary: dict) -> dict:
    """ESPN summary -> per-side release state, counting NAMES not containers.

    THE DISTINCTION THIS FUNCTION EXISTS FOR: `rosters` can be present with
    two entries and carry zero names (measured on Atletico v Getafe,
    2026-07-29). A present-but-empty container is NOT an empty lineup and
    NOT an absence claim — it is NOT RELEASED. Counting containers would
    report that fixture as covered.
    """
    sides = {}
    rosters = summary.get("rosters")
    rosters = rosters if isinstance(rosters, list) else []
    for team in rosters:
        if not isinstance(team, dict):
            continue
        side = team.get("homeAway")
        if side not in ("home", "away"):
            continue
        roster = team.get("roster")
        roster = roster if isinstance(roster, list) else []
        named = [e for e in roster
                 if isinstance(e, dict)
                 and ((e.get("athlete") or {}).get("displayName"))]
        starters = [e for e in named if e.get("starter")]
        sides[side] = {
            "formation": team.get("formation"),
            "named": len(named),
            "starters": len(starters),
            # released requires a full XI, matching src/live/lineups.py's
            # definition rather than inventing a second one
            "released": len(starters) >= 11,
        }
    return {"roster_containers": len(rosters), "sides": sides,
            "released": bool(sides) and all(
                v["released"] for v in sides.values()) and len(sides) == 2}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leagues", default="mls,epl,laliga,ligamx")
    ap.add_argument("--per-league", type=int, default=3,
                    help="upcoming fixtures probed per league")
    ap.add_argument("--friendlies", type=int, default=8,
                    help="ESPN friendly fixtures probed for rosters")
    ap.add_argument("--window-days", type=int, default=10,
                    help="how far ahead to look for upcoming fixtures")
    ap.add_argument("--friendly-search", default="Friendlies",
                    help="API-Football league search term for club friendlies")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    key, key_from, problems = load_key()
    now = datetime.now(timezone.utc)
    report: dict = {
        "_what": "team-news coverage measurement: injuries, lineups, events",
        "_governing_rule": ("ASSERT A NON-ZERO COUNT, NEVER A STATUS CODE. "
                            "A 200 with an empty response array is NOT "
                            "coverage. empty != unavailable != ok."),
        "_states": {
            "ok": "the response array had elements",
            "empty": "the request succeeded, the array was empty. For a "
                     "lineup this means NOT RELEASED, never 'no players'. "
                     "For injuries it means the provider listed none, "
                     "which is NOT the same as 'nobody is injured'.",
            "unavailable": "the request did not succeed (transport/HTTP)",
        },
        "run_utc": now.isoformat(),
        "key_source": key_from,
        "key_problems": problems,
        "apifootball": {"per_league": {}, "friendlies": {}},
        "espn_friendlies": {},
        "request_log": [],
        "requests_used": 0,
    }
    if not key:
        report["verdict"] = "INDETERMINATE — no key; nothing was measured"
        _write(report, args.out, now)
        print("INDETERMINATE: no key available. Nothing measured.")
        return 2

    client = Client(key)
    print(f"key from {key_from}; probing {args.leagues}")

    st = client.get("status", {}, "status")
    sub = ((st.get("body") or {}).get("response") or {})
    if isinstance(sub, dict):
        acct = dict(sub)
        acct.pop("account", None)          # name/email dropped at record layer
        report["apifootball"]["plan_status"] = acct
        print(f"  plan={((acct.get('subscription') or {}).get('plan'))!r} "
              f"quota={acct.get('requests')}")

    # ---- leagues: injuries + lineups on UPCOMING fixtures ------------------
    for lk in [x.strip() for x in args.leagues.split(",") if x.strip()]:
        cfg = LEAGUES.get(lk)
        if cfg is None:
            continue
        season = calendar_season(cfg, now)
        info: dict = {"apif_league_id": cfg["apif_id"], "season": season,
                      "display": cfg["display"], "fixtures": []}
        frm = now.date().isoformat()
        to = (now + timedelta(days=args.window_days)).date().isoformat()
        fx = client.get("fixtures", {"league": cfg["apif_id"],
                                     "season": season, "from": frm, "to": to},
                        f"fixtures:{lk}")
        info["fixture_window"] = {"from": frm, "to": to,
                                  "state": fx["state"], "count": fx["count"],
                                  "note": fx["note"]}
        items = response_items(fx.get("body") or {})
        # A league whose window is EMPTY is a distinct case from a league
        # with no fixtures at all: EPL and La Liga carry 380 season-2026
        # fixtures (measured 2026-07-29) but none in the next ten days,
        # because they are pre-season. Reporting "0 injury coverage" off
        # that window would blame the provider for our own calendar. So
        # fall back ONCE to the whole season and take the soonest
        # not-started fixture, recording that the fallback happened.
        info["season_fallback_used"] = False
        if not items:
            allfx = client.get("fixtures", {"league": cfg["apif_id"],
                                            "season": season},
                               f"fixtures_season:{lk}")
            info["season_sweep"] = {"state": allfx["state"],
                                    "count": allfx["count"],
                                    "note": allfx["note"]}
            items = response_items(allfx.get("body") or {})
            info["season_fallback_used"] = True
        # deterministic selection: soonest kickoff first, so the sample is
        # not cherry-picked and a re-run picks the same fixtures
        upcoming = []
        for it in items:
            f = (it.get("fixture") or {})
            short = ((f.get("status") or {}).get("short"))
            if short != "NS":
                continue
            upcoming.append((f.get("date") or "", f.get("id"), it))
        upcoming.sort(key=lambda t: t[0])
        info["upcoming_not_started_count"] = len(upcoming)
        print(f"\n[{lk}] season {season}: {fx['count']} fixtures in window "
              f"({fx['state']}), {len(upcoming)} not-started")

        for iso, fid, it in upcoming[:args.per_league]:
            teams = it.get("teams") or {}
            row: dict = {
                "fixture_id": fid, "kickoff_utc": iso,
                "home": ((teams.get("home") or {}).get("name")),
                "away": ((teams.get("away") or {}).get("name")),
            }
            inj = client.get("injuries", {"fixture": fid},
                             f"injuries:{lk}:{fid}")
            row["injuries"] = {"state": inj["state"], "count": inj["count"],
                               "note": inj["note"],
                               "records": _injury_records(inj)}
            lu = client.get("fixtures/lineups", {"fixture": fid},
                            f"lineups:{lk}:{fid}")
            row["lineups"] = {"state": lu["state"], "count": lu["count"],
                              "note": lu["note"],
                              # a lineup 'empty' is NOT RELEASED, not absent
                              "interpretation": ("not_released"
                                                 if lu["state"] == "empty"
                                                 else lu["state"]),
                              "sides": _lineup_sides(lu)}
            hrs = _hours_to(iso, now)
            row["hours_to_kickoff"] = hrs
            info["fixtures"].append(row)
            print(f"  {row['home']} v {row['away']}  T-{hrs}h  "
                  f"injuries={inj['state']}/{inj['count']}  "
                  f"lineups={row['lineups']['interpretation']}/{lu['count']}")

        # ---- events: on a COMPLETED fixture, where a timed feed exists ----
        done = client.get("fixtures", {"league": cfg["apif_id"],
                                       "season": season, "status": "FT",
                                       "last": 1}, f"fixtures_ft:{lk}")
        ditems = response_items(done.get("body") or {})
        if ditems:
            dfid = ((ditems[0].get("fixture") or {}).get("id"))
            ev = client.get("fixtures/events", {"fixture": dfid},
                            f"events:{lk}:{dfid}")
            info["events_on_completed_fixture"] = {
                "fixture_id": dfid, "state": ev["state"],
                "count": ev["count"], "note": ev["note"],
                "types": _event_types(ev),
                "sample": (response_items(ev.get("body") or {})[:3]),
            }
            print(f"  events on completed {dfid}: {ev['state']}/{ev['count']}")
            # Does injury coverage track PROXIMITY to kickoff? The upcoming
            # probes above are all T-46h or later. A completed fixture is the
            # cheapest way to see whether the feed is populated at all for
            # this league once the match has been played, which distinguishes
            # "this league has no injury data" from "the list fills up closer
            # to kickoff". Both are honest answers; they are different ones.
            cinj = client.get("injuries", {"fixture": dfid},
                              f"injuries_completed:{lk}:{dfid}")
            info["injuries_on_completed_fixture"] = {
                "fixture_id": dfid, "state": cinj["state"],
                "count": cinj["count"], "note": cinj["note"],
                "records": _injury_records(cinj)[:5],
            }
            print(f"  injuries on completed {dfid}: "
                  f"{cinj['state']}/{cinj['count']}")
        else:
            info["events_on_completed_fixture"] = {
                "state": done["state"], "count": 0,
                "note": "no completed fixture found in this season to probe"}
        report["apifootball"]["per_league"][lk] = info

    # ---- API-Football on FRIENDLIES: independent re-measurement -----------
    # The scope change asserted 0/7 lineups on friendlies. That is a claim
    # this probe re-measures rather than inherits.
    fr = client.get("leagues", {"search": args.friendly_search},
                    "leagues:friendlies")
    fr_items = response_items(fr.get("body") or {})
    fr_info: dict = {"discovery_state": fr["state"],
                     "discovery_search": args.friendly_search,
                     "discovery_count": fr["count"], "candidates": [
                         {"id": ((x.get("league") or {}).get("id")),
                          "name": ((x.get("league") or {}).get("name")),
                          "type": ((x.get("league") or {}).get("type"))}
                         for x in fr_items]}
    if not fr_items:
        fr_info["measured"] = (
            f"UNMEASURED — league discovery for {args.friendly_search!r} "
            f"returned an EMPTY array, so no friendly league id was resolved "
            f"and no lineup probe ran. This is 'we could not look', NOT "
            f"'friendlies have no lineups'.")
    if fr_items:
        # CLUB friendlies only. A search term matches national-team friendly
        # competitions too, and probing one of those would measure the wrong
        # thing — src/friendlies.py scopes itself to CLUB friendlies.
        club = [x for x in fr_items
                if "club" in ((x.get("league") or {}).get("name") or "").lower()]
        chosen = (club or fr_items)[0]
        flid = ((chosen.get("league") or {}).get("id"))
        fr_info["probed_league_name"] = ((chosen.get("league") or {})
                                         .get("name"))
        fr_info["club_scoped"] = bool(club)
        fr_info["probed_league_id"] = flid
        ffx = client.get("fixtures", {"league": flid, "season": now.year,
                                      "from": (now - timedelta(days=2)).date()
                                      .isoformat(),
                                      "to": (now + timedelta(days=2)).date()
                                      .isoformat()},
                         "fixtures:friendlies")
        fitems = response_items(ffx.get("body") or {})
        fr_info["fixture_window_state"] = ffx["state"]
        fr_info["fixture_count"] = ffx["count"]
        fr_info["lineup_probes"] = []
        # COMPLETED friendlies first. This ordering is the whole validity of
        # the measurement: on a NOT-STARTED fixture an empty lineup array is
        # the correct and expected answer, so a sample of NS fixtures proves
        # nothing about coverage. After full time, "not released yet" can no
        # longer explain an empty array — only absence of coverage can.
        _DONE = {"FT", "AET", "PEN"}

        def _rank(it):
            f = it.get("fixture") or {}
            short = ((f.get("status") or {}).get("short"))
            return (0 if short in _DONE else 1, f.get("date") or "")

        rows = sorted(fitems, key=_rank)
        fr_info["selection"] = ("completed fixtures first; an empty lineup on "
                               "a not-started fixture would prove nothing")
        for it in rows[:5]:
            f = it.get("fixture") or {}
            fid = f.get("id")
            lu = client.get("fixtures/lineups", {"fixture": fid},
                            f"lineups:friendly:{fid}")
            t = it.get("teams") or {}
            fr_info["lineup_probes"].append({
                "fixture_id": fid,
                "status": ((f.get("status") or {}).get("short")),
                "home": ((t.get("home") or {}).get("name")),
                "away": ((t.get("away") or {}).get("name")),
                "state": lu["state"], "count": lu["count"],
                "interpretation": ("not_released" if lu["state"] == "empty"
                                   else lu["state"]),
                # An ARRAY is not an XI. count=1 means one side only, which
                # is partial coverage, and a side object could carry an
                # empty startXI — the same present-but-empty container trap
                # ESPN sets with rosters. So count the NAMES.
                "sides": _lineup_sides(lu),
            })
        got = sum(1 for p in fr_info["lineup_probes"] if p["state"] == "ok")
        both = sum(1 for p in fr_info["lineup_probes"]
                   if len(p["sides"]) == 2
                   and all(s["start_xi"] >= 11 for s in p["sides"]))
        fr_info["measured"] = (f"{got}/{len(fr_info['lineup_probes'])} "
                               f"friendly fixtures returned a lineup array; "
                               f"{both}/{len(fr_info['lineup_probes'])} "
                               f"returned a FULL XI FOR BOTH SIDES")
        print(f"\n[friendlies/apifootball] {fr_info['measured']}")
    report["apifootball"]["friendlies"] = fr_info

    # ---- ESPN club.friendly: the friendly-lineup source ------------------
    elog: list = []
    espn: dict = {"slug": ESPN_FRIENDLY_SLUG, "fixtures": []}
    dates = [(now + timedelta(days=d)).strftime("%Y%m%d") for d in (-1, 0, 1)]
    seen: dict = {}
    for d in dates:
        sb = espn_get(f"{ESPN_BASE}/{ESPN_FRIENDLY_SLUG}/scoreboard",
                      {"dates": d}, f"espn_scoreboard:{d}", elog)
        for e in ((sb.get("body") or {}).get("events") or []):
            if e.get("id"):
                seen[str(e["id"])] = e
    espn["board_count"] = len(seen)
    print(f"\n[friendlies/espn] {len(seen)} fixtures on the board "
          f"({', '.join(dates)})")
    for eid, e in sorted(seen.items())[:args.friendlies]:
        sm = espn_get(f"{ESPN_BASE}/{ESPN_FRIENDLY_SLUG}/summary",
                      {"event": eid}, f"espn_summary:{eid}", elog)
        comp = ((e.get("competitions") or [{}])[0])
        names = [((c.get("team") or {}).get("displayName"))
                 for c in (comp.get("competitors") or [])]
        row: dict = {"espn_event_id": eid, "name": e.get("name"),
                     "teams": names,
                     "state": ((comp.get("status") or {}).get("type") or {})
                     .get("state"),
                     "detail": ((comp.get("status") or {}).get("type") or {})
                     .get("shortDetail")}
        if sm["state"] != "ok":
            row["roster_state"] = "unavailable"
            row["note"] = sm["note"]
        else:
            rs = espn_roster_state(sm.get("body") or {})
            row["roster_containers"] = rs["roster_containers"]
            row["sides"] = rs["sides"]
            if rs["roster_containers"] == 0:
                row["roster_state"] = "no_coverage"
            elif rs["released"]:
                row["roster_state"] = "released"
            else:
                # containers present, XI absent -> NOT RELEASED. Never
                # "no players are playing", never an absence claim.
                row["roster_state"] = "not_released"
        espn["fixtures"].append(row)
        print(f"  {row.get('detail')}  {' v '.join(n or '?' for n in names)}"
              f"  -> {row['roster_state']}"
              f"  named={[v['named'] for v in (row.get('sides') or {}).values()]}")
    tally: dict = {}
    for r in espn["fixtures"]:
        tally[r["roster_state"]] = tally.get(r["roster_state"], 0) + 1
    espn["coverage"] = {"probed": len(espn["fixtures"]),
                        "by_state": tally,
                        "denominator_on_board": espn["board_count"]}
    espn["request_log"] = elog
    report["espn_friendlies"] = espn

    report["request_log"] = client.log
    report["requests_used"] = client.used
    report["coverage_summary"] = _summary(report)
    print(f"\nAPI-Football requests used: {client.used}")
    print(json.dumps(report["coverage_summary"], indent=2))
    _write(report, args.out, now)
    return 0


def _hours_to(iso: str, now: datetime):
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return round((dt - now).total_seconds() / 3600, 1)


def _injury_records(inj: dict) -> list:
    """The provider's own typing, VERBATIM. `type` and `reason` are quoted,
    never re-worded into our vocabulary: 'Missing Fixture / Knee Injury' is
    API-Football's claim about a player, not ours."""
    out = []
    for it in response_items(inj.get("body") or {}):
        p = it.get("player") or {}
        t = it.get("team") or {}
        out.append({"player_id": p.get("id"), "player": p.get("name"),
                    "provider_type": p.get("type"),
                    "provider_reason": p.get("reason"),
                    "team_id": t.get("id"), "team": t.get("name")})
    return out


def _lineup_sides(lu: dict) -> list:
    out = []
    for it in response_items(lu.get("body") or {}):
        xi = it.get("startXI")
        xi = xi if isinstance(xi, list) else []
        sub = it.get("substitutes")
        sub = sub if isinstance(sub, list) else []
        out.append({"team": ((it.get("team") or {}).get("name")),
                    "team_id": ((it.get("team") or {}).get("id")),
                    "formation": it.get("formation"),
                    "start_xi": len(xi), "substitutes": len(sub)})
    return out


def _event_types(ev: dict) -> dict:
    tally: dict = {}
    for it in response_items(ev.get("body") or {}):
        k = f"{it.get('type')}/{it.get('detail')}"
        tally[k] = tally.get(k, 0) + 1
    return tally


def _summary(report: dict) -> dict:
    """The headline table. Every cell is a measured count with its state."""
    rows = {}
    for lk, info in (report["apifootball"]["per_league"] or {}).items():
        fixtures = info.get("fixtures") or []
        inj_ok = sum(1 for f in fixtures if f["injuries"]["state"] == "ok")
        inj_n = sum(f["injuries"]["count"] for f in fixtures)
        lu_ok = sum(1 for f in fixtures if f["lineups"]["state"] == "ok")
        ev = info.get("events_on_completed_fixture") or {}
        rows[lk] = {
            "fixtures_probed": len(fixtures),
            "injuries_fixtures_with_records": f"{inj_ok}/{len(fixtures)}",
            "injury_records_total": inj_n,
            "lineups_released_pre_match": f"{lu_ok}/{len(fixtures)}",
            "events_on_completed": f"{ev.get('state')}/{ev.get('count', 0)}",
        }
    fr = report["apifootball"].get("friendlies") or {}
    esp = report["espn_friendlies"].get("coverage") or {}
    return {"apifootball_leagues": rows,
            "apifootball_friendlies_lineups": fr.get("measured"),
            "espn_friendly_rosters": esp}


def _write(report: dict, out: str | None, now: datetime) -> None:
    root = Path(__file__).resolve().parent.parent
    date = now.date().isoformat()
    path = Path(out) if out else (root / "research_archive" /
                                  f"team_news_coverage_{date}.json")
    # non-clobbering: a re-run must never destroy an earlier run's evidence
    # in place. That rule was written after it happened here on 2026-07-29.
    if path.exists():
        n = 2
        while True:
            cand = path.with_suffix(f".run{n}.json")
            if not cand.exists():
                path = cand
                break
            n += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1, sort_keys=True,
                               default=str), encoding="utf-8")
    print(f"archived -> {path.relative_to(root)}")


if __name__ == "__main__":
    sys.exit(main())
