"""Fetch a league's COMPLETED prior-season fixtures from ESPN and archive
them as the replay ladder's input bytes (backlog S-5, stage 1).

This writes to research_archive/ and NEVER to the live plane. The live
Fixture table holds only the competitions the platform actually runs
(mls-2026 / epl-2026 / liga-mx-2026); prior-season history is research
input, so it stays a file whose sha256 the replay report records. That
also makes the ladder reproducible from bytes rather than from mutable
database state.

Stage-1 gate (S-5's falsifier): the script counts the completed events it
actually got and reports the ratio against the season's expected length.
It does not decide anything — `ladder_replay.completeness()` applies the
gate, and an incomplete history kills the replay for that league.

Usage:
    python -m scripts.fetch_prior_season                # every league
    python -m scripts.fetch_prior_season eng.1 esp.1    # a subset

Per-event semantic invariants are checked at parse time (the ESPN drift
rule: derive the W/L/D letter from the same numbers rendered beside it).
A competitor `winner` flag that disagrees with the two scores is recorded
as a discrepancy rather than silently trusted.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
              "{league}/scoreboard")

# Per-league fetch windows and the expected season length used ONLY as
# the stage-1 denominator. Expectations are documented arithmetic, not
# provider claims:
#   eng.1 / esp.1  20 clubs x 38 rounds x 10 matches/round = 380
#   mex.1          18 clubs, 17 rounds x 9 = 153 per tournament, plus a
#                  Liguilla; Apertura and Clausura are SEPARATE
#                  tournaments and are never merged into one table.
#   usa.1          MLS 2025 regular season (the methodological control:
#                  the same replay run on the one league whose live
#                  ladder result is already known).
LEAGUES = {
    "eng.1": {
        "name": "English Premier League",
        "prior_season": "2025-26",
        "windows": [("2025-08-01", "2026-06-05")],
        "expected_regular": 380,
        "segments_from": "season_slug",
    },
    "esp.1": {
        "name": "Spanish LaLiga",
        "prior_season": "2025-26",
        "windows": [("2025-08-01", "2026-06-05")],
        "expected_regular": 380,
        "segments_from": "season_slug",
    },
    "mex.1": {
        "name": "Liga BBVA MX",
        "prior_season": "Apertura 2025 + Clausura 2026",
        "windows": [("2025-06-25", "2025-12-31"),
                    ("2026-01-01", "2026-06-05")],
        # per TOURNAMENT, not per season-year: the split season is the
        # whole point of keeping the segments distinct
        "expected_regular": 153,
        "segments_from": "season_slug",
    },
    "usa.1": {
        "name": "Major League Soccer",
        "prior_season": "2025",
        "windows": [("2025-02-01", "2025-12-31")],
        # 30 clubs x 34 matches = 1020 team-games / 2 = 510. An earlier
        # revision of this file said 493, which was simply wrong
        # arithmetic; the fetched count (510) is what exposed it.
        "expected_regular": 510,
        "segments_from": "season_slug",
    },
}

CHUNK_DAYS = 21


def _iso(d: date) -> str:
    return d.strftime("%Y%m%d")


def _chunks(start: str, end: str):
    s = datetime.strptime(start, "%Y-%m-%d").date()
    e = datetime.strptime(end, "%Y-%m-%d").date()
    while s <= e:
        stop = min(s + timedelta(days=CHUNK_DAYS - 1), e)
        yield _iso(s), _iso(stop)
        s = stop + timedelta(days=1)


def _parse_event(ev: dict) -> dict | None:
    """One scoreboard event -> a flat replay record, or None when it is
    not a usable completed fixture. Every field is provider-stable: ESPN
    event id and ESPN team ids, never names (AGENTS.md §13)."""
    comp = (ev.get("competitions") or [{}])[0]
    status = (((ev.get("status") or comp.get("status")) or {})
              .get("type") or {})
    state = status.get("state")
    sides: dict[str, dict] = {}
    for c in comp.get("competitors") or []:
        team = c.get("team") or {}
        score = c.get("score")
        if isinstance(score, dict):
            score = score.get("value")
        sides[c.get("homeAway")] = {
            "team_id": str(team.get("id")) if team.get("id") else None,
            "team_name": team.get("displayName"),
            "score": score,
            "winner_flag": c.get("winner"),
        }
    if "home" not in sides or "away" not in sides:
        return None
    if not (sides["home"]["team_id"] and sides["away"]["team_id"]):
        return None
    goals = {}
    for side in ("home", "away"):
        try:
            goals[side] = int(float(sides[side]["score"]))
        except (TypeError, ValueError):
            return None
    season = ev.get("season") or {}
    # semantic invariant: the provider's own winner flag must agree with
    # the two numbers. Recorded, never silently trusted (the winner-first
    # `score` drift of 2026-07-24 rendered every defeat as a win).
    if goals["home"] > goals["away"]:
        derived = "home"
    elif goals["away"] > goals["home"]:
        derived = "away"
    else:
        derived = "draw"
    flags = {s: sides[s]["winner_flag"] for s in ("home", "away")}
    if derived == "draw":
        agrees = flags["home"] is not True and flags["away"] is not True
    else:
        agrees = flags[derived] is True
    return {
        "espn_event_id": str(ev.get("id")),
        "kickoff_utc": ev.get("date"),
        "state": state,
        "season_year": season.get("year"),
        "season_slug": season.get("slug"),
        "home_team_id": sides["home"]["team_id"],
        "away_team_id": sides["away"]["team_id"],
        "home_team_name": sides["home"]["team_name"],
        "away_team_name": sides["away"]["team_name"],
        "home_goals": goals["home"],
        "away_goals": goals["away"],
        "result": derived,
        "winner_flag_agrees": bool(agrees),
    }


def fetch_league(league: str, cfg: dict, sleep: float = 0.4) -> dict:
    events: dict[str, dict] = {}
    non_post = 0
    unusable = 0
    requests_made = []
    for start, end in cfg["windows"]:
        for a, b in _chunks(start, end):
            url = SCOREBOARD.format(league=league)
            r = requests.get(url, params={"dates": f"{a}-{b}"}, timeout=30)
            requests_made.append({"dates": f"{a}-{b}",
                                  "http_status": r.status_code})
            if r.status_code != 200:
                print(f"  !! {league} {a}-{b} HTTP {r.status_code}")
                continue
            raw = r.json().get("events") or []
            for ev in raw:
                st = ((((ev.get("status") or {}).get("type")) or {})
                      .get("state"))
                if st != "post":
                    non_post += 1
                    continue
                rec = _parse_event(ev)
                if rec is None:
                    unusable += 1
                    continue
                events[rec["espn_event_id"]] = rec
            print(f"  {league} {a}-{b}: {len(raw)} events "
                  f"(running total {len(events)})")
            time.sleep(sleep)
    rows = sorted(events.values(), key=lambda e: (e["kickoff_utc"] or ""))
    segments: dict[str, int] = {}
    for e in rows:
        key = f"{e['season_slug']}-{e['season_year']}"
        segments[key] = segments.get(key, 0) + 1
    return {
        "league": league,
        "league_name": cfg["name"],
        "prior_season_label": cfg["prior_season"],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "ESPN site.api scoreboard (keyless, public)",
        "evidence_class": "REPLAYED",
        "evidence_note": ("prior-season history retrieved AFTER the fact; "
                          "nothing here was frozen before kickoff, so it "
                          "can never be MEASURED evidence"),
        "windows": cfg["windows"],
        "chunk_days": CHUNK_DAYS,
        "requests": requests_made,
        "expected_regular_per_segment": cfg["expected_regular"],
        "counts": {
            "completed_events": len(rows),
            "skipped_not_post": non_post,
            "skipped_unusable": unusable,
            "winner_flag_disagreements": sum(
                1 for e in rows if not e["winner_flag_agrees"]),
        },
        "segment_counts": segments,
        "events": rows,
    }


def main(argv: list[str]) -> int:
    targets = argv[1:] or list(LEAGUES)
    stamp = date.today().isoformat()
    for league in targets:
        cfg = LEAGUES.get(league)
        if cfg is None:
            print(f"unknown league {league}")
            return 2
        print(f"== {league} ({cfg['name']}) prior season "
              f"{cfg['prior_season']}")
        doc = fetch_league(league, cfg)
        out = (f"research_archive/ladder_{league.replace('.', '')}"
               f"_prior_season_{stamp}.json")
        blob = json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False)
        with open(out, "w") as fh:
            fh.write(blob)
        print(f"  -> {out}  n={doc['counts']['completed_events']}  "
              f"sha256={hashlib.sha256(blob.encode()).hexdigest()[:16]}")
        print(f"     segments: {doc['segment_counts']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
