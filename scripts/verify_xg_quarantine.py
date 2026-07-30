"""Derive, verify and cost the Sportec xG plausibility guard.

Three things, in the order they have to be done:

  1. CALIBRATE   the thresholds from the CLEAN Feb-May window only, so they
                 are not fitted to the anomaly they are meant to catch.
  2. VERIFY      replay the SHIPPED guard (src.live.mls_stats) over every
                 archived payload and report false positives in the clean
                 window and catches in July.
  3. COST        refit model_mls.fit() with and without the quarantined
                 matches and report how far the ratings move — the number
                 needed to decide MLS_XG_QUARANTINE_EXCLUDE.

It does NOT modify src/live/model_mls.py. That file is inside the MLS
engine signature and an edit invalidates the live approval, halting shadow
collection until an operator reactivates. fit() is already a pure function
of its inputs, so it is imported and parameterized instead.

It makes NO provider requests: everything replays from the xG-comparison
archive, whose sha256 is checked and recorded. Re-fetching the July slate
to test whether Sportec has corrected it is a separate, deliberate action
(`--refetch`), because that one does touch the network.

    python scripts/verify_xg_quarantine.py \
        --archive research_archive/xg_source_comparison_2026-07-29.json

The archive is 6 MB of provider bodies and lives on branch
research-xg-source-comparison. From another branch, extract it first:

    git show research-xg-source-comparison:\
research_archive/xg_source_comparison_2026-07-29.json > /tmp/xg.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                                  # noqa: E402
from src.live import model_mls as mm                           # noqa: E402
from src.live.mls_stats import (_team_fields, _poisson_sf,      # noqa: E402
                                xg_plausibility)

DEFAULT_ARCHIVE = "research_archive/xg_source_comparison_2026-07-29.json"
# the World Cup break is the split boundary: MLS played no June fixture, so
# the boundary is external to the measured values rather than a threshold
# chosen from the disagreement distribution
CLEAN_MONTHS = ("2026-02", "2026-03", "2026-04", "2026-05")
JULY = "2026-07"


def say(msg: str = "") -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------
# load
# --------------------------------------------------------------------------

def load_matches(path: str) -> tuple[list[dict], str]:
    """Every Sportec club-statistics payload in the archive, parsed with the
    SHIPPED parser. Returns (matches, archive_sha256)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    with open(path) as fh:
        d = json.load(fh)

    out: list[dict] = []
    for rec in d.get("provider_records") or []:
        if rec.get("provider") != "sportec":
            continue
        if "statistics/clubs/matches/" not in (rec.get("path") or ""):
            continue
        body = rec.get("body")
        if not body:
            continue
        try:
            block = body["match_statistics_list"][0]["match_statistics"]
            teams = block["team_statistics"]
        except (KeyError, IndexError, TypeError):
            continue
        if len(teams) != 2:
            continue
        by_role: dict[str, dict] = {}
        for t in teams:
            role = (t.get("team_role") or "").lower()
            by_role[role] = _team_fields(t)
        if set(by_role) != {"home", "away"}:
            continue
        ko = block.get("planned_kick_off")
        if not ko:
            continue
        home, away = by_role["home"], by_role["away"]
        xgs = [home["xg"], away["xg"]]
        sots = [home["shots_on_target"], away["shots_on_target"]]
        goals = [home["goals"], away["goals"]]
        out.append({
            "match_id": block.get("match_id"),
            "title": block.get("game_title"),
            "result": block.get("result"),
            "data_status": block.get("data_status"),
            "scope": block.get("scope"),
            "kickoff": datetime.fromisoformat(ko.replace("Z", "+00:00")),
            "month": ko[:7], "date": ko[:10],
            "home": home, "away": away,
            "total_xg": (sum(xgs) if all(x is not None for x in xgs)
                         else None),
            "total_sot": sum(v or 0 for v in sots),
            "total_goals": (sum(goals) if all(g is not None for g in goals)
                            else None),
        })
    out.sort(key=lambda m: m["kickoff"])
    return out, h.hexdigest()


# --------------------------------------------------------------------------
# 1. calibrate
# --------------------------------------------------------------------------

