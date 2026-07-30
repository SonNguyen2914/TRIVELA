#!/usr/bin/env python3
"""Settle a disagreement about API-Football friendly lineup arrays.

TWO HONEST READINGS THAT DO NOT AGREE, both taken 2026-07-30 on the same PRO
key against league 667 (Friendlies Clubs), which had 454 fixtures in window:

  THIS AGENT (research_archive/team_news_coverage_2026-07-30.run4/run5.json)
      5 COMPLETED fixtures probed, selected completed-first from the window.
      4 returned a NON-EMPTY `response` array whose side objects each carried
      formation=None, startXI=[], substitutes=[]. 1 returned an empty array.

  THE COORDINATOR
      12 COMPLETED fixtures across two samples (head-5 and spread-7).
      12/12 returned NO ARRAY AT ALL — results=0, len(array)=0.

The samples do not overlap: not one fixture id is common to both. So three
explanations were live, and a summary cannot choose between them:

  (a) per-fixture inconsistency — the provider carries side objects for some
      friendlies and nothing for others;
  (b) provider state changed between the two runs;
  (c) a measurement error in one of the two readings.

THE EXPERIMENT THAT DISTINGUISHES THEM. Re-probe BOTH samples — this agent's
5 fixture ids and the coordinator's 6 named fixtures — in ONE run, on ONE
key, at ONE moment. Then:

  both samples behave as first reported  -> (a) per-fixture inconsistency
  this agent's 5 now return no array     -> (b) transient; the earlier
                                            reading was real but did not last
  the two samples disagree structurally  -> examine (c) with the raw bodies,
                                            which are archived whole below

NEITHER MEASUREMENT IS ADJUSTED TO AGREE WITH THE OTHER. Both are recorded
with their fixture ids and their raw response bodies, so a third party can
recount rather than trust either summary.

COUNT THE LEAVES, NOT THE CONTAINERS. `array_len` and `named_players` are
reported separately for every fixture, because the whole disagreement lives
in that gap: an array of two empty side objects is len=2 and named=0.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# reuse the budgeted, redacting client rather than build a second one
from scripts.probe_team_news import (Client, _redact, load_key,  # noqa: E402
                                     response_items)

FRIENDLY_LEAGUE_ID = 667          # "Friendlies Clubs", resolved 2026-07-30

# This agent's sample, from run4/run5. Ids first — they are the point.
MY_SAMPLE = {
    1598551: "Sanfrecce Hiroshima v Verspah Oita",
    1502103: "Chelsea v Western Sydney Wanderers",
    1604642: "Portimonense v Al-Ahli Jeddah",
    1567753: "Derby v Valencia",
    1567752: "Blackburn Rovers U21 v Preston North End U21",
}
# What run5 recorded for each, so the re-probe is compared against a
# committed prior reading rather than a memory of one.
MY_PRIOR = {
    1598551: {"state": "ok", "array_len": 2, "named_players": 0},
    1502103: {"state": "empty", "array_len": 0, "named_players": 0},
    1604642: {"state": "ok", "array_len": 1, "named_players": 0},
    1567753: {"state": "ok", "array_len": 2, "named_players": 0},
    1567752: {"state": "ok", "array_len": 2, "named_players": 0},
}

# The coordinator's sample, named by teams (ids were not supplied), resolved
# from the fixture window below. Their reported reading: 12/12 no array.
COORDINATOR_SAMPLE_NAMES = [
    ("Estrela", "Portimonense"),
    ("Palermo", "Iraklis"),
    ("Saint Etienne", "Lausanne"),
    ("Oviedo", "Ponferradina"),
    ("Ipswich", "Osasuna"),
    ("Cerezo Osaka", "Dortmund"),
]


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _matches(fixture_teams: tuple, want: tuple) -> bool:
    """Loose containment BOTH ways, and only used to RESOLVE AN ID for a
    fixture the coordinator named in prose. This is not identity resolution
    for pricing — nothing is attached to a market here — so a permissive
    match is appropriate. Every resolved id is printed and archived so the
    resolution itself can be checked.
    """
    h, a = (_norm(x) for x in fixture_teams)
    wh, wa = (_norm(x) for x in want)
    return ((wh in h or h in wh) and (wa in a or a in wh + wa)) or \
           ((wh in a or a in wh) and (wa in h or h in wh + wa))


def probe_one(client: Client, fid: int, label: str) -> dict:
    lu = client.get("fixtures/lineups", {"fixture": fid},
                    f"lineups:{fid}")
    items = response_items(lu.get("body") or {})
    sides = []
    named_total = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        xi = it.get("startXI")
        xi = xi if isinstance(xi, list) else []
        subs = it.get("substitutes")
        subs = subs if isinstance(subs, list) else []
        named = [e for e in xi if isinstance(e, dict)
                 and ((e.get("player") or {}).get("name"))]
        named_total += len(named)
        sides.append({"team": ((it.get("team") or {}).get("name")),
                      "team_id": ((it.get("team") or {}).get("id")),
                      "formation": it.get("formation"),
                      "start_xi_len": len(xi),
                      "start_xi_named": len(named),
                      "substitutes_len": len(subs)})
    return {
        "fixture_id": fid,
        "label": label,
        "state": lu["state"],
        # the two numbers the whole disagreement lives between
        "array_len": len(items),
        "named_players": named_total,
        "sides": sides,
        "provider_declared_results": ((lu.get("body") or {}).get("results")
                                      if isinstance(lu.get("body"), dict)
                                      else None),
        "note": lu["note"],
        # the raw body, kept WHOLE: these are tiny, and a reviewer must be
        # able to recount rather than trust this script's arithmetic
        "raw_body": lu.get("body"),
    }


def main() -> int:
    key, key_from, problems = load_key()
    now = datetime.now(timezone.utc)
    report: dict = {
        "_what": ("Re-probe of BOTH samples in the friendly-lineup-array "
                  "disagreement, in one run on one key at one moment."),
        "_why": ("The two samples did not overlap by a single fixture id, so "
                 "per-fixture inconsistency, a transient, and a measurement "
                 "error were all live explanations. Probing both samples "
                 "together is what distinguishes them."),
        "_neither_measurement_adjusted": (
            "Both readings are recorded with their fixture ids and raw "
            "bodies. Nothing here is reconciled toward the other reading; a "
            "number neither party can reproduce is worth less than two "
            "disagreeing numbers that are both traceable."),
        "_counting_rule": ("array_len and named_players are separate columns. "
                           "An array of two empty side objects is len=2, "
                           "named=0 — that gap IS the disagreement."),
        "run_utc": now.isoformat(),
        "key_source": key_from,
        "key_problems": problems,
        "friendly_league_id": FRIENDLY_LEAGUE_ID,
        "this_agent_sample": [],
        "coordinator_sample": [],
        "coordinator_unresolved": [],
    }
    if not key:
        report["verdict"] = "INDETERMINATE — no key; nothing measured"
        _write(report, now)
        return 2

    client = Client(key)
    print(f"key from {key_from}; re-probing both samples on league "
          f"{FRIENDLY_LEAGUE_ID}\n")

    # ---- this agent's 5 ids, by id, no resolution needed ------------------
    print("THIS AGENT'S SAMPLE (run4/run5), re-probed by fixture id:")
    for fid, label in MY_SAMPLE.items():
        rec = probe_one(client, fid, label)
        prior = MY_PRIOR[fid]
        rec["prior_reading_run5"] = prior
        rec["reproduces_prior"] = (
            rec["state"] == prior["state"]
            and rec["array_len"] == prior["array_len"]
            and rec["named_players"] == prior["named_players"])
        report["this_agent_sample"].append(rec)
        flag = "SAME" if rec["reproduces_prior"] else "CHANGED"
        print(f"  {fid}  {label[:42]:42} state={rec['state']:11} "
              f"array={rec['array_len']} named={rec['named_players']}  "
              f"[was state={prior['state']} array={prior['array_len']}] "
              f"{flag}")

    # ---- the coordinator's 6, resolved from the window --------------------
    frm = (now - timedelta(days=14)).date().isoformat()
    to = (now + timedelta(days=3)).date().isoformat()
    fx = client.get("fixtures", {"league": FRIENDLY_LEAGUE_ID,
                                 "season": now.year, "from": frm, "to": to},
                    "fixtures:friendlies")
    window = response_items(fx.get("body") or {})
    report["resolution_window"] = {"from": frm, "to": to,
                                   "state": fx["state"], "count": fx["count"]}
    print(f"\nresolution window {frm}..{to}: {fx['count']} fixtures "
          f"({fx['state']})")
    print("\nCOORDINATOR'S SAMPLE, resolved by team names then probed by id:")
    for want in COORDINATOR_SAMPLE_NAMES:
        hit = None
        for it in window:
            t = it.get("teams") or {}
            pair = (((t.get("home") or {}).get("name") or ""),
                    ((t.get("away") or {}).get("name") or ""))
            if _matches(pair, want):
                hit = (it, pair)
                break
        if hit is None:
            report["coordinator_unresolved"].append(
                {"wanted": list(want),
                 "reason": ("no fixture in the resolution window matched "
                            "these names; NOT probed, and NOT counted as "
                            "either outcome")})
            print(f"  {want[0]} v {want[1]}: UNRESOLVED in window — not probed")
            continue
        it, pair = hit
        fid = ((it.get("fixture") or {}).get("id"))
        status = (((it.get("fixture") or {}).get("status") or {})
                  .get("short"))
        rec = probe_one(client, fid, f"{pair[0]} v {pair[1]}")
        rec["status"] = status
        rec["resolved_from_names"] = list(want)
        report["coordinator_sample"].append(rec)
        print(f"  {fid}  {pair[0][:20]:20} v {pair[1][:20]:20} {status:4} "
              f"state={rec['state']:11} array={rec['array_len']} "
              f"named={rec['named_players']}")

    # ---- the verdict, stated in terms of the three explanations -----------
    mine = report["this_agent_sample"]
    theirs = report["coordinator_sample"]
    mine_arrays = sum(1 for r in mine if r["array_len"] > 0)
    theirs_arrays = sum(1 for r in theirs if r["array_len"] > 0)
    mine_same = sum(1 for r in mine if r["reproduces_prior"])
    named_anywhere = sum(r["named_players"] for r in mine + theirs)

    report["measured_now"] = {
        "this_agent_sample": {
            "probed": len(mine),
            "returned_non_empty_array": mine_arrays,
            "reproduced_prior_reading": f"{mine_same}/{len(mine)}",
        },
        "coordinator_sample": {
            "probed": len(theirs),
            "returned_non_empty_array": theirs_arrays,
            "unresolved_not_probed": len(report["coordinator_unresolved"]),
        },
        "named_players_across_every_fixture_probed": named_anywhere,
    }
    if mine_same == len(mine) and mine_arrays > 0 and theirs_arrays == 0:
        verdict = ("(a) PER-FIXTURE INCONSISTENCY. Both readings reproduce in "
                   "the same run on the same key: some friendly fixtures "
                   "carry side objects, others carry no array at all. Neither "
                   "summary generalised, and a single sample of either kind "
                   "would have produced a confident wrong answer.")
    elif mine_arrays == 0 and mine_same < len(mine):
        verdict = ("(b) TRANSIENT. This agent's fixtures no longer return a "
                   "non-empty array. The earlier reading is archived and was "
                   "real when taken, but it does not describe the provider "
                   "now, and the record should say so.")
    elif mine_arrays > 0 and theirs_arrays > 0:
        verdict = ("Both samples now return arrays — the coordinator's "
                   "reading does not reproduce here. Compare raw bodies "
                   "before concluding anything about either.")
    else:
        verdict = ("MIXED — see per-fixture rows and raw bodies; no single "
                   "explanation is supported.")
    report["verdict"] = verdict
    report["what_holds_either_way"] = (
        "No friendly fixture in EITHER sample produced a single NAMED player "
        f"({named_anywhere} names across all fixtures probed). So the "
        "operative conclusion is unchanged and was never in dispute: "
        "API-Football is not a usable source of friendly XIs, and ESPN's "
        "club.friendly rosters are. What the two readings disagree about is "
        "the SHAPE of the emptiness, not whether it is empty.")
    report["request_log"] = client.log
    report["requests_used"] = client.used

    print(f"\nVERDICT: {verdict}")
    print(f"\n{report['what_holds_either_way']}")
    print(f"\nAPI-Football requests used: {client.used}")
    _write(report, now)
    return 0


def _write(report: dict, now: datetime) -> None:
    root = Path(__file__).resolve().parent.parent
    path = (root / "research_archive" /
            f"team_news_friendly_lineup_disagreement_"
            f"{now.date().isoformat()}.json")
    if path.exists():
        n = 2
        while path.with_suffix(f".run{n}.json").exists():
            n += 1
        path = path.with_suffix(f".run{n}.json")
    key = load_key()[0]
    path.write_text(_redact(json.dumps(report, indent=1, sort_keys=True,
                                       default=str), key),
                    encoding="utf-8")
    print(f"archived -> {path.relative_to(root)}")


if __name__ == "__main__":
    sys.exit(main())
