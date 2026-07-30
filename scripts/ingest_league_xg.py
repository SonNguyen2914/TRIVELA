"""Operator script: resolve friendly clubs to leagues, measure xG coverage,
ingest league xG.

    LIVE_DATABASE_URL=... APIFOOTBALL_KEY=... \
      python scripts/ingest_league_xg.py resolve
      python scripts/ingest_league_xg.py coverage
      python scripts/ingest_league_xg.py ingest [--max-fixtures N] [--league ID]

Every step is budgeted against the shared daily quota and prints the requests
it spent. Research bundles are written to research_archive/ and NEVER contain
the key.

The three steps are separate commands on purpose. `resolve` is the one that
decides which leagues matter at all — probing or ingesting a league no friendly
club plays in is quota spent for nothing — so the order is resolve, then
coverage, then ingest.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import requests                                              # noqa: E402

from src.live import apifootball, xg_ratings                  # noqa: E402

ARCHIVE = REPO / "research_archive"
ESPN = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
        "club.friendly/scoreboard")
STAMP = "2026-07-29"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def archive(what: str, payload: dict) -> Path:
    ARCHIVE.mkdir(exist_ok=True)
    p = ARCHIVE / f"xg_ratings_{what}_{STAMP}.json"
    p.write_text(json.dumps(payload, indent=1, ensure_ascii=False),
                 encoding="utf-8")
    print(f"  archived -> {p.relative_to(REPO)}")
    return p


def espn_friendly_clubs(days: int = 8) -> tuple[list[dict], list[str]]:
    """(fixtures, distinct club displayNames) from ESPN's club.friendly feed.
    ESPN is free and unmetered here — no API-Football quota is spent deciding
    WHICH clubs matter."""
    fixtures: dict[str, dict] = {}
    today = datetime.now(timezone.utc)
    for i in range(days):
        day = (today + timedelta(days=i)).strftime("%Y%m%d")
        r = requests.get(ESPN, params={"dates": day}, timeout=20)
        r.raise_for_status()
        for e in r.json().get("events") or []:
            comp = (e.get("competitions") or [{}])[0]
            sides = []
            for t in comp.get("competitors") or []:
                tm = t.get("team") or {}
                sides.append({"name": tm.get("displayName"),
                              "abbrev": tm.get("abbreviation"),
                              "home_away": t.get("homeAway")})
            fixtures[e["id"]] = {"espn_event_id": e["id"],
                                 "date": e.get("date"),
                                 "name": e.get("name"), "sides": sides}
    clubs = sorted({s["name"] for f in fixtures.values()
                    for s in f["sides"] if s["name"]})
    return list(fixtures.values()), clubs


def cmd_resolve(args) -> int:
    """Two stages, and the order matters.

    STAGE A (discovery) asks the provider which leagues each club MIGHT play
    in. Its answer is structurally ambiguous — `/leagues?team=` mixes the real
    league with pre-season friendly tournaments and stale lower-tier
    registrations — so it is used only to decide which rosters are worth
    fetching.

    STAGE B (authoritative) fetches those league rosters, one request per
    league, and resolves every club against them. Membership of a roster is a
    fact; the club-side answer was a guess."""
    fixtures, clubs = espn_friendly_clubs(args.days)
    print(f"ESPN club.friendly: {len(fixtures)} fixtures, "
          f"{len(clubs)} distinct clubs over {args.days} days")
    client = apifootball.Client(budget=args.budget)

    print("\nSTAGE A — discover candidate leagues (proposal only)")
    proposals: dict[int, dict] = {}
    discovery = []
    for name in clubs:
        try:
            d = apifootball.discover_club_leagues(name, client=client)
        except apifootball.BudgetExceeded as exc:
            print(f"  ABORT: {exc}")
            break
        except apifootball.ApiFootballError as exc:
            print(f"  {name:26s} ERROR -> {exc}")
            discovery.append({"query_name": name, "error": str(exc)[:200]})
            continue
        for lg in d["leagues"]:
            proposals.setdefault(lg["league_id"], lg)
        print(f"  {name:26s} -> "
              f"{[lg['league_name'] for lg in d['leagues']] or 'none'}")
        discovery.append(d)
    print(f"  {len(proposals)} candidate leagues discovered "
          f"({client.used} requests so far)")

    print("\nSTAGE B — fetch league rosters and resolve authoritatively")
    built = apifootball.build_roster_index(list(proposals.values()),
                                           client=client)
    index, meta = built["index"], built["meta"]
    for (lid, season), m in sorted(meta.items()):
        print(f"  league {lid:5d} season {season}: "
              f"{m.get('clubs', m.get('error'))} clubs  "
              f"{m.get('league_name') or ''}")
    rows = apifootball.resolve_all_from_index(clubs, index, meta)
    counts: dict[str, int] = {}
    for r in rows:
        apifootball.store_club_league(r)
        counts[r["resolution"]] = counts.get(r["resolution"], 0) + 1
        print(f"  {r['query_name']:26s} {r['resolution']:18s} "
              f"{(r.get('league_name') or '-'):24s} "
              f"({r.get('league_id')}) s={r.get('season')} "
              f"tier={r.get('match_tier') or '-'}")
    print(f"\nresolution counts: {counts}")
    archive("club_league_resolution", {
        "_what": ("ESPN club.friendly clubs resolved to their DOMESTIC league. "
                  "Two stages: discovery proposes candidate leagues, then "
                  "league ROSTERS decide. Ambiguous and uncovered cases are "
                  "stored as such — an ambiguous identity match fails "
                  "explicitly and never picks a club."),
        "_why_rosters": (
            "the provider's own club->league answer mixes the real league "
            "with pre-season friendly tournaments ('Premier League - Summer "
            "Series' came back for Liverpool and Sunderland) and years-stale "
            "lower-tier registrations, and no field distinguishes them. "
            "Roster membership has no such ambiguity and costs one request "
            "per league instead of two per club."),
        "resolved_utc": _now(), "espn_days": args.days,
        "fixtures": fixtures, "clubs_queried": clubs,
        "requests_used": client.used, "rate_limit": client.rate_limit,
        "resolution_counts": counts,
        "candidate_leagues": list(proposals.values()),
        "roster_meta": [{"league_id": k[0], "season": k[1], **v}
                        for k, v in meta.items()],
        "discovery": discovery,
        "resolutions": apifootball.club_league_table(),
        "detail": rows})
    print(f"requests used: {client.used}  rate: {client.rate_limit}")
    return 0


def _leagues_to_probe() -> list[tuple[int, int, str, str]]:
    """(league_id, season, name, country) for every league a resolved club
    plays in. This is the whole point of resolving first: coverage is probed
    only where a friendly club actually plays."""
    seen: dict[tuple[int, int], tuple] = {}
    for r in apifootball.club_league_table():
        if r["resolution"] != "resolved" or not r["league_id"]:
            continue
        key = (r["league_id"], r["season"])
        seen.setdefault(key, (r["league_id"], r["season"],
                              r["league_name"], r["league_country"]))
    return sorted(seen.values(), key=lambda x: x[0])


# A season with fewer completed fixtures than this cannot support league-form
# ratings, so the previous season is measured too and the one with the data
# wins. Not a guess about calendars: the provider's 'current' season was
# measured near-empty for the Premier League (2026) and J1 (2027) on
# 2026-07-29 while the history sat one season back.
_THIN_SEASON_FIXTURES = 40


def cmd_coverage(args) -> int:
    targets = _leagues_to_probe()
    print(f"{len(targets)} distinct (league, season) pairs to measure")
    client = apifootball.Client(budget=args.budget)
    out = []
    for lid, season, name, country in targets:
        if lid == apifootball.MLS_LEAGUE_ID:
            print(f"  {name}: SKIPPED — MLS keeps Sportec (refused by name)")
            continue
        for attempt_season in (season, season - 1):
            have = apifootball.coverage_for(lid, attempt_season)
            if have and not args.refresh:
                print(f"  {str(name)[:28]:28s} s={attempt_season} cached -> "
                      f"{have['verdict']} "
                      f"({have.get('completed_fixtures_visible')} completed)")
                out.append(have)
                thin = (have.get("completed_fixtures_visible") or 0) \
                    < _THIN_SEASON_FIXTURES
                if not thin:
                    break
                continue
            try:
                m = apifootball.measure_coverage(
                    lid, attempt_season, client=client, samples=args.samples,
                    league_name=name, country=country)
            except apifootball.BudgetExceeded as exc:
                print(f"  ABORT: {exc}")
                return _finish_coverage(args, client, out)
            except apifootball.ApiFootballError as exc:
                print(f"  {str(name)[:28]:28s} s={attempt_season} ERROR -> {exc}")
                continue
            apifootball.store_coverage(m)
            print(f"  {str(name)[:28]:28s} lid={lid:5d} s={attempt_season} "
                  f"completed={m['completed_visible']:4d} "
                  f"{m['samples_with_xg']}/{m['samples_taken']} -> "
                  f"{m['verdict']}"
                  f"{'  misses=' + str(m['miss_rounds']) if m['miss_rounds'] else ''}")
            out.append(m)
            if m["completed_visible"] >= _THIN_SEASON_FIXTURES:
                break
            print(f"      season {attempt_season} is thin "
                  f"({m['completed_visible']} completed) — measuring "
                  f"{attempt_season - 1} as well")
    return _finish_coverage(args, client, out)


def _finish_coverage(args, client, out) -> int:
    archive("measured_coverage", {
        "_what": ("EMPIRICAL per-(league, season) xG coverage for "
                  "API-Football. The provider's documentation is 403-walled, "
                  "so this measurement IS the coverage documentation — there "
                  "is nothing published to check it against."),
        "_governing_rule": (
            "coverage is measured on VALUES, not on the statistic type: the "
            "expected-goals TYPE appears in responses where every value is "
            "null, so a type-presence check succeeds while carrying no data. "
            "null/''/'-'/non-numeric and exactly 0 are all ABSENT."),
        "_sampling": (
            "fixtures are sampled SPREAD EVENLY across the season, not the "
            "most recent N. A most-recent-N sample is biased: the newest "
            "completed fixtures on a split-year league are the "
            "promotion/relegation playoff block, which carries no xG, and "
            "that alone made three fully-covered leagues read 'partial'."),
        "measured_utc": _now(), "samples_per_league": args.samples,
        "requests_used": client.used, "rate_limit": client.rate_limit,
        "table": apifootball.coverage_table(), "detail": out})
    print(f"requests used: {client.used}  rate: {client.rate_limit}")
    return 0


def cmd_ingest(args) -> int:
    if args.league:
        cov = apifootball.coverage_for(args.league)
        if cov is None:
            print(f"league {args.league} has no measured coverage — "
                  "run `coverage` first; refusing to ingest blind")
            return 2
        targets = [(cov["league_id"], cov["season"], cov["league_name"],
                    cov["verdict"])]
    else:
        targets = [(c["league_id"], c["season"], c["league_name"], c["verdict"])
                   for c in apifootball.coverage_table() if c["fittable"]]
    print(f"{len(targets)} fittable (league, season) pairs")
    client = apifootball.Client(budget=args.budget)
    results = []
    for lid, season, name, verdict in targets:
        try:
            res = apifootball.ingest_league_xg(
                lid, season, client=client, max_fixtures=args.max_fixtures,
                skip_existing=not args.refetch)
        except apifootball.BudgetExceeded as exc:
            print(f"  ABORT: {exc}")
            break
        except apifootball.ApiFootballError as exc:
            print(f"  {str(name)[:28]:28s} ERROR -> {exc}")
            continue
        c = res.get("counts", {})
        print(f"  {str(name)[:28]:28s} lid={lid:5d} s={season} "
              f"fixtures={res.get('completed_fixtures')} "
              f"written={c.get('written')} unchanged={c.get('unchanged')} "
              f"corrected={c.get('corrected')} "
              f"skipped={c.get('skipped_existing')} "
              f"no_stats={c.get('no_stats')} used={res.get('requests_used')}")
        results.append(res)
        if c.get("aborted"):
            print(f"    {c['aborted']}")
            break
    archive("ingest_run", {
        "_what": ("append-only league xG ingest. A re-read returning the same "
                  "values writes nothing; a re-read returning DIFFERENT "
                  "values inserts a second row, so a retroactive provider "
                  "correction is visible rather than silently applied."),
        "ingested_utc": _now(),
        "max_fixtures_per_league": args.max_fixtures,
        "refetch": bool(args.refetch),
        "requests_used": client.used, "rate_limit": client.rate_limit,
        "results": results,
        "census": apifootball.ingest_summary(),
        "retroactive_corrections": apifootball.retroactive_corrections()})
    print(f"requests used: {client.used}  rate: {client.rate_limit}")
    return 0


def cmd_report(args) -> int:
    """Print the ratings a friendly slate would actually show, and archive it.
    Costs no provider requests — everything is read from the store."""
    fixtures, _clubs = espn_friendly_clubs(args.days)
    cache: dict = {}
    rows = []
    for f in fixtures:
        sides = {s["home_away"]: s["name"] for s in f["sides"]}
        home = sides.get("home") or ""
        away = sides.get("away") or ""
        fr = xg_ratings.fixture_ratings(home, away, _cache=cache)
        rows.append({"espn_event_id": f["espn_event_id"], "date": f["date"],
                     "name": f["name"], "ratings": fr})
        def desc(side):
            if side["rated"]:
                r = side["rating"]
                return (f"{r['attack']:.3f}/{r['defence']:.3f} n={r['n_fixtures']} "
                        f"{side['league']['league_name']}")
            return f"NO RATING ({side['reason']})"
        print(f"  {f['date'][:10]} {f['name']}")
        print(f"      home {home:24s} {desc(fr['home'])}")
        print(f"      away {away:24s} {desc(fr['away'])}")
    rated = sum(1 for r in rows for s in ("home", "away")
                if r["ratings"][s]["rated"])
    total = len(rows) * 2
    print(f"\n{rated}/{total} club slots rated "
          f"({rated / total:.0%})" if total else "no fixtures")
    archive("friendlies_surface", {
        "_what": ("exactly what the friendlies page would render, per club "
                  "slot: a league-derived rating or a NAMED refusal. No "
                  "blanks, no zeros, and no match probability anywhere."),
        "generated_utc": _now(),
        "club_slots_total": total, "club_slots_rated": rated,
        "summary": xg_ratings.summary(), "fixtures": rows})
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("resolve", cmd_resolve), ("coverage", cmd_coverage),
                     ("ingest", cmd_ingest), ("report", cmd_report)):
        sp = sub.add_parser(name)
        sp.set_defaults(fn=fn)
        sp.add_argument("--days", type=int, default=8)
        sp.add_argument("--budget", type=int, default=None)
        sp.add_argument("--refresh", action="store_true")
        sp.add_argument("--samples", type=int, default=6)
        sp.add_argument("--max-fixtures", type=int, default=None)
        sp.add_argument("--league", type=int, default=None)
        sp.add_argument("--refetch", action="store_true",
                        help="re-read fixtures already held, to look for "
                             "retroactive provider corrections")
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