def calibrate(ms: list[dict]) -> dict:
    """The clean window's empirical extremes. A threshold set below these is
    a threshold that cannot fire on data measured to be fine."""
    say("=" * 74)
    say("1. CALIBRATION — from the clean Feb-May window only")
    say("=" * 74)

    by_month = defaultdict(list)
    for m in ms:
        by_month[m["month"]].append(m)

    say(f"\n{'month':9} {'n':>4} {'xG':>7} {'SoT':>6} {'xG/SoT':>8} "
        f"{'goals':>6}")
    monthly = {}
    for mth in sorted(by_month):
        rs = [m for m in by_month[mth] if m["total_xg"] is not None]
        if not rs:
            continue
        xg = sum(m["total_xg"] for m in rs) / len(rs)
        sot = sum(m["total_sot"] for m in rs) / len(rs)
        gl = sum(m["total_goals"] or 0 for m in rs) / len(rs)
        monthly[mth] = {"n": len(rs), "xg": round(xg, 4),
                        "sot": round(sot, 3),
                        "xg_per_sot": round(xg / sot, 4) if sot else None,
                        "goals": round(gl, 3)}
        say(f"{mth:9} {len(rs):4d} {xg:7.4f} {sot:6.2f} "
            f"{xg / sot:8.4f} {gl:6.2f}")
    say("\n  ^ Sportec's own ratio holds 0.328-0.334 Feb-May then collapses")
    say("    in July while its own shot volume is unchanged.")

    clean = [m for m in ms
             if m["month"] in CLEAN_MONTHS and m["total_xg"] is not None]
    ratios = [m["total_xg"] / m["total_sot"] for m in clean
              if m["total_sot"] >= config.MLS_XG_GUARD_MIN_SOT]
    tails = [_poisson_sf(m["total_goals"], m["total_xg"]) for m in clean
             if m["total_goals"] is not None]

    r_min, t_min = min(ratios), min(tails)
    say(f"\nclean-window extremes over n={len(clean)} matches:")
    say(f"  match xG per own SoT   min {r_min:.4f}   "
        f"threshold {config.MLS_XG_GUARD_MIN_XG_PER_SOT}")
    say(f"  P(goals | total xG)    min {t_min:.4f}   "
        f"threshold {config.MLS_XG_GUARD_MIN_GOALS_TAIL_P}")
    ok_r = config.MLS_XG_GUARD_MIN_XG_PER_SOT < r_min
    ok_t = config.MLS_XG_GUARD_MIN_GOALS_TAIL_P < t_min
    say(f"  both thresholds below the clean extreme? "
        f"{'YES' if ok_r and ok_t else 'NO — a threshold could fire on '
                                      'data measured to be fine'}")
    return {"monthly": monthly, "clean_n": len(clean),
            "clean_min_xg_per_sot": round(r_min, 4),
            "clean_min_goals_tail_p": round(t_min, 6),
            "thresholds_below_clean_extreme": bool(ok_r and ok_t)}


# --------------------------------------------------------------------------
# 2. verify the shipped guard
# --------------------------------------------------------------------------

def verify(ms: list[dict]) -> dict:
    say("\n" + "=" * 74)
    say("2. VERIFICATION — the SHIPPED guard, replayed over every payload")
    say("=" * 74)

    per_month = defaultdict(lambda: {"n": 0, "q": 0})
    per_date = defaultdict(lambda: {"n": 0, "q": 0})
    flagged = []
    for m in ms:
        v = xg_plausibility([m["home"], m["away"]])
        per_month[m["month"]]["n"] += 1
        per_date[m["date"]]["n"] += 1
        if v["quarantined"]:
            per_month[m["month"]]["q"] += 1
            per_date[m["date"]]["q"] += 1
            flagged.append({
                "date": m["date"], "match_id": m["match_id"],
                "title": m["title"], "result": m["result"],
                "data_status": m["data_status"], "scope": m["scope"],
                "total_xg": round(m["total_xg"], 4),
                "total_own_sot": m["total_sot"],
                "total_goals": m["total_goals"],
                "reason": v["reason"], "checks": v["checks"],
            })

    say(f"\n{'month':9} {'n':>4} {'quarantined':>12}")
    for mth in sorted(per_month):
        r = per_month[mth]
        say(f"{mth:9} {r['n']:4d} {r['q']:12d}")

    clean_n = sum(per_month[m]["n"] for m in CLEAN_MONTHS if m in per_month)
    clean_q = sum(per_month[m]["q"] for m in CLEAN_MONTHS if m in per_month)
    say(f"\nfalse positives in the clean window: {clean_q} of {clean_n}")
    say(f"caught in July: {per_month[JULY]['q']} of {per_month[JULY]['n']}")

    dates = sorted({f["date"] for f in flagged})
    say(f"affected slates: {dates}")
    say("  ^ the slates either side (07-16/17/18 before, 07-25/26 after) are")
    say("    clean, which is what makes this a bounded provider incident")
    say("    rather than a screen that drifts.")

    say("\nquarantined matches:")
    for f in flagged:
        say(f"  {f['date']}  {f['match_id']}  "
            f"{(f['title'] or '')[:42]:44} {f['result'] or '':6} "
            f"data_status={f['data_status']}")
        say(f"      {f['reason']}")

    statuses = defaultdict(int)
    for f in flagged:
        statuses[f["data_status"] or "unknown"] += 1
    say(f"\nthe provider's own claim on the rejected rows: {dict(statuses)}")
    say("  ^ 'postmatch' means FINAL. There is no provider flag to reject")
    say("    these by, which is why the guard has to be derived.")

    return {"screened": len(ms), "quarantined": len(flagged),
            "clean_window": {"n": clean_n, "false_positives": clean_q},
            "july": dict(per_month[JULY]),
            "by_month": {k: dict(v) for k, v in per_month.items()},
            "by_date": {k: dict(v) for k, v in per_date.items() if v["q"]},
            "affected_dates": dates,
            "provider_data_status_on_flagged": dict(statuses),
            "flagged": flagged}


