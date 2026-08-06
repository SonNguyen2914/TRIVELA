#!/usr/bin/env python3
"""Turn pre-kickoff fixture snapshots into the clean arm's corpus.

    python scripts/build_prospective_corpus.py \
        --snapshot pre_md1.json --snapshot pre_md2.json \
        --out research_archive/leagues_cup_prospective_2026.json

WHY THIS EXISTS. `scripts/measure_league_offset.py` has a contaminated
retrospective arm and a clean prospective one, and the clean arm has
refused every time it has run — not for want of fixtures but for want
of a file. It needs reads that DEMONSTRABLY existed before kickoff,
because that is the only thing separating it from the retrospective arm
it must never silently become.

This builds that file. It does not measure anything and draws no
conclusion; it is the plumbing between the T-60 briefings, which
already capture pre-kickoff reads, and the measurement, which cannot
use them while they sit in scratch files.

WHAT MAKES A ROW ADMISSIBLE, checked rather than assumed. Every row
must clear BOTH:

  1. the snapshot says the fixture had NOT started — `status` in
     {NS, TBD} with no goals recorded. A read taken beside a live
     scoreline is not a pre-kickoff read;
  2. the snapshot FILE was written before the fixture's kickoff. The
     payload carries no capture time, so mtime is the only independent
     witness, and a witness that can only ever be too LATE is the safe
     direction: a file written after kickoff is rejected even if its
     contents look pre-match.

Condition 1 alone is not enough, and this slate proved why — on
2026-08-05 the provider reported `NS` for two fixtures six minutes
past their kickoff, and reported `PST` for two others that went on to
play. Fixture status from this feed is a claim, not a fact. Condition 2
does not depend on the feed at all.

A fixture appearing in several snapshots keeps the LATEST admissible
read — the one closest to kickoff while still strictly before it. That
is the read the T-60 briefing published and the operator decided
against, so it is the read any correction would have to correct. Taking
the earliest instead would build the corpus out of numbers nobody acted
on: a first pass over this slate's scratch files paired outcomes with
reads 18 to 45 hours stale, which measures a different quantity.

Latest is safe here only because admissibility does not rest on the
feed: condition 2 rejects any snapshot file written at or after
kickoff, so "closest to kickoff" can never cross it.

Outcomes come from a settled snapshot or from the live API. Only
fixtures that have finished contribute a row; anything unsettled is
counted and named, never dropped silently.

This writes a corpus. It does not decide whether the T-60 briefing
should write one automatically each slate — that question is open with
the coordinator, and this script works either way.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A fixture that has produced a 90-minute result. PEN belongs here: in
# this competition a shootout follows a 90-minute DRAW, and the stored
# goals are the 90-minute ones — the same convention
# scripts/measure_leagues_cup_history.py counts. AET does not: its
# goals include extra time, so the 90-minute result is unrecoverable.
SETTLED = {"FT", "PEN"}
NOT_STARTED = {"NS", "TBD"}


def _utc(ts: str | None):
    if not ts:
        return None
    t = ts.replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _read_of(fx: dict):
    """The read as the viewer surface publishes it: the home side's
    expected points share. None where the strength read is unavailable
    — which costs coverage, never accuracy."""
    s = fx.get("strength") or {}
    if not s.get("available"):
        return None
    return ((s.get("expected_points_share") or {}).get("home"))


def _key(fx: dict) -> str:
    """Provider-stable identity. Never names, never name+date —
    AGENTS.md section 13. A fixture without one is refused rather than
    matched loosely."""
    fid = fx.get("fixture_id")
    return str(fid) if fid is not None else ""


def collect_pre(paths: list[str]) -> tuple[dict, list]:
    """Earliest admissible pre-kickoff read per fixture, plus the
    reasons every rejected row was rejected."""
    best, rejects = {}, []
    for p in paths:
        path = Path(p)
        captured = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        doc = json.loads(path.read_text())
        for fx in (doc.get("fixtures") or []):
            k = _key(fx)
            ko = _utc(fx.get("kickoff_utc"))
            name = (f"{(fx.get('home') or {}).get('name')} v "
                    f"{(fx.get('away') or {}).get('name')}")
            if not k or ko is None:
                rejects.append({"file": path.name, "fixture": name,
                                "why": "no provider fixture id or kickoff"})
                continue
            g = fx.get("goals") or {}
            if fx.get("status") not in NOT_STARTED or g.get("home") is not None:
                rejects.append({"file": path.name, "fixture": name,
                                "why": f"snapshot status {fx.get('status')!r}"
                                       " is not pre-kickoff"})
                continue
            if captured >= ko:
                rejects.append({"file": path.name, "fixture": name,
                                "why": f"file written {captured.isoformat()} "
                                       f"at or after kickoff {ko.isoformat()}"})
                continue
            r = _read_of(fx)
            if r is None:
                rejects.append({"file": path.name, "fixture": name,
                                "why": "no strength read in this snapshot"})
                continue
            prior = best.get(k)
            if prior and prior["captured_at"] >= captured.isoformat():
                continue
            best[k] = {
                "fixture_id": k,
                "home": {"name": (fx.get("home") or {}).get("name")},
                "away": {"name": (fx.get("away") or {}).get("name")},
                "_read": r,
                "kickoff_utc": ko.isoformat(),
                "captured_at": captured.isoformat(),
                "source_file": path.name,
                "lead_seconds": int((ko - captured).total_seconds()),
            }
    return best, rejects


def outcomes(path: str | None, api_base: str | None) -> dict:
    if path:
        doc = json.loads(Path(path).read_text())
    else:
        u = (f"{api_base}/api/comp/leagues-cup/fixtures"
             "?days=1&include_finished=true")
        doc = json.load(urllib.request.urlopen(u))
    return {_key(f): f for f in (doc.get("fixtures") or []) if _key(f)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", action="append", required=True,
                    help="pre-kickoff fixtures payload; repeatable")
    ap.add_argument("--results", default=None,
                    help="settled fixtures payload (default: live API)")
    ap.add_argument("--api-base", default=os.getenv("TRIVELA_API_BASE"))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    if not a.results and not a.api_base:
        print(json.dumps({"error": "no_results_source", "built": False,
                          "means": "pass --results or set TRIVELA_API_BASE"}))
        return 2

    pre, rejects = collect_pre(a.snapshot)
    res = outcomes(a.results, a.api_base)

    rows, pending, missing = [], [], []
    for k, snap in sorted(pre.items()):
        fx = res.get(k)
        if fx is None:
            missing.append(snap["home"]["name"] + " v " + snap["away"]["name"])
            continue
        if fx.get("status") not in SETTLED:
            pending.append({"fixture": snap["home"]["name"] + " v "
                            + snap["away"]["name"], "status": fx.get("status")})
            continue
        g = fx.get("goals") or {}
        if g.get("home") is None or g.get("away") is None:
            pending.append({"fixture": snap["home"]["name"] + " v "
                            + snap["away"]["name"],
                            "status": f"{fx.get('status')} without a score"})
            continue
        row = dict(snap)
        row["goals"] = {"home": g["home"], "away": g["away"]}
        row["final_status"] = fx.get("status")
        rows.append(row)

    doc = {
        "what_this_is": (
            "pre-kickoff reads paired with settled 90-minute outcomes. The "
            "clean arm of scripts/measure_league_offset.py consumes this. "
            "Every row's read DEMONSTRABLY predates its kickoff: the "
            "snapshot said the fixture had not started AND the snapshot "
            "file was written before kickoff"),
        "what_this_is_NOT": (
            "not an edge claim, not a calibration, not a correction, and "
            "not a performance record. It is paired inputs and outcomes; "
            "the measurement lives in the script that reads it"),
        "admissibility": {
            "status_in_snapshot": sorted(NOT_STARTED),
            "file_mtime_before_kickoff": True,
            "why_both": (
                "this feed reported NS six minutes past kickoff and PST for "
                "matches that then played, on 2026-08-05 — its status is a "
                "claim. The file's mtime does not depend on the feed"),
            "settled_statuses": sorted(SETTLED),
            "why_not_AET": (
                "AET goals include extra time, so the 90-minute result the "
                "read and the market both answer is unrecoverable"),
            "duplicate_policy": (
                "latest admissible read per fixture wins — the one closest "
                "to kickoff while still before it, which is the read the "
                "briefing published and the operator decided against"),
        },
        "n_rows": len(rows),
        "n_pre_kickoff_reads_collected": len(pre),
        "pending_not_yet_settled": pending,
        "in_snapshot_but_absent_from_results": missing,
        "rejected_from_snapshots": rejects,
        "rejected_count": len(rejects),
        "rows": rows,
    }
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"wrote {a.out}: {len(rows)} rows, {len(rejects)} rejected, "
              f"{len(pending)} pending", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
