"""Operator script: ingest per-fixture raw style inputs, then report the
per-league separation and correlation measurements.

    LIVE_DATABASE_URL=... APIFOOTBALL_KEY=... \
      python scripts/ingest_team_style.py ingest --league 39 --season 2025
      python scripts/ingest_team_style.py report

`ingest` spends one provider request per fixture and prints what it spent.
`report` spends NONE — it reads the store — so the separation and correlation
tables can be regenerated freely.

Research bundles are written to research_archive/ and NEVER contain the key.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.live import apifootball, team_style          # noqa: E402
from src.live.xg_ratings import SOURCE_APIFOOTBALL    # noqa: E402

ARCHIVE = REPO / "research_archive"
STAMP = "2026-07-29"

# The leagues this platform actually runs, with the API-Football league id each
# one's style is sourced from. MLS is present and DELIBERATELY has no id: its
# stats come from Sportec, which publishes neither possession nor offsides nor
# goalkeeper saves, so three of the five axes are structurally unavailable
# there. Listing it with a null is the point — omitting it would hide the gap.
TRIVELA_LEAGUES = {
    "mls-2026": {"league_id": None, "season": None, "source": "sportec",
                 "label": "Major League Soccer"},
    "epl-2026": {"league_id": 39, "season": 2025, "source": SOURCE_APIFOOTBALL,
                 "label": "Premier League"},
    "la-liga-2026": {"league_id": 140, "season": 2025,
                     "source": SOURCE_APIFOOTBALL, "label": "La Liga"},
    "liga-mx-2026": {"league_id": 262, "season": 2025,
                     "source": SOURCE_APIFOOTBALL, "label": "Liga MX"},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def archive(what: str, payload: dict) -> Path:
    ARCHIVE.mkdir(exist_ok=True)
    p = ARCHIVE / f"team_style_{what}_{STAMP}.json"
    p.write_text(json.dumps(payload, indent=1, ensure_ascii=False),
                 encoding="utf-8")
    print(f"  archived -> {p.relative_to(REPO)}")
    return p


def _targets(args) -> list[tuple[str, dict]]:
    if args.league:
        for slug, cfg in TRIVELA_LEAGUES.items():
            if cfg["league_id"] == args.league:
                return [(slug, {**cfg,
                                "season": args.season or cfg["season"]})]
        return [("ad-hoc", {"league_id": args.league, "season": args.season,
                            "source": SOURCE_APIFOOTBALL,
                            "label": f"league {args.league}"})]
    return [(s, c) for s, c in TRIVELA_LEAGUES.items()
            if c["league_id"] is not None]


def cmd_ingest(args) -> int:
    targets = _targets(args)
    client = apifootball.Client(budget=args.budget)
    results = []
    for slug, cfg in targets:
        if cfg["league_id"] is None:
            print(f"  {cfg['label']}: SKIPPED — Sportec source, see "
                  "team_style.SOURCE_AXES for its axis availability")
            continue
        try:
            res = team_style.ingest_league_style(
                cfg["league_id"], cfg["season"], client=client,
                max_fixtures=args.max_fixtures,
                skip_existing=not args.refetch, source=cfg["source"])
        except apifootball.BudgetExceeded as exc:
            print(f"  ABORT: {exc}")
            break
        except apifootball.ApiFootballError as exc:
            print(f"  {cfg['label']}: ERROR -> {exc}")
            continue
        c = res.get("counts", {})
        print(f"  {cfg['label'][:24]:24s} lid={cfg['league_id']:5d} "
              f"s={cfg['season']} fixtures={res.get('completed_fixtures')} "
              f"written={c.get('written')} unchanged={c.get('unchanged')} "
              f"corrected={c.get('corrected')} "
              f"skipped={c.get('skipped_existing')} "
              f"no_stats={c.get('no_stats')} used={res.get('requests_used')}")
        results.append({"slug": slug, **res})
        if c.get("aborted"):
            print(f"    {c['aborted']}")
            break
    archive("ingest_run", {
        "_what": ("append-only per-fixture RAW playstyle inputs. A re-read "
                  "returning the same values writes nothing; a re-read "
                  "returning DIFFERENT values inserts a second row, so a "
                  "retroactive provider correction is visible rather than "
                  "silently applied."),
        "_cost_note": (
            "one request per fixture on fixtures/statistics — the SAME "
            "endpoint the xG ingest already paid for. That ingest kept one of "
            "the eighteen statistic types the response carries and discarded "
            "the rest, so these axes had to be bought twice."),
        "ingested_utc": _now(),
        "requests_used": client.used, "rate_limit": client.rate_limit,
        "results": results,
        "census": team_style.style_census(),
        "retroactive_corrections":
            team_style.retroactive_style_corrections()})
    print(f"requests used: {client.used}  rate: {client.rate_limit}")
    return 0


def cmd_report(args) -> int:
    """Separation + correlation per league. Costs no provider requests."""
    out = []
    for slug, cfg in TRIVELA_LEAGUES.items():
        if cfg["league_id"] is None:
            out.append({
                "slug": slug, "label": cfg["label"], "source": cfg["source"],
                "measured": False,
                "why_not": (
                    "MLS style comes from Sportec, which publishes shots "
                    "inside/outside box and xG but NO possession, NO offsides "
                    "and NO goalkeeper saves. Three of the five axes are "
                    "structurally unavailable, not merely unmeasured."),
                "axes_this_source_can_carry": list(
                    team_style.SOURCE_AXES[cfg["source"]]),
                "axes_this_source_cannot_carry": [
                    a for a in team_style.ALL_AXES
                    if a not in team_style.SOURCE_AXES[cfg["source"]]],
            })
            continue
        ls = team_style.league_style(cfg["source"], cfg["league_id"],
                                     cfg["season"])
        if ls.get("refused_reason"):
            print(f"\n{cfg['label']}: {ls['refused_words']}")
            out.append({"slug": slug, "label": cfg["label"],
                        "measured": False,
                        "why_not": ls["refused_words"]})
            continue
        sep, cor = ls["separation"], ls["correlation"]
        print(f"\n=== {cfg['label']}  ({ls['scope']}) "
              f"team-matches={ls['n_team_matches']} "
              f"teams={len(ls['vectors'])} ===")
        print(f"  {'axis':22s} {'n':>3s} {'raw min':>9s} {'raw max':>9s} "
              f"{'spread':>8s} {'raw sd':>8s} {'shrunk sd':>9s}  separates")
        for axis, r in sep["axes"].items():
            tag = " (candidate)" if r.get("candidate") else ""
            if not r["n_teams"]:
                print(f"  {axis + tag:22s}  -- REFUSED: "
                      f"{r['refused_reason']}")
                continue
            print(f"  {axis + tag:22s} {r['n_teams']:3d} "
                  f"{r['raw_min']:9.4f} {r['raw_max']:9.4f} "
                  f"{r['raw_spread']:8.4f} {r['raw_sd']:8.4f} "
                  f"{r['shrunk_sd']:9.4f}  {r['separates']}")
            print(f"      low={r['min_team']}  high={r['max_team']}")
        print("  correlation (teams, pairwise-complete):")
        for p in cor["pairs"]:
            flag = f"   <-- {p['flag']}" if p.get("flag") else ""
            rv = "undefined" if p["r"] is None else f"{p['r']:+.4f}"
            print(f"      {p['a']:22s} x {p['b']:22s} r={rv:>9s} "
                  f"n={p['n_teams']}{flag}")
        if cor["flagged"]:
            print(f"  FLAGGED >= |{cor['threshold_abs_r']}|: "
                  f"{[(f['a'], f['b'], f['r']) for f in cor['flagged']]}")
        out.append({
            "slug": slug, "label": cfg["label"], "measured": True,
            "scope": ls["scope"], "n_team_matches": ls["n_team_matches"],
            "n_teams": len(ls["vectors"]),
            "axes_available": ls["axes_available"],
            "axes_not_in_source": ls["axes_not_in_source"],
            "league_moments": ls["league"],
            "separation": sep, "correlation": cor,
            "vectors": {str(k): v.as_dict() for k, v in ls["vectors"].items()},
        })
    archive("separation_and_correlation", {
        "_what": ("per-league playstyle axis SEPARATION (spread and standard "
                  "deviation) and the axis CORRELATION matrix, over a full "
                  "season of each league's own fixtures."),
        "_why": ("five axes were pre-registered from a 40-fixture EPL look. "
                 "Whether they separate, and whether they stay independent, "
                 "over a full season in every league is a MEASUREMENT — this "
                 "is it. An axis that does not separate in a league is "
                 "reported as such rather than shipped because five was the "
                 "plan."),
        "_label_policy": (
            "no label appears in any computed field. Every vector carries a "
            "display_label rendered through DisplayLabel.for_display(), the "
            "single sanctioned exit; the label is never an input."),
        "generated_utc": _now(),
        "collinear_abs_r": team_style.COLLINEAR_ABS_R,
        "pre_registered_axes": list(team_style.AXES),
        "candidate_axes": list(team_style.CANDIDATE_AXES),
        "source_axes": {k: list(v)
                        for k, v in team_style.SOURCE_AXES.items()},
        "parameters": team_style.summary()["parameters"],
        "leagues": out})
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("ingest", cmd_ingest), ("report", cmd_report)):
        sp = sub.add_parser(name)
        sp.set_defaults(fn=fn)
        sp.add_argument("--budget", type=int, default=None)
        sp.add_argument("--max-fixtures", type=int, default=None)
        sp.add_argument("--league", type=int, default=None)
        sp.add_argument("--season", type=int, default=None)
        sp.add_argument("--refetch", action="store_true",
                        help="re-read fixtures already held, to look for "
                             "retroactive provider corrections")
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