# --------------------------------------------------------------------------
# 3. cost the exclusion
# --------------------------------------------------------------------------

class StandInFixture:
    """The attribute surface model_mls.fit reads. Deliberately not an ORM
    Fixture: this runs with no database, and building ORM rows would imply
    a live plane that is not there."""
    __slots__ = ("id", "espn_event_id", "home_team_id", "away_team_id",
                 "home_goals", "away_goals", "current_kickoff_utc")

    def __init__(self, fid, h, a, hg, ag, ko):
        self.id = fid
        self.espn_event_id = None
        self.home_team_id = h
        self.away_team_id = a
        self.home_goals = hg
        self.away_goals = ag
        self.current_kickoff_utc = ko


def _entry(a: dict, b: dict) -> dict:
    return {"xg": a["xg"], "xg_against": b["xg"], "goals": a["goals"],
            "goals_conceded": b["goals"], "shots_total": a["shots_total"],
            "shots_inside_box": a["shots_inside_box"],
            "shots_outside_box": a["shots_outside_box"]}


def cost(ms: list[dict]) -> dict:
    say("\n" + "=" * 74)
    say("3. COST — how far the fitted ratings move if quarantined xG goes")
    say("=" * 74)

    codes = sorted({m[s]["code"] for m in ms for s in ("home", "away")
                    if m[s]["code"]})
    tid = {c: i + 1 for i, c in enumerate(codes)}
    label = {v: k for k, v in tid.items()}

    fixtures, xg_all, xg_excl = [], {}, {}
    dropped_codes: set[str] = set()
    n_drop = 0
    for i, m in enumerate(ms, start=1):
        h, a = m["home"], m["away"]
        if h["goals"] is None or a["goals"] is None:
            continue
        if h["code"] not in tid or a["code"] not in tid:
            continue
        fixtures.append(StandInFixture(i, tid[h["code"]], tid[a["code"]],
                                       h["goals"], a["goals"], m["kickoff"]))
        entry = {"home": _entry(h, a), "away": _entry(a, h)}
        xg_all[i] = entry
        if xg_plausibility([h, a])["quarantined"]:
            n_drop += 1                    # dropped WHOLE, as team_xg_map does
            dropped_codes.update({h["code"], a["code"]})
        else:
            xg_excl[i] = entry

    as_of = max(f.current_kickoff_utc for f in fixtures)
    alpha = config.MLS_XG_RATING_ALPHA
    say(f"\nfixtures {len(fixtures)}   teams {len(codes)}   "
        f"xg_alpha {alpha} (the DEPLOYED MLS setting)")
    say(f"xG map: {len(xg_all)} -> {len(xg_excl)} "
        f"({n_drop} quarantined fixtures dropped whole)")

    fit_now = mm.fit(fixtures, as_of, xg_by_fixture=xg_all, xg_alpha=alpha)
    fit_ex = mm.fit(fixtures, as_of, xg_by_fixture=xg_excl, xg_alpha=alpha)
    fit_goals = mm.fit(fixtures, as_of, xg_by_fixture=None, xg_alpha=0.0)

    say(f"\nxg_coverage   {fit_now['xg_coverage']} -> {fit_ex['xg_coverage']}")
    say(f"league_xg     {fit_now['league_xg']:.4f} -> "
        f"{fit_ex['league_xg']:.4f}  "
        f"({100 * (fit_ex['league_xg'] / fit_now['league_xg'] - 1):+.2f}%)")
    say("  ^ league_xg is the SHARED denominator every rating divides by, so")
    say("    the corrupt matches bias all 30 teams, not only the 14 that")
    say("    played in them.")

    rows = []
    for t in sorted(fit_now["ratings"], key=lambda x: label[x]):
        ra, rb = fit_now["ratings"][t], fit_ex["ratings"][t]
        rows.append({"team": label[t], "games": ra["games"],
                     "played_quarantined": label[t] in dropped_codes,
                     "attack_now": round(ra["attack"], 4),
                     "attack_excl": round(rb["attack"], 4),
                     "d_attack": round(rb["attack"] - ra["attack"], 4),
                     "defence_now": round(ra["defence"], 4),
                     "defence_excl": round(rb["defence"], 4),
                     "d_defence": round(rb["defence"] - ra["defence"], 4)})

    say(f"\n{'team':6} {'q?':>3} {'att now':>9} {'att excl':>9} {'d_att':>8} "
        f"{'def now':>9} {'def excl':>9} {'d_def':>8}")
    for r in sorted(rows, key=lambda x: -max(abs(x["d_attack"]),
                                             abs(x["d_defence"])))[:10]:
        say(f"{r['team']:6} {'Y' if r['played_quarantined'] else 'n':>3} "
            f"{r['attack_now']:9.4f} {r['attack_excl']:9.4f} "
            f"{r['d_attack']:+8.4f} {r['defence_now']:9.4f} "
            f"{r['defence_excl']:9.4f} {r['d_defence']:+8.4f}")

    def stats(key):
        v = [abs(r[key]) for r in rows]
        return {"mean": round(sum(v) / len(v), 4), "max": round(max(v), 4)}

    da, dd = stats("d_attack"), stats("d_defence")
    say(f"\n|d attack|   mean {da['mean']:.4f}  max {da['max']:.4f}")
    say(f"|d defence|  mean {dd['mean']:.4f}  max {dd['max']:.4f}")

    direct = [r for r in rows if r["played_quarantined"]]
    indirect = [r for r in rows if not r["played_quarantined"]]
    for grp, name in ((direct, "played a quarantined match"),
                      (indirect, "did not play one")):
        v = [abs(r["d_attack"]) for r in grp]
        say(f"  {name:28} n={len(grp):2d}  |d_att| mean "
            f"{sum(v) / len(v):.4f}  max {max(v):.4f}")

    # ordering is what a relative-strength model actually consumes, but a
    # 1-place swap between near-ties is noise: report the distribution
    ordering = {}
    for key in ("attack", "defence"):
        now = sorted(rows, key=lambda r: -r[f"{key}_now"])
        exc = {r["team"]: i for i, r in
               enumerate(sorted(rows, key=lambda r: -r[f"{key}_excl"]))}
        moves = [abs(exc[r["team"]] - i) for i, r in enumerate(now)]
        ordering[key] = {"changed_at_all": sum(1 for m in moves if m),
                         "moved_3_or_more": sum(1 for m in moves if m >= 3),
                         "moved_5_or_more": sum(1 for m in moves if m >= 5),
                         "largest_move": max(moves), "n": len(moves)}
        o = ordering[key]
        say(f"{key:8} ordering: {o['moved_3_or_more']}/{o['n']} teams move "
            f">=3 places, {o['moved_5_or_more']} move >=5, "
            f"largest {o['largest_move']}")

    ref = [abs(fit_now["ratings"][t]["attack"]
               - fit_goals["ratings"][t]["attack"])
           for t in fit_now["ratings"]]
    ref_stat = {"mean": round(sum(ref) / len(ref), 4),
                "max": round(max(ref), 4)}
    say(f"\nreference scale |xG rating - goals-only| attack: "
        f"mean {ref_stat['mean']:.4f}  max {ref_stat['max']:.4f}")
    say(f"  ^ read the {da['mean']:.4f} mean shift against THIS, not against")
    say("    zero: the quarantine moves the ratings by a material fraction")
    say("    of the entire distance the xG rung buys over goals-only.")

    say("\nNOT MEASURED HERE: the M3 walk-forward ladder. It scores locked")
    say("predictions against frozen market data in the live plane, which is")
    say("not reachable from an archive replay. Status: NOT_RUN.")

    return {"method": ("model_mls.fit imported and parameterized; "
                       "src/live/model_mls.py NOT modified"),
            "fixtures": len(fixtures), "xg_alpha": alpha,
            "quarantined_dropped": n_drop,
            "teams_that_played_one": sorted(dropped_codes),
            "xg_coverage": {"now": fit_now["xg_coverage"],
                            "excluded": fit_ex["xg_coverage"]},
            "league_xg": {"now": round(fit_now["league_xg"], 4),
                          "excluded": round(fit_ex["league_xg"], 4)},
            "abs_delta_attack": da, "abs_delta_defence": dd,
            "goals_only_reference_scale_attack": ref_stat,
            "ordering": ordering, "per_team": rows,
            "m3_ladder": ("NOT_RUN — needs the live plane's locked "
                          "market data")}


# --------------------------------------------------------------------------
# optional: has the provider corrected it?
# --------------------------------------------------------------------------

def refetch(ms: list[dict]) -> dict:
    """Re-read the affected slates from the provider and compare. The only
    part of this script that touches the network; throttled, read-only."""
    import time

    import requests
    say("\n" + "=" * 74)
    say("REFETCH — has Sportec corrected the affected slates?")
    say("=" * 74)
    affected = sorted({
        m["date"] for m in ms
        if xg_plausibility([m["home"], m["away"]])["quarantined"]})
    want = {m["match_id"]: m for m in ms if m["date"] in affected}
    say(f"slates {affected}: {len(want)} matches to re-read")

    out = []
    for mid, was in want.items():
        time.sleep(0.4)
        try:
            r = requests.get(
                "https://stats-api.mlssoccer.com/statistics/clubs/"
                f"matches/{mid}",
                headers={"Referer": "https://www.mlssoccer.com/",
                         "User-Agent": "wc26-shadow-research/1.0 "
                                       "(personal; throttled)"},
                timeout=20)
            r.raise_for_status()
            blk = r.json()["match_statistics_list"][0]["match_statistics"]
            now = [_team_fields(t) for t in blk["team_statistics"]]
        except Exception as exc:
            say(f"  {mid}: FETCH FAILED {exc}")
            out.append({"match_id": mid, "status": "fetch_failed"})
            continue
        now_xg = sum(s["xg"] or 0.0 for s in now)
        changed = abs(now_xg - (was["total_xg"] or 0.0)) > 1e-6
        v = xg_plausibility(now)
        say(f"  {mid} {(was['title'] or '')[:38]:40} "
            f"xG {was['total_xg']:.4f} -> {now_xg:.4f}  "
            f"{'CHANGED' if changed else 'unchanged'}  "
            f"still_quarantined={v['quarantined']}")
        out.append({"match_id": mid, "was_total_xg": was["total_xg"],
                    "now_total_xg": round(now_xg, 4), "changed": changed,
                    "still_quarantined": v["quarantined"],
                    "data_status": blk.get("data_status")})
    n_changed = sum(1 for r in out if r.get("changed"))
    say(f"\ncorrected by the provider: {n_changed} of {len(out)}")
    if not n_changed:
        say("  ^ a re-ingest alone fixes nothing; the guard is load-bearing.")
    return {"slates": affected, "matches": out, "corrected": n_changed}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--archive", default=DEFAULT_ARCHIVE,
                    help="xG comparison archive with Sportec bodies")
    ap.add_argument("--out", default=None, help="write results JSON here")
    ap.add_argument("--refetch", action="store_true",
                    help="also re-read the affected slates from the provider")
    args = ap.parse_args()

    if not os.path.exists(args.archive):
        say(f"archive not found: {args.archive}")
        say("It lives on branch research-xg-source-comparison. Extract it:")
        say("  git show research-xg-source-comparison:"
            f"{DEFAULT_ARCHIVE} > /tmp/xg.json")
        return 2

    ms, sha = load_matches(args.archive)
    say(f"archive {args.archive}")
    say(f"sha256  {sha}")
    say(f"parsed  {len(ms)} Sportec match-statistics payloads\n")

    result = {
        "_what": ("Derivation, verification and cost of the Sportec xG "
                  "plausibility guard in src/live/mls_stats.py"),
        "run_utc": datetime.now().astimezone().isoformat(),
        "source_archive": os.path.basename(args.archive),
        "source_archive_sha256": sha,
        "matches_parsed": len(ms),
        "thresholds": {
            "min_xg_per_own_sot": config.MLS_XG_GUARD_MIN_XG_PER_SOT,
            "min_goals_tail_p": config.MLS_XG_GUARD_MIN_GOALS_TAIL_P,
            "min_own_sot_for_ratio_check": config.MLS_XG_GUARD_MIN_SOT,
            "guard_enabled": config.MLS_XG_GUARD_ENABLED,
            "quarantine_excluded_from_ratings":
                config.MLS_XG_QUARANTINE_EXCLUDE,
        },
        "calibration": calibrate(ms),
        "verification": verify(ms),
        "cost_of_exclusion": cost(ms),
    }
    if args.refetch:
        result["refetch"] = refetch(ms)

    if args.out:
        with open(args.out, "w") as fh:
            json.dump(result, fh, indent=1)
        say(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
